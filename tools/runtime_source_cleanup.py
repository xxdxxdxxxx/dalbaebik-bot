"""Build the cleaned runtime from the last full pre-cleanup bot source."""
from __future__ import annotations
import ast

REMOVE_FUNCTIONS = {
    "cmd_list", "cmd_reset_session", "cmd_voice_scan_start", "cmd_voice_scan_stop",
    "cmd_stats", "cmd_stats_dates", "cmd_scan_dates", "cmd_scan_view",
    "cmd_fix_slash",
    "format_voice_scan_live_table", "_update_manual_voice_scan_report", "format_voice_scan_report",
    "_stats_average", "_round_stats_value", "_fmt_stats_number", "_fmt_stats_time",
    "_fmt_stats_nick", "_stats_display_values", "calc_stats_efficiencies",
    "_fmt_stats_row", "build_stats_embeds", "_sheet_lock_path", "_entries_signature",
    "_cell_str", "_parse_discord_id", "_parse_int_cell", "_is_subs_header",
    "_squad_header_id", "_find_tech_sheet", "_read_tech_nick_map",
    "write_tech_sheet_from_players", "sync_tech_sheet", "_read_roster_table",
    "_read_pretty_grid", "read_squad_sheet", "apply_sheet_entries", "run_sheet_sync",
    "_auto_sheet_sync_once", "start_sheet_watcher", "stop_sheet_watcher",
    "cmd_sheet_sync", "cmd_squad_list", "cmd_sheet_path",
}
REFRESH = '''@bot.tree.command(name="refresh", description="Обновить сообщения явки и гранат")
async def cmd_refresh(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    await refresh_voice_presence()
    async with db_lock:
        data = load_db()
        _data, post_err = await upsert_status_messages(data, force=True)
    if post_err:
        await interaction.followup.send(embed=make_reply_embed("⚠️  Сообщения не обновлены", post_err, color=COLOR_LIVE), ephemeral=True)
        return
    await interaction.followup.send(embed=make_reply_embed("🔄  Обновлено", "Сообщения **ЯВКА** и **ГРАНАТЫ** перерисованы; войсы перепроверены.", color=COLOR_OK), ephemeral=True)
'''
HELP = '''@bot.tree.command(name="help", description="Список команд бота")
async def cmd_help(interaction: discord.Interaction):
    text = (
        "**Состав**\\n· `/add` · `/remove` · `/alias_add`\\n\\n"
        "**КВ**\\n· `/map` · `/refresh` · `/scan_now` · `/ls_send`\\n\\n"
        "**Аналитика**\\n· `/scan` — распознать и сохранить таб\\n· статистика и история доступны в админ-панели\\n\\n"
        "**Настройка**\\n· `/setup` · `/set_log` · `/settings`\\n"
        "· `/voice_add` · `/voice_remove` · `/voice_clear`\\n"
        "· `/access_add` · `/access_remove` · `/access_list`\\n\\n"
        "**Служебное**\\n· `/say` · `/help`"
    )
    await interaction.response.send_message(embed=make_reply_embed("📖  Команды", text, color=COLOR_INFO), ephemeral=True)
'''

def clean_source(text: str) -> str:
    tree = ast.parse(text)
    lines = text.splitlines(keepends=True)
    replacements = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        start = min([node.lineno] + [d.lineno for d in node.decorator_list]) - 1
        end = node.end_lineno
        if node.name in REMOVE_FUNCTIONS:
            replacements.append((start, end, ""))
        elif node.name == "cmd_refresh":
            replacements.append((start, end, REFRESH + "\n"))
        elif node.name == "cmd_help":
            replacements.append((start, end, HELP + "\n"))
    for start, end, replacement in sorted(replacements, reverse=True):
        lines[start:end] = [replacement] if replacement else []
    text = "".join(lines)
    text = text.replace("from sheet_watcher import watch_file\n", "")
    text = text.replace("from openpyxl import load_workbook\n", "")
    start = text.index("# Excel-таблица отрядов (OneDrive-путь или файл в корне проекта)")
    end = text.index("MSK = pytz.timezone", start)
    text = text[:start] + text[end:]
    for line in ("_sheet_last_mtime: float | None = None\n", "_sheet_last_sig: tuple | None = None\n", "_sheet_last_error: str | None = None\n", '_TECH_SHEET_NAMES = ("tech", "ids", "id", "база", "discord", "mapping", "привязки", "nicks")\n', "_tech_pending: bool = False\n", "_tech_lock_warned: bool = False\n", "_sheet_watcher_task: asyncio.Task | None = None\n"):
        text = text.replace(line, "")
    changes = {
        "- отряды + синк из Excel (SHEET_PATH)\n": "- состав и отряды управляются через локальную админ-панель\n",
        "/dm_absent": "/ls_send", 'name="dm_absent"': 'name="ls_send"',
        "async def cmd_dm_absent(": "async def cmd_ls_send(",
        '    for name in ("add", "remove", "list"):': '    for name in ("add", "remove"):',
        "    start_sheet_watcher()\n": "", "        await stop_sheet_watcher()\n": "",
        '            f"excel {SHEET_PATH.name}",\n': '            "состав: админ-панель + Clan Map",\n',
        'status = "finished · /reset_session?"': 'status = "КВ сегодня завершён"',
        'status = f"⏳ День КВ · сессия не открыта (жди тик или `/reset_session`)"': 'status = "⏳ День КВ · сессия откроется автоматически"',
        ' или используйте `/reset_session`': '; сессия откроется автоматически по расписанию',
        ' (тик старта явки / reset_session)': ' (тик старта явки / внутренний принудительный запуск)',
        'Скан / deletegren / reset_session / опасные KV-команды.': 'Скан и опасные KV-команды.',
        'id отряда (str) → название; подтягивается из Excel лист squads': 'id отряда (str) → название из базы',
        'Чтобы при смене регистра / правке Excel данные не «терялись».': 'Чтобы при смене регистра ника данные не «терялись».',
        '# Excel: отряды (squads.xlsx / SHEET_PATH)': '# История ников и сортировка состава',
        '    # tech = players.json (после unlock — Excel может подтормаживать)\n': '',
        '# Отряд «Чемпионы» / замены (Excel F6:F16) — не пинговать ЛС перед КВ': '# Отряд «Чемпионы» / замены — не пинговать ЛС перед КВ',
        '      · или по нику STALCRAFT (game_nick) — как в Excel/таблице': '      · или по нику STALCRAFT (game_nick) — как в админ-панели',
        '# Всегда состав отрядов 1–6 + Чемпионы (как в Excel); «Без отряда» не показываем.': '# Всегда состав отрядов 1–6 + Чемпионы; «Без отряда» не показываем.',
    }
    for old, new in changes.items():
        text = text.replace(old, new)
    ast.parse(text)
    return text
