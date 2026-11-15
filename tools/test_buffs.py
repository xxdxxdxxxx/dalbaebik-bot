from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import player_store
from tools import admin_ui, buffs

MSK = timezone(timedelta(hours=3))


def msk(day: int, hour: int, minute: int = 0, month: int = 11) -> datetime:
    """Момент по Москве в ноябре 2026 (12.11 — четверг)."""
    return datetime(2026, month, day, hour, minute, tzinfo=MSK)


class ParserTests(unittest.TestCase):
    def parse(self, text):
        p = buffs.parse_message(text)
        return p.nick, p.counts, p.status

    def test_real_clan_messages(self):
        cases = [
            ("KROWA_DASHA \n70 топотов\n12 убийц", "KROWA_DASHA", {"topot": 70, "killer": 12}),
            ("TergosX \n100 мин\n3 убийцы \n60 топотов", "TergosX", {"mine": 100, "killer": 3, "topot": 60}),
            ("Extanz\n3 убийцы", "Extanz", {"killer": 3}),
            ("DinGOBY\n100 топотов, 3 убийцы, 3 хилки, 3 смока", "DinGOBY",
             {"topot": 100, "killer": 3, "heal": 3, "smoke": 3}),
            ("DinGOBY\n3 подорожника\n2 дыма\n3 убийцы\n100 топотов", "DinGOBY",
             {"heal": 3, "smoke": 2, "killer": 3, "topot": 100}),
            ("MLoveX\n100 топотов\n10 убийц\n1 смок \n3 зеленки", "MLoveX",
             {"topot": 100, "killer": 10, "smoke": 1, "heal": 3}),
            ("Пельмешек\n100 топот \n5 убиц", "Пельмешек", {"topot": 100, "killer": 5}),
        ]
        for text, nick, counts in cases:
            with self.subTest(nick=nick):
                self.assertEqual(self.parse(text), (nick, counts, "ok"))

    def test_comma_separated_template(self):
        text = "Extanz\n100 топотов (это топоты), 10 шизей, 10 убийц, 3 хилки, 20 мин, 3 смока, 7 напалмов"
        self.assertEqual(self.parse(text), ("Extanz", {
            "topot": 100, "shiza": 10, "killer": 10, "heal": 3, "mine": 20, "smoke": 3, "napalm": 7}, "ok"))

    def test_template_variants(self):
        self.assertEqual(self.parse("1. Extanz\n2. 100 топотов, 3 убийцы")[:2], ("Extanz", {"topot": 100, "killer": 3}))
        self.assertEqual(self.parse("Ник: Extanz\nбафы: 5 мин, 2 дыма")[:2], ("Extanz", {"mine": 5, "smoke": 2}))
        self.assertEqual(self.parse("**Vasya**\n<@123> 3 убийцы")[:2], ("Vasya", {"killer": 3}))
        self.assertEqual(self.parse("Vasya\nтопоты 100, убийцы 5")[:2], ("Vasya", {"topot": 100, "killer": 5}))
        self.assertEqual(self.parse("Vasya\n100топотов 5х убийц")[:2], ("Vasya", {"topot": 100, "killer": 5}))

    def test_missing_nick_and_nicks_that_look_like_buffs(self):
        self.assertEqual(self.parse("100 топотов\n5 мин"), (None, {"topot": 100, "mine": 5}, "ok"))
        # «Шиза2008» — это ник, а не «2008 шиз»; подчёркивание в нике сохраняется
        self.assertEqual(self.parse("Шиза2008\n10 убийц")[:2], ("Шиза2008", {"killer": 10}))
        self.assertEqual(self.parse("KROWA_DASHA\n1 смок")[0], "KROWA_DASHA")

    def test_unknown_and_unparsed(self):
        p = buffs.parse_message("Vasya\n3 убийцы, 5 пушек")
        self.assertEqual((p.counts, p.unrecognized, p.status), ({"killer": 3}, ["5 пушек"], "partial"))
        for text in ("Привет всем", "Vasya", "Vasya\nубийцы", ""):
            with self.subTest(text=text):
                self.assertEqual(buffs.parse_message(text).status, "unparsed")

    def test_repeats_zero_and_limits(self):
        self.assertEqual(self.parse("Vasya\n50 топотов, 50 топотов")[1], {"topot": 100})
        self.assertEqual(self.parse("Vasya\n0 мин, 3 хилки")[1], {"heal": 3})
        p = buffs.parse_message("Vasya\n9999999 мин")
        self.assertEqual((p.counts, p.status), ({}, "unparsed"))

    def test_mine_is_not_minutes(self):
        self.assertIsNone(buffs.buff_key("минут"))
        self.assertIsNone(buffs.buff_key("минимум"))
        self.assertEqual(buffs.buff_key("мин"), "mine")


