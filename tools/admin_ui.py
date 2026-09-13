#!/usr/bin/env python3
"""Локальная админ-панель для scan_stats.sqlite3.

Запуск:  python tools/admin_ui.py   →  http://127.0.0.1:8787
(также поднимается автоматически вместе с bot.py)

Страницы:
  /               — дни КВ, сгруппированы по месяцам;
  /day/<дата>     — сводная таблица дня: игрок → таб (У/С/П/СЧЁТ за день),
                    гранаты по этапам, войс; правка гранат/войса прямо в строке,
                    исходные табы — в раскрывающемся разделе;
  /players, /player/<id> — история игрока по дням (поиск накруток).

Правки сразу видны в /stats бота (исторические данные читаются из SQLite).
"""
from __future__ import annotations

import argparse
import html
import sys
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import player_store  # noqa: E402
from flask import Flask, redirect, request, url_for  # noqa: E402

app = Flask(__name__)

DB_PATH = ROOT / "scan_stats.sqlite3"

RU_MONTHS = ["Январь", "Февраль", "Март", "Апрель", "Май", "Июнь", "Июль",
             "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"]
RU_WD = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
STAGE_ROMAN = {1: "I", 2: "II", 3: "III", 4: "IV", 5: "V", 6: "VI"}


def q(con: Any, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    return [dict(r) for r in con.execute(sql, params)]


def one(con: Any, sql: str, params: tuple = ()) -> dict[str, Any] | None:
    row = con.execute(sql, params).fetchone()
    return dict(row) if row is not None else None


def fmt_sec(seconds: Any) -> str:
    try:
        s = int(seconds)
    except (TypeError, ValueError):
        return "—"
    if s >= 3600:
        return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"
    return f"{s // 60:02d}:{s % 60:02d}"


def fmt_date_ru(day: str) -> str:
    try:
        d = datetime.strptime(day, "%Y-%m-%d")
        return f"{d.strftime('%d.%m.%y')} {RU_WD[d.weekday()]}"
    except ValueError:
        return day


def month_ru(day: str) -> str:
    try:
        d = datetime.strptime(day, "%Y-%m-%d")
        return f"{RU_MONTHS[d.month - 1]} {d.year}"
    except ValueError:
        return day[:7]


def esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""))


def fmt_attended(value: Any) -> str:
    if value is None:
        return "—"
    return "✅" if int(value) else "❌"


def stage_label(n: Any) -> str:
    try:
        return STAGE_ROMAN.get(int(n), str(n))
    except (TypeError, ValueError):
        return "?"


