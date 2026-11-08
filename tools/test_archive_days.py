from __future__ import annotations

import os
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

D1, D2 = "2026-11-01", "2026-11-05"   # D2 — «пустой» день, его будем прятать
GUILD = 1


class ArchiveDayTests(unittest.TestCase):
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
        for pid, slot in ((self.alpha, 1), (self.beta, 2)):
            player_store.upsert_roster_member(self.db, GUILD, pid, squad_id=1, slot=slot)
        # D1: Alpha лучше, D2: Beta лучше → средний EFF зависит от того, учтён ли D2
        self.seed_day(D1, {self.alpha: (10, 1, 5, 900, 100), self.beta: (2, 5, 1, 100, 20)})
        self.seed_day(D2, {self.alpha: (1, 5, 0, 50, 5), self.beta: (9, 1, 4, 800, 90)})
        with closing(player_store.connect(self.db)) as con:  # runtime-снапшот на D2
            con.execute("""INSERT INTO grenade_session_stats(source_key,player_id,stat_key,value,match_date,guild_id)
                           VALUES('runtime-kv:1:2026-11-05',?,'20:00',7,?,?)""", (self.alpha, D2, GUILD))
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
            for row_no, (pid, (k, d, a, score, grenades)) in enumerate(rows.items(), 1):
                con.execute("""INSERT INTO scan_players(scan_id,place,nick,kills,deaths,assists,score,player_id)
                               VALUES(?,?,?,?,?,?,?,?)""", (scan_id, row_no, f"nick{pid}", k, d, a, score, pid))
                con.execute("""INSERT INTO kv_daily_players(report_id,row_no,player_id,raw_nick,voice_seconds,attended,total_grenades)
                               VALUES(?,?,?,?,600,1,?)""", (report_id, row_no, pid, f"nick{pid}", grenades))
                con.execute("""INSERT INTO kv_daily_grenade_stages(report_id,row_no,stage_no,grenades)
                               VALUES(?,?,1,?)""", (report_id, row_no, grenades))

    def get(self, path, **kw):
        return self.client.get(path, base_url=self.local, **kw)

    def post(self, path, **kw):
        return self.client.post(path, base_url=self.local, **kw)

    def counts(self):
        with closing(player_store.connect(self.db)) as con:
            return [con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                    for t in ("scans", "scan_players", "kv_daily_reports", "kv_daily_players",
                              "kv_daily_grenade_stages", "grenade_session_stats")]

    # ---------------------------------------------------------------------------------------
    def test_archive_hides_day_everywhere_and_restore_brings_it_back(self):
        self.assertIn(f"/day/{D2}", self.get("/").get_data(as_text=True))
        before = self.counts()

        r = self.post(f"/day/{D2}/archive")
        self.assertEqual(r.status_code, 303)
        self.assertIn(f"archived={D2}", r.headers["Location"])

        index = self.get("/").get_data(as_text=True)
        self.assertNotIn(f"href='/day/{D2}'", index)
        self.assertIn(f"href='/day/{D1}'", index)
        self.assertIn("<label>Дней КВ</label><b>1</b>", index)
        self.assertIn("Архив (1)", index)
        # данные не удалены — только спрятаны
        self.assertEqual(self.counts(), before)

        player_page = self.get(f"/player/{self.alpha}").get_data(as_text=True)
        self.assertNotIn(f"/day/{D2}", player_page)
        self.assertIn(f"/day/{D1}", player_page)

        archive = self.get("/archive").get_data(as_text=True)
        self.assertIn(f"/day/{D2}", archive)
        self.assertIn(f"/day/{D2}/unarchive", archive)

        r = self.post(f"/day/{D2}/unarchive")
        self.assertEqual(r.status_code, 303)
        self.assertIn("restored=1", r.headers["Location"])
        index = self.get("/").get_data(as_text=True)
        self.assertIn(f"href='/day/{D2}'", index)
        self.assertIn("<label>Дней КВ</label><b>2</b>", index)
        self.assertNotIn("Архив (", index)

    def test_players_totals_and_eff_ignore_archived_day(self):
        def alpha_row(html):
            line = next(l for l in html.split("<tr class='player-row'>") if "Alpha" in l)
            return line

        full = self.get("/players").get_data(as_text=True)
        self.assertIn("<td>2</td><td class='total'>105</td>", alpha_row(full))   # 100 + 5 гранат, 2 дня
        with closing(player_store.connect(self.db)) as con:
            eff_full = admin_ui.average_player_efficiencies(con)
        self.assertEqual((eff_full[self.alpha], eff_full[self.beta]), (50.0, 50.0))

        self.post(f"/day/{D2}/archive")
        part = self.get("/players").get_data(as_text=True)
        self.assertIn("<td>1</td><td class='total'>100</td>", alpha_row(part))
        with closing(player_store.connect(self.db)) as con:
            eff_part = admin_ui.average_player_efficiencies(con)
        self.assertEqual((eff_part[self.alpha], eff_part[self.beta]), (100.0, 0.0))

    def test_day_page_has_delete_button_or_restore_button(self):
        page = self.get(f"/day/{D2}").get_data(as_text=True)
        self.assertIn("Удалить день (в архив)", page)
        self.assertIn(f"action='/day/{D2}/archive'", page)
        self.assertNotIn("Вернуть из архива", page)
        self.assertNotIn("onsubmit=", page)               # подтверждение не вшито в JS-строку атрибута
        self.assertIn("data-confirm='Убрать день", page)

        self.post(f"/day/{D2}/archive")
        page = self.get(f"/day/{D2}").get_data(as_text=True)
        self.assertIn("Вернуть из архива", page)
        self.assertIn("День в архиве", page)
        self.assertNotIn("Удалить день (в архив)", page)
        self.assertEqual(self.get(f"/day/{D2}").status_code, 200)   # данные дня по-прежнему открываются

    def test_archive_is_idempotent_and_validates_date(self):
        self.assertEqual(self.post(f"/day/{D2}/archive").status_code, 303)
        self.assertEqual(self.post(f"/day/{D2}/archive").status_code, 303)
        with closing(player_store.connect(self.db)) as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM archived_days").fetchone()[0], 1)
        self.assertEqual(self.post("/day/не-дата/archive").status_code, 400)
        self.assertEqual(self.post("/day/не-дата/unarchive").status_code, 400)
        self.assertEqual(self.post(f"/day/{D1}/unarchive").status_code, 303)   # вернуть не архивный — не ошибка

    def test_archived_day_is_not_resurrected_by_startup_recovery(self):
        self.post(f"/day/{D2}/archive")
        self.assertEqual(player_store.recover_daily_reports(self.db), 0)
        self.assertNotIn(f"href='/day/{D2}'", self.get("/").get_data(as_text=True))

    def test_store_level_stats_skip_archived_days(self):
        self.assertEqual(player_store.list_kv_daily_dates(self.db), [D2, D1])
        self.assertEqual([r["date"] for r in player_store.list_stats_dates(self.db)], [D2, D1])
        self.assertEqual([r["match_date"] for r in player_store.list_scan_dates(self.db, GUILD)], [D2, D1])
        full = player_store.collect_kv_daily_stats(self.db)
        self.assertEqual(full["completed_dates"], {D1, D2})
        self.assertEqual(full["samples"][self.alpha]["grenades"], [105.0, 2.0])

        self.post(f"/day/{D2}/archive")
        self.assertEqual(player_store.list_kv_daily_dates(self.db), [D1])
        self.assertEqual([r["date"] for r in player_store.list_stats_dates(self.db)], [D1])
        self.assertEqual([r["match_date"] for r in player_store.list_scan_dates(self.db, GUILD)], [D1])
        part = player_store.collect_kv_daily_stats(self.db)
        self.assertEqual(part["completed_dates"], {D1})
        self.assertEqual(part["samples"][self.alpha]["grenades"], [100.0, 1.0])
        self.assertEqual(part["samples"][self.alpha]["kills"], [10.0, 1.0])
        ranged = player_store.collect_kv_daily_stats(self.db, date_from=D1, date_to=D2)
        self.assertEqual(ranged["completed_dates"], {D1})

    def test_archive_requires_login_when_password_is_set(self):
        os.environ["ADMIN_PASSWORD"] = "s3cret-pass"
        admin_ui._secret_cache = b"\x03" * 32
        try:
            r = self.client.post(f"/day/{D2}/archive", base_url="https://panel.example.com")
            self.assertEqual(r.status_code, 401)
            with closing(player_store.connect(self.db)) as con:
                self.assertEqual(con.execute("SELECT COUNT(*) FROM archived_days").fetchone()[0], 0)
        finally:
            admin_ui._secret_cache = None

    # ── внешний вид: полоски, кнопка «В архив», безопасные подтверждения ─────────────────────
    def test_overview_has_grenade_bar_and_row_level_archive_button(self):
        index = self.get("/").get_data(as_text=True)
        row_d1 = index.split(f"<td class='nick grow'><a href='/day/{D1}'", 1)[1].split("</tr>", 1)[0]
        row_d2 = index.split(f"<td class='nick grow'><a href='/day/{D2}'", 1)[1].split("</tr>", 1)[0]
        self.assertIn("<span class='meter'><i style='width:100.0%'></i></span><b>120</b>", row_d1)   # самый большой день
        self.assertIn("<b>95</b>", row_d2)
        self.assertNotIn("width:100.0%", row_d2)
        self.assertIn(f"action='/day/{D2}/archive'", row_d2)
        self.assertIn(">В архив</button>", row_d2)
        self.assertNotIn("confirm(", index.split("<main", 1)[1].split("<script>", 1)[0])
        self.assertEqual(self.post(f"/day/{D2}/archive").status_code, 303)
        self.assertNotIn(f"href='/day/{D2}'", self.get("/").get_data(as_text=True))

    def test_players_eff_bar_sits_right_of_the_number(self):
        def alpha_row(html):
            return next(l for l in html.split("<tr class='player-row'>") if "Alpha" in l)
        page = self.get("/players").get_data(as_text=True)
        self.assertIn("<td class='eff'><span class='effcell'><span class='n'>50.0</span>"
                      "<span class='meter'><i style='width:50.0%'></i></span></span></td>", alpha_row(page))
        self.post(f"/day/{D2}/archive")
        self.assertIn("<span class='n'>100.0</span><span class='meter'><i style='width:100.0%'>",
                      alpha_row(self.get("/players").get_data(as_text=True)))

    def test_delete_confirms_survive_apostrophes_in_nicks(self):
        with closing(player_store.connect(self.db)) as con:
            con.execute("UPDATE players SET canonical_nick=? WHERE id=?", ("O'Brien \"X\"", self.alpha))
        page = self.get(f"/day/{D2}").get_data(as_text=True)
        self.assertIn("data-confirm='Удалить гранаты и войс O&#x27;Brien &quot;X&quot;", page)
        self.assertNotIn("confirm('", page.split("<main", 1)[1].split("<script>", 1)[0])   # нет inline-confirm с ником
        self.assertIn("class='danger wide' name='__del_scan'", page)     # «✕ таб» не сжимается до 25px
        self.assertIn("f.requestSubmit(b)", page)                            # Ctrl+S
        self.assertIn("beforeunload", page)

    def test_navigation_order_and_squads_page_without_hint(self):
        index = self.get("/").get_data(as_text=True)
        self.assertIn("Игроки</a><a class='' href='/squads'>Отряды</a></nav>", index)
        self.assertNotIn("<!--navx-->", index)
        squads = self.get("/squads").get_data(as_text=True)
        self.assertNotIn("Нажмите на ячейку", squads)
        self.assertIn("keepalive:true", squads)
        os.environ["ADMIN_PASSWORD"] = "s3cret-pass"
        admin_ui._secret_cache = b"\x03" * 32
        try:
            self.client.post("/login", base_url=self.local, data={"password": "s3cret-pass"})
            page = self.client.get("/", base_url=self.local).get_data(as_text=True)
            self.assertIn("Выйти", page)
            self.assertLess(page.index("Отряды</a>"), page.index("Выйти"))
        finally:
            admin_ui._secret_cache = None
            os.environ.pop("ADMIN_PASSWORD", None)


if __name__ == "__main__":
    unittest.main()
