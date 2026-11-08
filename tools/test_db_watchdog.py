"""The bot must explain and survive a database that suddenly cannot be opened."""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import bot


class DbWatchdogTests(unittest.TestCase):
    def setUp(self):
        bot._DB_FAIL.update(count=0, last=0.0, warned_policy=0)

    def fail(self, times, exc=None):
        for _ in range(times):
            bot.note_db_failure(exc or sqlite3.OperationalError("unable to open database file"))

    def test_restarts_the_process_after_minutes_of_failures_when_systemd_restarts_it(self):
        with patch.object(bot, "_systemd_restarts_on_failure", return_value=True), \
                patch.object(bot.os, "_exit") as exit_mock, patch.object(bot.logging, "shutdown"):
            self.fail(bot.DB_FAIL_RESTART_AFTER - 1)
            exit_mock.assert_not_called()
            self.fail(1)
            exit_mock.assert_called_once_with(75)

    def test_never_exits_without_a_restart_policy(self):
        with patch.object(bot, "_systemd_restarts_on_failure", return_value=False), \
                patch.object(bot.os, "_exit") as exit_mock:
            self.fail(bot.DB_FAIL_RESTART_AFTER + 5)
            exit_mock.assert_not_called()
            self.assertEqual(bot._DB_FAIL["warned_policy"], 1)

    def test_other_errors_and_gaps_do_not_count(self):
        with patch.object(bot, "_systemd_restarts_on_failure", return_value=True), \
                patch.object(bot.os, "_exit") as exit_mock:
            self.fail(bot.DB_FAIL_RESTART_AFTER + 3, ValueError("not a database problem"))
            self.fail(bot.DB_FAIL_RESTART_AFTER - 1)
            bot._DB_FAIL["last"] -= 500          # the failures stopped for a while
            self.fail(1)
            self.assertEqual(bot._DB_FAIL["count"], 1)
            exit_mock.assert_not_called()

    def test_report_says_what_is_wrong(self):
        with tempfile.TemporaryDirectory() as td:
            missing = Path(td) / "scan_stats.sqlite3"
            with patch.object(bot, "PLAYER_DB_PATH", missing):
                text = bot.db_failure_report()
                self.assertIn("НЕТ ФАЙЛА", text)
                self.assertIn("свободно=", text)
                missing.write_bytes(b"")
                self.assertIn("права=0o", bot.db_failure_report())

    def test_systemd_check_is_off_outside_systemd(self):
        with patch.dict(bot.os.environ, {}, clear=False):
            bot.os.environ.pop("INVOCATION_ID", None)
            self.assertFalse(bot._systemd_restarts_on_failure())


if __name__ == "__main__":
    unittest.main()
