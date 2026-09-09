#!/usr/bin/env python3
"""Локальная админ-панель для scan_stats.sqlite3.

Запуск:  python tools/admin_ui.py   →  http://127.0.0.1:8787
Панель открывает базу рядом с ботом и позволяет:
  * смотреть все дни КВ (табы, гранаты, войс);
  * править цифры прямо в таблице (У/С/П/СЧЁТ, гранаты по этапам, войс);
  * удалять отдельный таб, строку игрока за день, гранаты за день.
Правки сразу видны в /stats и сообщениях бота (исторические данные он
читает из SQLite напрямую, перезапускать бота не нужно).
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
    return f"{s // 60:02d}:{s % 60:02d}"


def fmt_date_ru(day: str) -> str:
    try:
        d = datetime.strptime(day, "%Y-%m-%d")
        return d.strftime("%d.%m.%y (%a)").replace("Mon", "пн").replace("Tue", "вт") \
            .replace("Wed", "ср").replace("Thu", "чт").replace("Fri", "пт") \
            .replace("Sat", "сб").replace("Sun", "вс")
    except ValueError:
        return day


def esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""))


STAGE_ROMAN = {1: "I", 2: "II", 3: "III", 4: "IV", 5: "V", 6: "VI"}


def stage_label(n: Any) -> str:
    try:
        return STAGE_ROMAN.get(int(n), str(n))
    except (TypeError, ValueError):
        return "?"


PAGE = """<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} · админ-панель</title>
<style>
:root {{ --bg:#16171d; --panel:#1e2028; --line:#2c2f3a; --text:#dcddde;
        --dim:#8e9297; --acc:#5865f2; --ok:#3ba55d; --warn:#ed4245; }}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--text);
        font:14px/1.45 "Segoe UI",system-ui,sans-serif; padding:18px; }}
