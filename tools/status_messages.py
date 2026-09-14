"""Keep exactly one canonical attendance embed and one grenade embed."""
from __future__ import annotations

def _kind(message):
    for embed in getattr(message, "embeds", ()):
        title=(getattr(embed,"title",None) or "").upper()
        if "ЯВКА НА КВ" in title:return "online"
        if "ГРАНАТЫ" in title:return "grenades"
    return None

def install(runtime):
    if getattr(runtime,"_status_dedupe_installed",False):return
    runtime._status_dedupe_installed=True
    original=runtime.upsert_status_messages
    reconciled=False
    async def reconcile(data):
        nonlocal reconciled
        if reconciled:return
        channel=await runtime.get_log_channel(data);bot_user=getattr(runtime.bot,"user",None)
        if channel is None or bot_user is None:return
        found={"online":[],"grenades":[]}
        try:
            async for message in channel.history(limit=500,oldest_first=True):
                if getattr(getattr(message,"author",None),"id",None)!=bot_user.id:continue
                kind=_kind(message)
                if kind:found[kind].append(message)
        except Exception as exc:
            runtime.log(f"status dedupe: история недоступна: {exc}","warn");return
        mids=data.setdefault("message_ids",{"online":None,"grenades":None})
        for kind,messages in found.items():
            if not messages:continue
            mids[kind]=messages[0].id
            for duplicate in messages[1:]:
                try:await duplicate.delete()
                except Exception as exc:runtime.log(f"status dedupe: не удалено {duplicate.id}: {exc}","warn")
        data["message_ids"]=mids;runtime.save_db(data);reconciled=True
        runtime.log(f"status dedupe: явка={mids.get('online')} грены={mids.get('grenades')}","ok")
    async def upsert_status_messages(data=None,*,force=False,parts=None):
        if data is None:data=runtime.load_db()
        await reconcile(data)
        return await original(data,force=force,parts=parts)
    runtime.upsert_status_messages=upsert_status_messages
