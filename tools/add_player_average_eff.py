#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ADMIN = ROOT / "tools" / "admin_ui.py"
TEST = ROOT / "tools" / "test_admin_efficiency.py"


def replace_one(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected one match, found {count}")
    return text.replace(old, new, 1)


def main() -> None:
    text = ADMIN.read_text(encoding="utf-8")
    helper = '''def average_player_efficiencies(con: Any) -> dict[int, float]:
    """Average the existing relative day EFF over completed KV days."""
    rows = q(con, """
        WITH tabs AS (
            SELECT s.match_date date, sp.player_id,
                   COUNT(*) tabs,
                   SUM(sp.kills) kills,
                   SUM(sp.deaths) deaths,
                   SUM(sp.assists) assists,
                   COALESCE(SUM(sp.score), 0) score
            FROM scans s
            JOIN scan_players sp ON sp.scan_id=s.id
            WHERE s.match_date IS NOT NULL AND sp.player_id IS NOT NULL
            GROUP BY s.match_date, sp.player_id
        ), grenades AS (
            SELECT r.match_date date, k.player_id,
                   MAX(k.total_grenades) grenades
            FROM kv_daily_reports r
            JOIN kv_daily_players k ON k.report_id=r.id
            WHERE k.player_id IS NOT NULL AND k.total_grenades IS NOT NULL
            GROUP BY r.match_date, k.player_id
        )
        SELECT t.date, t.player_id, t.tabs, t.kills, t.deaths,
               t.assists, t.score, g.grenades
        FROM tabs t
        JOIN grenades g ON g.date=t.date AND g.player_id=t.player_id
        ORDER BY t.date, t.player_id
    """)
    weights = {"kills_per_tab": 0.25, "kda": 0.25,
               "score_per_tab": 0.20, "grenades": 0.20}
    by_day: dict[str, dict[int, dict[str, float]]] = {}
    for row in rows:
        tabs = max(int(row["tabs"]), 1)
        deaths = int(row["deaths"] or 0)
        kills = int(row["kills"] or 0)
        assists = int(row["assists"] or 0)
        by_day.setdefault(str(row["date"]), {})[int(row["player_id"])] = {
            "kills_per_tab": kills / tabs,
            "kda": ((kills + assists) / deaths) if deaths else float(kills + assists),
            "score_per_tab": float(row["score"] or 0) / tabs,
            "grenades": float(row["grenades"]),
        }
    samples: dict[int, list[float]] = {}
    for values_by_player in by_day.values():
        bounds = {
            key: (min(values[key] for values in values_by_player.values()),
                  max(values[key] for values in values_by_player.values()))
            for key in weights
        }
        for player_id, values in values_by_player.items():
            normalized = {}
            for key, value in values.items():
                minimum, maximum = bounds[key]
                normalized[key] = (50.0 if maximum == minimum
                                   else (value - minimum) / (maximum - minimum) * 100.0)
            score = sum(normalized[key] * weight for key, weight in weights.items()) / sum(weights.values())
            samples.setdefault(player_id, []).append(score)
    return {player_id: round(sum(values) / len(values), 1)
            for player_id, values in samples.items()}


'''
    text = replace_one(text, '@app.get("/players")\n', helper + '@app.get("/players")\n', "EFF helper")
    text = replace_one(
        text,
        '    groups: dict[int, list[dict[str, Any]]] = {99: [], **{i: [] for i in range(1, 7)}}\n',
        '    eff_by_player = average_player_efficiencies(con)\n'
        '    groups: dict[int, list[dict[str, Any]]] = {99: [], **{i: [] for i in range(1, 7)}}\n',
        "players EFF load",
    )
    text = replace_one(
        text,
        '            f"<td>{r[\'days\'] or 0}</td><td class=\'total\'>{r[\'grenades\'] or 0}</td>"\n'
        '            "</tr>"\n',
        '            f"<td>{r[\'days\'] or 0}</td><td class=\'total\'>{r[\'grenades\'] or 0}</td>"\n'
        '            f"<td class=\'eff\'>{esc(eff_by_player.get(int(r[\'id\']), \'—\'))}</td>"\n'
        '            "</tr>"\n',
        "players EFF cell",
    )
    text = replace_one(text, "f\"<tbody class='squad-group'><tr class='squad-title'><td colspan='3'>{label}</td></tr>\"", "f\"<tbody class='squad-group'><tr class='squad-title'><td colspan='4'>{label}</td></tr>\"", "players colspan")
    text = replace_one(
        text,
        '            "<div class=\'panel\'><table><thead><tr><th class=\'nick\'>Ник</th><th>Дни</th><th>Грены</th></tr></thead>"\n',
        '            "<div class=\'panel\'><table><thead><tr><th class=\'nick\'>Ник</th><th>Дни</th><th>Грены</th><th title=\'Среднее текущего EFF по дням КВ\'>EFF</th></tr></thead>"\n',
        "players EFF header",
    )
    compile(text, str(ADMIN), "exec")
    ADMIN.write_text(text, encoding="utf-8")
    TEST.write_text('''from __future__ import annotations
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
''', encoding="utf-8")
    print("OK: added average EFF column to Players")


if __name__ == "__main__":
    main()
