from __future__ import annotations

import tempfile
import unittest
from contextlib import closing
from pathlib import Path

import player_store
from tools import roster_consistency


class RosterConsistencyTests(unittest.TestCase):
    def test_unbound_admin_member_survives_runtime_snapshot(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "roster.sqlite3"
            player_store.ensure_schema(db)
            player_store.upsert_guild_config(db, 42)
            bound = player_store.upsert_binding(db, "100", "BoundPlayer", guild_id=42)
            with closing(player_store.connect(db)) as con:
                con.execute("INSERT INTO players(canonical_nick) VALUES(?)", ("extanz",))
                unbound = int(con.execute("SELECT last_insert_rowid()").fetchone()[0])
                con.execute("INSERT INTO player_guilds(player_id,guild_id) VALUES(?,?)", (unbound, 42))
            player_store.upsert_roster_member(db, 42, bound, squad_id=1, slot=1)
            player_store.upsert_roster_member(db, 42, unbound, squad_id=2, slot=1)

            snapshot = player_store.load_bot_snapshot(db, 42)
            snapshot["config"]["log_channel_id"] = 9
            player_store.save_bot_snapshot(db, snapshot, 42)

            rows = player_store.list_roster(db, 42)
            self.assertEqual({row["player_id"] for row in rows}, {bound, unbound})
            self.assertEqual(
                (2, 1),
                next((row["squad_id"], row["slot"])
                     for row in rows if row["player_id"] == unbound),
            )

    def test_board_rebuild_clears_stale_positions(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "roster.sqlite3"
            player_store.ensure_schema(db)
            player_store.upsert_guild_config(db, 42)
            pid = player_store.upsert_binding(db, "100", "extanz", guild_id=42)
            player_store.upsert_roster_member(db, 42, pid, squad_id=2, slot=1)
            with closing(player_store.connect(db)) as con:
                roster_consistency.sync_board_cells(con, 42)
                self.assertEqual(
                    "extanz",
                    con.execute("""SELECT text FROM roster_board_cells
                                   WHERE guild_id=42 AND squad_id=2 AND slot=1""").fetchone()[0],
                )
                con.execute("""UPDATE roster_memberships SET squad_id=99,slot=1
                               WHERE guild_id=42 AND player_id=?""", (pid,))
                roster_consistency.sync_board_cells(con, 42)
                self.assertEqual(
                    "",
                    con.execute("""SELECT text FROM roster_board_cells
                                   WHERE guild_id=42 AND squad_id=2 AND slot=1""").fetchone()[0],
                )
                self.assertEqual(
                    "extanz",
                    con.execute("""SELECT text FROM roster_board_cells
                                   WHERE guild_id=42 AND squad_id=99 AND slot=1""").fetchone()[0],
                )


if __name__ == "__main__":
    unittest.main()
