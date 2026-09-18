"""Keep the normalized admin roster and the runtime Discord view consistent."""
from __future__ import annotations

import sqlite3
from typing import Any

import player_store


def install_player_store() -> None:
    """Preserve active admin-only roster rows during legacy snapshot writes.

    The runtime compatibility snapshot contains only players with a Discord
    binding.  The admin board can also contain an active game nickname without
    such a binding, so the old full-table rewrite must not deactivate those
    rows on every status refresh.
    """
    if getattr(player_store, "_roster_consistency_installed", False):
        return
    player_store._roster_consistency_installed = True
    original = player_store.save_bot_snapshot

    def save_bot_snapshot(db_path, data, guild_id=None):
        cfg = data.get("config") if isinstance(data.get("config"), dict) else {}
        gid = int(guild_id or cfg.get("guild_id") or 0)
        player_store.ensure_schema(db_path)
        with player_store.connect(db_path) as con:
            preserved = [
                (int(row["player_id"]), row["squad_id"], row["slot"])
                for row in con.execute(
                    """SELECT r.player_id,r.squad_id,r.slot
                       FROM roster_memberships r
                       LEFT JOIN discord_bindings d ON d.player_id=r.player_id
                       LEFT JOIN roster_removals x
                         ON x.guild_id=r.guild_id AND x.player_id=r.player_id
                       WHERE r.guild_id=? AND r.active=1
                         AND d.player_id IS NULL AND x.player_id IS NULL""",
                    (gid,),
                )
            ]

        result = original(db_path, data, guild_id)

        if not preserved:
            return result
        with player_store.connect(db_path) as con:
            con.execute("BEGIN IMMEDIATE")
            try:
                for player_id, squad_id, slot in preserved:
                    if squad_id is not None and slot is not None:
                        occupied = con.execute(
                            """SELECT player_id FROM roster_memberships
                               WHERE guild_id=? AND squad_id=? AND slot=? AND active=1""",
                            (gid, int(squad_id), int(slot)),
                        ).fetchone()
                        # A bound player written by the current snapshot has
                        # legitimately replaced this admin-only member.
                        if occupied is not None and int(occupied[0]) != player_id:
                            continue
                    con.execute(
                        """UPDATE roster_memberships SET active=1,
                           deactivated_at=NULL,updated_at=CURRENT_TIMESTAMP
                           WHERE guild_id=? AND player_id=?""",
                        (gid, player_id),
                    )
                con.commit()
            except Exception:
                con.rollback()
                raise
        return result

    player_store.save_bot_snapshot = save_bot_snapshot


def sync_board_cells(con: sqlite3.Connection, guild_id: int) -> None:
    """Rebuild the visual board from active normalized memberships."""
    con.executescript(
        """CREATE TABLE IF NOT EXISTS roster_board_cells(
               guild_id INTEGER NOT NULL, squad_id INTEGER NOT NULL,
               slot INTEGER NOT NULL, text TEXT NOT NULL DEFAULT '',
               updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
               PRIMARY KEY(guild_id,squad_id,slot));"""
    )
    con.execute("DELETE FROM roster_board_cells WHERE guild_id=?", (int(guild_id),))
    rows = list(
        con.execute(
            """SELECT r.squad_id,r.slot,p.canonical_nick
               FROM roster_memberships r JOIN players p ON p.id=r.player_id
               WHERE r.guild_id=? AND r.active=1
                 AND r.squad_id BETWEEN 1 AND 6 AND r.slot BETWEEN 1 AND 5
               UNION ALL
               SELECT r.squad_id,r.slot,p.canonical_nick
               FROM roster_memberships r JOIN players p ON p.id=r.player_id
               WHERE r.guild_id=? AND r.active=1
                 AND r.squad_id=99 AND r.slot BETWEEN 1 AND 5""",
            (int(guild_id), int(guild_id)),
        )
    )
    con.executemany(
        "INSERT INTO roster_board_cells(guild_id,squad_id,slot,text) VALUES(?,?,?,?)",
        [(int(guild_id), int(row[0]), int(row[1]), str(row[2])) for row in rows],
    )
    # Keep the five editable positions visible even when empty.
    con.executemany(
        "INSERT OR IGNORE INTO roster_board_cells(guild_id,squad_id,slot,text) VALUES(?,?,?,'')",
        [(int(guild_id), squad_id, slot)
         for squad_id in (1, 2, 3, 99, 4, 5, 6)
         for slot in range(1, 6)],
    )


def install_roster_board(roster_board) -> None:
    """Make every admin-board render reconcile stale cached cells."""
    if getattr(roster_board, "_roster_consistency_installed", False):
        return
    roster_board._roster_consistency_installed = True
    original_seed = roster_board._seed

    def synced_seed(con: sqlite3.Connection, guild_id: int) -> None:
        original_seed(con, guild_id)
        sync_board_cells(con, guild_id)

    roster_board._seed = synced_seed