class IssueDayTests(unittest.TestCase):
    def test_issue_day_changes_at_five_in_the_morning(self):
        self.assertEqual(buffs.issue_day_for(msk(12, 19, 38)), "2026-11-12")
        self.assertEqual(buffs.issue_day_for(msk(13, 0, 30)), "2026-11-12")    # после полуночи это ещё КВ четверга
        self.assertEqual(buffs.issue_day_for(msk(13, 4, 59)), "2026-11-12")
        self.assertEqual(buffs.issue_day_for(msk(13, 5, 0)), "2026-11-13")
        self.assertEqual(buffs.issue_day_for(msk(13, 0, 30).astimezone(timezone.utc)), "2026-11-12")
        self.assertEqual(buffs.msk_date(msk(14, 2, 8)), "2026-11-14")


def rec(message_id, content, *, when, author="игрок", author_id=1, channel_id=500, edited=None):
    return {"message_id": message_id, "guild_id": 7, "channel_id": channel_id, "author_id": author_id,
            "author_name": author, "created_at": when, "edited_at": edited, "content": content}


# сообщения клана из примера: id, текст, время
CLAN = [
    (1, "KROWA_DASHA \n70 топотов\n12 убийц", msk(12, 19, 38)),
    (2, "TergosX \n100 мин\n3 убийцы \n60 топотов", msk(12, 19, 54)),
    (3, "Extanz\n3 убийцы", msk(12, 19, 56)),
    (4, "DinGOBY\n100 топотов, 3 убийцы, 3 хилки, 3 смока", msk(13, 18, 51)),
    (5, "MLoveX\n100 топотов\n10 убийц\n1 смок \n3 зеленки", msk(14, 2, 8)),
    (6, "Пельмешек\n100 топот \n5 убиц", msk(14, 17, 8)),
]


class BuffBase(unittest.TestCase):
    # при падении не печатаем страницу целиком
    def assertIn(self, member, container, msg=None):
        if isinstance(container, str) and msg is None and len(container) > 400:
            self.assertTrue(member in container, f"на странице нет: {member!r}")
        else:
            super().assertIn(member, container, msg)

    def assertNotIn(self, member, container, msg=None):
        if isinstance(container, str) and msg is None and len(container) > 400:
            self.assertTrue(member not in container, f"на странице есть лишнее: {member!r}")
        else:
            super().assertNotIn(member, container, msg)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "scan_stats.sqlite3"
        player_store.ensure_schema(self.db)
        buffs.set_channel(self.db, 7, 500)
        self.patch_db = patch.object(admin_ui, "DB_PATH", self.db)
        self.patch_db.start()
        self.now = msk(14, 20)      # суббота, 20:00 МСК — идёт КВ
        self.patch_now = patch.object(buffs, "_now", side_effect=lambda: self.now.astimezone(timezone.utc))
        self.patch_now.start()
        self.client = admin_ui.app.test_client()

    def tearDown(self):
        self.patch_now.stop()
        self.patch_db.stop()
        self.tmp.cleanup()

    def con(self):
        con = player_store.connect(self.db)
        buffs._schema(con)
        return con

    def add_player(self, nick, discord_id=None):
        with closing(self.con()) as con:
            con.execute("INSERT INTO players(canonical_nick) VALUES(?)", (nick,))
            pid = int(con.execute("SELECT last_insert_rowid()").fetchone()[0])
            if discord_id:
                con.execute("INSERT INTO discord_bindings(discord_id,player_id) VALUES(?,?)", (str(discord_id), pid))
        return pid

    def seed_clan(self, upto=None):
        for nick in ("KROWA_DASHA", "TergosX", "Extanz", "DinGOBY", "MLoveX", "Пельмешек"):
            if not self.player_exists(nick):
                self.add_player(nick)
        buffs.ingest_messages(self.db, [rec(i, t, when=w) for i, t, w in CLAN if upto is None or i <= upto])

    def player_exists(self, nick):
        with closing(self.con()) as con:
            return con.execute("SELECT 1 FROM players WHERE canonical_nick=?", (nick,)).fetchone() is not None

    def page(self, url="/buffs"):
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        return resp.get_data(as_text=True)

    def rows(self):
        """Ники на доске в порядке показа."""
        html = self.page()
        return [re.search(r"class='who'>(?:<a[^>]*>)?([^<]*)", r).group(1) for r in html.split("<tr class='brow")[1:]]

    def done_nicks(self):
        html = self.page()
        return [re.search(r"class='who'>(?:<a[^>]*>)?([^<]*)", r).group(1)
                for r in html.split("<tr class='brow")[1:] if r.startswith(" done")]

    def key_for(self, nick):
        for chunk in self.page().split("<tr class='brow")[1:]:
            if f">{nick}<" in chunk:
                return re.search(r"name='key' value='([^']*)'", chunk).group(1)
        raise AssertionError(f"{nick} нет на доске")

    def toggle(self, nick, issued=1, key=None):
        return self.client.post("/buffs/toggle", data={"key": key or self.key_for(nick), "issued": str(issued)},
                                headers={"Accept": "application/json"})

    def foot(self, row_id):
        html = self.page()
        row = html.split(f"id='{row_id}'")[1].split("</tr>")[0]
        return re.findall(r"<td class='bc'>(.*?)</td>", row)

    def history_rows(self):
        with closing(self.con()) as con:
            return [dict(r) for r in con.execute("SELECT * FROM buff_issued ORDER BY id")]


