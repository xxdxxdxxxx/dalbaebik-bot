#!/usr/bin/env python3
"""Replace the temporary Git-history loader with the verified static bot source."""
from __future__ import annotations

import ast
import py_compile
import subprocess
import tempfile
from pathlib import Path

from runtime_source_cleanup import clean_source

ROOT = Path(__file__).resolve().parent.parent
BOT_PATH = ROOT / "bot.py"
SOURCE_COMMIT = "3f60b0581ce5e56d75f93cabdb7ccf5c7b14f960"
FORBIDDEN = (
    "from openpyxl import",
    "from sheet_watcher import",
    "ensure_legacy_cutover(PLAYER_DB_PATH",
    "runtime_source_cleanup",
    "runtime_source_finalize",
    'name="reset_session"',
    'name="voice_scan_start"',
    'name="voice_scan_stop"',
    'name="stats"',
    'name="stats_dates"',
    'name="scan_dates"',
    'name="scan_view"',
    'name="fix_slash"',
)


def main() -> None:
    original = subprocess.check_output(
        ["git", "show", f"{SOURCE_COMMIT}:bot.py"],
        cwd=ROOT,
        text=True,
        encoding="utf-8",
    )
    source = clean_source(original)
    ast.parse(source)
    found = [marker for marker in FORBIDDEN if marker in source]
    if found:
        raise RuntimeError("Static source still contains removed code: " + ", ".join(found))

    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".py", dir=ROOT, delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(source)
    try:
        py_compile.compile(str(temporary), doraise=True)
        temporary.replace(BOT_PATH)
    finally:
        temporary.unlink(missing_ok=True)

    line_count = source.count("\n") + 1
    print(f"Static bot.py written: {line_count} lines")
    print("Next: compile, restart, test, then commit bot.py and remove the two materializer files.")


if __name__ == "__main__":
    main()
