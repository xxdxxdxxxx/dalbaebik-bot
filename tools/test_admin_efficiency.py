from __future__ import annotations
import re
import sqlite3
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

T = admin_ui.tab_metrics


def eff(tabs, grenades=None):
    """tabs — списки (player_id, показатели строки); округляем до 0.1 для читаемости."""
    return {k: round(v, 1) for k, v in admin_ui.day_efficiency(tabs, grenades).items()}


class EfficiencyFormulaTests(unittest.TestCase):
    def test_each_tab_is_compared_on_its_own(self):
        # Таб 1 «тяжёлый» (у всех мало), таб 2 «лёгкий» (у всех много). Лучший в своём табе
        # получает 100 независимо от того, насколько цифры больше или меньше соседнего таба.
        hard = [(1, T(5, 2, 1, 500)), (2, T(3, 4, 0, 200))]
        easy = [(3, T(20, 2, 5, 3000)), (4, T(12, 6, 2, 1500))]
        self.assertEqual(eff([hard, easy]), {1: 100.0, 2: 0.0, 3: 100.0, 4: 0.0})

    def test_day_eff_is_mean_over_player_tabs(self):
        best = (10, 1, 5, 900)
        worst = (2, 5, 1, 100)
        tab_a = [(1, T(*best)), (2, T(*worst))]
        tab_b = [(1, T(*worst)), (2, T(*best))]
        self.assertEqual(eff([tab_a, tab_b]), {1: 50.0, 2: 50.0})

    def test_one_outlier_does_not_squash_everybody_else(self):
        tab = [(1, T(100, 1, 0, 9000)),
               (2, T(10, 5, 0, 500)), (3, T(9, 5, 0, 400)),
               (4, T(8, 5, 0, 300)), (5, T(7, 5, 0, 200))]
        # при min–max второй получил бы ≈3 из 100
        self.assertEqual(eff([tab]), {1: 100.0, 2: 75.0, 3: 50.0, 4: 25.0, 5: 0.0})

    def test_equal_results_share_the_average_rank(self):
        same = T(10, 2, 2, 1000)
        tab = [(1, same), (2, same), (3, same), (4, T(1, 5, 0, 10))]
        result = admin_ui.day_efficiency([tab])
        for pid in (1, 2, 3):
            self.assertAlmostEqual(result[pid], 200 / 3)
        self.assertEqual(result[4], 0.0)

    def test_tab_with_one_player_has_nobody_to_compare_with(self):
        alone = [(1, T(10, 1, 1, 100))]
        pair = [(1, T(10, 1, 1, 100)), (2, T(1, 5, 0, 10))]
        self.assertEqual(eff([alone]), {})
        self.assertEqual(eff([alone, pair]), {1: 100.0, 2: 0.0})   # одинокий таб просто не считается

    def test_player_twice_in_one_tab_counts_each_row(self):
        # у sawerqe два ника в одном табе: строки не склеиваются, EFF — среднее по строкам
        tab = [(1, T(30, 2, 5, 9000)), (1, T(2, 10, 0, 100)), (2, T(10, 5, 2, 3000))]
        self.assertEqual(eff([tab]), {1: 50.0, 2: 50.0})

    def test_missing_score_is_skipped_and_weights_are_renormalized(self):
        tab = [(1, T(10, 2, 5, None)), (2, T(5, 5, 1, None))]
        self.assertEqual(eff([tab]), {1: 100.0, 2: 0.0})

    # ── гранаты ─────────────────────────────────────────────────────────────────────────
    def _three(self):
        return [(1, T(10, 2, 5, 3000)), (2, T(5, 5, 1, 1000)), (3, T(7, 3, 2, 2000))]

    def test_missing_grenades_do_not_punish_player(self):
        # p1 лучший по табу, но гранат нет; у p2 по табу худший, зато гранаты максимальные
        result = eff([self._three()], {2: 80, 3: 10})
        self.assertEqual(result[1], 100.0)               # без гранат — чистый таб
        self.assertEqual(result[2], 15.0)                # 0 по табу + 100 за гранаты × 0.15
        self.assertEqual(result[3], 42.5)                # 50 × 0.85 + 0 × 0.15
        self.assertGreater(result[3], result[2])

    def test_none_is_the_same_as_absent(self):
        self.assertEqual(eff([self._three()], {1: None, 2: 80, 3: 10}),
                         eff([self._three()], {2: 80, 3: 10}))

    def test_zero_grenades_is_a_real_zero_and_lowers_eff(self):
        # p1 лучший по табу, но кинул 0 гранат, когда остальные кидали: это минус
        result = eff([self._three()], {1: 0, 2: 80, 3: 10})
        self.assertEqual(result, {1: 85.0, 2: 15.0, 3: 50.0})

    def test_zero_is_not_the_same_as_not_scanned(self):
        scanned_zero = eff([self._three()], {1: 0, 2: 80, 3: 10})
        not_scanned = eff([self._three()], {1: None, 2: 80, 3: 10})
        self.assertEqual(scanned_zero[1], 85.0)       # нулю гранат — минус 15 очков
        self.assertEqual(not_scanned[1], 100.0)       # не отсканировано — гранаты просто пропущены

    def test_grenades_ignored_when_nothing_to_compare(self):
        pair = [(1, T(10, 2, 5, 3000)), (2, T(5, 5, 1, 1000))]
        for grenades in ({1: 30}, {1: 30, 2: 30}):      # только у одного / у всех поровну
            with self.subTest(grenades=grenades):
                self.assertEqual(eff([pair], grenades), {1: 100.0, 2: 0.0})

    def test_grenades_are_compared_per_tab_not_per_day(self):
        same = T(10, 2, 2, 1000)
        tab1 = [(1, same), (2, same), (3, same)]
        tab2 = [(1, same), (3, same)]
        # всего по 60, но p1 играл 2 таба (30 на таб), p2 — один (60 на таб), p3 — 15 на таб
        result = eff([tab1, tab2], {1: 60, 2: 60, 3: 30})
        self.assertEqual(result, {1: 50.0, 2: 57.5, 3: 42.5})

    def test_no_players_no_eff(self):
        self.assertEqual(admin_ui.day_efficiency([]), {})
        self.assertEqual(admin_ui.day_efficiency([[]], {1: 5}), {})

    def test_percentile_ranks(self):
        self.assertEqual(admin_ui.percentile_ranks({"a": 1, "b": 2, "c": 3}), {"a": 0.0, "b": 50.0, "c": 100.0})
        self.assertEqual(admin_ui.percentile_ranks({"a": 5}), {"a": 50.0})