class IngestTests(BuffBase):
    def test_ingest_edit_delete_and_idempotency(self):
        first = buffs.ingest_messages(self.db, [rec(1, "Extanz\n3 убийцы", when=msk(12, 19, 56))])
        self.assertEqual(first["ok"], 1)
        again = buffs.ingest_messages(self.db, [rec(1, "Extanz\n3 убийцы", when=msk(12, 19, 56))])
        self.assertEqual((again["same"], again["ok"]), (1, 0))
        with closing(self.con()) as con:
            version = buffs._version(con, self.now)
        buffs.ingest_messages(self.db, [rec(1, "Extanz\n3 убийцы", when=msk(12, 19, 56))])
        with closing(self.con()) as con:
            self.assertEqual(buffs._version(con, self.now), version)    # повторное чтение не будит таблицу

        self.now = msk(14, 20, 1)
        buffs.ingest_messages(self.db, [rec(1, "Extanz\n5 убийц, 2 мины", when=msk(12, 19, 56))])
        with closing(self.con()) as con:
            row = con.execute("SELECT counts_json,buff_day FROM buff_messages").fetchone()
            self.assertNotEqual(buffs._version(con, self.now), version)
        self.assertEqual(json.loads(row["counts_json"]), {"killer": 5, "mine": 2})
        self.assertEqual(row["buff_day"], "2026-11-12")

        self.assertEqual(buffs.mark_deleted(self.db, [1]), 1)
        self.assertEqual(buffs.mark_deleted(self.db, [1]), 0)
        self.assertNotIn("Extanz", self.page())

    def test_deleting_a_message_changes_the_version_even_with_a_frozen_clock(self):
        buffs.ingest_messages(self.db, [rec(1, "Extanz\n3 убийцы", when=msk(14, 19))])
        with closing(self.con()) as con:
            before = buffs._version(con, self.now)
        buffs.mark_deleted(self.db, [1])               # часы не сдвинуты: updated_at остался тем же
        with closing(self.con()) as con:
            self.assertNotEqual(buffs._version(con, self.now), before)

    def test_empty_and_unparsed_messages(self):
        stats = buffs.ingest_messages(self.db, [
            rec(1, "", when=msk(14, 19)),
            rec(2, "   ", when=msk(14, 19)),
            rec(3, "всем привет", when=msk(14, 19)),
        ])
        self.assertEqual((stats["skipped"], stats["unparsed"]), (2, 1))
        self.assertIn("Не распознано (1)", self.page())

    def test_nick_from_discord_binding_when_forgotten(self):
        self.add_player("Нормальный Ник", discord_id=42)
        buffs.ingest_messages(self.db, [rec(1, "5 мин", when=msk(14, 19), author_id=42, author="кто-то")])
        with closing(self.con()) as con:
            self.assertEqual(con.execute("SELECT nick FROM buff_messages").fetchone()[0], "Нормальный Ник")
        buffs.ingest_messages(self.db, [rec(2, "7 мин", when=msk(14, 19), author_id=99, author="Аноним")])
        with closing(self.con()) as con:
            self.assertEqual(con.execute("SELECT nick FROM buff_messages WHERE message_id=2").fetchone()[0], "Аноним")

    def test_channel_settings(self):
        buffs.clear_channel(self.db, 7)
        self.assertEqual(buffs.configured_channels(self.db), {})
        buffs.set_channel(self.db, 7, 500)
        buffs.set_channel(self.db, 7, 501)
        self.assertEqual(buffs.configured_channels(self.db), {7: 501})
        buffs.clear_channel(self.db, 7)
        self.assertEqual(buffs.configured_channels(self.db), {})


