"""Keep five visible slots and turn every main-cell nickname into a roster player."""
from __future__ import annotations
import builtins


def _find_existing(player_store, con, nickname):
    normal = player_store._norm(nickname)
    player_ids = {
        int(row["id"])
        for row in con.execute("SELECT id,canonical_nick FROM players")
        if player_store._norm(row["canonical_nick"]) == normal
    }
    player_ids.update(
        int(row["player_id"])
        for row in con.execute("SELECT alias,player_id FROM player_aliases")
        if player_store._norm(row["alias"]) == normal
    )
    return next(iter(player_ids)) if len(player_ids) == 1 else None


def _find_or_create(admin, con, guild_id, nickname):
    player_id = _find_existing(admin.player_store, con, nickname)
    if player_id is None:
        con.execute("INSERT INTO players(canonical_nick) VALUES(?)", (nickname,))
        player_id = int(con.execute("SELECT last_insert_rowid()").fetchone()[0])
    con.execute(
        "INSERT OR IGNORE INTO player_guilds(player_id,guild_id) VALUES(?,?)",
        (player_id, guild_id),
    )
    return player_id


def _set_cell(admin, con, guild_id, squad_id, slot, text):
    text = " ".join(str(text or "").strip().split())
    old = con.execute(
        "SELECT player_id FROM roster_memberships WHERE guild_id=? AND squad_id=? AND slot=? AND active=1",
        (guild_id, squad_id, slot),
    ).fetchone()
    old_player_id = int(old[0]) if old else None
    player_id = _find_or_create(admin, con, guild_id, text) if text else None
    if old_player_id is not None and old_player_id != player_id:
        con.execute(
            "UPDATE roster_memberships SET active=0,deactivated_at=COALESCE(deactivated_at,CURRENT_TIMESTAMP),updated_at=CURRENT_TIMESTAMP WHERE guild_id=? AND player_id=?",
            (guild_id, old_player_id),
        )
    if player_id is not None:
        previous = con.execute(
            "SELECT squad_id,slot FROM roster_memberships WHERE guild_id=? AND player_id=? AND active=1",
            (guild_id, player_id),
        ).fetchone()
        if previous:
            previous_squad, previous_slot = previous[0], previous[1]
            if previous_squad is not None and previous_slot is not None and (
                int(previous_squad) != squad_id or int(previous_slot) != slot
            ):
                con.execute(
                    "UPDATE roster_board_cells SET text='',updated_at=CURRENT_TIMESTAMP WHERE guild_id=? AND squad_id=? AND slot=?",
                    (guild_id, int(previous_squad), int(previous_slot)),
                )
        con.execute(
            "UPDATE roster_memberships SET active=0,squad_id=NULL,slot=NULL,updated_at=CURRENT_TIMESTAMP WHERE guild_id=? AND player_id=?",
            (guild_id, player_id),
        )
        con.execute(
            "DELETE FROM roster_removals WHERE guild_id=? AND player_id=?",
            (guild_id, player_id),
        )
        con.execute(
            "INSERT INTO roster_memberships(guild_id,player_id,squad_id,slot,active,deactivated_at) VALUES(?,?,?,?,1,NULL) ON CONFLICT(guild_id,player_id) DO UPDATE SET squad_id=excluded.squad_id,slot=excluded.slot,active=1,deactivated_at=NULL,updated_at=CURRENT_TIMESTAMP",
            (guild_id, player_id, squad_id, slot),
        )
    con.execute(
        "INSERT INTO roster_board_cells(guild_id,squad_id,slot,text) VALUES(?,?,?,?) ON CONFLICT(guild_id,squad_id,slot) DO UPDATE SET text=excluded.text,updated_at=CURRENT_TIMESTAMP",
        (guild_id, squad_id, slot, text),
    )


def install(roster_sheet):
    if getattr(roster_sheet, "_five_slot_limit", False):
        return
    roster_sheet._five_slot_limit = True
    roster_sheet.range = lambda start, stop: builtins.range(start, min(stop, 6))
    roster_sheet._exact = _find_existing
    roster_sheet._set = _set_cell