class AverageEfficiencyTests(unittest.TestCase):
    def test_average_uses_relative_eff_of_each_day(self):
        con = sqlite3.connect(":memory:")
        con.row_factory = sqlite3.Row
        con.executescript("""
            CREATE TABLE scans(id INTEGER PRIMARY KEY, match_date TEXT);
            CREATE TABLE scan_players(scan_id INTEGER, place INTEGER, player_id INTEGER, nick TEXT, kills INTEGER, deaths INTEGER, assists INTEGER, score INTEGER);
            CREATE TABLE kv_daily_reports(id INTEGER PRIMARY KEY, match_date TEXT);
            CREATE TABLE kv_daily_players(report_id INTEGER, player_id INTEGER, total_grenades INTEGER);
            INSERT INTO scans VALUES(1,'2026-09-10'),(2,'2026-09-11');
            INSERT INTO scan_players VALUES
              (1,1,1,'a',10,2,5,3000),(1,2,2,'b',5,5,1,1000),
              (2,1,1,'a',7,3,2,2000),(2,2,2,'b',9,1,4,2500);
            INSERT INTO kv_daily_reports VALUES(1,'2026-09-10'),(2,'2026-09-11');
            INSERT INTO kv_daily_players VALUES(1,1,40),(1,2,10),(2,1,20);
        """)
        # день 1: p1 100, p2 0; день 2: p2 лучший (гранат у него нет — не штраф), p1 хуже
        self.assertEqual(admin_ui.average_player_efficiencies(con), {1: 50.0, 2: 50.0})
        self.assertEqual(admin_ui.average_player_efficiencies(con, {"2026-09-11"}), {1: 0.0, 2: 100.0})
        con.close()


class PeriodMatchesDayPageTests(unittest.TestCase):
    """EFF на «Игроках» (по периоду) не должен расходиться с EFF на странице дня."""
    DAY = "2026-11-12"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "scan_stats.sqlite3"
        player_store.ensure_schema(self.db)
        player_store.upsert_guild_config(self.db, 1)
        self.ids = {name: player_store.upsert_binding(self.db, 300 + i, name, guild_id=1)
                    for i, name in enumerate(("Alpha", "Beta", "Gamma"))}
        rows = [("Alpha", self.ids["Alpha"], 20, 3, 5, 3000), ("Beta", self.ids["Beta"], 10, 8, 2, 1500),
                ("Gamma", None, 15, 5, 3, 2200)]      # ник Gamma в табе не привязан, но такой игрок есть
        with closing(player_store.connect(self.db)) as con:
            scan = con.execute("""INSERT INTO scans(stage,map,guild_id,channel_id,user_id,scanned_at,source_filename,match_date)
                                  VALUES(1,'m',1,1,1,'t','a.png',?)""", (self.DAY,)).lastrowid
            for place, (nick, pid, k, d, a, s) in enumerate(rows, 1):
                con.execute("""INSERT INTO scan_players(scan_id,place,nick,kills,deaths,assists,score,player_id)
                               VALUES(?,?,?,?,?,?,?,?)""", (scan, place, nick, k, d, a, s, pid))
            con.commit()
        self.patch = patch.object(admin_ui, "DB_PATH", self.db)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def test_unbound_tab_nick_gets_eff_in_period_like_on_the_day_page(self):
        html = admin_ui.app.test_client().get(f"/day/{self.DAY}", base_url="http://localhost:8787").get_data(as_text=True)
        page = {n: float(v) for n, v in re.findall(r"<td class='nick[^>]*><a[^>]*>([^<]+)</a>.*?<td class='eff'>([\d.]+)</td>",
                                                    html, flags=re.S)}
        with closing(player_store.connect(self.db)) as con:
            period = admin_ui.average_player_efficiencies(con)
        self.assertEqual(page, {"Alpha": 100.0, "Beta": 0.0, "Gamma": 50.0})
        self.assertEqual({n: period[i] for n, i in self.ids.items()}, page)


