from __future__ import annotations

import asyncio
import copy
import tempfile
import time
import unittest
from contextlib import ExitStack, closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import bot
import player_store
from tools import admin_ui


def snapshot():
    return {
        "config": {"guild_id": 42},
        "session_date": "2026-09-10", "grenade_date": "2026-09-10",
        "kv_session_active": True, "kv_finished": False,
        "players": {
            "100": {"game_nick": "Alpha", "squad": 1, "slot": 1, "came": True},
            "200": {"game_nick": "Beta", "squad": 2, "slot": 1, "came": False},
        },
        "grenade_history": {}, "voice_speak_seconds": {},
    }


def finish(db, data):
    final = copy.deepcopy(data)
    final.update(kv_finished=True, kv_session_active=False)
    player_store.save_bot_snapshot(db, final)
    return final


class SummaryTests(unittest.TestCase):
    def test_missing_is_not_zero_and_stages_do_not_absorb_other_intervals(self):
        self.assertEqual(player_store.grenade_summary({"20:00": 100}, 3), ([None] * 3, None))
        stages, total = player_store.grenade_summary(
            {"20:00": 100, "20:50": 130, "21:20": 140}, 3, final=True)
        self.assertEqual(stages, [None, None, 10])
        self.assertEqual(total, 40)
        self.assertIsNone(player_store.grenade_summary({"20:00": 100, "20:25": 105}, 3, final=True)[1])
        self.assertEqual(player_store.grenade_summary(dict.fromkeys(player_store.GRENADE_STEPS, 100), 4, final=True), ([0] * 4, 0))
        self.assertIsNone(player_store.grenade_summary({"20:00": 100, "20:25": 5, "21:20": 110}, 3, final=True)[1])
        data = {"grenade_history": {"Alpha": {"20:00": 100}}}
        bot._forward_fill_step(data, "20:25")
        self.assertEqual(data["grenade_history"], {"Alpha": {"20:00": 100}})
        self.assertEqual(bot.fmt_voice_mmss(0), "00:00")
        self.assertEqual(bot.fmt_voice_mmss(None), "-")

    def test_rating_requires_complete_metrics_uses_raw_means_and_ignores_voice(self):
        def metrics(kills, voice):
            return {key: [value, 1] for key, value in {
                "kills": kills, "deaths": 1, "assists": 2,
                "score": 100, "grenades": 10, "voice_seconds": voice,
            }.items()}
        a, b = metrics(.48, 0), metrics(.49, 10000)
        ratings = bot.calc_stats_efficiencies([(1, a), (5, b)])
        self.assertLess(ratings[0], ratings[1])
        a["voice_seconds"] = [99999, 1]
        self.assertEqual(ratings, bot.calc_stats_efficiencies([(1, a), (5, b)]))
        self.assertIsNone(bot.calc_stats_efficiencies([(1, {"grenades": [1000, 1]})])[0])


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / "test.sqlite3"
        self.data = snapshot()
        self.data["grenade_history"] = {"Alpha": {"20:00": 100, "20:50": 130, "21:20": 140}, "Beta": {"20:00": 100}}
        self.data["voice_speak_seconds"] = {"100": 0}
        player_store.save_bot_snapshot(self.db, self.data)

    def report_rows(self):
        with closing(player_store.connect(self.db)) as con:
            return {r["raw_nick"]: dict(r) for r in con.execute("SELECT * FROM kv_daily_players")}

    def test_atomic_finalization_and_manual_edits_survive_repeated_save(self):
        with patch.object(player_store, "_ensure_final_daily_report", side_effect=RuntimeError("disk failure")):
            with self.assertRaises(RuntimeError):
                finish(self.db, self.data)
        restored = player_store.load_bot_snapshot(self.db, 42)
        self.assertFalse(restored["kv_finished"])
        self.assertEqual(restored["grenade_history"], self.data["grenade_history"])
        self.assertFalse(self.report_rows())
        final = finish(self.db, self.data)
        rows = self.report_rows()
        self.assertEqual(rows["Alpha"]["total_grenades"], 40)
        self.assertEqual(rows["Alpha"]["voice_seconds"], 0)
        self.assertEqual(rows["Alpha"]["attended"], 1)
        self.assertEqual(rows["Beta"]["attended"], 0)
        self.assertIsNone(rows["Beta"]["total_grenades"])
        self.assertIsNone(rows["Beta"]["voice_seconds"])
        with closing(player_store.connect(self.db)) as con:
            con.execute("UPDATE kv_daily_players SET total_grenades=77 WHERE raw_nick='Alpha'")
        player_store.save_bot_snapshot(self.db, final)
        player_store.finalize_runtime_session(self.db, 42, "2026-09-10")
        self.assertEqual(self.report_rows()["Alpha"]["total_grenades"], 77)
        with closing(player_store.connect(self.db)) as con:
            self.assertFalse(con.execute("SELECT * FROM voice_checkpoints").fetchall())
            self.assertFalse(con.execute("PRAGMA foreign_key_check").fetchall())

    def test_recovery_of_old_missing_report_is_idempotent(self):
        finish(self.db, self.data)
        with closing(player_store.connect(self.db)) as con:
            con.execute("DELETE FROM kv_daily_reports")
        self.assertEqual(player_store.recover_daily_reports(self.db), 1)
        self.assertEqual(player_store.recover_daily_reports(self.db), 0)
        self.assertEqual(self.report_rows()["Alpha"]["total_grenades"], 40)

    def test_historical_roster_does_not_follow_current_transfers_or_removals(self):
        finish(self.db, self.data)
        later = snapshot()
        later.update(session_date="2026-09-11", grenade_date="2026-09-11")
        later["players"]["100"]["squad"] = 4
        later["players"].pop("200")
        player_store.save_bot_snapshot(self.db, later)
        collected = player_store.collect_kv_daily_stats(self.db, date_from="2026-09-10", guild_id=42)
        roster = {r["game_nick"]: r for r in collected["historical_roster"]}
        self.assertEqual(roster["Alpha"]["squad"], 1)
        self.assertEqual(roster["Beta"]["squad"], 2)
        text = "\n".join(e.description for e in bot.build_stats_embeds(later, collected))
        self.assertIn("Beta", text)
        self.assertIn("Отряд 1", text)
        self.assertNotIn("Отряд 4", text)

    def test_export_round_trip_keeps_unknown_voice_and_endpoint_total(self):
        data = finish(self.db, self.data)
        with patch.object(bot, "SCANS_ETAPY_DIR", self.root / "steps"), patch.object(bot, "SCANS_ITOGI_DIR", self.root / "daily"):
            path = bot.save_grenade_scan(data, "21:20")
        parsed = player_store.parse_kv_daily_report(path)
        rows = {r["raw_nick"]: r for r in parsed["rows"]}
        self.assertEqual(rows["Alpha"]["stages"], [None, None, 10])
        self.assertEqual(rows["Alpha"]["total_grenades"], 40)
        self.assertIsNone(rows["Beta"]["voice_seconds"])
        self.assertEqual(rows["Alpha"]["voice_seconds"], 0)

    def test_admin_edit_preserves_unknowns_and_unchanged_endpoint_total(self):
        finish(self.db, self.data)
        rows = self.report_rows()
        a, b = rows["Alpha"]["row_no"], rows["Beta"]["row_no"]
        with patch.object(admin_ui, "DB_PATH", self.db):
            client = admin_ui.app.test_client()
            response = client.post("/day/2026-09-10/save", data={
                f"g{a}_1": "", f"g{a}_2": "", f"g{a}_3": "10", f"v{a}": "",
                f"w{b}": "0",
            })
            self.assertEqual(response.status_code, 302)
            self.assertEqual(self.report_rows()["Alpha"]["total_grenades"], 40)
            self.assertIsNone(self.report_rows()["Alpha"]["voice_seconds"])
            self.assertEqual(self.report_rows()["Beta"]["total_grenades"], 0)
            client.post("/day/2026-09-10/save", data={f"g{a}_3": ""})
            self.assertIsNone(self.report_rows()["Alpha"]["total_grenades"])


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_partial_api_failure_retries_only_missing_players(self):
        with tempfile.TemporaryDirectory() as td, ExitStack() as stack:
            db = Path(td) / "test.sqlite3"
            data = snapshot()
            player_store.save_bot_snapshot(db, data)
            stack.enter_context(patch.object(bot, "db_lock", asyncio.Lock()))
            stack.enter_context(patch.object(bot, "load_db", side_effect=lambda: player_store.load_bot_snapshot(db, 42)))
            stack.enter_context(patch.object(bot, "save_db", side_effect=lambda d: player_store.save_bot_snapshot(db, d)))
            stack.enter_context(patch.object(bot, "is_cw_day", return_value=True))
            stack.enter_context(patch.object(bot, "ensure_session_reset", new=AsyncMock(side_effect=lambda d: d)))
            stack.enter_context(patch.object(bot, "upsert_status_messages", new=AsyncMock()))
            stack.enter_context(patch.object(bot, "voice_scan_is_active", return_value=False))
            stack.enter_context(patch.object(bot, "save_grenade_scan", side_effect=OSError("export unavailable")))
            api = stack.enter_context(patch.object(bot, "scan_grenades", new=AsyncMock(side_effect=[
                {"Alpha": 100, "Beta": None}, {"Beta": 200},
                {"Alpha": 110, "Beta": None}, {"Beta": None},
            ])))
            await bot.run_grenade_step("20:00", None)
            self.assertIsNone(bot.load_db().get("last_grenade_step"))
            await bot.run_grenade_step("20:00", None)
            self.assertEqual(api.await_args_list[1].args[0], ["Beta"])
            self.assertEqual(bot.load_db()["grenade_history"]["Alpha"]["20:00"], 100)
            await bot.run_grenade_step("21:20", "20:50")
            self.assertFalse(bot.load_db()["kv_finished"])
            await bot.run_grenade_step("21:20", "20:50", close_incomplete=True)
            self.assertEqual(api.await_args_list[3].args[0], ["Beta"])
            self.assertTrue(bot.load_db()["kv_finished"])
            with closing(player_store.connect(db)) as con:
                rows = {r["raw_nick"]: r["total_grenades"] for r in con.execute("SELECT * FROM kv_daily_players")}
                self.assertEqual(rows, {"Alpha": 10, "Beta": None})

    async def test_voice_zero_only_for_observed_channel_and_known_state(self):
        for flag, expected in [(False, {"100": 0.0}), (None, {})]:
            member = SimpleNamespace(id=100, bot=False)
            vc = Mock()
            vc.channel = SimpleNamespace(members=[member])
            vc.is_connected.return_value = True
            vc.is_listening.return_value = True
            vc.get_speaking.return_value = flag
            state = {
                "active": True, "vc": vc, "manual": False, "eligible_ids": {"100", "200"},
                "last_packet_at": time.monotonic(), "totals": {}, "speaking": {},
                "last_pcm_ok": {}, "last_loud": {}, "gate_debug": {}, "gate_debug_log": {},
            }
            async def stop_after_one_tick(_):
                bot._voice_scan["active"] = False
            with patch.dict(bot._voice_scan, state, clear=True), \
                 patch.object(bot, "_persist_voice_totals_async", new=AsyncMock()), \
                 patch.object(bot, "_flush_voice_calibration"), \
                 patch.object(bot, "VOICE_SCAN_GATE_DEBUG", False), \
                 patch.object(bot, "VOICE_VOLUME_CALIBRATION_DEBUG", False), \
                 patch.object(bot.asyncio, "sleep", new=AsyncMock(side_effect=stop_after_one_tick)):
                await bot._voice_scan_poll_loop()
                self.assertEqual(bot._voice_scan["totals"], expected)


if __name__ == "__main__":
    unittest.main()
