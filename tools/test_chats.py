from __future__ import annotations

import asyncio
import os
import re
import sys
import tempfile
import threading
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import player_store
from tools import admin_ui, chats

GUILD = 42
LOCAL = "http://localhost:8787"
BOT_ID = 1
DID1, DID2, DID3, STRANGER = "100000000000000001", "100000000000000002", "100000000000000003", "100000000000009999"


def at(hour: int, minute: int = 0, day: int = 22) -> datetime:
    return datetime(2026, 11, day, hour, minute, tzinfo=timezone.utc)


class Base(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {}, clear=False)
        self.env.start()
        for key in ("ADMIN_PASSWORD", "ADMIN_TRUST_PROXY"):
            os.environ.pop(key, None)
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "chats.sqlite3"
        player_store.ensure_schema(self.db)
        player_store.upsert_guild_config(self.db, GUILD)
        self.patch = patch.object(admin_ui, "DB_PATH", self.db)
        self.patch.start()
        chats._STATE["bridge"] = None
        self.client = admin_ui.app.test_client()

    def tearDown(self):
        chats._STATE["bridge"] = None
        self.patch.stop()
        self.tmp.cleanup()
        self.env.stop()

    def add_player(self, nick, squad, slot, did=None, uname=None, dname=None):
        with closing(player_store.connect(self.db)) as con:
            pid = con.execute("INSERT INTO players(canonical_nick) VALUES(?)", (nick,)).lastrowid
            con.execute("INSERT INTO roster_memberships(guild_id,player_id,squad_id,slot,active) VALUES(?,?,?,?,1)",
                        (GUILD, pid, squad, slot))
            if did:
                con.execute("INSERT INTO discord_bindings(discord_id,player_id,discord_name,discord_username) VALUES(?,?,?,?)",
                            (did, pid, dname, uname))
        return pid

    def put(self, did, mid, text, direction="in", source=None, when=None, **kw):
        source = source or ("discord" if direction == "in" else "bot")
        return chats.store_message(self.db, discord_id=did, message_id=mid, direction=direction, source=source,
                                   content=text, created_at=when or at(9), channel_id=kw.pop("channel_id", "900"),
                                   author_name=kw.pop("author_name", "Игрок"), **kw)

    def rows(self, did=None):
        with closing(player_store.connect(self.db)) as con:
            chats._schema(con)
            sql = "SELECT * FROM dm_messages" + (" WHERE discord_id=?" if did else "") + " ORDER BY id"
            return [dict(r) for r in con.execute(sql, (did,) if did else ())]

    def get(self, path):
        return self.client.get(path, base_url=LOCAL)

    def absent(self, needle, body, label=""):
        """Как assertNotIn, но при провале показывает только место находки, а не всю страницу."""
        at_ = body.find(needle)
        if at_ >= 0:
            self.fail(f"{label}: найдено {needle!r} …{body[max(0, at_ - 80):at_ + 120]}…")

    def present(self, needle, body, label=""):
        if needle not in body:
            self.fail(f"{label}: не найдено {needle!r}")

    def post(self, path, **data):
        return self.client.post(path, base_url=LOCAL, data=data)


