#!/usr/bin/env python3
from __future__ import annotations
import ast
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
BOT = ROOT / "bot.py"
TEST = ROOT / "tools" / "test_runtime_logic.py"

def one(text, old, new, label):
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected one match, found {count}")
    return text.replace(old, new, 1)

def main():
    text = BOT.read_text(encoding="utf-8")
    marker = '@bot.tree.command(name="setup", description="Настроить канал логов и войс-каналы КВ")\n'
    helper = '''async def require_bot_admin(interaction: discord.Interaction) -> bool:
    actor = await resolve_member(interaction)
    if is_bot_admin(actor):
        return True
    await interaction.response.send_message(
        embed=make_reply_embed("❌  Нет прав", "Только администратор или Manage Server.", color=COLOR_ERR),
        ephemeral=True,
    )
    return False


'''
    text = one(text, marker, helper + marker, "admin helper")
    text = one(text, '''):
    try:
        day = player_store.parse_match_date(match_date)
    except ValueError as exc:
        await interaction.response.send_message(str(exc), ephemeral=True)
        return
    if interaction.guild is None:
''', '''):
    if not await require_bot_admin(interaction):
        return
    if interaction.guild is None:
''', "setup NameError")

    changes = (
      ('async def cmd_set_log(interaction: discord.Interaction, channel: discord.TextChannel):\n    if interaction.guild is None:\n', 'async def cmd_set_log(interaction: discord.Interaction, channel: discord.TextChannel):\n    if not await require_bot_admin(interaction):\n        return\n    if interaction.guild is None:\n', 'set_log'),
      ('async def cmd_voice_add(interaction: discord.Interaction, channel: discord.VoiceChannel):\n    if interaction.guild is None:\n', 'async def cmd_voice_add(interaction: discord.Interaction, channel: discord.VoiceChannel):\n    if not await require_bot_admin(interaction):\n        return\n    if interaction.guild is None:\n', 'voice_add'),
      ('async def cmd_voice_remove(interaction: discord.Interaction, channel: discord.VoiceChannel):\n    await interaction.response.defer(ephemeral=True)\n', 'async def cmd_voice_remove(interaction: discord.Interaction, channel: discord.VoiceChannel):\n    if not await require_bot_admin(interaction):\n        return\n    await interaction.response.defer(ephemeral=True)\n', 'voice_remove'),
      ('async def cmd_voice_clear(interaction: discord.Interaction):\n    await interaction.response.defer(ephemeral=True)\n', 'async def cmd_voice_clear(interaction: discord.Interaction):\n    if not await require_bot_admin(interaction):\n        return\n    await interaction.response.defer(ephemeral=True)\n', 'voice_clear'),
      ('async def cmd_refresh(interaction: discord.Interaction):\n    await interaction.response.defer(ephemeral=True)\n', 'async def cmd_refresh(interaction: discord.Interaction):\n    actor = await resolve_member(interaction)\n    if not can_manage_kv(actor):\n        await interaction.response.send_message(embed=deny_embed(), ephemeral=True)\n        return\n    await interaction.response.defer(ephemeral=True)\n', 'refresh'),
    )
    for old,new,label in changes:
        text=one(text,old,new,label)

    text=one(text, '''async def cmd_scan(interaction: discord.Interaction, match_date: str, map_name: app_commands.Choice[str], attachment: discord.Attachment):
    try:
        normalized_date = player_store.parse_match_date(match_date)
''', '''async def cmd_scan(interaction: discord.Interaction, match_date: str, map_name: app_commands.Choice[str], attachment: discord.Attachment):
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
''', 'scan validation')

    text=one(text, '''    today = today_msk_str()
    finished = is_kv_finished(data)
    has_scans = bool(data.get("grenade_history"))
''', '''    today = today_msk_str()
    finished = is_kv_finished(data)
    has_scans = bool(data.get("grenade_history"))
    if data.get("session_date") != today and n.time() > kv_final_time(n):
        return data
''', 'late start')

    text=one(text, '''        # ЛС неявившимся
        if t == absent_t and f"{minute_key}:absent_dm" not in done:
            done.add(f"{minute_key}:absent_dm")
''', '''        # ЛС неявившимся: догон до закрытия явки, без дублей.
        absent_key = f"{now.strftime('%Y-%m-%d')}:absent_dm"
        if absent_t <= t < kv_attendance_end(now) and absent_key not in done:
            done.add(absent_key)
''', 'DM catchup')
    text=one(text, '        minute_key = now.strftime("%Y-%m-%d %H:%M")\n', '', 'minute key')
    ast.parse(text)
    setup=text[text.index('async def cmd_setup'):text.index('async def cmd_set_log')]
    if 'match_date' in setup:
        raise RuntimeError('post-check failed')
    BOT.write_text(text,encoding='utf-8')
    TEST.write_text('''from __future__ import annotations
import ast
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
import bot

class RuntimeLogicTests(unittest.TestCase):
    def test_schedule(self):
        thu=bot.MSK.localize(datetime(2026,9,17,12)); sun=bot.MSK.localize(datetime(2026,9,20,12))
        self.assertEqual(bot.kv_start(thu).strftime("%H:%M"),"19:30")
        self.assertEqual([t.strftime("%H:%M") for _,t,_ in bot.kv_grenade_steps(thu)],["20:05","20:25","20:50","21:15"])
        self.assertEqual(bot.kv_start(sun).strftime("%H:%M"),"18:30")
        self.assertEqual([t.strftime("%H:%M") for _,t,_ in bot.kv_grenade_steps(sun)],["19:00","19:20","19:40","20:00","20:15"])
    def test_setup_source(self):
        source=Path(bot.__file__).read_text(encoding="utf-8")
        tree=ast.parse(source)
        setup=next(n for n in tree.body if isinstance(n,ast.AsyncFunctionDef) and n.name=="cmd_setup")
        self.assertNotIn("match_date",{n.id for n in ast.walk(setup) if isinstance(n,ast.Name)})
class LateStartTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_late_fake_session(self):
        late=bot.MSK.localize(datetime(2026,9,17,22)); data=bot.default_db()
        with patch.object(bot,"now_msk",return_value=late): result=await bot.ensure_session_reset(data)
        self.assertFalse(result["kv_session_active"]); self.assertIsNone(result["session_date"])
if __name__=="__main__": unittest.main()
''',encoding='utf-8')
    print('OK: runtime fixes applied')
if __name__=='__main__': main()
