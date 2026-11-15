"""Regression tests: nickname claim on /add, alias promotion, and full /remove unbinding."""
from __future__ import annotations

import asyncio
import sqlite3
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
NICK = "Пельмешек"


def make_db(td):
    db = Path(td) / "unbind.sqlite3"
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


def sql(db, query, *args):
    with closing(player_store.connect(db)) as con:
        return [tuple(row) for row in con.execute(query, args)]


def add_alias(db, pid, alias):
    sql(db, "INSERT INTO player_aliases(alias,player_id) VALUES(?,?)", alias, pid)


def left_roster(db, pid):
    """What the old /remove left behind: inactive membership + tombstone, binding untouched."""
    player_store.mark_roster_removed(db, GUILD, player_id=pid)


def add_scan(db, pid, nick):
    sql(db, "INSERT INTO scans(id,map,user_id,scanned_at,source_filename) VALUES(1,'m',1,'2026-11-05','f')")
    sql(db, """INSERT INTO scan_players(scan_id,place,nick,kills,deaths,assists,player_id)
        VALUES(1,1,?,5,1,2,?)""", nick, pid)


def snapshot_add(db, discord_id, nick):
    """What /add does to the runtime snapshot before it is saved."""
    data = player_store.load_bot_snapshot(db, GUILD)
    data["players"][str(discord_id)] = {
        "discord_name": f"d{discord_id}", "discord_username": f"u{discord_id}", "game_nick": nick,
        "came": False, "in_voice": False, "squad": None, "slot": None,
    }
    player_store.save_bot_snapshot(db, data, GUILD)
    return data


def bindings(db, pid=None):
    if pid is None:
        return dict(sql(db, "SELECT discord_id,player_id FROM discord_bindings"))
    return sorted(row[0] for row in sql(db, "SELECT discord_id FROM discord_bindings WHERE player_id=?", pid))


def canonical(db, pid):
    return sql(db, "SELECT canonical_nick FROM players WHERE id=?", pid)[0][0]


def aliases(db, pid):
    return sorted(row[0] for row in sql(db, "SELECT alias FROM player_aliases WHERE player_id=?", pid))


def membership(db, pid):
    rows = sql(db, "SELECT active FROM roster_memberships WHERE guild_id=? AND player_id=?", GUILD, pid)
    return None if not rows else bool(rows[0][0])


def removals(db, pid):
    return sql(db, "SELECT 1 FROM roster_removals WHERE guild_id=? AND player_id=?", GUILD, pid)


class ClaimNickOnAddTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.db = make_db(self._td.name)

    def test_nick_of_removed_player_is_reused_by_a_new_discord_account(self):
        """/add @other <nick> used to say «Ник уже занят» for a player who had left the roster."""
        old = bind(self.db, "100", NICK, 1, 1)
        add_scan(self.db, old, NICK)
        left_roster(self.db, old)                       # binding to @100 stays behind

        snapshot_add(self.db, "200", NICK)              # used to raise NicknameConflict

        self.assertEqual(bindings(self.db), {"200": old})   # one player, one Discord account
        self.assertEqual(sql(self.db, "SELECT COUNT(*) FROM players")[0][0], 1)
        self.assertEqual(sql(self.db, "SELECT player_id FROM scan_players")[0][0], old)   # history kept
        self.assertEqual(removals(self.db, old), [])
        self.assertTrue(membership(self.db, old))

    def test_new_account_does_not_get_a_second_binding_on_an_existing_player(self):
        """The binding-is-None branch used to attach a second Discord to the same player."""
        old = bind(self.db, "100", NICK)
        player_store.deactivate_roster_member(self.db, GUILD, old)   # not removed explicitly, just inactive
        snapshot_add(self.db, "200", NICK)
        self.assertEqual(bindings(self.db, old), ["200"])

    def test_active_member_keeps_the_nick_for_a_new_account(self):
        owner = bind(self.db, "100", NICK, 1, 1)
        with self.assertRaises(player_store.NicknameConflict) as ctx:
            snapshot_add(self.db, "300", NICK)
        self.assertEqual(ctx.exception.discord_id, "100")
        self.assertEqual(bindings(self.db), {"100": owner})          # nothing half-applied
        self.assertEqual(sql(self.db, "SELECT COUNT(*) FROM players")[0][0], 1)
        self.assertTrue(membership(self.db, owner))

    def test_active_member_keeps_the_nick_for_an_account_that_already_has_a_player(self):
        bind(self.db, "100", "Mine", 1, 1)
        owner = bind(self.db, "200", NICK, 1, 2)
        data = player_store.load_bot_snapshot(self.db, GUILD)
        data["players"].pop("200")
        data["players"]["100"]["game_nick"] = NICK
        with self.assertRaises(player_store.NicknameConflict) as ctx:
            player_store.save_bot_snapshot(self.db, data, GUILD)
        self.assertEqual(ctx.exception.discord_id, "200")
        self.assertEqual(bindings(self.db, owner), ["200"])

    def test_renaming_into_the_nick_of_a_removed_player_merges_and_drops_its_binding(self):
        mine = bind(self.db, "200", "Mine", 1, 1)
        old = bind(self.db, "100", NICK, 1, 2)
        add_scan(self.db, old, NICK)
        left_roster(self.db, old)

        data = player_store.load_bot_snapshot(self.db, GUILD)
        data["players"]["200"]["game_nick"] = NICK
        player_store.save_bot_snapshot(self.db, data, GUILD)

        self.assertEqual(bindings(self.db), {"200": mine})
        self.assertEqual(canonical(self.db, mine), NICK)
        self.assertIn("Mine", aliases(self.db, mine))
        self.assertEqual(sql(self.db, "SELECT player_id FROM scan_players")[0][0], mine)
        self.assertEqual(removals(self.db, mine), [])
        self.assertEqual(sql(self.db, "SELECT COUNT(*) FROM players")[0][0], 1)
        self.assertEqual(sql(self.db, "PRAGMA foreign_key_check"), [])

    def test_player_without_discord_who_is_on_the_board_is_still_merged(self):
        board = unbound(self.db, NICK, 2, 2)
        snapshot_add(self.db, "200", NICK)
        self.assertEqual(bindings(self.db), {"200": board})
        self.assertTrue(membership(self.db, board))

    def test_several_stale_owners_are_merged_into_one_player(self):
        first = bind(self.db, "100", "Foo")
        second = bind(self.db, "101", "Bar")
        add_alias(self.db, second, "Foo")                # "Foo" is canonical of one, spare of the other
        for pid in (first, second):
            player_store.deactivate_roster_member(self.db, GUILD, pid)
        snapshot_add(self.db, "200", "Foo")
        self.assertEqual(sql(self.db, "SELECT COUNT(*) FROM players")[0][0], 1)
        self.assertEqual(bindings(self.db), {"200": first})
        self.assertEqual(sql(self.db, "PRAGMA foreign_key_check"), [])


class SpareNickPromotionTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.db = make_db(self._td.name)

    def test_add_by_a_spare_nick_makes_it_the_main_one(self):
        pid = bind(self.db, "100", "OldMain", 1, 1)
        add_alias(self.db, pid, "NewMain")
        left_roster(self.db, pid)

        snapshot_add(self.db, "200", "NewMain")

        self.assertEqual(canonical(self.db, pid), "NewMain")
        self.assertEqual(aliases(self.db, pid), ["OldMain"])          # old main stays, the promoted one is gone
        self.assertEqual(bindings(self.db), {"200": pid})
        self.assertEqual(player_store.resolve_player_id(self.db, "OldMain"), pid)

    def test_rename_to_an_own_spare_nick_swaps_main_and_spare(self):
        pid = bind(self.db, "100", "Main", 1, 1)
        add_alias(self.db, pid, "Spare")
        data = player_store.load_bot_snapshot(self.db, GUILD)
        data["players"]["100"]["game_nick"] = "Spare"
        player_store.save_bot_snapshot(self.db, data, GUILD)
        self.assertEqual(canonical(self.db, pid), "Spare")
        self.assertEqual(aliases(self.db, pid), ["Main"])

    def test_a_nick_that_is_only_spelled_differently_is_not_turned_into_an_alias(self):
        pid = bind(self.db, "100", NICK)
        player_store.deactivate_roster_member(self.db, GUILD, pid)
        snapshot_add(self.db, "200", NICK.lower())
        self.assertEqual(aliases(self.db, pid), [])
        self.assertEqual(bindings(self.db), {"200": pid})


class UnbindDiscordTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.db = make_db(self._td.name)

    def test_removes_from_roster_and_drops_all_bindings_but_keeps_history(self):
        pid = bind(self.db, "100", NICK, 1, 1)
        sql(self.db, "INSERT INTO discord_bindings(discord_id,player_id) VALUES('101',?)", pid)   # legacy double binding
        add_alias(self.db, pid, "Spare")
        add_scan(self.db, pid, NICK)

        result = player_store.unbind_discord_everywhere(self.db, GUILD, 101)    # int ids are accepted

        self.assertEqual(result.status, "removed")
        self.assertEqual((result.player_id, result.nick, result.was_active), (pid, NICK, True))
        self.assertEqual(result.discord_ids, ("101", "100"))        # the requested account first
        self.assertEqual(bindings(self.db), {})
        self.assertFalse(membership(self.db, pid))
        self.assertEqual(len(removals(self.db, pid)), 1)
        self.assertEqual(aliases(self.db, pid), ["Spare"])
        self.assertEqual(sql(self.db, "SELECT player_id FROM scan_players")[0][0], pid)
        self.assertEqual(canonical(self.db, pid), NICK)
        self.assertNotIn("100", player_store.load_bot_snapshot(self.db, GUILD)["players"])

    def test_account_outside_the_roster_is_unbound_too(self):
        pid = bind(self.db, "100", NICK, 1, 1)
        player_store.deactivate_roster_member(self.db, GUILD, pid)      # bound, but not in the active roster
        result = player_store.unbind_discord_everywhere(self.db, GUILD, "100")
        self.assertEqual((result.status, result.discord_ids, result.was_active), ("removed", ("100",), False))
        self.assertEqual(bindings(self.db), {})
        self.assertEqual(len(removals(self.db, pid)), 1)

    def test_account_without_a_membership_row_is_unbound_too(self):
        pid = player_store.upsert_binding(self.db, "100", NICK, "d", "u", GUILD)   # never on the roster
        result = player_store.unbind_discord_everywhere(self.db, GUILD, "100")
        self.assertEqual(result.status, "removed")
        self.assertEqual(bindings(self.db), {})
        self.assertIsNone(membership(self.db, pid))

    def test_unknown_account_changes_nothing(self):
        bind(self.db, "100", NICK, 1, 1)
        result = player_store.unbind_discord_everywhere(self.db, GUILD, "999")
        self.assertEqual(result.status, "not_found")
        self.assertEqual(sql(self.db, "SELECT COUNT(*) FROM roster_removals")[0][0], 0)
        self.assertEqual(bindings(self.db), {"100": 1})

    def test_other_players_are_not_touched(self):
        bind(self.db, "100", "Alpha", 1, 1)
        other = bind(self.db, "200", "Beta", 1, 2)
        player_store.unbind_discord_everywhere(self.db, GUILD, "100")
        self.assertTrue(membership(self.db, other))
        self.assertEqual(bindings(self.db), {"200": other})

    def test_the_nick_is_free_for_a_new_account_afterwards(self):
        pid = bind(self.db, "100", NICK, 1, 1)
        player_store.unbind_discord_everywhere(self.db, GUILD, "100")
        snapshot_add(self.db, "200", NICK)
        self.assertEqual(bindings(self.db), {"200": pid})

    def test_failure_rolls_everything_back(self):
        pid = bind(self.db, "100", NICK, 1, 1)
        sql(self.db, """CREATE TRIGGER boom BEFORE DELETE ON discord_bindings
            BEGIN SELECT RAISE(ABORT,'boom'); END""")
        with self.assertRaises(sqlite3.DatabaseError):
            player_store.unbind_discord_everywhere(self.db, GUILD, "100")
        self.assertTrue(membership(self.db, pid))        # one transaction: nothing was applied
        self.assertEqual(removals(self.db, pid), [])
        self.assertEqual(bindings(self.db), {"100": pid})

    def test_explicit_removal_helpers_are_still_there(self):
        self.assertTrue(callable(player_store.mark_roster_removed))
        self.assertTrue(callable(player_store.restore_roster_member))


class UnbindNickTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.db = make_db(self._td.name)

    def test_main_nick_works_like_unbind_by_discord_and_ignores_case(self):
        pid = bind(self.db, "100", NICK, 1, 1)
        add_scan(self.db, pid, NICK)
        result = player_store.unbind_nick_everywhere(self.db, GUILD, "  пЕЛЬМЕШЕК ")
        self.assertEqual((result.status, result.player_id, result.nick), ("removed", pid, NICK))
        self.assertEqual(result.discord_ids, ("100",))
        self.assertEqual(bindings(self.db), {})
        self.assertFalse(membership(self.db, pid))
        self.assertEqual(len(removals(self.db, pid)), 1)
        self.assertEqual(sql(self.db, "SELECT player_id FROM scan_players")[0][0], pid)

    def test_main_nick_of_an_inactive_but_still_bound_player_is_found(self):
        """The reported bug: «не найден в составе» while the binding was still there."""
        pid = bind(self.db, "100", NICK, 1, 1)
        left_roster(self.db, pid)
        result = player_store.unbind_nick_everywhere(self.db, GUILD, NICK)
        self.assertEqual((result.status, result.discord_ids), ("removed", ("100",)))
        self.assertEqual(bindings(self.db), {})

    def test_spare_nick_only_deletes_the_alias(self):
        pid = bind(self.db, "100", "Main", 1, 1)
        add_alias(self.db, pid, "Spare")
        result = player_store.unbind_nick_everywhere(self.db, GUILD, "SPARE")
        self.assertEqual((result.status, result.player_id, result.nick, result.alias),
                         ("alias_removed", pid, "Main", "Spare"))
        self.assertEqual(aliases(self.db, pid), [])
        self.assertTrue(membership(self.db, pid))
        self.assertEqual(bindings(self.db), {"100": pid})
        self.assertEqual(removals(self.db, pid), [])

    def test_player_from_the_admin_board_without_discord_is_removed(self):
        pid = unbound(self.db, NICK, 2, 2)
        result = player_store.unbind_nick_everywhere(self.db, GUILD, NICK)
        self.assertEqual((result.status, result.discord_ids, result.was_active), ("removed", (), True))
        self.assertFalse(membership(self.db, pid))
        self.assertEqual(len(removals(self.db, pid)), 1)

    def test_unknown_and_empty_nicks_and_repeat_calls_are_not_found(self):
        bind(self.db, "100", NICK, 1, 1)
        self.assertEqual(player_store.unbind_nick_everywhere(self.db, GUILD, "Nobody").status, "not_found")
        self.assertEqual(player_store.unbind_nick_everywhere(self.db, GUILD, "   ").status, "not_found")
        self.assertEqual(player_store.unbind_nick_everywhere(self.db, GUILD, NICK).status, "removed")
        self.assertEqual(player_store.unbind_nick_everywhere(self.db, GUILD, NICK).status, "not_found")


