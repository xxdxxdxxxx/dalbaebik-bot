#!/usr/bin/env python3
"""Apply verified runtime scheduling, API URL, and command-permission fixes."""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BOT = ROOT / "bot.py"
TEST = ROOT / "tools" / "test_runtime_logic.py"


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected one match, found {count}")
    return text.replace(old, new, 1)


def main() -> None:
    text = BOT.read_text(encoding="utf-8")

    text = replace_once(
        text,
        '''async def fetch_player_grenades(
    session: aiohttp.ClientSession,
    token: str,
    nickname: str,
) -> tuple[str, int | None]:
    """Один запрос profile → gre-thr."""
    safe = quote(str(nickname), safe="")
    url = f"{{https://eapi.stalcraft.net/{REGION}}}/character/by-name/{safe}/profile"
''',
        '''def stalcraft_profile_url(nickname: str) -> str:
    """Build a valid eAPI profile URL with a safely quoted character name."""
    safe = quote(str(nickname), safe="")
    return f"https://eapi.stalcraft.net/{REGION}/character/by-name/{safe}/profile"


async def fetch_player_grenades(
    session: aiohttp.ClientSession,
    token: str,
    nickname: str,
) -> tuple[str, int | None]:
    """Один запрос profile → gre-thr."""
    url = stalcraft_profile_url(nickname)
''',
        "STALCRAFT profile URL",
    )

    helper_marker = '@bot.tree.command(name="setup", description="Настроить канал логов и войс-каналы КВ")\n'
    helper = '''async def require_bot_admin(interaction: discord.Interaction) -> bool:
    """Require Administrator or Manage Server for bot configuration commands."""
    actor = await resolve_member(interaction)
    if is_bot_admin(actor):
        return True
    await interaction.response.send_message(
        embed=make_reply_embed(
            "❌  Нет прав", "Только администратор или Manage Server.", color=COLOR_ERR
        ),
        ephemeral=True,
    )
    return False


'''
    text = replace_once(text, helper_marker, helper + helper_marker, "admin helper")

    text = replace_once(
        text,
        '''):
    try:
        day = player_store.parse_match_date(match_date)
    except ValueError as exc:
        await interaction.response.send_message(str(exc), ephemeral=True)
        return
    if interaction.guild is None:
''',
        '''):
    if not await require_bot_admin(interaction):
        return
    if interaction.guild is None:
''',
        "broken setup preamble",
    )

    command_guards = [
        (
            'async def cmd_set_log(interaction: discord.Interaction, channel: discord.TextChannel):\n    if interaction.guild is None:\n',
            'async def cmd_set_log(interaction: discord.Interaction, channel: discord.TextChannel):\n    if not await require_bot_admin(interaction):\n        return\n    if interaction.guild is None:\n',
            "set_log guard",
        ),
        (
            'async def cmd_voice_add(interaction: discord.Interaction, channel: discord.VoiceChannel):\n    if interaction.guild is None:\n',
            'async def cmd_voice_add(interaction: discord.Interaction, channel: discord.VoiceChannel):\n    if not await require_bot_admin(interaction):\n        return\n    if interaction.guild is None:\n',
            "voice_add guard",
        ),
        (
            'async def cmd_voice_remove(interaction: discord.Interaction, channel: discord.VoiceChannel):\n    await interaction.response.defer(ephemeral=True)\n',
            'async def cmd_voice_remove(interaction: discord.Interaction, channel: discord.VoiceChannel):\n    if not await require_bot_admin(interaction):\n        return\n    await interaction.response.defer(ephemeral=True)\n',
            "voice_remove guard",
        ),
        (
            'async def cmd_voice_clear(interaction: discord.Interaction):\n    await interaction.response.defer(ephemeral=True)\n',
            'async def cmd_voice_clear(interaction: discord.Interaction):\n    if not await require_bot_admin(interaction):\n        return\n    await interaction.response.defer(ephemeral=True)\n',
            "voice_clear guard",
        ),
    ]
    for old, new, label in command_guards:
        text = replace_once(text, old, new, label)

    text = replace_once(
        '''async def cmd_refresh(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
''',
        '''async def cmd_refresh(interaction: discord.Interaction):
    actor = await resolve_member(interaction)
    if not can_manage_kv(actor):
        await interaction.response.send_message(embed=deny_embed(), ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
''',
        "refresh guard",
    )

    text = replace_once(
        '''async def cmd_scan(interaction: discord.Interaction, match_date: str, map_name: app_commands.Choice[str], attachment: discord.Attachment):
    try:
        normalized_date = player_store.parse_match_date(match_date)
''',
        '''async def cmd_scan(interaction: discord.Interaction, match_date: str, map_name: app_commands.Choice[str], attachment: discord.Attachment):
    actor = await resolve_member(interaction)
    if not can_manage_kv(actor):
        await interaction.response.send_message(embed=deny_embed(), ephemeral=True)
        return
    content_type = (attachment.content_type or "").lower()
    if not content_type.startswith("image/"):
        await interaction.response.send_message("❌ Нужен файл изображения.", ephemeral=True)
        return
    if attachment.size > 10 * 1024 * 1024:
        await interaction.response.send_message("❌ Изображение больше 10 МБ.", ephemeral=True)
        return
    try:
        normalized_date = player_store.parse_match_date(match_date)
''',
        "scan guard and validation",
    )

    text = replace_once(
        '''    today = today_msk_str()
    finished = is_kv_finished(data)
    has_scans = bool(data.get("grenade_history"))
''',
        '''    today = today_msk_str()
    finished = is_kv_finished(data)
    has_scans = bool(data.get("grenade_history"))

    # Do not fabricate a whole KV session when the bot was offline all day and
    # first starts only after the final. Existing same-day sessions still resume.
    if data.get("session_date") != today and n.time() > kv_final_time(n):
        return data
''',
        "late startup guard",
    )

    text = replace_once(
        '''        # ЛС неявившимся
        if t == absent_t and f"{minute_key}:absent_dm" not in done:
            done.add(f"{minute_key}:absent_dm")
''',
        '''        # ЛС неявившимся. Catch up until attendance closes if one loop tick
        # was missed; persistent delivery records still prevent duplicate DMs.
        absent_key = f"{now.strftime('%Y-%m-%d')}:absent_dm"
        if absent_t <= t < kv_attendance_end(now) and absent_key not in done:
            done.add(absent_key)
''',
        "absent DM catch-up",
    )
    text = text.replace('        minute_key = now.strftime("%Y-%m-%d %H:%M")\n', '')

    ast.parse(text)
    if 'f"{{https://eapi.stalcraft.net/' in text or "parse_match_date(match_date)\n    except" in text[text.index("async def cmd_setup"):text.index("async def cmd_set_log")]:
        raise RuntimeError("critical broken markers remain")
    BOT.write_text(text, encoding="utf-8")

    TEST.write_text('''from __future__ import annotations

import ast
import asyncio
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import bot


class RuntimeLogicTests(unittest.TestCase):
    def test_default_moscow_schedule(self):
        thu = bot.MSK.localize(datetime(2026, 9, 17, 12, 0))
        sun = bot.MSK.localize(datetime(2026, 9, 20, 12, 0))
        self.assertEqual(bot.kv_start(thu).strftime("%H:%M"), "19:30")
        self.assertEqual(bot.kv_absent_dm(thu).strftime("%H:%M"), "19:50")
        self.assertEqual([t.strftime("%H:%M") for _, t, _ in bot.kv_grenade_steps(thu)],
                         ["20:05", "20:25", "20:50", "21:15"])
        self.assertEqual(bot.kv_start(sun).strftime("%H:%M"), "18:30")
        self.assertEqual(bot.kv_absent_dm(sun).strftime("%H:%M"), "18:45")
        self.assertEqual([t.strftime("%H:%M") for _, t, _ in bot.kv_grenade_steps(sun)],
                         ["19:00", "19:20", "19:40", "20:00", "20:15"])

    def test_stalcraft_profile_url(self):
        with patch.object(bot, "REGION", "ru"):
            self.assertEqual(
                bot.stalcraft_profile_url("Nick Name/Тест"),
                "https://eapi.stalcraft.net/ru/character/by-name/Nick%20Name%2F%D0%A2%D0%B5%D1%81%D1%82/profile",
            )

    def test_setup_has_no_undefined_match_date(self):
        source = Path(bot.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        setup = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "cmd_setup")
        names = {node.id for node in ast.walk(setup) if isinstance(node, ast.Name)}
        self.assertNotIn("match_date", names)


class LateStartupTests(unittest.IsolatedAsyncioTestCase):
    async def test_late_first_start_does_not_fabricate_session(self):
        late = bot.MSK.localize(datetime(2026, 9, 17, 22, 0))
        data = bot.default_db()
        with patch.object(bot, "now_msk", return_value=late):
            result = await bot.ensure_session_reset(data)
        self.assertFalse(result["kv_session_active"])
        self.assertIsNone(result["session_date"])


if __name__ == "__main__":
    unittest.main()
''', encoding="utf-8")
    print("Applied runtime logic fixes and created tools/test_runtime_logic.py")


if __name__ == "__main__":
    main()