# ======================================================================= хранение
class StorageTests(Base):
    def test_same_message_is_stored_once(self):
        self.assertTrue(self.put(DID1, "5001", "привет"))
        self.assertFalse(self.put(DID1, "5001", "привет"))
        self.assertEqual(len(self.rows()), 1)

    def test_snowflakes_stay_exact_text(self):
        big = "1234567890123456789"
        self.put(DID1, big, "x")
        self.assertEqual(self.rows()[0]["message_id"], big)

    def test_panel_label_wins_whichever_side_saved_first(self):
        self.put(DID1, "1", "a", direction="out", source="bot")
        self.put(DID1, "1", "a", direction="out", source="panel")
        self.assertEqual(self.rows()[0]["source"], "panel")
        self.put(DID1, "2", "b", direction="out", source="panel")
        self.put(DID1, "2", "b", direction="out", source="bot")       # слушатель пришёл позже
        self.assertEqual(self.rows()[1]["source"], "panel")
        self.assertEqual(len(self.rows()), 2)

    def test_incoming_is_unread_outgoing_is_read(self):
        self.put(DID1, "1", "от игрока")
        self.put(DID1, "2", "от бота", direction="out")
        self.assertEqual([r["seen"] for r in self.rows()], [0, 1])

    def test_edit_marks_changed_text_but_ignores_unchanged(self):
        self.put(DID1, "1", "старый")
        self.assertTrue(chats.apply_edit(self.db, "1", "новый", "2026-11-22T10:00:00+00:00"))
        row = self.rows()[0]
        self.assertEqual((row["content"], bool(row["edited_at"])), ("новый", True))
        self.assertFalse(chats.apply_edit(self.db, "1", "новый"))      # раскрылась ссылка — текст тот же
        self.assertFalse(chats.apply_edit(self.db, "404", "нет такого"))

    def test_delete_keeps_text_in_history(self):
        self.put(DID1, "1", "я это написал")
        self.assertEqual(chats.mark_deleted(self.db, ["1", "404"]), 1)
        self.assertEqual(chats.mark_deleted(self.db, ["1"]), 0)
        row = self.rows()[0]
        self.assertEqual((row["deleted"], row["content"]), (1, "я это написал"))

    def test_message_fields_collects_files_and_stickers(self):
        message = SimpleNamespace(
            id=7, content="смотри", created_at=at(9), author=SimpleNamespace(id=5, display_name="Вася"),
            attachments=[SimpleNamespace(filename="tab.png", url="https://cdn.example/tab.png",
                                         content_type="image/png", size=123)],
            stickers=[SimpleNamespace(name="Вау")])
        fields = chats.message_fields(message)
        self.assertEqual(fields["author_name"], "Вася")
        self.assertIn("[стикер: Вау]", fields["content"])
        self.assertEqual(fields["attachments"][0]["name"], "tab.png")