PAGE = """<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{title} · Clan Analytics</title><style>
:root{{--bg:#f7f7f5;--panel:#fff;--soft:#f1f1ef;--line:#e5e5e2;--text:#242424;--dim:#787774;--blue:#2783de;--blue2:#e5f2fc;--green:#46a171;--red:#e56458}}
@media(prefers-color-scheme:dark){{:root{{--bg:#191919;--panel:#202020;--soft:#292928;--line:#3b3b39;--text:#fff;--dim:#aaa;--blue:#5e9fe8;--blue2:#253343;--green:#72bc8f;--red:#e97366}}}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--text);font:15px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Arial,sans-serif}}a{{color:inherit;text-decoration:none}}a:hover{{color:var(--blue)}}
.top{{position:sticky;top:0;z-index:20;background:var(--bg);border-bottom:1px solid var(--line)}}.topin,.wrap{{width:min(1180px,calc(100% - 32px));margin:auto}}.topin{{height:64px;display:flex;align-items:center;justify-content:space-between}}.brand{{font-weight:750;display:flex;align-items:center;gap:9px}}.logo{{width:30px;height:30px;border-radius:8px;background:var(--text);color:var(--bg);display:grid;place-items:center;font-size:12px}}.nav{{display:flex;gap:4px;padding:4px;background:var(--panel);border:1px solid var(--line);border-radius:10px}}.nav a{{min-height:36px;padding:0 13px;display:flex;align-items:center;border-radius:7px;color:var(--dim);font-weight:650}}.nav a.active{{background:var(--blue2);color:var(--blue)}}
.wrap{{padding:34px 0 70px}}h1{{font-size:30px;line-height:1.15;letter-spacing:-.025em;margin:0 0 6px}}h2{{font-size:18px;margin:30px 0 12px}}.pagehead{{display:flex;justify-content:space-between;align-items:end;gap:16px;margin-bottom:24px}}.subtitle,.hint,.dim,.chips{{color:var(--dim)}}
.metrics{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:18px 0 26px}}.metric,.panel{{background:var(--panel);border:1px solid var(--line);border-radius:10px}}.panel{{overflow:auto}}.metric{{padding:16px}}.metric label{{display:block;color:var(--dim);font-size:13px;font-weight:650}}.metric b{{display:block;font-size:26px;letter-spacing:-.03em;margin-top:5px}}.panel{{padding:14px;margin-bottom:14px}}
.actions,.chips,nav.days{{display:flex;gap:8px;flex-wrap:wrap;align-items:center}}.button,button{{min-height:40px;padding:0 13px;border:1px solid var(--line);border-radius:8px;background:var(--panel);color:var(--text);font:650 14px inherit;cursor:pointer;display:inline-flex;align-items:center;justify-content:center}}.button:hover,button:hover{{background:var(--soft)}}button.save,.button.primary{{background:var(--blue);border-color:var(--blue);color:#fff}}button.danger{{min-height:31px;padding:0 9px;color:var(--red);background:transparent}}button:disabled{{opacity:.4}}
.scroll{{overflow:auto;max-height:68vh;border:1px solid var(--line);border-radius:8px}}table{{width:100%;border-collapse:collapse}}th,td{{padding:10px 11px;text-align:center;white-space:nowrap;border-bottom:1px solid var(--line);font-variant-numeric:tabular-nums}}th{{position:sticky;top:0;background:var(--soft);color:var(--dim);font-size:12px;text-transform:uppercase;letter-spacing:.035em;z-index:2}}tr:last-child td{{border-bottom:0}}tbody tr:hover{{background:var(--blue2)}}td.nick,th.nick{{text-align:left;max-width:240px;overflow:hidden;text-overflow:ellipsis}}th.gcol,td.gcol{{background:var(--blue2)}}.total{{font-weight:750}}input[type=number],input#flt,select{{min-height:38px;border:1px solid var(--line);border-radius:7px;background:var(--panel);color:var(--text);padding:0 9px}}input[type=number]{{width:70px;text-align:center}}input#flt{{width:min(100%,300px)}}
.chips{{margin:10px 0 18px}}.chip{{padding:5px 9px;border:1px solid var(--line);border-radius:7px;background:var(--panel)}}.seg{{display:inline-flex;gap:3px;padding:3px;border:1px solid var(--line);border-radius:9px;background:var(--soft);margin:4px 0 14px}}.seg a,.seg b{{min-height:34px;padding:0 12px;display:flex;align-items:center;border-radius:6px}}.seg b{{background:var(--panel)}}.float{{position:fixed;right:20px;bottom:20px;z-index:5;box-shadow:0 8px 24px #0003}}.back{{display:inline-flex;color:var(--dim);margin-bottom:14px}}.banner{{display:inline-flex;padding:8px 12px;border-radius:8px;background:#e8f1ec;color:var(--green);font-weight:700}}.flag{{color:var(--red)}}details summary{{cursor:pointer;color:var(--blue);padding:10px 0}}nav.days{{margin:12px 0}}nav.days select{{max-width:210px}}.month{{color:var(--dim);font-size:12px;font-weight:750;text-transform:uppercase;letter-spacing:.06em;margin:20px 0 7px}}
@media(max-width:760px){{.topin,.wrap{{width:calc(100% - 24px)}}.topin{{height:58px}}.brand .txt{{display:none}}.wrap{{padding-top:24px}}.metrics{{grid-template-columns:repeat(2,1fr)}}h1{{font-size:25px}}.pagehead{{align-items:start;flex-direction:column}}th,td{{padding:9px 8px}}}}
</style></head><body><header class="top"><div class="topin"><a class="brand" href="{home}"><span class="logo">CA</span><span class="txt">Clan Analytics</span></a><nav class="nav"><a class="{ov}" href="{home}">Обзор</a><a class="{pl}" href="{players}">Игроки</a></nav></div></header><main class="wrap">{body}</main></body></html>"""


def render(title: str, body: str) -> str:
    path=request.path
    return PAGE.format(title=html.escape(title),body=body,home=url_for("index"),players=url_for("players"),ov="active" if path=="/" or path.startswith("/day/") else "",pl="active" if path.startswith("/player") else "")


def all_dates(con: Any) -> list[str]:
    return [r["date"] for r in q(con, """
        SELECT match_date date FROM scans
        UNION SELECT match_date FROM kv_daily_reports
        UNION SELECT match_date FROM grenade_session_stats WHERE match_date IS NOT NULL
        ORDER BY date DESC""")]


# ---------------------------------------------------------------- страницы --

