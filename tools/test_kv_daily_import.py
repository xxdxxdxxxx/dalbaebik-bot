from __future__ import annotations
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import player_store

class KvDailyImportTests(unittest.TestCase):
    def fixture(self, pattern):
        path = next((ROOT / "scans" / "itogi").glob(pattern), None)
        if path is None:
            self.skipTest("runtime scans/itogi fixtures are not committed")
        return path

    def test_all_real_reports_parse_and_arithmetic(self):
        expected = {"2026-08-07": (3, 33), "2026-08-08": (3, 32),
                    "2026-08-09": (4, 34), "2026-08-13": (3, 36),
                    "2026-08-14": (3, 32), "2026-08-15": (3, 35),
                    "2026-08-16": (4, 33), "2026-08-20": (3, 31)}
        for path in sorted((ROOT / "scans" / "itogi").glob("2026-*.txt")):
            report = player_store.parse_kv_daily_report(path)
            if report["match_date"] in expected:
                self.assertEqual((report["stage_count"], len(report["rows"])), expected[report["match_date"]])
            self.assertIn(report["stage_count"], (3, 4))
            self.assertTrue(report["rows"])
            for row in report["rows"]:
                known = [v for v in row["stages"] if v is not None]
                if len(known) == report["stage_count"]:
                    self.assertEqual(row["total_grenades"], sum(known))
                elif row["total_grenades"] is not None:
                    self.assertGreaterEqual(row["total_grenades"], sum(known))
        old = player_store.parse_kv_daily_report(self.fixture("2026-08-07_*"))
        self.assertIsNone(old["rows"][0]["voice_seconds"])
        modern = player_store.parse_kv_daily_report(self.fixture("2026-08-15_*"))
        sosew = next(r for r in modern["rows"] if r["raw_nick"].casefold() == "sosew")
        self.assertEqual(sosew["voice_seconds"], 11 * 60 + 11)
        zero = next(r for r in modern["rows"] if r["raw_nick"].casefold() == "exterminat")
        self.assertEqual(zero["voice_seconds"], 0)

    def test_idempotency_correction_identity_and_schema_compatibility(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); db = root / "test.sqlite3"
            player_store.ensure_schema(db)
            pid = player_store.upsert_binding(db, "1", "sosew", discord_username="id621", guild_id=42)
            source = root / "2026-08-15_╨╕╤В╨╛╨│.txt"
            shutil.copy2(self.fixture("2026-08-15_*"), source)
            first = player_store.import_kv_daily_report(db, source, 42)
            second = player_store.import_kv_daily_report(db, source, 42)
            self.assertEqual((first["status"], second["status"]), ("created", "unchanged"))
            text = source.read_text(encoding="utf-8").replace("sosew(id621)                          7     31     14        52", "sosew(id621)                          8     31     14        53")
            source.write_text(text, encoding="utf-8")
            corrected = player_store.import_kv_daily_report(db, source, 42)
            self.assertEqual(corrected["status"], "corrected")
            con = player_store.connect(db)
            try:
                self.assertEqual(con.execute("SELECT COUNT(*) FROM kv_daily_reports").fetchone()[0], 1)
                row = con.execute("SELECT player_id,total_grenades,voice_seconds FROM kv_daily_players WHERE raw_nick='sosew'").fetchone()
                self.assertEqual(tuple(row), (pid, 53, 671))
                stages = [r[0] for r in con.execute("SELECT grenades FROM kv_daily_grenade_stages s JOIN kv_daily_players p USING(report_id,row_no) WHERE p.raw_nick='sosew' ORDER BY stage_no")]
                self.assertEqual(stages, [8, 31, 14])
                self.assertEqual(con.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0], str(player_store.SCHEMA_VERSION))
                self.assertEqual(con.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            finally:
                con.close()

    def test_bad_arithmetic_rolls_back(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); db = root / "test.sqlite3"; source = root / "2026-08-15_╨╕╤В╨╛╨│.txt"
            text = self.fixture("2026-08-15_*").read_text(encoding="utf-8").replace("8     18      9        35", "8     18      9        36", 1)
            source.write_text(text, encoding="utf-8")
            with self.assertRaises(ValueError):
                player_store.import_kv_daily_report(db, source, 42)
            self.assertFalse(db.exists())

if __name__ == "__main__": unittest.main()