# ======================================================================= страницы
class PageTests(Base):
    def setUp(self):
        super().setUp()
        self.add_player("Alpha", 1, 1, did=DID1, uname="alpha_tg", dname="Альфа")
        self.add_player("Bravo", 1, 2)                                  # без привязки Discord
        self.add_player("Champ", 99, 1, did=DID2)

    def test_chats_is_in_the_menu_after_gear(self):
        html = self.get("/chats").get_data(as_text=True)
        nav = re.search(r"<nav class=\"nav\".*?</nav>", html, flags=re.S).group(0)
        self.assertIn(">Чаты</a>", nav)
        self.assertLess(nav.index(">Шмот</a>"), nav.index(">Чаты</a>"))
        self.assertIn("class=\"active\"", re.search(r"<a[^>]*active[^>]*>Чаты", nav).group(0))

    def test_list_groups_squad_members_and_flags_unbound(self):
        html = self.get("/chats").get_data(as_text=True)
        self.assertIn("Отряд 1", html)
        self.assertIn("Чемпионы", html)
        self.assertLess(html.index("Отряд 1"), html.index("Чемпионы"))
        self.assertIn(f"/chats/{DID1}", html)
        self.assertIn(f"/chats/{DID2}", html)
        bravo = re.search(r"<div class='ch-row off'[^>]*>.*?</div>", html, flags=re.S).group(0)
        self.assertIn("Bravo", bravo)
        self.assertIn("Нет привязки Discord", bravo)

    def test_list_shows_last_message_unread_badge_and_strangers(self):
        self.put(DID1, "1", "первое", when=at(9))
        self.put(DID1, "2", "второе", when=at(10))
        self.put(DID1, "3", "ответ бота", direction="out", when=at(11))
        self.put(STRANGER, "4", "я не из отрядов", channel_id="901", author_name="Прохожий")
        html = self.get("/chats").get_data(as_text=True)
        alpha = re.search(rf"<a class='ch-row unread' href='/chats/{DID1}'.*?</a>", html, flags=re.S).group(0)
        self.assertIn("Бот: ответ бота", alpha)
        self.assertIn("title='Непрочитанных: 2'", alpha)
        self.assertIn("Вне отрядов", html)
        self.assertIn("Прохожий", html)
        self.assertIn("я не из отрядов", html)

    def test_user_input_is_escaped_everywhere(self):
        evil = "<script>alert(1)</script><img src=x onerror=alert(2)>"
        self.put(DID1, "1", evil, author_name="<b>Hax</b>")
        self.put(STRANGER, "2", evil, channel_id="901", author_name="<i>Stranger</i>")
        self.put(DID1, "3", "файл", attachments=[
            {"name": "<u>x</u>.png", "url": "javascript:alert(3)"},
            {"name": "ok.png", "url": "https://cdn.example/ok.png?a=1&b=2"}])
        for path in ("/chats", f"/chats/{DID1}", f"/chats/{STRANGER}", f"/chats/{DID1}/feed?v=zzz"):
            body = self.get(path).get_data(as_text=True)
            self.absent("<script>alert(1)", body, path)
            self.absent("<img src=x", body, path)
            self.absent("<i>Stranger</i>", body, path)
        page = self.get(f"/chats/{DID1}").get_data(as_text=True)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", page)
        self.absent("href='javascript:", page)                            # опасная ссылка не кликабельна
        self.assertIn("href='https://cdn.example/ok.png?a=1&amp;b=2'", page)
        self.assertIn("&lt;u&gt;x&lt;/u&gt;.png", page)

    def test_opening_chat_marks_incoming_as_read(self):
        self.put(DID1, "1", "прочитай меня")
        badge = "<span class='ch-badge'"                                  # в CSS страницы «ch-badge» есть всегда
        self.present(badge, self.get("/chats").get_data(as_text=True), "до открытия")
        page = self.get(f"/chats/{DID1}")
        self.assertEqual(page.status_code, 200)
        self.assertIn("прочитай меня", page.get_data(as_text=True))
        self.absent(badge, self.get("/chats").get_data(as_text=True), "после открытия")
        self.assertEqual([r["seen"] for r in self.rows(DID1)], [1])

    def test_chat_header_shows_nick_squad_and_discord(self):
        page = self.get(f"/chats/{DID1}").get_data(as_text=True)
        self.assertIn("Alpha", page)
        self.assertIn("Отряд 1", page)
        self.assertIn("@alpha_tg", page)
        self.assertIn(f"ID {DID1}", page)
        self.assertIn("Сообщений пока нет", page)

    def test_bound_player_without_messages_has_an_openable_chat(self):
        self.assertEqual(self.get(f"/chats/{DID2}").status_code, 200)

    def test_unknown_or_garbage_ids_are_404(self):
        self.assertEqual(self.get(f"/chats/{STRANGER}").status_code, 404)
        self.assertEqual(self.get("/chats/abc").status_code, 404)
        self.assertEqual(self.get("/chats/%C2%B2").status_code, 404)       # «²» — цифра для str.isdigit
        self.assertEqual(self.get("/chats/" + "1" * 30).status_code, 404)

    def test_messages_show_msk_time_day_separator_and_marks(self):
        self.put(DID1, "1", "утро", when=at(9, 5))                          # 09:05 UTC = 12:05 МСК
        self.put(DID1, "2", "правка", when=at(9, 6))
        chats.apply_edit(self.db, "2", "правка2")
        self.put(DID1, "3", "удалю", when=at(9, 7))
        chats.mark_deleted(self.db, ["3"])
        self.put(DID1, "4", "из панели", direction="out", source="panel", when=at(9, 8))
        page = self.get(f"/chats/{DID1}").get_data(as_text=True)
        self.assertIn(">12:05</span>", page)
        self.assertIn("22.11.26", page)
        self.assertIn("<span>изменено</span>", page)
        self.assertIn("<span>удалено в Discord</span>", page)
        self.assertIn("<span>из панели</span>", page)
        self.assertIn("правка2", page)
        self.assertLess(page.index("утро"), page.index("из панели"))        # по времени

    def test_long_chat_shows_tail_with_load_earlier_link(self):
        for i in range(chats.PAGE + 5):
            self.put(DID1, str(1000 + i), f"m{i}", when=at(9, 0, day=1 if i < 5 else 22))
        page = self.get(f"/chats/{DID1}").get_data(as_text=True)
        self.assertIn(f"Показаны последние {chats.PAGE} из {chats.PAGE + 5}", page)
        self.absent("<div class='ch-txt'>m0</div>", page, "старое сообщение за пределами окна")
        wide = self.get(f"/chats/{DID1}?n={chats.PAGE + 100}").get_data(as_text=True)
        self.absent("Показаны последние", wide)
        self.present("<div class='ch-txt'>m0</div>", wide)

    def test_composer_is_disabled_without_bot(self):
        page = self.get(f"/chats/{DID1}").get_data(as_text=True)
        self.assertIn("Бот не запущен — отправка недоступна", page)
        self.assertRegex(page, r"<textarea[^>]*disabled")
        self.assertRegex(page, r"<button type='button' id='ch-sync' disabled")
        chats._STATE["bridge"] = SimpleNamespace()
        page = self.get(f"/chats/{DID1}").get_data(as_text=True)
        self.assertNotRegex(page, r"<textarea[^>]*disabled")

    def test_message_ids_never_leak_into_js_as_numbers(self):
        self.put(DID1, "1234567890123456789", "x")
        page = self.get(f"/chats/{DID1}").get_data(as_text=True)
        self.assertNotRegex(page, r"[^\d']1234567890123456789[^\d']")


