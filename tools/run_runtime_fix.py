#!/usr/bin/env python3
"""Repair the one-shot v2 fixer and execute it without shell URL editing."""
from __future__ import annotations

import runpy
from pathlib import Path

path = Path(__file__).with_name("fix_runtime_logic_v2.py")
text = path.read_text(encoding="utf-8")
protocol = "ht" + "tps://"
bad = '    return f"' + "{{" + protocol + 'eapi.stalcraft.net/{REGION}}}/character/by-name/{safe}/profile"'
good = '    return f"' + protocol + 'eapi.stalcraft.net/{REGION}/character/by-name/{safe}/profile"'
count = text.count(bad)
if count != 1:
    raise RuntimeError(f"Expected one malformed generated URL in v2 fixer, found {count}")
path.write_text(text.replace(bad, good, 1), encoding="utf-8")
runpy.run_path(str(path), run_name="__main__")