@app.get("/")
def index():
    with closing(player_store.connect(DB_PATH)) as con:
        rows = q(con, """
            SELECT d.date, COALESCE(t.tabs,0) tabs, COALESCE(g.players,0) players,
                   COALESCE(g.grenades,0) grenades, COALESCE(g.voice,0) voice
            FROM (SELECT match_date date FROM scans UNION
                  SELECT match_date FROM kv_daily_reports UNION
                  SELECT match_date FROM grenade_session_stats WHERE match_date IS NOT NULL) d
            LEFT JOIN (SELECT match_date, COUNT(*) tabs FROM scans GROUP BY match_date) t
                   ON t.match_date=d.date
            LEFT JOIN (SELECT r.match_date, COUNT(DISTINCT p.player_id) players,
                              SUM(p.total_grenades) grenades, SUM(p.voice_seconds) voice
                       FROM kv_daily_reports r JOIN kv_daily_players p ON p.report_id=r.id
                       GROUP BY r.match_date) g ON g.match_date=d.date
            ORDER BY d.date DESC""")
    out, cur_month = [], None
    for r in rows:
        m = month_ru(r["date"])
        if m != cur_month:
            cur_month = m
            out.append(f"<div class='month'>{esc(m)}</div>")
        out.append(
            f"<tr><td class='nick'><a href='{url_for('day', date=r['date'])}'>"
            f"{fmt_date_ru(r['date'])}</a></td>"
            f"<td>{r['tabs'] or 0}</td><td>{r['players'] or 0}</td>"
            f"<td class='total'>{r['grenades'] or 0}</td>"
            f"<td>{fmt_sec(r['voice'])}</td></tr>")
    total_tabs = sum(int(r["tabs"] or 0) for r in rows)
    total_grenades = sum(int(r["grenades"] or 0) for r in rows)
    total_voice = sum(int(r["voice"] or 0) for r in rows)
    latest = rows[0]["date"] if rows else None
    summary = ("<div class='metrics'>"
               f"<div class='metric'><label>Дней КВ</label><b>{len(rows)}</b></div>"
               f"<div class='metric'><label>Табов</label><b>{total_tabs}</b></div>"
               f"<div class='metric'><label>Гранат</label><b>{total_grenades}</b></div>"
               f"<div class='metric'><label>Войс всего</label><b>{fmt_sec(total_voice)}</b></div></div>")
    body = ("<div class='pagehead'><div><h1>Обзор</h1>"
            "<p class='subtitle'>Короткая сводка и переход к любому дню КВ.</p></div>"
            + (f"<a class='button primary' href='{url_for('day',date=latest)}'>Последний КВ</a>" if latest else "")
            + "</div>" + summary
            + "<div class='panel'><table><thead><tr><th>Дата</th><th>Табов</th>"
            "<th>Игроков</th><th>💣 Гранат</th><th>🎙 Войс</th></tr></thead>"
            f"<tbody>{''.join(out)}</tbody></table></div>"
            f"<div class='actions'><a class='button' href='{url_for('players')}'>Все игроки</a></div>")
    return render("Дни", body)


@app.get("/players")
def players():
    with closing(player_store.connect(DB_PATH)) as con:
        rows = q(con, """
            SELECT p.id, p.canonical_nick, COUNT(DISTINCT r.match_date) days,
                   SUM(k.total_grenades) grenades,
                   MAX(r.match_date) last_day
            FROM players p
            LEFT JOIN kv_daily_players k ON k.player_id=p.id
            LEFT JOIN kv_daily_reports r ON r.id=k.report_id
            GROUP BY p.id ORDER BY p.canonical_nick COLLATE NOCASE""")
    trs = "".join(
        f"<tr><td class='nick'><a href='{url_for('player', player_id=r['id'])}'>"
        f"{esc(r['canonical_nick'])}</a></td>"
        f"<td>{r['days'] or 0}</td><td class='total'>{r['grenades'] or 0}</td>"
        f"<td>{fmt_date_ru(r['last_day']) if r['last_day'] else '—'}</td></tr>"
        for r in rows)
    body = (f"<div class='pagehead'><div><h1>Игроки</h1><p class='subtitle'>История, гранаты и быстрый поиск по составу.</p></div></div>"
            "<input id='flt' placeholder='Поиск по нику…' aria-label='Поиск игрока' oninput="
            "\"[...document.querySelectorAll('tbody tr')].forEach(tr=>tr.style.display"
            "=tr.textContent.toLowerCase().includes(this.value.toLowerCase())?'':'none')\">"
            "<div class='panel'><table><thead><tr><th>Ник</th><th>Дней</th>"
            "<th>💣 Гранат всего</th><th>Последний день</th></tr></thead>"
            f"<tbody>{trs}</tbody></table></div>")
    return render("Игроки", body)