class BoardTests(BuffBase):
    def test_navigation_and_empty_state(self):
        buffs.clear_channel(self.db, 7)
        html = self.page()
        self.assertIn("href='/buffs'>Бафы</a>", html.replace('"', "'"))
        self.assertIn("/buffs_channel", html)            # канал ещё не задан — подсказка
        buffs.set_channel(self.db, 7, 500)
        self.assertIn("Заявок на бафы нет", self.page())

    def test_board_shows_everyone_who_wrote_regardless_of_day(self):
        self.seed_clan()
        # сообщения за 12, 13 и 14 ноября — все на одной доске, по алфавиту
        self.assertEqual(self.rows(), ["DinGOBY", "Extanz", "KROWA_DASHA", "MLoveX", "TergosX", "Пельмешек"])
        html = self.page()
        self.assertIn("В таблице: 6", html)
        self.assertNotIn("<select", html)                # дат-навигации больше нет
        # Шизы, Топоты, Убийцы, Хилки, Напалмы, Смоки, Мины
        self.assertEqual(self.foot("brow-tot"), ["·", "430", "36", "6", "·", "4", "100"])
        self.assertEqual(self.foot("brow-rest"), self.foot("brow-tot"))
        self.assertIn("id='flt'", html)                  # поиск по нику

    def test_each_row_is_parsed_into_the_right_columns(self):
        self.seed_clan()
        html = self.page()
        row = html.split(">DinGOBY<")[1].split("</tr>")[0]
        self.assertEqual(re.findall(r"data-n='(\d+)'", row), ["0", "100", "3", "3", "0", "3", "0"])
        row = html.split(">Пельмешек<")[1].split("</tr>")[0]
        self.assertEqual(re.findall(r"data-n='(\d+)'", row), ["0", "100", "5", "0", "0", "0", "0"])

    def test_latest_message_of_a_player_replaces_the_previous_one(self):
        self.seed_clan()
        buffs.ingest_messages(self.db, [rec(7, "KROWA_DASHA\n1 смок", when=msk(14, 18))])
        html = self.page()
        self.assertEqual(html.count(">KROWA_DASHA<"), 1)
        row = html.split(">KROWA_DASHA<")[1].split("</tr>")[0]
        self.assertEqual(re.findall(r"data-n='(\d+)'", row), ["0", "0", "0", "0", "0", "1", "0"])

    def test_unknown_nick_and_partial_are_flagged(self):
        buffs.ingest_messages(self.db, [rec(1, "Незнакомец\n3 убийцы, 5 пушек", when=msk(14, 19))])
        html = self.page()
        self.assertIn("нет в базе", html)
        self.assertIn("Не распознано: 5 пушек", html)

    def test_request_stays_as_long_as_the_message_exists(self):
        buffs.ingest_messages(self.db, [
            rec(1, "Старик\n3 убийцы", when=self.now - timedelta(days=400)),
            rec(2, "Свежий\n3 убийцы", when=self.now - timedelta(days=1)),
        ])
        self.assertEqual(self.rows(), ["Свежий", "Старик"])          # срока давности нет
        buffs.mark_deleted(self.db, [1])                              # удалили сообщение — заявки нет
        self.assertEqual(self.rows(), ["Свежий"])

    def test_deleting_a_message_never_touches_history(self):
        self.seed_clan()
        self.toggle("Extanz", 1)
        buffs.mark_deleted(self.db, [3])
        self.assertNotIn("Extanz", self.rows())                       # ни заявки, ни зелёной строки
        (row,) = self.history_rows()
        self.assertEqual((row["nick"], row["total"]), ("Extanz", 3))
        self.assertIn("Extanz", self.page("/buffs/history"))

    def test_only_the_current_channel_is_shown(self):
        buffs.ingest_messages(self.db, [
            rec(1, "Тестовый\n3 убийцы", when=msk(14, 19), channel_id=999),
            rec(2, "Настоящий\n3 убийцы", when=msk(14, 19), channel_id=500),
            rec(3, "болтовня в тесте", when=msk(14, 19), channel_id=999),
        ])
        self.assertEqual(self.rows(), ["Настоящий"])
        self.assertNotIn("Не распознано", self.page())
        buffs.set_channel(self.db, 7, 999)                            # переключили канал
        self.assertEqual(self.rows(), ["Тестовый"])
        buffs.clear_channel(self.db, 7)                               # выключили — доска пуста, история цела
        self.assertEqual(self.rows(), [])

    def test_unparsed_list_is_capped_but_counts_everything(self):
        buffs.ingest_messages(self.db, [rec(i, f"болтовня {i}", when=msk(14, 19) + timedelta(minutes=i))
                                        for i in range(1, 41)])
        html = self.page()
        self.assertIn("Не распознано (40, показаны последние 30)", html)
        self.assertIn("болтовня 40", html)
        self.assertNotIn("болтовня 5<", html)


