"""Unified SQLite player identity store and idempotent legacy migration."""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
import unicodedata
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NamedTuple

SCHEMA_VERSION = 11

def parse_match_date(value: str) -> str:
    """Parse a KV date without relying on the system locale."""
    text = str(value or "").strip()
    for fmt in ("%d.%m.%y", "%d.%m.%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            pass
    raise ValueError("Укажите реальную дату: 19.08.26, 19.08.2026 или 2026-08-19")


def _match_date_from_timestamp(value: str) -> str | None:
    text = str(value or "").strip()
    if not re.match(r"^\d{4}-\d{2}-\d{2}[T ]", text):
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc)
    return parsed.date().isoformat()


SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS schema_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS players(
 id INTEGER PRIMARY KEY,
 canonical_nick TEXT NOT NULL COLLATE NOCASE UNIQUE,
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS discord_bindings(
 discord_id TEXT PRIMARY KEY,
 player_id INTEGER NOT NULL REFERENCES players(id),
 discord_name TEXT,
 discord_username TEXT,
 updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS player_aliases(
 alias TEXT PRIMARY KEY COLLATE NOCASE,
 player_id INTEGER NOT NULL REFERENCES players(id),
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS player_guilds(
 player_id INTEGER NOT NULL REFERENCES players(id) ON DELETE CASCADE,
 guild_id INTEGER NOT NULL,
 PRIMARY KEY(player_id, guild_id)
);
CREATE TABLE IF NOT EXISTS fuzzy_exclusions(
 normalized_nick TEXT NOT NULL,
 guild_id INTEGER NOT NULL DEFAULT 0,
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 PRIMARY KEY(normalized_nick, guild_id)
);
CREATE TABLE IF NOT EXISTS grenade_stats(
 player_id INTEGER NOT NULL REFERENCES players(id),
 stat_key TEXT NOT NULL,
 value INTEGER NOT NULL,
 PRIMARY KEY(player_id, stat_key)
);
CREATE TABLE IF NOT EXISTS scans(
 id INTEGER PRIMARY KEY, stage INTEGER, map TEXT NOT NULL, guild_id INTEGER, channel_id INTEGER, user_id INTEGER NOT NULL, scanned_at TEXT NOT NULL, source_filename TEXT NOT NULL, content_fingerprint TEXT, match_date TEXT
);
CREATE TABLE IF NOT EXISTS scan_players(
 id INTEGER PRIMARY KEY, scan_id INTEGER NOT NULL REFERENCES scans(id) ON DELETE CASCADE, place INTEGER NOT NULL, nick TEXT NOT NULL, kills INTEGER NOT NULL, deaths INTEGER NOT NULL, assists INTEGER NOT NULL, score INTEGER, player_id INTEGER REFERENCES players(id)
);
CREATE INDEX IF NOT EXISTS idx_scan_players_nick ON scan_players(nick COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_scan_players_scan_id ON scan_players(scan_id);
CREATE TABLE IF NOT EXISTS voice_stats(
 source_key TEXT NOT NULL,
 discord_id TEXT NOT NULL,
 player_id INTEGER REFERENCES players(id),
 seconds REAL NOT NULL CHECK(seconds >= 0),
 session_date TEXT,
 updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 PRIMARY KEY(source_key, discord_id)
);
CREATE INDEX IF NOT EXISTS idx_voice_stats_player_id ON voice_stats(player_id);
CREATE TABLE IF NOT EXISTS grenade_session_stats(
 source_key TEXT NOT NULL,
 player_id INTEGER NOT NULL REFERENCES players(id),
 stat_key TEXT NOT NULL,
 value INTEGER NOT NULL CHECK(value >= 0),
 match_date TEXT,
 guild_id INTEGER,
 updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 PRIMARY KEY(source_key, player_id, stat_key)
);
CREATE INDEX IF NOT EXISTS idx_grenade_session_date ON grenade_session_stats(match_date, guild_id);
CREATE TABLE IF NOT EXISTS kv_daily_reports(
 id INTEGER PRIMARY KEY,
 match_date TEXT NOT NULL,
 guild_id INTEGER NOT NULL DEFAULT 0,
 source_path TEXT NOT NULL,
 source_sha256 TEXT NOT NULL,
 source_mtime_ns INTEGER,
 format_version INTEGER NOT NULL DEFAULT 1,
 stage_count INTEGER NOT NULL CHECK(stage_count IN (3,4)),
 generated_at_text TEXT,
 imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 UNIQUE(source_path, guild_id)
);
CREATE TABLE IF NOT EXISTS kv_daily_players(
 report_id INTEGER NOT NULL REFERENCES kv_daily_reports(id) ON DELETE CASCADE,
 row_no INTEGER NOT NULL,
 player_id INTEGER REFERENCES players(id),
 raw_nick TEXT NOT NULL,
 discord_username TEXT,
 squad_label TEXT,
 voice_seconds INTEGER CHECK(voice_seconds IS NULL OR voice_seconds >= 0),
 total_grenades INTEGER CHECK(total_grenades IS NULL OR total_grenades >= 0),
 PRIMARY KEY(report_id, row_no)
);
CREATE TABLE IF NOT EXISTS kv_daily_grenade_stages(
 report_id INTEGER NOT NULL,
 row_no INTEGER NOT NULL,
 stage_no INTEGER NOT NULL CHECK(stage_no BETWEEN 1 AND 4),
 grenades INTEGER CHECK(grenades IS NULL OR grenades >= 0),
 PRIMARY KEY(report_id, row_no, stage_no),
 FOREIGN KEY(report_id, row_no) REFERENCES kv_daily_players(report_id, row_no) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_kv_reports_date_guild ON kv_daily_reports(match_date, guild_id);
CREATE INDEX IF NOT EXISTS idx_kv_players_player ON kv_daily_players(player_id, report_id);

-- v10: DB-authoritative bot state. JSON is only an import/export format.
CREATE TABLE IF NOT EXISTS bot_guild_config(
 guild_id INTEGER PRIMARY KEY,
 log_channel_id INTEGER,
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS guild_voice_channels(
 guild_id INTEGER NOT NULL REFERENCES bot_guild_config(guild_id) ON DELETE CASCADE,
 channel_id INTEGER NOT NULL,
 position INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(guild_id, channel_id), UNIQUE(guild_id, position)
);
CREATE TABLE IF NOT EXISTS guild_access_roles(
 guild_id INTEGER NOT NULL REFERENCES bot_guild_config(guild_id) ON DELETE CASCADE,
 role_id INTEGER NOT NULL,
 PRIMARY KEY(guild_id, role_id)
);
CREATE TABLE IF NOT EXISTS squad_names(
 guild_id INTEGER NOT NULL REFERENCES bot_guild_config(guild_id) ON DELETE CASCADE,
 squad_id INTEGER NOT NULL,
 name TEXT NOT NULL,
 updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 PRIMARY KEY(guild_id, squad_id)
);
CREATE TABLE IF NOT EXISTS roster_memberships(
 id INTEGER PRIMARY KEY,
 guild_id INTEGER NOT NULL REFERENCES bot_guild_config(guild_id) ON DELETE CASCADE,
 player_id INTEGER NOT NULL REFERENCES players(id),
 squad_id INTEGER,
 slot INTEGER,
 active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
 joined_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 deactivated_at TEXT,
 UNIQUE(guild_id, player_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_roster_active_slot
 ON roster_memberships(guild_id, squad_id, slot)
 WHERE active=1 AND squad_id IS NOT NULL AND slot IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_roster_guild_active_squad
 ON roster_memberships(guild_id, active, squad_id, slot);
CREATE INDEX IF NOT EXISTS idx_roster_player ON roster_memberships(player_id);
-- v11: an explicit /remove is durable and cannot be undone by Excel refresh.
CREATE TABLE IF NOT EXISTS roster_removals(
 guild_id INTEGER NOT NULL REFERENCES bot_guild_config(guild_id) ON DELETE CASCADE,
 player_id INTEGER NOT NULL REFERENCES players(id),
 removed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 reason TEXT NOT NULL DEFAULT 'explicit_remove',
 PRIMARY KEY(guild_id, player_id)
);
CREATE INDEX IF NOT EXISTS idx_roster_removals_player ON roster_removals(player_id);
CREATE TABLE IF NOT EXISTS runtime_sessions(
 id INTEGER PRIMARY KEY,
 guild_id INTEGER NOT NULL REFERENCES bot_guild_config(guild_id) ON DELETE CASCADE,
 session_date TEXT NOT NULL,
 grenade_date TEXT,
 active INTEGER NOT NULL DEFAULT 0 CHECK(active IN (0,1)),
 finished INTEGER NOT NULL DEFAULT 0 CHECK(finished IN (0,1)),
 last_grenade_step TEXT,
 skipped_grenade_steps_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(skipped_grenade_steps_json)),
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 UNIQUE(guild_id, session_date)
);
CREATE INDEX IF NOT EXISTS idx_runtime_sessions_current
 ON runtime_sessions(guild_id, session_date DESC);
CREATE TABLE IF NOT EXISTS attendance(
 session_id INTEGER NOT NULL REFERENCES runtime_sessions(id) ON DELETE CASCADE,
 player_id INTEGER NOT NULL REFERENCES players(id),
 came INTEGER NOT NULL DEFAULT 0 CHECK(came IN (0,1)),
 in_voice INTEGER NOT NULL DEFAULT 0 CHECK(in_voice IN (0,1)),
 updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 PRIMARY KEY(session_id, player_id)
);
CREATE TABLE IF NOT EXISTS discord_message_refs(
 guild_id INTEGER NOT NULL REFERENCES bot_guild_config(guild_id) ON DELETE CASCADE,
 ref_kind TEXT NOT NULL,
 channel_id INTEGER,
 message_id INTEGER,
 updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 PRIMARY KEY(guild_id, ref_kind)
);
CREATE TABLE IF NOT EXISTS session_maps(
 session_id INTEGER NOT NULL REFERENCES runtime_sessions(id) ON DELETE CASCADE,
 position INTEGER NOT NULL CHECK(position >= 0),
 map_name TEXT NOT NULL,
 PRIMARY KEY(session_id, position)
);
CREATE TABLE IF NOT EXISTS absent_dm_deliveries(
 session_id INTEGER NOT NULL REFERENCES runtime_sessions(id) ON DELETE CASCADE,
 discord_id TEXT NOT NULL,
 player_id INTEGER REFERENCES players(id),
 status TEXT NOT NULL DEFAULT 'sent' CHECK(status IN ('sent','failed','missing')),
 delivered_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 error_text TEXT,
 PRIMARY KEY(session_id, discord_id)
);
CREATE INDEX IF NOT EXISTS idx_absent_dm_player ON absent_dm_deliveries(player_id, session_id);
CREATE TABLE IF NOT EXISTS live_grenade_state(
 session_id INTEGER NOT NULL REFERENCES runtime_sessions(id) ON DELETE CASCADE,
 player_id INTEGER NOT NULL REFERENCES players(id),
 step_id TEXT NOT NULL,
 value INTEGER NOT NULL CHECK(value >= 0),
 updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 PRIMARY KEY(session_id, player_id, step_id)
);
CREATE INDEX IF NOT EXISTS idx_live_grenade_session_step ON live_grenade_state(session_id, step_id);
CREATE TABLE IF NOT EXISTS voice_checkpoints(
 session_id INTEGER NOT NULL REFERENCES runtime_sessions(id) ON DELETE CASCADE,
 discord_id TEXT NOT NULL,
 player_id INTEGER REFERENCES players(id),
 seconds REAL NOT NULL DEFAULT 0 CHECK(seconds >= 0),
 state_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(state_json)),
 updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 PRIMARY KEY(session_id, discord_id)
);
CREATE INDEX IF NOT EXISTS idx_voice_checkpoint_player ON voice_checkpoints(player_id, session_id);
CREATE TABLE IF NOT EXISTS bot_kv_state(
 guild_id INTEGER NOT NULL REFERENCES bot_guild_config(guild_id) ON DELETE CASCADE,
 state_key TEXT NOT NULL,
 value_json TEXT NOT NULL CHECK(json_valid(value_json)),
 updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 PRIMARY KEY(guild_id, state_key)
);
CREATE TABLE IF NOT EXISTS legacy_imports(
 source_path TEXT NOT NULL,
 source_sha256 TEXT NOT NULL,
 imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 schema_version INTEGER NOT NULL,
 guild_id INTEGER NOT NULL,
 row_counts_json TEXT NOT NULL CHECK(json_valid(row_counts_json)),
 PRIMARY KEY(source_path, source_sha256)
);
"""


def _display_nick(value: str) -> str:
    """Preserve spelling while normalizing Unicode and insignificant whitespace."""
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).strip().split())


def _norm(value: str) -> str:
    return _display_nick(value).casefold()


def damerau_levenshtein(left: str, right: str) -> int:
    """Unicode-aware optimal-string-alignment edit distance."""
    left, right = _norm(left), _norm(right)
    rows = [[0] * (len(right) + 1) for _ in range(len(left) + 1)]
    for i in range(len(left) + 1):
        rows[i][0] = i
    for j in range(len(right) + 1):
        rows[0][j] = j
    for i in range(1, len(left) + 1):
        for j in range(1, len(right) + 1):
            rows[i][j] = min(
                rows[i - 1][j] + 1,
                rows[i][j - 1] + 1,
                rows[i - 1][j - 1] + (left[i - 1] != right[j - 1]),
            )
            if i > 1 and j > 1 and left[i - 1] == right[j - 2] and left[i - 2] == right[j - 1]:
                rows[i][j] = min(rows[i][j], rows[i - 2][j - 2] + 1)
    return rows[-1][-1]


def _fuzzy_limit(normalized_nick: str) -> int:
    length = len(normalized_nick)
    if length < 5:
        return 0
    if length < 8:
        return 1
    return 2


class PlayerMatch(NamedTuple):
    player_id: int | None
    method: str
    distance: int | None = None
    second_distance: int | None = None


def _is_fuzzy_excluded(con: sqlite3.Connection, nickname: str, guild_id: int | None) -> bool:
    key = _norm(nickname)
    gids = (0, int(guild_id)) if guild_id is not None else (0,)
    placeholders = ",".join("?" for _ in gids)
    return con.execute(
        f"SELECT 1 FROM fuzzy_exclusions WHERE normalized_nick=? AND guild_id IN ({placeholders})",
        (key, *gids),
    ).fetchone() is not None


def match_player_in_connection(
    con: sqlite3.Connection, nickname: str, guild_id: int | None = None,
) -> PlayerMatch:
    """Resolve only against the curated roster; never learn from old OCR rows."""
    key = _norm(nickname)
    if not key:
        return PlayerMatch(None, "empty")
    exact = _identity_ids(con, nickname)
    if len(exact) == 1:
        return PlayerMatch(next(iter(exact)), "exact", 0)
    if len(exact) > 1:
        return PlayerMatch(None, "exact-conflict", 0)
    if _is_fuzzy_excluded(con, nickname, guild_id):
        return PlayerMatch(None, "excluded")
    limit = _fuzzy_limit(key)
    if limit == 0:
        return PlayerMatch(None, "too-short")

    params: tuple[Any, ...] = ()
    scope = ""
    if guild_id is not None:
        scope = " WHERE EXISTS (SELECT 1 FROM player_guilds pg WHERE pg.player_id=p.id AND pg.guild_id=?)"
        params = (int(guild_id),)
    names: dict[int, list[str]] = {}
    for row in con.execute(
        "SELECT p.id,p.canonical_nick FROM players p" + scope, params
    ):
        names.setdefault(int(row["id"]), []).append(str(row["canonical_nick"]))
    if names:
        placeholders = ",".join("?" for _ in names)
        for row in con.execute(
            f"SELECT player_id,alias FROM player_aliases WHERE player_id IN ({placeholders})",
            tuple(names),
        ):
            names[int(row["player_id"])].append(str(row["alias"]))
    ranked = sorted(
        (min(damerau_levenshtein(key, candidate) for candidate in candidates), pid)
        for pid, candidates in names.items()
    )
    if not ranked or ranked[0][0] > limit:
        return PlayerMatch(None, "no-close-candidate", ranked[0][0] if ranked else None)
    best_distance, best_id = ranked[0]
    second_distance = ranked[1][0] if len(ranked) > 1 else None
    # A two-edit gap is deliberate: distance 1 versus 2 is too risky for real close names.
    if second_distance is not None and second_distance < best_distance + 2:
        return PlayerMatch(None, "ambiguous", best_distance, second_distance)
    return PlayerMatch(best_id, "fuzzy", best_distance, second_distance)


def _identity_ids(con: sqlite3.Connection, nickname: str) -> set[int]:
    key = _norm(nickname)
    if not key:
        return set()
    ids: set[int] = set()
    for row in con.execute("SELECT id,canonical_nick FROM players"):
        if _norm(row["canonical_nick"]) == key:
            ids.add(int(row["id"]))
    for row in con.execute("SELECT alias,player_id FROM player_aliases"):
        if _norm(row["alias"]) == key:
            ids.add(int(row["player_id"]))
    return ids


def _merge_player(con: sqlite3.Connection, survivor: int, duplicate: int) -> None:
    """Merge a proven same-identity duplicate without multiplying statistics."""
    if survivor == duplicate:
        return
    con.execute("UPDATE discord_bindings SET player_id=? WHERE player_id=?", (survivor, duplicate))
    con.execute("UPDATE scan_players SET player_id=? WHERE player_id=?", (survivor, duplicate))
    con.execute("UPDATE voice_stats SET player_id=? WHERE player_id=?", (survivor, duplicate))
    con.execute("UPDATE kv_daily_players SET player_id=? WHERE player_id=?", (survivor, duplicate))
    for row in con.execute("SELECT guild_id,removed_at,reason FROM roster_removals WHERE player_id=?", (duplicate,)):
        con.execute("""INSERT INTO roster_removals(guild_id,player_id,removed_at,reason)
            VALUES(?,?,?,?) ON CONFLICT(guild_id,player_id) DO UPDATE SET
            removed_at=MIN(roster_removals.removed_at,excluded.removed_at)""",
            (row["guild_id"], survivor, row["removed_at"], row["reason"]))
    con.execute("DELETE FROM roster_removals WHERE player_id=?", (duplicate,))
    for row in con.execute("SELECT stat_key,value FROM grenade_stats WHERE player_id=?", (duplicate,)):
        con.execute("""INSERT INTO grenade_stats(player_id,stat_key,value) VALUES(?,?,?)
                       ON CONFLICT(player_id,stat_key) DO UPDATE SET value=MAX(value,excluded.value)""",
                    (survivor, row["stat_key"], row["value"]))
    con.execute("DELETE FROM grenade_stats WHERE player_id=?", (duplicate,))
    for row in con.execute("SELECT guild_id FROM player_guilds WHERE player_id=?", (duplicate,)):
        con.execute("INSERT OR IGNORE INTO player_guilds(player_id,guild_id) VALUES(?,?)",
                    (survivor, row["guild_id"]))
    con.execute("DELETE FROM player_guilds WHERE player_id=?", (duplicate,))
    con.execute("UPDATE player_aliases SET player_id=? WHERE player_id=?", (survivor, duplicate))
    con.execute("DELETE FROM players WHERE id=?", (duplicate,))


def _reconcile_unicode_identities(con: sqlite3.Connection) -> None:
    """Merge legacy players connected by equal Unicode-casefolded names/aliases."""
    parent: dict[int, int] = {}

    def find(value: int) -> int:
        parent.setdefault(value, value)
        while parent[value] != value:
            parent[value] = parent[parent[value]]
            value = parent[value]
        return value

    def union(left: int, right: int) -> None:
        left, right = find(left), find(right)
        if left != right:
            parent[max(left, right)] = min(left, right)

    owners: dict[str, int] = {}
    identities = [(int(r["id"]), str(r["canonical_nick"]))
                  for r in con.execute("SELECT id,canonical_nick FROM players")]
    identities += [(int(r["player_id"]), str(r["alias"]))
                   for r in con.execute("SELECT player_id,alias FROM player_aliases")]
    for player_id, name in identities:
        key = _norm(name)
        if not key:
            continue
        if key in owners:
            union(owners[key], player_id)
        else:
            owners[key] = player_id
    groups: dict[int, list[int]] = {}
    for player_id in parent:
        groups.setdefault(find(player_id), []).append(player_id)
    for ids in groups.values():
        survivor = min(ids)
        for duplicate in sorted(ids):
            _merge_player(con, survivor, duplicate)


def connect(db_path: Path | str) -> sqlite3.Connection:
    con = sqlite3.connect(str(db_path), timeout=10, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA busy_timeout=10000")
    return con


def ensure_schema(db_path: Path | str) -> None:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(connect(path)) as con:
        con.executescript(SCHEMA)
        con.executescript("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_kv_daily_report_day_guild ON kv_daily_reports(match_date,guild_id);
            CREATE INDEX IF NOT EXISTS idx_kv_daily_players_player ON kv_daily_players(player_id,report_id);
            CREATE INDEX IF NOT EXISTS idx_kv_daily_stages_report_row ON kv_daily_grenade_stages(report_id,row_no,stage_no);
        """)
        con.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        cols = {r[1] for r in con.execute("PRAGMA table_info(scan_players)")} if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='scan_players'").fetchone() else set()
        if cols and "player_id" not in cols:
            con.execute("ALTER TABLE scan_players ADD COLUMN player_id INTEGER REFERENCES players(id)")
        score_info = next((r for r in con.execute("PRAGMA table_info(scan_players)") if r[1] == "score"), None)
        if score_info is not None and int(score_info[3]) == 1:
            con.execute("BEGIN IMMEDIATE")
            try:
                con.execute("""CREATE TABLE scan_players_nullable(
                    id INTEGER PRIMARY KEY,
                    scan_id INTEGER NOT NULL REFERENCES scans(id) ON DELETE CASCADE,
                    place INTEGER NOT NULL,
                    nick TEXT NOT NULL,
                    kills INTEGER NOT NULL,
                    deaths INTEGER NOT NULL,
                    assists INTEGER NOT NULL,
                    score INTEGER,
                    player_id INTEGER REFERENCES players(id)
                )""")
                con.execute("""INSERT INTO scan_players_nullable
                    (id,scan_id,place,nick,kills,deaths,assists,score,player_id)
                    SELECT id,scan_id,place,nick,kills,deaths,assists,score,player_id
                    FROM scan_players""")
                con.execute("DROP TABLE scan_players")
                con.execute("ALTER TABLE scan_players_nullable RENAME TO scan_players")
                con.commit()
            except Exception:
                con.rollback()
                raise
        scan_cols = {r[1] for r in con.execute("PRAGMA table_info(scans)")}
        if "content_fingerprint" not in scan_cols:
            con.execute("ALTER TABLE scans ADD COLUMN content_fingerprint TEXT")
        if "match_date" not in scan_cols:
            con.execute("ALTER TABLE scans ADD COLUMN match_date TEXT")
        stage_info = next((r for r in con.execute("PRAGMA table_info(scans)") if r[1] == "stage"), None)
        if stage_info is not None and int(stage_info[3]) == 1:
            con.execute("PRAGMA foreign_keys=OFF")
            con.execute("BEGIN IMMEDIATE")
            try:
                con.execute("""CREATE TABLE scans_nullable_stage(
                    id INTEGER PRIMARY KEY, stage INTEGER, map TEXT NOT NULL,
                    guild_id INTEGER, channel_id INTEGER, user_id INTEGER NOT NULL,
                    scanned_at TEXT NOT NULL, source_filename TEXT NOT NULL,
                    content_fingerprint TEXT, match_date TEXT
                )""")
                con.execute("""INSERT INTO scans_nullable_stage
                    SELECT id,stage,map,guild_id,channel_id,user_id,scanned_at,
                           source_filename,content_fingerprint,match_date FROM scans""")
                con.execute("DROP TABLE scans")
                con.execute("ALTER TABLE scans_nullable_stage RENAME TO scans")
                con.commit()
            except Exception:
                con.rollback()
                raise
            finally:
                con.execute("PRAGMA foreign_keys=ON")
        for row in con.execute("SELECT id,scanned_at FROM scans WHERE match_date IS NULL"):
            migrated_date = _match_date_from_timestamp(row["scanned_at"])
            if migrated_date:
                con.execute("UPDATE scans SET match_date=? WHERE id=?", (migrated_date, row["id"]))
        con.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_scans_content_fingerprint ON scans(content_fingerprint) WHERE content_fingerprint IS NOT NULL")
        con.execute("CREATE INDEX IF NOT EXISTS idx_scans_match_date ON scans(match_date)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_scans_map ON scans(map)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_scans_scanned_at ON scans(scanned_at)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_scan_players_player_id ON scan_players(player_id)")
        _reconcile_unicode_identities(con)
        con.execute("INSERT OR REPLACE INTO schema_meta(key,value) VALUES('schema_version',?)", (str(SCHEMA_VERSION),))


def _player_id(con: sqlite3.Connection, canonical_nick: str) -> int:
    nick = _display_nick(canonical_nick)
    if not nick:
        raise ValueError("empty canonical nickname")
    ids = _identity_ids(con, nick)
    if len(ids) > 1:
        raise ValueError("nickname conflicts with multiple existing players/aliases")
    if ids:
        return next(iter(ids))
    con.execute("INSERT INTO players(canonical_nick) VALUES(?)", (nick,))
    return int(con.execute("SELECT last_insert_rowid()").fetchone()[0])


def resolve_player_id_in_connection(
    con: sqlite3.Connection, nickname: str, guild_id: int | None = None,
) -> int | None:
    return match_player_in_connection(con, nickname, guild_id).player_id


def resolve_player_id(db_path: Path | str, nickname: str, guild_id: int | None = None) -> int | None:
    ensure_schema(db_path)
    with closing(connect(db_path)) as con:
        return resolve_player_id_in_connection(con, nickname, guild_id)


def upsert_binding(db_path: Path | str, discord_id: str | int, canonical_nick: str,
                   discord_name: str = "", discord_username: str = "",
                   guild_id: int | None = None) -> int:
    ensure_schema(db_path)
    with closing(connect(db_path)) as con:
        pid = _player_id(con, canonical_nick)
        con.execute("""INSERT INTO discord_bindings(discord_id,player_id,discord_name,discord_username)
                       VALUES(?,?,?,?)
                       ON CONFLICT(discord_id) DO UPDATE SET player_id=excluded.player_id,
                       discord_name=excluded.discord_name,discord_username=excluded.discord_username,
                       updated_at=CURRENT_TIMESTAMP""", (str(discord_id), pid, discord_name, discord_username))
        if guild_id is not None:
            con.execute("INSERT OR IGNORE INTO player_guilds(player_id,guild_id) VALUES(?,?)",
                        (pid, int(guild_id)))
        return pid


def add_alias(db_path: Path | str, canonical_nick: str, alias: str) -> int:
    canonical_nick, alias = _display_nick(canonical_nick), _display_nick(alias)
    if not canonical_nick or not alias:
        raise ValueError("canonical nickname and alias are required")
    ensure_schema(db_path)
    with closing(connect(db_path)) as con:
        pid = _player_id(con, canonical_nick)
        owners = _identity_ids(con, alias)
        if owners and owners != {pid}:
            raise ValueError("alias is already owned by another player")
        if owners == {pid}:
            return pid
        con.execute("INSERT INTO player_aliases(alias,player_id) VALUES(?,?)", (alias, pid))
        return pid


def sync_grenade_history(db_path: Path | str, history: dict[str, Any]) -> int:
    ensure_schema(db_path)
    changed = 0
    with closing(connect(db_path)) as con:
        for nick, stats in (history or {}).items():
            if not isinstance(stats, dict):
                continue
            ids = _identity_ids(con, str(nick))
            pid = next(iter(ids)) if len(ids) == 1 else _player_id(con, str(nick))
            for key, value in stats.items():
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    continue
                con.execute("""INSERT INTO grenade_stats(player_id,stat_key,value) VALUES(?,?,?)
                               ON CONFLICT(player_id,stat_key) DO UPDATE SET value=excluded.value""", (pid, str(key), int(value)))
                changed += 1
    return changed


class ScanSaveResult(NamedTuple):
    scan_id: int
    created: bool


def save_grenade_session(db_path: Path | str, history: dict[str, Any], match_date: str, source_key: str, guild_id: int | None = None) -> int:
    """Persist an idempotent, explicitly dated grenade session snapshot."""
    day = parse_match_date(match_date)
    if not source_key:
        raise ValueError("source_key is required")
    ensure_schema(db_path)
    written = 0
    with closing(connect(db_path)) as con:
        for nick, stats in (history or {}).items():
            if not isinstance(stats, dict):
                continue
            ids = _identity_ids(con, str(nick))
            pid = next(iter(ids)) if len(ids) == 1 else _player_id(con, str(nick))
            if guild_id is not None:
                con.execute("INSERT OR IGNORE INTO player_guilds(player_id,guild_id) VALUES(?,?)", (pid, int(guild_id)))
            for key, value in stats.items():
                if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
                    continue
                con.execute("""INSERT INTO grenade_session_stats(source_key,player_id,stat_key,value,match_date,guild_id)
                    VALUES(?,?,?,?,?,?) ON CONFLICT(source_key,player_id,stat_key) DO UPDATE SET
                    value=excluded.value,match_date=excluded.match_date,guild_id=excluded.guild_id,updated_at=CURRENT_TIMESTAMP""",
                    (source_key, pid, str(key), int(value), day, guild_id))
                written += 1
    return written


def list_stats_dates(db_path: Path | str, guild_id: int | None = None) -> list[dict[str, Any]]:
    ensure_schema(db_path)
    with closing(connect(db_path)) as con:
        params: tuple[Any, ...] = () if guild_id is None else (int(guild_id),)
        sw = "match_date IS NOT NULL" + (" AND guild_id=?" if guild_id is not None else "")
        gw = "match_date IS NOT NULL" + (" AND guild_id=?" if guild_id is not None else "")
        vw = "v.session_date IS NOT NULL" + (" AND EXISTS (SELECT 1 FROM player_guilds pg WHERE pg.player_id=v.player_id AND pg.guild_id=?)" if guild_id is not None else "")
        dates: dict[str, dict[str, Any]] = {}
        for source, sql in (("tabs", f"SELECT match_date d,COUNT(DISTINCT id) n FROM scans WHERE {sw} GROUP BY match_date"), ("grenades", f"SELECT match_date d,COUNT(DISTINCT source_key) n FROM grenade_session_stats WHERE {gw} GROUP BY match_date"), ("voice", f"SELECT v.session_date d,COUNT(DISTINCT v.source_key) n FROM voice_stats v WHERE {vw} GROUP BY v.session_date")):
            for row in con.execute(sql, params):
                dates.setdefault(str(row["d"]), {"date": str(row["d"]), "tabs": 0, "grenades": 0, "voice": 0})[source] = int(row["n"])
        return [dates[key] for key in sorted(dates, reverse=True)]


def _short_scan_nick(value: Any, width: int = 18) -> str:
    """Compact a nickname without exposing anything beyond normalized scan data."""
    nick = _display_nick(str(value or "")) or "?"
    return nick if len(nick) <= width else nick[: width - 1] + "…"


def format_scan_private_report(
    rows: list[dict[str, Any]], *, match_date: str | None, map_name: str | None,
    save_result: ScanSaveResult, limit: int = 1900,
) -> list[str]:
    """Render a compact Discord-safe report and split it into fenced chunks."""
    if limit < 200:
        raise ValueError("report chunk limit is too small")
    status = "создан новый скан" if save_result.created else "обнаружен дубль"
    heading = (
        f"Скан #{save_result.scan_id}: {status}. Дата КВ: {match_date or '—'}; "
        f"карта: {map_name or '—'}; строк: {len(rows)}."
    )
    table_header = "Мес Ник                 K   D   A    Score"
    separator = "--- ------------------ --- --- --- --------"
    lines = [
        f"{int(row['place']):>3} {_short_scan_nick(row.get('nickname')):<18} "
        f"{int(row['kills']):>3} {int(row['deaths']):>3} "
        f"{int(row['assists']):>3} "
        f"{(str(int(row['score'])) if row.get('score') is not None else '—'):>8}"
        for row in rows
    ]
    chunks: list[str] = []
    current = heading + "\n```\n" + table_header + "\n" + separator
    for line in lines:
        candidate = current + "\n" + line + "\n```"
        if len(candidate) > limit and current != heading + "\n```\n" + table_header + "\n" + separator:
            chunks.append(current + "\n```")
            current = "Продолжение отчёта\n```\n" + table_header + "\n" + separator + "\n" + line
        else:
            current += "\n" + line
    chunks.append(current + "\n```")
    return chunks


def scan_content_fingerprint(
    rows: list[dict[str, Any]], *, match_date: str, map_name: str, guild_id: int | None,
    stage: int | None = None,
) -> str:
    """Hash semantic content; stage is intentionally ignored for new scans."""
    normalized_rows = sorted(
        (
            int(row["place"]), _norm(str(row["nickname"])), int(row["kills"]),
            int(row["deaths"]), int(row["assists"]),
            int(row["score"]) if row.get("score") is not None else None,
        )
        for row in rows
    )
    payload = {
        "version": 2,
        "guild_id": int(guild_id) if guild_id is not None else None,
        "match_date": parse_match_date(match_date),
        "map": _norm(map_name),
        "players": normalized_rows,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def save_scan_result(
    db_path: Path | str,
    rows: list[dict[str, Any]],
    *,
    match_date: str,
    map_name: str,
    guild_id: int | None,
    channel_id: int | None,
    user_id: int,
    scanned_at: str,
    source_filename: str,
) -> ScanSaveResult:
    """Atomically save one semantic scoreboard per guild and KV date."""
    normalized_date = parse_match_date(match_date)
    ensure_schema(db_path)
    fingerprint = scan_content_fingerprint(
        rows, match_date=normalized_date, map_name=map_name, guild_id=guild_id
    )
    with closing(connect(db_path)) as con:
        con.execute("BEGIN IMMEDIATE")
        try:
            cursor = con.execute(
                """INSERT OR IGNORE INTO scans
                   (stage,map,guild_id,channel_id,user_id,scanned_at,source_filename,
                    content_fingerprint,match_date)
                   VALUES(NULL,?,?,?,?,?,?,?,?)""",
                (map_name, guild_id, channel_id, user_id, scanned_at,
                 source_filename, fingerprint, normalized_date),
            )
            if cursor.rowcount == 0:
                existing = con.execute(
                    "SELECT id FROM scans WHERE content_fingerprint=?", (fingerprint,)
                ).fetchone()
                if existing is None:
                    raise sqlite3.IntegrityError("scan fingerprint conflict without existing row")
                con.commit()
                return ScanSaveResult(int(existing[0]), False)
            scan_id = int(cursor.lastrowid)
            con.executemany(
                """INSERT INTO scan_players
                   (scan_id,place,nick,kills,deaths,assists,score,player_id)
                   VALUES(?,?,?,?,?,?,?,?)""",
                [(
                    scan_id, int(row["place"]), str(row["nickname"]).strip(),
                    int(row["kills"]), int(row["deaths"]), int(row["assists"]),
                    int(row["score"]) if row.get("score") is not None else None,
                    resolve_player_id_in_connection(
                        con, str(row["nickname"]).strip(), guild_id
                    ),
                ) for row in rows],
            )
            con.commit()
            return ScanSaveResult(scan_id, True)
        except Exception:
            con.rollback()
            raise


def get_scan_for_guild(
    db_path: Path | str, scan_id: int, guild_id: int,
) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
    """Return one complete scan only when it belongs to the requesting guild."""
    ensure_schema(db_path)
    with closing(connect(db_path)) as con:
        scan = con.execute(
            """SELECT id,map,guild_id,scanned_at,source_filename,match_date
               FROM scans WHERE id=? AND guild_id=?""",
            (int(scan_id), int(guild_id)),
        ).fetchone()
        if scan is None:
            return None
        rows = con.execute(
            """SELECT place,nick AS nickname,kills,deaths,assists,score
               FROM scan_players WHERE scan_id=? ORDER BY place,id""",
            (int(scan_id),),
        ).fetchall()
        return dict(scan), [dict(row) for row in rows]


def list_scan_dates(db_path: Path | str, guild_id: int) -> list[dict[str, Any]]:
    """List dated scoreboard counts for one guild, newest first."""
    ensure_schema(db_path)
    with closing(connect(db_path)) as con:
        return [dict(row) for row in con.execute(
            """SELECT match_date,COUNT(*) AS scan_count FROM scans
               WHERE guild_id=? AND match_date IS NOT NULL
               GROUP BY match_date ORDER BY match_date DESC""",
            (int(guild_id),),
        )]


def link_scan_rows(db_path: Path | str) -> int:
    """Safely migrate unlinked rows; OCR spelling remains untouched and no aliases are learned."""
    ensure_schema(db_path)
    with closing(connect(db_path)) as con:
        exists = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='scan_players'").fetchone()
        if not exists:
            return 0
        rows = con.execute(
            """SELECT sp.id,sp.nick,s.guild_id FROM scan_players sp
               JOIN scans s ON s.id=sp.scan_id WHERE sp.player_id IS NULL"""
        ).fetchall()
        linked = 0
        for row in rows:
            match = match_player_in_connection(con, str(row["nick"]), row["guild_id"])
            if match.player_id is not None:
                con.execute("UPDATE scan_players SET player_id=? WHERE id=?",
                            (match.player_id, row["id"]))
                linked += 1
        return linked


def sync_voice_snapshot(db_path: Path | str, totals: dict[str, Any], source_key: str,
                        session_date: str | None = None) -> int:
    """Replace one session snapshot; exact Discord bindings are the only identity link."""
    if not source_key:
        raise ValueError("source_key is required")
    ensure_schema(db_path)
    written = 0
    with closing(connect(db_path)) as con:
        for discord_id, value in (totals or {}).items():
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            seconds = float(value)
            if seconds < 0:
                continue
            did = str(discord_id)
            binding = con.execute(
                "SELECT player_id FROM discord_bindings WHERE discord_id=?", (did,)
            ).fetchone()
            player_id = int(binding[0]) if binding else None
            con.execute("""INSERT INTO voice_stats(source_key,discord_id,player_id,seconds,session_date)
                           VALUES(?,?,?,?,?)
                           ON CONFLICT(source_key,discord_id) DO UPDATE SET
                           player_id=excluded.player_id,seconds=excluded.seconds,
                           session_date=excluded.session_date,updated_at=CURRENT_TIMESTAMP""",
                        (source_key, did, player_id, seconds, session_date))
            written += 1
    return written


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


# Stable public schema returned by migration/reconciliation status dictionaries.
# Keep every key present even while upgrading an older/partially-created database.
STATUS_COUNT_TABLES = (
    "players", "discord_bindings", "player_aliases", "player_guilds",
    "roster_memberships", "roster_removals", "runtime_sessions", "attendance", "discord_message_refs",
    "session_maps", "absent_dm_deliveries", "live_grenade_state", "voice_checkpoints",
    "bot_kv_state", "grenade_stats", "grenade_session_stats", "voice_stats",
    "scans", "scan_players", "kv_daily_reports", "kv_daily_players",
    "kv_daily_grenade_stages",
)


def _table_counts(con: sqlite3.Connection) -> dict[str, int]:
    existing = {str(row[0]) for row in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    return {
        name: (int(con.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0])
               if name in existing else 0)
        for name in STATUS_COUNT_TABLES
    }


def backup_cutover_files(db_path: Path | str, legacy_json: Path | str) -> list[str]:
    """Create durable, timestamped JSON and transaction-consistent SQLite backups."""
    db_path, legacy_json = Path(db_path), Path(legacy_json)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    made: list[str] = []
    if legacy_json.exists():
        target = legacy_json.with_name(legacy_json.name + f".{stamp}.pre-sqlite.bak")
        shutil.copy2(legacy_json, target)
        made.append(str(target))
    if db_path.exists():
        target = db_path.with_name(db_path.name + f".{stamp}.pre-sqlite.bak")
        source_con = sqlite3.connect(str(db_path), timeout=10)
        target_con = sqlite3.connect(str(target))
        try:
            source_con.backup(target_con)
        finally:
            target_con.close()
            source_con.close()
        made.append(str(target))
    return made


def legacy_cutover_complete(db_path: Path | str) -> bool:
    """Return whether the one-time legacy import marker exists."""
    ensure_schema(db_path)
    with closing(connect(db_path)) as con:
        return con.execute(
            "SELECT 1 FROM schema_meta WHERE key='legacy_cutover_complete'"
        ).fetchone() is not None


def ensure_legacy_cutover(db_path: Path | str, legacy_json: Path | str) -> dict[str, Any]:
    """Import JSON exactly once; after the marker it is never read as a fallback."""
    if legacy_cutover_complete(db_path):
        with closing(connect(db_path)) as con:
            return {"skipped": True, "reason": "legacy cutover complete", "counts": _table_counts(con)}
    return migrate(db_path, legacy_json, backup=False)


def _runtime_guild_id(con: sqlite3.Connection, preferred: int | None = None) -> int:
    if preferred:
        return int(preferred)
    row = con.execute(
        "SELECT guild_id FROM bot_guild_config ORDER BY (guild_id=0), guild_id LIMIT 1"
    ).fetchone()
    return int(row[0]) if row is not None else 0


def load_bot_snapshot(db_path: Path | str, guild_id: int | None = None) -> dict[str, Any]:
    """Build the legacy-shaped runtime object exclusively from normalized SQLite state."""
    ensure_schema(db_path)
    with closing(connect(db_path)) as con:
        gid = _runtime_guild_id(con, guild_id)
        cfg_row = con.execute(
            "SELECT log_channel_id FROM bot_guild_config WHERE guild_id=?", (gid,)
        ).fetchone()
        config = {
            "guild_id": gid or None,
            "log_channel_id": cfg_row[0] if cfg_row else None,
            "voice_channel_ids": [int(r[0]) for r in con.execute(
                "SELECT channel_id FROM guild_voice_channels WHERE guild_id=? ORDER BY position", (gid,)
            )],
            "access_role_ids": [int(r[0]) for r in con.execute(
                "SELECT role_id FROM guild_access_roles WHERE guild_id=? ORDER BY role_id", (gid,)
            )],
        }
        players: dict[str, dict[str, Any]] = {}
        player_to_did: dict[int, str] = {}
        for row in con.execute("""SELECT r.player_id,r.squad_id,r.slot,p.canonical_nick,
                d.discord_id,d.discord_name,d.discord_username
            FROM roster_memberships r JOIN players p ON p.id=r.player_id
            LEFT JOIN discord_bindings d ON d.player_id=p.id
            WHERE r.guild_id=? AND r.active=1
            ORDER BY r.squad_id,r.slot,p.canonical_nick""", (gid,)):
            if row["discord_id"] is None:
                continue
            did = str(row["discord_id"])
            player_to_did[int(row["player_id"])] = did
            players[did] = {
                "discord_name": row["discord_name"] or "",
                "discord_username": row["discord_username"] or "",
                "game_nick": row["canonical_nick"],
                "came": False, "in_voice": False,
                "squad": row["squad_id"], "slot": row["slot"],
            }
        data: dict[str, Any] = {
            "players": players,
            "message_ids": {"online": None, "grenades": None},
            "session_date": None, "grenade_date": None,
            "grenade_history": {}, "voice_speak_seconds": {},
            "skipped_grenade_steps": [], "last_grenade_step": None,
            "kv_finished": False, "kv_session_active": False,
            "absent_dm_sent": [],
            "squad_names": {str(r[0]): str(r[1]) for r in con.execute(
                "SELECT squad_id,name FROM squad_names WHERE guild_id=? ORDER BY squad_id", (gid,)
            )},
            "kv_maps": {"date": None, "maps": []},
            "config": config,
        }
        for row in con.execute(
            "SELECT ref_kind,message_id FROM discord_message_refs WHERE guild_id=?", (gid,)
        ):
            data["message_ids"][str(row[0])] = row[1]
        session = con.execute(
            "SELECT * FROM runtime_sessions WHERE guild_id=? ORDER BY session_date DESC,id DESC LIMIT 1", (gid,)
        ).fetchone()
        if session is None:
            return data
        sid = int(session["id"])
        day = str(session["session_date"])
        data.update({
            "session_date": day, "grenade_date": session["grenade_date"],
            "kv_session_active": bool(session["active"]), "kv_finished": bool(session["finished"]),
            "last_grenade_step": session["last_grenade_step"],
            "skipped_grenade_steps": json.loads(session["skipped_grenade_steps_json"]),
        })
        for row in con.execute("SELECT player_id,came,in_voice FROM attendance WHERE session_id=?", (sid,)):
            did = player_to_did.get(int(row["player_id"]))
            if did in players:
                players[did]["came"] = bool(row["came"])
                players[did]["in_voice"] = bool(row["in_voice"])
        maps = [str(r[0]) for r in con.execute(
            "SELECT map_name FROM session_maps WHERE session_id=? ORDER BY position", (sid,)
        )]
        data["kv_maps"] = {"date": day if maps else None, "maps": maps}
        data["absent_dm_sent"] = [str(r[0]) for r in con.execute(
            "SELECT discord_id FROM absent_dm_deliveries WHERE session_id=? ORDER BY discord_id", (sid,)
        )]
        data["voice_speak_seconds"] = {str(r[0]): float(r[1]) for r in con.execute(
            "SELECT discord_id,seconds FROM voice_checkpoints WHERE session_id=?", (sid,)
        )}
        for row in con.execute("""SELECT g.player_id,g.step_id,g.value,p.canonical_nick
            FROM live_grenade_state g JOIN players p ON p.id=g.player_id WHERE g.session_id=?""", (sid,)):
            data["grenade_history"].setdefault(str(row["canonical_nick"]), {})[str(row["step_id"])] = int(row["value"])
        # Closed sessions have no resumable rows. Hydrate the compatibility view
        # from their deterministic historical snapshots without reopening state.
        if bool(session["finished"]):
            source = f"runtime-kv:{gid}:{day}"
            if not data["voice_speak_seconds"]:
                data["voice_speak_seconds"] = {str(r[0]): float(r[1]) for r in con.execute(
                    "SELECT discord_id,seconds FROM voice_stats WHERE source_key=?", (source,)
                )}
            if not data["grenade_history"]:
                for row in con.execute("""SELECT h.stat_key,h.value,p.canonical_nick
                    FROM grenade_session_stats h JOIN players p ON p.id=h.player_id
                    WHERE h.source_key=?""", (source,)):
                    data["grenade_history"].setdefault(str(row["canonical_nick"]), {})[
                        str(row["stat_key"])
                    ] = int(row["value"])
        return data


def save_bot_snapshot(db_path: Path | str, data: dict[str, Any], guild_id: int | None = None) -> int:
    """Transactionally persist the legacy-shaped runtime object to SQLite only."""
    ensure_schema(db_path)
    cfg = data.get("config") if isinstance(data.get("config"), dict) else {}
    gid = int(guild_id or cfg.get("guild_id") or 0)
    with closing(connect(db_path)) as con:
        con.execute("BEGIN IMMEDIATE")
        try:
            con.execute("""INSERT INTO bot_guild_config(guild_id,log_channel_id) VALUES(?,?)
                ON CONFLICT(guild_id) DO UPDATE SET log_channel_id=excluded.log_channel_id,
                updated_at=CURRENT_TIMESTAMP""", (gid, cfg.get("log_channel_id")))
            con.execute("DELETE FROM guild_voice_channels WHERE guild_id=?", (gid,))
            con.executemany("INSERT INTO guild_voice_channels(guild_id,channel_id,position) VALUES(?,?,?)",
                [(gid, int(cid), pos) for pos, cid in enumerate(dict.fromkeys(cfg.get("voice_channel_ids") or []))])
            con.execute("DELETE FROM guild_access_roles WHERE guild_id=?", (gid,))
            con.executemany("INSERT INTO guild_access_roles(guild_id,role_id) VALUES(?,?)",
                [(gid, int(rid)) for rid in dict.fromkeys(cfg.get("access_role_ids") or [])])

            current: dict[str, int] = {}
            for did, record in (data.get("players") or {}).items():
                nick = str(record.get("game_nick") or "").strip()
                if not nick:
                    continue
                did = str(did)
                binding = con.execute(
                    "SELECT player_id FROM discord_bindings WHERE discord_id=?", (did,)
                ).fetchone()
                if binding is None:
                    pid = _player_id(con, nick)
                else:
                    # A Discord binding is the stable identity.  In particular, an
                    # Excel nickname edit must rename that player instead of creating
                    # a second identity and moving the binding away from its history.
                    pid = int(binding[0])
                    owner_ids = _identity_ids(con, nick)
                    if owner_ids and owner_ids != {pid}:
                        raise ValueError(f"nickname {nick!r} belongs to another player")
                    old_row = con.execute(
                        "SELECT canonical_nick FROM players WHERE id=?", (pid,)
                    ).fetchone()
                    old_nick = str(old_row[0]) if old_row else ""
                    if old_nick and _norm(old_nick) != _norm(nick):
                        con.execute("UPDATE players SET canonical_nick=? WHERE id=?", (nick, pid))
                        # Keep the previous spelling/name resolvable for old scans and
                        # imported reports without changing their player_id.
                        con.execute(
                            "INSERT OR IGNORE INTO player_aliases(alias,player_id) VALUES(?,?)",
                            (old_nick, pid),
                        )
                    elif old_nick != nick:
                        con.execute("UPDATE players SET canonical_nick=? WHERE id=?", (nick, pid))
                current[did] = pid
                con.execute("""INSERT INTO discord_bindings(discord_id,player_id,discord_name,discord_username)
                    VALUES(?,?,?,?) ON CONFLICT(discord_id) DO UPDATE SET player_id=excluded.player_id,
                    discord_name=excluded.discord_name,discord_username=excluded.discord_username,
                    updated_at=CURRENT_TIMESTAMP""", (did, pid, str(record.get("discord_name") or ""),
                    str(record.get("discord_username") or "")))
                con.execute("INSERT OR IGNORE INTO player_guilds(player_id,guild_id) VALUES(?,?)", (pid, gid))
            # Deactivate first so slot swaps and removals cannot violate the partial unique index.
            con.execute("""UPDATE roster_memberships SET active=0,
                deactivated_at=COALESCE(deactivated_at,CURRENT_TIMESTAMP),updated_at=CURRENT_TIMESTAMP
                WHERE guild_id=?""", (gid,))
            for did, pid in current.items():
                record = data["players"][did]
                con.execute("""INSERT INTO roster_memberships(guild_id,player_id,squad_id,slot,active,deactivated_at)
                    VALUES(?,?,?,?,1,NULL) ON CONFLICT(guild_id,player_id) DO UPDATE SET
                    squad_id=excluded.squad_id,slot=excluded.slot,active=1,deactivated_at=NULL,
                    updated_at=CURRENT_TIMESTAMP""", (gid, pid, record.get("squad"), record.get("slot")))

            con.execute("DELETE FROM squad_names WHERE guild_id=?", (gid,))
            con.executemany("INSERT INTO squad_names(guild_id,squad_id,name) VALUES(?,?,?)",
                [(gid, int(k), str(v)) for k, v in (data.get("squad_names") or {}).items()])
            con.execute("DELETE FROM discord_message_refs WHERE guild_id=?", (gid,))
            con.executemany("INSERT INTO discord_message_refs(guild_id,ref_kind,message_id) VALUES(?,?,?)",
                [(gid, str(k), v) for k, v in (data.get("message_ids") or {}).items()])

            day_raw = data.get("session_date") or data.get("grenade_date")
            if day_raw:
                day = parse_match_date(str(day_raw))
                con.execute("""INSERT INTO runtime_sessions
                    (guild_id,session_date,grenade_date,active,finished,last_grenade_step,skipped_grenade_steps_json)
                    VALUES(?,?,?,?,?,?,?) ON CONFLICT(guild_id,session_date) DO UPDATE SET
                    grenade_date=excluded.grenade_date,active=excluded.active,finished=excluded.finished,
                    last_grenade_step=excluded.last_grenade_step,
                    skipped_grenade_steps_json=excluded.skipped_grenade_steps_json,updated_at=CURRENT_TIMESTAMP""",
                    (gid, day, data.get("grenade_date"), int(bool(data.get("kv_session_active"))),
                     int(bool(data.get("kv_finished"))), data.get("last_grenade_step"),
                     _json_text(data.get("skipped_grenade_steps") or [])))
                sid = int(con.execute("SELECT id FROM runtime_sessions WHERE guild_id=? AND session_date=?",
                                      (gid, day)).fetchone()[0])
                for table in ("attendance", "session_maps", "absent_dm_deliveries", "live_grenade_state", "voice_checkpoints"):
                    con.execute(f"DELETE FROM {table} WHERE session_id=?", (sid,))
                con.executemany("INSERT INTO attendance(session_id,player_id,came,in_voice) VALUES(?,?,?,?)",
                    [(sid, current[did], int(bool(rec.get("came"))), int(bool(rec.get("in_voice"))))
                     for did, rec in (data.get("players") or {}).items() if did in current])
                maps = data.get("kv_maps") or {}
                con.executemany("INSERT INTO session_maps(session_id,position,map_name) VALUES(?,?,?)",
                    [(sid, pos, str(name)) for pos, name in enumerate(maps.get("maps") or [])])
                con.executemany("INSERT INTO absent_dm_deliveries(session_id,discord_id,player_id) VALUES(?,?,?)",
                    [(sid, str(did), current.get(str(did))) for did in dict.fromkeys(data.get("absent_dm_sent") or [])])
                con.executemany("INSERT INTO voice_checkpoints(session_id,discord_id,player_id,seconds) VALUES(?,?,?,?)",
                    [(sid, str(did), current.get(str(did)), float(value)) for did, value in
                     (data.get("voice_speak_seconds") or {}).items() if not isinstance(value, bool) and isinstance(value, (int, float)) and value >= 0])
                grenade_rows = []
                for nick, values in (data.get("grenade_history") or {}).items():
                    if not isinstance(values, dict):
                        continue
                    ids = _identity_ids(con, str(nick))
                    if len(ids) != 1:
                        continue
                    pid = next(iter(ids))
                    for step, value in values.items():
                        if not isinstance(value, bool) and isinstance(value, (int, float)) and value >= 0:
                            grenade_rows.append((sid, pid, str(step), int(value)))
                con.executemany("INSERT INTO live_grenade_state(session_id,player_id,step_id,value) VALUES(?,?,?,?)", grenade_rows)

                # Final cutover is one transaction: publish immutable historical
                # snapshots, close the session, and remove resumable checkpoints.
                # Empty live tables on a repeated save mean it was already finalized;
                # do not replace the historical rows with an empty snapshot.
                if bool(data.get("kv_finished")):
                    grenade_count = int(con.execute(
                        "SELECT COUNT(*) FROM live_grenade_state WHERE session_id=?", (sid,)
                    ).fetchone()[0])
                    voice_count = int(con.execute(
                        "SELECT COUNT(*) FROM voice_checkpoints WHERE session_id=?", (sid,)
                    ).fetchone()[0])
                    grenade_source = f"runtime-kv:{gid}:{day}"
                    voice_source = f"runtime-kv:{gid}:{day}"
                    if grenade_count:
                        con.execute("DELETE FROM grenade_session_stats WHERE source_key=?", (grenade_source,))
                        con.execute("""INSERT INTO grenade_session_stats
                            (source_key,player_id,stat_key,value,match_date,guild_id)
                            SELECT ?,player_id,step_id,value,?,? FROM live_grenade_state
                            WHERE session_id=?""", (grenade_source, day, gid, sid))
                    if voice_count:
                        con.execute("DELETE FROM voice_stats WHERE source_key=?", (voice_source,))
                        con.execute("""INSERT INTO voice_stats
                            (source_key,discord_id,player_id,seconds,session_date)
                            SELECT ?,discord_id,player_id,seconds,? FROM voice_checkpoints
                            WHERE session_id=?""", (voice_source, day, sid))
                    con.execute("UPDATE runtime_sessions SET active=0 WHERE id=?", (sid,))
                    con.execute("DELETE FROM live_grenade_state WHERE session_id=?", (sid,))
                    con.execute("DELETE FROM voice_checkpoints WHERE session_id=?", (sid,))
            con.commit()
            return gid
        except Exception:
            con.rollback()
            raise


def upsert_guild_config(db_path: Path | str, guild_id: int, *,
                        log_channel_id: int | None = None,
                        voice_channel_ids: list[int] | None = None,
                        access_role_ids: list[int] | None = None) -> None:
    ensure_schema(db_path)
    gid = int(guild_id)
    with closing(connect(db_path)) as con:
        con.execute("BEGIN IMMEDIATE")
        try:
            con.execute("""INSERT INTO bot_guild_config(guild_id,log_channel_id) VALUES(?,?)
                ON CONFLICT(guild_id) DO UPDATE SET log_channel_id=excluded.log_channel_id,
                updated_at=CURRENT_TIMESTAMP""", (gid, log_channel_id))
            if voice_channel_ids is not None:
                con.execute("DELETE FROM guild_voice_channels WHERE guild_id=?", (gid,))
                con.executemany("INSERT INTO guild_voice_channels(guild_id,channel_id,position) VALUES(?,?,?)",
                                [(gid, int(cid), pos) for pos, cid in enumerate(dict.fromkeys(voice_channel_ids))])
            if access_role_ids is not None:
                con.execute("DELETE FROM guild_access_roles WHERE guild_id=?", (gid,))
                con.executemany("INSERT INTO guild_access_roles(guild_id,role_id) VALUES(?,?)",
                                [(gid, int(rid)) for rid in dict.fromkeys(access_role_ids)])
            con.commit()
        except Exception:
            con.rollback()
            raise


def get_guild_config(db_path: Path | str, guild_id: int) -> dict[str, Any] | None:
    ensure_schema(db_path)
    with closing(connect(db_path)) as con:
        row = con.execute("SELECT guild_id,log_channel_id FROM bot_guild_config WHERE guild_id=?",
                          (int(guild_id),)).fetchone()
        if row is None:
            return None
        return {"guild_id": int(row["guild_id"]), "log_channel_id": row["log_channel_id"],
                "voice_channel_ids": [int(r[0]) for r in con.execute(
                    "SELECT channel_id FROM guild_voice_channels WHERE guild_id=? ORDER BY position", (int(guild_id),))],
                "access_role_ids": [int(r[0]) for r in con.execute(
                    "SELECT role_id FROM guild_access_roles WHERE guild_id=? ORDER BY role_id", (int(guild_id),))]}


def upsert_roster_member(db_path: Path | str, guild_id: int, player_id: int, *,
                         squad_id: int | None = None, slot: int | None = None,
                         active: bool = True) -> None:
    ensure_schema(db_path)
    with closing(connect(db_path)) as con:
        con.execute("""INSERT INTO roster_memberships(guild_id,player_id,squad_id,slot,active,deactivated_at)
            VALUES(?,?,?,?,?,CASE WHEN ? THEN NULL ELSE CURRENT_TIMESTAMP END)
            ON CONFLICT(guild_id,player_id) DO UPDATE SET squad_id=excluded.squad_id,
            slot=excluded.slot,active=excluded.active,deactivated_at=excluded.deactivated_at,
            updated_at=CURRENT_TIMESTAMP""",
            (int(guild_id), int(player_id), squad_id, slot, int(active), int(active)))


def deactivate_roster_member(db_path: Path | str, guild_id: int, player_id: int) -> bool:
    ensure_schema(db_path)
    with closing(connect(db_path)) as con:
        cur = con.execute("""UPDATE roster_memberships SET active=0,
            deactivated_at=COALESCE(deactivated_at,CURRENT_TIMESTAMP),updated_at=CURRENT_TIMESTAMP
            WHERE guild_id=? AND player_id=? AND active=1""", (int(guild_id), int(player_id)))
        return cur.rowcount > 0


def mark_roster_removed(db_path: Path | str, guild_id: int, *,
                        player_id: int | None = None,
                        discord_id: str | int | None = None) -> int:
    """Persist an explicit full-roster removal while retaining identity/history."""
    ensure_schema(db_path)
    with closing(connect(db_path)) as con:
        if player_id is None and discord_id is not None:
            row = con.execute("SELECT player_id FROM discord_bindings WHERE discord_id=?",
                              (str(discord_id),)).fetchone()
            player_id = int(row[0]) if row else None
        if player_id is None:
            raise ValueError("player not found for roster removal")
        gid, pid = int(guild_id), int(player_id)
        con.execute("INSERT OR IGNORE INTO bot_guild_config(guild_id) VALUES(?)", (gid,))
        con.execute("""INSERT INTO roster_removals(guild_id,player_id) VALUES(?,?)
            ON CONFLICT(guild_id,player_id) DO UPDATE SET
            removed_at=CURRENT_TIMESTAMP,reason='explicit_remove'""", (gid, pid))
        con.execute("""UPDATE roster_memberships SET active=0,
            deactivated_at=COALESCE(deactivated_at,CURRENT_TIMESTAMP),updated_at=CURRENT_TIMESTAMP
            WHERE guild_id=? AND player_id=?""", (gid, pid))
        return pid


def restore_roster_member(db_path: Path | str, guild_id: int, *,
                          player_id: int | None = None,
                          discord_id: str | int | None = None) -> bool:
    """Clear an explicit-removal tombstone; the caller then activates membership."""
    ensure_schema(db_path)
    with closing(connect(db_path)) as con:
        if player_id is None and discord_id is not None:
            row = con.execute("SELECT player_id FROM discord_bindings WHERE discord_id=?",
                              (str(discord_id),)).fetchone()
            player_id = int(row[0]) if row else None
        if player_id is None:
            return False
        cur = con.execute("DELETE FROM roster_removals WHERE guild_id=? AND player_id=?",
                          (int(guild_id), int(player_id)))
        return cur.rowcount > 0


def removed_roster_identities(db_path: Path | str, guild_id: int) -> dict[str, set[str]]:
    """Return normalized nicknames and Discord IDs blocked from source imports."""
    ensure_schema(db_path)
    with closing(connect(db_path)) as con:
        rows = list(con.execute("""SELECT p.canonical_nick,d.discord_id
            FROM roster_removals x JOIN players p ON p.id=x.player_id
            LEFT JOIN discord_bindings d ON d.player_id=x.player_id
            WHERE x.guild_id=?""", (int(guild_id),)))
        return {
            "nicks": {_norm(str(r["canonical_nick"])) for r in rows},
            "discord_ids": {str(r["discord_id"]) for r in rows if r["discord_id"] is not None},
        }


def list_roster(db_path: Path | str, guild_id: int, *, include_inactive: bool = False) -> list[dict[str, Any]]:
    ensure_schema(db_path)
    where = "r.guild_id=?" + ("" if include_inactive else " AND r.active=1")
    with closing(connect(db_path)) as con:
        return [dict(row) for row in con.execute(f"""SELECT r.*,p.canonical_nick,
            d.discord_id,d.discord_name,d.discord_username FROM roster_memberships r
            JOIN players p ON p.id=r.player_id LEFT JOIN discord_bindings d ON d.player_id=p.id
            WHERE {where} ORDER BY r.active DESC,r.squad_id,r.slot,p.canonical_nick""", (int(guild_id),))]


def save_runtime_session(db_path: Path | str, guild_id: int, state: dict[str, Any]) -> int:
    """Atomically upsert session flags and its normalized attendance/maps/live checkpoints."""
    ensure_schema(db_path)
    day = parse_match_date(str(state["session_date"]))
    gid = int(guild_id)
    with closing(connect(db_path)) as con:
        con.execute("BEGIN IMMEDIATE")
        try:
            con.execute("INSERT OR IGNORE INTO bot_guild_config(guild_id) VALUES(?)", (gid,))
            con.execute("""INSERT INTO runtime_sessions
                (guild_id,session_date,grenade_date,active,finished,last_grenade_step,skipped_grenade_steps_json)
                VALUES(?,?,?,?,?,?,?) ON CONFLICT(guild_id,session_date) DO UPDATE SET
                grenade_date=excluded.grenade_date,active=excluded.active,finished=excluded.finished,
                last_grenade_step=excluded.last_grenade_step,
                skipped_grenade_steps_json=excluded.skipped_grenade_steps_json,updated_at=CURRENT_TIMESTAMP""",
                (gid, day, state.get("grenade_date"), int(bool(state.get("active"))),
                 int(bool(state.get("finished"))), state.get("last_grenade_step"),
                 _json_text(state.get("skipped_grenade_steps") or [])))
            sid = int(con.execute("SELECT id FROM runtime_sessions WHERE guild_id=? AND session_date=?",
                                  (gid, day)).fetchone()[0])
            if "attendance" in state:
                con.execute("DELETE FROM attendance WHERE session_id=?", (sid,))
                con.executemany("INSERT INTO attendance(session_id,player_id,came,in_voice) VALUES(?,?,?,?)",
                    [(sid, int(pid), int(bool(v.get("came"))), int(bool(v.get("in_voice"))))
                     for pid, v in (state.get("attendance") or {}).items()])
            if "maps" in state:
                con.execute("DELETE FROM session_maps WHERE session_id=?", (sid,))
                con.executemany("INSERT INTO session_maps(session_id,position,map_name) VALUES(?,?,?)",
                                [(sid, i, str(name)) for i, name in enumerate(state.get("maps") or [])])
            if "grenades" in state:
                con.execute("DELETE FROM live_grenade_state WHERE session_id=?", (sid,))
                con.executemany("INSERT INTO live_grenade_state(session_id,player_id,step_id,value) VALUES(?,?,?,?)",
                    [(sid, int(pid), str(step), int(value)) for pid, values in (state.get("grenades") or {}).items()
                     for step, value in values.items()])
            if "voice" in state:
                con.execute("DELETE FROM voice_checkpoints WHERE session_id=?", (sid,))
                con.executemany("""INSERT INTO voice_checkpoints
                    (session_id,discord_id,player_id,seconds,state_json) VALUES(?,?,?,?,?)""",
                    [(sid, str(did), value.get("player_id"), float(value.get("seconds", 0)),
                      _json_text(value.get("state") or {})) for did, value in (state.get("voice") or {}).items()])
            con.commit()
            return sid
        except Exception:
            con.rollback()
            raise


def load_runtime_session(db_path: Path | str, guild_id: int, session_date: str) -> dict[str, Any] | None:
    ensure_schema(db_path)
    day = parse_match_date(session_date)
    with closing(connect(db_path)) as con:
        row = con.execute("SELECT * FROM runtime_sessions WHERE guild_id=? AND session_date=?",
                          (int(guild_id), day)).fetchone()
        if row is None:
            return None
        sid = int(row["id"])
        return {"id": sid, "session_date": row["session_date"], "grenade_date": row["grenade_date"],
                "active": bool(row["active"]), "finished": bool(row["finished"]),
                "last_grenade_step": row["last_grenade_step"],
                "skipped_grenade_steps": json.loads(row["skipped_grenade_steps_json"]),
                "attendance": {str(r["player_id"]): {"came": bool(r["came"]), "in_voice": bool(r["in_voice"])}
                    for r in con.execute("SELECT * FROM attendance WHERE session_id=?", (sid,))},
                "maps": [str(r[0]) for r in con.execute(
                    "SELECT map_name FROM session_maps WHERE session_id=? ORDER BY position", (sid,))],
                "grenades": _load_nested(con, "live_grenade_state", sid),
                "voice": {str(r["discord_id"]): {"player_id": r["player_id"], "seconds": r["seconds"],
                    "state": json.loads(r["state_json"])} for r in con.execute(
                    "SELECT * FROM voice_checkpoints WHERE session_id=?", (sid,))}}


def _load_nested(con: sqlite3.Connection, table: str, session_id: int) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for row in con.execute(f"SELECT player_id,step_id,value FROM {table} WHERE session_id=?", (session_id,)):
        out.setdefault(str(row["player_id"]), {})[str(row["step_id"])] = int(row["value"])
    return out


def finalize_runtime_session(db_path: Path | str, guild_id: int, session_date: str) -> dict[str, int]:
    """Atomically publish final live checkpoints and close the runtime session.

    Repeating this operation is safe: historical rows use deterministic source keys,
    and an already finalized/cleared session is a no-op.
    """
    ensure_schema(db_path)
    gid, day = int(guild_id), parse_match_date(session_date)
    grenade_source = f"runtime-kv:{gid}:{day}"
    voice_source = f"runtime-kv:{gid}:{day}"
    with closing(connect(db_path)) as con:
        con.execute("BEGIN IMMEDIATE")
        try:
            session = con.execute(
                "SELECT id,finished FROM runtime_sessions WHERE guild_id=? AND session_date=?",
                (gid, day),
            ).fetchone()
            if session is None:
                raise ValueError(f"runtime session not found: {gid}/{day}")
            sid = int(session["id"])
            grenade_count = int(con.execute(
                "SELECT COUNT(*) FROM live_grenade_state WHERE session_id=?", (sid,)
            ).fetchone()[0])
            voice_count = int(con.execute(
                "SELECT COUNT(*) FROM voice_checkpoints WHERE session_id=?", (sid,)
            ).fetchone()[0])
            if not grenade_count and not voice_count and bool(session["finished"]):
                con.commit()
                return {"grenades": 0, "voice": 0}

            con.execute("DELETE FROM grenade_session_stats WHERE source_key=?", (grenade_source,))
            con.execute("""INSERT INTO grenade_session_stats
                (source_key,player_id,stat_key,value,match_date,guild_id)
                SELECT ?,player_id,step_id,value,?,? FROM live_grenade_state
                WHERE session_id=?""", (grenade_source, day, gid, sid))
            con.execute("DELETE FROM voice_stats WHERE source_key=?", (voice_source,))
            con.execute("""INSERT INTO voice_stats
                (source_key,discord_id,player_id,seconds,session_date)
                SELECT ?,discord_id,player_id,seconds,? FROM voice_checkpoints
                WHERE session_id=?""", (voice_source, day, sid))
            con.execute("""UPDATE runtime_sessions SET active=0,finished=1,
                updated_at=CURRENT_TIMESTAMP WHERE id=?""", (sid,))
            con.execute("DELETE FROM live_grenade_state WHERE session_id=?", (sid,))
            con.execute("DELETE FROM voice_checkpoints WHERE session_id=?", (sid,))
            con.commit()
            return {"grenades": grenade_count, "voice": voice_count}
        except Exception:
            con.rollback()
            raise


def reconciliation_status(db_path: Path | str, legacy_json: Path | str | None = None) -> dict[str, Any]:
    ensure_schema(db_path)
    with closing(connect(db_path)) as con:
        status = {"schema_version": int(con.execute("PRAGMA user_version").fetchone()[0]),
                  "integrity": str(con.execute("PRAGMA integrity_check").fetchone()[0]),
                  "foreign_key_violations": [tuple(r) for r in con.execute("PRAGMA foreign_key_check")],
                  "counts": _table_counts(con)}
        if legacy_json is not None and Path(legacy_json).exists():
            digest = hashlib.sha256(Path(legacy_json).read_bytes()).hexdigest()
            status["legacy_sha256"] = digest
            status["legacy_imported"] = con.execute(
                "SELECT 1 FROM legacy_imports WHERE source_path=? AND source_sha256=?",
                (str(Path(legacy_json).resolve()), digest)).fetchone() is not None
        return status


def export_snapshot(db_path: Path | str, guild_id: int, session_date: str | None = None) -> dict[str, Any]:
    """Compatibility snapshot for a later bot.py cutover; does not write players.json."""
    cfg = get_guild_config(db_path, guild_id) or {"guild_id": int(guild_id), "log_channel_id": None,
                                                   "voice_channel_ids": [], "access_role_ids": []}
    roster = list_roster(db_path, guild_id)
    result: dict[str, Any] = {"config": cfg, "players": {}}
    for row in roster:
        if row.get("discord_id") is not None:
            result["players"][str(row["discord_id"])] = {
                "game_nick": row["canonical_nick"], "discord_name": row.get("discord_name") or "",
                "discord_username": row.get("discord_username") or "", "squad": row.get("squad_id"),
                "slot": row.get("slot"), "came": False, "in_voice": False}
    if session_date:
        result["session"] = load_runtime_session(db_path, guild_id, session_date)
    return result


def migrate(db_path: Path | str, legacy_json: Path | str, *, backup: bool = True) -> dict[str, Any]:
    """One-time, transactional import. A completed cutover is never replayed automatically."""
    db_path, legacy_json = Path(db_path), Path(legacy_json)
    ensure_schema(db_path)
    raw = legacy_json.read_bytes() if legacy_json.exists() else b"{}"
    digest = hashlib.sha256(raw).hexdigest()
    source_path = str(legacy_json.resolve())
    data = json.loads(raw.decode("utf-8-sig")) if raw else {}
    with closing(connect(db_path)) as con:
        existing = con.execute("SELECT row_counts_json FROM legacy_imports WHERE source_path=?",
                               (source_path,)).fetchone()
        if existing is not None:
            counts = _table_counts(con)
            return {"backups": [], "skipped": True, "reason": "legacy source already imported",
                    "counts": counts, "imported_bindings": counts.get("discord_bindings", 0),
                    "voice_values_seen": 0, "grenade_values_seen": 0,
                    "scan_rows_linked_now": 0, "safe_example_alias_added": False}
    backups = backup_cutover_files(db_path, legacy_json) if backup else []
    cfg = data.get("config") or {}
    guild_id = int(cfg.get("guild_id") or 0)
    with closing(connect(db_path)) as con:
        con.execute("BEGIN IMMEDIATE")
        try:
            con.execute("INSERT OR IGNORE INTO bot_guild_config(guild_id,log_channel_id) VALUES(?,?)",
                        (guild_id, cfg.get("log_channel_id")))
            con.execute("DELETE FROM guild_voice_channels WHERE guild_id=?", (guild_id,))
            con.executemany("INSERT INTO guild_voice_channels(guild_id,channel_id,position) VALUES(?,?,?)",
                            [(guild_id, int(v), i) for i, v in enumerate(dict.fromkeys(cfg.get("voice_channel_ids") or []))])
            con.execute("DELETE FROM guild_access_roles WHERE guild_id=?", (guild_id,))
            con.executemany("INSERT INTO guild_access_roles(guild_id,role_id) VALUES(?,?)",
                            [(guild_id, int(v)) for v in dict.fromkeys(cfg.get("access_role_ids") or [])])
            player_ids: dict[str, int] = {}
            for discord_id, record in (data.get("players") or {}).items():
                nick = str(record.get("game_nick") or "").strip()
                if not nick:
                    continue
                pid = _player_id(con, nick)
                player_ids[str(discord_id)] = pid
                con.execute("""INSERT INTO discord_bindings(discord_id,player_id,discord_name,discord_username)
                    VALUES(?,?,?,?) ON CONFLICT(discord_id) DO UPDATE SET player_id=excluded.player_id,
                    discord_name=excluded.discord_name,discord_username=excluded.discord_username,
                    updated_at=CURRENT_TIMESTAMP""", (str(discord_id), pid,
                    str(record.get("discord_name") or ""), str(record.get("discord_username") or "")))
                con.execute("INSERT OR IGNORE INTO player_guilds(player_id,guild_id) VALUES(?,?)", (pid, guild_id))
                con.execute("""INSERT INTO roster_memberships(guild_id,player_id,squad_id,slot,active)
                    VALUES(?,?,?,?,1) ON CONFLICT(guild_id,player_id) DO NOTHING""",
                    (guild_id, pid, record.get("squad"), record.get("slot")))
            con.executemany("""INSERT INTO squad_names(guild_id,squad_id,name) VALUES(?,?,?)
                ON CONFLICT(guild_id,squad_id) DO UPDATE SET name=excluded.name,updated_at=CURRENT_TIMESTAMP""",
                [(guild_id, int(k), str(v)) for k, v in (data.get("squad_names") or {}).items()])
            session_day = str(data.get("session_date") or data.get("grenade_date") or "").strip()
            sid = None
            if session_day:
                session_day = parse_match_date(session_day)
                con.execute("""INSERT INTO runtime_sessions
                    (guild_id,session_date,grenade_date,active,finished,last_grenade_step,skipped_grenade_steps_json)
                    VALUES(?,?,?,?,?,?,?) ON CONFLICT(guild_id,session_date) DO NOTHING""",
                    (guild_id, session_day, data.get("grenade_date"), int(bool(data.get("kv_session_active")) and not bool(data.get("kv_finished"))),
                     int(bool(data.get("kv_finished"))), data.get("last_grenade_step"),
                     _json_text(data.get("skipped_grenade_steps") or [])))
                sid = int(con.execute("SELECT id FROM runtime_sessions WHERE guild_id=? AND session_date=?",
                                      (guild_id, session_day)).fetchone()[0])
                for did, record in (data.get("players") or {}).items():
                    pid = player_ids.get(str(did))
                    if pid is not None:
                        con.execute("INSERT OR IGNORE INTO attendance(session_id,player_id,came,in_voice) VALUES(?,?,?,?)",
                                    (sid, pid, int(bool(record.get("came"))), int(bool(record.get("in_voice")))))
                maps = data.get("kv_maps") or {}
                if not maps.get("date") or str(maps.get("date")) == session_day:
                    con.executemany("INSERT OR IGNORE INTO session_maps(session_id,position,map_name) VALUES(?,?,?)",
                                    [(sid, i, str(v)) for i, v in enumerate(maps.get("maps") or [])])
                for did in data.get("absent_dm_sent") or []:
                    con.execute("INSERT OR IGNORE INTO absent_dm_deliveries(session_id,discord_id,player_id) VALUES(?,?,?)",
                                (sid, str(did), player_ids.get(str(did))))
                for did, seconds in (data.get("voice_speak_seconds") or {}).items():
                    if not isinstance(seconds, bool) and isinstance(seconds, (int, float)) and seconds >= 0:
                        con.execute("INSERT OR IGNORE INTO voice_checkpoints(session_id,discord_id,player_id,seconds) VALUES(?,?,?,?)",
                                    (sid, str(did), player_ids.get(str(did)), float(seconds)))
                        con.execute("""INSERT INTO voice_stats(source_key,discord_id,player_id,seconds,session_date)
                            VALUES(?,?,?,?,?) ON CONFLICT(source_key,discord_id) DO NOTHING""",
                            (f"legacy-kv:{session_day}", str(did), player_ids.get(str(did)), float(seconds), session_day))
                for nick, values in (data.get("grenade_history") or {}).items():
                    if not isinstance(values, dict):
                        continue
                    ids = _identity_ids(con, str(nick))
                    pid = next(iter(ids)) if len(ids) == 1 else _player_id(con, str(nick))
                    for step, value in values.items():
                        if not isinstance(value, bool) and isinstance(value, (int, float)) and value >= 0:
                            con.execute("INSERT OR IGNORE INTO live_grenade_state(session_id,player_id,step_id,value) VALUES(?,?,?,?)",
                                        (sid, pid, str(step), int(value)))
                            con.execute("""INSERT INTO grenade_stats(player_id,stat_key,value) VALUES(?,?,?)
                                ON CONFLICT(player_id,stat_key) DO UPDATE SET value=MAX(value,excluded.value)""",
                                (pid, str(step), int(value)))
            for kind, mid in (data.get("message_ids") or {}).items():
                con.execute("INSERT OR IGNORE INTO discord_message_refs(guild_id,ref_kind,message_id) VALUES(?,?,?)",
                            (guild_id, str(kind), mid))
            con.execute("INSERT OR IGNORE INTO bot_kv_state(guild_id,state_key,value_json) VALUES(?,?,?)",
                        (guild_id, "legacy_import_sha256", _json_text(digest)))
            counts = _table_counts(con)
            con.execute("""INSERT INTO legacy_imports
                (source_path,source_sha256,schema_version,guild_id,row_counts_json) VALUES(?,?,?,?,?)""",
                (source_path, digest, SCHEMA_VERSION, guild_id, _json_text(counts)))
            con.execute("INSERT OR REPLACE INTO schema_meta(key,value) VALUES('legacy_cutover_complete',?)", (digest,))
            con.commit()
        except Exception:
            con.rollback()
            raise
    linked = link_scan_rows(db_path)
    with closing(connect(db_path)) as con:
        counts = _table_counts(con)
    return {"backups": backups, "skipped": False, "imported_bindings": len(player_ids),
            "voice_values_seen": len(data.get("voice_speak_seconds") or {}),
            "voice_source_key": f"legacy-kv:{session_day}" if session_day else "legacy-kv:undated-current",
            "grenade_values_seen": sum(len(v) for v in (data.get("grenade_history") or {}).values() if isinstance(v, dict)),
            "scan_rows_linked_now": linked, "safe_example_alias_added": False, "counts": counts}


def parse_kv_daily_report(path: Path | str) -> dict[str, Any]:
    """Parse a generated scans/itogi report without inventing missing values.

    Stage columns are deltas (not cumulative snapshots): the generator subtracts
    consecutive gre-thr lifetime snapshots and writes their sum as ИТОГ.
    """
    source = Path(path)
    raw = source.read_bytes()
    text = raw.decode("utf-8-sig")
    lines = text.splitlines()
    if not lines:
        raise ValueError(f"empty KV report: {source}")
    date_match = re.match(r"^(\d{4}-\d{2}-\d{2})\b", lines[0].strip())
    if not date_match:
        raise ValueError(f"missing report date: {source}")
    match_date = parse_match_date(date_match.group(1))
    header_index = next((i for i, line in enumerate(lines) if "ИТОГ" in line), None)
    if header_index is None:
        raise ValueError(f"missing ИТОГ header: {source}")
    header = lines[header_index]
    stage_count = 4 if re.search(r"\bIV\b", header) else 3
    has_voice = "ВРЕМЯ" in header
    current_squad: str | None = None
    rows: list[dict[str, Any]] = []
    cell = r"(?:—|-|\d+)"
    row_re = re.compile(
        rf"^(?P<name>.+?)\s+(?P<values>{cell}(?:\s+{cell}){{{stage_count}}})"
        + (r"\s+(?P<voice>\d+:\d{2})" if has_voice else "") + r"\s*$"
    )
    for line_no, line in enumerate(lines[header_index + 1:], start=header_index + 2):
        stripped = line.strip()
        if not stripped or set(stripped) <= {"-"}:
            continue
        if stripped.endswith(":"):
            current_squad = stripped[:-1]
            continue
        match = row_re.match(line)
        if not match:
            raise ValueError(f"unrecognized player row {source}:{line_no}: {line!r}")
        name_field = match.group("name").strip()
        username = None
        name_match = re.match(r"^(.*)\(([^()]*)\)$", name_field)
        if name_match:
            name_field = name_match.group(1).strip()
            username = name_match.group(2).strip() or None
        tokens = match.group("values").split()
        values = [None if token in ("—", "-") else int(token) for token in tokens]
        stages, total = values[:-1], values[-1]
        known = [value for value in stages if value is not None]
        if known and total != sum(known):
            raise ValueError(f"ИТОГ does not equal stage deltas at {source}:{line_no}")
        if not known and total not in (None, 0):
            raise ValueError(f"ИТОГ exists without stage values at {source}:{line_no}")
        voice_seconds = None
        if has_voice:
            minutes, seconds = map(int, match.group("voice").split(":"))
            if seconds >= 60:
                raise ValueError(f"invalid voice duration at {source}:{line_no}")
            voice_seconds = minutes * 60 + seconds
        rows.append({"row_no": len(rows) + 1, "raw_nick": name_field,
                     "discord_username": username, "squad_label": current_squad,
                     "stages": stages, "total_grenades": total,
                     "voice_seconds": voice_seconds})
    if not rows:
        raise ValueError(f"report has no player rows: {source}")
    return {"match_date": match_date, "stage_count": stage_count,
            "generated_at_text": lines[0].strip(), "source_path": str(source.resolve()),
            "source_sha256": hashlib.sha256(raw).hexdigest(),
            "source_mtime_ns": source.stat().st_mtime_ns, "rows": rows}


def import_kv_daily_report(db_path: Path | str, report_path: Path | str,
                           guild_id: int | None = None) -> dict[str, Any]:
    """Transactionally insert or replace one source report (corrections replace rows)."""
    parsed = parse_kv_daily_report(report_path)
    ensure_schema(db_path)
    scope_guild = int(guild_id or 0)
    with closing(connect(db_path)) as con:
        con.execute("BEGIN IMMEDIATE")
        try:
            old = con.execute("SELECT id,source_sha256 FROM kv_daily_reports WHERE source_path=? AND guild_id=?",
                              (parsed["source_path"], scope_guild)).fetchone()
            if old and old["source_sha256"] == parsed["source_sha256"]:
                con.commit()
                return {"report_id": int(old["id"]), "status": "unchanged", "rows": len(parsed["rows"])}
            if old:
                report_id = int(old["id"])
                con.execute("DELETE FROM kv_daily_players WHERE report_id=?", (report_id,))
                con.execute("""UPDATE kv_daily_reports SET match_date=?,source_sha256=?,source_mtime_ns=?,
                    stage_count=?,generated_at_text=?,imported_at=CURRENT_TIMESTAMP WHERE id=?""",
                    (parsed["match_date"], parsed["source_sha256"], parsed["source_mtime_ns"],
                     parsed["stage_count"], parsed["generated_at_text"], report_id))
                status = "corrected"
            else:
                report_id = int(con.execute("""INSERT INTO kv_daily_reports
                    (match_date,guild_id,source_path,source_sha256,source_mtime_ns,stage_count,generated_at_text)
                    VALUES(?,?,?,?,?,?,?)""", (parsed["match_date"], scope_guild, parsed["source_path"],
                    parsed["source_sha256"], parsed["source_mtime_ns"], parsed["stage_count"],
                    parsed["generated_at_text"])).lastrowid)
                status = "created"
            unresolved = 0
            for row in parsed["rows"]:
                match = match_player_in_connection(con, row["raw_nick"], guild_id)
                player_id = match.player_id if match.method == "exact" else None
                if player_id is None and row["discord_username"]:
                    binding = con.execute("SELECT player_id FROM discord_bindings WHERE discord_username=? COLLATE NOCASE",
                                          (row["discord_username"],)).fetchall()
                    if len(binding) == 1:
                        player_id = int(binding[0][0])
                unresolved += player_id is None
                con.execute("""INSERT INTO kv_daily_players
                    (report_id,row_no,player_id,raw_nick,discord_username,squad_label,voice_seconds,total_grenades)
                    VALUES(?,?,?,?,?,?,?,?)""", (report_id, row["row_no"], player_id, row["raw_nick"],
                    row["discord_username"], row["squad_label"], row["voice_seconds"], row["total_grenades"]))
                con.executemany("INSERT INTO kv_daily_grenade_stages(report_id,row_no,stage_no,grenades) VALUES(?,?,?,?)",
                                [(report_id, row["row_no"], i, value) for i, value in enumerate(row["stages"], 1)])
            con.commit()
            return {"report_id": report_id, "status": status, "rows": len(parsed["rows"]),
                    "unresolved": unresolved, "sha256": parsed["source_sha256"]}
        except Exception:
            con.rollback()
            raise


def import_kv_daily_reports(db_path: Path | str, reports_dir: Path | str,
                            guild_id: int | None = None, backup: bool = True) -> list[dict[str, Any]]:
    """Explicit offline entry point. Backup occurs before the first DB mutation."""
    db = Path(db_path)
    paths = sorted(Path(reports_dir).glob("*_итог.txt"))
    parsed = [parse_kv_daily_report(path) for path in paths]  # fail before backup/write
    if backup and db.exists():
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        shutil.copy2(db, db.with_name(f"{db.name}.{stamp}.kv-import.bak"))
    results = []
    for item, path in zip(parsed, paths):
        del item  # parsing above is the all-files validation gate
        results.append(import_kv_daily_report(db, path, guild_id))
    return results


def collect_stats_samples(
    db_path: Path | str,
    completed_logs_dir: Path | str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    guild_id: int | None = None,
) -> dict[str, Any]:
    """Compatibility wrapper; completed txt reports are derived artifacts only."""
    del completed_logs_dir
    return collect_kv_daily_stats(
        db_path, date_from=date_from, date_to=date_to, guild_id=guild_id
    )


def list_kv_daily_dates(db_path, guild_id=None):
    ensure_schema(db_path)
    sql = "SELECT DISTINCT match_date FROM kv_daily_reports"
    params: list[Any] = []
    if guild_id is not None:
        sql += " WHERE guild_id=?"
        params.append(int(guild_id))
    sql += " ORDER BY match_date DESC"
    with closing(connect(db_path)) as con:
        return [str(row[0]) for row in con.execute(sql, params)]


def save_kv_daily_report(
    db_path,
    match_date,
    guild_id,
    rows,
    stage_count,
    source_key=None,
    generated_at_text=None,
):
    day = parse_match_date(match_date)
    stage_count = int(stage_count)
    if stage_count not in (3, 4):
        raise ValueError("stage_count must be 3 or 4")
    normalized_rows = []
    for row in rows:
        nick = str(row.get("raw_nick") or row.get("nick") or "").strip()
        if not nick:
            continue
        stages = list(row.get("stages") or [])
        if len(stages) != stage_count:
            raise ValueError(f"{nick}: expected {stage_count} stages, got {len(stages)}")
        clean_stages = [None if value is None else max(0, int(value)) for value in stages]
        known = [value for value in clean_stages if value is not None]
        total = row.get("total_grenades")
        clean_total = None if total is None else max(0, int(total))
        if known and clean_total != sum(known):
            raise ValueError(f"{nick}: total_grenades does not equal stage deltas")
        if not known and clean_total not in (None, 0):
            raise ValueError(f"{nick}: total_grenades exists without stage deltas")
        voice = row.get("voice_seconds")
        normalized_rows.append(
            {
                "player_id": row.get("player_id"),
                "raw_nick": nick,
                "discord_username": row.get("discord_username"),
                "squad_label": row.get("squad_label"),
                "stages": clean_stages,
                "total_grenades": clean_total,
                "voice_seconds": None if voice is None else max(0, int(round(float(voice)))),
            }
        )
    ensure_schema(db_path)
    payload = json.dumps(normalized_rows, ensure_ascii=False, sort_keys=True).encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()
    source = source_key or f"runtime:{int(guild_id)}:{day}"
    with closing(connect(db_path)) as con:
        con.execute("BEGIN IMMEDIATE")
        try:
            old = con.execute(
                "SELECT id FROM kv_daily_reports WHERE match_date=? AND guild_id=?",
                (day, int(guild_id)),
            ).fetchone()
            if old:
                con.execute("DELETE FROM kv_daily_reports WHERE id=?", (int(old[0]),))
            report_id = int(
                con.execute(
                    """INSERT INTO kv_daily_reports
                    (match_date,guild_id,source_path,source_sha256,format_version,stage_count,generated_at_text)
                    VALUES(?,?,?,?,1,?,?)""",
                    (day, int(guild_id), source, digest, stage_count, generated_at_text),
                ).lastrowid
            )
            stage_rows = 0
            for row_no, row in enumerate(normalized_rows, 1):
                player_id = row["player_id"]
                if player_id is None:
                    match = match_player_in_connection(con, row["raw_nick"], int(guild_id))
                    player_id = match.player_id if match.method == "exact" else None
                con.execute(
                    """INSERT INTO kv_daily_players
                    (report_id,row_no,player_id,raw_nick,discord_username,squad_label,voice_seconds,total_grenades)
                    VALUES(?,?,?,?,?,?,?,?)""",
                    (
                        report_id, row_no, player_id, row["raw_nick"],
                        row["discord_username"], row["squad_label"],
                        row["voice_seconds"], row["total_grenades"],
                    ),
                )
                for stage_no, value in enumerate(row["stages"], 1):
                    con.execute(
                        """INSERT INTO kv_daily_grenade_stages
                        (report_id,row_no,stage_no,grenades) VALUES(?,?,?,?)""",
                        (report_id, row_no, stage_no, value),
                    )
                    stage_rows += 1
            con.commit()
        except Exception:
            con.rollback()
            raise
    return {"report_id": report_id, "players": len(normalized_rows), "stages": stage_rows}


def collect_kv_daily_stats(db_path, date_from=None, date_to=None, guild_id=None):
    ensure_schema(db_path)
    start = parse_match_date(date_from) if date_from else None
    end = parse_match_date(date_to) if date_to else None
    if start and not end:
        end = start
    if end and not start:
        start = end
    if start and end and start > end:
        raise ValueError("Дата «с» не может быть позже даты «по»")
    samples: dict[int, dict[str, list[float]]] = {}
    completed_dates: set[str] = set()

    def add(player_id, metric, value):
        if player_id is None or value is None:
            return
        pair = samples.setdefault(int(player_id), {}).setdefault(metric, [0.0, 0.0])
        pair[0] += float(value)
        pair[1] += 1.0

    with closing(connect(db_path)) as con:
        bindings = {
            str(row["discord_id"]): int(row["player_id"])
            for row in con.execute("SELECT discord_id,player_id FROM discord_bindings")
        }
        scan_filters: list[str] = []
        scan_params: list[Any] = []
        if start:
            scan_filters.append("s.match_date>=?")
            scan_params.append(start)
        if end:
            scan_filters.append("s.match_date<=?")
            scan_params.append(end)
        if guild_id is not None:
            scan_filters.append("s.guild_id=?")
            scan_params.append(int(guild_id))
        scan_where = " WHERE " + " AND ".join(scan_filters) if scan_filters else ""
        scan_sql = """SELECT sp.player_id,sp.nick,sp.kills,sp.deaths,sp.assists,sp.score
                      FROM scan_players sp JOIN scans s ON s.id=sp.scan_id""" + scan_where
        for row in con.execute(scan_sql, scan_params):
            player_id = row["player_id"]
            if player_id is None:
                player_id = resolve_player_id_in_connection(con, str(row["nick"]), guild_id)
            add(player_id, "kills", row["kills"])
            add(player_id, "deaths", row["deaths"])
            add(player_id, "assists", row["assists"])
            add(player_id, "score", row["score"])

        report_filters: list[str] = []
        report_params: list[Any] = []
        if start:
            report_filters.append("r.match_date>=?")
            report_params.append(start)
        if end:
            report_filters.append("r.match_date<=?")
            report_params.append(end)
        if guild_id is not None:
            report_filters.append("r.guild_id=?")
            report_params.append(int(guild_id))
        report_where = " WHERE " + " AND ".join(report_filters) if report_filters else ""
        report_sql = """SELECT r.match_date,p.player_id,p.total_grenades,p.voice_seconds
                        FROM kv_daily_reports r
                        JOIN kv_daily_players p ON p.report_id=r.id""" + report_where
        for row in con.execute(report_sql, report_params):
            completed_dates.add(str(row["match_date"]))
            add(row["player_id"], "grenades", row["total_grenades"])
            add(row["player_id"], "voice_seconds", row["voice_seconds"])
    return {
        "samples": samples,
        "bindings": bindings,
        "completed_dates": completed_dates,
        "date_from": start,
        "date_to": end,
    }