@app.get("/player/<int:player_id>")
def player(player_id: int):
    with closing(player_store.connect(DB_PATH)) as con:
        p = one(con, "SELECT canonical_nick FROM players WHERE id=?", (player_id,))
        if not p:
            return render("Нет игрока", "<p>Игрок не найден.</p>"), 404
        tabs = q(con, """SELECT s.match_date date, sp.kills k, sp.deaths d, sp.assists a,
                                sp.score sc, s.stage
                         FROM scan_players sp JOIN scans s ON s.id=sp.scan_id
                         WHERE sp.player_id=? ORDER BY s.match_date DESC, s.stage, s.id""",
                 (player_id,))
        days = q(con, """SELECT r.match_date date, k.total_grenades total,
                                k.voice_seconds voice, k.attended attended,
                                GROUP_CONCAT(g.stage_no||':'||g.grenades) stages
                         FROM kv_daily_players k
                         JOIN kv_daily_reports r ON r.id=k.report_id
                         LEFT JOIN kv_daily_grenade_stages g
                           ON g.report_id=k.report_id AND g.row_no=k.row_no
                         WHERE k.player_id=? GROUP BY r.match_date, k.report_id, k.row_no
                         ORDER BY r.match_date DESC""", (player_id,))
        snaps = q(con, """SELECT match_date date, GROUP_CONCAT(stat_key||'='||value) snap
                          FROM grenade_session_stats WHERE player_id=?
                          AND match_date IS NOT NULL
                          GROUP BY match_date ORDER BY match_date DESC""", (player_id,))
    max_stage = 0
    for r in days:
        stages = {}
        for part in (r.get("stages") or "").split(","):
            if ":" in part:
                sn, val = part.split(":", 1)
                stages[int(sn)] = val if val != "" else "·"
        r["stages"] = stages
        max_stage = max([max_stage, *stages.keys()])
    stage_cols = "".join(f"<th class='gcol'>{stage_label(i)}</th>"
                         for i in range(1, max_stage + 1))
    max_total = max((r["total"] or 0) for r in days) if days else 0
    trs = []
    for r in days:
        cells = "".join(f"<td class='gcol'>{esc(r['stages'].get(i, '—'))}</td>"
                        for i in range(1, max_stage + 1))
        flag = (" <span class='flag'>⚠</span>"
                if r["total"] and r["total"] == max_total and max_total > 150 else "")
        trs.append(
            f"<tr><td class='nick'><a href='{url_for('day', date=r['date'])}'>"
            f"{fmt_date_ru(r['date'])}</a></td>{cells}"
            f"<td class='total'>{r['total']}</td><td>{fmt_sec(r['voice'])}</td>"
            f"<td>{fmt_attended(r['attended'])}</td><td>{flag}</td></tr>")
    tab_rows = "".join(
        "<tr><td class='nick'><a href='{u}'>{d}</a></td><td>{st}</td>"
        "<td>{k}/{dt}/{a}</td><td>{sc}</td></tr>".format(
            u=url_for("day", date=t["date"]), d=fmt_date_ru(t["date"]),
            st=stage_label(t["stage"]), k=t["k"], dt=t["d"], a=t["a"], sc=t["sc"])
        for t in tabs)
    snap_rows = "".join(
        f"<tr><td>{fmt_date_ru(s['date'])}</td><td>{esc(s['snap'])}</td></tr>"
        for s in snaps)
    body = (f"<a class='back' href='{url_for('players')}'>← Игроки</a>"
            f"<h1>👤 {esc(p['canonical_nick'])}</h1>"
            "<h2>💣 Гранаты по этапам и 🎙 войс по дням</h2>"
            f"<div class='panel'><div class='scroll'><table><thead><tr><th>Дата</th>"
            f"{stage_cols}<th>Всего</th><th>Войс</th><th>Явка</th><th></th></tr></thead>"
            f"<tbody>{''.join(trs)}</tbody></table></div></div>"
            "<h2>📄 Табы (У/С/П · СЧЁТ)</h2>"
            "<div class='panel'><div class='scroll'><table><thead><tr><th>Дата</th>"
            f"<th>Этап</th><th>У/С/П</th><th>СЧЁТ</th></tr></thead>"
            f"<tbody>{tab_rows}</tbody></table></div></div>")
    if snap_rows:
        body += ("<h2>Счётчик-снапшоты (runtime)</h2>"
                 "<div class='panel'><table><thead><tr><th>Дата</th><th>Значения</th>"
                 f"</tr></thead><tbody>{snap_rows}</tbody></table></div>")
    return render(p["canonical_nick"], body)