class IssueTests(BuffBase):
    def test_tick_saves_history_and_untick_removes_it(self):
        self.seed_clan()
        resp = self.toggle("KROWA_DASHA", 1)
        data = resp.get_json()
        self.assertEqual((resp.status_code, data["ok"], data["issued"]), (200, True, True))
        self.assertNotEqual(data["before"], data["after"])
        self.assertEqual(self.done_nicks(), ["KROWA_DASHA"])
        self.assertIn("Выдано: <b id='chip-done'>1 из 6</b>", self.page())
        self.assertEqual(self.foot("brow-rest")[1], "360")        # топоты: 430 − 70 уже выданы
        (row,) = self.history_rows()
        self.assertEqual((row["nick"], row["total"], row["buff_day"]), ("KROWA_DASHA", 82, "2026-11-14"))
        self.assertEqual(json.loads(row["counts_json"]), {"killer": 12, "topot": 70})

        self.assertFalse(self.toggle("KROWA_DASHA", 0).get_json()["issued"])
        self.assertEqual(self.history_rows(), [])
        self.assertEqual(self.done_nicks(), [])

    def test_toggle_sets_state_so_stale_pages_cannot_flip_each_other(self):
        self.seed_clan()
        key = self.key_for("Extanz")
        self.toggle("Extanz", 1, key=key)
        again = self.toggle("Extanz", 1, key=key)          # второй человек со старой страницей
        self.assertEqual(again.status_code, 200)
        self.assertEqual(len(self.history_rows()), 1)       # выдача не удвоилась и не снялась
        self.assertEqual(self.done_nicks(), ["Extanz"])
        self.toggle("Extanz", 0, key=key)
        self.assertEqual(self.toggle("Extanz", 0, key=key).status_code, 200)
        self.assertEqual(self.history_rows(), [])

    def test_unknown_key_is_404_not_an_empty_issue(self):
        self.seed_clan()
        resp = self.toggle("", 1, key="p9999")
        self.assertEqual((resp.status_code, resp.get_json()["ok"]), (404, False))
        self.assertEqual(self.history_rows(), [])

    def test_toggle_without_js_redirects(self):
        self.seed_clan()
        resp = self.client.post("/buffs/toggle", data={"key": self.key_for("Extanz"), "issued": "1"})
        self.assertEqual(resp.status_code, 303)
        self.assertTrue(resp.headers["Location"].endswith("/buffs"))

    def test_issued_requests_leave_the_board_after_the_kv(self):
        self.seed_clan()
        self.toggle("KROWA_DASHA", 1)
        self.toggle("TergosX", 1)
        v_evening = self.client.get("/buffs/version").get_json()["v"]
        self.now = msk(15, 4, 0)                            # ночь после КВ: ещё день выдачи субботы
        self.assertEqual(self.done_nicks(), ["KROWA_DASHA", "TergosX"])
        self.assertEqual(self.client.get("/buffs/version").get_json()["v"], v_evening)
        self.now = msk(15, 5, 30)                           # новый день выдачи
        self.assertEqual(self.rows(), ["DinGOBY", "Extanz", "MLoveX", "Пельмешек"])
        self.assertNotEqual(self.client.get("/buffs/version").get_json()["v"], v_evening)
        self.assertEqual(len(self.history_rows()), 2)       # история на месте
        self.assertEqual(self.foot("brow-tot")[1], "300")   # 100+100+100 невыданных топотов

    def test_issue_after_midnight_belongs_to_the_previous_kv_day(self):
        self.seed_clan()
        self.now = msk(15, 0, 40)
        self.toggle("Extanz", 1)
        self.assertEqual(self.history_rows()[0]["buff_day"], "2026-11-14")

    def test_message_after_issue_is_a_new_request(self):
        self.seed_clan()
        self.toggle("KROWA_DASHA", 1)
        self.now = msk(14, 21, 5)
        buffs.ingest_messages(self.db, [rec(7, "KROWA_DASHA\n5 мин", when=msk(14, 21))])
        self.assertEqual(self.done_nicks(), [])             # снова невыдана
        html = self.page()
        self.assertIn("уже выдавали", html)
        self.assertIn("Сегодня уже выдано: 70 топотов, 12 убийц", html)
        self.toggle("KROWA_DASHA", 1)
        self.assertEqual([r["total"] for r in self.history_rows()], [82, 5])
        self.toggle("KROWA_DASHA", 0)                       # снимается только последняя выдача
        self.assertEqual([r["total"] for r in self.history_rows()], [82])
        self.assertEqual(self.done_nicks(), [])

    def test_edit_after_issue_is_a_new_request(self):
        self.seed_clan()
        self.toggle("Extanz", 1)
        self.now = msk(14, 20, 40)
        buffs.ingest_messages(self.db, [rec(3, "Extanz\n10 убийц", when=msk(12, 19, 56), edited=msk(14, 20, 30))])
        self.assertNotIn("Extanz", self.done_nicks())
        self.assertEqual(self.history_rows()[0]["total"], 3)   # прежняя выдача в истории не меняется

    def test_tick_sticks_even_if_server_clock_is_behind_discord(self):
        buffs.ingest_messages(self.db, [rec(1, "Extanz\n3 убийцы", when=self.now + timedelta(minutes=2))])
        self.toggle("Extanz", 1)
        self.assertEqual(self.done_nicks(), ["Extanz"])

    def test_version_changes_with_messages_and_ticks(self):
        self.seed_clan()
        v0 = self.client.get("/buffs/version").get_json()["v"]
        self.now = msk(14, 20, 1)
        buffs.ingest_messages(self.db, [rec(9, "Extanz\n1 мина", when=msk(14, 20))])
        v1 = self.client.get("/buffs/version").get_json()["v"]
        self.toggle("Extanz", 1)
        v2 = self.client.get("/buffs/version").get_json()["v"]
        self.assertEqual(len({v0, v1, v2}), 3)