class FeedTests(Base):
    def setUp(self):
        super().setUp()
        self.add_player("Alpha", 1, 1, did=DID1)

    def version(self):
        with closing(player_store.connect(self.db)) as con:
            chats._schema(con)
            return chats._chat_version(con, DID1)

    def test_feed_is_empty_until_something_changes(self):
        self.put(DID1, "1", "раз")
        v = self.version()
        same = self.get(f"/chats/{DID1}/feed?v={v}").get_json()
        self.assertEqual(same, {"v": v})
        self.put(DID1, "2", "два")
        changed = self.get(f"/chats/{DID1}/feed?v={v}").get_json()
        self.assertNotEqual(changed["v"], v)
        self.assertIn("два", changed["html"])

    def test_feed_notices_edits_and_deletes(self):
        self.put(DID1, "1", "текст")
        v = self.version()
        chats.mark_deleted(self.db, ["1"])
        fresh = self.get(f"/chats/{DID1}/feed?v={v}").get_json()
        self.assertIn("удалено в Discord", fresh["html"])
        v2 = fresh["v"]
        self.put(DID1, "2", "ещё")
        chats.apply_edit(self.db, "2", "ещё!")
        self.assertIn("изменено", self.get(f"/chats/{DID1}/feed?v={v2}").get_json()["html"])

    def test_open_feed_marks_new_incoming_as_read(self):
        self.put(DID1, "1", "новое")
        self.get(f"/chats/{DID1}/feed?v=old")
        self.assertEqual([r["seen"] for r in self.rows(DID1)], [1])

    def test_list_version_changes_with_new_and_read_messages(self):
        def v():
            return self.get("/chats/version").get_json()["v"]
        v0 = v()
        self.put(DID1, "1", "привет")
        v1 = v()
        self.assertNotEqual(v0, v1)
        self.get(f"/chats/{DID1}")                                          # прочитали
        self.assertNotEqual(v1, v())


