"""Регрессии по аудиту: войс «не отсканирован», сброс гранат, кривые даты, запросы с чужих сайтов,
неопределённые имена в коде."""
from __future__ import annotations

import os
import re
import sys
import tempfile
import unittest
from contextlib import closing
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import player_store
from tools import admin_ui

DAY = "2026-11-14"
GUILD = 1


class _Inputs(HTMLParser):
    """Поля формы дня так, как их отправит браузер (без чекбоксов)."""

    def __init__(self):
        super().__init__()
        self.fields: list[tuple[str, str]] = []
        self.in_form = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "form" and "day-edit" in (a.get("class") or ""):
            self.in_form = True
        if tag == "input" and self.in_form and a.get("name") and a.get("type") != "checkbox":
            self.fields.append((a["name"], a.get("value") or ""))

    def handle_endtag(self, tag):
        if tag == "form":
            self.in_form = False


class AdminFixTests(unittest.TestCase):
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
        for slot, (nick, discord_id) in enumerate((("Alpha", 100), ("Beta", 200), ("Gamma", 300), ("Delta", 400)), 1):
            pid = player_store.upsert_binding(self.db, discord_id, nick, guild_id=GUILD)
            player_store.upsert_roster_member(self.db, GUILD, pid, squad_id=1, slot=slot)
            self.ids[nick] = pid
        # (граната, войс): Alpha — ошибочный 0 и войс не отсканирован; Beta — ничего не отсканировано;
        # Gamma — 30 гранат и час войса; Delta — настоящий нулевой войс
        data = {"Alpha": (0, None), "Beta": (None, None), "Gamma": (30, 3600), "Delta": (5, 0)}
        with closing(player_store.connect(self.db)) as con:
            scan_id = con.execute(
                """INSERT INTO scans(stage,map,guild_id,channel_id,user_id,scanned_at,source_filename,match_date)
                   VALUES(1,'map',?,1,1,?,?,?)""", (GUILD, f"{DAY} 20:00:00", "a.png", DAY)).lastrowid
            report_id = con.execute(
                """INSERT INTO kv_daily_reports(match_date,guild_id,source_path,source_sha256,stage_count)
                   VALUES(?,?,?,?,3)""", (DAY, GUILD, "runtime:1", "x")).lastrowid
            for row_no, (nick, (grenades, voice)) in enumerate(data.items(), 1):
                pid = self.ids[nick]
                con.execute("""INSERT INTO scan_players(scan_id,place,nick,kills,deaths,assists,score,player_id)
                               VALUES(?,?,?,?,?,?,?,?)""", (scan_id, row_no, nick, 5, 2, 1, 1000, pid))
                con.execute("""INSERT INTO kv_daily_players(report_id,row_no,player_id,raw_nick,total_grenades,voice_seconds,attended)
                               VALUES(?,?,?,?,?,?,1)""", (report_id, row_no, pid, nick, grenades, voice))
                if grenades is not None:
                    con.execute("""INSERT INTO kv_daily_grenade_stages(report_id,row_no,stage_no,grenades)
                                   VALUES(?,?,1,?)""", (report_id, row_no, grenades))
            con.commit()
        self.patch = patch.object(admin_ui, "DB_PATH", self.db)
        self.patch.start()
        self.client = admin_ui.app.test_client()
        self.local = "http://localhost:8787"

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()
        self.env.stop()

    # ------------------------------------------------------------------ помощники
    def get(self, path, **kw):
        return self.client.get(path, base_url=self.local, **kw)

    def post(self, path, **kw):
        return self.client.post(path, base_url=self.local, **kw)

    def form(self):
        parser = _Inputs()
        parser.feed(self.get(f"/day/{DAY}").get_data(as_text=True))
        return dict(parser.fields)

    def row(self, nick):
        with closing(player_store.connect(self.db)) as con:
            r = con.execute("""SELECT total_grenades g, voice_seconds v FROM kv_daily_players WHERE player_id=?""",
                            (self.ids[nick],)).fetchone()
            stages = con.execute("""SELECT COUNT(*) FROM kv_daily_grenade_stages s JOIN kv_daily_players p
                                    ON p.report_id=s.report_id AND p.row_no=s.row_no WHERE p.player_id=?""",
                                 (self.ids[nick],)).fetchone()[0]
        return {"g": r["g"], "v": r["v"], "stages": stages}

    def archived(self):
        with closing(player_store.connect(self.db)) as con:
            return con.execute("SELECT COUNT(*) FROM archived_days").fetchone()[0]

    # ------------------------------------------------------------------ 1. войс
    def test_not_scanned_voice_is_empty_in_the_form_and_stays_null_after_saving(self):
        form = self.form()
        voice = {nick: form[f"v{i}"] for i, nick in enumerate(("Alpha", "Beta", "Gamma", "Delta"), 1)}
        self.assertEqual(voice["Alpha"], "")                 # раньше было «0:00»
        self.assertEqual(voice["Beta"], "")
        self.assertEqual(voice["Gamma"], "1:00:00")
        self.assertEqual(voice["Delta"], "0:00")             # настоящий ноль остаётся нулём
        self.assertEqual(self.post(f"/day/{DAY}/save", data=form).status_code, 303)
        self.assertEqual([self.row(n)["v"] for n in ("Alpha", "Beta", "Gamma", "Delta")], [None, None, 3600, 0])

    def test_skipped_table_shows_dash_for_not_scanned_values(self):
        with closing(player_store.connect(self.db)) as con:       # игрок есть в отчёте, но не на табе
            pid = player_store.upsert_binding(self.db, 500, "Echo", guild_id=GUILD)
            rid = con.execute("SELECT id FROM kv_daily_reports").fetchone()[0]
            con.execute("""INSERT INTO kv_daily_players(report_id,row_no,player_id,raw_nick,total_grenades,voice_seconds)
                           VALUES(?,?,?,?,NULL,NULL)""", (rid, 9, pid, "Echo"))
            con.commit()
        html = self.get(f"/day/{DAY}").get_data(as_text=True)
        echo = html.split("Echo")[-1].split("</tr>")[0]
        self.assertIn("<td>—</td><td>—</td>", echo)

    # ------------------------------------------------------------------ 3. гранаты
    def test_clearing_the_grenade_field_resets_a_wrong_zero_to_not_scanned(self):
        form = self.form()
        self.assertEqual(form["w1"], "0")
        self.assertEqual(self.row("Alpha"), {"g": 0, "v": None, "stages": 1})
        form["w1"] = ""
        self.post(f"/day/{DAY}/save", data=form)
        self.assertEqual(self.row("Alpha"), {"g": None, "v": None, "stages": 0})   # не отсканировано, этапы убраны
        self.assertEqual(self.row("Gamma")["g"], 30)                              # остальных не задело
        self.assertEqual(self.row("Beta")["g"], None)

    def test_saving_without_edits_keeps_every_grenade_value(self):
        self.post(f"/day/{DAY}/save", data=self.form())
        self.assertEqual([self.row(n)["g"] for n in ("Alpha", "Beta", "Gamma", "Delta")], [0, None, 30, 5])

    def test_changing_the_total_still_works(self):
        form = self.form()
        form["w3"] = "45"
        self.post(f"/day/{DAY}/save", data=form)
        self.assertEqual(self.row("Gamma"), {"g": 45, "v": 3600, "stages": 0})

    # ------------------------------------------------------------------ 4. не 500
    def test_malformed_dates_are_client_errors_not_500(self):
        self.assertEqual(self.get("/day/abc").status_code, 404)
        self.assertEqual(self.get("/day/2026-13-45").status_code, 404)
        self.assertEqual(self.post("/day/abc/save", data={}).status_code, 400)
        self.assertEqual(self.post("/day/2026-13-45/save", data={"__del_row": "1"}).status_code, 400)
        self.assertLess(self.get("/day/2030-01-01").status_code, 500)          # верная дата без данных

    def test_deleting_a_row_or_tab_that_is_already_gone_is_harmless(self):
        before = (self.row("Gamma"), self.row("Alpha"))
        r = self.post(f"/day/{DAY}/save", data={"__del_row": "99"})
        self.assertEqual(r.status_code, 303)
        self.assertNotIn("deleted=1", r.headers["Location"])                   # удалять было нечего
        r = self.post(f"/day/{DAY}/save", data={"__del_scan": "999"})
        self.assertEqual(r.status_code, 303)
        self.assertNotIn("deleted=1", r.headers["Location"])
        self.assertEqual((self.row("Gamma"), self.row("Alpha")), before)

    def test_deleting_an_existing_row_still_reports_it(self):
        r = self.post(f"/day/{DAY}/save", data={"__del_row": "3"})
        self.assertIn("deleted=1", r.headers["Location"])
        with closing(player_store.connect(self.db)) as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM kv_daily_players").fetchone()[0], 3)

    # ------------------------------------------------------------------ 2. чужие сайты
    def test_requests_from_other_sites_are_refused_even_without_a_password(self):
        for headers in ({"Origin": "https://evil.example"},
                        {"Origin": "null"},
                        {"Origin": "http://localhost:3000"},                       # другой порт на том же хосте
                        {"Referer": "https://evil.example/page"},
                        {"Sec-Fetch-Site": "cross-site"}):
            r = self.post(f"/day/{DAY}/archive", headers=headers)
            self.assertEqual(r.status_code, 403, headers)
            self.assertEqual(self.archived(), 0, headers)
        r = self.post(f"/day/{DAY}/save", data={"__del_row": "1"}, headers={"Origin": "https://evil.example"})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.row("Alpha")["g"], 0)

    def test_the_panel_itself_and_plain_clients_still_work(self):
        r = self.post(f"/day/{DAY}/archive", headers={"Origin": "http://localhost:8787", "Sec-Fetch-Site": "same-origin"})
        self.assertEqual(r.status_code, 303)
        self.assertEqual(self.archived(), 1)
        self.assertEqual(self.post(f"/day/{DAY}/unarchive").status_code, 303)      # без Origin: curl, тесты
        self.assertEqual(self.archived(), 0)
        self.assertEqual(self.get(f"/day/{DAY}", headers={"Origin": "https://evil.example"}).status_code, 200)   # чтение не меняет данных


class StaticChecks(unittest.TestCase):
    def test_no_undefined_names_in_the_code(self):
        """Ловит вызовы несуществующих функций (так жила _update_manual_voice_scan_report в bot.py)."""
        try:
            from pyflakes import messages
            from pyflakes.api import check
        except ImportError:
            self.skipTest("pyflakes не установлен")

        class Collect:
            def __init__(self):
                self.undefined: list[str] = []

            def flake(self, message):
                if isinstance(message, messages.UndefinedName):
                    self.undefined.append(f"{message.filename}:{message.lineno} {message.message_args[0]}")

            def unexpectedError(self, filename, msg):
                self.undefined.append(f"{filename}: {msg}")

            syntaxError = unexpectedError

        files = [ROOT / "bot.py", ROOT / "player_store.py"] + sorted(
            p for p in (ROOT / "tools").glob("*.py") if not p.name.startswith("test_"))
        report = Collect()
        for path in files:
            check(path.read_text(encoding="utf-8"), str(path.relative_to(ROOT)), report)
        self.assertEqual(report.undefined, [])


if __name__ == "__main__":
    unittest.main()