class HistoryTests(BuffBase):
    def play_three_kv_days(self):
        for nick in ("KROWA_DASHA", "TergosX", "Extanz", "DinGOBY", "MLoveX", "Пельмешек"):
            self.add_player(nick)
        for upto_ids, when in (((1, 2, 3), msk(12, 20)), ((4,), msk(13, 19)), ((5, 6), msk(14, 20))):
            self.now = when
            buffs.ingest_messages(self.db, [rec(i, t, when=w) for i, t, w in CLAN if i in upto_ids])
            for nick in self.rows():
                self.toggle(nick, 1)
            self.assertEqual(self.rows(), self.done_nicks())

    def test_history_totals_and_days(self):
        self.play_three_kv_days()
        history = self.page("/buffs/history")
        grand = sum(r["total"] for r in self.history_rows())
        self.assertIn(f"<b>{grand}</b>", history)
        self.assertIn("<label>Игроков</label><b>6</b>", history)
        self.assertIn("<label>Дней выдачи</label><b>3</b>", history)
        tergos = history.split("TergosX")[1].split("</tr>")[0]
        self.assertIn("12.11.26 чт", tergos)               # в какие дни получал
        self.assertIn(">163<", tergos)                      # 100 мин + 3 убийцы + 60 топотов
        by_days = self.page("/buffs/history?view=days")
        self.assertIn("13.11.26 пт", by_days)
        self.assertIn("DinGOBY", by_days)

    def test_each_kv_day_clears_the_board_for_the_next_one(self):
        self.play_three_kv_days()
        self.now = msk(15, 6)
        self.assertEqual(self.rows(), [])                   # всем выдали — доска пуста

    def test_deleting_a_history_entry_brings_the_request_back(self):
        self.play_three_kv_days()
        self.now = msk(14, 21)
        before = len(self.history_rows())
        krowa = next(r for r in self.history_rows() if r["nick"] == "KROWA_DASHA")
        resp = self.client.post(f"/buffs/issued/{krowa['id']}/delete")
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(len(self.history_rows()), before - 1)
        self.assertEqual(self.rows(), ["KROWA_DASHA", "MLoveX", "Пельмешек"])   # выданные сегодня остаются до 05:00
        self.assertEqual(self.done_nicks(), ["MLoveX", "Пельмешек"])             # а KROWA_DASHA снова невыдана

    def test_history_follows_player_rename_via_alias(self):
        pid = self.add_player("Старый")
        buffs.ingest_messages(self.db, [rec(1, "Старый\n3 убийцы", when=msk(14, 19))])
        self.toggle("Старый", 1)
        with closing(self.con()) as con:
            con.execute("UPDATE players SET canonical_nick='Новый' WHERE id=?", (pid,))
            con.execute("INSERT INTO player_aliases(alias,player_id) VALUES('Старый',?)", (pid,))
        history = self.page("/buffs/history")
        self.assertIn("Новый", history)
        self.assertNotIn(">Старый<", history)

    def test_empty_history(self):
        self.assertIn("История пока пустая", self.page("/buffs/history"))


class ReconcileTests(BuffBase):
    def test_messages_deleted_while_the_bot_was_offline_are_removed(self):
        self.seed_clan()
        # в канале сейчас остались только 1, 2, 4, 5, 6 — сообщение 3 удалили без ведома бота
        removed = buffs.reconcile_deleted(self.db, 500, {1, 2, 4, 5, 6, 77}, window_start=0)
        self.assertEqual(removed, 1)
        self.assertNotIn("Extanz", self.rows())
        self.assertEqual(len(self.rows()), 5)

    def test_messages_older_than_the_read_window_are_left_alone(self):
        self.seed_clan()
        removed = buffs.reconcile_deleted(self.db, 500, {5, 6}, window_start=5)   # прочитали только 5 и 6
        self.assertEqual(removed, 0)
        self.assertEqual(len(self.rows()), 6)

    def test_empty_answer_deletes_nothing(self):
        self.seed_clan()                       # пустой ответ истории = скорее нет прав, чем пустой канал
        self.assertEqual(buffs.reconcile_deleted(self.db, 500, set(), 0), 0)
        self.assertEqual(len(self.rows()), 6)

    def test_other_channels_are_not_reconciled(self):
        buffs.ingest_messages(self.db, [rec(1, "Ник\n3 мины", when=msk(14, 19), channel_id=999)])
        self.assertEqual(buffs.reconcile_deleted(self.db, 500, {42}, 0), 0)
        with closing(self.con()) as con:
            self.assertEqual(con.execute("SELECT deleted FROM buff_messages").fetchone()[0], 0)


