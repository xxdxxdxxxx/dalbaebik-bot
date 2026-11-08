"""Regression tests: /add nickname conflicts and roster edits lost to stale snapshots."""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from contextlib import ExitStack, closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import bot
import player_store
from tools import roster_board  # noqa: F401  (also installs the snapshot wrappers)

GUILD = 42


def make_db(td):
    db = Path(td) / "roster.sqlite3"
    player_store.ensure_schema(db)
    player_store.upsert_guild_config(db, GUILD)
    return db


def bind(db, discord_id, nick, squad=None, slot=None):
    pid = player_store.upsert_binding(db, discord_id, nick, f"d{discord_id}", f"u{discord_id}", GUILD)
    player_store.upsert_roster_member(db, GUILD, pid, squad_id=squad, slot=slot)
    return pid


def unbound(db, nick, squad=None, slot=None):
    with closing(player_store.connect(db)) as con:
        con.execute("INSERT INTO players(canonical_nick) VALUES(?)", (nick,))
        pid = int(con.execute("SELECT last_insert_rowid()").fetchone()[0])
        con.execute("INSERT INTO player_guilds(player_id,guild_id) VALUES(?,?)", (pid, GUILD))
    if squad is not None:
        player_store.upsert_roster_member(db, GUILD, pid, squad_id=squad, slot=slot)
    return pid


def admin_set(db, squad, slot, text):
    """What the admin board does when a cell is edited."""
    admin = SimpleNamespace(player_store=player_store)
    with closing(player_store.connect(db)) as con:
        con.execute("BEGIN IMMEDIATE")
        roster_board._seed(con, GUILD)
        roster_board._set_cell(admin, con, GUILD, squad, slot, text)
        con.commit()


def admin_swap(db, left, right):
    """The real /squads/cells/swap endpoint of the admin panel."""
    from tools import admin_ui
    with patch.dict("os.environ", {}, clear=False):
        os.environ.pop("ADMIN_PASSWORD", None)
        os.environ.pop("ADMIN_TRUST_PROXY", None)
        with patch.object(admin_ui, "DB_PATH", db):
            client = admin_ui.app.test_client()
            return client.post("/squads/cells/swap", base_url="http://localhost:8787", data={
                "a_squad": left[0], "a_slot": left[1], "b_squad": right[0], "b_slot": right[1]})


def positions(db):
    with closing(player_store.connect(db)) as con:
        return {
            row["canonical_nick"]: (row["squad_id"], row["slot"]) if row["active"] else None
            for row in con.execute("""SELECT p.canonical_nick,r.squad_id,r.slot,r.active
                FROM roster_memberships r JOIN players p ON p.id=r.player_id WHERE r.guild_id=?""", (GUILD,))
        }


def fk_clean(db):
    with closing(player_store.connect(db)) as con:
        return not con.execute("PRAGMA foreign_key_check").fetchall()


class StaleSnapshotTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.db = make_db(self._td.name)
        bind(self.db, "100", "Alpha", 1, 1)
        bind(self.db, "200", "Beta", 1, 2)
        bind(self.db, "300", "Gamma", 2, 1)

    def test_admin_move_survives_stale_save(self):
        stale = player_store.load_bot_snapshot(self.db, GUILD)
        admin_set(self.db, 1, 3, "Beta")
        stale["message_ids"]["online"] = 777
        player_store.save_bot_snapshot(self.db, stale, GUILD)
        self.assertEqual(positions(self.db)["Beta"], (1, 3))
        self.assertEqual(player_store.load_bot_snapshot(self.db, GUILD)["message_ids"]["online"], 777)

    def test_admin_clear_survives_stale_save(self):
        stale = player_store.load_bot_snapshot(self.db, GUILD)
        admin_set(self.db, 2, 1, "")
        player_store.save_bot_snapshot(self.db, stale, GUILD)
        self.assertIsNone(positions(self.db)["Gamma"])
        self.assertNotIn("300", player_store.load_bot_snapshot(self.db, GUILD)["players"])

    def test_admin_replacement_and_new_member_survive_stale_save(self):
        stale = player_store.load_bot_snapshot(self.db, GUILD)
        admin_set(self.db, 1, 1, "Newcomer")      # replaces Alpha
        admin_set(self.db, 3, 2, "Another")        # brand new cell
        player_store.save_bot_snapshot(self.db, stale, GUILD)
        pos = positions(self.db)
        self.assertEqual(pos["Newcomer"], (1, 1))
        self.assertEqual(pos["Another"], (3, 2))
        self.assertIsNone(pos["Alpha"])

    def test_admin_swap_survives_stale_save(self):
        stale = player_store.load_bot_snapshot(self.db, GUILD)
        self.assertEqual(admin_swap(self.db, (1, 1), (2, 1)).status_code, 204)
        player_store.save_bot_snapshot(self.db, stale, GUILD)
        pos = positions(self.db)
        self.assertEqual((pos["Alpha"], pos["Gamma"]), ((2, 1), (1, 1)))

    def test_repeated_saves_of_the_same_object_keep_admin_edits(self):
        stale = player_store.load_bot_snapshot(self.db, GUILD)
        player_store.save_bot_snapshot(self.db, stale, GUILD)
        admin_set(self.db, 1, 3, "Beta")
        player_store.save_bot_snapshot(self.db, stale, GUILD)
        player_store.save_bot_snapshot(self.db, stale, GUILD)
        self.assertEqual(positions(self.db)["Beta"], (1, 3))
        # The in-memory object follows the database, so later status renders agree.
        self.assertEqual((stale["players"]["200"]["squad"], stale["players"]["200"]["slot"]), (1, 3))

    def test_bot_side_changes_still_apply(self):
        data = player_store.load_bot_snapshot(self.db, GUILD)
        data["players"].pop("100")                              # /remove
        data["players"]["400"] = {"game_nick": "Delta", "discord_name": "d", "discord_username": "d",
                                  "squad": None, "slot": None}  # /add
        data["players"]["300"].update(squad=1, slot=2)          # explicit move wins over Beta
        player_store.save_bot_snapshot(self.db, data, GUILD)
        pos = positions(self.db)
        self.assertIsNone(pos["Alpha"])
        self.assertEqual(pos["Delta"], (None, None))
        self.assertEqual(pos["Gamma"], (1, 2))
        self.assertIsNone(pos["Beta"])
        self.assertTrue(fk_clean(self.db))

    def test_player_added_on_board_is_not_dropped_by_stale_removal_of_others(self):
        stale = player_store.load_bot_snapshot(self.db, GUILD)
        admin_set(self.db, 4, 1, "BoardOnly")
        stale["players"].pop("200")
        player_store.save_bot_snapshot(self.db, stale, GUILD)
        pos = positions(self.db)
        self.assertEqual(pos["BoardOnly"], (4, 1))
        self.assertIsNone(pos["Beta"])
        self.assertEqual(pos["Alpha"], (1, 1))

    def test_hand_built_data_without_base_keeps_old_semantics(self):
        data = {"config": {"guild_id": GUILD}, "players": {
            "100": {"game_nick": "Alpha", "squad": 2, "slot": 2}}}
        player_store.save_bot_snapshot(self.db, data, GUILD)
        pos = positions(self.db)
        self.assertEqual(pos["Alpha"], (2, 2))
        self.assertIsNone(pos["Beta"])


class NicknameMergeTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.db = make_db(self._td.name)

    def rename(self, discord_id, nick):
        data = player_store.load_bot_snapshot(self.db, GUILD)
        data["players"][discord_id]["game_nick"] = nick
        player_store.save_bot_snapshot(self.db, data, GUILD)
        return data

    def test_add_with_nick_of_unbound_board_player_merges_instead_of_failing(self):
        bound = bind(self.db, "100", "sawerq", 1, 1)
        other = unbound(self.db, "sawerqe", 2, 2)
        with closing(player_store.connect(self.db)) as con:
            con.execute("INSERT INTO scans(id,map,user_id,scanned_at,source_filename) VALUES(1,'m',1,'2026-11-05','f')")
            con.execute("""INSERT INTO scan_players(scan_id,place,nick,kills,deaths,assists,player_id)
                VALUES(1,1,'sawerqe',5,1,2,?)""", (other,))
            con.execute("INSERT INTO runtime_sessions(id,guild_id,session_date,active) VALUES(7,?,'2026-11-05',0)", (GUILD,))
            con.execute("INSERT INTO runtime_sessions(id,guild_id,session_date,active) VALUES(8,?,'2026-11-06',0)", (GUILD,))
            con.execute("INSERT INTO session_roster(session_id,player_id,game_nick) VALUES(7,?,'sawerqe')", (other,))
            con.execute("INSERT INTO attendance(session_id,player_id,came) VALUES(7,?,1)", (other,))
            con.execute("INSERT INTO live_grenade_state(session_id,player_id,step_id,value) VALUES(7,?,'20:00',9)", (other,))
            con.execute("INSERT INTO grenade_stats(player_id,stat_key,value) VALUES(?,'20:00',9)", (other,))

        self.rename("100", "sawerqe")      # used to raise ValueError: belongs to another player

        with closing(player_store.connect(self.db)) as con:
            self.assertEqual(con.execute("SELECT canonical_nick FROM players WHERE id=?", (bound,)).fetchone()[0], "sawerqe")
            self.assertIsNone(con.execute("SELECT 1 FROM players WHERE id=?", (other,)).fetchone())
            self.assertEqual(con.execute("SELECT player_id FROM scan_players WHERE scan_id=1").fetchone()[0], bound)
            self.assertEqual(con.execute("SELECT player_id FROM session_roster WHERE session_id=7").fetchone()[0], bound)
            self.assertEqual(con.execute("SELECT came FROM attendance WHERE player_id=?", (bound,)).fetchone()[0], 1)
            self.assertEqual(con.execute("SELECT value FROM grenade_stats WHERE player_id=?", (bound,)).fetchone()[0], 9)
            self.assertEqual(player_store.resolve_player_id(self.db, "sawerq"), bound)
            self.assertEqual(player_store.resolve_player_id(self.db, "sawerqe"), bound)
        self.assertTrue(fk_clean(self.db))
        # The bound player keeps its own place; the board copy is gone.
        self.assertEqual(positions(self.db), {"sawerqe": (1, 1)})

    def test_player_without_place_takes_the_board_position(self):
        bind(self.db, "100", "sawerq")
        unbound(self.db, "sawerqe", 2, 2)
        data = self.rename("100", "sawerqe")
        self.assertEqual(positions(self.db), {"sawerqe": (2, 2)})
        self.assertEqual((data["players"]["100"]["squad"], data["players"]["100"]["slot"]), (2, 2))

    def test_alias_owner_is_merged_and_its_own_name_stays_resolvable(self):
        bound = bind(self.db, "100", "Mainnick", 1, 1)
        other = unbound(self.db, "Foo")
        with closing(player_store.connect(self.db)) as con:
            con.execute("INSERT INTO player_aliases(alias,player_id) VALUES('sawerqe',?)", (other,))
        self.rename("100", "sawerqe")
        self.assertEqual(player_store.resolve_player_id(self.db, "Foo"), bound)
        self.assertEqual(player_store.resolve_player_id(self.db, "Mainnick"), bound)
        self.assertEqual(player_store.resolve_player_id(self.db, "sawerqe"), bound)
        self.assertTrue(fk_clean(self.db))

    def test_nick_bound_to_another_discord_user_is_still_refused(self):
        bind(self.db, "100", "Mine", 1, 1)
        other = bind(self.db, "200", "sawerqe", 1, 2)
        data = player_store.load_bot_snapshot(self.db, GUILD)
        data["players"].pop("200")          # the other user is no longer in this snapshot
        data["players"]["100"]["game_nick"] = "sawerqe"
        with self.assertRaises(player_store.NicknameConflict) as ctx:
            player_store.save_bot_snapshot(self.db, data, GUILD)
        self.assertEqual(ctx.exception.discord_id, "200")
        self.assertIsInstance(ctx.exception, ValueError)
        with closing(player_store.connect(self.db)) as con:    # nothing was half-applied
            self.assertIsNotNone(con.execute("SELECT 1 FROM players WHERE id=?", (other,)).fetchone())
            self.assertEqual(con.execute("SELECT canonical_nick FROM players WHERE canonical_nick='Mine'").fetchone()[0], "Mine")

    def test_schema_check_merges_duplicate_that_owns_roster_rows(self):
        """Used to raise FOREIGN KEY failed on every load/save, stopping the whole bot."""
        a = bind(self.db, "100", "Alpha", 1, 1)
        with closing(player_store.connect(self.db)) as con:
            con.execute("INSERT INTO players(canonical_nick) VALUES('Elsewhere')")
            other = int(con.execute("SELECT last_insert_rowid()").fetchone()[0])
            con.execute("""INSERT INTO roster_memberships(guild_id,player_id,squad_id,slot,active)
                VALUES(?,?,2,1,1)""", (GUILD, other))
            con.execute("INSERT INTO player_aliases(alias,player_id) VALUES('alpha',?)", (other,))
        player_store.ensure_schema(self.db)
        with closing(player_store.connect(self.db)) as con:
            self.assertIsNone(con.execute("SELECT 1 FROM players WHERE id=?", (other,)).fetchone())
        self.assertEqual(player_store.resolve_player_id(self.db, "Elsewhere"), a)
        self.assertTrue(fk_clean(self.db))
        self.assertEqual(positions(self.db), {"Alpha": (1, 1)})

    def test_stale_snapshot_does_not_undo_a_rename_done_in_between(self):
        bind(self.db, "100", "OldNick", 1, 1)
        stale = player_store.load_bot_snapshot(self.db, GUILD)
        self.rename("100", "NewNick")
        player_store.save_bot_snapshot(self.db, stale, GUILD)
        self.assertEqual(set(positions(self.db)), {"NewNick"})