def _day_data(con: Any, date: str):
    scans = q(con, """SELECT id, stage, map, scanned_at, source_filename FROM scans
                      WHERE match_date=? ORDER BY COALESCE(stage,99), id""", (date,))
    for s in scans:
        s["players"] = q(con, """SELECT sp.id row_id, sp.place, sp.nick, sp.kills,
                                 sp.deaths, sp.assists, sp.score,
                                 sp.player_id, p.canonical_nick
                                 FROM scan_players sp LEFT JOIN players p ON p.id=sp.player_id
                                 WHERE sp.scan_id=? ORDER BY sp.place""", (s["id"],))
        for r in s["players"]:
            if r["player_id"] is None:  # подтянуть игрока по нику/алиасу
                m = player_store.match_player_in_connection(con, str(r["nick"]))
                r["player_id"] = m.player_id if m else None
    report = one(con, """SELECT id, stage_count FROM kv_daily_reports
                         WHERE match_date=? ORDER BY id DESC LIMIT 1""", (date,))
    krows: list[dict[str, Any]] = []
    if report:
        krows = q(con, """SELECT k.row_no, k.player_id, k.raw_nick, k.discord_username,
                          k.squad_label, k.total_grenades, k.voice_seconds, k.attended,
                          p.canonical_nick
                          FROM kv_daily_players k LEFT JOIN players p ON p.id=k.player_id
                          WHERE k.report_id=? ORDER BY k.row_no""", (report["id"],))
        for k in krows:
            if k["player_id"] is None and k["raw_nick"]:
                m = player_store.match_player_in_connection(con, str(k["raw_nick"]))
                k["player_id"] = m.player_id if m else None
            stages: dict[int, int | None] = {}
            for r in con.execute("""SELECT stage_no, grenades FROM kv_daily_grenade_stages
                                    WHERE report_id=? AND row_no=?""",
                                  (report["id"], k["row_no"])):
                stages[int(r["stage_no"])] = int(r["grenades"] or 0)
            k["stages"] = stages
    return scans, report, krows