# ======================================================================= отправка
class FakeBridge:
    def __init__(self, error=None, added=3):
        self.sent, self.pulled, self.error, self.added = [], [], error, added

    def send(self, uid, text):
        if self.error:
            raise self.error
        self.sent.append((uid, text))
        chats.store_message(admin_ui.DB_PATH, discord_id=uid, message_id=str(9000 + len(self.sent)), direction="out",
                            source="panel", content=text, created_at=at(12), channel_id="900")

    def history(self, uid, limit):
        if self.error:
            raise self.error
        self.pulled.append((uid, limit))
        return self.added


class SendRouteTests(Base):
    def setUp(self):
        super().setUp()
        self.add_player("Alpha", 1, 1, did=DID1)
        self.bridge = FakeBridge()
        chats._STATE["bridge"] = self.bridge

    def test_send_goes_through_bridge_and_shows_in_chat(self):
        r = self.post(f"/chats/{DID1}/send", text="  Выходи на КВ\r\nв 20:00  ")
        self.assertEqual((r.status_code, r.get_json()), (200, {"ok": True}))
        self.assertEqual(self.bridge.sent, [(int(DID1), "Выходи на КВ\nв 20:00")])
        self.assertIn("Выходи на КВ", self.get(f"/chats/{DID1}").get_data(as_text=True))

    def test_empty_and_too_long_are_rejected_before_discord(self):
        self.assertEqual(self.post(f"/chats/{DID1}/send", text="   ").status_code, 400)
        r = self.post(f"/chats/{DID1}/send", text="я" * (chats.MAX_LEN + 1))
        self.assertEqual(r.status_code, 400)
        self.assertIn(str(chats.MAX_LEN + 1), r.get_json()["error"])
        self.assertEqual(self.post(f"/chats/{DID1}/send", text="я" * chats.MAX_LEN).status_code, 200)
        self.assertEqual(len(self.bridge.sent), 1)

    def test_cannot_message_an_unknown_id(self):
        r = self.post(f"/chats/{STRANGER}/send", text="привет")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(self.bridge.sent, [])

    def test_can_reply_to_someone_who_wrote_first(self):
        self.put(STRANGER, "1", "здрасьте", channel_id="901")
        self.assertEqual(self.post(f"/chats/{STRANGER}/send", text="привет").status_code, 200)

    def test_bridge_errors_become_readable_json(self):
        chats._STATE["bridge"] = FakeBridge(error=chats.ChatError("у игрока закрыты ЛС", 409))
        r = self.post(f"/chats/{DID1}/send", text="привет")
        self.assertEqual((r.status_code, r.get_json()), (409, {"ok": False, "error": "у игрока закрыты ЛС"}))
        chats._STATE["bridge"] = FakeBridge(error=RuntimeError("boom"))
        with self.assertLogs(chats.LOG, "ERROR"):                           # сбой должен попасть в лог
            r = self.post(f"/chats/{DID1}/send", text="привет")
        self.assertEqual(r.status_code, 500)
        self.assertNotIn("boom", r.get_data(as_text=True))                  # внутренности не светим

    def test_without_bot_sending_is_503(self):
        chats._STATE["bridge"] = None
        self.assertEqual(self.post(f"/chats/{DID1}/send", text="привет").status_code, 503)
        self.assertEqual(self.post(f"/chats/{DID1}/sync").status_code, 503)

    def test_cross_site_post_is_forbidden(self):
        r = self.client.post(f"/chats/{DID1}/send", base_url=LOCAL, data={"text": "привет"},
                             headers={"Origin": "https://evil.example"})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.bridge.sent, [])

    def test_sync_reports_how_many_were_added(self):
        r = self.post(f"/chats/{DID1}/sync")
        self.assertEqual(r.get_json(), {"ok": True, "added": 3})
        self.assertEqual(self.bridge.pulled, [(int(DID1), chats.SYNC_LIMIT)])
        chats._STATE["bridge"] = FakeBridge(error=chats.ChatError("нет доступа", 409))
        self.assertEqual(self.post(f"/chats/{DID1}/sync").status_code, 409)

    def test_get_is_not_allowed_on_write_routes(self):
        self.assertEqual(self.get(f"/chats/{DID1}/send").status_code, 405)
        self.assertEqual(self.get(f"/chats/{DID1}/sync").status_code, 405)