class AddCommandTests(unittest.IsolatedAsyncioTestCase):
    def member(self, member_id, name):
        guild = SimpleNamespace(id=GUILD)
        return SimpleNamespace(id=member_id, bot=False, display_name=name, name=name, guild=guild,
                               mention=f"<@{member_id}>")

    async def run_add(self, db, target, nick):
        with ExitStack() as stack:
            stack.enter_context(patch.object(bot, "PLAYER_DB_PATH", db))
            stack.enter_context(patch.object(bot, "db_lock", asyncio.Lock()))
            stack.enter_context(patch.object(bot, "can_use_roster", return_value=True))
            stack.enter_context(patch.object(bot, "upsert_status_messages", new=AsyncMock()))
            stack.enter_context(patch.object(bot, "is_kv_live", return_value=False))
            stack.enter_context(patch.object(bot, "reindex_scan_histories"))
            actor = SimpleNamespace(guild=target.guild)
            return await bot.do_roster_add(actor, target, nick)

    async def test_add_existing_member_with_nick_known_from_the_board(self):
        with tempfile.TemporaryDirectory() as td:
            db = make_db(td)
            bound = bind(db, "100", "sawerq", 1, 1)
            unbound(db, "sawerqe", 2, 2)
            ok, embed = await self.run_add(db, self.member(100, "Sawer"), "sawerqe")
            self.assertTrue(ok, embed.description)
            self.assertEqual(player_store.resolve_player_id(db, "sawerqe"), bound)
            self.assertEqual(positions(db), {"sawerqe": (1, 1)})
            self.assertTrue(fk_clean(db))

    async def test_add_nick_owned_by_other_discord_user_gets_a_readable_answer(self):
        with tempfile.TemporaryDirectory() as td:
            db = make_db(td)
            bind(db, "100", "Mine", 1, 1)
            bind(db, "200", "sawerqe", 1, 2)
            with closing(player_store.connect(db)) as con:   # other user left the active roster
                con.execute("UPDATE roster_memberships SET active=0 WHERE player_id=(SELECT player_id FROM discord_bindings WHERE discord_id='200')")
            ok, embed = await self.run_add(db, self.member(100, "Me"), "sawerqe")
            self.assertFalse(ok)
            self.assertIn("<@200>", embed.description)


if __name__ == "__main__":
    unittest.main()
