"""Keep five visible slots and provide roster identity lookup compatibility."""
from __future__ import annotations
import builtins

def _find_existing(player_store,con,nickname):
    normal=player_store._norm(nickname)
    player_ids={
        int(row["id"])
        for row in con.execute("SELECT id,canonical_nick FROM players")
        if player_store._norm(row["canonical_nick"])==normal
    }
    player_ids.update(
        int(row["player_id"])
        for row in con.execute("SELECT alias,player_id FROM player_aliases")
        if player_store._norm(row["alias"])==normal
    )
    if len(player_ids)!=1:return None
    player_id=next(iter(player_ids))
    binding=con.execute("SELECT 1 FROM discord_bindings WHERE player_id=? LIMIT 1",(player_id,)).fetchone()
    return player_id if binding is not None else None

def install(roster_sheet):
    if getattr(roster_sheet,"_five_slot_limit",False):return
    roster_sheet._five_slot_limit=True
    roster_sheet.range=lambda start,stop:builtins.range(start,min(stop,6))
    # Production player_store has no _identity_ids helper. Use the canonical
    # nickname and alias tables directly instead.
    roster_sheet._exact=_find_existing
