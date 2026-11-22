"""Ручная смена игрока у ника на табе («не найден» → chibrick из состава): привязка, откат, границы."""
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

DAY, OTHER_DAY = "2026-11-20", "2026-11-19"
GUILD = 1


class TabBindTests(unittest.TestCase):
    """
    Houfuku — человек из состава под ником Chibrick; на табе он «Houfuku» (ника в базе нет).
    DAY:       этап 1: Alpha, «Houfuku»; этап 2: Alpha, «houfuku» (другой регистр); этап 3: Alpha.
    OTHER_DAY: этап 1: «Houfuku» — другой день, его трогать нельзя.
    Delta есть в базе, но не в составе.
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
        for slot, nick in enumerate(("Alpha", "Chibrick"), 1):
            self.pid[nick] = player_store.upsert_binding(self.db, 100 * slot, nick, guild_id=GUILD)
            player_store.upsert_roster_member(self.db, GUILD, self.pid[nick], squad_id=1, slot=slot)
        self.pid["Delta"] = player_store.upsert_binding(self.db, 300, "Delta", guild_id=GUILD)
        with closing(player_store.connect(self.db)) as con:       # Delta — не в составе
            con.execute("UPDATE roster_memberships SET active=0 WHERE player_id=?", (self.pid["Delta"],))
        self.rows = {}
        self.rows["s1"] = self.seed_scan(DAY, 1, [("Alpha", "Alpha"), ("Houfuku", None)])
        self.rows["s2"] = self.seed_scan(DAY, 2, [("Alpha", "Alpha"), ("houfuku", None)])
        self.rows["s3"] = self.seed_scan(DAY, 3, [("Alpha", "Alpha")])
        self.rows["other"] = self.seed_scan(OTHER_DAY, 1, [("Houfuku", None)])
        self.seed_report(DAY)
        self.seed_report(OTHER_DAY)
        self.patch = patch.object(admin_ui, "DB_PATH", self.db)
        self.patch.start()
        self.client = admin_ui.app.test_client()
        self.local = "http://localhost:8787"

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()
        self.env.stop()

    # ── данные ──────────────────────────────────────────────────────────────
    def seed_scan(self, day, stage, rows):
        """Возвращает {ник: id строки scan_players}."""
        ids = {}
        with closing(player_store.connect(self.db)) as con:
            scan_id = con.execute(
                """INSERT INTO scans(stage,map,guild_id,channel_id,user_id,scanned_at,source_filename,match_date)
                   VALUES(?,'map',?,1,1,?,?,?)""", (stage, GUILD, f"{day} 20:00:00", f"{day}-{stage}.png", day)).lastrowid
            for place, (nick, owner) in enumerate(rows, 1):
                ids[nick] = con.execute(
                    """INSERT INTO scan_players(scan_id,place,nick,kills,deaths,assists,score,player_id)
                       VALUES(?,?,?,5,2,1,300,?)""",
                    (scan_id, place, nick, self.pid[owner] if owner else None)).lastrowid
        return ids

    def seed_report(self, day):
        with closing(player_store.connect(self.db)) as con:
            rid = con.execute("""INSERT INTO kv_daily_reports(match_date,guild_id,source_path,source_sha256,stage_count)
                                 VALUES(?,?,?,?,3)""", (day, GUILD, f"runtime:{day}", day, )).lastrowid
            for n, (nick, grenades) in enumerate((("Alpha", 30), ("Chibrick", 20)), 1):
                con.execute("""INSERT INTO kv_daily_players(report_id,row_no,player_id,raw_nick,voice_seconds,attended,total_grenades)
                               VALUES(?,?,?,?,600,1,?)""", (rid, n, self.pid[nick], nick, grenades))

    def owner(self, row_id):
        with closing(player_store.connect(self.db)) as con:
            return con.execute("SELECT player_id FROM scan_players WHERE id=?", (row_id,)).fetchone()[0]

    def log(self):
        with closing(player_store.connect(self.db)) as con:
            return [dict(r) for r in con.execute("SELECT * FROM scan_relink_log ORDER BY id")] \
                if con.execute("SELECT 1 FROM sqlite_master WHERE name='scan_relink_log'").fetchone() else []

    def get(self, path):
        return self.client.get(path, base_url=self.local)

    def bind(self, row_id, player_id, day=DAY):
        return self.client.post(f"/day/{day}/bind", data={"row": row_id, "player": player_id}, base_url=self.local)

    def page(self, day=DAY):
        return self.get(f"/day/{day}").get_data(as_text=True)

    # ── страница ────────────────────────────────────────────────────────────
    def test_unmatched_nick_is_a_button_that_knows_how_many_rows_it_changes(self):
        html = self.page()
        button = re.search(r"<button type='button' class='match bad' data-row='%d'[^>]*>" % self.rows["s1"]["Houfuku"], html)
        self.assertIsNotNone(button)
        self.assertIn("data-nick='Houfuku'", button.group(0))
        self.assertIn("data-same='2'", button.group(0))           # «Houfuku» и «houfuku» — один ник на двух табах
        self.assertIn(f"data-bind-url='/day/{DAY}/bind'", html)
        self.assertIn(f"data-players-url='/day/{DAY}/players.json'", html)
        self.assertIn("class='match ok'", html)                   # найденные (Alpha) тоже можно сменить
        self.assertIn("Не в табе (1)", html)                      # Chibrick в день не попал: на табе его нет

    def test_page_does_not_embed_the_player_list(self):
        main = self.page().partition("<details class='skipped'>")[0]
        self.assertNotIn("Delta", main)                           # список игроков грузится отдельно, при открытии окна

    def test_nick_with_markup_is_escaped_in_the_button(self):
        self.seed_scan(DAY, 1, [("<b>x</b>'\"", None)])
        html = self.page()
        self.assertNotIn("<b>x</b>", html)
        self.assertIn("data-nick='&lt;b&gt;x&lt;/b&gt;&#x27;&quot;'", html)

    # ── привязка ────────────────────────────────────────────────────────────
    def test_bind_moves_the_nick_on_every_tab_of_the_day_and_nothing_else(self):
        r = self.bind(self.rows["s1"]["Houfuku"], self.pid["Chibrick"])
        self.assertEqual(r.status_code, 303)
        self.assertIn("relinked=2", r.headers["Location"])
        self.assertEqual(self.owner(self.rows["s1"]["Houfuku"]), self.pid["Chibrick"])
        self.assertEqual(self.owner(self.rows["s2"]["houfuku"]), self.pid["Chibrick"])      # другой регистр — тот же ник
        self.assertIsNone(self.owner(self.rows["other"]["Houfuku"]))                        # другой день не тронут
        self.assertEqual(self.owner(self.rows["s1"]["Alpha"]), self.pid["Alpha"])
        log = self.log()
        self.assertEqual(len(log), 2)
        self.assertEqual(len({row["batch"] for row in log}), 1)
        self.assertTrue(all(row["old_player_id"] is None and row["new_player_id"] == self.pid["Chibrick"]
                            and row["match_date"] == DAY for row in log))

    def test_bound_player_counts_as_played_that_day(self):
        before = self.page()
        self.assertIn("Игроков: 1", before)
        self.assertIn("не найдено 2", before)                      # Houfuku на двух табах дня
        self.bind(self.rows["s1"]["Houfuku"], self.pid["Chibrick"])
        after = self.page()
        main, _, skipped = after.partition("<details class='skipped'>")
        self.assertIn("Игроков: 2", main)
        self.assertIn("Chibrick", main)
        self.assertEqual(skipped, "")                              # из «Не в табе» он ушёл
        self.assertIn("<span class='chip'>Гранат: 50</span>", main)   # 30 + 20
        self.assertIn("Houfuku <span class='dim'>(Chibrick)</span>", main)   # на табе остался ник как на скрине
        self.assertIn("все ники найдены", main)                    # в «Исходных табах» не осталось «не найден»
        self.assertNotIn("class='match bad'", main)

    def test_players_page_counts_the_day_for_the_bound_player(self):
        def days(nick):
            html = self.get("/players").get_data(as_text=True)
            row = next(c for c in html.split("<tr class='player-row'>") if f">{nick}<" in c)
            return int(re.search(r"<td>(\d+)</td>", row).group(1))
        before = days("Chibrick")
        self.bind(self.rows["s1"]["Houfuku"], self.pid["Chibrick"])
        self.assertEqual(days("Chibrick"), before + 1)

    def test_banner_links_to_history_and_undo_restores_the_old_binding(self):
        r = self.bind(self.rows["s1"]["Houfuku"], self.pid["Chibrick"])
        banner = self.get(r.headers["Location"]).get_data(as_text=True)
        self.assertIn("Перепривязано строк: 2", banner)
        self.assertIn(f"/day/{DAY}/relink", banner)
        history = self.get(f"/day/{DAY}/relink").get_data(as_text=True)
        self.assertIn("История перепривязок дня", history)
        self.assertIn("Houfuku", history)
        batch = self.log()[0]["batch"]
        undo = self.client.post(f"/day/{DAY}/relink/undo", data={"batch": batch}, base_url=self.local)
        self.assertEqual(undo.status_code, 303)
        self.assertIsNone(self.owner(self.rows["s1"]["Houfuku"]))
        self.assertIsNone(self.owner(self.rows["s2"]["houfuku"]))

    def test_bind_can_replace_an_earlier_choice(self):
        self.bind(self.rows["s1"]["Houfuku"], self.pid["Chibrick"])
        r = self.bind(self.rows["s1"]["Houfuku"], self.pid["Delta"])
        self.assertIn("relinked=2", r.headers["Location"])
        self.assertEqual(self.owner(self.rows["s2"]["houfuku"]), self.pid["Delta"])
        self.assertEqual([row["old_player_id"] for row in self.log()[2:]], [self.pid["Chibrick"]] * 2)

    def test_binding_the_same_player_again_changes_nothing(self):
        self.bind(self.rows["s1"]["Houfuku"], self.pid["Chibrick"])
        logged = len(self.log())
        r = self.bind(self.rows["s1"]["Houfuku"], self.pid["Chibrick"])
        self.assertIn("relinked=0", r.headers["Location"])
        self.assertEqual(len(self.log()), logged)

    # ── границы ─────────────────────────────────────────────────────────────
    def test_player_already_on_that_tab_is_refused_without_changes(self):
        with closing(player_store.connect(self.db)) as con:        # Chibrick уже есть в том же табе под своим ником
            scan_id = con.execute("SELECT scan_id FROM scan_players WHERE id=?", (self.rows["s1"]["Houfuku"],)).fetchone()[0]
            con.execute("""INSERT INTO scan_players(scan_id,place,nick,kills,deaths,assists,score,player_id)
                           VALUES(?,9,'Chibrick',1,1,1,100,?)""", (scan_id, self.pid["Chibrick"]))
        r = self.bind(self.rows["s1"]["Houfuku"], self.pid["Chibrick"])
        self.assertIn("bindfail=dup", r.headers["Location"])
        self.assertIsNone(self.owner(self.rows["s1"]["Houfuku"]))
        self.assertIsNone(self.owner(self.rows["s2"]["houfuku"]))  # и второй таб не изменился: всё или ничего
        self.assertEqual(self.log(), [])
        self.assertIn("задвоились бы", self.get(r.headers["Location"]).get_data(as_text=True))

    def test_stale_row_unknown_player_and_garbage_are_refused(self):
        cases = [
            (self.rows["other"]["Houfuku"], self.pid["Chibrick"], "gone"),     # строка другого дня
            (999999, self.pid["Chibrick"], "gone"),
            (self.rows["s1"]["Houfuku"], 999999, "player"),
        ]
        for row, player, reason in cases:
            with self.subTest(reason=reason, row=row):
                r = self.bind(row, player)
                self.assertEqual(r.status_code, 303)
                self.assertIn(f"bindfail={reason}", r.headers["Location"])
        for data in ({}, {"row": "x", "player": "1"}, {"row": "1"}):
            with self.subTest(data=data):
                r = self.client.post(f"/day/{DAY}/bind", data=data, base_url=self.local)
                self.assertIn("bindfail=gone", r.headers["Location"])
        self.assertEqual(self.log(), [])
        self.assertIsNone(self.owner(self.rows["s1"]["Houfuku"]))

    def test_bad_date_is_rejected(self):
        self.assertEqual(self.client.post("/day/не-дата/bind", data={"row": 1, "player": 1}, base_url=self.local).status_code, 400)
        self.assertEqual(self.get("/day/не-дата/players.json").status_code, 404)

    def test_numbers_and_other_tables_are_untouched(self):
        def snapshot():
            with closing(player_store.connect(self.db)) as con:
                return {t: [tuple(r) for r in con.execute(f"SELECT * FROM {t} ORDER BY 1,2")]
                        for t in ("kv_daily_players", "kv_daily_grenade_stages", "player_aliases", "roster_memberships")} | {
                    "tabs": [tuple(r)[:8] for r in con.execute("SELECT * FROM scan_players ORDER BY id")]}
        before = snapshot()
        self.bind(self.rows["s1"]["Houfuku"], self.pid["Chibrick"])
        after = snapshot()
        for key in ("kv_daily_players", "kv_daily_grenade_stages", "player_aliases", "roster_memberships"):
            self.assertEqual(before[key], after[key], key)
        self.assertEqual(len(before["tabs"]), len(after["tabs"]))     # строки на месте, меняется только привязка

    # ── список игроков для окна ─────────────────────────────────────────────
    def test_players_json_lists_roster_first_then_the_rest(self):
        r = self.get(f"/day/{DAY}/players.json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json(), [
            {"id": self.pid["Alpha"], "nick": "Alpha", "group": "Отряд 1"},
            {"id": self.pid["Chibrick"], "nick": "Chibrick", "group": "Отряд 1"},
            {"id": self.pid["Delta"], "nick": "Delta", "group": "Не в составе"},
        ])


if __name__ == "__main__":
    unittest.main()