def _tab_totals(scans: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    """Суммы табов дня по игроку: pid → {k,d,a,s,n,rows}."""
    out: dict[int, dict[str, Any]] = {}
    for s in scans:
        for r in s["players"]:
            pid = r["player_id"]
            if pid is None:
                continue
            b = out.setdefault(pid, {"k": 0, "d": 0, "a": 0, "s": 0, "n": 0, "rows": []})
            b["k"] += int(r["kills"] or 0)
            b["d"] += int(r["deaths"] or 0)
            b["a"] += int(r["assists"] or 0)
            b["s"] += int(r["score"] or 0)
            b["n"] += 1
            b["rows"].append(f"{r['kills']}/{r['deaths']}/{r['assists']}·{r['score']}")
    return out


@app.get("/day/<date>")
def day(date: str):
    date = player_store.parse_match_date(date)
    mode = request.args.get("mode", "total")
    if mode not in {"total", "stages"}:
        mode = "total"
    with closing(player_store.connect(DB_PATH)) as con:
        scans, report, krows = _day_data(con, date)
        dates = all_dates(con)
    tab_sum = _tab_totals(scans)
    has_tabs = bool(scans)
    max_stage = max((max(k["stages"], default=0) for k in krows), default=0)
    max_stage = max(max_stage, report["stage_count"] if report else 0)
    stage_cols = ("".join(f"<th class='gcol'>{stage_label(i)}</th>"
                          for i in range(1, max_stage + 1))
                  if mode == "stages" else "")
    save_url = url_for("day_save", date=date)

    # ── сводная строка игрока: итог табов + гранаты + войс ────────────────
    rows_html = []
    for k in krows:
        nick = k["canonical_nick"] or k["raw_nick"] or "—"
        tab_cells = ""
        if has_tabs:
            t = tab_sum.get(k["player_id"]) if k["player_id"] else None
            if t:
                tip = esc(" | ".join(t["rows"]))
                kd = t["k"] / t["d"] if t["d"] else float(t["k"])
                avg_score = t["s"] / t["n"] if t["n"] else 0
                tab_cells = (f"<td class='sum' title='{tip}'>{t['k']}</td>"
                             f"<td class='sum' title='{tip}'>{t['d']}</td>"
                             f"<td class='sum' title='{tip}'>{t['a']}</td>"
                             f"<td class='sum' title='{tip}'>{t['s']}</td>"
                             f"<td class='sum'>{kd:.2f}</td><td class='sum'>{avg_score:.0f}</td>"
                             f"<td class='dim' title='{tip}'>{t['n']}</td>")
            else:
                tab_cells = ("<td class='dim'>—</td><td class='dim'>—</td>"
                             "<td class='dim'>—</td><td class='dim'>—</td>"
                             "<td class='dim'>—</td><td class='dim'>—</td><td class='dim'>0</td>")
        if mode == "stages":
            gcells = ""
            for i in range(1, max_stage + 1):
                val = k["stages"].get(i)
                shown = "" if val is None else val
                gcells += (f"<td class='gcol'><input type='number' min='0' "
                           f"name='g{k['row_no']}_{i}' value='{shown}' placeholder='·'></td>")
            total_cell = f"<td class='total'>{k['total_grenades'] or 0}</td>"
        else:
            gcells = ""
            total_cell = (f"<td class='gcol'><input type='number' min='0' "
                          f"name='w{k['row_no']}' value='{k['total_grenades'] or 0}' "
                          f"title='гранат за день (всего)'></td>")
        link = (f"<a href='{url_for('player', player_id=k['player_id'])}'>{esc(nick)}</a>"
                if k["player_id"] else esc(nick))
        rows_html.append(
            f"<tr><td class='dim'>{k['row_no']}</td><td class='nick'>{link}</td>"
            f"<td>{fmt_attended(k['attended'])}</td>{tab_cells}{gcells}{total_cell}"
            f"<td><input type='number' min='0' name='v{k['row_no']}' "
            f"value='{k['voice_seconds'] or 0}' title='{fmt_sec(k['voice_seconds'])}'></td>"
            f"<td><button class='danger' name='__del_row' value='{k['row_no']}' "
            f"formaction='{save_url}' onclick=\"return confirm('Удалить гранаты и войс "
            f"{esc(nick)} за {fmt_date_ru(date)}?')\">✕</button></td></tr>")

    # ── исходные табы: правка У/С/П/СЧЁТ по каждому табу ──────────────────
    scan_tables = []
    for s in scans:
        head = (
            "<tr><td colspan='6' style='text-align:left'>"
            f"<button class='danger' name='__del_scan' value='{s['id']}' "
            f"formaction='{save_url}' onclick=\"return confirm('Удалить весь таб?')\""
            ">✕ таб</button> <span class='dim'>"
            + " · ".join(filter(None, [
                f"этап {stage_label(s['stage'])}" if s["stage"] else "без этапа",
                esc(s["map"] or ""),
                esc((s["scanned_at"] or "")[:16]),
                f"файл: {esc(s['source_filename'])}" if s.get("source_filename") else "",
            ])) + "</span></td></tr>")
        rows = "".join(
            "<tr>"
            f"<td class='dim'>{r['place'] or ''}</td>"
            f"<td class='nick'>{esc(r['nick'])}"
            + (f" <span class='dim'>({esc(r['canonical_nick'])})</span>"
               if r["canonical_nick"] and r["canonical_nick"] != r["nick"] else "")
            + "</td>"
            f"<td><input type='number' min='0' name='t{r['row_id']}_k' value='{r['kills']}'></td>"
            f"<td><input type='number' min='0' name='t{r['row_id']}_d' value='{r['deaths']}'></td>"
            f"<td><input type='number' min='0' name='t{r['row_id']}_a' value='{r['assists']}'></td>"
            f"<td><input type='number' min='0' name='t{r['row_id']}_s' value='{r['score']}'></td>"
            "</tr>"
            for r in s["players"])
        scan_tables.append(head + rows)
    tabs_details = (
        "<details" + (" open" if len(scans) == 1 else "") + ">"
        f"<summary>✏️ Исходные табы ({len(scans)}) — правка У/С/П/СЧЁТ</summary>"
        "<div class='scroll'><table><thead><tr><th>#</th><th>Ник</th>"
        "<th>У</th><th>С</th><th>П</th><th>СЧЁТ</th></tr></thead>"
        f"<tbody>{''.join(scan_tables)}</tbody></table></div></details>"
        if scans else "<p class='hint'>Табов за этот день нет.</p>")

    # ── навигация по дням ──────────────────────────────────────────────────
    pos = dates.index(date) if date in dates else -1
    prev_d = dates[pos + 1] if 0 <= pos < len(dates) - 1 else None   # старее
    next_d = dates[pos - 1] if pos > 0 else None                     # новее
    options = "".join(
        f"<option value='{url_for('day', date=d)}'"
        + (" selected" if d == date else "") + f">{fmt_date_ru(d)}</option>"
        for d in dates)
    nav = ("<nav class='days'>"
           + (f"<button onclick=\"location='{url_for('day', date=prev_d)}'\">← старее</button>"
              if prev_d else "<button disabled>← старее</button>")
           + "<select onchange=\"if(this.value)location=this.value\">"
           f"<option value=''>— выбери день —</option>{options}</select>"
           + (f"<button onclick=\"location='{url_for('day', date=next_d)}'\">новее →</button>"
              if next_d else "<button disabled>новее →</button>")
           + f"<a href='{url_for('index')}'>все дни</a></nav>")

    banner = ""
    if request.args.get("saved"):
        banner = "<div class='banner ok-pill'>✅ Сохранено</div>"
    elif request.args.get("deleted"):
        banner = "<div class='banner ok-pill'>🗑 Удалено</div>"

    chips = ("<div class='chips'>"
             f"<span class='chip'>📄 табов: {len(scans)}</span>"
             f"<span class='chip'>👤 игроков: {len(krows)}</span>"
             f"<span class='chip'>💣 гранат: {sum(k['total_grenades'] or 0 for k in krows)}"
             "</span>"
             "<span class='chip'>🎙 войс: "
             + fmt_sec(sum(k["voice_seconds"] or 0 for k in krows)) + "</span></div>")

    tab_head = ("<th>У</th><th>С</th><th>П</th><th>Счёт</th><th>K/D</th><th>Ср/таб</th><th>Табы</th>"
                if has_tabs else "")
    toggle = ("<div class='seg'>" + ("<b>Всего</b>" if mode == "total" else f"<a href='{url_for('day',date=date,mode='total')}'>Всего</a>") + (f"<a href='{url_for('day',date=date,mode='stages')}'>По этапам</a>" if mode == "total" else "<b>По этапам</b>") + "</div>")

    kv_block = (
        "<h2>💣 Гранаты · 🎙 войс" + (" · 📄 итог табов" if has_tabs else "") + "</h2>"
        + toggle
        + ("<p class='hint'>У/С/П/СЧЁТ — итог дня по табам (наведи курсор — покажет "
           "разбивку по табам).</p>" if has_tabs else "")
        + "<div class='scroll'><table><thead><tr><th>#</th><th>Ник</th><th>Явка</th>"
        + tab_head + stage_cols
        + "<th>💣</th><th>🎙 Войс, сек</th><th></th></tr></thead>"
        + f"<tbody>{''.join(rows_html)}</tbody></table></div>"
        if krows else
        "<p class='hint'>Дневной отчёт за эту дату не найден "
        "(гранаты за этот день не импортировались).</p>")

    body = (f"<a class='back' href='{url_for('index')}'>← Обзор</a>"
            f"<div class='pagehead'><div><h1>{fmt_date_ru(date)}</h1><p class='subtitle'>Табы, явка, гранаты и войс за один день.</p></div></div>{nav}{chips}{banner}"
            f"<form method='post' action='{save_url}'>"
            f"<input type='hidden' name='mode' value='{mode}'>"
            + kv_block + tabs_details
            + "<button class='save float'>💾 Сохранить</button></form>")
    return render(date, body)


def _to_int(value: str | None) -> int | None:
    value = (value or "").strip()
    if value == "":
        return None
    try:
        return int(value)
    except ValueError:
        return None


@app.post("/day/<date>/save")
def day_save(date: str):
    date = player_store.parse_match_date(date)
    form = request.form
    mode = form.get("mode", "total")
    if mode not in {"total", "stages"}:
        mode = "total"

    with closing(player_store.connect(DB_PATH)) as con:
        con.execute("BEGIN IMMEDIATE")

        # удаление целого таба
        del_scan = _to_int(form.get("__del_scan"))
        if del_scan is not None:
            con.execute("DELETE FROM scan_players WHERE scan_id=?", (del_scan,))
            con.execute("DELETE FROM scans WHERE id=?", (del_scan,))
            con.execute("COMMIT")
            return redirect(url_for("day", date=date, deleted=1, mode=mode))

        report = one(con, """SELECT id,stage_count FROM kv_daily_reports WHERE match_date=?
                             ORDER BY id DESC LIMIT 1""", (date,))

        # удаление строки игрока (гранаты+войс за день)
        del_row = _to_int(form.get("__del_row"))
        if del_row is not None and report:
            pid = one(con, """SELECT player_id FROM kv_daily_players
                              WHERE report_id=? AND row_no=?""",
                      (report["id"], del_row))["player_id"]
            con.execute("DELETE FROM kv_daily_grenade_stages WHERE report_id=? AND row_no=?",
                        (report["id"], del_row))
            con.execute("DELETE FROM kv_daily_players WHERE report_id=? AND row_no=?",
                        (report["id"], del_row))
            if pid is not None:
                con.execute("""DELETE FROM grenade_session_stats
                               WHERE player_id=? AND match_date=?""", (pid, date))
            con.execute("COMMIT")
            return redirect(url_for("day", date=date, deleted=1, mode=mode))

        # правка табов
        for key, value in form.items():
            if not key.startswith("t") or "_" not in key:
                continue
            row_id_str, field = key[1:].rsplit("_", 1)
            if field not in {"k", "d", "a", "s"}:
                continue
            num = _to_int(value)
            if num is None or num < 0:
                continue
            column = {"k": "kills", "d": "deaths", "a": "assists", "s": "score"}[field]
            con.execute(f"UPDATE scan_players SET {column}=? WHERE id=?",
                        (num, int(row_id_str)))

        # правка гранат и войса
        if report:
            rid = report["id"]
            for key, value in form.items():
                if key.startswith("v") and key[1:].isdigit():
                    num = _to_int(value)
                    if not str(value).strip() or (num is not None and num >= 0):
                        con.execute("""UPDATE kv_daily_players SET voice_seconds=?
                                       WHERE report_id=? AND row_no=?""",
                                    (num, rid, int(key[1:])))
            # режим «всего»: правка общего числа напрямую; этапы затираются
            # только если число реально изменили
            for key, value in form.items():
                if not (key.startswith("w") and key[1:].isdigit()):
                    continue
                num = _to_int(value)
                if num is None or num < 0:
                    continue
                row_no = int(key[1:])
                cur = one(con, """SELECT total_grenades FROM kv_daily_players
                                  WHERE report_id=? AND row_no=?""", (rid, row_no))
                if cur is None or cur["total_grenades"] == num:
                    continue
                con.execute("""DELETE FROM kv_daily_grenade_stages
                               WHERE report_id=? AND row_no=?""", (rid, row_no))
                con.execute("""UPDATE kv_daily_players SET total_grenades=?
                               WHERE report_id=? AND row_no=?""", (num, rid, row_no))
            stage_updates: dict[int, dict[int, int | None]] = {}
            for key, value in form.items():
                if not key.startswith("g"):
                    continue
                row_str, stage_str = key[1:].split("_", 1)
                if not row_str.isdigit() or not stage_str.isdigit():
                    continue
                val = _to_int(value)
                if not 1 <= int(stage_str) <= report["stage_count"]:
                    continue
                if str(value).strip() and (val is None or val < 0):
                    continue
                stage_updates.setdefault(int(row_str), {})[int(stage_str)] = val
            for row_no, stages in stage_updates.items():
                existing = {r["stage_no"]: r["grenades"] for r in con.execute(
                    "SELECT stage_no,grenades FROM kv_daily_grenade_stages WHERE report_id=? AND row_no=?",
                    (rid, row_no))}
                if all(existing.get(stage_no) == val for stage_no, val in stages.items()):
                    continue  # retain an independently measured endpoint total
                for stage_no, val in stages.items():
                    if val is None:
                        con.execute("""DELETE FROM kv_daily_grenade_stages
                                       WHERE report_id=? AND row_no=? AND stage_no=?""",
                                    (rid, row_no, stage_no))
                    else:
                        con.execute("""INSERT INTO kv_daily_grenade_stages
                                       (report_id,row_no,stage_no,grenades) VALUES(?,?,?,?)
                                       ON CONFLICT(report_id,row_no,stage_no)
                                       DO UPDATE SET grenades=excluded.grenades""",
                                    (rid, row_no, stage_no, val))
                # A partial set of stages cannot define a complete daily total.
                aggregate = one(con, """SELECT SUM(grenades) t,COUNT(grenades) n
                                    FROM kv_daily_grenade_stages
                                    WHERE report_id=? AND row_no=?""", (rid, row_no))
                total = aggregate["t"] if aggregate["n"] == report["stage_count"] else None
                con.execute("""UPDATE kv_daily_players SET total_grenades=?
                               WHERE report_id=? AND row_no=?""", (total, rid, row_no))
        con.execute("COMMIT")
    return redirect(url_for("day", date=date, saved=1, mode=mode))


def main() -> None:
    global DB_PATH
    parser = argparse.ArgumentParser(description="админ-панель scan_stats.sqlite3")
    parser.add_argument("--db", default=str(DB_PATH), help="путь к базе (для тестов)")
    parser.add_argument("--port", type=int, default=8787)
    args = parser.parse_args()
    DB_PATH = Path(args.db)
    player_store.ensure_schema(DB_PATH)
    print(f"✅ Админ-панель: http://127.0.0.1:{args.port}  (база: {DB_PATH})")
    app.run(host="127.0.0.1", port=args.port, debug=False)


if __name__ == "__main__":
    main()