a {{ color:#8ea1ff; text-decoration:none; }} a:hover {{ text-decoration:underline; }}
h1 {{ font-size:20px; margin:0 0 14px; }} h2 {{ font-size:16px; margin:22px 0 8px; }}
.panel {{ background:var(--panel); border:1px solid var(--line); border-radius:10px;
          padding:14px; margin-bottom:16px; }}
table {{ border-collapse:collapse; width:100%; margin:6px 0; }}
th,td {{ border:1px solid var(--line); padding:4px 8px; text-align:center;
         white-space:nowrap; }}
th {{ background:#232634; color:var(--dim); font-weight:600; }}
td.nick {{ text-align:left; max-width:220px; overflow:hidden; text-overflow:ellipsis; }}
input[type=number] {{ width:76px; background:#111218; color:var(--text);
  border:1px solid var(--line); border-radius:6px; padding:3px 6px; text-align:center; }}
input.num {{ width:64px; }}
button {{ background:var(--acc); color:#fff; border:0; border-radius:6px;
          padding:7px 16px; font-size:14px; cursor:pointer; }}
button.danger {{ background:transparent; color:var(--warn); border:1px solid var(--warn);
                 padding:2px 9px; font-size:12px; }}
button.save {{ background:var(--ok); font-weight:600; margin-top:8px; }}
.dim {{ color:var(--dim); }} .big {{ font-size:16px; }}
.back {{ margin-bottom:10px; display:inline-block; }}
.total {{ font-weight:700; color:#ffd166; }}
.hint {{ color:var(--dim); font-size:12.5px; margin:4px 0 10px; }}
.flag {{ color:var(--warn); font-weight:700; }}
.ok-pill {{ color:var(--ok); }}
</style></head><body>{body}</body></html>"""


def render(title: str, body: str) -> str:
    return PAGE.format(title=html.escape(title), body=body)


@app.get("/")
def index():
    with closing(player_store.connect(DB_PATH)) as con:
        rows = q(con, """
            SELECT d.date,
                   COALESCE(t.tabs,0) tabs, COALESCE(g.players,0) gplayers,
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
    trs = []
    for r in rows:
        trs.append(
            f"<tr><td class='nick'><a href='{url_for('day', date=r['date'])}'>"
            f"{fmt_date_ru(r['date'])}</a></td>"
            f"<td>{r['tabs'] or 0}</td><td>{r['gplayers'] or 0}</td>"
            f"<td class='total'>{r['grenades'] or 0}</td><td>{fmt_sec(r['voice'])}</td></tr>"
        )
    body = ("<h1>📅 Дни КВ</h1>"
            "<p class='hint'>Кликни день — откроется таб/гранаты/войс, всё можно править.</p>"
            "<div class='panel'><table><tr><th>Дата</th><th>Табов</th>"
            "<th>Игроков с гранатами</th><th>Гранат всего</th><th>Войс</th></tr>"
            + "".join(trs) + "</table></div>"
            "<div class='panel'><a class='big' href='" + url_for("players")
            + "'>👥 Все игроки и их история (найти накрутку)</a></div>")
    return render("Дни", body)


@app.get("/players")
def players():
    with closing(player_store.connect(DB_PATH)) as con:
        rows = q(con, """
            SELECT p.id, p.canonical_nick,
                   COUNT(DISTINCT r.match_date) days,
                   SUM(k.total_grenades) grenades
            FROM players p
            LEFT JOIN kv_daily_players k ON k.player_id=p.id
            LEFT JOIN kv_daily_reports r ON r.id=k.report_id
            GROUP BY p.id ORDER BY p.canonical_nick COLLATE NOCASE""")
    trs = "".join(
        f"<tr><td class='nick'><a href='{url_for('player', player_id=r['id'])}'>"
        f"{esc(r['canonical_nick'])}</a></td>"
        f"<td>{r['days'] or 0}</td><td class='total'>{r['grenades'] or 0}</td></tr>"
        for r in rows)
    body = ("<a class='back' href='/'>← Дни</a><h1>👥 Игроки</h1>"
            "<div class='panel'><table><tr><th>Ник</th><th>Дней с гранатами</th>"
            f"<th>Гранат всего</th></tr>{trs}</table></div>")
    return render("Игроки", body)


@app.get("/player/<int:player_id>")
def player(player_id: int):
    with closing(player_store.connect(DB_PATH)) as con:
        p = one(con, "SELECT canonical_nick FROM players WHERE id=?", (player_id,))
        if not p:
            return render("Нет игрока", "<p>Игрок не найден.</p>"), 404
        tabs = q(con, """
            SELECT s.match_date date, sp.kills k, sp.deaths d, sp.assists a, sp.score sc,
                   s.stage, s.id scan_id
            FROM scan_players sp JOIN scans s ON s.id=sp.scan_id
            WHERE sp.player_id=? ORDER BY s.match_date DESC, s.stage, s.id""", (player_id,))
        days = q(con, """
            SELECT r.match_date date, k.total_grenades total, k.voice_seconds voice,
                   GROUP_CONCAT(g.stage_no||':'||g.grenades) stages
            FROM kv_daily_players k
            JOIN kv_daily_reports r ON r.id=k.report_id
            LEFT JOIN kv_daily_grenade_stages g ON g.report_id=k.report_id AND g.row_no=k.row_no
            WHERE k.player_id=? GROUP BY r.match_date, k.report_id, k.row_no
            ORDER BY r.match_date DESC""", (player_id,))
        snaps = q(con, """
            SELECT match_date date, GROUP_CONCAT(stat_key||'='||value) snap
            FROM grenade_session_stats WHERE player_id=? AND match_date IS NOT NULL
            GROUP BY match_date ORDER BY match_date DESC""", (player_id,))
    # гранаты по этапам в колонки
    max_stage = 0
    for r in days:
        stages = {}
        for part in (r.get("stages") or "").split(","):
            if ":" in part:
                sn, val = part.split(":", 1)
                stages[int(sn)] = val if val != "" else "·"
        r["stages"] = stages
        max_stage = max([max_stage, *stages.keys()])
    stage_cols = "".join(f"<th>Э{stage_label(i)}</th>" for i in range(1, max_stage + 1))
    max_total = max((r["total"] or 0) for r in days) if days else 0
    trs = []
    for r in days:
        cells = "".join(
            f"<td>{esc(r['stages'].get(i, '—'))}</td>" for i in range(1, max_stage + 1))
        flag = " <span class='flag'>⚠ подозрительно</span>" \
            if r["total"] and r["total"] == max_total and max_total > 150 else ""
        trs.append(
            f"<tr><td class='nick'><a href='{url_for('day', date=r['date'])}'>"
            f"{fmt_date_ru(r['date'])}</a></td>{cells}"
            f"<td class='total'>{r['total']}</td><td>{fmt_sec(r['voice'])}</td>"
            f"<td>{flag}</td></tr>")
    snap_rows = "".join(
        f"<tr><td>{fmt_date_ru(s['date'])}</td><td>{esc(s['snap'])}</td></tr>"
        for s in snaps)
    tab_by_date = {}
    for t in tabs:
        tab_by_date.setdefault(t["date"], []).append(t)
    tab_rows = "".join(
        "<tr><td class='nick'><a href='%s'>%s</a></td><td>%s</td><td>%s/%s/%s</td><td>%s</td></tr>"
        % (url_for("day", date=t["date"]), fmt_date_ru(t["date"]),
           stage_label(t["stage"]), t["k"], t["d"], t["a"], t["sc"])
        for t in tabs)
    body = (f"<a class='back' href='{url_for('players')}'>← Игроки</a>"
            f"<h1>👤 {esc(p['canonical_nick'])}</h1>"
            "<h2>Гранаты и войс по дням</h2>"
            f"<div class='panel'><table><tr><th>Дата</th>{stage_cols}<th>Всего</th>"
            f"<th>Войс</th><th></th></tr>{''.join(trs)}</table></div>"
            "<h2>Табы (У/С/П по этапам)</h2>"
            "<div class='panel'><table><tr><th>Дата</th><th>Этап</th><th>У/С/П</th>"
            f"<th>СЧЁТ</th></tr>{tab_rows}</table></div>")
    if snap_rows:
        body += ("<h2>Снапшоты счётчика (runtime)</h2>"
                 "<div class='panel'><table><tr><th>Дата</th><th>Значения</th></tr>"
                 f"{snap_rows}</table></div>")
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
    report = one(con, """SELECT id, stage_count FROM kv_daily_reports
                         WHERE match_date=? ORDER BY id DESC LIMIT 1""", (date,))
    krows: list[dict[str, Any]] = []
    if report:
        krows = q(con, """SELECT k.row_no, k.player_id, k.raw_nick, k.discord_username,
                          k.squad_label, k.total_grenades, k.voice_seconds,
                          p.canonical_nick,
                          (SELECT GROUP_CONCAT(stage_no) FROM kv_daily_grenade_stages g
                           WHERE g.report_id=k.report_id AND g.row_no=k.row_no) stage_list
                          FROM kv_daily_players k LEFT JOIN players p ON p.id=k.player_id
                          WHERE k.report_id=? ORDER BY k.row_no""", (report["id"],))
        for k in krows:
            stages = {int(s): None for s in (k.pop("stage_list") or "").split(",") if s}
            for r in con.execute("""SELECT stage_no, grenades FROM kv_daily_grenade_stages
                                    WHERE report_id=? AND row_no=?""",
                                  (report["id"], k["row_no"])):
                stages[int(r["stage_no"])] = int(r["grenades"] or 0)
            k["stages"] = stages
    return scans, report, krows


@app.get("/day/<date>")
def day(date: str):
    date = player_store.parse_match_date(date)
    with closing(player_store.connect(DB_PATH)) as con:
        scans, report, krows = _day_data(con, date)
        max_stage = max((max(k["stages"], default=0) for k in krows), default=0)
        max_stage = max(max_stage, report["stage_count"] if report else 0)
    stage_cols = "".join(f"<th>Э{stage_label(i)}</th>" for i in range(1, max_stage + 1))

    scan_html = ""
    for s in scans:
        rows = "".join(
            "<tr>"
            f"<td>{r['place'] or ''}</td>"
            f"<td class='nick'>{esc(r['nick'])}"
            + (f" <span class='dim'>({esc(r['canonical_nick'])})</span>"
               if r["canonical_nick"] and r["canonical_nick"] != r["nick"] else "")
            + "</td>"
            f"<td><input class='num' type='number' min='0' name='t{r['row_id']}_k' value='{r['kills']}'></td>"
            f"<td><input class='num' type='number' min='0' name='t{r['row_id']}_d' value='{r['deaths']}'></td>"
            f"<td><input class='num' type='number' min='0' name='t{r['row_id']}_a' value='{r['assists']}'></td>"
            f"<td><input class='num' type='number' min='0' name='t{r['row_id']}_s' value='{r['score']}'></td>"
            "</tr>"
            for r in s["players"])
        meta = " ".join(filter(None, [
            f"этап {stage_label(s['stage'])}" if s["stage"] else "этап —",
            esc(s["map"] or ""),
            esc((s["scanned_at"] or "")[:16]),
            f"файл: {esc(s['source_filename'])}" if s.get("source_filename") else "",
        ]))
        scan_html += (
            f"<div class='panel'><form method='post'>"
            f"<input type='hidden' name='__del_scan' value='{s['id']}'>"
            f"<button class='danger' formaction='{url_for('day_save', date=date)}'"
            f" onclick=\"return confirm('Удалить весь таб {esc(meta)}?')\">✕ удалить таб</button>"
            f"<span class='dim' style='margin-left:10px'>{meta}</span>"
            f"<table><tr><th>#</th><th>Ник</th><th>У</th><th>С</th><th>П</th><th>СЧЁТ</th></tr>"
            f"{rows}</table></form></div>")

    kv_rows = ""
    for k in krows:
        nick = k["canonical_nick"] or k["raw_nick"] or "—"
        cells = ""
        for i in range(1, max_stage + 1):
            val = k["stages"].get(i)
            shown = "" if val is None else val
            cells += (f"<td><input class='num' type='number' min='0' "
                      f"name='g{k['row_no']}_{i}' value='{shown}' placeholder='·'></td>")
        link = (f"<a href='{url_for('player', player_id=k['player_id'])}'>{esc(nick)}</a>"
                if k["player_id"] else esc(nick))
        kv_rows += (
            "<tr>"
            f"<td>{k['row_no']}</td><td class='nick'>{link}</td>{cells}"
            f"<td class='total'>{k['total_grenades'] or 0}</td>"
            f"<td><input class='num' type='number' min='0' name='v{k['row_no']}' "
            f"value='{k['voice_seconds'] or 0}'></td>"
            f"<td class='dim'>{fmt_sec(k['voice_seconds'])}</td>"
            f"<td><form method='post' action='{url_for('day_save', date=date)}'>"
            f"<input type='hidden' name='__del_row' value='{k['row_no']}'>"
            f"<button class='danger' onclick=\"return confirm('Удалить гранаты и войс "
            f"{esc(nick)} за {fmt_date_ru(date)}?')\">✕</button></form></td>"
            "</tr>")
    kv_block = (
        f"<h2>💣 Гранаты по этапам и 🎙 войс за {fmt_date_ru(date)}</h2>"
        "<p class='hint'>Пустая ячейка этапа = записи нет; 0 = записан ноль. "
        "«Всего» пересчитается как сумма этапов при сохранении. Войс — в секундах.</p>"
        f"<form method='post' action='{url_for('day_save', date=date)}'>"
        f"<div class='panel'><table><tr><th>#</th><th>Ник</th>{stage_cols}"
        "<th>Всего</th><th>Войс, сек</th><th>Войс</th><th></th></tr>"
        f"{kv_rows}</table>"
        "<button class='save'>💾 Сохранить гранаты и войс</button></div></form>"
        if krows else
        f"<h2>💣 Гранаты и 🎙 войс за {fmt_date_ru(date)}</h2>"
        "<p class='hint'>Дневной отчёт за эту дату в базе не найден "
        "(гранаты за этот день не импортировались).</p>")

    body = (f"<a class='back' href='{url_for('index')}'>← Дни</a>"
            f"<h1>📆 {fmt_date_ru(date)}</h1>"
            f"<h2>📄 Табы ({len(scans)})</h2>"
            + (scan_html or "<p class='hint'>Табов за этот день нет.</p>")
            + kv_block)
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

    with closing(player_store.connect(DB_PATH)) as con:
        con.execute("BEGIN IMMEDIATE")

        # удаление целого таба
        del_scan = _to_int(form.get("__del_scan"))
        if del_scan is not None:
            con.execute("DELETE FROM scan_players WHERE scan_id=?", (del_scan,))
            con.execute("DELETE FROM scans WHERE id=?", (del_scan,))
            con.execute("COMMIT")
            return redirect(url_for("day", date=date))

        report = one(con, """SELECT id FROM kv_daily_reports WHERE match_date=?
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
            return redirect(url_for("day", date=date))

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
                    if num is not None and num >= 0:
                        con.execute("""UPDATE kv_daily_players SET voice_seconds=?
                                       WHERE report_id=? AND row_no=?""",
                                    (num, rid, int(key[1:])))
            stage_updates: dict[int, dict[int, int | None]] = {}
            for key, value in form.items():
                if not key.startswith("g"):
                    continue
                row_str, stage_str = key[1:].split("_", 1)
                if not row_str.isdigit() or not stage_str.isdigit():
                    continue
                stage_updates.setdefault(int(row_str), {})[int(stage_str)] = _to_int(value)
            for row_no, stages in stage_updates.items():
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
                # «Всего» = сумма этапов
                total = one(con, """SELECT COALESCE(SUM(grenades),0) t
                                    FROM kv_daily_grenade_stages
                                    WHERE report_id=? AND row_no=?""", (rid, row_no))["t"]
                con.execute("""UPDATE kv_daily_players SET total_grenades=?
                               WHERE report_id=? AND row_no=?""", (total, rid, row_no))
        con.execute("COMMIT")
    return redirect(url_for("day", date=date))


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