class RemoveCommandTests(unittest.IsolatedAsyncioTestCase):
    def member(self, member_id, name="Name"):
        guild = SimpleNamespace(id=GUILD)
        return SimpleNamespace(id=member_id, bot=False, display_name=name, name=name, guild=guild,
                               mention=f"<@{member_id}>")

    async def run_cmd(self, db, func, *args, **kwargs):
        with ExitStack() as stack:
            stack.enter_context(patch.object(bot, "PLAYER_DB_PATH", db))
            stack.enter_context(patch.object(bot, "db_lock", asyncio.Lock()))
            stack.enter_context(patch.object(bot, "can_use_roster", return_value=True))
            status = stack.enter_context(patch.object(bot, "upsert_status_messages", new=AsyncMock()))
            stack.enter_context(patch.object(bot, "is_kv_live", return_value=False))
            stack.enter_context(patch.object(bot, "reindex_scan_histories"))
            actor = SimpleNamespace(guild=SimpleNamespace(id=GUILD))
            result = await func(actor, *args, **kwargs)
            return result, status

    async def test_reported_scenario_remove_by_nick_then_add_to_another_account(self):
        with tempfile.TemporaryDirectory() as td:
            db = make_db(td)
            pid = bind(db, "100", NICK, 1, 1)

            embed, status = await self.run_cmd(db, bot.do_roster_remove, game_nick=NICK)
            self.assertIn("Удалён из состава", embed.title)
            self.assertIn("<@100>", embed.description)
            status.assert_awaited()
            self.assertEqual(bindings(db), {})

            ok, embed = await self.run_cmd(db, bot.do_roster_add, self.member(200), NICK)
            self.assertTrue(ok, embed.description)
            self.assertEqual(bindings(db), {"200": pid})

    async def test_remove_by_nick_finds_a_player_who_left_the_roster_but_is_still_bound(self):
        with tempfile.TemporaryDirectory() as td:
            db = make_db(td)
            pid = bind(db, "100", NICK, 1, 1)
            left_roster(db, pid)
            embed, _ = await self.run_cmd(db, bot.do_roster_remove, game_nick=NICK)
            self.assertIn("Удалён из состава", embed.title)
            self.assertIn("<@100>", embed.description)
            self.assertEqual(bindings(db), {})

    async def test_remove_by_discord_for_an_account_outside_the_roster(self):
        with tempfile.TemporaryDirectory() as td:
            db = make_db(td)
            pid = player_store.upsert_binding(db, "100", NICK, "d", "u", GUILD)
            embed, _ = await self.run_cmd(db, bot.do_roster_remove, target=self.member(100, "Sawer"))
            self.assertIn("Удалён из состава", embed.title)
            self.assertIn("<@100>", embed.description)
            self.assertEqual(bindings(db), {})
            self.assertEqual(len(removals(db, pid)), 1)

    async def test_remove_by_spare_nick_unbinds_the_nick_only(self):
        with tempfile.TemporaryDirectory() as td:
            db = make_db(td)
            pid = bind(db, "100", "Main", 1, 1)
            add_alias(db, pid, "Spare")
            embed, status = await self.run_cmd(db, bot.do_roster_remove, game_nick="spare")
            self.assertIn("Ник отвязан", embed.title)
            self.assertIn("Spare", embed.description)
            self.assertEqual(aliases(db, pid), [])
            self.assertTrue(membership(db, pid))
            self.assertEqual(bindings(db), {"100": pid})

    async def test_remove_unknown_says_not_in_database(self):
        with tempfile.TemporaryDirectory() as td:
            db = make_db(td)
            bind(db, "100", NICK, 1, 1)
            by_nick, status = await self.run_cmd(db, bot.do_roster_remove, game_nick="Nobody")
            by_discord, _ = await self.run_cmd(db, bot.do_roster_remove, target=self.member(999))
            for embed in (by_nick, by_discord):
                self.assertIn("Нет в базе", embed.title)
            status.assert_not_awaited()
            self.assertEqual(bindings(db), {"100": 1})

    async def test_argument_checks_are_kept(self):
        with tempfile.TemporaryDirectory() as td:
            db = make_db(td)
            neither, _ = await self.run_cmd(db, bot.do_roster_remove)
            both, _ = await self.run_cmd(db, bot.do_roster_remove, target=self.member(1), game_nick="x")
            self.assertIn("Ошибка", neither.title)
            self.assertIn("Ошибка", both.title)

    async def test_live_data_of_the_removed_player_stays_in_the_db_through_the_day(self):
        """Grenades, voice and attendance of an unfinished day are not deleted by /remove
        or by the routine snapshot saves that follow it, and they reach the final report."""
        with tempfile.TemporaryDirectory() as td:
            db = make_db(td)
            gone = bind(db, "100", NICK, 1, 1)
            kept = bind(db, "200", "Other", 1, 2)
            data = player_store.load_bot_snapshot(db, GUILD)
            for did in ("100", "200"):
                data["players"][did]["came"] = True
            data.update(session_date="2026-11-05", grenade_date="2026-11-05", kv_session_active=True,
                        grenade_history={NICK: {"20:00": 7}, "Other": {"20:00": 3}},
                        voice_speak_seconds={"100": 120.0, "200": 60.0})
            player_store.save_bot_snapshot(db, data, GUILD)

            embed, _ = await self.run_cmd(db, bot.do_roster_remove, game_nick=NICK)
            self.assertIn("Удалён из состава", embed.title)
            for _ in range(2):                              # routine saves after the removal
                player_store.save_bot_snapshot(db, player_store.load_bot_snapshot(db, GUILD), GUILD)

            self.assertEqual(sorted(sql(db, "SELECT player_id,value FROM live_grenade_state")),
                             sorted([(gone, 7), (kept, 3)]))
            self.assertEqual(sorted(sql(db, "SELECT player_id,came FROM attendance")),
                             sorted([(gone, 1), (kept, 1)]))
            self.assertEqual(sorted(sql(db, "SELECT discord_id,player_id,seconds FROM voice_checkpoints")),
                             [("100", gone, 120.0), ("200", kept, 60.0)])
            self.assertFalse(membership(db, gone))
            self.assertNotIn("100", player_store.load_bot_snapshot(db, GUILD)["players"])

            final = player_store.load_bot_snapshot(db, GUILD)       # the day ends
            final.update(kv_session_active=False, kv_finished=True)
            player_store.save_bot_snapshot(db, final, GUILD)
            self.assertEqual(sorted(sql(db, "SELECT player_id,value FROM grenade_session_stats")),
                             sorted([(gone, 7), (kept, 3)]))
            self.assertEqual(sorted(sql(db, "SELECT player_id,seconds FROM voice_stats")),
                             sorted([(gone, 120.0), (kept, 60.0)]))
            self.assertEqual(sorted(r[0] for r in sql(db, "SELECT player_id FROM kv_daily_players")),
                             sorted([gone, kept]))

    async def test_final_stats_of_a_finished_day_survive_the_removal(self):
        with tempfile.TemporaryDirectory() as td:
            db = make_db(td)
            gone = bind(db, "100", NICK, 1, 1)
            bind(db, "200", "Other", 1, 2)
            data = player_store.load_bot_snapshot(db, GUILD)
            data.update(session_date="2026-11-05", grenade_date="2026-11-05", kv_session_active=False,
                        kv_finished=True, grenade_history={NICK: {"20:00": 7}, "Other": {"20:00": 3}})
            player_store.save_bot_snapshot(db, data, GUILD)
            before = sql(db, "SELECT player_id,stat_key,value FROM grenade_session_stats ORDER BY player_id")
            self.assertEqual(len(before), 2)

            await self.run_cmd(db, bot.do_roster_remove, game_nick=NICK)

            after = sql(db, "SELECT player_id,stat_key,value FROM grenade_session_stats ORDER BY player_id")
            self.assertEqual(after, before)
            self.assertFalse(membership(db, gone))


if __name__ == "__main__":
    unittest.main()