# ===================================================================== Discord-хуки
class FakeBot:
    def __init__(self, loop=None):
        self.handlers: dict[str, list] = {}
        self.user = SimpleNamespace(id=BOT_ID)
        self.guilds: list = []
        self.loop = loop
        self.channels: dict[int, object] = {}
        self.fetch_calls = 0

    def listen(self, name):
        def deco(fn):
            self.handlers.setdefault(name, []).append(fn)
            return fn
        return deco

    async def fetch_channel(self, cid):
        self.fetch_calls += 1
        if cid not in self.channels:
            raise LookupError(cid)
        return self.channels[cid]

    async def fetch_user(self, uid):
        return None


def msg(mid, author_id, text, *, channel_id=900, recipient=None, guild=None, bot=False, when=None, name="Игрок"):
    return SimpleNamespace(
        id=mid, guild=guild, content=text, created_at=when or at(9), attachments=[], stickers=[],
        author=SimpleNamespace(id=author_id, bot=bot, display_name=name),
        channel=SimpleNamespace(id=channel_id, recipient=recipient))


class HttpError(Exception):
    def __init__(self, status, code=None):
        super().__init__(f"HTTP {status}")
        self.status, self.code = status, code


class RuntimeTests(Base):
    def setUp(self):
        super().setUp()
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()
        self.bot = FakeBot(self.loop)
        self.logs: list = []
        self.runtime = SimpleNamespace(bot=self.bot, PLAYER_DB_PATH=self.db, log=lambda m, lvl="info": self.logs.append((lvl, m)))
        chats.install_runtime(self.runtime)

    def tearDown(self):
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=5)
        self.loop.close()
        super().tearDown()

    def fire(self, event, *args):
        for handler in self.bot.handlers.get(event, []):
            asyncio.run(handler(*args))

    # ---- события
    def test_listeners_are_registered_once_and_bridge_appears(self):
        chats.install_runtime(self.runtime)
        for event in ("on_message", "on_raw_message_edit", "on_raw_message_delete"):
            self.assertEqual(len(self.bot.handlers[event]), 1, event)
        self.assertIsNotNone(chats._STATE["bridge"])

    def test_incoming_dm_is_saved_as_unread(self):
        self.fire("on_message", msg(10, 555, "привет, бот", name="Вася"))
        row = self.rows()[0]
        self.assertEqual((row["discord_id"], row["direction"], row["source"], row["seen"], row["author_name"]),
                         ("555", "in", "discord", 0, "Вася"))
        self.assertEqual(row["channel_id"], "900")

    def test_server_messages_and_other_bots_are_ignored(self):
        self.fire("on_message", msg(10, 555, "в канале", guild=SimpleNamespace(id=GUILD)))
        self.fire("on_message", msg(11, 777, "я другой бот", bot=True))
        self.assertEqual(self.rows(), [])

    def test_bots_own_dm_goes_to_the_recipient_even_when_event_has_none(self):
        self.bot.channels[900] = SimpleNamespace(recipient=SimpleNamespace(id=555))
        self.fire("on_message", msg(20, BOT_ID, "Ты не пришёл на КВ", recipient=None, name="Bot"))
        self.fire("on_message", msg(21, BOT_ID, "второе", recipient=None, name="Bot"))
        rows = self.rows()
        self.assertEqual([(r["discord_id"], r["direction"], r["source"], r["seen"]) for r in rows],
                         [("555", "out", "bot", 1)] * 2)
        self.assertEqual(self.bot.fetch_calls, 1)                           # второй раз — из кэша

    def test_bots_own_dm_uses_recipient_from_event_or_database(self):
        self.fire("on_message", msg(30, BOT_ID, "с получателем", channel_id=901, recipient=SimpleNamespace(id=556)))
        self.put("557", "1", "раньше писали", channel_id="902")
        self.fire("on_message", msg(31, BOT_ID, "канал уже известен", channel_id=902))
        self.assertEqual({r["message_id"]: r["discord_id"] for r in self.rows()},
                         {"30": "556", "1": "557", "31": "557"})
        self.assertEqual(self.bot.fetch_calls, 0)

    def test_unresolvable_own_dm_is_skipped_not_crashing(self):
        self.fire("on_message", msg(40, BOT_ID, "кому?", channel_id=999))
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.logs, [])

    def test_edit_updates_text_but_ignores_embed_updates_and_server_edits(self):
        self.fire("on_message", msg(50, 555, "старое"))
        raw = lambda **kw: SimpleNamespace(guild_id=kw.get("guild_id"), message_id=50, data=kw.get("data", {}))
        self.fire("on_raw_message_edit", raw(data={"embeds": []}))
        self.fire("on_raw_message_edit", raw(data={"content": "старое"}))
        self.fire("on_raw_message_edit", raw(guild_id=GUILD, data={"content": "в канале"}))
        self.assertIsNone(self.rows()[0]["edited_at"])
        self.fire("on_raw_message_edit", raw(data={"content": "новое", "edited_timestamp": "2026-11-22T10:00:00.5+00:00"}))
        row = self.rows()[0]
        self.assertEqual((row["content"], bool(row["edited_at"])), ("новое", True))

    def test_delete_marks_dm_but_not_server_messages(self):
        self.fire("on_message", msg(60, 555, "сотру"))
        self.fire("on_raw_message_delete", SimpleNamespace(guild_id=GUILD, message_id=60))
        self.assertEqual(self.rows()[0]["deleted"], 0)
        self.fire("on_raw_message_delete", SimpleNamespace(guild_id=None, message_id=60))
        self.assertEqual(self.rows()[0]["deleted"], 1)

    def test_storage_failure_is_logged_and_does_not_raise(self):
        with patch.object(chats, "store_message", side_effect=RuntimeError("disk full")):
            self.fire("on_message", msg(70, 555, "x"))
        self.assertTrue(any(lvl == "err" and "disk full" in text for lvl, text in self.logs))

    # ---- мост панель → бот
    def make_user(self, uid=555, send_error=None, history=()):
        outer = self

        class User:
            id = uid

            async def send(self, text):
                if send_error:
                    raise send_error
                return msg(800 + len(outer.sent), BOT_ID, text, channel_id=910, recipient=self, name="Bot")

            async def history(self, limit):
                outer.history_limit = limit
                for item in history:
                    yield item

        self.sent = getattr(self, "sent", [])
        user = User()
        self.bot.guilds = [SimpleNamespace(get_member=lambda i: user if i == uid else None)]
        return user

    def test_panel_send_delivers_dm_and_stores_it_as_panel(self):
        self.sent = []
        self.make_user()
        chats._STATE["bridge"].send(555, "Привет из панели")
        row = self.rows()[0]
        self.assertEqual((row["discord_id"], row["direction"], row["source"], row["content"], row["seen"]),
                         ("555", "out", "panel", "Привет из панели", 1))
        self.assertEqual(row["channel_id"], "910")

    def test_listener_and_panel_do_not_duplicate_the_same_message(self):
        self.sent = []
        self.make_user()
        chats._STATE["bridge"].send(555, "раз")
        self.fire("on_message", msg(800, BOT_ID, "раз", channel_id=910, recipient=SimpleNamespace(id=555)))
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.rows()[0]["source"], "panel")

    def test_closed_dms_error_is_human_readable(self):
        self.make_user(send_error=HttpError(403, 50007))
        with self.assertRaises(chats.ChatError) as ctx:
            chats._STATE["bridge"].send(555, "привет")
        self.assertEqual(ctx.exception.status, 409)
        self.assertIn("закрыты личные сообщения", str(ctx.exception))
        self.assertEqual(self.rows(), [])                                   # неотправленное в историю не попадает

    def test_other_discord_errors_pass_their_text(self):
        self.make_user(send_error=HttpError(500))
        with self.assertRaises(chats.ChatError) as ctx:
            chats._STATE["bridge"].send(555, "привет")
        self.assertEqual(ctx.exception.status, 502)

    def test_unknown_user_is_404(self):
        self.bot.guilds = []
        with self.assertRaises(chats.ChatError) as ctx:
            chats._STATE["bridge"].send(555, "привет")
        self.assertEqual(ctx.exception.status, 404)

    def test_fallback_resolver_from_bot_runtime_is_used(self):
        outer = self
        user = SimpleNamespace(send=None)

        async def send(text):
            return msg(850, BOT_ID, text, channel_id=911, recipient=user)

        user.send = send
        self.sent = []

        async def resolver(guild, uid):
            return user if uid == 556 else None

        self.runtime.resolve_user_for_dm = resolver
        chats._STATE["bridge"].send(556, "через резолвер")
        self.assertEqual(self.rows()[0]["discord_id"], "556")

    def test_send_is_503_when_bot_loop_is_not_running(self):
        self.bot.loop = asyncio.new_event_loop()                            # есть, но не запущен
        with self.assertRaises(chats.ChatError) as ctx:
            chats._STATE["bridge"].send(555, "привет")
        self.assertEqual(ctx.exception.status, 503)
        self.bot.loop.close()

    def test_history_pull_stores_both_sides_as_read_and_is_repeatable(self):
        history = [msg(1, 555, "давно писал", when=at(8, 0, day=1)),
                   msg(2, BOT_ID, "бот ответил", channel_id=900, recipient=None, when=at(8, 1, day=1)),
                   msg(3, 999, "чужой бот", bot=True)]
        self.make_user(history=history)
        self.assertEqual(chats._STATE["bridge"].history(555, 50), 2)
        self.assertEqual(self.history_limit, 50)
        rows = self.rows("555")
        self.assertEqual([(r["direction"], r["seen"]) for r in rows], [("in", 1), ("out", 1)])
        self.assertEqual(chats._STATE["bridge"].history(555, 50), 0)        # повтор ничего не дублирует
        self.assertEqual(len(self.rows()), 2)

    def test_history_with_closed_dms_is_readable_error(self):
        outer = self

        class User:
            id = 555

            async def history(self, limit):
                raise HttpError(403, 50007)
                yield

        user = User()
        self.bot.guilds = [SimpleNamespace(get_member=lambda i: user)]
        with self.assertRaises(chats.ChatError) as ctx:
            chats._STATE["bridge"].history(555, 10)
        self.assertEqual(ctx.exception.status, 409)

    # ---- всё вместе: страница → мост → «Discord» → страница
    def test_end_to_end_from_the_page_form(self):
        self.sent = []
        self.make_user()
        self.add_player("Alpha", 1, 1, did="555")
        self.fire("on_message", msg(100, 555, "когда КВ?", name="Alpha"))
        page = self.get("/chats/555").get_data(as_text=True)
        self.assertIn("когда КВ?", page)
        r = self.post("/chats/555/send", text="В 20:00 по МСК")
        self.assertEqual(r.get_json(), {"ok": True})
        page = self.get("/chats/555").get_data(as_text=True)
        self.assertLess(page.index("когда КВ?"), page.index("В 20:00 по МСК"))
        self.assertIn("<span>из панели</span>", page)


if __name__ == "__main__":
    unittest.main()
