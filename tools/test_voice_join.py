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
D1, D2, D3 = "2026-11-07", "2026-11-08", "2026-11-14"     # сб, вс, сб
LIVE = "2026-11-15"                                        # идёт прямо сейчас: табов и гранат ещё нет


def stamp(day: str, hms: str) -> str:
    return f"{day}T{hms}+03:00"


class RecordVoiceJoinsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "scan_stats.sqlite3"
        player_store.ensure_schema(self.db)
        player_store.upsert_guild_config(self.db, GUILD)
        self.alpha = player_store.upsert_binding(self.db, 100, "Alpha", guild_id=GUILD)
        self.beta = player_store.upsert_binding(self.db, 200, "Beta", guild_id=GUILD)

    def tearDown(self):
        self.tmp.cleanup()

    def rows(self):
        with closing(player_store.connect(self.db)) as con:
            return [tuple(r) for r in con.execute(
                "SELECT player_id,match_date,joined_at FROM voice_join_times ORDER BY player_id,match_date")]

    def test_first_join_is_kept_and_later_ones_do_not_overwrite(self):
        first = stamp(D3, "19:39:12")
        self.assertEqual(player_store.record_voice_joins(self.db, GUILD, D3, {100: first}), 1)
        # вышел и зашёл снова / рестарт бота / тик обновления — время прежнее
        self.assertEqual(player_store.record_voice_joins(self.db, GUILD, D3, {"100": stamp(D3, "20:10:00")}), 0)
        self.assertEqual(self.rows(), [(self.alpha, D3, first)])

    def test_each_day_and_player_has_own_time(self):
        player_store.record_voice_joins(self.db, GUILD, D2, {100: stamp(D2, "18:31:00")})
        player_store.record_voice_joins(self.db, GUILD, D3, {100: stamp(D3, "19:40:00"), 200: stamp(D3, "19:45:30")})
        self.assertEqual(self.rows(), [
            (self.alpha, D2, stamp(D2, "18:31:00")),
            (self.alpha, D3, stamp(D3, "19:40:00")),
            (self.beta, D3, stamp(D3, "19:45:30")),
        ])

    def test_unbound_discord_account_and_blank_time_are_skipped(self):
        self.assertEqual(player_store.record_voice_joins(
            self.db, GUILD, D3, {999: stamp(D3, "19:30:00"), 100: "", 200: "  "}), 0)
        self.assertEqual(self.rows(), [])
        self.assertEqual(player_store.record_voice_joins(self.db, GUILD, D3, {}), 0)

    def test_bad_date_is_rejected(self):
        with self.assertRaises(ValueError):
            player_store.record_voice_joins(self.db, GUILD, "вчера", {100: stamp(D3, "19:30:00")})
        self.assertEqual(self.rows(), [])

    def test_table_appears_when_upgrading_an_old_database(self):
        old = Path(self.tmp.name) / "old.sqlite3"
        player_store.ensure_schema(old)
        with closing(player_store.connect(old)) as con:
            con.execute("DROP TABLE voice_join_times")
        player_store.ensure_schema(old)
        with closing(player_store.connect(old)) as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM voice_join_times").fetchone()[0], 0)

    def test_merging_duplicate_players_keeps_earliest_time(self):
        dup = player_store.upsert_binding(self.db, 300, "AlphaDup", guild_id=GUILD)
        player_store.record_voice_joins(self.db, GUILD, D3, {100: stamp(D3, "19:50:00"), 300: stamp(D3, "19:35:00")})
        player_store.record_voice_joins(self.db, GUILD, D2, {300: stamp(D2, "18:40:00")})
        with closing(player_store.connect(self.db)) as con:
            con.execute("BEGIN")
            player_store._merge_player(con, self.alpha, dup)
            con.commit()
        self.assertEqual(self.rows(), [
            (self.alpha, D2, stamp(D2, "18:40:00")),
            (self.alpha, D3, stamp(D3, "19:35:00")),
        ])


