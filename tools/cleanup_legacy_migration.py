#!/usr/bin/env python3
"""Remove completed players.json migration code and its obsolete tests."""
from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STORE = ROOT / "player_store.py"
TEST = ROOT / "tools" / "test_player_store.py"
REMOVED_FUNCTIONS = {
    "backup_cutover_files",
    "legacy_cutover_complete",
    "ensure_legacy_cutover",
    "migrate",
}


def remove_functions(source: str) -> str:
    tree = ast.parse(source)
    spans = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in REMOVED_FUNCTIONS:
            spans.append((node.lineno - 1, node.end_lineno))
    if {node.name for node in tree.body if isinstance(node, ast.FunctionDef)} & REMOVED_FUNCTIONS != REMOVED_FUNCTIONS:
        raise RuntimeError("player_store.py does not contain the expected migration functions")
    lines = source.splitlines(keepends=True)
    for start, end in sorted(spans, reverse=True):
        del lines[start:end]
        while start < len(lines) and lines[start].strip() == "" and start > 0 and lines[start - 1].strip() == "":
            del lines[start]
    return "".join(lines)


def replace_once(source: str, old: str, new: str, label: str) -> str:
    count = source.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected one match, found {count}")
    return source.replace(old, new, 1)


def clean_store(source: str) -> str:
    source = replace_once(
        source,
        '"""Unified SQLite player identity store and idempotent legacy migration."""',
        '"""Unified SQLite player identity, roster, scan, and statistics store."""',
        "module docstring",
    )
    source = remove_functions(source)
    source, count = re.subn(
        r"\nCREATE TABLE IF NOT EXISTS legacy_imports\(.*?\n\);",
        "",
        source,
        count=1,
        flags=re.S,
    )
    if count != 1:
        raise RuntimeError(f"legacy_imports schema: expected one match, found {count}")

    tree = ast.parse(source)
    target = next(
        (node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "reconciliation_status"),
        None,
    )
    if target is None:
        raise RuntimeError("reconciliation_status not found")
    replacement = '''def reconciliation_status(db_path: Path | str) -> dict[str, Any]:
    """Return SQLite integrity and row counts for operational diagnostics."""
    ensure_schema(db_path)
    with closing(connect(db_path)) as con:
        return {
            "schema_version": int(con.execute("PRAGMA user_version").fetchone()[0]),
            "integrity": str(con.execute("PRAGMA integrity_check").fetchone()[0]),
            "foreign_key_violations": [tuple(r) for r in con.execute("PRAGMA foreign_key_check")],
            "counts": _table_counts(con),
        }
'''
    lines = source.splitlines(keepends=True)
    lines[target.lineno - 1:target.end_lineno] = [replacement]
    source = "".join(lines)
    source = source.replace(
        "# v10: DB-authoritative bot state. JSON is only an import/export format.",
        "# DB-authoritative bot state.",
    ).replace(
        "# v11: an explicit /remove is durable and cannot be undone by Excel refresh.",
        "# An explicit /remove is durable until explicitly restored.",
    ).replace(
        "# A Discord binding is the stable identity.  In particular, an\n                    # Excel nickname edit must rename that player instead of creating\n                    # a second identity and moving the binding away from its history.",
        "# A Discord binding is the stable identity. A roster nickname edit\n                    # renames that player instead of creating a second identity.",
    )
    ast.parse(source)
    forbidden = tuple(REMOVED_FUNCTIONS) + ("legacy_imports", "legacy_cutover_complete", "pre-sqlite.bak")
    found = [item for item in forbidden if item in source]
    if found:
        raise RuntimeError("legacy migration markers remain: " + ", ".join(found))
    return source


