"""Serialize Discord status-message upserts to prevent duplicate creation."""
from __future__ import annotations
import asyncio

def install(runtime):
    """Ensure attendance and grenade messages are created or edited one call at a time."""
    if getattr(runtime,"_status_upsert_lock_installed",False):return
    runtime._status_upsert_lock_installed=True
    original=runtime.upsert_status_messages
    lock=asyncio.Lock()
    async def upsert_status_messages(data=None,*,force=False,parts=None):
        async with lock:
            fresh=runtime.load_db()
            stored_ids=fresh.get("message_ids") or {"online":None,"grenades":None}
            if data is None:
                data=fresh
            else:
                # Concurrent startup calls can carry an older DB snapshot. Reuse
                # the two IDs saved by the previous serialized update.
                data["message_ids"]={
                    "online":stored_ids.get("online"),
                    "grenades":stored_ids.get("grenades"),
                }
            return await original(data,force=force,parts=parts)
    runtime.upsert_status_messages=upsert_status_messages