class VoiceJoinPageTests(unittest.TestCase):
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
        # Alpha, Beta — отряд 1; Gamma — Чемпионы; Delta — без отряда; Eps — удалён из состава
        for name, did, squad, slot, active in (("Alpha", 100, 1, 1, True), ("Beta", 200, 1, 2, True),
                                               ("Gamma", 300, 99, 1, True), ("Delta", 400, None, None, True),
                                               ("Eps", 500, 2, 1, False)):
            pid = player_store.upsert_binding(self.db, did, name, guild_id=GUILD)
            player_store.upsert_roster_member(self.db, GUILD, pid, squad_id=squad, slot=slot, active=active)
            self.ids[name] = did
        for day in (D1, D2, D3):
            self.seed_day(day)
        player_store.record_voice_joins(self.db, GUILD, D3, {
            100: stamp(D3, "19:39:12"), 200: stamp(D3, "19:52:00"), 300: stamp(D3, "19:31:05"),
            400: stamp(D3, "19:30:30"), 500: stamp(D3, "19:30:31")})
        player_store.record_voice_joins(self.db, GUILD, D2, {100: stamp(D2, "18:35:44")})
        player_store.record_voice_joins(self.db, GUILD, D1, {200: stamp(D1, "19:33:00")})
        player_store.record_voice_joins(self.db, GUILD, LIVE, {100: stamp(LIVE, "18:31:09")})
        self.patch = patch.object(admin_ui, "DB_PATH", self.db)
        self.patch.start()
        self.client = admin_ui.app.test_client()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()
        self.env.stop()

    def seed_day(self, day):
        with closing(player_store.connect(self.db)) as con:
            con.execute(
                """INSERT INTO kv_daily_reports(match_date,guild_id,source_path,source_sha256,stage_count)
                   VALUES(?,?,?,?,3)""", (day, GUILD, f"runtime:1:{day}", day))

    def get(self, path):
        return self.client.get(path, base_url="http://localhost:8787")

    def page(self):
        r = self.get("/players/voice")
        self.assertEqual(r.status_code, 200)
        return r.get_data(as_text=True)

    @staticmethod
    def header(html):
        thead = re.search(r"<thead>(.*?)</thead>", html, flags=re.S).group(1)
        return [re.sub(r"<[^>]+>", "", c).strip() for c in re.findall(r"<th[^>]*>(.*?)</th>", thead, flags=re.S)]

    @staticmethod
    def table(html):
        out = {}
        for row in re.findall(r"<tr class='player-row'>(.*?)</tr>", html, flags=re.S):
            cells = [re.sub(r"<[^>]+>", "", c).strip() for c in re.findall(r"<td[^>]*>(.*?)</td>", row, flags=re.S)]
            out[cells[0]] = cells[1:]
        return out

    def test_button_on_players_page_leads_to_voice_page(self):
        html = self.get("/players").get_data(as_text=True)
        self.assertIn("href='/players/voice'", html)
        self.assertIn("Заход в войс", html)

    def test_table_has_nick_and_non_archived_dates_newest_first(self):
        html = self.page()
        # LIVE-день виден сразу, хотя у него ещё нет отчёта; все остальные — дни КВ
        self.assertEqual(self.header(html), ["Ник", "15.11 вс", "14.11 сб", "08.11 вс", "07.11 сб"])

    def test_times_are_shown_per_player_and_day(self):
        table = self.table(self.page())
        self.assertEqual(table["Alpha"], ["18:31", "19:39", "18:35", "—"])
        self.assertEqual(table["Beta"], ["—", "19:52", "—", "19:33"])
        self.assertEqual(table["Gamma"], ["—", "19:31", "—", "—"])

    def test_only_players_from_squads_are_listed(self):
        table = self.table(self.page())
        self.assertEqual(set(table), {"Alpha", "Beta", "Gamma"})   # Delta без отряда, Eps удалён
        html = self.page()
        self.assertLess(html.index("Отряд 1"), html.index("Чемпионы"))

    def test_archived_day_disappears_and_returns(self):
        self.client.post(f"/day/{D3}/archive", base_url="http://localhost:8787")
        html = self.page()
        self.assertEqual(self.header(html), ["Ник", "15.11 вс", "08.11 вс", "07.11 сб"])
        self.assertEqual(self.table(html)["Alpha"], ["18:31", "18:35", "—"])
        self.client.post(f"/day/{D3}/unarchive", base_url="http://localhost:8787")
        self.assertIn("14.11 сб", self.header(self.page()))
        self.assertEqual(self.table(self.page())["Alpha"][1], "19:39")      # данные не потерялись

    def test_archiving_live_day_hides_its_column_too(self):
        self.client.post(f"/day/{LIVE}/archive", base_url="http://localhost:8787")
        self.assertNotIn("15.11 вс", self.header(self.page()))

    def test_day_links_only_where_day_page_exists(self):
        html = self.page()
        self.assertIn(f"href='/day/{D3}'", html)
        self.assertNotIn(f"href='/day/{LIVE}'", html)

    def test_full_time_is_in_tooltip_and_nick_links_to_player(self):
        html = self.page()
        self.assertIn("19:39:12 МСК", html)
        self.assertIn(f"href='/player/{player_store.resolve_player_id(self.db, 'Alpha')}'", html)

    def test_empty_database_and_database_without_roster_render(self):
        empty = Path(self.tmp.name) / "empty.sqlite3"
        player_store.ensure_schema(empty)
        with patch.object(admin_ui, "DB_PATH", empty):
            html = self.page()
            self.assertIn("Дней КВ пока нет", html)
        no_roster = Path(self.tmp.name) / "no_roster.sqlite3"
        player_store.ensure_schema(no_roster)
        player_store.record_voice_joins(no_roster, GUILD, D3, {})
        with closing(player_store.connect(no_roster)) as con:
            con.execute("INSERT INTO kv_daily_reports(match_date,guild_id,source_path,source_sha256,stage_count) "
                        "VALUES(?,?,?,?,3)", (D3, GUILD, "x", "x"))
        with patch.object(admin_ui, "DB_PATH", no_roster):
            self.assertIn("В отрядах пока никого нет", self.page())

    def test_players_page_still_lists_same_players(self):
        html = self.get("/players").get_data(as_text=True)
        names = re.findall(r"<tr class='player-row'><td class='nick grow'><a [^>]*>(.*?)</a>", html)
        self.assertEqual(set(names), {"Alpha", "Beta", "Gamma"})
        self.assertIn("function filterPlayers(value)", html)
        self.assertIn("function sortPlayerSquads(th)", html)

    def test_clock_formatting(self):
        self.assertEqual(admin_ui.fmt_clock("2026-11-14T19:39:12+03:00"), "19:39")
        self.assertEqual(admin_ui.fmt_clock("2026-11-14 07:05:00"), "07:05")
        for bad in (None, "", "garbage", "19:39"):
            self.assertEqual(admin_ui.fmt_clock(bad), "—")


if __name__ == "__main__":
    unittest.main()
