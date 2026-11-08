from __future__ import annotations

import os
import re
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import player_store
from tools import admin_ui

D1, D2, D3 = "2026-11-01", "2026-11-02", "2026-11-05"
GUILD = 1


class DayEfficiencyUnitTests(unittest.TestCase):
    def test_weights_sum_to_one_and_grenades_weigh_less_than_before(self):
        self.assertAlmostEqual(sum(admin_ui.EFF_WEIGHTS.values()), 1.0)
        # раньше гранаты весили 0.20 из 0.90 (≈22%); теперь заметно меньше
        self.assertLess(admin_ui.EFF_WEIGHTS["grenades"], 0.20 / 0.90)
        self.assertEqual(admin_ui.EFF_WEIGHTS["grenades"], 0.15)

    def test_eff_exists_with_tab_only(self):
        stats = {
            1: admin_ui.tab_metrics(1, 10, 2, 5, 3000),
            2: admin_ui.tab_metrics(1, 5, 5, 1, 1000),
        }
        eff = admin_ui.day_efficiency(stats)
        self.assertEqual({k: round(v, 1) for k, v in eff.items()}, {1: 100.0, 2: 0.0})

    def test_missing_grenades_do_not_punish_player(self):
        # p1 лучший по табу, но гранат нет; p2 хуже по табу, зато гранаты максимальные
        stats = {
            1: admin_ui.tab_metrics(1, 10, 2, 5, 3000),
            2: admin_ui.tab_metrics(1, 5, 5, 1, 1000, grenades=80),
            3: admin_ui.tab_metrics(1, 7, 3, 2, 2000, grenades=10),
        }
        eff = admin_ui.day_efficiency(stats)
        self.assertEqual(round(eff[1], 1), 100.0)          # без гранат — чистый таб
        self.assertEqual(round(eff[2], 1), 15.0)           # 0 по табу + 100 за гранаты × 0.15
        self.assertGreater(eff[3], eff[2])

    def test_grenades_ignored_when_nothing_to_compare(self):
        for grenades in (0, 30):                           # у всех поровну / только у одного
            stats = {
                1: admin_ui.tab_metrics(1, 10, 2, 5, 3000, grenades=grenades),
                2: admin_ui.tab_metrics(1, 5, 5, 1, 1000),
            }
            eff = admin_ui.day_efficiency(stats)
            self.assertEqual({k: round(v, 1) for k, v in eff.items()}, {1: 100.0, 2: 0.0})

    def test_no_players_no_eff(self):
        self.assertEqual(admin_ui.day_efficiency({}), {})


class PlayersPeriodTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {}, clear=False)
        self.env.start()
        for key in ("ADMIN_PASSWORD", "ADMIN_TRUST_PROXY"):
            os.environ.pop(key, None)
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "scan_stats.sqlite3"
        player_store.ensure_schema(self.db)
        player_store.upsert_guild_config(self.db, GUILD)
        self.ids = {}
        for slot, (name, discord_id) in enumerate((("Alpha", 100), ("Beta", 200), ("Gamma", 300)), 1):
            pid = player_store.upsert_binding(self.db, discord_id, name, guild_id=GUILD)
            player_store.upsert_roster_member(self.db, GUILD, pid, squad_id=1, slot=slot)
            self.ids[name] = pid
        a, b, g = self.ids["Alpha"], self.ids["Beta"], self.ids["Gamma"]
        # (kills, deaths, assists, score, grenades|None); None → игрока нет в отчёте гранат
        self.seed_day(D1, {a: (10, 1, 5, 900, 100), b: (2, 5, 1, 100, 20)})
        self.seed_day(D2, {a: (1, 5, 0, 50, 5), b: (9, 1, 4, 800, 90)})
        # «сегодня»: гранаты/войс почти не отсканированы, у Beta и Gamma есть только таб
        self.seed_day(D3, {a: (8, 2, 3, 700, 40), b: (4, 4, 1, 300, None), g: (6, 3, 2, 500, None)})
        self.patch = patch.object(admin_ui, "DB_PATH", self.db)
        self.patch.start()
        self.client = admin_ui.app.test_client()
        self.local = "http://localhost:8787"

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()
        self.env.stop()

    def seed_day(self, day, rows):
        with closing(player_store.connect(self.db)) as con:
            scan_id = con.execute(
                """INSERT INTO scans(stage,map,guild_id,channel_id,user_id,scanned_at,source_filename,match_date)
                   VALUES(1,'map',?,1,1,?,?,?)""", (GUILD, f"{day} 20:00:00", f"{day}.png", day)).lastrowid
            report_id = con.execute(
                """INSERT INTO kv_daily_reports(match_date,guild_id,source_path,source_sha256,stage_count)
                   VALUES(?,?,?,?,3)""", (day, GUILD, f"runtime:1:{day}", day)).lastrowid
            row_no = 0
            for place, (pid, (k, d, a, score, grenades)) in enumerate(rows.items(), 1):
                con.execute("""INSERT INTO scan_players(scan_id,place,nick,kills,deaths,assists,score,player_id)
                               VALUES(?,?,?,?,?,?,?,?)""", (scan_id, place, f"nick{pid}", k, d, a, score, pid))
                if grenades is None:
                    continue
                row_no += 1
                con.execute("""INSERT INTO kv_daily_players(report_id,row_no,player_id,raw_nick,voice_seconds,attended,total_grenades)
                               VALUES(?,?,?,?,600,1,?)""", (report_id, row_no, pid, f"nick{pid}", grenades))
                con.execute("""INSERT INTO kv_daily_grenade_stages(report_id,row_no,stage_no,grenades)
                               VALUES(?,?,1,?)""", (report_id, row_no, grenades))

    def get(self, path, **kw):
        return self.client.get(path, base_url=self.local, **kw)

    def table(self, query=""):
        """{ник: (дни, грены, eff|None)} со страницы Игроки."""
        html = self.get("/players" + query).get_data(as_text=True)
        out = {}
        for row in re.findall(r"<tr class='player-row'>(.*?)</tr>", html, flags=re.S):
            cells = [re.sub(r"<[^>]+>", "", c).strip() for c in re.findall(r"<td[^>]*>(.*?)</td>", row, flags=re.S)]
            eff = None if cells[3] in ("", "—") else float(cells[3])
            out[cells[0]] = (int(cells[1]), int(cells[2]), eff)
        return out, html

    # ------------------------------------------------------------------------------------
    def test_all_time_is_the_default(self):
        table, html = self.table()
        self.assertEqual(table, {
            "Alpha": (3, 145, 66.7),
            "Beta": (3, 110, 33.3),      # D3: в отчёте нет, но таб есть → день засчитан
            "Gamma": (1, 0, 44.1),       # только таб, без гранат — EFF всё равно есть
        })
        self.assertEqual(self.table("?days=all")[0], table)
        self.assertIn("<b>Всё время</b>", html)
        self.assertIn("За всё время: 3 дня КВ", html)

    def test_last_one_day(self):
        table, html = self.table("?days=1")
        self.assertEqual(table, {
            "Alpha": (1, 40, 100.0),
            "Beta": (1, 0, 0.0),
            "Gamma": (1, 0, 44.1),
        })
        self.assertIn("<b>1</b>", html)
        self.assertIn("Последний день КВ: 05.11.26", html)

    def test_last_two_days_ignore_older_ones(self):
        table, html = self.table("?days=2")
        self.assertEqual(table, {
            "Alpha": (2, 45, 50.0),      # D1 (100 гранат) в период не входит
            "Beta": (2, 90, 50.0),
            "Gamma": (1, 0, 44.1),
        })
        self.assertIn("<b>2</b>", html)
        self.assertIn("Последние 2 дня КВ", html)

    def test_three_days_equals_all_time_and_oversized_period_is_clamped(self):
        everything = self.table("?days=all")[0]
        self.assertEqual(self.table("?days=3")[0], everything)
        table, html = self.table("?days=7")
        self.assertEqual(table, everything)
        self.assertIn("в базе только 3", html)
        self.assertIn("<b>7</b>", html)

    def test_garbage_period_falls_back_to_all_time(self):
        everything = self.table()[0]
        for raw in ("abc", "0", "-3", "", "1; DROP TABLE players"):
            with self.subTest(raw=raw):
                table, html = self.table("?days=" + raw)
                self.assertEqual(table, everything)
                self.assertIn("<b>Всё время</b>", html)

    def test_period_links_present(self):
        html = self.table("?days=2")[1]
        for n in (1, 3, 5, 7, 14, 30):
            self.assertIn(f"href='/players?days={n}'", html)
        self.assertIn("href='/players?days=all'", html)
        self.assertNotIn("href='/players?days=2'", html)       # выбранный — не ссылка

    def test_period_counts_only_not_archived_days(self):
        r = self.client.post(f"/day/{D3}/archive", base_url=self.local)
        self.assertIn(r.status_code, (302, 303))
        table, html = self.table("?days=1")
        self.assertEqual(table["Alpha"], (1, 5, 0.0))          # теперь «последний» — D2
        self.assertEqual(table["Beta"], (1, 90, 100.0))
        self.assertEqual(table["Gamma"], (0, 0, None))         # в D2 Gamma не было
        self.assertIn("Последний день КВ: 02.11.26", html)

    def test_player_without_tabs_in_period_has_no_eff_but_row_stays(self):
        table, _ = self.table("?days=2")
        self.assertIn("Gamma", table)
        # у Gamma в D2 нет ни таба, ни отчёта; EFF только за D3
        self.assertEqual(table["Gamma"][0], 1)

    def test_day_page_gives_eff_to_players_without_grenades(self):
        html = self.get(f"/day/{D3}").get_data(as_text=True)
        self.assertIn("<td class='eff'>100.0</td>", html)     # Alpha
        self.assertIn("<td class='eff'>44.1</td>", html)      # Gamma: только таб, нет в отчёте
        self.assertIn("<td class='eff'>0.0</td>", html)       # Beta: таб есть, гранат нет
        self.assertNotIn("<td class='eff'>—</td>", html)

    def test_average_uses_same_numbers_as_day_pages(self):
        with closing(player_store.connect(self.db)) as con:
            avg = admin_ui.average_player_efficiencies(con)
            only_last = admin_ui.average_player_efficiencies(con, {D3})
        self.assertEqual(avg[self.ids["Gamma"]], 44.1)
        self.assertEqual(only_last, {self.ids["Alpha"]: 100.0, self.ids["Beta"]: 0.0,
                                     self.ids["Gamma"]: 44.1})

    def test_empty_database_renders(self):
        for path in (Path(self.tmp.name) / "empty.sqlite3",):
            player_store.ensure_schema(path)
            with patch.object(admin_ui, "DB_PATH", path):
                for query in ("", "?days=1", "?days=all"):
                    r = self.get("/players" + query)
                    self.assertEqual(r.status_code, 200)
                    self.assertIn("Дней КВ пока нет", r.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
