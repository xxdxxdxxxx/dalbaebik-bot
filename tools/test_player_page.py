"""Страница игрока /player/<id>: дни из «Обзора», гранаты только за день, неявка видна."""
from __future__ import annotations

import os
import re
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import player_store  # noqa: E402
from tools import admin_ui  # noqa: E402

GUILD = 1
D_OLD = "2026-10-18"   # Alpha ещё не было в клане
D1 = "2026-11-01"      # Alpha на табе, есть отчёт с этапами
D2 = "2026-11-05"      # Alpha на двух табах, явку не отметил
D3 = "2026-11-13"      # таб есть, Alpha на нём нет, но в отчёте у него цифры
D4 = "2026-11-15"      # таб есть, Alpha на нём нет, в отчёте его вообще нет
D5 = "2026-11-16"      # архивный день — не показывать
D6 = "2026-11-17"      # таба нет, Alpha в отчёте
D7 = "2026-11-18"      # таба нет, Alpha нет и в отчёте


class PlayerPageTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {}, clear=False)
        self.env.start()
        for key in ("ADMIN_PASSWORD", "ADMIN_TRUST_PROXY"):
            os.environ.pop(key, None)
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "scan_stats.sqlite3"
        player_store.ensure_schema(self.db)
        player_store.upsert_guild_config(self.db, GUILD)
        self.alpha = player_store.upsert_binding(self.db, 100, "Alpha", guild_id=GUILD)
        self.beta = player_store.upsert_binding(self.db, 200, "Beta", guild_id=GUILD)
        self.gamma = player_store.upsert_binding(self.db, 300, "Gamma", guild_id=GUILD)
        a, b, g = self.alpha, self.beta, self.gamma
        # (игрок, убийства, смерти, помощь, счёт)
        self.seed_tab(D_OLD, [(b, 1, 1, 1, 100)])
        self.seed_tab(D1, [(a, 7, 3, 2, 900), (b, 2, 5, 1, 100), (g, 4, 4, 4, 300)])
        self.seed_tab(D2, [(a, 5, 1, 0, 500), (b, 1, 1, 1, 100)])
        self.seed_tab(D2, [(a, 4, 2, 1, 400), (b, 1, 1, 1, 100)])
        self.seed_tab(D3, [(b, 3, 3, 3, 300)])
        self.seed_tab(D4, [(b, 3, 3, 3, 300)])
        self.seed_tab(D5, [(b, 3, 3, 3, 300)])
        # (игрок, гранаты, войс, явка, {этап: гранаты}); у Gamma отчёта нет — «только таб»
        self.seed_report(D_OLD, [(b, 5, 60, 1, {1: 5})])
        self.seed_report(D1, [(a, 12, 600, 1, {1: 5, 2: 7}), (b, 3, 60, 1, {1: 3})])
        self.seed_report(D2, [(a, 20, 1250, 0, {1: 20}), (b, 3, 60, 1, {1: 3})])
        self.seed_report(D3, [(a, 7, 300, 0, {1: 7}), (b, 3, 60, 1, {1: 3})])
        self.seed_report(D4, [(b, 3, 60, 1, {1: 3})])
        self.seed_report(D5, [(a, 99, 60, 1, {1: 99})])
        self.seed_report(D6, [(a, 30, 900, None, {1: 10, 2: 20})])
        self.seed_report(D7, [(b, 4, 60, None, {1: 4})])
        with closing(player_store.connect(self.db)) as con:
            con.execute("INSERT INTO archived_days(match_date) VALUES(?)", (D5,))
        self.patch = patch.object(admin_ui, "DB_PATH", self.db)
        self.patch.start()
        self.client = admin_ui.app.test_client()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()
        self.env.stop()

    # ── данные ────────────────────────────────────────────────────────────
    def seed_tab(self, day, rows, nick_override=None):
        with closing(player_store.connect(self.db)) as con:
            scan_id = con.execute(
                """INSERT INTO scans(stage,map,guild_id,channel_id,user_id,scanned_at,source_filename,match_date)
                   VALUES(NULL,'map',?,1,1,?,?,?)""",
                (GUILD, f"{day} 20:00:00", f"{day}-{os.urandom(3).hex()}.png", day)).lastrowid
            for place, (pid, k, d, a, score) in enumerate(rows, 1):
                con.execute("""INSERT INTO scan_players(scan_id,place,nick,kills,deaths,assists,score,player_id)
                               VALUES(?,?,?,?,?,?,?,?)""", (scan_id, place, nick_override or f"nick{pid}", k, d, a, score, pid))
        return scan_id

    def seed_report(self, day, rows):
        with closing(player_store.connect(self.db)) as con:
            report_id = con.execute(
                """INSERT INTO kv_daily_reports(match_date,guild_id,source_path,source_sha256,stage_count)
                   VALUES(?,?,?,?,3)""", (day, GUILD, f"runtime:1:{day}", day)).lastrowid
            for row_no, (pid, total, voice, attended, stages) in enumerate(rows, 1):
                con.execute("""INSERT INTO kv_daily_players(report_id,row_no,player_id,raw_nick,voice_seconds,attended,total_grenades)
                               VALUES(?,?,?,?,?,?,?)""", (report_id, row_no, pid, f"nick{pid}", voice, attended, total))
                for stage_no, grenades in stages.items():
                    con.execute("""INSERT INTO kv_daily_grenade_stages(report_id,row_no,stage_no,grenades)
                                   VALUES(?,?,?,?)""", (report_id, row_no, stage_no, grenades))

    def page(self, pid=None):
        r = self.client.get(f"/player/{pid or self.alpha}", base_url="http://localhost:8787")
        self.assertEqual(r.status_code, 200)
        return r.get_data(as_text=True)

    @staticmethod
    def row(html, day):
        return re.search(rf"<tr[^>]*><td class='nick grow'><a href='/day/{day}'>.*?</tr>", html, re.S).group(0)

    # ── какие дни показываем ─────────────────────────────────────────────────
    def test_only_main_days_and_not_before_player_joined(self):
        html = self.page()
        self.assertNotIn(f"/day/{D5}", html)      # архивный день спрятан, хотя у Alpha там 99 гранат
        self.assertNotIn("<b>99</b>", html)
        self.assertNotIn(f"/day/{D_OLD}", html)   # до первого появления игрока дней нет
        for day in (D1, D2, D3, D4, D6, D7):
            self.assertIn(f"href='/day/{day}'", html)
        order = [m.group(1) for m in re.finditer(r"<td class='nick grow'><a href='/day/([\d-]+)'>", html)]
        self.assertEqual(order, [D7, D6, D4, D3, D2, D1])      # новые сверху

    # ── неявка ──────────────────────────────────────────────────────────────
    def test_day_with_tab_but_player_not_on_it_is_shown_as_absence(self):
        html = self.page()
        for day in (D3, D4):
            row = self.row(html, day)
            self.assertIn("class='absent'", row)
            self.assertIn("Неявка", row)
        for day in (D1, D2, D6):
            self.assertNotIn("Неявка", self.row(html, day))

    def test_absence_hints_when_report_has_numbers_but_player_missing_on_tab(self):
        html = self.page()
        self.assertIn("в отчёте: 7 гран., войс 5:00", self.row(html, D3))
        self.assertNotIn("в отчёте", self.row(html, D4))
        self.assertNotIn("<b>7</b>", self.row(html, D3))       # цифры отчёта не засчитываются как игра

    def test_day_without_tab_is_played_by_report_else_no_data(self):
        html = self.page()
        d6, d7 = self.row(html, D6), self.row(html, D7)
        self.assertIn("<b>30</b>", d6)
        self.assertNotIn("Неявка", d6)
        self.assertIn("class='nodata'", d7)
        self.assertIn("нет данных", d7)
        self.assertNotIn("Неявка", d7)

    def test_unbound_tab_row_counts_as_presence_by_nick(self):
        self.seed_tab(D4, [(None, 6, 2, 1, 700)], nick_override="Alpha")
        row = self.row(self.page(), D4)
        self.assertNotIn("Неявка", row)
        self.assertIn("6/2/1", row)

    # ── что показываем в дне ─────────────────────────────────────────────────────
    def test_grenades_are_day_total_only_without_stage_columns(self):
        html = self.page()
        self.assertNotIn("class='gcol", html)          # колонки этапов (в общем CSS класс остался — это не разметка)
        self.assertNotIn("data-sort", html)
        self.assertNotIn("по этапам", html)
        row = self.row(html, D1)
        self.assertIn("<b>12</b>", row)              # 5 + 7 одним числом
        self.assertNotRegex(row, r"<td[^>]*>\s*(5|7)\s*</td>")

    def test_played_row_values(self):
        html = self.page()
        d1, d2 = self.row(html, D1), self.row(html, D2)
        self.assertIn("10:00", d1)
        self.assertIn("7/3/2", d1)
        self.assertIn("<td>900</td>", d1)
        self.assertIn("9/3/1<span class='sub'>×2</span>", d2)     # два таба за день — суммой
        self.assertIn("<td>900</td>", d2)
        self.assertIn("5/1/0 · 500 | 4/2/1 · 400", d2)

    # ── явка ────────────────────────────────────────────────────────────────
    def test_player_on_tab_has_attendance_even_when_report_says_no_or_has_no_voice(self):
        html = self.page()
        d2 = self.row(html, D2)                           # на табе; в отчёте «не отметился»
        self.assertIn("class='yes'", d2)
        self.assertNotIn("class='no'", d2)
        g1 = self.row(self.page(self.gamma), D1)          # на табе, а в отчёте (войс/гранаты) его вообще нет
        self.assertIn("class='yes'", g1)
        self.assertNotIn("Неявка", g1)

    def test_day_without_tab_falls_back_to_report_attendance_flag(self):
        self.assertIn("class='na' title='Таба у дня нет", self.row(self.page(), D6))      # отметки нет — прочерк
        with closing(player_store.connect(self.db)) as con:
            con.execute("""UPDATE kv_daily_players SET attended=0 WHERE player_id=? AND report_id IN
                           (SELECT id FROM kv_daily_reports WHERE match_date=?)""", (self.alpha, D6))
        self.assertIn("class='no'", self.row(self.page(), D6))
        with closing(player_store.connect(self.db)) as con:
            con.execute("""UPDATE kv_daily_players SET attended=1 WHERE player_id=? AND report_id IN
                           (SELECT id FROM kv_daily_reports WHERE match_date=?)""", (self.alpha, D6))
        self.assertIn("class='yes'", self.row(self.page(), D6))

    def test_player_on_tab_but_not_in_report_is_played_without_numbers(self):
        html = self.page(self.gamma)
        d1 = self.row(html, D1)
        self.assertIn("4/4/4", d1)
        self.assertIn("<td class='na'>—</td>", d1)                # гранат нет — просто прочерк
        self.assertIn("Неявка", self.row(html, D2))               # дальше на табах его уже нет
        self.assertNotIn(f"/day/{D_OLD}", html)

    # ── сводка и статы ───────────────────────────────────────────────────────────
    def test_summary_tiles(self):
        html = self.page()
        self.assertIn("<label>Явка</label><b>3<span class='of'>из 5 · 60%</span></b>", html)
        self.assertIn("<label>Гранат</label><b>62</b>", html)                 # 12 + 20 + 30, отчёт неявки не в счёт
        self.assertIn("<label>Ср. гранат за КВ</label><b>21</b>", html)       # (12 + 20 + 30) / 3
        self.assertIn("<label>Ср. гранат за таб</label><b>10.7</b>", html)    # (12 + 20) / 3 таба; D6 без таба не в счёт
        self.assertNotIn("Рекорд", html)
        self.assertIn("<label>KDA</label><b>3.17<span class='of'>16/6/3</span></b>", html)   # (16+3)/6 по табам D1, D2
        self.assertIn("<label>Счёт за таб</label><b>600</b>", html)           # (900+500+400)/3
        self.assertIn("<label>Войс</label><b>45:50<span class='of'>15:16 за КВ</span></b>", html)

    def test_eff_per_day_and_average(self):
        html = self.page()
        self.assertIn("<label>EFF</label><b>100.0<span class='of'>из 100</span></b>", html)
        self.assertIn("<span class='effcell'><span class='n'>100.0</span>", self.row(html, D1))
        self.assertNotIn("effcell", self.row(html, D6))                        # у дня без таба EFF нет
        # рефакторинг не меняет средний EFF в «Игроках»
        with closing(player_store.connect(self.db)) as con:
            self.assertEqual(admin_ui.average_player_efficiencies(con)[self.alpha], 100.0)
            self.assertEqual(admin_ui.day_efficiencies(con)[D1][self.alpha], 100.0)

    def test_header_shows_squad_discord_and_other_nicks(self):
        player_store.upsert_roster_member(self.db, GUILD, self.alpha, squad_id=2, slot=1)
        with closing(player_store.connect(self.db)) as con:
            con.execute("UPDATE discord_bindings SET discord_username='alpha_ds' WHERE player_id=?", (self.alpha,))
            con.execute("INSERT INTO player_aliases(alias, player_id) VALUES('OldAlpha', ?)", (self.alpha,))
        html = self.page()
        self.assertIn("<span>Отряд 2</span>", html)
        self.assertIn("Discord: alpha_ds", html)
        self.assertIn("Также: OldAlpha", html)

    def test_voice_join_column_only_when_there_is_data(self):
        self.assertNotIn("<th title='Во сколько зашёл в войс явки'>", self.page())
        with closing(player_store.connect(self.db)) as con:
            con.executemany("INSERT INTO voice_join_times(guild_id,match_date,player_id,joined_at) VALUES(?,?,?,?)",
                            [(GUILD, D1, self.alpha, f"{D1}T19:39:12+03:00"),
                             (GUILD, D3, self.alpha, f"{D3}T19:41:00+03:00")])   # D3 — неявка, но в войсе был
        html = self.page()
        self.assertIn("<th title='Во сколько зашёл в войс явки'>Зашёл</th>", html)
        self.assertIn("<td class='vj' title='19:39:12 МСК'>19:39</td>", self.row(html, D1))
        self.assertIn("в отчёте: 7 гран., войс 5:00 · в войсе с 19:41", self.row(html, D3))   # возможно, ник на табе не опознан

    def test_gear_chips_show_only_filled_slots(self):
        self.assertNotIn("class='gear'", self.page())
        with closing(player_store.connect(self.db)) as con:
            con.execute("""CREATE TABLE IF NOT EXISTS player_gear(player_id INTEGER NOT NULL, slot TEXT NOT NULL,
                           item TEXT NOT NULL DEFAULT '', level INTEGER NOT NULL DEFAULT 0, text TEXT NOT NULL DEFAULT '',
                           updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(player_id, slot))""")
            con.executemany("INSERT INTO player_gear(player_id,slot,item,level,text) VALUES(?,?,?,?,?)",
                            [(self.alpha, "gauss", "gauss", 15, ""), (self.alpha, "hammer", "hammer", 0, ""),
                             (self.alpha, "bio", "none", 0, ""), (self.alpha, "speed", "", 0, "39")])
        html = self.page()
        self.assertIn("class='gear'", html)
        self.assertIn("Гаусс<span class='lv'>+15</span>", html)
        self.assertIn("/gear/icon/gauss.webp", html)
        self.assertIn("Кувалда", html)
        self.assertIn("<span class='gs'>Скорость</span>39", html)
        self.assertNotIn("Штурма", html)                                        # «предмета нет» и неуказанные слоты не показываем

    # ── убрано лишнее ────────────────────────────────────────────────────────────
    def test_no_snapshots_and_no_per_tab_blocks_but_snapshot_day_still_counts_as_played(self):
        d8 = "2026-11-19"                      # таба и отчёта нет, есть только живой счётчик гранат
        with closing(player_store.connect(self.db)) as con:
            con.executemany("""INSERT INTO grenade_session_stats(source_key,player_id,stat_key,value,match_date,guild_id)
                               VALUES(?,?,?,?,?,?)""",
                            [(f"runtime-kv:{D6}", self.alpha, "20:00", 7, D6, GUILD),
                             (f"runtime-kv:{D6}", self.alpha, "21:20", 17, D6, GUILD),
                             (f"runtime-kv:{d8}", self.alpha, "20:00", 3, d8, GUILD)])
        html = self.page()
        for gone in ("Снапшоты", "Табы по отдельности", "<details", "21:20=17"):
            self.assertNotIn(gone, html)
        row = self.row(html, d8)
        self.assertNotIn("Неявка", row)
        self.assertNotIn("class='nodata'", row)

    def test_player_without_any_days_and_unknown_player(self):
        nobody = player_store.upsert_binding(self.db, 400, "Nobody", guild_id=GUILD)
        html = self.page(nobody)
        self.assertIn("По этому игроку пока нет дней КВ.", html)
        self.assertNotIn("<table", html)
        r = self.client.get("/player/99999", base_url="http://localhost:8787")
        self.assertEqual(r.status_code, 404)

    def test_restoring_archived_day_brings_it_back_as_absence(self):
        self.client.post(f"/day/{D5}/unarchive", base_url="http://localhost:8787")
        html = self.page()
        row = self.row(html, D5)
        self.assertIn("Неявка", row)                  # таб дня есть, Alpha на нём нет
        self.assertNotIn("<b>99</b>", row)            # 99 гранат из отчёта в игру не засчитываются


if __name__ == "__main__":
    unittest.main()
