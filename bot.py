"""Runtime entry point built from the last full source snapshot."""
from __future__ import annotations

import subprocess
from pathlib import Path

from tools.runtime_source_cleanup import clean_source

ROOT = Path(__file__).resolve().parent
SOURCE_COMMIT = "3f60b0581ce5e56d75f93cabdb7ccf5c7b14f960"
try:
    original = subprocess.check_output(
        ["git", "show", f"{SOURCE_COMMIT}:bot.py"],
        cwd=ROOT,
        text=True,
        encoding="utf-8",
    )
except (OSError, subprocess.CalledProcessError) as exc:
    raise RuntimeError("Не удалось загрузить исходник bot.py из локальной истории Git") from exc

exec(compile(clean_source(original), str(Path(__file__)), "exec"), globals(), globals())
