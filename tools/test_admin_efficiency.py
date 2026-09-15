from __future__ import annotations
import sqlite3
import sys
import unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools import admin_ui

class AverageEfficiencyTests(unittest.TestCase):
    def test_average_uses_relative_eff_of_each_day(self):
        con = sqlite3.connect(":memory:")
        con.row_factory = sqlite3.Row
        con.executescript("""
            CREATE TABLE scans(id INTEGER PRIMARY KEY, match_date TEXT);
            CREATE TABLE scan_players(scan_id INTEGER, player_id INTEGER, kills INTEGER, deaths INTEGER, assists INTEGER, score INTEGER);
            CREATE TABLE kv_daily_reports(id INTEGER PRIMARY KEY, match_date TEXT);
            CREATE TABLE kv_daily_players(report_id INTEGER, player_id INTEGER, total_grenades INTEGER);
            INSERT INTO scans VALUES(1,'2026-09-10'),(2,'2026-09-11');
            INSERT INTO scan_players VALUES
              (1,1,10,2,5,3000),(1,2,5,5,1,1000),(2,1,7,3,2,2000);
            INSERT INTO kv_daily_reports VALUES(1,'2026-09-10'),(2,'2026-09-11');
            INSERT INTO kv_daily_players VALUES(1,1,40),(1,2,10),(2,1,20);
        """)
        self.assertEqual(admin_ui.average_player_efficiencies(con), {1: 75.0, 2: 0.0})
        con.close()

if __name__ == "__main__":
    unittest.main()