def clean_test(source: str) -> str:
    start = source.index("        legacy = root / 'players.json'")
    end = source.index("        # Semantic scan idempotency:", start)
    modern_setup = '''        # Upgrade a historical scan schema, then link it to the modern identity store.
        with closing(sqlite3.connect(db)) as con:
            con.executescript("""
            CREATE TABLE scans(id INTEGER PRIMARY KEY, stage INTEGER, map TEXT, guild_id INTEGER, channel_id INTEGER, user_id INTEGER, scanned_at TEXT, source_filename TEXT);
            CREATE TABLE scan_players(id INTEGER PRIMARY KEY, scan_id INTEGER, place INTEGER, nick TEXT, kills INTEGER, deaths INTEGER, assists INTEGER, score INTEGER);
            INSERT INTO scans VALUES(1,1,'map',1,1,1,'now','x.png');
            INSERT INTO scan_players VALUES(1,1,1,'lovecult',1,2,3,4);
            """)
        player_store.ensure_schema(db)
        binding = player_store.upsert_binding(db, '123', 'iovecuit', 'D', 'u')
        player_store.add_alias(db, 'iovecuit', 'lovecult')
        assert player_store.link_scan_rows(db) == 1
        player_store.sync_grenade_history(
            db, {'lovecult': {'20:00': 10}, 'iovecuit': {'20:25': 12}}
        )
        player_store.sync_voice_snapshot(
            db, {'123': 12.5, '999': 3.25}, 'test-kv:2026-08-16', '2026-08-16'
        )
        with closing(player_store.connect(db)) as con:
            alias = con.execute("SELECT player_id FROM player_aliases WHERE alias='lovecult'").fetchone()[0]
            scan = con.execute("SELECT player_id FROM scan_players WHERE nick='lovecult'").fetchone()[0]
            grenade_ids = {r[0] for r in con.execute("SELECT DISTINCT player_id FROM grenade_stats")}
            assert binding == alias == scan
            assert grenade_ids == {binding}
            voice = con.execute("SELECT player_id,seconds FROM voice_stats ORDER BY discord_id").fetchall()
            assert len(voice) == 2 and voice[0][0] == binding and voice[1][0] is None
            assert [row[1] for row in voice] == [12.5, 3.25]
'''
    source = source[:start] + modern_setup + source[end:]

    start = source.index("        # v10 full JSON cutover:")
    end = source.index("    after = working_db.read_bytes()", start)
    source = source[:start] + source[end:]

    old = '''        stage2_json = root / 'players.json'
        stage2_json.write_text(json.dumps({
            'config': {'guild_id': 88},
            'players': {'800': {'game_nick': 'RuntimePlayer'}},
        }), encoding='utf-8')
        player_store.ensure_legacy_cutover(stage2_db, stage2_json)
        json_before = stage2_json.read_bytes()
        snapshot = player_store.load_bot_snapshot(stage2_db, 88)
'''
    new = '''        player_store.ensure_schema(stage2_db)
        player_store.upsert_guild_config(stage2_db, 88)
        stage2_player = player_store.upsert_binding(
            stage2_db, '800', 'RuntimePlayer', guild_id=88
        )
        player_store.upsert_roster_member(
            stage2_db, 88, stage2_player, squad_id=None, slot=None
        )
        snapshot = player_store.load_bot_snapshot(stage2_db, 88)
'''
    source = replace_once(source, old, new, "stage2 setup")
    source = replace_once(
        source,
        "        assert stage2_json.read_bytes() == json_before\n",
        "",
        "stage2 JSON immutability assertion",
    )
    old = '''        stage2_json.write_text('{broken after marker', encoding='utf-8')
        cutover_status = player_store.ensure_legacy_cutover(stage2_db, stage2_json)
        assert cutover_status['skipped']
        assert set(cutover_status['counts']) == set(player_store.STATUS_COUNT_TABLES)
        assert player_store.load_bot_snapshot(stage2_db, 88)['players'] == {}
'''
    new = '''        stage2_status = player_store.reconciliation_status(stage2_db)
        assert stage2_status['integrity'] == 'ok'
        assert set(stage2_status['counts']) == set(player_store.STATUS_COUNT_TABLES)
        assert player_store.load_bot_snapshot(stage2_db, 88)['players'] == {}
'''
    source = replace_once(source, old, new, "stage2 cutover assertion")

    source = source.replace("        startup_json = root / 'players.json'\n        startup_json.write_text('{}', encoding='utf-8')\n", "")
    source = replace_once(
        source,
        "        original_values = (bot.PLAYER_DB_PATH, bot.LEGACY_JSON_PATH, bot.DISCORD_TOKEN,\n                           bot.CLIENT_SECRET, bot.bot.run, bot.bot.close)\n",
        "        original_values = (bot.PLAYER_DB_PATH, bot.DISCORD_TOKEN,\n                           bot.CLIENT_SECRET, bot.bot.run, bot.bot.close)\n",
        "startup original values",
    )
    source = source.replace("            bot.LEGACY_JSON_PATH = startup_json\n", "")
    source = replace_once(
        source,
        "            (bot.PLAYER_DB_PATH, bot.LEGACY_JSON_PATH, bot.DISCORD_TOKEN,\n             bot.CLIENT_SECRET, bot.bot.run, bot.bot.close) = original_values\n",
        "            (bot.PLAYER_DB_PATH, bot.DISCORD_TOKEN,\n             bot.CLIENT_SECRET, bot.bot.run, bot.bot.close) = original_values\n",
        "startup restore values",
    )
    source = replace_once(
        source,
        "        startup_status = player_store.ensure_legacy_cutover(startup_db, startup_json)\n",
        "        startup_status = player_store.reconciliation_status(startup_db)\n",
        "startup status",
    )
    source = source.replace("import json\n", "")
    ast.parse(source)
    forbidden = ("migrate(", "ensure_legacy_cutover", "LEGACY_JSON_PATH", "legacy_imports", "full-cutover")
    found = [item for item in forbidden if item in source]
    if found:
        raise RuntimeError("obsolete migration test markers remain: " + ", ".join(found))
    return source


def main() -> None:
    store = clean_store(STORE.read_text(encoding="utf-8"))
    test = clean_test(TEST.read_text(encoding="utf-8"))
    STORE.write_text(store, encoding="utf-8")
    TEST.write_text(test, encoding="utf-8")
    print(f"Cleaned player_store.py: {store.count(chr(10)) + 1} lines")
    print(f"Updated test_player_store.py: {test.count(chr(10)) + 1} lines")


if __name__ == "__main__":
    main()