class MigrationTests(BuffBase):
    def test_old_unique_table_is_rebuilt_without_losing_rows(self):
        raw = sqlite3.connect(self.tmp.name + "/old.sqlite3")
        raw.executescript("""
            CREATE TABLE buff_issued(id INTEGER PRIMARY KEY, buff_day TEXT NOT NULL, nick_key TEXT NOT NULL,
                player_id INTEGER, nick TEXT NOT NULL, counts_json TEXT NOT NULL CHECK(json_valid(counts_json)),
                total INTEGER NOT NULL, issued_at TEXT NOT NULL, UNIQUE(buff_day, nick_key));
            CREATE INDEX idx_buff_issued_day ON buff_issued(buff_day);
            INSERT INTO buff_issued VALUES(1,'2026-11-12','p1',1,'Extanz','{"killer":3}',3,'2026-11-12T17:00:00.000000+00:00');""")
        raw.row_factory = sqlite3.Row
        buffs._schema(raw)
        buffs._schema(raw)                                  # повторный вызов — безопасно
        sql = raw.execute("SELECT sql FROM sqlite_master WHERE name='buff_issued'").fetchone()[0]
        self.assertNotIn("UNIQUE", sql)
        self.assertEqual(raw.execute("SELECT nick,total FROM buff_issued").fetchone()[:], ("Extanz", 3))
        raw.execute("""INSERT INTO buff_issued(buff_day,nick_key,nick,counts_json,total,issued_at)
            VALUES('2026-11-12','p1','Extanz','{"mine":5}',5,'2026-11-12T18:00:00.000000+00:00')""")
        self.assertEqual(raw.execute("SELECT COUNT(*) FROM buff_issued").fetchone()[0], 2)
        self.assertEqual(raw.execute("SELECT COUNT(*) FROM sqlite_master WHERE name LIKE 'buff_issued%'"
                                     " AND type='table'").fetchone()[0], 1)
        raw.close()


class FakeMessage:
    def __init__(self, mid, content, when, *, channel, author_id=1, name="игрок", bot=False, edited=None):
        self.id, self.content, self.created_at, self.edited_at = mid, content, when, edited
        self.author = SimpleNamespace(id=author_id, display_name=name, name=name, bot=bot)
        self.channel, self.guild = channel, channel.guild


class FakeChannel:
    def __init__(self, cid=500, can_read=True):
        self.id, self.mention, self.can_read, self.messages = cid, f"<#{cid}>", can_read, []
        self.guild = SimpleNamespace(id=7, me=object(), get_channel=lambda c: self if c == self.id else None)

    def add(self, mid, content, when, **kw):
        self.messages.append(FakeMessage(mid, content, when, channel=self, **kw))

    def permissions_for(self, _me):
        return SimpleNamespace(view_channel=True, read_message_history=self.can_read)

    async def history(self, limit=100):
        # как Discord без права читать историю: пустой ответ, а не ошибка
        for m in sorted(self.messages, key=lambda m: -m.id)[:limit] if self.can_read else []:
            yield m

    async def fetch_message(self, mid):
        return next(m for m in self.messages if m.id == mid)


class FakeInteraction:
    def __init__(self, guild):
        self.guild, self.guild_id, self.sent = guild, guild.id, []

        async def send(*args, **kwargs):
            self.sent.append(kwargs)

        async def defer(**kwargs):
            pass

        self.response = SimpleNamespace(defer=defer, send_message=send)
        self.followup = SimpleNamespace(send=send)

    @property
    def text(self):
        return self.sent[-1]["embed"].title + "\n" + (self.sent[-1]["embed"].description or "")


