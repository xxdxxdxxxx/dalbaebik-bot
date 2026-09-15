"""Final source cleanup after the one-time JSON-to-SQLite cutover."""
from __future__ import annotations

import ast


def finalize_source(text: str) -> str:
    """Make SQLite authoritative and attach extensions after runtime construction."""
    text = text.replace('LEGACY_JSON_PATH = ROOT / "players.json"\n', '')
    text = text.replace(
        '    migration = player_store.ensure_legacy_cutover(PLAYER_DB_PATH, LEGACY_JSON_PATH)\n',
        '    player_store.ensure_schema(PLAYER_DB_PATH)\n'
        '    migration = player_store.reconciliation_status(PLAYER_DB_PATH)\n',
    )
    text = text.replace(
        '    player_store.ensure_legacy_cutover(PLAYER_DB_PATH, LEGACY_JSON_PATH)\n',
        '    player_store.ensure_schema(PLAYER_DB_PATH)\n',
    )
    text = text.replace(
        '"""Load the compatibility snapshot from SQLite after one-time legacy import."""',
        '"""Load the runtime snapshot from authoritative SQLite storage."""',
    )
    text = text.replace(
        '"""Transactionally save runtime state to SQLite; players.json is never written."""',
        '"""Transactionally save runtime state to SQLite."""',
    )
    text = text.replace(
        '# Реальный startup-path: схема и legacy import выполняются до подключения к Discord.',
        '# Схема SQLite проверяется до подключения к Discord.',
    )
    entry = 'if __name__ == "__main__":\n    main()\n'
    installed_entry = (
        'if __name__ == "__main__":\n'
        '    import tools\n'
        '    tools.install_runtime(sys.modules.get("__main__"))\n'
        '    main()\n'
    )
    if entry not in text:
        raise RuntimeError("runtime entry point was not found")
    text = text.replace(entry, installed_entry, 1)
    ast.parse(text)
    return text
