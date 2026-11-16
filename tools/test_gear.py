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
from tools import admin_ui, gear

GUILD = 42
LOCAL = "http://localhost:8787"


class GearPageTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {}, clear=False)
        self.env.start()
        for key in ("ADMIN_PASSWORD", "ADMIN_TRUST_PROXY"):
            os.environ.pop(key, None)
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "gear.sqlite3"
        player_store.ensure_schema(self.db)
        player_store.upsert_guild_config(self.db, GUILD)
        self.patch = patch.object(admin_ui, "DB_PATH", self.db)
        self.patch.start()
        self.client = admin_ui.app.test_client()
        self.place(1, 1, "Alpha")
        self.place(1, 2, "Bravo")
        self.place(99, 1, "Subby")
        self.alpha, self.bravo, self.subby = (self.pid(n) for n in ("Alpha", "Bravo", "Subby"))

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()
        self.env.stop()

    def place(self, squad, slot, nick):
        r = self.client.post("/squads/cell", base_url=LOCAL, data={"squad": squad, "slot": slot, "text": nick})
        self.assertEqual(r.status_code, 204)

    def pid(self, nick):
        with closing(player_store.connect(self.db)) as con:
            return int(con.execute("SELECT id FROM players WHERE canonical_nick=?", (nick,)).fetchone()[0])

    def page(self):
        r = self.client.get("/gear", base_url=LOCAL)
        self.assertEqual(r.status_code, 200)
        return r.get_data(as_text=True)

    def put(self, player, col, **extra):
        return self.client.post("/gear/set", base_url=LOCAL, data={"player": player, "col": col, **extra})

    def stored(self):
        with closing(player_store.connect(self.db)) as con:
            return {(r["player_id"], r["slot"]): (r["item"], r["level"], r["text"])
                    for r in con.execute("SELECT * FROM player_gear")}

    def speed_value(self, html, player):
        return re.search(rf"<input class='g-sp[^']*' data-player='{player}'[^>]*? value='([^']*)'", html).group(1)

    def slot_tag(self, html, player, col):
        return re.search(rf"<button[^>]*data-player='{player}' data-col='{col}'.*?</button>", html, flags=re.S).group(0)

    # ------------------------------------------------------------------ страница
    def test_page_lists_squad_players_with_empty_slots_and_nav_link(self):
        html = self.page()
        self.assertIn(">Шмот</a>", html)
        self.assertRegex(html, r"class='active' href='/gear'")
        heads = re.findall(r"<th class='g-c[^']*'>([^<]+)</th>", html)
        self.assertEqual(heads, ["Снайпы", "Кувалда", "Био", "Тяж", "Огнемёт", "Скорость"])   # порядок слева направо
        self.assertIn("Отряд 1", html)
        self.assertIn("Чемпионы", html)
        for nick in ("Alpha", "Bravo", "Subby"):
            self.assertIn(f">{nick}</a>", html)
        # 3 игрока × 5 слотов с предметами, все пустые, и 3 поля скорости
        self.assertEqual(len(re.findall(r"class='g-slot empty'", html)), 15)
        self.assertEqual(len(re.findall(r"class='g-sp'", html)), 3)
        self.assertEqual(self.stored(), {})

    def test_cells_follow_header_order_in_every_row(self):
        row = re.search(r"<tr class='player-row'>.*?</tr>", self.page(), flags=re.S).group(0)
        cols = re.findall(r"data-col='(\w+)'|class='g-sp[^']*' data-player='\d+'", row)
        order = [c or "speed" for c in cols]
        self.assertEqual(order, ["gauss", "hammer", "bio", "heavy", "flamer", "speed"])

    def test_page_has_no_hint_text_under_the_title(self):
        html = self.page()
        self.assertIn("<h1>Шмот</h1>", html)
        self.assertNotIn("<div class='subtitle'>", html)               # сам блок; класс .subtitle есть в общем CSS админки
        self.assertNotIn("Нажмите на слот", html)
        self.assertNotIn("двузначное число (например", html)

    def test_filled_slots_have_rarity_border_icon_and_level(self):
        for col, item, level in (("gauss", "gauss", 15), ("flamer", "flamer", 10), ("bio", "zivk", 0),
                                 ("heavy", "chief", 15), ("hammer", "hammer", 3)):
            r = self.put(self.alpha, col, item=item, level=level)
            self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
            self.assertEqual(r.get_json(), {"ok": True, "item": item, "level": level, "text": ""})
        html = self.page()
        gauss = self.slot_tag(html, self.alpha, "gauss")
        self.assertIn("border-color:#CC9933", gauss)               # легендарный — золотая рамка
        self.assertIn(">+15</span>", gauss)
        self.assertIn("/gear/icon/gauss.webp", gauss)
        flamer = self.slot_tag(html, self.alpha, "flamer")
        self.assertIn("border-color:#933", flamer)                 # исключительный — красная
        self.assertIn(">+10</span>", flamer)
        self.assertIn("border-color:#CC9933", self.slot_tag(html, self.alpha, "bio"))
        self.assertNotIn("g-lv", self.slot_tag(html, self.alpha, "bio"))   # +0 не подписывается
        self.assertIn("border-color:#933", self.slot_tag(html, self.alpha, "heavy"))
        self.assertIn("border-color:#CC9933", self.slot_tag(html, self.alpha, "hammer"))
        self.assertIn("class='g-slot empty'", self.slot_tag(html, self.bravo, "gauss"))   # у других — пусто

    def test_speed_is_two_digit_number_or_dash(self):
        for raw, saved in (("39", "39"), (" 40 ", "40"), ("25", "25"), ("10", "10"), ("99", "99")):
            r = self.put(self.alpha, "speed", text=raw)
            self.assertEqual(r.status_code, 200, raw)
            self.assertEqual(r.get_json(), {"ok": True, "item": "", "level": 0, "text": saved})
            self.assertEqual(self.stored()[(self.alpha, "speed")], ("", 0, saved))
            self.assertEqual(self.speed_value(self.page(), self.alpha), saved)
        for dash in ("-", "—", " – ", "--", "", "   "):            # прочерк = пусто, запись удаляется
            self.put(self.alpha, "speed", text="33")
            r = self.put(self.alpha, "speed", text=dash)
            self.assertEqual(r.get_json()["text"], "", repr(dash))
            self.assertNotIn((self.alpha, "speed"), self.stored())
        self.assertEqual(self.speed_value(self.page(), self.alpha), "")

    def test_speed_rejects_anything_but_two_digits(self):
        self.put(self.alpha, "speed", text="39")
        for bad in ("5", "0", "9", "100", "009", "05", "-5", "3 9", "33 32 45", "abc", "3a", "3.9", "39%", "٣٩", "３９"):
            with self.subTest(value=bad):
                r = self.put(self.alpha, "speed", text=bad)
                self.assertEqual(r.status_code, 400)
                self.assertFalse(r.get_json()["ok"])
                self.assertIn("двузначное", r.get_json()["error"])
        self.assertEqual(self.stored()[(self.alpha, "speed")], ("", 0, "39"))   # прежнее значение не тронуто

    def test_speed_input_is_numeric_and_old_free_text_is_kept_but_flagged(self):
        html = self.page()
        self.assertRegex(html, r"<input class='g-sp' data-player='\d+' placeholder='—' inputmode='numeric' autocomplete='off' maxlength='2'")
        with closing(player_store.connect(self.db)) as con:                    # запись из старой версии
            con.execute("INSERT INTO player_gear(player_id,slot,item,level,text) VALUES(?,?,?,?,?)",
                        (self.alpha, "speed", "", 0, "33 32 45"))
            con.commit()
        html = self.page()
        self.assertRegex(html, rf"<input class='g-sp err' data-player='{self.alpha}'[^>]*value='33 32 45' title='")
        self.assertEqual(self.stored()[(self.alpha, "speed")], ("", 0, "33 32 45"))   # страница ничего не стирает
        self.assertEqual(self.put(self.alpha, "speed", text="33").status_code, 200)   # а правка приводит к новому формату
        self.assertNotIn("g-sp err", self.page())

    def test_clearing_and_replacing_a_slot(self):
        self.put(self.alpha, "bio", item="assault", level=12)
        self.put(self.alpha, "bio", item="zivk", level=15)
        self.assertEqual(self.stored()[(self.alpha, "bio")], ("zivk", 15, ""))
        r = self.put(self.alpha, "bio", item="")
        self.assertEqual(r.get_json(), {"ok": True, "item": "", "level": 0, "text": ""})
        self.assertEqual(self.stored(), {})

    def test_bad_input_is_rejected_and_nothing_is_saved(self):
        cases = [
            dict(col="bio", item="gauss", level=15),        # предмет из чужой категории
            dict(col="gauss", item="nonexistent", level=1),
            dict(col="gauss", item="gauss", level=16),
            dict(col="gauss", item="gauss", level=-1),
            dict(col="gauss", item="gauss", level="abc"),
            dict(col="weapons", item="gauss", level=1),     # нет такой категории
        ]
        for data in cases:
            with self.subTest(data=data):
                r = self.put(self.alpha, **data)
                self.assertEqual(r.status_code, 400)
                self.assertFalse(r.get_json()["ok"])
        self.assertEqual(self.put(999999, "gauss", item="gauss", level=1).status_code, 400)
        self.assertEqual(self.client.post("/gear/set", base_url=LOCAL, data={"col": "gauss"}).status_code, 400)
        self.assertEqual(self.stored(), {})

    def test_gear_survives_leaving_and_returning_to_a_squad(self):
        self.put(self.alpha, "gauss", item="gauss", level=15)
        self.put(self.alpha, "speed", text="39")
        self.place(1, 1, "")                                   # Alpha убран из отряда
        self.assertNotIn(f"data-player='{self.alpha}' data-col='gauss'", self.page())
        self.place(2, 3, "Alpha")                              # вернулся в другой отряд
        html = self.page()
        self.assertIn("border-color:#CC9933", self.slot_tag(html, self.alpha, "gauss"))
        self.assertEqual(self.speed_value(html, self.alpha), "39")
        self.assertIn("Отряд 2", html)

    def swap(self, a, b):
        r = self.client.post("/squads/cells/swap", base_url=LOCAL,
                             data={"a_squad": a[0], "a_slot": a[1], "b_squad": b[0], "b_slot": b[1]})
        self.assertEqual(r.status_code, 204, r.get_data(as_text=True))

    def fill_known_gear(self):
        self.put(self.alpha, "gauss", item="gauss", level=15)
        self.put(self.alpha, "speed", text="39")
        self.put(self.bravo, "speed", text="25")
        self.put(self.bravo, "hammer", item="hammer", level=3)
        self.put(self.subby, "heavy", item="tank", level=9)
        return self.stored()

    def test_gear_follows_players_when_swapping_cells(self):
        before = self.fill_known_gear()
        self.swap((1, 1), (1, 2))                              # Alpha и Bravo поменялись местами в отряде
        self.assertEqual(self.stored(), before)
        html = self.page()
        self.assertIn("border-color:#CC9933", self.slot_tag(html, self.alpha, "gauss"))
        self.assertIn(">+15</span>", self.slot_tag(html, self.alpha, "gauss"))
        self.assertIn("class='g-slot empty'", self.slot_tag(html, self.bravo, "gauss"))
        self.assertIn(">+3</span>", self.slot_tag(html, self.bravo, "hammer"))
        self.assertEqual(self.speed_value(html, self.alpha), "39")      # скорость тоже едет с игроком
        self.assertEqual(self.speed_value(html, self.bravo), "25")
        self.assertEqual(self.speed_value(html, self.subby), "")

        self.swap((1, 1), (99, 1))                             # Bravo ↔ Subby: между отрядом 1 и Чемпионами
        self.assertEqual(self.stored(), before)
        html = self.page()
        champions = html.index("Чемпионы")
        self.assertGreater(html.index(f"data-player='{self.bravo}' data-col='hammer'"), champions)
        self.assertLess(html.index(f"data-player='{self.subby}' data-col='heavy'"), champions)
        self.assertIn(">+9</span>", self.slot_tag(html, self.subby, "heavy"))
        self.assertIn(">+3</span>", self.slot_tag(html, self.bravo, "hammer"))
        self.assertEqual(self.speed_value(html, self.bravo), "25")
        self.assertEqual(self.speed_value(html, self.alpha), "39")

    def test_gear_follows_a_player_moved_to_an_empty_slot(self):
        before = self.fill_known_gear()
        self.swap((1, 1), (4, 3))                              # Alpha уехал в пустую ячейку отряда 4
        self.assertEqual(self.stored(), before)
        html = self.page()
        self.assertIn("Отряд 4", html)
        self.assertIn(">+15</span>", self.slot_tag(html, self.alpha, "gauss"))

    def test_gear_survives_remove_command_and_adding_back(self):
        before = self.fill_known_gear()
        result = player_store.unbind_nick_everywhere(self.db, GUILD, "Alpha")      # /remove из бота
        self.assertEqual(result.status, "removed")
        self.assertNotIn(f"data-player='{self.alpha}' data-col='gauss'", self.page())
        self.assertEqual(self.stored(), before)                # в базе шмот никуда не делся
        self.place(3, 1, "Alpha")                              # снова добавили на доске отрядов
        self.assertEqual(self.pid("Alpha"), self.alpha)         # тот же игрок, а не новая запись
        html = self.page()
        self.assertIn(">+15</span>", self.slot_tag(html, self.alpha, "gauss"))
        self.assertEqual(self.speed_value(html, self.alpha), "39")
        self.assertIn("Отряд 3", html)

    def test_table_is_stretched_like_players_with_sticky_header(self):
        html = self.page()
        self.assertIn("class='panel g-panel'", html)
        self.assertNotRegex(html, r"\.g-panel\{width:fit-content")          # раньше таблица сжималась по содержимому
        self.assertRegex(html, r"\.gt\{width:100%;table-layout:fixed\}")    # фиксированная раскладка: ширины колонок заданы
        self.assertRegex(html, r"\.gt td\.g-f,\.gt th\.g-f\{width:25%")      # пустая колонка справа уже ников → блок категорий правее центра
        self.assertRegex(html, r"\.g-slot:not\(\.empty\)\{border-width:1\.5px\}")   # рамка заполненных слотов тоньше пустых
        self.assertNotRegex(html, r"\.gt td\.nick,\.gt th\.nick\{[^}]*width:")   # у ника нет своей ширины
        self.assertRegex(html, r"\.g-panel\{overflow:visible\}")            # без этого шапка не залипает
        self.assertRegex(html, r"\.gt thead th\{top:50px")                  # под верхней панелью, как в «Бафах»
        self.assertNotIn("class='scroll'", html)                            # обёртка со своим скроллом ломала бы sticky
        self.assertIn("<main class=\"wrap layout-players\">", html)         # ширина страницы как у «Игроков»
        self.assertEqual(html.count("<th class='g-f'></th>"), 1)           # пустая колонка справа
        self.assertEqual(html.count("<td class='g-f'></td>"), 3)           # по одной на игрока

    def test_speed_is_centered_under_its_header(self):
        html = self.page()
        self.assertRegex(html, r"\.gt td\.g-w,\.gt th\.g-w\{[^}]*text-align:center")
        self.assertRegex(html, r"\.g-sp\{[^}]*margin:0 auto[^}]*text-align:center")

    def test_item_names_are_renamed(self):
        self.assertEqual(gear.ITEMS["assault"].name, "Штурма")
        self.assertEqual(gear.ITEMS["zivk"].name, "Зивка")
        self.assertEqual(gear.ITEMS["tank"].name, "SBA ТАНК")
        self.assertEqual(gear.ITEMS["chief"].name, "Вождь")
        self.put(self.alpha, "bio", item="assault", level=5)
        self.put(self.alpha, "heavy", item="tank", level=7)
        html = self.page()
        self.assertIn("Био: Штурма +5", html)
        self.assertIn("Тяж: SBA ТАНК +7", html)
        self.assertIn('"name": "Зивка"', html)                              # окно выбора берёт имена из конфига

    def squad_order_on_squads_page(self):
        """Ники в порядке «отряд → место», как их видно на странице «Отряды»."""
        r = self.client.get("/squads", base_url=LOCAL)
        cells = re.findall(r"data-squad='(\d+)' data-slot='(\d+)' value='([^']*)'", r.get_data(as_text=True))
        by_pos = {(int(s), int(n)): v for s, n, v in cells if v}
        key = lambda pos: (7 if pos[0] == 99 else pos[0], pos[1])
        return [by_pos[pos] for pos in sorted(by_pos, key=key)]

    def test_players_inside_squads_follow_the_squads_page_order_not_alphabet(self):
        self.place(1, 1, "Zed")                                # в отряде 1 на первом месте — «Z», не «A»
        self.place(1, 2, "Mike")
        self.place(1, 3, "Alpha")
        self.place(1, 5, "Bravo")                              # пустые места пропускаются
        self.place(2, 1, "yankee")
        self.place(2, 4, "Xray")
        self.place(2, 2, "Charlie")
        self.place(99, 3, "Tango")
        self.place(99, 1, "Subby")
        expected = self.squad_order_on_squads_page()
        self.assertEqual(expected, ["Zed", "Mike", "Alpha", "Bravo", "yankee", "Charlie", "Xray", "Subby", "Tango"])
        html = self.page()
        shown = re.findall(r"<tr class='player-row'><td class='nick'><a [^>]*>([^<]*)</a>", html)
        self.assertEqual(shown, expected)                       # Шмот = Отряды, до последнего игрока
        self.assertLess(html.index("Отряд 1"), html.index(">Zed</a>"))
        self.assertLess(html.index("Отряд 2"), html.index(">yankee</a>"))
        self.assertLess(html.index("Чемпионы"), html.index(">Subby</a>"))

    def test_order_follows_a_swap_on_the_squads_page(self):
        self.place(1, 3, "Charlie")
        self.swap((1, 1), (1, 3))                               # Alpha ↔ Charlie
        shown = re.findall(r"<tr class='player-row'><td class='nick'><a [^>]*>([^<]*)</a>", self.page())
        self.assertEqual(shown, self.squad_order_on_squads_page())
        self.assertEqual(shown[:3], ["Charlie", "Bravo", "Alpha"])

    def test_nick_is_html_escaped(self):
        self.place(3, 1, "<b>x</b>")
        html = self.page()
        self.assertNotIn("<b>x</b>", html)
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", html)

    def test_icons_are_served_only_for_known_files(self):
        for item in gear.ITEMS.values():
            r = self.client.get(f"/gear/icon/{item.icon}", base_url=LOCAL)
            self.assertEqual(r.status_code, 200, item.icon)
            self.assertTrue(r.mimetype.startswith("image/"))
        for name in ("nope.webp", "..%2Fgear.py", "../gear.py"):
            self.assertEqual(self.client.get(f"/gear/icon/{name}", base_url=LOCAL).status_code, 404, name)

    def test_config_is_consistent(self):
        keys = {c.key for c in gear.COLUMNS}
        self.assertEqual(keys, {"gauss", "hammer", "flamer", "bio", "heavy", "speed"})
        self.assertEqual([c.key for c in gear.COLUMNS], ["gauss", "hammer", "bio", "heavy", "flamer", "speed"])
        self.assertEqual(gear.COLUMN_BY_KEY["bio"].items, ("assault", "zivk"))          # порядок в окне выбора
        self.assertEqual(gear.COLUMN_BY_KEY["heavy"].items, ("tank", "chief"))
        self.assertEqual(gear.COLUMN_BY_KEY["gauss"].title, "Снайпы")               # ключ gauss — из базы, заголовок новый
        self.assertEqual(gear.COLUMN_BY_KEY["gauss"].items, ("gauss", "sniper1", "sniper2", "sniper3"))
        used = {k for c in gear.COLUMNS for k in c.items}
        self.assertEqual(used, set(gear.ITEMS))
        for item in gear.ITEMS.values():
            self.assertIn(item.rarity, gear.RARITY)
            self.assertTrue((gear.ICON_DIR / item.icon).is_file(), item.icon)
        for key, rarity in (("gauss", "legendary"), ("hammer", "legendary"), ("assault", "legendary"),
                            ("zivk", "legendary"), ("flamer", "exceptional"), ("tank", "exceptional"),
                            ("chief", "exceptional"), ("sniper1", "exceptional"), ("sniper2", "exceptional"),
                            ("sniper3", "exceptional")):
            self.assertEqual(gear.ITEMS[key].rarity, rarity, key)

    def test_new_snipers_are_exceptional_red_and_live_in_the_snipers_column(self):
        for key in ("sniper1", "sniper2", "sniper3"):
            with self.subTest(item=key):
                r = self.put(self.alpha, "gauss", item=key, level=15)
                self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
                self.assertEqual(r.get_json(), {"ok": True, "item": key, "level": 15, "text": ""})
                tag = self.slot_tag(self.page(), self.alpha, "gauss")
                self.assertIn("border-color:#933", tag)                        # исключительный — красная рамка
                self.assertIn(f"/gear/icon/{key}.webp", tag)
                self.assertIn("Снайпы: ", tag)                                  # подпись слота — по новому названию
                self.assertEqual(self.client.get(f"/gear/icon/{key}.webp", base_url=LOCAL).status_code, 200)
        self.put(self.alpha, "gauss", item="gauss", level=15)                   # прежний Гаусс остался золотым
        self.assertIn("border-color:#CC9933", self.slot_tag(self.page(), self.alpha, "gauss"))
        for col in ("hammer", "bio", "heavy", "flamer"):                       # в чужие категории снайперов не поставить
            self.assertEqual(self.put(self.alpha, col, item="sniper1", level=1).status_code, 400, col)
        self.assertIn('"items": ["gauss", "sniper1", "sniper2", "sniper3"]', self.page())   # порядок в окне выбора

    def test_empty_roster_page_still_renders(self):
        for nick, (squad, slot) in {"Alpha": (1, 1), "Bravo": (1, 2), "Subby": (99, 1)}.items():
            self.place(squad, slot, "")
        self.assertIn("В отрядах пока никого нет", self.page())

    # --------------------------------------------------------------- слияние дублей
    def test_merging_duplicates_moves_gear_to_the_survivor(self):
        self.put(self.alpha, "gauss", item="gauss", level=15)         # у выжившего
        self.put(self.bravo, "gauss", item="gauss", level=1)          # у дубля — проиграет
        self.put(self.bravo, "hammer", item="hammer", level=7)        # а этого у выжившего нет — переедет
        self.put(self.bravo, "speed", text="44")
        with closing(player_store.connect(self.db)) as con:
            con.execute("BEGIN IMMEDIATE")
            player_store._merge_player_everywhere(con, self.alpha, self.bravo)
            con.execute("COMMIT")
        self.assertEqual(self.stored(), {(self.alpha, "gauss"): ("gauss", 15, ""),
                                         (self.alpha, "hammer"): ("hammer", 7, ""),
                                         (self.alpha, "speed"): ("", 0, "44")})

    def test_merging_works_before_the_gear_table_exists(self):
        with closing(player_store.connect(self.db)) as con:
            con.execute("DROP TABLE IF EXISTS player_gear")
            player_store._merge_player_everywhere(con, self.alpha, self.bravo)
            self.assertIsNone(con.execute("SELECT 1 FROM players WHERE id=?", (self.bravo,)).fetchone())


if __name__ == "__main__":
    unittest.main()