class DiscordFlowTests(BuffBase, unittest.IsolatedAsyncioTestCase):
    """Команды и обработчики бота на подставных объектах Discord (без сети)."""

    def setUp(self):
        BuffBase.setUp(self)
        buffs.clear_channel(self.db, 7)
        import discord
        import bot as botmod
        from discord import app_commands
        from discord.ext import commands
        self.bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
        runtime = SimpleNamespace(
            bot=self.bot, discord=discord, app_commands=app_commands, PLAYER_DB_PATH=self.db,
            log=lambda *a, **k: None, make_reply_embed=botmod.make_reply_embed,
            COLOR_OK=botmod.COLOR_OK, COLOR_ERR=botmod.COLOR_ERR, COLOR_LIVE=botmod.COLOR_LIVE)

        async def allow(_interaction):
            return True

        runtime.require_bot_admin = allow
        buffs.install_runtime(runtime)
        self.channel = FakeChannel()
        self.bot.get_channel = lambda cid: self.channel if cid == 500 else None
        for nick in ("KROWA_DASHA", "TergosX", "Extanz", "DinGOBY", "MLoveX", "Пельмешек"):
            self.add_player(nick)

    def tearDown(self):
        BuffBase.tearDown(self)

    def listener(self, event):
        return next(f for f in self.bot.extra_events[event] if f.__name__.startswith("_buffs_"))

    async def command(self, name, *args):
        interaction = FakeInteraction(self.channel.guild)
        await self.bot.tree.get_command(name).callback(interaction, *args)
        return interaction

    def fill_channel(self):
        for mid, text, when in CLAN:
            self.channel.add(mid, text, when)

    async def test_buffs_channel_reads_history_and_fills_the_board(self):
        self.fill_channel()
        self.channel.add(90, "бот пишет", msk(14, 19), bot=True)
        self.channel.add(91, "", msk(14, 19))                                  # только вложение
        interaction = await self.command("buffs_channel", self.channel)
        self.assertEqual(buffs.configured_channels(self.db), {7: 500})
        self.assertIn("Прочитано сообщений: **6**", interaction.text)
        self.assertEqual(self.rows(), ["DinGOBY", "Extanz", "KROWA_DASHA", "MLoveX", "TergosX", "Пельмешек"])

    async def test_buffs_sync_removes_requests_of_messages_deleted_while_offline(self):
        self.fill_channel()
        await self.command("buffs_channel", self.channel)
        self.channel.messages = [m for m in self.channel.messages if m.id != 3]    # Extanz удалил сообщение
        interaction = await self.command("buffs_sync", 500)
        self.assertIn("убрано заявок удалённых сообщений: **1**", interaction.text)
        self.assertNotIn("Extanz", self.rows())
        self.assertEqual(len(self.rows()), 5)

    async def test_buffs_sync_without_history_permission_deletes_nothing(self):
        self.fill_channel()
        await self.command("buffs_channel", self.channel)
        self.channel.can_read = False
        interaction = await self.command("buffs_sync", 500)
        self.assertIn("Нет доступа к истории", interaction.text)
        self.assertEqual(len(self.rows()), 6)

    async def test_live_messages_edits_and_deletes(self):
        await self.command("buffs_channel", self.channel)
        on_message, on_edit = self.listener("on_message"), self.listener("on_raw_message_edit")
        on_delete = self.listener("on_raw_message_delete")

        await on_message(FakeMessage(1, "Extanz\n3 убийцы", msk(14, 19), channel=self.channel))
        self.assertEqual(self.rows(), ["Extanz"])
        other = FakeChannel(cid=999)                                            # чужой канал и бот игнорируются
        await on_message(FakeMessage(2, "TergosX\n5 мин", msk(14, 19), channel=other))
        await on_message(FakeMessage(3, "TergosX\n5 мин", msk(14, 19), channel=self.channel, bot=True))
        self.assertEqual(self.rows(), ["Extanz"])

        self.channel.messages = [FakeMessage(1, "Extanz\n10 убийц", msk(14, 19), channel=self.channel,
                                             edited=msk(14, 19, 5))]
        await on_edit(SimpleNamespace(guild_id=7, channel_id=500, message_id=1))
        row = self.page().split(">Extanz<")[1].split("</tr>")[0]
        self.assertEqual(re.findall(r"data-n='(\d+)'", row)[2], "10")

        await on_delete(SimpleNamespace(guild_id=7, channel_id=999, message_id=1))   # другой канал — не трогаем
        self.assertEqual(self.rows(), ["Extanz"])
        await on_delete(SimpleNamespace(guild_id=7, channel_id=500, message_id=1))
        self.assertEqual(self.rows(), [])

    async def test_on_ready_catches_up_and_cleans_deleted(self):
        self.fill_channel()
        await self.command("buffs_channel", self.channel)
        self.channel.messages = [m for m in self.channel.messages if m.id != 2]    # пока бот спал: удалили 2…
        self.channel.add(7, "Extanz\n9 мин", msk(14, 19, 30))                      # …и написали новое
        await self.listener("on_ready")()
        self.assertNotIn("TergosX", self.rows())
        row = self.page().split(">Extanz<")[1].split("</tr>")[0]
        self.assertEqual(re.findall(r"data-n='(\d+)'", row)[6], "9")

    async def test_buffs_off_clears_the_board_but_not_history(self):
        self.fill_channel()
        await self.command("buffs_channel", self.channel)
        self.toggle("KROWA_DASHA", 1)
        await self.command("buffs_off")
        self.assertEqual(self.rows(), [])
        self.assertEqual(len(self.history_rows()), 1)


class RuntimeTests(unittest.TestCase):
    def test_commands_and_listeners_are_registered_once(self):
        import bot
        buffs.install_runtime(bot)
        buffs.install_runtime(bot)          # повторный вызов ничего не дублирует
        names = [c.name for c in bot.bot.tree.get_commands()]
        for name in ("buffs_channel", "buffs_sync", "buffs_off"):
            self.assertEqual(names.count(name), 1, name)
        for event in ("on_message", "on_raw_message_edit", "on_raw_message_delete",
                      "on_raw_bulk_message_delete", "on_ready"):
            ours = [f for f in bot.bot.extra_events.get(event, []) if f.__name__.startswith("_buffs_")]
            self.assertEqual(len(ours), 1, event)


if __name__ == "__main__":
    unittest.main()
