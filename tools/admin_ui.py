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
    return f"{s // 60}:{s % 60:02d}"


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
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--text);font:13px/1.35 -apple-system,BlinkMacSystemFont,"Segoe UI",Arial,sans-serif}}a{{color:inherit;text-decoration:none}}a:hover{{color:var(--blue)}}
.top{{position:sticky;top:0;z-index:20;background:var(--bg);border-bottom:1px solid var(--line)}}.topin{{width:min(980px,calc(100% - 32px));margin:auto}}.wrap{{width:min(940px,calc(100% - 32px));margin:auto}}.layout-overview{{max-width:940px}}.layout-players{{max-width:820px}}.layout-day{{max-width:820px}}.topin{{height:50px;display:flex;align-items:center;justify-content:space-between}}.brand{{font-weight:750;display:flex;align-items:center;gap:9px}}.logo{{width:26px;height:26px;border-radius:8px;background:var(--text);color:var(--bg);display:grid;place-items:center;font-size:12px}}.nav{{display:flex;gap:4px;padding:4px;background:var(--panel);border:1px solid var(--line);border-radius:10px}}.nav a{{min-height:30px;padding:0 10px;display:flex;align-items:center;border-radius:7px;color:var(--dim);font-weight:650}}.nav a.active{{background:var(--blue2);color:var(--blue)}}
.wrap{{padding:20px 0 48px}}h1{{font-size:24px;line-height:1.15;letter-spacing:-.02em;margin:0 0 4px}}h2{{font-size:15px;margin:18px 0 8px}}.pagehead{{display:flex;justify-content:space-between;align-items:end;gap:12px;margin-bottom:14px}}.subtitle,.hint,.dim,.chips{{color:var(--dim)}}
.metrics{{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin:10px 0 16px}}.metric,.panel{{background:var(--panel);border:1px solid var(--line);border-radius:10px}}.panel{{overflow:auto}}.metric{{padding:10px 12px}}.metric label{{display:block;color:var(--dim);font-size:11px;font-weight:650}}.metric b{{display:block;font-size:20px;letter-spacing:-.03em;margin-top:2px}}.panel{{padding:8px;margin-bottom:10px}}
.actions,.chips,nav.days{{display:flex;gap:8px;flex-wrap:wrap;align-items:center}}.button,button{{min-height:32px;padding:0 10px;border:1px solid var(--line);border-radius:8px;background:var(--panel);color:var(--text);font:650 12px inherit;cursor:pointer;display:inline-flex;align-items:center;justify-content:center}}.button:hover,button:hover{{background:var(--soft)}}button.save,.button.primary{{background:var(--blue);border-color:var(--blue);color:#fff}}button.danger{{min-height:31px;padding:0 9px;color:var(--red);background:transparent}}button:disabled{{opacity:.4}}
.scroll{{overflow-x:auto;overflow-y:visible;max-height:none;border:1px solid var(--line);border-radius:8px}}table{{width:100%;border-collapse:collapse}}th,td{{padding:6px 8px;text-align:center;white-space:nowrap;border-bottom:1px solid var(--line);font-variant-numeric:tabular-nums}}th{{position:sticky;top:0;background:var(--soft);color:var(--dim);font-size:10px;text-transform:uppercase;letter-spacing:.035em;z-index:2}}tr:last-child td{{border-bottom:0}}tbody tr:hover{{background:var(--blue2)}}td.nick,th.nick{{text-align:left;max-width:240px;overflow:hidden;text-overflow:ellipsis}}th.gcol,td.gcol{{background:transparent}}.total{{font-weight:750}}input[type=number],input#flt,select{{min-height:30px;border:1px solid var(--line);border-radius:7px;background:var(--panel);color:var(--text);padding:0 9px}}input[type=number]{{width:58px;text-align:center}}.day-edit td{{height:31px;padding:2px 8px}}.day-edit input[type=number],.day-edit input[type=text]{{width:48px;height:21px;min-height:21px;padding:0 3px;border:1px solid transparent;border-radius:4px;background:transparent;color:var(--text);font:inherit;text-align:center}}.day-edit input[name$="_s"]{{width:64px}}.day-edit button.danger{{width:25px;min-height:21px;height:21px;padding:0;border-radius:5px}}.day-edit input[type=number]:hover,.day-edit input[type=number]:focus,.day-edit input[type=text]:hover,.day-edit input[type=text]:focus{{background:var(--panel);border-color:var(--line);outline:none}}.ok-match{{color:var(--green);font-weight:650}}.source-badge{{display:inline-block;margin-left:5px;padding:1px 4px;border:1px solid var(--line);border-radius:4px;color:var(--dim);font-size:9px;vertical-align:1px}}input#flt{{width:min(100%,300px)}}
.chips{{margin:6px 0 10px}}.chip{{padding:3px 7px;border:1px solid var(--line);border-radius:7px;background:var(--panel)}}.seg{{display:inline-flex;gap:2px;padding:2px;border:1px solid var(--line);border-radius:9px;background:var(--soft);margin:2px 0 8px}}.seg a,.seg b{{min-height:28px;padding:0 9px;display:flex;align-items:center;border-radius:6px}}.seg b{{background:var(--panel)}}.float{{position:fixed;right:20px;bottom:20px;z-index:5;box-shadow:0 8px 24px #0003}}.back{{display:inline-flex;color:var(--dim);margin-bottom:8px}}.banner{{display:inline-flex;padding:8px 12px;border-radius:8px;background:#e8f1ec;color:var(--green);font-weight:700}}.flag{{color:var(--red)}}details summary{{cursor:pointer;color:var(--blue);padding:6px 0}}nav.days{{margin:7px 0}}nav.days select{{max-width:210px}}.month{{color:var(--dim);font-size:10px;font-weight:750;text-transform:uppercase;letter-spacing:.06em;background:var(--bg);text-align:left;padding:7px 8px 4px!important}}.squad-title td{{padding:8px 6px 4px!important;background:var(--bg);color:var(--dim);font-size:10px;font-weight:850;text-align:left;text-transform:uppercase;letter-spacing:.06em}}.squad-title span{{margin-left:6px;font-weight:500;text-transform:none;letter-spacing:0}}.sortable{{cursor:pointer;user-select:none}}.sortable::after{{content:" ↕";color:var(--dim);font-size:9px}}.sortable[data-dir="desc"]::after{{content:" ↓";color:var(--blue)}}.sortable[data-dir="asc"]::after{{content:" ↑";color:var(--blue)}}.eff{{font-weight:800;color:var(--blue)}}
@media(max-width:760px){{.topin,.wrap{{width:calc(100% - 24px);max-width:none}}.topin{{height:48px}}.brand .txt{{display:none}}.wrap{{padding-top:16px}}.metrics{{grid-template-columns:repeat(2,1fr)}}h1{{font-size:22px}}.pagehead{{align-items:start;flex-direction:column}}th,td{{padding:9px 8px}}}}
</style></head><body><header class="top"><div class="topin"><a class="brand" href="{home}"><span class="logo">CA</span><span class="txt">Clan Analytics</span></a><nav class="nav"><a class="{ov}" href="{home}">Обзор</a><a class="{pl}" href="{players}">Игроки</a></nav></div></header><main class="wrap {layout}">{body}</main></body></html>"""


def render(title: str, body: str) -> str:
    path=request.path
    layout = "layout-day" if path.startswith("/day/") else ("layout-players" if path.startswith("/player") else "layout-overview")
    return PAGE.format(title=html.escape(title),body=body,home=url_for("index"),players=url_for("players"),layout=layout,ov="active" if path=="/" or path.startswith("/day/") else "",pl="active" if path.startswith("/player") else "")


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
            out.append(f"<tr><td class='month' colspan='5'>{esc(m)}</td></tr>")
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
            "</div>"
            + (f"<a class='button primary' href='{url_for('day',date=latest)}'>Последний КВ</a>" if latest else "")
            + "</div>" + summary
            + "<div class='panel'><table><thead><tr><th>Дата</th><th>Табов</th>"
            "<th>Игроков</th><th>Гранаты</th><th>Войс</th></tr></thead>"
            f"<tbody>{''.join(out)}</tbody></table></div>"
            )
    return render("Дни", body)


def average_player_efficiencies(con: Any) -> dict[int, float]:
    """Average the existing relative day EFF over completed KV days."""
    rows = q(con, """
        WITH tabs AS (
            SELECT s.match_date date, sp.player_id,
                   COUNT(*) tabs,
                   SUM(sp.kills) kills,
                   SUM(sp.deaths) deaths,
                   SUM(sp.assists) assists,
                   COALESCE(SUM(sp.score), 0) score
            FROM scans s
            JOIN scan_players sp ON sp.scan_id=s.id
            WHERE s.match_date IS NOT NULL AND sp.player_id IS NOT NULL
            GROUP BY s.match_date, sp.player_id
        ), grenades AS (
            SELECT r.match_date date, k.player_id,
                   MAX(k.total_grenades) grenades
            FROM kv_daily_reports r
            JOIN kv_daily_players k ON k.report_id=r.id
            WHERE k.player_id IS NOT NULL AND k.total_grenades IS NOT NULL
            GROUP BY r.match_date, k.player_id
        )
        SELECT t.date, t.player_id, t.tabs, t.kills, t.deaths,
               t.assists, t.score, g.grenades
        FROM tabs t
        JOIN grenades g ON g.date=t.date AND g.player_id=t.player_id
        ORDER BY t.date, t.player_id
    """)
    weights = {"kills_per_tab": 0.25, "kda": 0.25,
               "score_per_tab": 0.20, "grenades": 0.20}
    by_day: dict[str, dict[int, dict[str, float]]] = {}
    for row in rows:
        tabs = max(int(row["tabs"]), 1)
        deaths = int(row["deaths"] or 0)
        kills = int(row["kills"] or 0)
        assists = int(row["assists"] or 0)
        by_day.setdefault(str(row["date"]), {})[int(row["player_id"])] = {
            "kills_per_tab": kills / tabs,
            "kda": ((kills + assists) / deaths) if deaths else float(kills + assists),
            "score_per_tab": float(row["score"] or 0) / tabs,
            "grenades": float(row["grenades"]),
        }
    samples: dict[int, list[float]] = {}
    for values_by_player in by_day.values():
        bounds = {
            key: (min(values[key] for values in values_by_player.values()),
                  max(values[key] for values in values_by_player.values()))
            for key in weights
        }
        for player_id, values in values_by_player.items():
            normalized = {}
            for key, value in values.items():
                minimum, maximum = bounds[key]
                normalized[key] = (50.0 if maximum == minimum
                                   else (value - minimum) / (maximum - minimum) * 100.0)
            score = sum(normalized[key] * weight for key, weight in weights.items()) / sum(weights.values())
            samples.setdefault(player_id, []).append(score)
    return {player_id: round(sum(values) / len(values), 1)
            for player_id, values in samples.items()}


@app.get("/players")
def players():
    with closing(player_store.connect(DB_PATH)) as con:
        rows = q(con, """
            SELECT p.id, p.canonical_nick, rm.squad_id,
                   COUNT(DISTINCT r.match_date) days,
                   SUM(k.total_grenades) grenades,
                   MAX(r.match_date) last_day
            FROM players p
            JOIN (
                SELECT player_id, MIN(squad_id) squad_id
                FROM roster_memberships
                WHERE active=1 AND (squad_id BETWEEN 1 AND 6 OR squad_id=99)
                GROUP BY player_id
            ) rm ON rm.player_id=p.id
            LEFT JOIN kv_daily_players k ON k.player_id=p.id
            LEFT JOIN kv_daily_reports r ON r.id=k.report_id
            GROUP BY p.id, rm.squad_id
            ORDER BY CASE WHEN rm.squad_id=99 THEN 7 ELSE rm.squad_id END,
                     p.canonical_nick COLLATE NOCASE""")
        eff_by_player = average_player_efficiencies(con)
    groups: dict[int, list[dict[str, Any]]] = {99: [], **{i: [] for i in range(1, 7)}}
    for row in rows:
        groups[int(row["squad_id"])].append(row)
    bodies = []
    for squad_id in [1, 2, 3, 4, 5, 6, 99]:
        members = groups[squad_id]
        if not members:
            continue
        label = "Чемпионы" if squad_id == 99 else f"Отряд {squad_id}"
        member_rows = "".join(
            f"<tr class='player-row'><td class='nick'><a href='{url_for('player', player_id=r['id'])}'>{esc(r['canonical_nick'])}</a></td>"
            f"<td>{r['days'] or 0}</td><td class='total'>{r['grenades'] or 0}</td>"
            f"<td class='eff'>{esc(eff_by_player.get(int(r['id']), '—'))}</td>"
            "</tr>"
            for r in members)
        bodies.append(
            f"<tbody class='squad-group'><tr class='squad-title'><td colspan='4'>{label}</td></tr>"
            f"{member_rows}</tbody>")
    body = ("<div class='pagehead'><div><h1>Игроки</h1>"
            "</div></div>"
            "<input id='flt' placeholder='Поиск по нику…' aria-label='Поиск игрока' oninput=\"filterPlayers(this.value)\">"
            "<div class='panel'><table><thead><tr><th class='nick'>Ник</th><th>Дни</th><th>Грены</th><th class='sortable' title='Среднее текущего EFF по дням КВ' onclick='sortPlayerSquads(this)'>EFF</th></tr></thead>"
            f"{''.join(bodies)}</table></div>"
            "<script>function filterPlayers(value){const q=value.toLowerCase();document.querySelectorAll('.squad-group').forEach(group=>{let n=0;group.querySelectorAll('.player-row').forEach(row=>{const show=row.textContent.toLowerCase().includes(q);row.style.display=show?'':'none';if(show)n++});group.style.display=n?'':'none'})}function sortPlayerSquads(th){const dir=th.dataset.dir==='desc'?'asc':'desc';th.dataset.dir=dir;document.querySelectorAll('.squad-group').forEach(group=>{const rows=[...group.querySelectorAll('.player-row')];const value=row=>{const raw=row.cells[3].textContent.trim().replace(',','.');if(!raw||raw==='—')return null;const parsed=Number(raw);return Number.isFinite(parsed)?parsed:null};rows.sort((a,b)=>{const av=value(a),bv=value(b);if(av===null||bv===null)return av===bv?0:av===null?1:-1;return dir==='asc'?av-bv:bv-av});rows.forEach(row=>group.appendChild(row))})}</script>")
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
    stage_cols = "".join(f"<th class='gcol sortable' data-sort='number'>{stage_label(i)}</th>"
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
    # A player may exist in a screenshot but be absent from the grenade/voice report
    # (substitution, stale roster, or partial import). Keep those combat stats visible.
    report_player_ids = {k["player_id"] for k in krows if k["player_id"] is not None}
    scan_names: dict[int, str] = {}
    for scan in scans:
        for row in scan["players"]:
            if row["player_id"] is not None:
                scan_names.setdefault(int(row["player_id"]), row["canonical_nick"] or row["nick"])
    for player_id in sorted(set(tab_sum) - report_player_ids,
                            key=lambda pid: scan_names.get(pid, "").casefold()):
        nick = scan_names.get(player_id, f"Игрок {player_id}")
        krows.append({
            "row_no": None, "player_id": player_id, "raw_nick": nick,
            "canonical_nick": nick, "total_grenades": None,
            "voice_seconds": None, "attended": None, "stages": {},
            "scan_only": True,
        })
    # EFF is calculated independently inside this KV day.
    # The four requested weights sum to 0.90, so divide by 0.90 to keep EFF on a 0–100 scale.
    eff_weights = {"kills_per_tab": 0.25, "kda": 0.25, "score_per_tab": 0.20, "grenades": 0.20}
    eff_values: dict[int, dict[str, float]] = {}
    for k in krows:
        player_id = k.get("player_id")
        t = tab_sum.get(player_id) if player_id is not None else None
        if not t or k.get("total_grenades") is None:
            continue
        tabs = max(int(t["n"]), 1)
        deaths = int(t["d"])
        eff_values[int(player_id)] = {
            "kills_per_tab": float(t["k"]) / tabs,
            "kda": (float(t["k"] + t["a"]) / deaths) if deaths else float(t["k"] + t["a"]),
            "score_per_tab": float(t["s"]) / tabs,
            "grenades": float(k["total_grenades"]),
        }
    eff_bounds = {
        key: (min(values[key] for values in eff_values.values()),
              max(values[key] for values in eff_values.values()))
        for key in eff_weights
    } if eff_values else {}
    day_eff: dict[int, float] = {}
    for player_id, values in eff_values.items():
        normalized = {}
        for key, value in values.items():
            minimum, maximum = eff_bounds[key]
            normalized[key] = 50.0 if maximum == minimum else (value - minimum) / (maximum - minimum) * 100.0
        day_eff[player_id] = round(
            sum(normalized[key] * weight for key, weight in eff_weights.items())
            / sum(eff_weights.values()), 1)

    has_tabs = bool(scans)
    max_stage = max((max(k["stages"], default=0) for k in krows), default=0)
    max_stage = max(max_stage, report["stage_count"] if report else 0)
    stage_cols = ("".join(f"<th class='gcol'>{stage_label(i)}</th>"
                          for i in range(1, max_stage + 1))
                  if mode == "stages" else "")
    save_url = url_for("day_save", date=date)

    # ── сводная строка игрока: итог табов + гранаты + войс ────────────────
    rows_html = []
    for display_no, k in enumerate(krows, 1):
        nick = k["canonical_nick"] or k["raw_nick"] or "—"
        scan_only = bool(k.get("scan_only"))
        tab_cells = ""
        if has_tabs:
            t = tab_sum.get(k["player_id"]) if k["player_id"] else None
            if t:
                tip = esc(" | ".join(t["rows"]))
                kda = ((t["k"] + t["a"]) / t["d"]
                       if t["d"] else float(t["k"] + t["a"]))
                efficiency = day_eff.get(int(k["player_id"]))
                eff_cell = (f"{efficiency:.1f}" if efficiency is not None else "—")
                tab_cells = (f"<td class='sum' title='{tip}'>{t['k']}</td>"
                             f"<td class='sum' title='{tip}'>{t['d']}</td>"
                             f"<td class='sum' title='{tip}'>{t['a']}</td>"
                             f"<td class='sum' title='{tip}'>{t['s']}</td>"
                             f"<td class='sum'>{kda:.2f}</td><td class='eff'>{eff_cell}</td>")
            else:
                tab_cells = ("<td class='dim'>—</td><td class='dim'>—</td>"
                             "<td class='dim'>—</td><td class='dim'>—</td>"
                             "<td class='dim'>—</td><td class='dim'>—</td>")

        if scan_only:
            gcells = "".join("<td class='dim'>—</td>" for _ in range(max_stage)) if mode == "stages" else ""
            total_cell = "<td class='dim'>—</td>"
            voice_cell = "<td class='dim'>—</td>"
            action_cell = "<td></td>"
            source_badge = " <span class='source-badge' title='Есть в табе, но нет в отчёте гранат/войса'>только таб</span>"
        else:
            row_no = int(k["row_no"])
            if mode == "stages":
                gcells = ""
                for i in range(1, max_stage + 1):
                    val = k["stages"].get(i)
                    shown = "" if val is None else val
                    gcells += (f"<td class='gcol'><input type='number' min='0' "
                               f"name='g{row_no}_{i}' value='{shown}' placeholder='·'></td>")
                total_cell = f"<td class='total'>{k['total_grenades'] or 0}</td>"
            else:
                gcells = ""
                total_cell = (f"<td class='gcol'><input type='number' min='0' "
                              f"name='w{row_no}' value='{k['total_grenades'] or 0}' "
                              f"title='гранат за день (всего)'></td>")
            voice_cell = (f"<td><input type='text' inputmode='numeric' name='v{row_no}' "
                          f"value='{fmt_sec(k['voice_seconds'] or 0)}' title='минуты:секунды'></td>")
            action_cell = (f"<td><button class='danger' name='__del_row' value='{row_no}' "
                           f"formaction='{save_url}' onclick=\"return confirm('Удалить гранаты и войс "
                           f"{esc(nick)} за {fmt_date_ru(date)}?')\">✕</button></td>")
            source_badge = ""

        link = (f"<a href='{url_for('player', player_id=k['player_id'])}'>{esc(nick)}</a>"
                if k["player_id"] else esc(nick))
        rows_html.append(
            f"<tr><td class='dim rank'>{display_no}</td><td class='nick'>{link}{source_badge}</td>"
            f"{tab_cells}{gcells}{total_cell}{voice_cell}{action_cell}</tr>")

    # ── исходные табы: правка У/С/П/СЧЁТ по каждому табу ──────────────────
    scan_tables = []
    scan_player_count = sum(len(s["players"]) for s in scans)
    unmatched_count = sum(1 for s in scans for r in s["players"] if r["player_id"] is None)
    for s in scans:
        head = (
            "<tr><td colspan='7' style='text-align:left'>"
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
            + ("<td class='ok-match'>найден</td>" if r["player_id"] is not None
               else "<td class='flag'>не найден</td>")
            + f"<td><input type='number' min='0' name='t{r['row_id']}_k' value='{r['kills']}'></td>"
            f"<td><input type='number' min='0' name='t{r['row_id']}_d' value='{r['deaths']}'></td>"
            f"<td><input type='number' min='0' name='t{r['row_id']}_a' value='{r['assists']}'></td>"
            f"<td><input type='number' min='0' name='t{r['row_id']}_s' value='{r['score']}'></td>"
            "</tr>"
            for r in s["players"])
        scan_tables.append(head + rows)
    tabs_details = (
        "<details" + (" open" if len(scans) == 1 else "") + ">"
        f"<summary>Исходные табы ({len(scans)}) · найдено {scan_player_count - unmatched_count}/{scan_player_count}"
        + (f" · не найдено {unmatched_count}" if unmatched_count else " · все ники найдены")
        + "</summary>"
        "<div class='scroll'><table><thead><tr><th>#</th><th>Ник</th><th>Совпадение</th>"
        "<th>У</th><th>С</th><th>П</th><th>Счёт</th></tr></thead>"
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
             f"<span class='chip'>Игроков: {len(krows)}</span>"
             f"<span class='chip'>Гранат: {sum(k['total_grenades'] or 0 for k in krows)}"
             "</span>"
             "<span class='chip'>Войс: "
             + fmt_sec(sum(k["voice_seconds"] or 0 for k in krows)) + "</span></div>")

    tab_head = ("<th class='sortable' data-sort='number'>У</th>"
                "<th class='sortable' data-sort='number'>С</th>"
                "<th class='sortable' data-sort='number'>П</th>"
                "<th class='sortable' data-sort='number'>Счёт</th>"
                "<th class='sortable' data-sort='number'>KDA</th>"
                "<th class='sortable' data-sort='number' title='EFF дня: У/таб 25%, KDA 25%, Счёт/таб 20%, Гранаты/КВ 20%'>EFF</th>"
                if has_tabs else "")
    toggle = ("<div class='seg'>" + ("<b>Всего</b>" if mode == "total" else f"<a href='{url_for('day',date=date,mode='total')}'>Всего</a>") + (f"<a href='{url_for('day',date=date,mode='stages')}'>По этапам</a>" if mode == "total" else "<b>По этапам</b>") + "</div>")

    sort_script = """<script>
function sortDayTable(th){
  const table=document.getElementById('day-table'), body=table.tBodies[0], col=th.cellIndex;
  const type=th.dataset.sort, old=th.dataset.dir;
  const dir=old==='desc'?'asc':old==='asc'?'desc':type==='text'?'asc':'desc';
  table.querySelectorAll('th.sortable').forEach(x=>delete x.dataset.dir); th.dataset.dir=dir;
  const value=row=>{const cell=row.cells[col], input=cell.querySelector('input'); let raw=(input?input.value:cell.textContent).trim();
    if(!raw||raw==='—')return null; if(type==='text')return raw.toLocaleLowerCase('ru');
    if(type==='time'){const parts=raw.split(':').map(Number); return parts.reduce((v,n)=>v*60+n,0)}
    const n=Number(raw.replace(',','.')); return Number.isFinite(n)?n:null};
  const rows=[...body.rows];
  rows.sort((a,b)=>{const av=value(a),bv=value(b); if(av===null||bv===null)return av===bv?0:av===null?1:-1;
    const cmp=type==='text'?av.localeCompare(bv,'ru'):(av-bv); return dir==='asc'?cmp:-cmp});
  rows.forEach((row,i)=>{body.appendChild(row); row.querySelector('.rank').textContent=i+1});
}
document.addEventListener('click',e=>{const th=e.target.closest('#day-table th.sortable');if(th)sortDayTable(th)});
</script>"""

    kv_block = (
        toggle
        + "<div class='scroll'><table id='day-table'><thead><tr>"
        "<th class='sortable' data-sort='number'>#</th>"
        "<th class='sortable' data-sort='text'>Ник</th>"
        + tab_head + stage_cols
        + "<th class='sortable' data-sort='number'>Гранаты</th>"
        "<th class='sortable' data-sort='time'>Войс</th><th></th></tr></thead>"
        + f"<tbody>{''.join(rows_html)}</tbody></table></div>" + sort_script
        if krows else
        "<p class='hint'>Дневной отчёт за эту дату не найден "
        "(гранаты за этот день не импортировались).</p>")

    body = (f"<a class='back' href='{url_for('index')}'>← Обзор</a>"
            f"<div class='pagehead'><div><h1>{fmt_date_ru(date)}</h1></div></div>{nav}{chips}{banner}"
            f"<form class='day-edit' method='post' action='{save_url}'>"
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


def _to_seconds(value: str | None) -> int | None:
    text = (value or "").strip()
    if not text:
        return None
    if ":" not in text:
        return _to_int(text)
    try:
        parts = [int(part) for part in text.split(":")]
    except ValueError:
        return None
    if len(parts) == 2:
        minutes, seconds = parts
        if minutes < 0 or not 0 <= seconds < 60:
            return None
        return minutes * 60 + seconds
    if len(parts) == 3:
        hours, minutes, seconds = parts
        if hours < 0 or not 0 <= minutes < 60 or not 0 <= seconds < 60:
            return None
        return hours * 3600 + minutes * 60 + seconds
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
                    num = _to_seconds(value)
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
