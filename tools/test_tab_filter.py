"""День КВ с привязанным табом: в статистику идут только игроки с таба."""
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

D1, D2, D3 = "2026-11-01", "2026-11-05", "2026-11-09"
GUILD = 1
GRENADES = {"Alpha": 10, "Beta": 20, "Gamma": 30, "Delta": 40}


class TabFilterTests(unittest.TestCase):
    """
    D1 — таб есть: Alpha, Beta (id в таблице) и «Gamma» строкой без привязки (id находится по нику).
         Delta в табе нет, хотя в отчёте дня она есть → считаться не должна.
    D2 — таба нет: считаются все четверо.
    D3 — таб есть, но ни один ник не опознан → фильтра нет (пустой таб не прячет состав).
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
        for i, nick in enumerate(GRENADES, 1):
            self.pid[nick] = player_store.upsert_binding(self.db, 100 * i, nick, guild_id=GUILD)
            player_store.upsert_roster_member(self.db, GUILD, self.pid[nick], squad_id=1, slot=i)
        self.scan_d1 = self.seed_scan(D1, [("Alpha", self.pid["Alpha"]), ("Beta", self.pid["Beta"]),
                                           ("Gamma", None), ("Qwerty", None)])
        self.seed_report(D1)
        self.seed_report(D2)
        self.scan_d3 = self.seed_scan(D3, [("Zzzz unknown", None)])
        self.seed_report(D3)
        with closing(player_store.connect(self.db)) as con:  # runtime-снапшот Delta на D1
            con.execute("""INSERT INTO grenade_session_stats(source_key,player_id,stat_key,value,match_date,guild_id)
                           VALUES('runtime-kv:1:d1',?,'20:00',7,?,?)""", (self.pid["Delta"], D1, GUILD))
        self.patch = patch.object(admin_ui, "DB_PATH", self.db)
        self.patch.start()
        self.client = admin_ui.app.test_client()
        self.local = "http://localhost:8787"

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()
        self.env.stop()

    # ── данные ──────────────────────────────────────────────────────────────
    def seed_scan(self, day, rows):
        with closing(player_store.connect(self.db)) as con:
            scan_id = con.execute(
                """INSERT INTO scans(stage,map,guild_id,channel_id,user_id,scanned_at,source_filename,match_date)
                   VALUES(NULL,'map',?,1,1,?,?,?)""", (GUILD, f"{day} 20:00:00", f"{day}.png", day)).lastrowid
            for place, (nick, pid) in enumerate(rows, 1):
                con.execute("""INSERT INTO scan_players(scan_id,place,nick,kills,deaths,assists,score,player_id)
                               VALUES(?,?,?,5,2,1,300,?)""", (scan_id, place, nick, pid))
        return scan_id

    def seed_report(self, day):
        with closing(player_store.connect(self.db)) as con:
            report_id = con.execute(
                """INSERT INTO kv_daily_reports(match_date,guild_id,source_path,source_sha256,stage_count)
                   VALUES(?,?,?,?,3)""", (day, GUILD, f"runtime:1:{day}", day)).lastrowid
            for row_no, (nick, grenades) in enumerate(GRENADES.items(), 1):
                con.execute("""INSERT INTO kv_daily_players(report_id,row_no,player_id,raw_nick,voice_seconds,attended,total_grenades)
                               VALUES(?,?,?,?,600,1,?)""", (report_id, row_no, self.pid[nick], nick, grenades))
                con.execute("""INSERT INTO kv_daily_grenade_stages(report_id,row_no,stage_no,grenades)
                               VALUES(?,?,1,?)""", (report_id, row_no, grenades))

    def get(self, path):
        return self.client.get(path, base_url=self.local)

    def post(self, path, **kw):
        return self.client.post(path, base_url=self.local, **kw)

    def count(self, table):
        with closing(player_store.connect(self.db)) as con:
            return con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def split_page(self, day):
        """(основная часть страницы дня, список «Не в табе» или '')."""
        page = self.get(f"/day/{day}").get_data(as_text=True)
        head, sep, tail = page.partition("<details class='skipped'>")
        return head, (sep + tail if sep else "")

    @staticmethod
    def player_row(page, nick):
        return next(chunk for chunk in page.split("<tr class='player-row'>") if nick in chunk)

    # ── ядро ────────────────────────────────────────────────────────────────
    def test_roster_by_day_resolves_unlinked_rows_and_skips_empty_tabs(self):
        with closing(player_store.connect(self.db)) as con:
            roster = player_store.tab_roster_by_day(con)
        self.assertEqual(roster, {D1: {self.pid["Alpha"], self.pid["Beta"], self.pid["Gamma"]}})
        self.assertNotIn(D3, roster)   # ни один ник не опознан — дня в списке нет
        self.assertNotIn(D2, roster)   # таба нет

    # ── страница дня ────────────────────────────────────────────────────────
    def test_day_page_lists_only_tab_players_and_keeps_others_in_collapsed_block(self):
        main, skipped = self.split_page(D1)
        for nick in ("Alpha", "Beta", "Gamma"):
            self.assertIn(nick, main)
        self.assertNotIn("Delta", main)                       # ника нет ни в таблице, ни в итогах
        self.assertIn("Не в табе (1)", skipped)
        self.assertIn("Delta", skipped)
        self.assertIn("<span class='chip'>Игроков: 3</span>", main)
        self.assertIn("Не в табе: 1", main)
        self.assertIn("<span class='chip'>Гранат: 60</span>", main)   # 10+20+30, без 40 у Delta

    def test_day_without_tab_or_with_unrecognized_tab_shows_everyone(self):
        for day in (D2, D3):
            main, skipped = self.split_page(day)
            self.assertEqual(skipped, "", day)
            self.assertIn("Игроков: 4", main)
            self.assertIn("<span class='chip'>Гранат: 100</span>", main)

    def test_unmatched_tab_nick_is_flagged_and_alias_brings_player_back(self):
        _, skipped = self.split_page(D1)
        self.assertIn("1 ников не найдено", skipped)           # «Qwerty» в табе не опознан
        player_store.add_alias(self.db, "Delta", "Qwerty")
        main, skipped = self.split_page(D1)
        self.assertEqual(skipped, "")
        self.assertIn("Delta", main)
        self.assertIn("<span class='chip'>Гранат: 100</span>", main)

    def test_partial_tabs_warning(self):
        _, skipped = self.split_page(D1)
        self.assertIn("Загружено табов: 1 из 3", skipped)

    # ── итоги ───────────────────────────────────────────────────────────────
    def test_overview_totals_exclude_non_tab_players(self):
        index = self.get("/").get_data(as_text=True)

        def row(day):
            return re.search(rf"<td class='nick grow'><a href='/day/{day}'>.*?</tr>", index, re.S).group(0)

        self.assertIn("<b>60</b>", row(D1))
        self.assertIn("<b>100</b>", row(D2))
        self.assertIn("<b>100</b>", row(D3))
        self.assertIn("<td class='hide-sm'>3</td>", row(D1))   # игроков с табом: 3, не 4

    # ── «Игроков» в списке дней = сколько всего на табе (как на странице дня) ──────────────────────
    def add_tab_only_player(self, nick="Epsilon", day=D1):
        """Игрок есть на табе дня, но в отчёте гранат/войса его нет («только таб»)."""
        pid = player_store.upsert_binding(self.db, 900, nick, guild_id=GUILD)
        with closing(player_store.connect(self.db)) as con:
            con.execute("""INSERT INTO scan_players(scan_id,place,nick,kills,deaths,assists,score,player_id)
                           VALUES(?,9,?,5,2,1,300,?)""", (self.scan_d1, nick, pid))
        return pid

    @staticmethod
    def day_row(page, day, cell="td class='nick grow'"):
        return re.search(rf"<{cell}><a href='/day/{day}'>.*?</tr>", page, re.S).group(0)

    @staticmethod
    def cells(row):
        return re.findall(r"<td class='hide-sm'>(.*?)</td>", row)

    def test_overview_players_counts_tab_only_player_like_the_day_page(self):
        self.add_tab_only_player()
        day = self.get(f"/day/{D1}").get_data(as_text=True)
        self.assertIn("<span class='chip'>Игроков: 4</span>", day)          # 3 из отчёта + 1 «только таб»
        self.assertIn("только таб", day)
        row = self.day_row(self.get("/").get_data(as_text=True), D1)
        self.assertEqual(self.cells(row)[:2], ["1", "4"])                     # табов 1, игроков 4 — то же число, что на странице дня

    def test_overview_players_without_tab_or_with_unrecognized_tab_is_scanned_count(self):
        index = self.get("/").get_data(as_text=True)
        self.assertEqual(self.cells(self.day_row(index, D2))[:2], ["0", "4"])  # табов нет → сколько просканировано
        self.assertEqual(self.cells(self.day_row(index, D3))[:2], ["1", "4"])  # таб есть, но ники не опознаны → тоже по отчёту
        for day in (D2, D3):
            self.assertIn("<span class='chip'>Игроков: 4</span>", self.get(f"/day/{day}").get_data(as_text=True))

    def test_overview_players_for_day_with_tab_but_no_report_is_tab_size(self):
        d4 = "2026-11-12"
        scan = self.seed_scan(d4, [("Alpha", self.pid["Alpha"]), ("Beta", self.pid["Beta"])])   # отчёта гранат/войса за день нет
        row = self.day_row(self.get("/").get_data(as_text=True), d4)
        self.assertEqual(self.cells(row)[:2], ["1", "2"])
        self.assertIn("<span class='chip'>Игроков: 2</span>", self.get(f"/day/{d4}").get_data(as_text=True))

    def test_overview_players_count_is_distinct_players_not_tab_rows(self):
        self.seed_scan(D1, [("Alpha", self.pid["Alpha"]), ("Beta", self.pid["Beta"])])         # второй таб того же дня, те же игроки
        row = self.day_row(self.get("/").get_data(as_text=True), D1)
        self.assertEqual(self.cells(row)[:2], ["2", "3"])                     # табов 2, а игроков по-прежнему 3

    def test_overview_totals_do_not_change_when_a_tab_only_player_appears(self):
        before = self.day_row(self.get("/").get_data(as_text=True), D1)
        self.add_tab_only_player()
        after = self.day_row(self.get("/").get_data(as_text=True), D1)
        self.assertIn("<b>60</b>", after)                                     # гранаты те же: у «только таб» их нет
        self.assertEqual(re.search(r"<b>60</b>", before).group(0), re.search(r"<b>60</b>", after).group(0))

    def test_archive_players_counts_tab_only_player_too(self):
        self.add_tab_only_player()
        self.post(f"/day/{D1}/archive")
        archive = self.get("/archive").get_data(as_text=True)
        row = self.day_row(archive, D1, cell="td class='nick'")
        self.assertEqual(re.findall(r"<td>(\d+)</td>", row)[:2], ["1", "4"])

    def test_players_page_counts_days_and_grenades_only_when_player_was_on_tab(self):
        page = self.get("/players").get_data(as_text=True)
        # Alpha: 3 дня, 10+10+10 → в среднем 10 | Delta: D1 не считается → 2 дня, 40+40 → в среднем 40
        self.assertIn("<td>3</td><td class='gavg total'", self.player_row(page, "Alpha"))
        self.assertIn(">10</td>", self.player_row(page, "Alpha"))
        self.assertIn("<td>2</td><td class='gavg total'", self.player_row(page, "Delta"))
        self.assertIn(">40</td>", self.player_row(page, "Delta"))

    def test_player_page_shows_day_where_player_was_not_on_tab_as_absence(self):
        def day_row(page, day):
            return re.search(rf"<tr[^>]*><td class='nick grow'><a href='/day/{day}'>.*?</tr>", page, re.S).group(0)

        delta = self.get(f"/player/{self.pid['Delta']}").get_data(as_text=True)
        row = day_row(delta, D1)
        self.assertIn("Не играл", row)                         # день виден: пришёл (есть в отчёте), но на табе его нет
        self.assertNotIn("Неявка", row)
        self.assertNotIn("<b>40</b>", row)                     # гранаты из отчёта за этот день не засчитываются
        self.assertNotIn("20:00=7", delta)                     # runtime-снапшот этого дня тоже спрятан
        self.assertNotIn("Неявка", day_row(delta, D2))         # D2 без таба — считается по отчёту
        alpha = self.get(f"/player/{self.pid['Alpha']}").get_data(as_text=True)
        self.assertNotIn("Неявка", day_row(alpha, D1))

    def test_collect_kv_daily_stats_ignores_non_tab_players(self):
        stats = player_store.collect_kv_daily_stats(self.db)["samples"]
        delta, alpha = stats[self.pid["Delta"]], stats[self.pid["Alpha"]]
        self.assertEqual(delta["grenades"], [80.0, 2.0])       # D2 + D3, без D1
        self.assertEqual(delta["voice_seconds"], [1200.0, 2.0])
        self.assertEqual(alpha["grenades"], [30.0, 3.0])
        self.assertIn(D1, player_store.collect_kv_daily_stats(self.db)["completed_dates"])

    # ── данные не теряются ──────────────────────────────────────────────────
    def test_nothing_is_deleted_and_day_save_does_not_touch_hidden_rows(self):
        players_before = self.count("kv_daily_players")
        stages_before = self.count("kv_daily_grenade_stages")
        self.get(f"/day/{D1}")
        r = self.post(f"/day/{D1}/save", data={"mode": "total", "w1": "11"})   # правим Alpha (row 1)
        self.assertEqual(r.status_code, 303)
        self.assertEqual(self.count("kv_daily_players"), players_before)
        # панель сама стирает этапы правленой строки (Alpha); у остальных, в том числе скрытых, они на месте
        self.assertEqual(self.count("kv_daily_grenade_stages"), stages_before - 1)
        with closing(player_store.connect(self.db)) as con:
            rows = {r["raw_nick"]: r["total_grenades"] for r in con.execute(
                "SELECT raw_nick,total_grenades FROM kv_daily_players kp "
                "JOIN kv_daily_reports r ON r.id=kp.report_id WHERE r.match_date=?", (D1,))}
        self.assertEqual(rows, {"Alpha": 11, "Beta": 20, "Gamma": 30, "Delta": 40})

    def test_deleting_the_tab_brings_everyone_back(self):
        self.post(f"/day/{D1}/save", data={"mode": "total", "__del_scan": str(self.scan_d1)})
        main, skipped = self.split_page(D1)
        self.assertEqual(skipped, "")
        self.assertIn("Игроков: 4", main)
        self.assertIn("<span class='chip'>Гранат: 100</span>", main)
        with closing(player_store.connect(self.db)) as con:
            self.assertNotIn(D1, player_store.tab_roster_by_day(con))

    def test_archive_page_uses_same_filter(self):
        self.post(f"/day/{D1}/archive")
        archive = self.get("/archive").get_data(as_text=True)
        row = re.search(rf"<td class='nick'><a href='/day/{D1}'>.*?</tr>", archive, re.S).group(0)
        self.assertIn("<td class='total'>60</td>", row)


if __name__ == "__main__":
    unittest.main()
