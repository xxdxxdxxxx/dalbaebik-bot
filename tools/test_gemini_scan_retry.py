import itertools
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


class BadResponse:
    """Ответ 200, но с содержимым, из которого таблицу не достать."""

    status_code = 200
    headers: dict = {}

    def __init__(self, payload=None, error=None):
        self._payload, self._error = payload, error

    def json(self):
        if self._error:
            raise self._error
        return self._payload


def _with_text(text):
    return BadResponse({"candidates": [{"content": {"parts": [{"text": text}]}}]})


class GeminiLongRetryTests(unittest.TestCase):
    """/scan повторяет запрос по кругу (12 раз по умолчанию), а не сдаётся после трёх попыток."""

    def run_scan(self, side_effect, env=None, **kwargs):
        env = {"GEMINI_API_KEY": "offline-test-key", **(env or {})}
        with patch.dict(os.environ, env), \
             patch.object(bot.requests, "post", side_effect=side_effect) as post, \
             patch.object(bot.time, "sleep") as sleep:
            try:
                result = bot.scan_image_bytes(b"image", "image/png", **kwargs)
                error = None
            except bot.GeminiScanError as exc:
                result, error = None, exc
        return result, error, post, sleep

    def test_default_is_twelve_attempts_and_the_last_one_can_still_succeed(self):
        rows, error, post, sleep = self.run_scan([FakeResponse(503)] * 11 + [FakeResponse(200)])
        self.assertEqual(ROWS, rows)
        self.assertEqual(12, post.call_count)
        self.assertEqual(11, sleep.call_count)

    def test_gives_up_after_twelve_attempts_and_reports_how_many(self):
        rows, error, post, sleep = self.run_scan([FakeResponse(503)] * 12)
        self.assertIsNone(rows)
        self.assertRegex(str(error), "HTTP 503")
        self.assertEqual(12, error.attempts)
        self.assertEqual(12, post.call_count)

    def test_attempts_come_from_env_with_sane_limits(self):
        with patch.dict(os.environ, {"GEMINI_SCAN_ATTEMPTS": "15"}):
            self.assertEqual(15, bot._gemini_attempts())
        for raw, expected in (("", 12), ("abc", 12), ("0", 1), ("-5", 1), ("999", 30), (" 10 ", 10)):
            with patch.dict(os.environ, {"GEMINI_SCAN_ATTEMPTS": raw}):
                self.assertEqual(expected, bot._gemini_attempts(), raw)
        _, error, post, _ = self.run_scan([FakeResponse(500)] * 15, env={"GEMINI_SCAN_ATTEMPTS": "15"})
        self.assertEqual(15, post.call_count)
        self.assertEqual(15, error.attempts)

    def test_overload_and_server_errors_are_retried(self):
        for status in (408, 429, 500, 502, 503, 504, 520, 599):
            rows, _, post, _ = self.run_scan([FakeResponse(status), FakeResponse(200)])
            self.assertEqual(ROWS, rows, status)
            self.assertEqual(2, post.call_count, status)

    def test_permanent_rejections_are_not_retried(self):
        for status in (400, 401, 403, 404):
            rows, error, post, sleep = self.run_scan([FakeResponse(status)] * 12)
            self.assertRegex(str(error), f"HTTP {status}")
            self.assertEqual(1, error.attempts)
            self.assertEqual(1, post.call_count, status)
            sleep.assert_not_called()

    def test_any_network_error_is_retried(self):
        for exc in (requests.Timeout("t"), requests.ConnectionError("c"),
                    requests.exceptions.ChunkedEncodingError("cut"), requests.exceptions.SSLError("ssl")):
            rows, _, post, _ = self.run_scan([exc, exc, FakeResponse(200)])
            self.assertEqual(ROWS, rows, type(exc).__name__)
            self.assertEqual(3, post.call_count)
        _, error, post, _ = self.run_scan([requests.ConnectionError("down")] * 12)
        self.assertRegex(str(error), "сетевая ошибка Gemini после повторных попыток")
        self.assertEqual(12, post.call_count)

    def test_broken_answers_are_retried(self):
        broken = [BadResponse(error=ValueError("not json")),
                  BadResponse({}),                                   # нет candidates (например, блокировка)
                  BadResponse({"candidates": []}),
                  _with_text("[{\"place\": 1, "),                    # оборванный JSON
                  _with_text('{"place": 1}')]                        # не список
        for bad in broken:
            rows, _, post, _ = self.run_scan([bad, FakeResponse(200)])
            self.assertEqual(ROWS, rows)
            self.assertEqual(2, post.call_count)
        _, error, post, _ = self.run_scan([BadResponse({})] * 12)
        self.assertRegex(str(error), "некорректный JSON-ответ Gemini после повторных попыток")
        self.assertEqual(12, error.attempts)

    def test_empty_table_is_retried_only_a_few_times(self):
        rows, error, post, sleep = self.run_scan([FakeResponse(200, rows=[])] * 12)
        self.assertEqual([], rows)                                    # не таблица: отвечаем «пусто», как раньше
        self.assertEqual(3, post.call_count)
        self.assertEqual(2, sleep.call_count)
        rows, _, post, _ = self.run_scan([FakeResponse(200, rows=[]), FakeResponse(200)])
        self.assertEqual(ROWS, rows)                                  # пустой ответ-сбой модели лечится повтором

    def test_total_time_is_capped_so_discord_can_still_get_the_answer(self):
        clock = itertools.chain([0.0], itertools.repeat(10_000.0))
        with patch.object(bot.time, "monotonic", side_effect=lambda: next(clock)):
            rows, error, post, sleep = self.run_scan([FakeResponse(503)] * 12)
        self.assertEqual(1, error.attempts)                           # время вышло: не ждём и не повторяем
        self.assertEqual(1, post.call_count)
        sleep.assert_not_called()

    def test_pauses_stay_short(self):
        for attempt in range(1, 31):
            for _ in range(20):
                self.assertLessEqual(bot._gemini_retry_delay(None, attempt), bot.GEMINI_RETRY_MAX_SECONDS)
        self.assertEqual(15.0, bot.GEMINI_RETRY_MAX_SECONDS)
        slow = FakeResponse(429, headers={"Retry-After": "120"})
        self.assertEqual(15.0, bot._gemini_retry_delay(slow, 1))      # просьбу подождать минуты не выполняем целиком


if __name__ == "__main__":
    unittest.main()
