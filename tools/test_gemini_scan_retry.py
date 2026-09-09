import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock, patch

import requests

import bot


ROWS = [{
    "place": 1,
    "nickname": "OfflinePlayer",
    "kills": 10,
    "deaths": 2,
    "assists": 3,
    "score": 1234,
}]


class FakeResponse:
    def __init__(self, status_code, *, rows=None, headers=None):
        self.status_code = status_code
        self.headers = headers or {}
        self._rows = ROWS if rows is None else rows

    def json(self):
        return {
            "candidates": [{
                "content": {"parts": [{"text": json.dumps(self._rows)}]}
            }]
        }


class GeminiRetryTests(unittest.TestCase):
    def scan(self, side_effect, attempts=3):
        with patch.dict(os.environ, {"GEMINI_API_KEY": "offline-test-key"}), \
             patch.object(bot.requests, "post", side_effect=side_effect) as post, \
             patch.object(bot.time, "sleep") as sleep:
            result = bot.scan_image_bytes(b"image", "image/png", max_retries=attempts)
        return result, post, sleep

    def test_503_then_success_and_single_idempotent_db_save(self):
        rows, post, sleep = self.scan([
            FakeResponse(503, headers={"Retry-After": "0"}),
            FakeResponse(200),
        ])
        self.assertEqual(ROWS, rows)
        self.assertEqual(2, post.call_count)
        sleep.assert_called_once_with(0.0)

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "scan.sqlite3"
            kwargs = dict(
                match_date="2026-08-20", map_name="Хвойник", guild_id=1,
                channel_id=2, user_id=3, scanned_at="2026-08-20T12:00:00+00:00",
                source_filename="offline.png", db_path=db,
            )
            first = bot.save_scan_result(rows, **kwargs)
            second = bot.save_scan_result(rows, **kwargs)
            self.assertTrue(first.created)
            self.assertFalse(second.created)
            self.assertEqual(first.scan_id, second.scan_id)
            with closing(sqlite3.connect(db)) as con:
                self.assertEqual(1, con.execute("SELECT COUNT(*) FROM scans").fetchone()[0])
                self.assertEqual(1, con.execute("SELECT COUNT(*) FROM scan_players").fetchone()[0])

    def test_retry_exhaustion(self):
        with patch.dict(os.environ, {"GEMINI_API_KEY": "offline-test-key"}), \
             patch.object(bot.requests, "post", side_effect=[FakeResponse(503)] * 3) as post, \
             patch.object(bot.time, "sleep") as sleep:
            with self.assertRaisesRegex(bot.GeminiScanError, "HTTP 503"):
                bot.scan_image_bytes(b"image", "image/png", max_retries=3)
        self.assertEqual(3, post.call_count)
        self.assertEqual(2, sleep.call_count)

    def test_permanent_400_is_not_retried(self):
        with patch.dict(os.environ, {"GEMINI_API_KEY": "offline-test-key"}), \
             patch.object(bot.requests, "post", return_value=FakeResponse(400)) as post, \
             patch.object(bot.time, "sleep") as sleep:
            with self.assertRaisesRegex(bot.GeminiScanError, "HTTP 400"):
                bot.scan_image_bytes(b"image", "image/png", max_retries=3)
        post.assert_called_once()
        sleep.assert_not_called()

    def test_timeout_then_success(self):
        rows, post, sleep = self.scan([
            requests.Timeout("offline timeout"),
            FakeResponse(200),
        ])
        self.assertEqual(ROWS, rows)
        self.assertEqual(2, post.call_count)
        sleep.assert_called_once()


if __name__ == "__main__":
    unittest.main()
