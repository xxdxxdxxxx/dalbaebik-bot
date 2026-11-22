"""«Гр. ср.» на странице «Игроки»: среднее число гранат за полный КВ, в том числе когда игрок был не на всех этапах."""
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

GUILD = 1
A, B, C, D, E = "2026-11-01", "2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05"


class GrenadeAverageTests(unittest.TestCase):
    """
    A, B — табы всех трёх этапов загружены.
        Alpha играл все этапы обоих дней (по 45 гранат), Beta — этапы 1–2 в дне A (30) и все три в дне B (60),
        Gamma — только этап 1 в дне A (15).
    C — загружены табы двух этапов из трёх: «не на табе» ещё не значит «не играл этап».
    D — гранаты не отсканированы (NULL) у Epsilon и настоящий 0 у Zeta.
    E — таба у дня нет вообще, считается весь отчёт.
    """

    def setUp(self):
        self.env = patch.dict(os.environ, {}, clear=False)
        self.env.start()
        for key in ("ADMIN_PASSWORD", "ADMIN_TRUST_PROXY"):
            os.environ.pop(key, None)
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "scan_stats.sqlite3"
        player_store.ensure_schema(self.db)
        player_store.upsert_guild_config(self.db, GUILD)
        self.pid = {}
        for slot, nick in enumerate(("Alpha", "Beta", "Gamma", "Delta", "Epsilon", "Zeta", "Eta"), 1):
            self.pid[nick] = player_store.upsert_binding(self.db, 100 * slot, nick, guild_id=GUILD)
            player_store.upsert_roster_member(self.db, GUILD, self.pid[nick], squad_id=1, slot=slot)
        # день: на каких этапах кто стоит на табе; гранаты за день
        self.seed(A, 3, {1: ["Alpha", "Beta", "Gamma"], 2: ["Alpha", "Beta"], 3: ["Alpha"]},
                  {"Alpha": 45, "Beta": 30, "Gamma": 15})
        self.seed(B, 3, {1: ["Alpha", "Beta"], 2: ["Alpha", "Beta"], 3: ["Alpha", "Beta"]},
                  {"Alpha": 45, "Beta": 60})
        self.seed(C, 3, {1: ["Delta"], 2: ["Delta"]}, {"Delta": 20})
        self.seed(D, 3, {1: ["Epsilon", "Zeta"]}, {"Epsilon": None, "Zeta": 0})
        self.seed(E, 3, {}, {"Eta": 30})
        self.patch = patch.object(admin_ui, "DB_PATH", self.db)
        self.patch.start()
        self.client = admin_ui.app.test_client()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()
        self.env.stop()

    def seed(self, day, stage_count, tabs, grenades):
        with closing(player_store.connect(self.db)) as con:
            for stage, nicks in tabs.items():
                scan_id = con.execute(
                    """INSERT INTO scans(stage,map,guild_id,channel_id,user_id,scanned_at,source_filename,match_date)
                       VALUES(?,'map',?,1,1,?,?,?)""", (stage, GUILD, f"{day} 20:00:00", f"{day}-{stage}.png", day)).lastrowid
                for place, nick in enumerate(nicks, 1):
                    con.execute("""INSERT INTO scan_players(scan_id,place,nick,kills,deaths,assists,score,player_id)
                                   VALUES(?,?,?,5,2,1,300,?)""", (scan_id, place, nick, self.pid[nick]))
            report_id = con.execute(
                """INSERT INTO kv_daily_reports(match_date,guild_id,source_path,source_sha256,stage_count)
                   VALUES(?,?,?,?,?)""", (day, GUILD, f"runtime:1:{day}", day, stage_count)).lastrowid
            for row_no, (nick, value) in enumerate(grenades.items(), 1):
                con.execute("""INSERT INTO kv_daily_players(report_id,row_no,player_id,raw_nick,voice_seconds,attended,total_grenades)
                               VALUES(?,?,?,?,600,1,?)""", (report_id, row_no, self.pid[nick], nick, value))

    def averages(self):
        """{ник: info} — то, что считает страница «Игроки» за всё время."""
        with closing(player_store.connect(self.db)) as con:
            per_player: dict[int, dict[str, int | None]] = {}
            for row in con.execute("SELECT r.match_date d, k.player_id p, k.total_grenades g "
                                   "FROM kv_daily_players k JOIN kv_daily_reports r ON r.id=k.report_id"):
                per_player.setdefault(int(row["p"]), {})[row["d"]] = row["g"]
            raw = admin_ui.player_grenade_averages(con, per_player)
        names = {pid: nick for nick, pid in self.pid.items()}
        return {names[pid]: info for pid, info in raw.items()}

    # ── расчёт ──────────────────────────────────────────────────────────────
    def test_full_participation_is_plain_mean_per_day(self):
        alpha = self.averages()["Alpha"]
        self.assertEqual((alpha["avg"], alpha["days"], alpha["total"], alpha["partial"]), (45.0, 2, 90, False))

    def test_partial_stages_are_scaled_to_full_kv(self):
        beta = self.averages()["Beta"]
        # день A: 30 гранат за 2 этапа из 3, день B: 60 за 3 из 3 → 90 / 5 этапов = 18 на этап × 3 этапа
        self.assertEqual((beta["avg"], beta["played"], beta["full"], beta["partial"]), (54.0, 5, 6, True))
        self.assertEqual(beta["total"], 90)
        self.assertGreater(beta["avg"], beta["total"] / beta["days"])     # без пересчёта было бы 45

    def test_single_stage_player_is_scaled_too(self):
        gamma = self.averages()["Gamma"]
        self.assertEqual((gamma["avg"], gamma["partial"]), (45.0, True))   # 15 за один этап → 45 за КВ

    def test_day_with_incomplete_tabs_is_not_scaled(self):
        # загружено 2 таба из 3: Delta мог играть и третий этап, поэтому пересчёта нет
        delta = self.averages()["Delta"]
        self.assertEqual((delta["avg"], delta["partial"]), (20.0, False))

    def test_not_scanned_day_is_skipped_but_real_zero_counts(self):
        averages = self.averages()
        self.assertNotIn("Epsilon", averages)                              # NULL — «не отсканировано», среднего нет
        self.assertEqual((averages["Zeta"]["avg"], averages["Zeta"]["days"]), (0.0, 1))

    def test_day_without_tabs_counts_whole_report(self):
        eta = self.averages()["Eta"]
        self.assertEqual((eta["avg"], eta["partial"]), (30.0, False))

    def test_scaling_never_lowers_the_average(self):
        for nick, info in self.averages().items():
            with self.subTest(nick=nick):
                self.assertGreaterEqual(info["avg"] + 1e-9, info["total"] / info["days"])

    def test_duplicate_tab_of_the_same_stage_is_not_another_stage(self):
        # этап 1 дня A загрузили второй раз (повторный скрин): этапов по-прежнему три, Gamma — на одном
        with closing(player_store.connect(self.db)) as con:
            scan_id = con.execute(
                """INSERT INTO scans(stage,map,guild_id,channel_id,user_id,scanned_at,source_filename,match_date)
                   VALUES(1,'map',?,1,1,?,?,?)""", (GUILD, f"{A} 21:00:00", f"{A}-1-again.png", A)).lastrowid
            con.execute("""INSERT INTO scan_players(scan_id,place,nick,kills,deaths,assists,score,player_id)
                           VALUES(?,1,'Gamma',5,2,1,300,?)""", (scan_id, self.pid["Gamma"]))
        gamma = self.averages()["Gamma"]
        self.assertEqual((gamma["avg"], gamma["played"], gamma["full"]), (45.0, 1, 3))

    # ── страница ────────────────────────────────────────────────────────────
    def cells(self):
        html = self.client.get("/players", base_url="http://localhost:8787").get_data(as_text=True)
        out = {}
        for row in re.findall(r"<tr class='player-row'>(.*?)</tr>", html, flags=re.S):
            nick = re.sub(r"<[^>]+>", "", re.search(r"<td class='nick grow'>(.*?)</td>", row, re.S).group(1)).strip()
            cell = re.search(r"<td class='(gavg[^']*)'[^>]*>(.*?)</td>", row, re.S)
            out[nick] = (cell.group(2).strip(), "partial" in cell.group(1))
        return out, html

    def test_page_shows_average_star_and_dash(self):
        cells, html = self.cells()
        self.assertEqual(cells["Alpha"], ("45", False))
        self.assertEqual(cells["Beta"], ("54", True))          # со звёздочкой (CSS): пересчитано на полный КВ
        self.assertEqual(cells["Gamma"], ("45", True))
        self.assertEqual(cells["Delta"], ("20", False))
        self.assertEqual(cells["Epsilon"], ("—", False))
        self.assertEqual(cells["Zeta"], ("0", False))
        self.assertEqual(cells["Eta"], ("30", False))
        self.assertIn(">Гр. ср.</th>", html)
        self.assertNotIn(">Грены</th>", html)

    def test_tooltip_explains_the_numbers(self):
        html = self.cells()[1]
        beta = next(chunk for chunk in html.split("<tr class='player-row'>") if ">Beta<" in chunk)
        self.assertIn("Всего 90 за 2 дня", beta)
        self.assertIn("Этапов сыграно 5 из 6", beta)
        self.assertIn("без пересчёта было бы 45.0", beta)

    def test_period_limits_the_averaged_days(self):
        html = self.client.get("/players?days=1", base_url="http://localhost:8787").get_data(as_text=True)
        # последний день — E: у Eta 30, у остальных за него ничего нет
        eta = next(chunk for chunk in html.split("<tr class='player-row'>") if ">Eta<" in chunk)
        self.assertIn(">30</td>", eta)
        alpha = next(chunk for chunk in html.split("<tr class='player-row'>") if ">Alpha<" in chunk)
        self.assertIn(">—</td>", alpha)


if __name__ == "__main__":
    unittest.main()
