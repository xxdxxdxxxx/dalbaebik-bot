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
from tools import admin_ui, clanmap_sync, roster_board, roster_consistency

GUILD = 42
LOCAL = "http://localhost:8787"


class SubsTenSlotsTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {}, clear=False)
        self.env.start()
        for key in ("ADMIN_PASSWORD", "ADMIN_TRUST_PROXY"):
            os.environ.pop(key, None)
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "roster.sqlite3"
        player_store.ensure_schema(self.db)
        player_store.upsert_guild_config(self.db, GUILD)
        self.patch = patch.object(admin_ui, "DB_PATH", self.db)
        self.patch.start()
        self.client = admin_ui.app.test_client()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()
        self.env.stop()

    def page(self):
        r = self.client.get("/squads", base_url=LOCAL)
        self.assertEqual(r.status_code, 200)
        return r.get_data(as_text=True)

    def cell(self, squad, slot, text):
        return self.client.post("/squads/cell", base_url=LOCAL, data={"squad": squad, "slot": slot, "text": text})

    def membership(self, nick):
        with closing(player_store.connect(self.db)) as con:
            row = con.execute("""SELECT r.squad_id,r.slot,r.active FROM roster_memberships r
                JOIN players p ON p.id=r.player_id WHERE p.canonical_nick=?""", (nick,)).fetchone()
        return None if row is None else (row["squad_id"], row["slot"], bool(row["active"]))

    # ------------------------------------------------------------------ страница
    def test_subs_have_eleven_cells_and_squads_five(self):
        html = self.page()
        cells = re.findall(r"data-squad='(\d+)' data-slot='(\d+)'", html)
        per_squad = {}
        for squad, slot in cells:
            per_squad.setdefault(int(squad), []).append(int(slot))
        self.assertEqual(per_squad[99], list(range(1, 12)))
        for squad in (1, 2, 3, 4, 5, 6):
            self.assertEqual(per_squad[squad], [1, 2, 3, 4, 5])
        self.assertEqual(len(cells), 6 * 5 + 11)

    def test_subs_are_one_column_beside_all_squads_down_to_the_bottom(self):
        html = self.page()
        zone = re.search(r"<div class='squads-zone'>(.*?)<div class='notes-grid'>", html, flags=re.S).group(1)
        main, subs = zone.split("<section class='sheet-col sheet-subs'>")
        # слева — отряды 1–6 в двух рядах по три, замен там нет
        self.assertEqual(re.findall(r"<div class='sheet-head'>(\w+)</div>", main), ["1", "2", "3", "4", "5", "6"])
        self.assertNotIn("data-squad='99'", main)
        # справа — один заголовок «Замены» и 11 одинаковых рабочих мест; 6-е стоит в полосе между рядами
        self.assertEqual(subs.count("<div class='sheet-head'>Замены</div>"), 1)
        self.assertNotIn("sheet-band", subs)                       # серой нередактируемой полосы больше нет
        self.assertNotIn("6–10", subs)
        slots = re.findall(r"data-slot='(\d+)'", subs)
        self.assertEqual(slots, [str(i) for i in range(1, 12)])
        mid = subs.index("sheet-mid")
        self.assertEqual(len(re.findall(r"data-slot='\d+'", subs[:mid])), 5)   # места 1–5 до 6-го
        self.assertIn("data-slot='6'", subs[mid - 120:mid + 120])
        self.assertEqual(len(re.findall(r"data-slot='\d+'", subs[mid:])), 6)               # 6-е и 7–11
        self.assertEqual(html.count("class='sheet-col sheet-subs'"), 1)

    def test_notes_still_line_up_with_four_columns(self):
        html = self.page()
        self.assertEqual(html.count("class='note-col'"), 4)

    # ---------------------------------------------------------------- редактирование
    def test_all_eleven_subs_places_can_be_filled_and_survive_reload(self):
        for slot in range(1, 12):
            self.assertEqual(self.cell(99, slot, f"Sub{slot}").status_code, 204, slot)
        for slot in range(1, 12):
            self.assertEqual(self.membership(f"Sub{slot}"), (99, slot, True))
        html = self.page()
        for slot in range(1, 12):
            self.assertRegex(html, rf"data-squad='99' data-slot='{slot}' value='Sub{slot}'")
        # повторный заход на страницу (пересборка ячеек из состава) ничего не теряет
        html = self.page()
        self.assertRegex(html, r"data-squad='99' data-slot='11' value='Sub11'")

    def test_places_outside_limits_are_rejected(self):
        self.assertEqual(self.cell(99, 12, "Nobody").status_code, 400)
        self.assertEqual(self.cell(99, 0, "Nobody").status_code, 400)
        self.assertEqual(self.cell(1, 6, "Nobody").status_code, 400)      # у отрядов по-прежнему 5
        self.assertEqual(self.cell(4, 10, "Nobody").status_code, 400)
        self.assertIsNone(self.membership("Nobody"))

    def test_swap_between_subs_and_squad_works_in_both_directions(self):
        self.cell(99, 8, "Reserve")
        self.cell(4, 2, "Fighter")
        r = self.client.post("/squads/cells/swap", base_url=LOCAL, data={
            "a_squad": 99, "a_slot": 8, "b_squad": 4, "b_slot": 2})
        self.assertEqual(r.status_code, 204)
        self.assertEqual(self.membership("Reserve"), (4, 2, True))
        self.assertEqual(self.membership("Fighter"), (99, 8, True))
        # слот 8 есть только у замен: обратный обмен на место 8 отряда — ошибка
        r = self.client.post("/squads/cells/swap", base_url=LOCAL, data={
            "a_squad": 4, "a_slot": 2, "b_squad": 1, "b_slot": 8})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.membership("Reserve"), (4, 2, True))

    def test_moving_player_between_subs_places_clears_old_cell(self):
        self.cell(99, 3, "Mover")
        self.cell(99, 9, "Mover")
        self.assertEqual(self.membership("Mover"), (99, 9, True))
        html = self.page()
        self.assertRegex(html, r"data-squad='99' data-slot='3' value=''")
        self.assertRegex(html, r"data-squad='99' data-slot='9' value='Mover'")

    # ------------------------------------------------------------ соседние механизмы
    def test_sync_board_cells_keeps_slots_six_to_eleven_of_subs(self):
        pid = player_store.upsert_binding(self.db, "100", "Late", guild_id=GUILD)
        player_store.upsert_roster_member(self.db, GUILD, pid, squad_id=99, slot=11)
        with closing(player_store.connect(self.db)) as con:
            roster_consistency.sync_board_cells(con, GUILD)
            rows = {(r["squad_id"], r["slot"]): r["text"] for r in con.execute(
                "SELECT squad_id,slot,text FROM roster_board_cells WHERE guild_id=?", (GUILD,))}
        self.assertEqual(rows[(99, 11)], "Late")
        self.assertEqual({s for sq, s in rows if sq == 99}, set(range(1, 12)))
        self.assertEqual({s for sq, s in rows if sq == 1}, set(range(1, 6)))
        self.assertEqual(len(rows), 6 * 5 + 11)

    def test_clan_map_still_gets_only_squads_one_to_six(self):
        self.cell(99, 7, "Reserve")
        self.cell(2, 3, "Fighter")
        desired = clanmap_sync._desired_roster(self.db)
        self.assertEqual(desired[(2, 3)], "Fighter")
        self.assertEqual(len(desired), 30)
        self.assertNotIn("Reserve", desired.values())

    def test_slots_for(self):
        self.assertEqual(list(roster_board.slots_for(99)), list(range(1, 12)))
        for squad in (1, 2, 3, 4, 5, 6):
            self.assertEqual(list(roster_board.slots_for(squad)), [1, 2, 3, 4, 5])


if __name__ == "__main__":
    unittest.main()
