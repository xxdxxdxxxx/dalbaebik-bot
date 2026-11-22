from __future__ import annotations

import json
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

    def board_rows(self, html):
        """Места доски: {(отряд, место): (id игрока или '', html строки)}."""
        found = re.findall(r"<div class='g-row(?: filled)?' data-squad='(\d+)' data-slot='(\d+)' data-player='(\d*)'[^>]*>(.*?)(?=<div class='g-row|<div class='gb-h|</div></div>)",
                           html, flags=re.S)
        return {(int(s), int(n)): (pid, body.removesuffix("</div>")) for s, n, pid, body in found}   # без закрывающего тега самой строки

    def row_pos(self, html, player):
        """(отряд, место), где игрок стоит на доске «Шмот»."""
        for pos, (pid, _) in self.board_rows(html).items():
            if pid == str(player):
                return pos
        return None

    def board_nicks(self, html):
        return {pos: re.search(r"<a [^>]*>([^<]*)</a>", body).group(1)
                for pos, (pid, body) in self.board_rows(html).items() if pid}

    # ------------------------------------------------------------------ страница
    def test_page_lists_squad_players_with_empty_slots_and_nav_link(self):
        html = self.page()
        self.assertIn(">Шмот</a>", html)
        self.assertRegex(html, r'class="active" aria-current="page" href="/gear"')
        heads = re.findall(r"<div class='gb-h'><span class='gb-t'>[^<]*</span>((?:<span class='gb-l'[^>]*>[^<]*</span>)+)", html)
        self.assertEqual(len(heads), 7)                                          # 6 отрядов + «Замены»
        titles = re.findall(r"title='([^']+)'>[^<]+</span>", heads[0])
        self.assertEqual(titles, ["Снайпы", "Кувалда", "Био", "Тяж", "Огнемёт", "Скорость"])   # порядок слева направо
        self.assertEqual(re.findall(r">([^<]+)</span>", heads[0]), ["Снайп", "Кувал", "Био", "Тяж", "Огнем", "Скор"])   # подписи короткие
        for title in ("Отряд 1", "Отряд 6", "Замены"):
            self.assertIn(f"<span class='gb-t'>{title}</span>", html)
        self.assertNotIn("Чемпионы", html)                                        # справа «Замены», как на странице «Отряды»
        for nick in ("Alpha", "Bravo", "Subby"):
            self.assertIn(f">{nick}</a>", html)
        # 3 игрока × 5 слотов с предметами, все пустые, и 3 поля скорости
        self.assertEqual(len(re.findall(r"class='g-slot empty'", html)), 15)
        self.assertEqual(len(re.findall(r"class='g-sp'", html)), 3)
        self.assertEqual(self.stored(), {})

    def test_cells_follow_header_order_in_every_row(self):
        row = next(body for pid, body in self.board_rows(self.page()).values() if pid)
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
        self.assertIn(">Гаусс</span>", gauss)                       # в «Снайпах» вместо «+15» — название
        self.assertNotIn("g-lv", gauss)
        self.assertIn("Снайпы: Гаусс +15", gauss)                  # заточка при этом сохранена и видна в подсказке
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

    # ------------------------------------------------------- название вместо заточки в «Снайпах»
    def test_sniper_slots_show_the_name_instead_of_the_level(self):
        for item, name in (("gauss", "Гаусс"), ("sniper1", "EBR"), ("sniper2", "pZB"), ("sniper3", "СВД-м")):
            for level in (0, 7, 15):
                with self.subTest(item=item, level=level):
                    self.assertEqual(self.put(self.alpha, "gauss", item=item, level=level).status_code, 200)
                    tag = self.slot_tag(self.page(), self.alpha, "gauss")
                    self.assertIn(f"<span class='g-nm'>{name}</span>", tag)     # название — и при +0, и при +15
                    self.assertNotIn("g-lv", tag)                               # «+N» в слоте не рисуется
                    self.assertNotIn(f">+{level}</span>", tag)
                    self.assertIn(f"Снайпы: {name}" + (f" +{level}" if level else "") + "'", tag)   # заточка — в подсказке

    def test_name_label_is_only_in_the_sniper_column(self):
        for col, item in (("hammer", "hammer"), ("bio", "zivk"), ("heavy", "chief"), ("flamer", "flamer")):
            self.put(self.alpha, col, item=item, level=12)
        html = self.page()
        for col in ("hammer", "bio", "heavy", "flamer"):
            with self.subTest(col=col):
                tag = self.slot_tag(html, self.alpha, col)
                self.assertIn(">+12</span>", tag)                               # там по-прежнему заточка
                self.assertNotIn("g-nm", tag)

    def test_name_label_not_drawn_for_unset_or_empty_sniper_slot(self):
        self.put(self.alpha, "gauss", item="none")
        html = self.page()
        self.assertNotIn("g-nm", self.slot_tag(html, self.alpha, "gauss"))      # «Пусто» — пустой слот без «+»
        self.assertNotIn("g-nm", self.slot_tag(html, self.bravo, "gauss"))      # «не указано» — «+»

    def test_name_label_is_white_centered_at_the_bottom_without_outline(self):
        css = re.search(r"\.g-nm\{([^}]*)\}", self.page()).group(1)
        self.assertIn("color:#fff", css)
        self.assertIn("bottom:", css)                                           # внизу иконки, где была заточка
        self.assertIn("left:0;right:0", css)                                    # на всю ширину слота
        self.assertIn("text-align:center", css)                                 # по центру
        self.assertNotIn("text-shadow", css)                                    # без чёрной обводки
        self.assertNotIn("-webkit-text-stroke", css)
        self.assertNotIn("monospace", css)                                      # обычный шрифт страницы, не моноширинный

    def test_page_script_draws_the_name_for_show_name_columns(self):
        html = self.page()
        cfg = json.loads(re.search(r"id='gear-cfg'>(.*?)</script>", html, re.S).group(1))
        show = {c["key"]: c["showName"] for c in cfg["columns"]}
        self.assertEqual(show, {"gauss": True, "hammer": False, "bio": False, "heavy": False, "flamer": False, "speed": False})
        self.assertIn("if(col.showName)btn.append(el('span','g-nm',it.name))", html)   # перерисовка после выбора в окне

    # ------------------------------------------------------- «Пусто» = предмета нет
    def test_empty_choice_is_saved_and_shown_as_plain_slot_without_plus(self):
        r = self.put(self.alpha, "hammer", item=gear.NONE_KEY)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(r.get_json(), {"ok": True, "item": "none", "level": 0, "text": ""})
        self.assertEqual(self.stored(), {(self.alpha, "hammer"): ("none", 0, "")})   # запись есть — «нет» помнится
        html = self.page()
        tag = self.slot_tag(html, self.alpha, "hammer")
        self.assertIn("class='g-slot none'", tag)                   # пустой слот
        self.assertNotIn("g-plus", tag)                             # без «+»
        self.assertNotIn("<img", tag)
        self.assertNotIn("g-lv", tag)
        self.assertNotIn("border-color", tag)                       # рамка без цвета редкости
        self.assertIn("Кувалда: нет", tag)                          # понятно и по подсказке
        self.assertIn("class='g-slot empty'", self.slot_tag(html, self.alpha, "gauss"))   # «не указано» — по-прежнему с «+»
        self.assertIn("g-plus", self.slot_tag(html, self.alpha, "gauss"))
        self.assertIn("Снайпы: не указано", self.slot_tag(html, self.alpha, "gauss"))
        self.assertEqual(len(re.findall(r"class='g-slot none'", html)), 1)
        self.assertEqual(len(re.findall(r"class='g-slot empty'", html)), 14)

    def test_empty_choice_works_in_every_item_column_and_ignores_level(self):
        for col in ("gauss", "hammer", "bio", "heavy", "flamer"):
            with self.subTest(col=col):
                r = self.put(self.alpha, col, item="none", level=99)       # заточка у «нет» не проверяется и не хранится
                self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
                self.assertEqual(r.get_json(), {"ok": True, "item": "none", "level": 0, "text": ""})
                self.assertEqual(self.stored()[(self.alpha, col)], ("none", 0, ""))
        self.assertEqual(len(re.findall(r"class='g-slot none'", self.page())), 5)

    def test_empty_choice_can_be_replaced_by_item_and_reset_to_unset(self):
        self.put(self.alpha, "bio", item="none")
        self.put(self.alpha, "bio", item="zivk", level=9)                  # потом выяснилось, что предмет есть
        self.assertEqual(self.stored()[(self.alpha, "bio")], ("zivk", 9, ""))
        self.put(self.alpha, "bio", item="none")                           # и снова «нет»: заточка не остаётся
        self.assertEqual(self.stored()[(self.alpha, "bio")], ("none", 0, ""))
        r = self.put(self.alpha, "bio", item="")                           # «Сбросить» — снова «не указано»
        self.assertEqual(r.get_json(), {"ok": True, "item": "", "level": 0, "text": ""})
        self.assertEqual(self.stored(), {})
        self.assertIn("class='g-slot empty'", self.slot_tag(self.page(), self.alpha, "bio"))

    def test_empty_choice_does_not_touch_speed_or_other_players(self):
        self.put(self.alpha, "speed", text="39")
        self.put(self.alpha, "hammer", item="none")
        self.assertEqual(self.stored()[(self.alpha, "speed")], ("", 0, "39"))
        self.assertEqual(self.speed_value(self.page(), self.alpha), "39")
        self.assertIn("class='g-slot empty'", self.slot_tag(self.page(), self.bravo, "hammer"))

    def test_only_the_none_marker_is_accepted_besides_real_items(self):
        for bad in ("none ", "None", "NONE", "nothing", "empty", "-"):
            with self.subTest(item=bad):
                r = self.put(self.alpha, "hammer", item=bad)
                if bad.strip() == "none":                                     # пробелы вокруг обрезаются, как у остальных ключей
                    self.assertEqual(r.status_code, 200)
                else:
                    self.assertEqual(r.status_code, 400)
        self.assertEqual(self.stored(), {(self.alpha, "hammer"): ("none", 0, "")})

    def test_empty_choice_follows_the_player_when_swapping_cells(self):
        self.put(self.alpha, "flamer", item="none")
        self.put(self.alpha, "gauss", item="gauss", level=15)
        before = self.stored()
        self.swap((1, 1), (1, 2))
        self.assertEqual(self.stored(), before)
        html = self.page()
        self.assertIn("class='g-slot none'", self.slot_tag(html, self.alpha, "flamer"))
        self.assertIn("class='g-slot empty'", self.slot_tag(html, self.bravo, "flamer"))

    def test_merging_duplicates_moves_the_empty_choice_too(self):
        self.put(self.bravo, "heavy", item="none")
        with closing(player_store.connect(self.db)) as con:
            con.execute("BEGIN IMMEDIATE")
            player_store._merge_player_everywhere(con, self.alpha, self.bravo)
            con.execute("COMMIT")
        self.assertEqual(self.stored(), {(self.alpha, "heavy"): ("none", 0, "")})

    def test_page_script_knows_the_none_marker_and_slot_styles(self):
        html = self.page()
        self.assertIn('"noneKey": "none"', html)
        self.assertRegex(html, r"\.g-slot\.none\{[^}]*border-style:solid")            # сплошная рамка, не пунктир
        none_css = re.search(r"\.g-slot\.none\{([^}]*)\}", html).group(1)
        self.assertNotIn("background", none_css)                                      # фон не переопределён → штриховка как у остальных слотов
        self.assertNotRegex(html, r"\.g-slot\.none:hover")                             # и при наведении тоже обычная
        self.assertIn("border-color:rgba(127,127,127,.3)", none_css)                  # рамка «Пусто» прежняя: сплошная тонкая
        self.assertIn("border-width:1px", none_css)
        self.assertRegex(html, r"\.g-slot\{[^}]*background:repeating-linear-gradient")   # штриховка лежит в базовом .g-slot
        self.assertRegex(html, r"\.g-slot\.empty\{border-style:dashed\}")             # «не указано» осталось пунктирным
        self.assertIn("Сбросить", html)                                               # возврат к «не указано»

    def test_slot_background_is_the_original_dark_one_set_via_variables(self):
        html = self.page()
        self.assertRegex(html, r"\.g-slot\{--g-bg:#0f1419;--g-bg-hi:#18202a;")           # заливка и наведение — через переменные
        self.assertIn("),var(--g-bg);color:#fff;cursor:pointer}", html)                 # штриховка лежит поверх var(--g-bg), цвет не зашит

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
        self.assertEqual(self.row_pos(html, self.alpha), (2, 3))

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
        self.assertIn("Снайпы: Гаусс +15", self.slot_tag(html, self.alpha, "gauss"))
        self.assertIn("class='g-slot empty'", self.slot_tag(html, self.bravo, "gauss"))
        self.assertIn(">+3</span>", self.slot_tag(html, self.bravo, "hammer"))
        self.assertEqual(self.speed_value(html, self.alpha), "39")      # скорость тоже едет с игроком
        self.assertEqual(self.speed_value(html, self.bravo), "25")
        self.assertEqual(self.speed_value(html, self.subby), "")
        self.assertEqual((self.row_pos(html, self.alpha), self.row_pos(html, self.bravo)), ((1, 2), (1, 1)))

        self.swap((1, 1), (99, 1))                             # Bravo ↔ Subby: между отрядом 1 и «Заменами»
        self.assertEqual(self.stored(), before)
        html = self.page()
        self.assertEqual(self.row_pos(html, self.bravo), (99, 1))
        self.assertEqual(self.row_pos(html, self.subby), (1, 1))
        self.assertIn(">+9</span>", self.slot_tag(html, self.subby, "heavy"))
        self.assertIn(">+3</span>", self.slot_tag(html, self.bravo, "hammer"))
        self.assertEqual(self.speed_value(html, self.bravo), "25")
        self.assertEqual(self.speed_value(html, self.alpha), "39")

    def test_gear_follows_a_player_moved_to_an_empty_slot(self):
        before = self.fill_known_gear()
        self.swap((1, 1), (4, 3))                              # Alpha уехал в пустую ячейку отряда 4
        self.assertEqual(self.stored(), before)
        html = self.page()
        self.assertEqual(self.row_pos(html, self.alpha), (4, 3))
        self.assertEqual(self.board_rows(html)[(1, 1)][0], "")
        self.assertIn("Снайпы: Гаусс +15", self.slot_tag(html, self.alpha, "gauss"))

    def test_gear_survives_remove_command_and_adding_back(self):
        before = self.fill_known_gear()
        result = player_store.unbind_nick_everywhere(self.db, GUILD, "Alpha")      # /remove из бота
        self.assertEqual(result.status, "removed")
        self.assertNotIn(f"data-player='{self.alpha}' data-col='gauss'", self.page())
        self.assertEqual(self.stored(), before)                # в базе шмот никуда не делся
        self.place(3, 1, "Alpha")                              # снова добавили на доске отрядов
        self.assertEqual(self.pid("Alpha"), self.alpha)         # тот же игрок, а не новая запись
        html = self.page()
        self.assertIn("Снайпы: Гаусс +15", self.slot_tag(html, self.alpha, "gauss"))
        self.assertEqual(self.speed_value(html, self.alpha), "39")
        self.assertEqual(self.row_pos(html, self.alpha), (3, 1))

    def test_board_is_sized_to_fit_the_screen_without_scrolling(self):
        html = self.page()
        self.assertIn("class='panel g-panel'", html)
        self.assertIn('<body class="g-wide">', html)                         # страница шире остальных: четыре колонки со слотами
        self.assertRegex(html, r"body\.g-wide\{--w:\d+px\}")
        self.assertIn('<main class="wrap">', html)
        self.assertRegex(html, r"#gear-root\{container-type:inline-size;--row:clamp\([^;]*100vh")   # высота строки — от высоты окна
        self.assertRegex(html, r"--gs:clamp\([^;]*25cqw")                      # размер слота — от ширины колонки
        self.assertRegex(html, r"\.g-panel\{margin:0;overflow-x:auto;overflow-y:hidden\}")   # вертикальной прокрутки нет
        self.assertNotIn("<table", html)                                     # таблицы и её залипающей шапки больше нет
        self.assertRegex(html, r"\.g-slot:not\(\.empty\)\{border-width:1\.5px\}")   # рамка заполненных слотов тоньше пустых

    def test_speed_input_is_centered_in_its_column(self):
        html = self.page()
        self.assertRegex(html, r"\.g-sp\{[^}]*margin:0;[^}]*text-align:center")
        self.assertRegex(html, r"\.gb-l\{[^}]*text-align:center")             # и подпись над ним тоже по центру

    def test_item_names_are_renamed(self):
        self.assertEqual(gear.ITEMS["assault"].name, "Штурма")
        self.assertEqual(gear.ITEMS["zivk"].name, "Зивка")
        self.assertEqual(gear.ITEMS["tank"].name, "SBA ТАНК")
        self.assertEqual(gear.ITEMS["chief"].name, "Вождь")
        self.assertEqual(gear.ITEMS["sniper1"].name, "EBR")
        self.assertEqual(gear.ITEMS["sniper2"].name, "pZB")
        self.assertEqual(gear.ITEMS["sniper3"].name, "СВД-м")
        self.put(self.alpha, "bio", item="assault", level=5)
        self.put(self.alpha, "heavy", item="tank", level=7)
        html = self.page()
        self.assertIn("Био: Штурма +5", html)
        self.assertIn("Тяж: SBA ТАНК +7", html)
        self.assertIn('"name": "Зивка"', html)                              # окно выбора берёт имена из конфига
        self.put(self.alpha, "gauss", item="sniper3", level=4)
        html = self.page()
        self.assertIn("Снайпы: СВД-м +4", html)
        self.assertNotIn("Снайп 1", html)                                    # старых временных названий нет нигде
        self.assertNotIn("Снайп 2", html)
        self.assertNotIn("Снайп 3", html)
        for name in ("EBR", "pZB", "СВД-м"):
            self.assertIn(f'"name": "{name}"', html)

    def squad_order_on_squads_page(self):
        """Ники в порядке «отряд → место», как их видно на странице «Отряды»."""
        r = self.client.get("/squads", base_url=LOCAL)
        cells = re.findall(r"data-squad='(\d+)' data-slot='(\d+)' value='([^']*)'", r.get_data(as_text=True))
        by_pos = {(int(s), int(n)): v for s, n, v in cells if v}
        key = lambda pos: (7 if pos[0] == 99 else pos[0], pos[1])
        return [by_pos[pos] for pos in sorted(by_pos, key=key)]

    def squads_page_positions(self):
        """Места ников на странице «Отряды»: {(отряд, место): ник}."""
        r = self.client.get("/squads", base_url=LOCAL)
        cells = re.findall(r"data-squad='(\d+)' data-slot='(\d+)' value='([^']*)'", r.get_data(as_text=True))
        return {(int(s), int(n)): v for s, n, v in cells if v}

    def test_every_player_stands_on_the_same_place_as_on_the_squads_page(self):
        self.place(1, 1, "Zed")                                # в отряде 1 на первом месте — «Z», не «A»
        self.place(1, 2, "Mike")
        self.place(1, 3, "Alpha")
        self.place(1, 5, "Bravo")                              # пустые места остаются пустыми
        self.place(2, 1, "yankee")
        self.place(2, 4, "Xray")
        self.place(2, 2, "Charlie")
        self.place(99, 3, "Tango")
        self.place(99, 11, "Subby")                            # последнее место «Замен»
        expected = self.squads_page_positions()
        self.assertEqual(expected[(1, 1)], "Zed")
        self.assertEqual(expected[(99, 11)], "Subby")
        shown = self.board_nicks(self.page())
        self.assertEqual(shown, expected)                       # Шмот = Отряды: те же ники на тех же местах
        self.assertNotIn("Без места на доске", self.page())

    def test_order_follows_a_swap_on_the_squads_page(self):
        self.place(1, 3, "Charlie")
        self.swap((1, 1), (1, 3))                               # Alpha ↔ Charlie
        shown = self.board_nicks(self.page())
        self.assertEqual(shown, self.squads_page_positions())
        self.assertEqual([shown[(1, n)] for n in (1, 2, 3)], ["Charlie", "Bravo", "Alpha"])

    # ------------------------------------------------------------- раскладка доски
    def test_board_has_the_squads_page_layout(self):
        html = self.page()
        rows = self.board_rows(html)
        expected = {(sq, n) for sq in range(1, 7) for n in range(1, 6)} | {(99, n) for n in range(1, 12)}
        self.assertEqual(set(rows), expected)                   # 6 отрядов по 5 мест и 11 мест «Замен», ничего лишнего
        self.assertEqual(html.count("class='gb-h'"), 7)
        # колонки слева направо: 1 над 4, 2 над 5, 3 над 6, «Замены»; внутри колонки — 12 ячеек сверху вниз
        board = re.search(r"<div id='gear-board' class='gb'>(.*?)</div></div><div id='gear-pop'", html, flags=re.S).group(1)
        order = re.findall(r"<div class='gb-h'><span class='gb-t'>([^<]*)</span>|<div class='g-row(?: filled)?' data-squad='(\d+)' data-slot='(\d+)'", board)
        self.assertEqual(len(order), 4 * 12)
        flat = [t or f"{s}:{n}" for t, s, n in order]
        self.assertEqual(flat[:12], ["Отряд 1", "1:1", "1:2", "1:3", "1:4", "1:5", "Отряд 4", "4:1", "4:2", "4:3", "4:4", "4:5"])
        self.assertEqual(flat[36:], ["Замены"] + [f"99:{n}" for n in range(1, 12)])
        self.assertIn("grid-template-rows:repeat(12,var(--row))", html)
        self.assertIn("grid-template-columns:repeat(4,minmax(0,1fr))", html)
        self.assertIn("grid-auto-flow:column", html)

    def test_player_row_is_nick_then_slots_then_speed(self):
        html = self.page()
        body = self.board_rows(html)[(1, 1)][1]
        self.assertTrue(body.startswith("<div class='g-nick' draggable='true'"))     # ручка перетаскивания — ник
        self.assertIn(f"<a href='/player/{self.alpha}' draggable='false'>Alpha</a>", body)   # ссылка на игрока по-прежнему есть
        self.assertEqual(len(re.findall(r"class='g-slot", body)), 5)
        self.assertEqual(len(re.findall(r"class='g-sp", body)), 1)
        self.assertLess(body.index("g-nick"), body.index("g-slot"))
        self.assertLess(body.rindex("g-slot"), body.index("g-sp"))                  # скорость — последней

    def test_empty_places_are_plain_drop_targets(self):
        html = self.page()
        pid, body = self.board_rows(html)[(2, 3)]
        self.assertEqual((pid, body), ("", ""))                                     # пусто: ни ника, ни слотов
        self.assertIn("<div class='g-row' data-squad='2' data-slot='3' data-player=''", html)
        self.assertNotIn("draggable='true'", self.board_rows(html)[(2, 3)][1])

    def test_script_moves_nicks_with_the_squads_swap_endpoint(self):
        html = self.page()
        self.assertIn("fetch('/squads/cells/swap'", html)                           # тот же запрос, что на странице «Отряды»
        for part in ("a_squad:a.dataset.squad", "a_slot:a.dataset.slot", "b_squad:b.dataset.squad", "b_slot:b.dataset.slot"):
            self.assertIn(part, html)
        self.assertIn("function swapRows(a,b)", html)                               # строка целиком (ник, слоты, скорость) едет к новому месту
        self.assertIn("addEventListener('dragstart'", html)
        self.assertIn("addEventListener('drop'", html)

    def test_players_without_a_place_are_still_listed_below_the_board(self):
        with closing(player_store.connect(self.db)) as con:
            con.execute("UPDATE roster_memberships SET slot=NULL WHERE player_id=?", (self.bravo,))
            con.commit()
        self.put(self.bravo, "hammer", item="hammer", level=7)
        html = self.page()
        self.assertIn("Без места на доске", html)
        self.assertIsNone(self.row_pos(html, self.bravo))                           # на доске его нет
        extra = html[html.index("Без места на доске"):]
        self.assertIn(f"data-player='{self.bravo}' data-col='hammer'", extra)       # но шмот редактируется
        self.assertIn(">+7</span>", self.slot_tag(extra, self.bravo, "hammer"))
        self.assertEqual(self.board_nicks(html)[(1, 1)], "Alpha")

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
