from __future__ import annotations

import asyncio
import types
import unittest
from pathlib import Path
import importlib.util

MODULE_PATH = Path(__file__).with_name("status_messages.py")
spec = importlib.util.spec_from_file_location("status_messages_under_test", MODULE_PATH)
status_messages = importlib.util.module_from_spec(spec)
spec.loader.exec_module(status_messages)


class NotFound(Exception):
    pass


class Forbidden(Exception):
    pass


class HTTPException(Exception):
    def __init__(self, status=503, text="temporary"):
        super().__init__(text)
        self.status = status


class Embed:
    def __init__(self, title):
        self.title = title


class Message:
    def __init__(self, message_id, title, author_id=1):
        self.id = message_id
        self.embeds = [Embed(title)]
        self.author = types.SimpleNamespace(id=author_id)
        self.edits = 0
        self.edit_error = None
        self.active = 0
        self.max_active = 0

    async def edit(self, *, embed):
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await asyncio.sleep(0)
        self.active -= 1
        if self.edit_error:
            raise self.edit_error
        self.edits += 1


class Channel:
    def __init__(self, messages):
        self.id = 77
        self.guild = None
        self.mention = "#logs"
        self.messages = list(messages)
        self.fetch_error = None
        self.sent = 0

    async def history(self, *, limit, oldest_first):
        assert limit == 500
        assert oldest_first is True
        for message in self.messages:
            yield message

    async def fetch_message(self, message_id):
        if self.fetch_error:
            raise self.fetch_error
        for message in self.messages:
            if message.id == message_id:
                return message
        raise NotFound()

    async def send(self, *, embed):
        self.sent += 1
        message = Message(1000 + self.sent, embed.title)
        self.messages.append(message)
        return message


class Runtime:
    def __init__(self, channel, grenade_id=None):
        self.channel = channel
        self.store = {"message_ids": {"online": None, "grenades": grenade_id}}
        self.discord = types.SimpleNamespace(
            NotFound=NotFound, Forbidden=Forbidden, HTTPException=HTTPException
        )
        self.bot = types.SimpleNamespace(user=types.SimpleNamespace(id=1))
        self._EMBED_MIN_INTERVAL = 12.0
        self._last_embed_edit_mono = 0.0
        self._embed_flush_task = None
        self._embed_pending_parts = set()
        self.logs = []

    def load_db(self):
        return {"message_ids": dict(self.store["message_ids"])}

    def save_db(self, data):
        self.store = {"message_ids": dict(data["message_ids"])}

    async def get_log_channel(self, data):
        return self.channel

    def check_channel_post_perms(self, channel, me):
        return None

    def format_online_embed(self, data):
        return Embed(status_messages.TITLES["online"])

    def format_grenades_embed(self, data):
        return Embed(status_messages.TITLES["grenades"])

    def log(self, message, level="info"):
        self.logs.append((level, message))


class StatusMessagesTests(unittest.IsolatedAsyncioTestCase):
    async def test_repairs_newer_reference_to_oldest_message(self):
        oldest = Message(10, status_messages.TITLES["grenades"])
        duplicate = Message(20, status_messages.TITLES["grenades"])
        runtime = Runtime(Channel([oldest, duplicate]), grenade_id=20)
        status_messages.install(runtime)

        _data, error = await runtime.upsert_status_messages(
            force=True, parts=("grenades",)
        )

        self.assertIsNone(error)
        self.assertEqual(10, runtime.store["message_ids"]["grenades"])
        self.assertEqual(1, oldest.edits)
        self.assertEqual(0, runtime.channel.sent)

    async def test_transient_edit_error_never_sends_duplicate(self):
        canonical = Message(10, status_messages.TITLES["grenades"])
        runtime = Runtime(Channel([canonical]), grenade_id=10)
        status_messages.install(runtime)
        await runtime.upsert_status_messages(force=True, parts=("grenades",))

        runtime.channel.fetch_error = HTTPException(503)
        _data, error = await runtime.upsert_status_messages(
            force=True, parts=("grenades",)
        )

        self.assertIn("Не удалось изменить сообщение", error)
        self.assertEqual(0, runtime.channel.sent)
        self.assertEqual(10, runtime.store["message_ids"]["grenades"])

    async def test_parallel_upserts_are_serialized(self):
        canonical = Message(10, status_messages.TITLES["grenades"])
        runtime = Runtime(Channel([canonical]), grenade_id=10)
        status_messages.install(runtime)

        await asyncio.gather(
            runtime.upsert_status_messages(force=True, parts=("grenades",)),
            runtime.upsert_status_messages(force=True, parts=("grenades",)),
        )

        self.assertEqual(1, canonical.max_active)
        self.assertEqual(0, runtime.channel.sent)


if __name__ == "__main__":
    unittest.main()
