"""Перепривязка ников табов дня к игрокам: предпросмотр, применение, откат, границы."""
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

OLD_DAY, DAY = "2026-08-18", "2026-11-14"
GUILD = 1


class RelinkTests(unittest.TestCase):
    """
    Meid — человек, который раньше играл под ником «Пельмешек»; теперь ник «Пельмешек» у нового игрока.
    OLD_DAY (август): ник «Пельмешек» на табе — это Meid, строка привязана к Meid. Трогать нельзя.
    DAY (14.11): на табе «Пельмешек» (новый человек), но строка привязана к Meid; «meid» — Meid; Alpha.
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
        for i, nick in enumerate(("Meid", "Пельмешек", "Alpha"), 1):
            self.pid[nick] = player_store.upsert_binding(self.db, 100 * i, nick, guild_id=GUILD)
        self.old_scan = self.seed_scan(OLD_DAY, [("Пельмешек", "Meid", 30, 20, 10, 9000)])
        self.scan1 = self.seed_scan(DAY, [("Alpha", "Alpha", 20, 10, 5, 8000),
                                          ("Пельмешек", "Meid", 14, 17, 6, 5453),
                                          ("meid", "Meid", 15, 11, 8, 4950)])
        self.scan2 = self.seed_scan(DAY, [("Пельмешек", "Meid", 18, 11, 6, 6270),
                                          ("meid", "Meid", 12, 13, 8, 5408)])
        self.seed_report(DAY)
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
        """rows: (ник на табе, к кому привязана строка, У, С, П, счёт)."""
        with closing(player_store.connect(self.db)) as con:
            scan_id = con.execute(
                """INSERT INTO scans(stage,map,guild_id,channel_id,user_id,scanned_at,source_filename,match_date)
                   VALUES(NULL,'map',?,1,1,?,?,?)""", (GUILD, f"{day} 20:00:00", f"{day}.png", day)).lastrowid
            for place, (nick, owner, k, d, a, score) in enumerate(rows, 1):
                con.execute("""INSERT INTO scan_players(scan_id,place,nick,kills,deaths,assists,score,player_id)
                               VALUES(?,?,?,?,?,?,?,?)""",
                            (scan_id, place, nick, k, d, a, score, self.pid[owner] if owner else None))
        return scan_id

    def seed_report(self, day):
        with closing(player_store.connect(self.db)) as con:
            rid = con.execute("""INSERT INTO kv_daily_reports(match_date,guild_id,source_path,source_sha256,stage_count)
                                 VALUES(?,?,?,?,3)""", (day, GUILD, f"runtime:{day}", day)).lastrowid
            for n, nick in enumerate(("Meid", "Пельмешек", "Alpha"), 1):
                con.execute("""INSERT INTO kv_daily_players(report_id,row_no,player_id,raw_nick,voice_seconds,attended,total_grenades)
                               VALUES(?,?,?,?,600,1,10)""", (rid, n, self.pid[nick], nick))

    def get(self, path):
        return self.client.get(path, base_url=self.local)

    def post(self, path, **kw):
        return self.client.post(path, base_url=self.local, **kw)

    def owner_of(self, scan_id, nick):
        with closing(player_store.connect(self.db)) as con:
            row = con.execute("""SELECT p.canonical_nick FROM scan_players sp LEFT JOIN players p ON p.id=sp.player_id
                                 WHERE sp.scan_id=? AND sp.nick=?""", (scan_id, nick)).fetchone()
        return row[0] if row else "?"

    def plan(self, day=DAY, scan_id=None):
        with closing(player_store.connect(self.db)) as con:
            return player_store.plan_scan_relink(con, day, scan_id)

    def snapshot(self):
        with closing(player_store.connect(self.db)) as con:
            return {
                "numbers": [tuple(r) for r in con.execute(
                    "SELECT id,scan_id,place,nick,kills,deaths,assists,score FROM scan_players ORDER BY id")],
                "kv": [tuple(r) for r in con.execute("SELECT * FROM kv_daily_players ORDER BY report_id,row_no")],
                "players": [tuple(r) for r in con.execute("SELECT id,canonical_nick FROM players ORDER BY id")],
                "bindings": [tuple(r) for r in con.execute("SELECT discord_id,player_id FROM discord_bindings ORDER BY 1")],
            }

    def skipped_nicks(self, day=DAY):
        page = self.get(f"/day/{day}").get_data(as_text=True)
        tail = page.partition("<details class='skipped'>")[2]
        return re.findall(r"<td class='nick'>(?:<a [^>]*>)?([^<]+)", tail)

    # ── план ────────────────────────────────────────────────────────────────
    def test_plan_moves_only_the_wrongly_linked_nick(self):
        plan = self.plan()
        self.assertEqual(len(plan["groups"]), 1)
        g = plan["groups"][0]
        self.assertEqual((g["nick"], g["old_nick"], g["new_nick"], len(g["rows"]), g["conflict"]),
                         ("Пельмешек", "Meid", "Пельмешек", 2, False))        # по одной строке на каждом из двух табов
        self.assertEqual((plan["total"], plan["unchanged"], plan["kept"]), (5, 3, []))
        self.assertEqual(self.owner_of(self.scan1, "Пельмешек"), "Meid")      # предпросмотр ничего не меняет

    def test_scope_of_one_tab(self):
        plan = self.plan(scan_id=self.scan2)
        self.assertEqual((plan["total"], len(plan["groups"][0]["rows"])), (2, 1))

    def test_unknown_and_fuzzy_linked_rows_are_left_alone(self):
        with closing(player_store.connect(self.db)) as con:   # один и тот же ник — алиас у двух игроков, основного нет
            con.execute("INSERT INTO player_aliases(alias,player_id) VALUES('Двойной',?)", (self.pid["Meid"],))
            con.execute("INSERT INTO player_aliases(alias,player_id) VALUES('двойной ',?)", (self.pid["Alpha"],))
        sid = self.seed_scan(DAY, [("Alpa", "Alpha", 1, 1, 1, 100), ("Nikto_Takoj", None, 1, 1, 1, 100),
                                   ("Двойной", None, 1, 1, 1, 100)])
        plan = self.plan(scan_id=sid)
        self.assertEqual(plan["groups"], [])
        reasons = {k["nick"]: k["reason"] for k in plan["kept"]}
        self.assertEqual(reasons, {"Alpa": "ника нет в базе", "Nikto_Takoj": "ника нет в базе",
                                   "Двойной": "ник принадлежит нескольким игрокам"})
        self.assertEqual(self.owner_of(sid, "Alpa"), "Alpha")                  # привязка по нечёткому совпадению осталась

    def test_unlinked_row_with_exact_nick_gets_linked(self):
        sid = self.seed_scan(DAY, [("alpha", None, 3, 3, 3, 300)])
        group = self.plan(scan_id=sid)["groups"][0]
        self.assertEqual((group["old_nick"], group["new_nick"]), (None, "Alpha"))

    def test_alias_conflict_prefers_canonical_but_is_not_preselected(self):
        with closing(player_store.connect(self.db)) as con:
            con.execute("INSERT INTO player_aliases(alias,player_id) VALUES('Пельмешек',?)", (self.pid["Meid"],))
        g = self.plan()["groups"][0]
        self.assertTrue(g["conflict"])
        self.assertEqual((g["new_nick"], g["also"]), ("Пельмешек", ["Meid"]))
        page = self.get(f"/day/{DAY}/relink").get_data(as_text=True)
        self.assertIn("конфликт", page)
        self.assertNotRegex(page, r"name='pick' value='[^']+' checked")        # конфликт — галочка снята

    # ── применение и откат ──────────────────────────────────────────────────
    def test_apply_moves_the_new_player_back_into_the_day_and_undo_restores(self):
        self.assertIn("Пельмешек", self.skipped_nicks())                        # до: новый человек «не играл»
        before = self.snapshot()
        key = self.plan()["groups"][0]["key"]
        batch, moved = player_store.apply_scan_relink(self.db, DAY, {key})
        self.assertEqual(moved, 2)
        self.assertEqual((self.owner_of(self.scan1, "Пельмешек"), self.owner_of(self.scan2, "Пельмешек")),
                         ("Пельмешек", "Пельмешек"))
        self.assertEqual(self.owner_of(self.scan1, "meid"), "Meid")
        self.assertNotIn("Пельмешек", self.skipped_nicks())                     # после: в дне, не в «Не в табе»
        self.assertEqual(self.snapshot()["numbers"], before["numbers"])         # цифры табов не тронуты
        self.assertEqual(self.snapshot()["kv"], before["kv"])
        self.assertEqual((self.snapshot()["players"], self.snapshot()["bindings"]),
                         (before["players"], before["bindings"]))
        self.assertEqual(self.plan()["groups"], [])                             # повторно менять нечего

        self.assertEqual(player_store.undo_scan_relink(self.db, batch), (2, 0))
        self.assertEqual(self.owner_of(self.scan1, "Пельмешек"), "Meid")
        self.assertIn("Пельмешек", self.skipped_nicks())
        self.assertEqual(player_store.undo_scan_relink(self.db, batch), (0, 0))  # повторный откат ничего не делает

    def test_other_days_are_never_touched(self):
        key = self.plan()["groups"][0]["key"]
        player_store.apply_scan_relink(self.db, DAY, {key})
        self.assertEqual(self.owner_of(self.old_scan, "Пельмешек"), "Meid")     # август остался за Meid

    def test_apply_only_selected_groups_and_one_tab(self):
        key = self.plan()["groups"][0]["key"]
        _, moved = player_store.apply_scan_relink(self.db, DAY, {key}, scan_id=self.scan2)
        self.assertEqual(moved, 1)
        self.assertEqual((self.owner_of(self.scan1, "Пельмешек"), self.owner_of(self.scan2, "Пельмешек")),
                         ("Meid", "Пельмешек"))
        self.assertEqual(player_store.apply_scan_relink(self.db, DAY, set())[1], 0)   # ничего не выбрано — ничего не меняется

    def test_stale_preview_cannot_overwrite_a_changed_row(self):
        key = self.plan()["groups"][0]["key"]
        with closing(player_store.connect(self.db)) as con:                     # кто-то уже поменял привязку строки
            con.execute("UPDATE scan_players SET player_id=? WHERE scan_id=? AND nick='Пельмешек'",
                        (self.pid["Alpha"], self.scan1))
        _, moved = player_store.apply_scan_relink(self.db, DAY, {key})
        self.assertEqual(moved, 1)                                              # ушла только вторая строка, первую не трогаем
        self.assertEqual(self.owner_of(self.scan1, "Пельмешек"), "Alpha")

    def test_undo_skips_rows_changed_since(self):
        key = self.plan()["groups"][0]["key"]
        batch, _ = player_store.apply_scan_relink(self.db, DAY, {key})
        with closing(player_store.connect(self.db)) as con:
            con.execute("UPDATE scan_players SET player_id=? WHERE scan_id=? AND nick='Пельмешек'",
                        (self.pid["Alpha"], self.scan1))
        self.assertEqual(player_store.undo_scan_relink(self.db, batch), (1, 1))
        self.assertEqual(self.owner_of(self.scan1, "Пельмешек"), "Alpha")

    # ── панель ──────────────────────────────────────────────────────────────
    def test_day_page_has_buttons_for_the_day_and_for_each_tab(self):
        page = self.get(f"/day/{DAY}").get_data(as_text=True)
        self.assertIn(f"href='/day/{DAY}/relink'", page)
        self.assertIn(f"href='/day/{DAY}/relink?scan={self.scan1}'", page)
        self.assertIn(f"href='/day/{DAY}/relink?scan={self.scan2}'", page)

    def test_preview_apply_and_undo_through_the_panel(self):
        page = self.get(f"/day/{DAY}/relink").get_data(as_text=True)
        self.assertIn("Перепривязка ников", page)
        self.assertRegex(page, r"name='pick' value='[^']+' checked")            # обычная группа выбрана заранее
        self.assertEqual(self.get(f"/day/{DAY}/relink?scan=999999").status_code, 404)
        self.assertEqual(self.get("/day/2026-11-20/relink").status_code, 200)   # день без табов — пустой предпросмотр

        pick = re.search(r"name='pick' value='([^']+)'", page).group(1)
        r = self.post(f"/day/{DAY}/relink", data={"pick": pick})
        self.assertEqual(r.status_code, 303)
        self.assertEqual(r.headers["Location"], f"/day/{DAY}?relinked=2")
        day_page = self.get(r.headers["Location"]).get_data(as_text=True)
        self.assertIn("Перепривязано строк: 2", day_page)
        self.assertNotIn("Пельмешек", self.skipped_nicks())

        hist = self.get(f"/day/{DAY}/relink").get_data(as_text=True)
        self.assertIn("История перепривязок дня", hist)
        batch = re.search(r"name='batch' value='([^']+)'", hist).group(1)
        r = self.post(f"/day/{DAY}/relink/undo", data={"batch": batch})
        self.assertEqual(r.headers["Location"], f"/day/{DAY}?unrelinked=2&skipped=0")
        self.assertIn("Возвращено строк: 2", self.get(r.headers["Location"]).get_data(as_text=True))
        self.assertEqual(self.owner_of(self.scan1, "Пельмешек"), "Meid")
        self.assertIn("возвращено", self.get(f"/day/{DAY}/relink").get_data(as_text=True))

    def test_apply_without_selection_changes_nothing(self):
        r = self.post(f"/day/{DAY}/relink", data={})
        self.assertEqual(r.headers["Location"], f"/day/{DAY}?relinked=0")
        self.assertIn("Ничего не выбрано", self.get(r.headers["Location"]).get_data(as_text=True))

    def test_bad_date_is_rejected(self):
        self.assertEqual(self.get("/day/not-a-date/relink").status_code, 400)
        self.assertEqual(self.post("/day/not-a-date/relink", data={}).status_code, 400)


if __name__ == "__main__":
    unittest.main()
