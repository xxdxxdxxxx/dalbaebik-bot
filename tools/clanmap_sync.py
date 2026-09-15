"""Push the SQLite roster board to Clan Map's Supabase tables."""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from contextlib import closing
from functools import wraps
from urllib.parse import quote
from urllib.error import HTTPError
from urllib.request import Request, urlopen

LOG = logging.getLogger(__name__)
DEFAULT_URL = "https://ephwmvohktdqccmdyhek.supabase.co"
_TIMER: threading.Timer | None = None
_TIMER_LOCK = threading.Lock()
_SYNC_LOCK = threading.Lock()


def _clean(value: object) -> str:
    return " ".join(str(value or "").strip().split())


def _request(method: str, path: str, payload=None):
    base = os.getenv("CLANMAP_SUPABASE_URL", DEFAULT_URL).rstrip("/")
    key = os.getenv("CLANMAP_SUPABASE_SECRET_KEY", "").strip()
    if not key:
        raise RuntimeError("CLANMAP_SUPABASE_SECRET_KEY is not configured")
    body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(
        f"{base}/rest/v1/{path}",
        data=body,
        method=method,
        headers={
            "apikey": key,
            "Accept": "application/json",
            "Content-Type": "application/json; charset=utf-8",
            "Prefer": "return=representation",
        },
    )
    try:
        with urlopen(request, timeout=15) as response:
            raw = response.read()
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"Supabase HTTP {exc.code}: {detail}") from exc
    return json.loads(raw.decode("utf-8")) if raw else None


def _desired_roster(db_path) -> dict[tuple[int, int], str]:
    with closing(sqlite3.connect(str(db_path), timeout=10)) as con:
        con.row_factory = sqlite3.Row
        guild = con.execute(
            "SELECT guild_id FROM bot_guild_config ORDER BY (guild_id=0),guild_id LIMIT 1"
        ).fetchone()
        if guild is None:
            return {}
        guild_id = int(guild[0])
        has_board = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='roster_board_cells'"
        ).fetchone()
        desired = {(squad, slot): "" for squad in range(1, 7) for slot in range(1, 6)}
        if has_board:
            for row in con.execute(
                """SELECT squad_id,slot,text FROM roster_board_cells
                   WHERE guild_id=? AND squad_id BETWEEN 1 AND 6 AND slot BETWEEN 1 AND 5""",
                (guild_id,),
            ):
                desired[(int(row["squad_id"]), int(row["slot"]))] = _clean(row["text"])
            return desired
        for row in con.execute(
            """SELECT r.squad_id,r.slot,p.canonical_nick
               FROM roster_memberships r JOIN players p ON p.id=r.player_id
               WHERE r.guild_id=? AND r.active=1 AND r.squad_id BETWEEN 1 AND 6
                 AND r.slot BETWEEN 1 AND 5""",
            (guild_id,),
        ):
            desired[(int(row["squad_id"]), int(row["slot"]))] = _clean(row["canonical_nick"])
        return desired


def sync_now(db_path) -> bool:
    """Replace the cloud roster with the six five-slot SQLite squads."""
    if not os.getenv("CLANMAP_SUPABASE_SECRET_KEY", "").strip():
        return False
    with _SYNC_LOCK:
        desired = _desired_roster(db_path)
        if not desired:
            return False
        meta = _request("GET", "roster_meta?select=map_set_id&order=updated_at.desc&limit=1") or []
        if not meta:
            raise RuntimeError("Clan Map roster_meta is empty")
        map_set_id = str(meta[0]["map_set_id"])
        squads = _request(
            "GET",
            "squads?select=id,idx&map_set_id=eq."
            + quote(map_set_id, safe="-")
            + "&idx=in.(1,2,3,4,5,6)",
        ) or []
        squad_ids = {int(row["idx"]): str(row["id"]) for row in squads}
        if set(squad_ids) != set(range(1, 7)):
            raise RuntimeError("Clan Map must contain squads 1-6")
        ids_csv = ",".join(squad_ids[index] for index in range(1, 7))
        rows = [
            {"squad_id": squad_ids[squad], "nickname": nickname, "slot": slot}
            for (squad, slot), nickname in sorted(desired.items())
            if nickname
        ]
        # Player rows are roster-only data. Replacing them avoids the database's
        # 1..5 slot constraint while keeping the six squad records themselves stable.
        _request("DELETE", f"players?squad_id=in.({ids_csv})")
        if rows:
            _request("POST", "players", rows)

        _request(
            "PATCH",
            "roster_meta?map_set_id=eq." + quote(map_set_id, safe="-"),
            {"source": "dalbaebik-bot"},
        )
        LOG.info("Clan Map roster synchronized")
        return True


def _run(db_path) -> None:
    try:
        sync_now(db_path)
    except Exception:
        LOG.exception("Clan Map roster synchronization failed")


def schedule(db_path, delay: float = 0.4) -> None:
    """Debounce admin edits and never make the HTTP response wait for Supabase."""
    if not os.getenv("CLANMAP_SUPABASE_SECRET_KEY", "").strip():
        return
    global _TIMER
    with _TIMER_LOCK:
        if _TIMER is not None:
            _TIMER.cancel()
        _TIMER = threading.Timer(delay, _run, args=(db_path,))
        _TIMER.daemon = True
        _TIMER.start()


def install(admin) -> None:
    if getattr(admin.app, "_clanmap_sync_installed", False):
        return
    admin.app._clanmap_sync_installed = True
    for endpoint in ("roster_cell", "roster_cells_swap", "roster_swap"):
        original = admin.app.view_functions.get(endpoint)
        if original is None:
            continue

        @wraps(original)
        def wrapped(*args, __original=original, **kwargs):
            result = __original(*args, **kwargs)
            status = result[1] if isinstance(result, tuple) and len(result) > 1 else getattr(result, "status_code", 200)
            if int(status) < 400:
                schedule(admin.DB_PATH)
            return result

        admin.app.view_functions[endpoint] = wrapped
    schedule(admin.DB_PATH, delay=2.0)
