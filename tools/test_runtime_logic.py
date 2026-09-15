from __future__ import annotations
import ast
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import bot

class RuntimeLogicTests(unittest.TestCase):
    def test_schedule(self):
        thu=bot.MSK.localize(datetime(2026,9,17,12)); sun=bot.MSK.localize(datetime(2026,9,20,12))
        self.assertEqual(bot.kv_start(thu).strftime("%H:%M"),"19:30")
        self.assertEqual([t.strftime("%H:%M") for _,t,_ in bot.kv_grenade_steps(thu)],["20:05","20:25","20:50","21:15"])
        self.assertEqual(bot.kv_start(sun).strftime("%H:%M"),"18:30")
        self.assertEqual([t.strftime("%H:%M") for _,t,_ in bot.kv_grenade_steps(sun)],["19:00","19:20","19:40","20:00","20:15"])
    def test_setup_source(self):
        source=Path(bot.__file__).read_text(encoding="utf-8")
        tree=ast.parse(source)
        setup=next(n for n in tree.body if isinstance(n,ast.AsyncFunctionDef) and n.name=="cmd_setup")
        self.assertNotIn("match_date",{n.id for n in ast.walk(setup) if isinstance(n,ast.Name)})
class LateStartTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_late_fake_session(self):
        late=bot.MSK.localize(datetime(2026,9,17,22)); data=bot.default_db()
        with patch.object(bot,"now_msk",return_value=late): result=await bot.ensure_session_reset(data)
        self.assertFalse(result["kv_session_active"]); self.assertIsNone(result["session_date"])
if __name__=="__main__": unittest.main()
