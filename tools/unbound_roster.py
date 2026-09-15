"""Include roster players without Discord bindings in embeds and grenade scans."""
from __future__ import annotations

import copy
import re
from contextlib import closing


_UNBOUND_MENTION = re.compile(r"<@unbound:(\d+)>")


def _rows(runtime):
    with closing(runtime.player_store.connect(runtime.PLAYER_DB_PATH)) as con:
        return [
            dict(row)
            for row in con.execute(
                """SELECT p.id,p.canonical_nick,r.squad_id,r.slot
                   FROM roster_memberships r
                   JOIN players p ON p.id=r.player_id
                   LEFT JOIN discord_bindings d ON d.player_id=p.id
                   WHERE r.active=1 AND d.discord_id IS NULL
                     AND (r.squad_id BETWEEN 1 AND 6 OR r.squad_id=99)
                   ORDER BY r.squad_id,r.slot,p.canonical_nick COLLATE NOCASE"""
            )
        ]


def _with_unbound(runtime, data):
    result = copy.deepcopy(data)
    players = result.setdefault("players", {})
    for row in _rows(runtime):
        players[f"unbound:{int(row['id'])}"] = {
            "discord_name": "",
            "discord_username": "",
            "game_nick": str(row["canonical_nick"]),
            "came": False,
            "in_voice": False,
            "squad": row["squad_id"],
            "slot": row["slot"],
        }
    return result


def _without_fake_mentions(value):
    return _UNBOUND_MENTION.sub("без привязанного Discord", str(value or ""))


def _clean_online_embed(embed):
    if embed.description is not None:
        embed.description = _without_fake_mentions(embed.description)
    for index, field in enumerate(list(embed.fields)):
        embed.set_field_at(
            index,
            name=field.name,
            value=_without_fake_mentions(field.value),
            inline=field.inline,
        )
    return embed


def install(runtime):
    if getattr(runtime, "_unbound_roster_installed", False):
        return
    runtime._unbound_roster_installed = True

    original_online = runtime.format_online_embed
    original_grenades = runtime.format_grenades_embed
    original_scan = runtime.scan_grenades
    original_save_snapshot = runtime.player_store.save_bot_snapshot

    def format_online_embed(data):
        return _clean_online_embed(original_online(_with_unbound(runtime, data)))

    def format_grenades_embed(data):
        return original_grenades(_with_unbound(runtime, data))

    async def scan_grenades(nicks):
        combined = list(nicks or [])
        known = {str(nick).casefold() for nick in combined}
        for row in _rows(runtime):
            nickname = str(row["canonical_nick"]).strip()
            if nickname and nickname.casefold() not in known:
                combined.append(nickname)
                known.add(nickname.casefold())
        return await original_scan(combined)

    def save_bot_snapshot(db_path, data, guild_id=None):
        configured = (data.get("config") or {}).get("guild_id")
        gid = int(guild_id or configured or 0)
        preserved = []
        if gid:
            with closing(runtime.player_store.connect(db_path)) as con:
                preserved = [
                    tuple(row)
                    for row in con.execute(
                        """SELECT r.player_id,r.squad_id,r.slot FROM roster_memberships r
                           LEFT JOIN discord_bindings d ON d.player_id=r.player_id
                           WHERE r.guild_id=? AND r.active=1 AND d.discord_id IS NULL""",
                        (gid,),
                    )
                ]
                for record in (data.get("players") or {}).values():
                    nickname = str(record.get("game_nick") or "").strip()
                    if not nickname or (record.get("squad") is not None and record.get("slot") is not None):
                        continue
                    ids = runtime.player_store._identity_ids(con, nickname)
                    if len(ids) != 1:
                        continue
                    player_id = next(iter(ids))
                    position = con.execute(
                        "SELECT squad_id,slot FROM roster_memberships WHERE guild_id=? AND player_id=? AND active=1",
                        (gid, player_id),
                    ).fetchone()
                    if position is not None and position[0] is not None and position[1] is not None:
                        record["squad"] = int(position[0])
                        record["slot"] = int(position[1])
        result = original_save_snapshot(db_path, data, guild_id)
        if gid and preserved:
            with closing(runtime.player_store.connect(db_path)) as con:
                for player_id, squad_id, slot in preserved:
                    if con.execute(
                        "SELECT 1 FROM discord_bindings WHERE player_id=?", (player_id,)
                    ).fetchone() is not None:
                        continue
                    occupied = con.execute(
                        """SELECT 1 FROM roster_memberships WHERE guild_id=? AND squad_id=? AND slot=?
                           AND active=1 AND player_id<>?""",
                        (gid, squad_id, slot, player_id),
                    ).fetchone()
                    if occupied is not None:
                        continue
                    con.execute(
                        """INSERT INTO roster_memberships(guild_id,player_id,squad_id,slot,active,deactivated_at)
                           VALUES(?,?,?,?,1,NULL) ON CONFLICT(guild_id,player_id) DO UPDATE SET
                           squad_id=excluded.squad_id,slot=excluded.slot,active=1,deactivated_at=NULL,
                           updated_at=CURRENT_TIMESTAMP""",
                        (gid, player_id, squad_id, slot),
                    )
        return result

    runtime.format_online_embed = format_online_embed
    runtime.format_grenades_embed = format_grenades_embed
    runtime.scan_grenades = scan_grenades
    runtime.player_store.save_bot_snapshot = save_bot_snapshot