class DayFormKeepsMissingGrenadesTests(unittest.TestCase):
    """Сохранение страницы дня не должно превращать «не отсканировано» в реальный 0."""
    DAY = "2026-11-12"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "scan_stats.sqlite3"
        player_store.ensure_schema(self.db)
        player_store.upsert_guild_config(self.db, 1)
        ids = [player_store.upsert_binding(self.db, 100 + i, name, guild_id=1)
               for i, name in enumerate(("Alpha", "Beta"))]
        with closing(player_store.connect(self.db)) as con:
            scan = con.execute("""INSERT INTO scans(stage,map,guild_id,channel_id,user_id,scanned_at,source_filename,match_date)
                                  VALUES(1,'map',1,1,1,'t','a.png',?)""", (self.DAY,)).lastrowid
            for place, (pid, k) in enumerate(zip(ids, (10, 5)), 1):
                con.execute("""INSERT INTO scan_players(scan_id,place,nick,kills,deaths,assists,score,player_id)
                               VALUES(?,?,?,?,?,?,?,?)""", (scan, place, f"n{pid}", k, 2, 1, k * 100, pid))
            rid = con.execute("""INSERT INTO kv_daily_reports(match_date,guild_id,source_path,source_sha256,stage_count)
                                 VALUES(?,1,'runtime',?,3)""", (self.DAY, self.DAY)).lastrowid
            # row 1: гранаты есть; row 2: НЕ отсканированы (NULL), этапы пустые
            con.execute("""INSERT INTO kv_daily_players(report_id,row_no,player_id,raw_nick,voice_seconds,attended,total_grenades)
                           VALUES(?,1,?,'Alpha',600,1,40),(?,2,?,'Beta',600,1,NULL)""", (rid, ids[0], rid, ids[1]))
            con.executemany("INSERT INTO kv_daily_grenade_stages(report_id,row_no,stage_no,grenades) VALUES(?,?,?,?)",
                            [(rid, 1, 1, 40), (rid, 1, 2, 0), (rid, 1, 3, 0),
                             (rid, 2, 1, None), (rid, 2, 2, None), (rid, 2, 3, None)])
            con.commit()
        self.patch = patch.object(admin_ui, "DB_PATH", self.db)
        self.patch.start()
        self.client = admin_ui.app.test_client()
        self.local = "http://localhost:8787"

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def form_fields(self, mode):
        html = self.client.get(f"/day/{self.DAY}?mode={mode}", base_url=self.local).get_data(as_text=True)
        form = html[html.index("<form class='day-edit'"):]
        return html, dict(re.findall(r"<input[^>]*name='([^']+)'[^>]*value='([^']*)'", form))

    def save(self, mode, fields):
        r = self.client.post(f"/day/{self.DAY}/save", base_url=self.local, data=fields)
        self.assertIn(r.status_code, (302, 303))

    def grenade_rows(self):
        with closing(player_store.connect(self.db)) as con:
            total = [r[0] for r in con.execute(
                "SELECT total_grenades FROM kv_daily_players ORDER BY row_no")]
            stages = [r[0] for r in con.execute(
                "SELECT grenades FROM kv_daily_grenade_stages WHERE row_no=2 ORDER BY stage_no")]
        return total, stages

    def test_total_mode_save_keeps_null(self):
        _, fields = self.form_fields("total")
        self.assertEqual((fields["w1"], fields["w2"]), ("40", ""))   # у неотсканированного поле пустое, не «0»
        self.save("total", fields)
        self.assertEqual(self.grenade_rows(), ([40, None], [None, None, None]))

    def test_stages_mode_save_keeps_null(self):
        _, fields = self.form_fields("stages")
        self.assertEqual([fields[f"g2_{i}"] for i in (1, 2, 3)], ["", "", ""])
        self.save("stages", fields)
        self.assertEqual(self.grenade_rows(), ([40, None], [None, None, None]))

    def test_unscanned_player_still_has_eff_and_shows_dash(self):
        html, _ = self.form_fields("stages")
        self.assertIn("<td class='total dim' title='не отсканировано'>—</td>", html)
        self.assertEqual(len(re.findall(r"<td class='eff'>(?:100\.0|0\.0)</td>", html)), 2)


if __name__ == "__main__":
    unittest.main()
