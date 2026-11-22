#!/usr/bin/env python3
"""Локальная админ-панель для scan_stats.sqlite3.

Запуск:  python tools/admin_ui.py   →  http://127.0.0.1:8787
(также поднимается автоматически вместе с bot.py)

Страницы:
  /               — дни КВ, сгруппированы по месяцам;
  /day/<дата>     — сводная таблица дня: игрок → таб (У/С/П/СЧЁТ за день),
                    гранаты по этапам, войс; правка гранат/войса прямо в строке,
                    исходные табы — в раскрывающемся разделе. Если у дня есть таб, в таблице
                    и итогах только игроки с таба, остальные — в свёрнутом списке «Не в табе»;
  /players, /player/<id> — история игрока по дням КВ (гранаты за день, войс, явка, табы; дни без игрока
                    на табе помечены «Неявка»; только дни из «Обзора», без архива);
  /players/voice  — «Заход войс»: игроки из отрядов × дни КВ → время первого захода в войс.

Правки сразу видны в /stats бота (исторические данные читаются из SQLite).
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import html
import os
import secrets
import sqlite3
import sys
import threading
import time
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import player_store  # noqa: E402
from flask import Flask, Response, abort, redirect, request, url_for  # noqa: E402

app = Flask(__name__)

DB_PATH = ROOT / "scan_stats.sqlite3"

# ---------------------------------------------------------------------------
# Доступ.
#   ADMIN_PASSWORD=...    включает вход по паролю — панель можно открывать из интернета;
#   ADMIN_TRUST_PROXY=1   панель стоит за reverse-proxy с HTTPS (Caddy / Cloudflare / Tailscale):
#                         берём IP клиента из X-Forwarded-For, cookie ставится с флагом Secure;
#   ADMIN_SESSION_DAYS=7  сколько дней живёт вход (по умолчанию 7).
# Без ADMIN_PASSWORD панель отвечает только на localhost (SSH-туннель), всё остальное — 403.
# ---------------------------------------------------------------------------
AUTH_COOKIE = "admin_auth"
LOGIN_MAX_FAILS = 5          # неверных паролей с одного IP ...
LOGIN_WINDOW = 15 * 60       # ... за это число секунд → блокировка до конца окна
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
_FORWARD_HEADERS = ("X-Forwarded-For", "X-Forwarded-Host", "X-Forwarded-Proto", "Forwarded",
                    "X-Real-Ip", "Cf-Connecting-Ip")
_fails: dict[str, list[float]] = {}
_fails_lock = threading.Lock()
_secret_cache: bytes | None = None


def _password() -> str:
    return os.getenv("ADMIN_PASSWORD", "").strip()


def _trust_proxy() -> bool:
    return os.getenv("ADMIN_TRUST_PROXY", "").strip().lower() in {"1", "true", "yes", "on"}


def _session_seconds() -> int:
    try:
        return max(1, int(os.getenv("ADMIN_SESSION_DAYS", "7"))) * 86400
    except ValueError:
        return 7 * 86400


def _client_ip() -> str:
    if _trust_proxy():
        forwarded = request.headers.get("X-Forwarded-For", "")
        if forwarded:
            return forwarded.split(",")[-1].strip() or (request.remote_addr or "?")
    return request.remote_addr or "?"


def _is_https() -> bool:
    if _trust_proxy():
        return request.headers.get("X-Forwarded-Proto", "").split(",")[-1].strip().lower() == "https"
    return request.is_secure


def _is_local_request() -> bool:
    host = (urlsplit("//" + request.host).hostname or "").lower()
    return host in _LOCAL_HOSTS and not any(h in request.headers for h in _FORWARD_HEADERS)


def _signing_key() -> bytes:
    """Случайный секрет хранится в .admin_secret; пароль подмешан, поэтому смена пароля
    сразу разлогинивает всех."""
    global _secret_cache
    if _secret_cache is None:
        path = ROOT / ".admin_secret"
        try:
            _secret_cache = bytes.fromhex(path.read_text().strip())
        except (OSError, ValueError):
            _secret_cache = secrets.token_bytes(32)
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w") as fh:
                    fh.write(_secret_cache.hex())
            except OSError:
                pass  # не смогли сохранить — секрет живёт до перезапуска бота
    return hashlib.sha256(_secret_cache + b"|" + _password().encode("utf-8")).digest()


def _sign(expires: int) -> str:
    mac = hmac.new(_signing_key(), f"v1|{expires}".encode(), hashlib.sha256).hexdigest()
    return f"{expires}.{mac}"


def _cookie_valid(value: str | None) -> bool:
    try:
        expires_s, mac = (value or "").split(".", 1)
        expires = int(expires_s)
    except ValueError:
        return False
    return expires > time.time() and hmac.compare_digest(_sign(expires), f"{expires}.{mac}")


def _password_ok(given: str) -> bool:
    return hmac.compare_digest(hashlib.sha256(given.encode("utf-8")).digest(),
                               hashlib.sha256(_password().encode("utf-8")).digest())


def _retry_after(ip: str) -> int:
    """Секунд до конца блокировки (0 — можно пробовать)."""
    now = time.time()
    with _fails_lock:
        recent = [t for t in _fails.get(ip, []) if now - t < LOGIN_WINDOW]
        if recent:
            _fails[ip] = recent
        else:
            _fails.pop(ip, None)
        if len(recent) >= LOGIN_MAX_FAILS:
            return int(LOGIN_WINDOW - (now - recent[0])) + 1
    return 0


def _safe_next(target: str | None) -> str:
    parts = urlsplit(target or "")
    if parts.scheme or parts.netloc or not (target or "").startswith("/") or (target or "").startswith("//"):
        return "/"
    return target  # type: ignore[return-value]


ASSET_DIR = Path(__file__).resolve().parent / "assets"


def _asset(name: str) -> str:
    return (ASSET_DIR / name).read_text(encoding="utf-8")


BASE_CSS = _asset("app.css")     # единая система оформления (токены, компоненты, страницы)
BASE_JS = _asset("app.js")       # подтверждения, уведомления, панель сохранения, меню

# Линейные иконки (24×24, штрих 1.7). Вместо эмодзи: одинаково выглядят в любой ОС.
ICON_PATHS = {
    "archive": "<rect x='3' y='4' width='18' height='4' rx='1'/><path d='M5 8v10a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8'/><path d='M10 12h4'/>",
    "trash": "<path d='M4 7h16'/><path d='M9 7V4h6v3'/><path d='M6 7l1 13h10l1-13'/><path d='M10 11v5M14 11v5'/>",
    "check": "<path d='M5 12.5l4.5 4.5L19 7.5'/>",
    "x": "<path d='M6 6l12 12M18 6L6 18'/>",
    "refresh": "<path d='M20 11a8 8 0 0 0-14.3-4.5L4 8'/><path d='M4 4v4h4'/><path d='M4 13a8 8 0 0 0 14.3 4.5L20 16'/><path d='M20 20v-4h-4'/>",
    "undo": "<path d='M9 14L4 9l5-5'/><path d='M4 9h10a6 6 0 0 1 0 12h-3'/>",
    "mic": "<rect x='9' y='3' width='6' height='11' rx='3'/><path d='M5 11a7 7 0 0 0 14 0'/><path d='M12 18v3'/>",
    "clock": "<circle cx='12' cy='12' r='9'/><path d='M12 7v5l3 2'/>",
    "left": "<path d='M15 5l-7 7 7 7'/>",
    "right": "<path d='M9 5l7 7-7 7'/>",
    "arrow-left": "<path d='M19 12H5'/><path d='M11 6l-6 6 6 6'/>",
    "more": "<circle cx='5' cy='12' r='1.4' fill='currentColor' stroke='none'/><circle cx='12' cy='12' r='1.4' fill='currentColor' stroke='none'/><circle cx='19' cy='12' r='1.4' fill='currentColor' stroke='none'/>",
    "search": "<circle cx='11' cy='11' r='6.5'/><path d='M16 16l4 4'/>",
    "alert": "<path d='M12 4l9 16H3z'/><path d='M12 10v4'/><path d='M12 17.2v.1'/>",
    "logout": "<path d='M10 4H5v16h5'/><path d='M15 8l4 4-4 4'/><path d='M19 12H9'/>",
}


def icon(name: str, cls: str = "") -> str:
    extra = f" {cls}" if cls else ""
    return f"<svg class='ic{extra}' aria-hidden='true'><use href='#i-{name}'/></svg>"


def _sprite() -> str:
    symbols = "".join(f"<symbol id='i-{n}' viewBox='0 0 24 24'>{d}</symbol>" for n, d in ICON_PATHS.items())
    return f"<svg width='0' height='0' style='position:absolute' aria-hidden='true' focusable='false'>{symbols}</svg>"


LOGIN_PAGE = ('''<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex">
<meta name="color-scheme" content="light dark"><title>Вход · Clan Analytics</title><style>''' + BASE_CSS + '''</style></head>
<body class="login-page"><form class="login" method="post" action="/login">
<span class="logo">CA</span><h1>Clan Analytics</h1><p>Админ-панель клана. Введите пароль.</p>
<input type="hidden" name="next" value="@@NEXT@@">
<input type="password" name="password" placeholder="Пароль" aria-label="Пароль" autocomplete="current-password" autofocus required>
@@ERR@@<button type="submit">Войти</button></form></body></html>''')


def _login_page(next_url: str, error: str = "", status: int = 200) -> Response:
    body = LOGIN_PAGE.replace("@@NEXT@@", html.escape(next_url, quote=True)).replace(
        "@@ERR@@", f'<div class="err" role="alert">{html.escape(error)}</div>' if error else "")
    return Response(body, status=status, mimetype="text/html")


def _cross_site_write() -> bool:
    """Изменяющий запрос пришёл не с самой панели. Браузер всегда присылает Origin (или Referer):
    если он указывает на другой сайт или порт, чужая страница просит панель что-то изменить от
    вашего имени. Запросы без этих заголовков (curl, тесты) не из браузера и под проверку не попадают."""
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return False
    if request.headers.get("Sec-Fetch-Site") == "cross-site":
        return True
    origin = request.headers.get("Origin") or request.headers.get("Referer")
    return bool(origin) and urlsplit(origin).netloc != request.host


@app.before_request
def _guard():
    if _cross_site_write():     # и с паролем, и локально без него (раньше проверка была только с паролем)
        return Response("Forbidden\n", status=403, mimetype="text/plain")
    if not _password():
        if _is_local_request():
            return None
        return Response("Панель доступна только локально. Чтобы открыть её в интернете, "
                        "задайте ADMIN_PASSWORD в .env бота.\n", status=403, mimetype="text/plain")
    if request.endpoint == "login":
        return None
    if _cookie_valid(request.cookies.get(AUTH_COOKIE)):
        return None
    if request.method in ("GET", "HEAD"):
        target = request.full_path.rstrip("?")
        return redirect(url_for("login", next=target), code=303)
    return Response("Нужен вход\n", status=401, mimetype="text/plain")


@app.after_request
def _secure_headers(resp):
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    if _password():
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["X-Robots-Tag"] = "noindex"
        if _is_https():
            resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
    return resp


@app.route("/login", methods=["GET", "POST"])
def login():
    if not _password():
        return redirect("/")
    next_url = _safe_next(request.values.get("next"))
    if request.method == "GET":
        return redirect(next_url) if _cookie_valid(request.cookies.get(AUTH_COOKIE)) else _login_page(next_url)
    ip = _client_ip()
    wait = _retry_after(ip)
    if wait:
        resp = _login_page(next_url, f"Слишком много попыток. Подождите {wait // 60 + 1} мин.", 429)
        resp.headers["Retry-After"] = str(wait)
        return resp
    if not _password_ok(request.form.get("password", "")):
        with _fails_lock:
            _fails.setdefault(ip, []).append(time.time())
        time.sleep(0.7)
        return _login_page(next_url, "Неверный пароль.", 401)
    with _fails_lock:
        _fails.pop(ip, None)
    resp = redirect(next_url, code=303)
    resp.set_cookie(AUTH_COOKIE, _sign(int(time.time()) + _session_seconds()), max_age=_session_seconds(),
                    httponly=True, secure=_is_https(), samesite="Lax", path="/")
    return resp


@app.post("/logout")
def logout():
    resp = redirect(url_for("login") if _password() else "/", code=303)
    resp.delete_cookie(AUTH_COOKIE, path="/")
    return resp


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


def meter(value: Any, maximum: Any) -> str:
    """Тонкая полоска заполнения 0…100 % от maximum (цвета — из палитры панели)."""
    try:
        share = float(value or 0) / float(maximum) if float(maximum) > 0 else 0.0
    except (TypeError, ValueError):
        share = 0.0
    share = max(0.0, min(1.0, share))
    width = 0.0 if share <= 0 else max(share * 100.0, 3.0)
    return f"<span class='meter'><i style='width:{width:.1f}%'></i></span>"


def stage_label(n: Any) -> str:
    try:
        return STAGE_ROMAN.get(int(n), str(n))
    except (TypeError, ValueError):
        return "?"


LOGOUT_FORM = ('<form class="logout" method="post" action="/logout"><button type="submit" '
               f'title="Выйти" aria-label="Выйти">{icon("logout")}</button></form>')

# Меню и стили расширений (Отряды, Бафы, Шмот…) регистрируются здесь, а не подменой готовой страницы.
NAV: list[dict[str, Any]] = []
EXTRA_CSS: list[str] = []


def register_nav(label: str, endpoint: str, *, order: int, exact: tuple[str, ...] = (),
                 prefixes: tuple[str, ...] = ()) -> None:
    """Добавить пункт верхнего меню. order — позиция слева направо; exact/prefixes — когда пункт активен."""
    NAV[:] = [item for item in NAV if item["endpoint"] != endpoint]
    NAV.append({"label": label, "endpoint": endpoint, "order": order, "exact": exact, "prefixes": prefixes})
    NAV.sort(key=lambda item: item["order"])


def add_css(css: str) -> None:
    """Подключить дополнительные стили страницы (один раз)."""
    if css not in EXTRA_CSS:
        EXTRA_CSS.append(css)


register_nav("Обзор", "index", order=10, exact=("/",), prefixes=("/day/", "/archive"))
register_nav("Игроки", "players", order=20, prefixes=("/player",))


def _nav_html(path: str) -> str:
    out = []
    for item in NAV:
        prefixes = tuple(item["prefixes"])
        active = path in item["exact"] or (bool(prefixes) and path.startswith(prefixes))
        attrs = ' class="active" aria-current="page"' if active else ""
        href = url_for(item["endpoint"])
        out.append(f'<a{attrs} href="{href}">{html.escape(item["label"])}</a>')
    return "".join(out)


def render(title: str, body: str, *, wide: bool = False, body_class: str = "") -> str:
    home = url_for("index")
    extra_css = "".join(EXTRA_CSS)
    logout = LOGOUT_FORM if _password() else ""
    body_class = " ".join(part for part in ("wide" if wide else "", body_class) if part)
    return (
        '<!doctype html><html lang="ru"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="color-scheme" content="light dark">'
        f"<title>{html.escape(title)} · Clan Analytics</title><style>{BASE_CSS}{extra_css}</style></head>"
        f'<body class="{body_class}">{_sprite()}'
        f'<header class="top"><div class="topin"><a class="brand" href="{home}">'
        '<span class="logo">CA</span><span class="txt">Clan Analytics</span></a>'
        f'<nav class="nav" aria-label="Разделы">{_nav_html(request.path)}</nav>'
        f"{logout}</div></header>"
        f'<main class="wrap">{body}</main><div id="toasts" aria-live="polite"></div>'
        f"<script>{BASE_JS}</script></body></html>")


_archive_ready: set[str] = set()


def _ensure_archive_table(con: Any) -> None:
    con.execute("CREATE TABLE IF NOT EXISTS archived_days("
                "match_date TEXT NOT NULL PRIMARY KEY, "
                "archived_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")


@app.before_request
def _prepare_archive_table():
    """Таблица архива есть после ensure_schema бота; тут страховка для отдельного запуска панели."""
    key = str(DB_PATH)
    if key not in _archive_ready and Path(DB_PATH).exists():
        with closing(player_store.connect(DB_PATH)) as con:
            _ensure_archive_table(con)
        _archive_ready.add(key)


def all_dates(con: Any) -> list[str]:
    """Даты КВ без архивных."""
    na = player_store.not_archived
    return [r["date"] for r in q(con, f"""
        SELECT match_date date FROM scans WHERE {na('scans.match_date')}
        UNION SELECT match_date FROM kv_daily_reports WHERE {na('kv_daily_reports.match_date')}
        UNION SELECT match_date FROM grenade_session_stats
              WHERE match_date IS NOT NULL AND {na('grenade_session_stats.match_date')}
        ORDER BY date DESC""")]


# ---------------------------------------------------------------- страницы --

@app.get("/")
def index():
    na = player_store.not_archived
    with closing(player_store.connect(DB_PATH)) as con:
        player_store.prepare_tab_filter(con)
        played = player_store.played_filter("r.match_date", "p.player_id")
        # «Игроков» — как на странице дня: есть таб → сколько игроков на табе (в том числе тех, кого нет в отчёте
        # гранат/войса, «только таб»); табов нет → сколько просканировано в отчёте.
        rows = q(con, f"""
            SELECT d.date, COALESCE(t.tabs,0) tabs, COALESCE(tr.n, g.players, 0) players,
                   COALESCE(g.grenades,0) grenades, COALESCE(g.voice,0) voice
            FROM (SELECT match_date date FROM scans WHERE {na('scans.match_date')} UNION
                  SELECT match_date FROM kv_daily_reports WHERE {na('kv_daily_reports.match_date')} UNION
                  SELECT match_date FROM grenade_session_stats
                  WHERE match_date IS NOT NULL AND {na('grenade_session_stats.match_date')}) d
            LEFT JOIN (SELECT match_date, COUNT(*) tabs FROM scans GROUP BY match_date) t
                   ON t.match_date=d.date
            LEFT JOIN (SELECT match_date, COUNT(*) n FROM kv_tab_roster GROUP BY match_date) tr
                   ON tr.match_date=d.date
            LEFT JOIN (SELECT r.match_date, COUNT(DISTINCT p.player_id) players,
                              SUM(p.total_grenades) grenades, SUM(p.voice_seconds) voice
                       FROM kv_daily_reports r JOIN kv_daily_players p ON p.report_id=r.id
                       WHERE {played}
                       GROUP BY r.match_date) g ON g.match_date=d.date
            ORDER BY d.date DESC""")
        archived_n = int(one(con, "SELECT COUNT(*) n FROM archived_days")["n"])
    out, cur_month = [], None
    max_grenades = max((int(r["grenades"] or 0) for r in rows), default=0)
    for r in rows:
        m = month_ru(r["date"])
        if m != cur_month:
            cur_month = m
            out.append(f"<tr><td class='month' colspan='6'>{esc(m)}</td></tr>")
        out.append(
            f"<tr><td class='nick grow'><a href='{url_for('day', date=r['date'])}'>"
            f"{fmt_date_ru(r['date'])}</a></td>"
            f"<td class='hide-sm'>{r['tabs'] or 0}</td><td class='hide-sm'>{r['players'] or 0}</td>"
            f"<td class='total'><span class='mcell'>{meter(r['grenades'], max_grenades)}"
            f"<b>{r['grenades'] or 0}</b></span></td>"
            f"<td>{fmt_sec(r['voice'])}</td>"
            f"<td class='act'><form method='post' action='{url_for('day_archive', date=r['date'])}'>"
            "<button class='quiet' title='Убрать день из списка и итогов (можно вернуть)'>В архив</button>"
            "</form></td></tr>")
    total_tabs = sum(int(r["tabs"] or 0) for r in rows)
    total_grenades = sum(int(r["grenades"] or 0) for r in rows)
    total_voice = sum(int(r["voice"] or 0) for r in rows)
    latest = rows[0]["date"] if rows else None
    n_days = len(rows)
    summary = ("<div class='stats'>"
               f"<span><b>{n_days}</b> {_plural(n_days, 'день', 'дня', 'дней')} КВ</span>"
               f"<span><b>{total_tabs}</b> {_plural(total_tabs, 'таб', 'таба', 'табов')}</span>"
               f"<span><b>{_fmt_num(total_grenades)}</b> {_plural(total_grenades, 'граната', 'гранаты', 'гранат')}</span>"
               f"<span><b>{fmt_sec(total_voice)}</b> в войсе</span></div>" if rows else
               "<div class='stats'><span>Дней КВ пока нет</span></div>")
    banner = ""
    just = request.args.get("archived")
    if just:
        try:
            just = player_store.parse_match_date(just)
            banner = (f"<div class='banner'>{icon('archive')}<span>День " + fmt_date_ru(just) + " убран в архив</span>"
                      f"<form method='post' action='{url_for('day_unarchive', date=just)}'>"
                      f"<button>{icon('undo')} Вернуть</button></form></div>")
        except ValueError:
            pass
    archive_link = (f"<a class='button' href='{url_for('archive')}'>{icon('archive')} Архив ({archived_n})</a>"
                    if archived_n else "")
    body = ("<div class='pagehead'><div><h1>Обзор</h1>" + summary
            + "</div><div class='actions'>" + archive_link
            + (f"<a class='button primary' href='{url_for('day',date=latest)}'>Последний КВ</a>" if latest else "")
            + "</div></div>" + banner
            + "<div class='panel'><table><thead><tr><th class='nick grow'>Дата</th>"
            "<th class='hide-sm'>Табов</th><th class='hide-sm'>Игроков</th><th>Гранаты</th><th>Войс</th><th></th></tr></thead>"
            f"<tbody>{''.join(out)}</tbody></table></div>"
            if rows else
            "<div class='pagehead'><div><h1>Обзор</h1>" + summary + "</div></div>" + banner
            + "<div class='empty'>Когда бот сохранит первый день КВ, он появится здесь.</div>")
    return render("Дни", body)


# Веса EFF. Войс в EFF не входит. Гранаты — самая «шумная» часть (их часто не успевают
# отсканировать), поэтому их вес снижен; остальное — бой по табу.
EFF_WEIGHTS = {"kills": 0.30, "kda": 0.30, "score": 0.25, "grenades": 0.15}
COMBAT_KEYS = ("kills", "kda", "score")
# Меньше стольких игроков сравнивать не с кем: таб (или гранаты дня) в EFF не участвует.
MIN_COMPARE = 2


def percentile_ranks(values: dict[Any, float]) -> dict[Any, float]:
    """Место каждого среди остальных, шкала 0–100: лучший 100, худший 0, при равенстве — среднее.

    Ранг, а не min–max: один игрок с рекордом не прижимает всех остальных к нулю.
    """
    n = len(values)
    if n < 2:
        return {key: 50.0 for key in values}
    items = list(values.values())
    ranks: dict[Any, float] = {}
    for key, value in values.items():
        less = sum(1 for other in items if other < value)
        equal = sum(1 for other in items if other == value)
        ranks[key] = (less + (equal - 1) / 2) / (n - 1) * 100.0
    return ranks


def tab_metrics(kills: Any, deaths: Any, assists: Any, score: Any) -> dict[str, float | None]:
    """Показатели одной строки одного таба."""
    k, d, a = int(kills or 0), int(deaths or 0), int(assists or 0)
    return {
        "kills": float(k),
        "kda": ((k + a) / d) if d else float(k + a),
        "score": None if score is None else float(score),
    }


def tab_efficiency(entries: list[tuple[int, dict[str, float | None]]]) -> list[float | None]:
    """Боевой EFF каждой строки ОДНОГО таба относительно остальных в этом же табе (0–100).

    Условия у всех в табе одинаковые (карта, соперник), поэтому сравнивать честно только так.
    Если в табе меньше MIN_COMPARE игроков — None. Метрика без значения (например, не
    распознан счёт) пропускается, веса остальных пересчитываются.
    """
    if len(entries) < MIN_COMPARE:
        return [None] * len(entries)
    ranked: dict[str, dict[int, float]] = {}
    for key in COMBAT_KEYS:
        values = {i: m[key] for i, (_, m) in enumerate(entries) if m.get(key) is not None}
        if len(values) >= MIN_COMPARE:
            ranked[key] = percentile_ranks(values)
    result: list[float | None] = []
    for i in range(len(entries)):
        total = weight_sum = 0.0
        for key, ranks in ranked.items():
            if i in ranks:
                total += ranks[i] * EFF_WEIGHTS[key]
                weight_sum += EFF_WEIGHTS[key]
        result.append(total / weight_sum if weight_sum else None)
    return result


def day_efficiency(tabs: list[list[tuple[int, dict[str, float | None]]]],
                   grenades: dict[int, Any] | None = None) -> dict[int, float]:
    """EFF каждого игрока за один день КВ, шкала 0–100.

    tabs — табы дня; таб — список (player_id, tab_metrics(...)) по его строкам.
    grenades — player_id → гранат за день; None / нет в словаре — не отсканировано.

    Бой: каждый таб сравнивается отдельно (см. tab_efficiency), у игрока берётся среднее по
    его табам. Гранаты: место по «гранатам на таб» среди игроков дня, у которых они есть.
    0 гранат — это настоящий ноль, он снижает EFF. Нет данных (None, не отсканировано), либо
    сравнивать не с кем — гранаты в EFF игрока пропускаются: он считается только по табу.
    """
    combat: dict[int, list[float]] = {}
    tab_count: dict[int, int] = {}
    for entries in tabs:
        for (player_id, _), value in zip(entries, tab_efficiency(entries)):
            tab_count[int(player_id)] = tab_count.get(int(player_id), 0) + 1
            if value is not None:
                combat.setdefault(int(player_id), []).append(value)
    rates: dict[int, float] = {}
    for player_id, total in (grenades or {}).items():
        player_id = int(player_id)
        if player_id not in combat or total is None:
            continue
        rates[player_id] = float(total) / tab_count[player_id]
    grenade_rank: dict[int, float] = {}
    if len(rates) >= MIN_COMPARE and max(rates.values()) != min(rates.values()):
        grenade_rank = percentile_ranks(rates)
    weight = EFF_WEIGHTS["grenades"]
    result: dict[int, float] = {}
    for player_id, values in combat.items():
        fight = sum(values) / len(values)
        result[player_id] = (fight * (1 - weight) + grenade_rank[player_id] * weight
                             if player_id in grenade_rank else fight)
    return result


def day_efficiencies(con: Any, only_dates: Any = None) -> dict[str, dict[int, float]]:
    """EFF каждого игрока по каждому дню КВ: дата → player_id → EFF, шкала 0–100 (архивные дни не учитываются).

    only_dates — набор дат, по которым считать (None — все). EFF дня считается по табам, гранаты — только если есть.
    Игрока без таба в этот день в словаре дня нет.
    """
    _ensure_archive_table(con)
    allowed = None if only_dates is None else {str(d) for d in only_dates}
    rows = q(con, """
        SELECT s.match_date date, s.id scan_id, sp.player_id, sp.nick,
               sp.kills, sp.deaths, sp.assists, sp.score
        FROM scans s
        JOIN scan_players sp ON sp.scan_id=s.id
        WHERE s.match_date IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM archived_days _a WHERE _a.match_date=s.match_date)
        ORDER BY s.match_date, s.id, sp.place
    """)
    totals = {(str(r["date"]), int(r["player_id"])): r["grenades"] for r in q(con, """
        SELECT r.match_date date, k.player_id, MAX(k.total_grenades) grenades
        FROM kv_daily_reports r
        JOIN kv_daily_players k ON k.report_id=r.id
        WHERE k.player_id IS NOT NULL AND k.total_grenades IS NOT NULL
        GROUP BY r.match_date, k.player_id
    """)}
    tabs_by_day: dict[str, dict[int, list[tuple[int, dict[str, float | None]]]]] = {}
    grenades_by_day: dict[str, dict[int, Any]] = {}
    resolved: dict[str, int | None] = {}
    for row in rows:
        date = str(row["date"])
        if allowed is not None and date not in allowed:
            continue
        player_id = row["player_id"]
        if player_id is None:
            # ник в табе не привязан к игроку — узнаём по нику, как на странице дня и в составе дня
            nick = str(row["nick"] or "")
            if nick not in resolved:
                resolved[nick] = player_store.resolve_player_id_in_connection(con, nick)
            player_id = resolved[nick]
        if player_id is None:
            continue
        player_id = int(player_id)
        tabs_by_day.setdefault(date, {}).setdefault(int(row["scan_id"]), []).append(
            (player_id, tab_metrics(row["kills"], row["deaths"], row["assists"], row["score"])))
        total = totals.get((date, player_id))
        if total is not None:
            grenades_by_day.setdefault(date, {})[player_id] = total
    return {date: day_efficiency(list(tabs.values()), grenades_by_day.get(date))
            for date, tabs in tabs_by_day.items()}


def average_player_efficiencies(con: Any, only_dates: Any = None) -> dict[int, float]:
    """Средний EFF игрока по дням КВ (архивные дни не учитываются).

    only_dates — набор дат, по которым считать среднее (например, последние N дней КВ);
    None — за всё время. EFF каждого дня считается по табам, гранаты — только если есть.
    """
    samples: dict[int, list[float]] = {}
    for by_player in day_efficiencies(con, only_dates).values():
        for player_id, value in by_player.items():
            samples.setdefault(player_id, []).append(value)
    return {player_id: round(sum(values) / len(values), 1)
            for player_id, values in samples.items()}


def _eff_cell(value: Any) -> str:
    """EFF числом и тонкая полоска справа (шкала 0–100); без данных — прочерк."""
    if value is None:
        return "<span class='na'>—</span>"
    return f"<span class='effcell'><span class='n'>{esc(value)}</span>{meter(value, 100)}</span>"


PERIOD_CHOICES = (1, 2, 3, 5, 7, 14, 30)


def _parse_period(raw: Any) -> int | None:
    """?days=N → последние N дней КВ; пусто / all / мусор → за всё время (None)."""
    try:
        n = int(str(raw or "").strip())
    except ValueError:
        return None
    return min(n, 3650) if n > 0 else None


def _plural(n: int, one: str, few: str, many: str) -> str:
    return (many if 11 <= n % 100 <= 14 else one if n % 10 == 1 else few if 2 <= n % 10 <= 4 else many)


def _ru_days(n: int) -> str:
    word = ("дней" if 11 <= n % 100 <= 14 else
            "день" if n % 10 == 1 else
            "дня" if 2 <= n % 10 <= 4 else "дней")
    return f"{n} {word}"


def _period_bar(current: int | None, view: str = "squads") -> str:
    extra = {"view": "all"} if view == "all" else {}
    options = sorted(set(PERIOD_CHOICES) | ({current} if current else set()))
    links = "".join(
        (f"<b>{n}</b>" if n == current else
         f"<a href='{url_for('players', days=n, **extra)}' "
         f"title='{'Последний день КВ' if n == 1 else 'Последние ' + _ru_days(n) + ' КВ'}'>{n}</a>")
        for n in options)
    everything = ("<b>Всё время</b>" if current is None else
                  f"<a href='{url_for('players', days='all', **extra)}'>Всё время</a>")
    return ("<div class='period'><span class='dim'>Последние дни КВ</span>"
            f"<span class='seg'>{links}</span><span class='seg'>{everything}</span></div>")


def _period_label(period: int | None, dates: list[str], available: int) -> str:
    if not dates:
        return "Дней КВ пока нет"
    newest, oldest = dates[0], dates[-1]
    span = (fmt_date_ru(newest) if newest == oldest
            else f"{fmt_date_ru(oldest)} — {fmt_date_ru(newest)}")
    if period is None:
        return f"За всё время: {_ru_days(len(dates))} КВ, {span}"
    note = f" (в базе только {available})" if available < period else ""
    head = "Последний день КВ" if len(dates) == 1 else f"Последние {_ru_days(len(dates))} КВ"
    return f"{head}{note}: {span}"


SQUAD_ORDER = (1, 2, 3, 4, 5, 6, 99)


def squad_roster(con: Any) -> list[dict[str, Any]]:
    """Игроки, которые сейчас стоят в отрядах 1–6 или «Чемпионах» (99): id, ник, squad_id."""
    return q(con, """
        SELECT p.id, p.canonical_nick, rm.squad_id
        FROM players p
        JOIN (
            SELECT player_id, MIN(squad_id) squad_id
            FROM roster_memberships
            WHERE active=1 AND (squad_id BETWEEN 1 AND 6 OR squad_id=99)
            GROUP BY player_id
        ) rm ON rm.player_id=p.id
        ORDER BY CASE WHEN rm.squad_id=99 THEN 7 ELSE rm.squad_id END,
                 p.canonical_nick COLLATE NOCASE""")


def squad_label(squad_id: int) -> str:
    return "Чемпионы" if squad_id == 99 else f"Отряд {squad_id}"


# Поиск по нику: прячет строки `.player-row` и пустые группы `.squad-group`.
FILTER_PLAYERS_JS = ("function filterPlayers(value){const q=value.toLowerCase();"
                     "document.querySelectorAll('.squad-group').forEach(group=>{let n=0;"
                     "group.querySelectorAll('.player-row').forEach(row=>{const show=row.textContent.toLowerCase().includes(q);"
                     "row.style.display=show?'':'none';if(show)n++});group.style.display=n?'':'none'})}")


def _search_box() -> str:
    return (f"<label class='search'>{icon('search')}"
            "<input id='flt' type='search' placeholder='Поиск по нику…' aria-label='Поиск игрока' "
            "oninput=\"filterPlayers(this.value)\"></label>")


# Сортировка таблицы игроков по любому столбцу: внутри каждого отряда (или по всему списку в виде «Общий»).
SORT_PLAYERS_JS = (
    "function sortPlayerSquads(th){const dir=th.dataset.dir?(th.dataset.dir==='desc'?'asc':'desc'):"
    "(th.dataset.sort==='text'?'asc':'desc');"
    "th.closest('table').querySelectorAll('th.sortable').forEach(x=>delete x.dataset.dir);th.dataset.dir=dir;"
    "const col=th.cellIndex,text=th.dataset.sort==='text';"
    "const value=row=>{const raw=row.cells[col].textContent.trim().replace(',','.');if(!raw||raw==='—')return null;"
    "if(text)return raw;const parsed=Number(raw);return Number.isFinite(parsed)?parsed:null};"
    "document.querySelectorAll('.squad-group').forEach(group=>{const rows=[...group.querySelectorAll('.player-row')];"
    "rows.sort((a,b)=>{const av=value(a),bv=value(b);if(av===null||bv===null)return av===bv?0:av===null?1:-1;"
    "const c=text?av.localeCompare(bv,'ru',{numeric:true}):av-bv;return dir==='asc'?c:-c});"
    "rows.forEach(row=>group.appendChild(row))})}")


@app.get("/players")
def players():
    period = _parse_period(request.args.get("days"))
    with closing(player_store.connect(DB_PATH)) as con:
        rows = squad_roster(con)
        available_dates = all_dates(con)                      # новые сверху, без архива
        dates = available_dates[:period] if period else available_dates
        date_set = set(dates)
        # игрок × день: гранаты (максимум за день) и факт участия (отчёт или таб);
        # в дне с табом отчёт считается только у игроков с таба
        player_store.prepare_tab_filter(con)
        attendance: dict[int, dict[str, int]] = {}
        for r in q(con, f"""
                SELECT player_id, match_date, MAX(g) grenades FROM (
                    SELECT k.player_id player_id, r.match_date match_date, k.total_grenades g
                    FROM kv_daily_players k JOIN kv_daily_reports r ON r.id=k.report_id
                    WHERE k.player_id IS NOT NULL
                      AND {player_store.played_filter('r.match_date', 'k.player_id')}
                    UNION ALL
                    SELECT sp.player_id, s.match_date, NULL
                    FROM scan_players sp JOIN scans s ON s.id=sp.scan_id
                    WHERE sp.player_id IS NOT NULL AND s.match_date IS NOT NULL)
                GROUP BY player_id, match_date"""):
            if r["match_date"] in date_set:
                attendance.setdefault(int(r["player_id"]), {})[r["match_date"]] = int(r["grenades"] or 0)
        eff_by_player = average_player_efficiencies(con, date_set)
    view = "all" if request.args.get("view") == "all" else "squads"
    groups: dict[int, list[dict[str, Any]]] = {99: [], **{i: [] for i in range(1, 7)}}
    for row in rows:
        groups[int(row["squad_id"])].append(row)

    def player_row(r: dict[str, Any], squad: str = "") -> str:
        pid = int(r["id"])
        return (f"<tr class='player-row'><td class='nick grow'><a href='{url_for('player', player_id=r['id'])}'>{esc(r['canonical_nick'])}</a></td>"
                + (f"<td class='l dim'>{esc(squad)}</td>" if squad else "")
                + f"<td>{len(attendance.get(pid, {}))}</td>"
                f"<td class='total'>{sum(attendance.get(pid, {}).values())}</td>"
                f"<td class='eff'>{_eff_cell(eff_by_player.get(pid))}</td></tr>")

    bodies = []
    if view == "all":
        everyone = [(squad_label(sid), r) for sid in SQUAD_ORDER for r in groups[sid]]
        everyone.sort(key=lambda item: -(eff_by_player.get(int(item[1]["id"])) if eff_by_player.get(int(item[1]["id"])) is not None else -1))
        if everyone:
            bodies.append("<tbody class='squad-group'>" + "".join(player_row(r, label) for label, r in everyone) + "</tbody>")
        ncols = 5
    else:
        for squad_id in SQUAD_ORDER:
            members = groups[squad_id]
            if not members:
                continue
            bodies.append(
                f"<tbody class='squad-group'><tr class='squad-title'><td colspan='4'>{squad_label(squad_id)}</td></tr>"
                f"{''.join(player_row(r) for r in members)}</tbody>")
        ncols = 4
    tip = ("Среднее EFF за выбранные дни КВ. Каждый таб сравнивается отдельно: место среди игроков "
           "на этом табе по убийствам 30%, KDA 30%, счёту 25%; гранаты 15% (место по гранатам на таб). "
           "Нет гранат — считается только по табу.")
    view_toggle = ("<span class='seg'>"
                   + ("<b>По отрядам</b>" if view == "squads" else
                      f"<a href='{url_for('players', **({'days': period} if period else {'days': 'all'}))}'>По отрядам</a>")
                   + ("<b>Общий список</b>" if view == "all" else
                      f"<a href='{url_for('players', view='all', **({'days': period} if period else {'days': 'all'}))}'>Общий список</a>")
                   + "</span>")
    squad_th = ("<th class='l sortable' data-sort='text' onclick='sortPlayerSquads(this)'>Отряд</th>" if view == "all" else "")
    tbody_html = "".join(bodies) or (f"<tbody><tr><td colspan='{ncols}' class='empty'>"
                                     "В отрядах пока никого нет.</td></tr></tbody>")
    eff_dir = " data-dir='desc'" if view == "all" else ""
    body = ("<div class='pagehead'><div><h1>Игроки</h1>"
            f"<div class='subtitle'>{esc(_period_label(period, dates, len(available_dates)))}</div>"
            "</div><div class='actions'>"
            f"<a class='button' href='{url_for('players_voice')}' "
            f"title='Во сколько каждый зашёл в войс явки по дням КВ'>{icon('mic')} Заход в войс</a>"
            "</div></div>"
            "<div class='toolbar'>" + _period_bar(period, view) + view_toggle + _search_box() + "</div>"
            "<div class='panel'><table><thead><tr>"
            "<th class='nick grow sortable' data-sort='text' onclick='sortPlayerSquads(this)'>Ник</th>"
            + squad_th +
            "<th class='sortable' data-sort='number' onclick='sortPlayerSquads(this)' "
            "title='Дни КВ из выбранных, где у игрока есть отчёт или таб'>Дни</th>"
            "<th class='sortable' data-sort='number' onclick='sortPlayerSquads(this)' "
            "title='Сумма гранат за выбранные дни'>Грены</th>"
            f"<th class='sortable' data-sort='number'{eff_dir} title='{esc(tip)}' onclick='sortPlayerSquads(this)'>EFF</th>"
            f"</tr></thead>{tbody_html}</table></div>"
            "<script>" + FILTER_PLAYERS_JS + SORT_PLAYERS_JS + "</script>")
    return render("Игроки", body)


def fmt_clock(joined_at: Any) -> str:
    """'2026-11-14T19:39:12+03:00' → '19:39' (время МСК как записал бот); нет данных → '—'."""
    text = str(joined_at or "")
    return text[11:16] if len(text) >= 16 and text[10] in "T " else "—"


def fmt_date_short(day: str) -> str:
    try:
        d = datetime.strptime(day, "%Y-%m-%d")
        return f"{d.strftime('%d.%m')} {RU_WD[d.weekday()]}"
    except ValueError:
        return day


@app.get("/players/voice")
def players_voice():
    """Игроки из отрядов × дни КВ (не архивные): во сколько зашёл в войс явки."""
    na = player_store.not_archived
    with closing(player_store.connect(DB_PATH)) as con:
        rows = squad_roster(con)
        day_pages = set(all_dates(con))
        joins: dict[tuple[int, str], str] = {}
        for r in q(con, f"""SELECT player_id, match_date, MIN(joined_at) joined_at FROM voice_join_times
                            WHERE {na('voice_join_times.match_date')} GROUP BY player_id, match_date"""):
            joins[(int(r["player_id"]), r["match_date"])] = r["joined_at"]
    # дни КВ как в «Обзоре» + день, в котором уже есть заходы, но ещё нет табов/гранат (идёт прямо сейчас)
    dates = sorted(day_pages | {day for _, day in joins}, reverse=True)
    groups: dict[int, list[dict[str, Any]]] = {squad_id: [] for squad_id in SQUAD_ORDER}
    for row in rows:
        groups[int(row["squad_id"])].append(row)

    head = "".join(
        f"<th title='{esc(fmt_date_ru(day))}'>"
        + (f"<a href='{url_for('day', date=day)}'>{esc(fmt_date_short(day))}</a>" if day in day_pages
           else esc(fmt_date_short(day)))
        + "</th>"
        for day in dates)
    bodies = []
    for squad_id in SQUAD_ORDER:
        members = groups[squad_id]
        if not members:
            continue
        member_rows = []
        for r in members:
            cells = []
            for day in dates:
                at = joins.get((int(r["id"]), day))
                cells.append(f"<td class='vj' title='{esc(fmt_date_ru(day))} · {esc(str(at)[11:19])} МСК'>{esc(fmt_clock(at))}</td>"
                             if at else "<td class='dim'>—</td>")
            member_rows.append(
                f"<tr class='player-row'><td class='nick'><a href='{url_for('player', player_id=r['id'])}'>"
                f"{esc(r['canonical_nick'])}</a></td>{''.join(cells)}</tr>")
        bodies.append(f"<tbody class='squad-group'><tr class='squad-title'><td colspan='{len(dates) + 1}'>"
                      f"{squad_label(squad_id)}</td></tr>{''.join(member_rows)}</tbody>")

    if not dates:
        table = "<p class='hint'>Дней КВ пока нет.</p>"
    elif not bodies:
        table = "<p class='hint'>В отрядах пока никого нет.</p>"
    else:
        table = ("<div class='panel'><div class='scroll'><table class='tight vt'><thead><tr>"
                 f"<th class='nick'>Ник</th>{head}</tr></thead>{''.join(bodies)}</table></div></div>")
    body = (f"<a class='back' href='{url_for('players')}'>{icon('arrow-left')} Игроки</a>"
            "<div class='pagehead'><div><h1>Заход в войс</h1>"
            "<div class='subtitle'>Время первого захода в войс явки, МСК</div></div></div>"
            "<div class='toolbar'>" + _search_box() + "</div>"
            + table + f"<script>{FILTER_PLAYERS_JS}</script>")
    return render("Заход в войс", body, wide=True)


def _player_history(con: Any, player_id: int) -> dict[str, Any]:
    """История игрока по дням КВ для /player/<id>: ровно те дни, что в «Обзоре» (без архива).

    «Следы прихода» (shown_up): отметка на явке, время в войсе, заход в войс, гранаты в отчёте или живой счётчик.

    status дня:
      played — у дня есть таб и игрок на нём (явка есть, даже если войс/гранаты не просканированы);
      came   — таб есть, игрока на нём нет, но следы прихода есть: пришёл, а на КВ не играл;
      absent — таб есть, игрока на нём нет и следов нет → неявка;
      report — таба у дня нет, но следы прихода есть: явка по отчёту;
      notab  — таба нет и следов нет: день не просканирован, ничего определённого сказать нельзя
               (войс мог не просканироваться, а на «Берде» гранат почти не бросают) — в явку и средние не идёт.
    Дни раньше первого упоминания игрока (отчёт, таб, счётчик) не показываются: его ещё не было в клане.
    """
    dates = all_dates(con)                                  # новые сверху, как в «Обзоре»
    allowed = set(dates)
    player_store.prepare_tab_filter(con)
    roster_days = {r["match_date"] for r in q(con, "SELECT DISTINCT match_date FROM kv_tab_roster")}
    on_tab = {r["match_date"] for r in q(
        con, "SELECT match_date FROM kv_tab_roster WHERE player_id=?", (player_id,))}
    reports = {r["date"]: r for r in q(con, """
        SELECT r.match_date date, MAX(k.total_grenades) total, MAX(k.voice_seconds) voice,
               MAX(k.attended) attended
        FROM kv_daily_players k JOIN kv_daily_reports r ON r.id=k.report_id
        WHERE k.player_id=? GROUP BY r.match_date""", (player_id,)) if r["date"] in allowed}
    tabs: dict[str, list[dict[str, Any]]] = {}
    resolved: dict[str, int | None] = {}
    for r in q(con, """
            SELECT s.match_date date, s.stage stage, sp.player_id pid, sp.nick nick,
                   sp.kills k, sp.deaths d, sp.assists a, sp.score sc
            FROM scan_players sp JOIN scans s ON s.id=sp.scan_id
            WHERE s.match_date IS NOT NULL AND (sp.player_id=? OR sp.player_id IS NULL)
            ORDER BY s.match_date, COALESCE(s.stage, 99), s.id, sp.place""", (player_id,)):
        if r["date"] not in allowed:
            continue
        if r["pid"] is None:    # ник на табе не привязан — опознаём по нику, как на странице дня
            nick = str(r["nick"] or "")
            if nick not in resolved:
                resolved[nick] = player_store.resolve_player_id_in_connection(con, nick)
            if resolved[nick] != player_id:
                continue
        tabs.setdefault(r["date"], []).append(r)
    # живой счётчик гранат (runtime): сам не показываем, он лишь говорит, что игрок в этот день был
    snap_days = {r["match_date"] for r in q(con, f"""
        SELECT DISTINCT match_date FROM grenade_session_stats
        WHERE player_id=? AND match_date IS NOT NULL
          AND {player_store.not_archived('grenade_session_stats.match_date')}""", (player_id,))}
    joins: dict[str, str] = {}          # первый заход в войс явки (бот пишет его только игрокам из отрядов)
    try:
        for r in q(con, f"""
                SELECT match_date, MIN(joined_at) joined_at FROM voice_join_times
                WHERE player_id=? AND {player_store.not_archived('voice_join_times.match_date')}
                GROUP BY match_date""", (player_id,)):
            joins[r["match_date"]] = r["joined_at"]
    except sqlite3.OperationalError:    # таблицы нет в совсем старой базе — страница от этого не ломается
        pass
    eff_by_day = {d: v[player_id] for d, v in day_efficiencies(con).items() if player_id in v}

    seen = set(reports) | on_tab | set(tabs) | snap_days
    first = min(seen) if seen else None
    days: list[dict[str, Any]] = []
    for d in dates:
        if first is None or d < first:
            continue
        rep = reports.get(d)
        joined = joins.get(d)
        shown_up = (bool(rep and ((rep["total"] or 0) > 0 or (rep["voice"] or 0) > 0 or rep["attended"] == 1))
                    or bool(joined) or d in snap_days)
        if d in roster_days:
            status = "played" if d in on_tab else ("came" if shown_up else "absent")
        else:
            status = "report" if shown_up else "notab"
        row: dict[str, Any] = {"date": d, "status": status, "total": None, "voice": None, "attended": None,
                               "joined": joined, "eff": eff_by_day.get(d), "tabs": tabs.get(d, []), "note": ""}
        if rep:
            row.update(total=rep["total"], voice=rep["voice"], attended=rep["attended"])
        if status == "came":
            # пришёл, но на табе его нет: что именно о нём известно (и, если есть гранаты, возможно, ник на табе не опознан)
            bits = []
            if rep and rep["attended"] == 1:
                bits.append("отметился на явке")
            if rep and rep["voice"]:
                bits.append(f"войс {fmt_sec(rep['voice'])}")
            if joined:
                bits.append(f"в войсе с {fmt_clock(joined)}")
            if rep and rep["total"]:
                bits.append(f"{rep['total']} гран. в отчёте")
            row["note"] = " · ".join(bits)
        elif status == "notab":
            row["note"] = "таба нет, по отчёту не отметился" if rep and rep["attended"] == 0 else "за этот день нет таба"
        days.append(row)
    return {"days": days, "has_join": any(r["joined"] for r in days)}


def _gear_chips(con: Any, player_id: int) -> str:
    """Шмот игрока (то, что указано на странице «Шмот») — только для чтения.

    Шмот — отдельный необязательный модуль: нет его или иконок — страница игрока просто без этой строки.
    """
    try:
        from tools import gear as gear_mod
        entries = {r["slot"]: dict(r) for r in con.execute(
            "SELECT slot, item, level, text FROM player_gear WHERE player_id=?", (player_id,))}
        chips = []
        for column in gear_mod.COLUMNS:
            entry = entries.get(column.key)
            if column.is_text:
                text = str((entry or {}).get("text") or "")
                if text:
                    chips.append(f"<span class='gi txt' title='Скорость'><span class='gs'>Скорость</span>{esc(text)}</span>")
                continue
            key, level = gear_mod._slot_state(column, entry)
            item = gear_mod.ITEMS.get(key)
            if item is None:                    # не указано или «предмета нет» — не показываем
                continue
            color = gear_mod.RARITY[item.rarity][1]
            lv = f"<span class='lv'>+{level}</span>" if level > 0 else ""
            tip = f"{column.title}: {item.name}" + (f" +{level}" if level > 0 else "")
            chips.append(f"<span class='gi' title='{esc(tip)}'>"
                         f"<img src='{esc(gear_mod._icon_url(item.icon))}' alt='' style='border-color:{color}'>"
                         f"<span>{esc(item.name)}{lv}</span></span>")
        return f"<div class='gear'>{''.join(chips)}</div>" if chips else ""
    except Exception:       # noqa: BLE001 — шмот не должен ломать профиль
        return ""


def _fmt_num(value: Any) -> str:
    try:
        return f"{int(value):,}".replace(",", " ")
    except (TypeError, ValueError):
        return "—"


def _tab_cells(tabs: list[dict[str, Any]]) -> tuple[str, str]:
    """Табы дня → (У/С/П, счёт): суммы по всем табам дня, как на странице дня."""
    if not tabs:
        return "<td class='na'>—</td>", "<td class='na'>—</td>"
    k = sum(int(t["k"] or 0) for t in tabs)
    d = sum(int(t["d"] or 0) for t in tabs)
    a = sum(int(t["a"] or 0) for t in tabs)
    scores = [int(t["sc"]) for t in tabs if t["sc"] is not None]
    each = " | ".join(f"{t['k']}/{t['d']}/{t['a']} · {t['sc'] if t['sc'] is not None else '—'}" for t in tabs)
    more = f"<span class='sub'>×{len(tabs)}</span>" if len(tabs) > 1 else ""
    kda = f"<td title='{esc(each)}'>{k}/{d}/{a}{more}</td>"
    score = f"<td>{_fmt_num(sum(scores))}</td>" if scores else "<td class='na'>—</td>"
    return kda, score


def _attendance_cell(row: dict[str, Any]) -> str:
    """Явка для строки, где игрок играл (на табе) или засветился в отчёте дня без таба."""
    if row["status"] == "played":
        return f"<td class='yes c' title='Был на табе'>{icon('check')}</td>"
    return f"<td class='yes c' title='Таба у дня нет; приход виден по отчёту (явка, войс или гранаты)'>{icon('check')}</td>"


def _voice_cell(row: dict[str, Any]) -> str:
    if row["voice"] is None:
        return "<td class='na' title='Войс за этот день не просканирован'>—</td>"
    return f"<td>{fmt_sec(row['voice'])}</td>"


def _join_cell(row: dict[str, Any]) -> str:
    if not row["joined"]:
        return "<td class='na' title='Время захода в войс не записано (бот пишет его только во время живого КВ)'>—</td>"
    return f"<td class='vj' title='{esc(str(row['joined'])[11:19])} МСК'>{esc(fmt_clock(row['joined']))}</td>"


def _tile(label: str, value: str, sub: str = "", title: str = "") -> str:
    tip = f" title='{esc(title)}'" if title else ""
    extra = f"<span class='of'>{sub}</span>" if sub else ""
    return f"<div class='metric'{tip}><label>{esc(label)}</label><b>{value}{extra}</b></div>"


@app.get("/player/<int:player_id>")
def player(player_id: int):
    with closing(player_store.connect(DB_PATH)) as con:
        p = one(con, "SELECT canonical_nick FROM players WHERE id=?", (player_id,))
        if not p:
            return render("Нет игрока", "<p>Игрок не найден.</p>"), 404
        hist = _player_history(con, player_id)
        gear_html = _gear_chips(con, player_id)
    days = hist["days"]
    show_join = hist["has_join"]
    ncols = 7 + (1 if show_join else 0)
    played = [r for r in days if r["status"] == "played"]
    present = [r for r in days if r["status"] in ("played", "came", "report")]    # явка: играл или хотя бы пришёл
    absent = [r for r in days if r["status"] == "absent"]
    plays = [r for r in days if r["status"] in ("played", "report")]              # дни, где он (по данным) играл

    # ── сводка: 5 плиток в один ряд ──
    totals = [int(r["total"]) for r in plays if r["total"] is not None]
    max_total = max(totals, default=0)
    grenades = sum(totals)
    voices = [int(r["voice"]) for r in present if r["voice"] is not None]    # None = войс не просканирован, это не 0
    tab_rows = [t for r in played for t in r["tabs"]]
    kills = sum(int(t["k"] or 0) for t in tab_rows)
    deaths = sum(int(t["d"] or 0) for t in tab_rows)
    effs = [float(r["eff"]) for r in played if r["eff"] is not None]
    n_days = len(present) + len(absent)

    tiles = [
        _tile("КД", f"{kills / deaths:.2f}" if deaths else (f"{kills}" if tab_rows else "—"), "",
              "Убийства / Смерти по всем табам"),
        _tile("Гранат за КВ", f"{grenades / len(totals):.0f}" if totals else "—", "",
              "Среднее за день КВ по дням, где игрок играл и есть отчёт"),
        _tile("EFF", f"{sum(effs) / len(effs):.1f}" if effs else "—", "из 100" if effs else "",
              "Средний EFF по дням с табом (как на странице «Игроки»)"),
        _tile("Явка", f"{len(present)}", f"из {n_days} · {round(100 * len(present) / n_days)}%" if n_days else "",
              "Дни, где игрок пришёл (играл или хотя бы был на явке/в войсе), из всех дней КВ. "
              "Дни без таба и без следов игрока («Не просканирован») не считаются"),
        _tile("Войс за КВ ср.", fmt_sec(round(sum(voices) / len(voices))) if voices else "—",
              f"всего {fmt_sec(sum(voices))}" if voices else "",
              "Среднее время в войсе за день КВ, где войс просканирован"),
    ]
    summary = f"<div class='metrics'>{''.join(tiles)}</div>" if days else ""

    # ── таблица по дням ──
    trs = []
    for r in days:
        link = f"<td class='nick grow'><a href='{url_for('day', date=r['date'])}'>{fmt_date_ru(r['date'])}</a></td>"
        marks = {"absent": ("", "Неявка", "Нет на табе этого дня и никаких следов прихода (явка, войс, гранаты)"),
                 "came": ("amb", "Не играл", "Пришёл (явка или войс), но на табе этого дня его нет"),
                 "notab": ("gry", "Не просканирован", "Для этого дня нет таба — игру и явку определить нельзя")}
        if r["status"] in marks:
            css, label, tip = marks[r["status"]]
            if r["note"]:                       # подробности — только во всплывающей подсказке, в строке текста нет
                tip += f" — {r['note']}"
            pill = f"<td class='c' title='{esc(tip)}'><span class='pill {css}'>{label}</span></td>"
            if r["status"] == "came":           # пришёл: войс и заход видны, а игры (гранаты, таб, EFF) нет
                trs.append(f"<tr class='came'>{link}{pill}<td class='na'></td>{_voice_cell(r)}"
                           f"{_join_cell(r) if show_join else ''}<td class='note' colspan='3'></td></tr>")
            else:
                trs.append(f"<tr class='{r['status']}'>{link}{pill}<td class='note' colspan='{ncols - 2}'></td></tr>")
            continue
        if r["total"] is None:
            gren = "<td class='na'>—</td>"
        else:
            flag = (f" <span class='flag' title='Максимум и больше 150 — проверить на накрутку'>{icon('alert')}</span>"
                    if r["total"] == max_total and max_total > 150 else "")
            gren = (f"<td class='total'><span class='mcell'>{meter(r['total'], max_total)}"
                    f"<b>{r['total']}</b></span>{flag}</td>")
        join = _join_cell(r) if show_join else ""
        if r["status"] == "report":      # таба нет: вместо трёх прочерков — одна пометка
            tail = "<td class='note' colspan='3'>нет таба</td>"
        else:
            eff = (f"<td>{_eff_cell(round(float(r['eff']), 1))}</td>" if r["eff"] is not None else "<td class='na'>—</td>")
            kda, score = _tab_cells(r["tabs"])
            tail = f"{kda}{score}{eff}"
        trs.append(f"<tr>{link}{_attendance_cell(r)}{gren}{_voice_cell(r)}{join}{tail}</tr>")

    if days:
        table = ("<div class='panel'><table class='tight'><thead><tr><th class='nick grow'>Дата</th>"
                 "<th class='c' title='Галочка — был на КВ; «Не играл» — пришёл, но на табе его нет; «Неявка» — не пришёл; «Не просканирован» — у дня нет таба'>Явка</th><th>Гранаты</th><th>Войс</th>"
                 + ("<th title='Во сколько зашёл в войс явки'>Зашёл</th>" if show_join else "") +
                 "<th title='Убийства / Смерти / Помощь за день (сумма по табам)'>У/С/П</th>"
                 "<th title='Сумма счёта по табам дня'>Счёт</th>"
                 "<th title='EFF за этот день: место среди игроков на табе (как на странице «Игроки»)'>EFF</th>"
                 f"</tr></thead><tbody>{''.join(trs)}</tbody></table></div>")
    else:
        table = "<div class='empty'>По этому игроку пока нет дней КВ.</div>"

    body = ("<div class='ph'>"
            f"<a class='back' href='{url_for('players')}'>{icon('arrow-left')} Игроки</a>"
            f"<div class='pagehead'><h1>{esc(p['canonical_nick'])}</h1></div>"
            + summary + gear_html + table + "</div>")
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
                stages[int(r["stage_no"])] = None if r["grenades"] is None else int(r["grenades"])
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
    date = _valid_day(date, 404)
    mode = request.args.get("mode", "total")
    if mode not in {"total", "stages"}:
        mode = "total"
    with closing(player_store.connect(DB_PATH)) as con:
        scans, report, krows = _day_data(con, date)
        dates = all_dates(con)
        archived = one(con, "SELECT archived_at FROM archived_days WHERE match_date=?", (date,))
    tab_sum = _tab_totals(scans)
    # День с табом: играли только те, кто на табе. Остальные из отчёта (гранаты/войс на весь состав)
    # в таблицу и итоги дня не попадают — они лежат свёрнутым списком «Не в табе» внизу страницы.
    # Дни без таба (или с табом, где ни один ник не опознан) показываются целиком, как раньше.
    skipped_rows: list[dict[str, Any]] = []
    if tab_sum:
        skipped_rows = [k for k in krows if k["player_id"] not in tab_sum]
        krows = [k for k in krows if k["player_id"] in tab_sum]
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
    # EFF: каждый таб сравнивается отдельно; гранаты учитываются, только если они есть.
    grenades_by_player = {int(k["player_id"]): k["total_grenades"] for k in krows
                          if k.get("player_id") is not None and k.get("total_grenades") is not None}
    day_tabs = [
        [(int(r["player_id"]), tab_metrics(r["kills"], r["deaths"], r["assists"], r["score"]))
         for r in scan["players"] if r["player_id"] is not None]
        for scan in scans
    ]
    day_eff = {player_id: round(value, 1)
               for player_id, value in day_efficiency(day_tabs, grenades_by_player).items()}

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
                tab_cells = (f"<td class='sum hide-sm' title='{tip}'>{t['k']}</td>"
                             f"<td class='sum hide-sm' title='{tip}'>{t['d']}</td>"
                             f"<td class='sum hide-sm' title='{tip}'>{t['a']}</td>"
                             f"<td class='sum hide-sm' title='{tip}'>{t['s']}</td>"
                             f"<td class='sum hide-sm'>{kda:.2f}</td><td class='eff'>{eff_cell}</td>")
            else:
                tab_cells = ("<td class='dim hide-sm'>—</td><td class='dim hide-sm'>—</td>"
                             "<td class='dim hide-sm'>—</td><td class='dim hide-sm'>—</td>"
                             "<td class='dim hide-sm'>—</td><td class='dim'>—</td>")

        if scan_only:
            gcells = "".join("<td class='dim'>—</td>" for _ in range(max_stage)) if mode == "stages" else ""
            total_cell = "<td class='dim'>—</td>"
            voice_cell = "<td class='dim'>—</td>"
            action_cell = "<td class='act'></td>"
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
                total_cell = (f"<td class='total'>{k['total_grenades']}</td>" if k['total_grenades'] is not None
                              else "<td class='total dim' title='не отсканировано'>—</td>")
            else:
                gcells = ""
                total_cell = (f"<td class='gcol'><input type='number' min='0' "
                              f"name='w{row_no}' placeholder='—' "
                              f"value='{'' if k['total_grenades'] is None else k['total_grenades']}' "
                              f"title='гранат за день (всего); пусто — не отсканировано'></td>")
            voice_cell = (f"<td><input type='text' inputmode='numeric' name='v{row_no}' "
                          f"value='{'' if k['voice_seconds'] is None else fmt_sec(k['voice_seconds'])}' placeholder='—' "
                          "title='минуты:секунды; пусто — не отсканировано'></td>")
            action_cell = (f"<td class='act'><button class='danger rowdel' name='__del_row' value='{row_no}' "
                           f"formaction='{save_url}' data-confirm='Удалить гранаты и войс "
                           f"{esc(nick)} за {fmt_date_ru(date)}?' aria-label='Удалить строку' title='Удалить строку'>"
                           f"{icon('x')}</button></td>")
            source_badge = ""

        link = (f"<a href='{url_for('player', player_id=k['player_id'])}'>{esc(nick)}</a>"
                if k["player_id"] else esc(nick))
        rows_html.append(
            f"<tr><td class='dim rank hide-sm'>{display_no}</td><td class='nick'>{link}{source_badge}</td>"
            f"{tab_cells}{gcells}{total_cell}{voice_cell}{action_cell}</tr>")

    # ── исходные табы: правка У/С/П/СЧЁТ по каждому табу ──────────────────
    scan_tables = []
    scan_player_count = sum(len(s["players"]) for s in scans)
    unmatched_count = sum(1 for s in scans for r in s["players"] if r["player_id"] is None)
    for s in scans:
        head = (
            "<tr><td colspan='7' style='text-align:left'>"
            f"<button class='danger wide' name='__del_scan' value='{s['id']}' "
            f"formaction='{save_url}' data-confirm='Удалить весь таб?'"
            f">{icon('trash')} Удалить таб</button> "
            f"<a class='button' href='{url_for('day_relink', date=date, scan=s['id'])}' "
            f"title='Перепривязать ники только этого таба к игрокам'>{icon('refresh')} Ники</a> <span class='dim'>"
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

    # ── не в табе: не играли, в статистику не входят (данные не стёрты) ─────
    skipped_block = ""
    if skipped_rows:
        notes = ""
        if report and len(scans) < int(report["stage_count"]):
            notes += (f"<p class='hint flag'>{icon('alert')} Загружено табов: {len(scans)} из {report['stage_count']} этапов. "
                      "Кто не попал ни на один загруженный таб, считается не игравшим.</p>")
        if unmatched_count:
            notes += (f"<p class='hint flag'>{icon('alert')} В табах {unmatched_count} ников не найдено в базе. Если кто-то из "
                      "списка ниже играл, добавьте его ник в алиасы — игрок сразу вернётся в день.</p>")
        skipped_trs = "".join(
            "<tr><td class='nick'>"
            + (f"<a href='{url_for('player', player_id=k['player_id'])}'>{esc(k['canonical_nick'] or k['raw_nick'])}</a>"
               if k["player_id"] else esc(k["canonical_nick"] or k["raw_nick"] or "—"))
            + f"</td><td>{'—' if k['total_grenades'] is None else k['total_grenades']}</td>"
            f"<td>{'—' if k['voice_seconds'] is None else fmt_sec(k['voice_seconds'])}</td></tr>"
            for k in sorted(skipped_rows, key=lambda k: str(k["canonical_nick"] or k["raw_nick"] or "").casefold()))
        skipped_block = (
            f"<details class='skipped'><summary>Не в табе ({len(skipped_rows)}) · не играли, "
            "в статистику не входят</summary>" + notes
            + "<div class='scroll'><table><thead><tr><th class='nick'>Ник</th><th>Гранаты</th><th>Войс</th></tr></thead>"
            f"<tbody>{skipped_trs}</tbody></table></div></details>")

    # ── навигация по дням ──────────────────────────────────────────────────
    pos = dates.index(date) if date in dates else -1
    prev_d = dates[pos + 1] if 0 <= pos < len(dates) - 1 else None   # старее
    next_d = dates[pos - 1] if pos > 0 else None                     # новее
    options = "".join(
        f"<option value='{url_for('day', date=d)}'"
        + (" selected" if d == date else "") + f">{fmt_date_ru(d)}</option>"
        for d in dates)
    day_select = ("<select aria-label='Перейти к дню' style='margin-left:auto' "
                  "onchange=\"if(this.value)location=this.value\">"
                  f"<option value=''>Перейти к дню…</option>{options}</select>")
    older = (f"<a class='button' href='{url_for('day', date=prev_d)}' title='Старее' aria-label='Предыдущий день'>{icon('left')}</a>"
             if prev_d else f"<button disabled aria-label='Предыдущего дня нет'>{icon('left')}</button>")
    newer = (f"<a class='button' href='{url_for('day', date=next_d)}' title='Новее' aria-label='Следующий день'>{icon('right')}</a>"
             if next_d else f"<button disabled aria-label='Следующего дня нет'>{icon('right')}</button>")

    banner = ""
    if request.args.get("saved"):
        banner = f"<div class='banner'>{icon('check')}<span>Сохранено</span></div>"
    elif request.args.get("deleted"):
        banner = f"<div class='banner'>{icon('trash')}<span>Удалено</span></div>"
    elif request.args.get("restored"):
        banner = f"<div class='banner'>{icon('undo')}<span>День возвращён из архива</span></div>"
    elif request.args.get("relinked") is not None:
        moved = _to_int(request.args.get("relinked")) or 0
        banner = (f"<div class='banner'>{icon('refresh')}<span>Перепривязано строк: {moved}</span> "
                  f"<a href='{url_for('day_relink', date=date)}'>история / вернуть</a></div>"
                  if moved else "<div class='banner' style='background:var(--soft);color:var(--dim)'>"
                                "Ничего не выбрано — изменений нет</div>")
    elif request.args.get("unrelinked") is not None:
        banner = (f"<div class='banner'>{icon('undo')}<span>Возвращено строк: {_to_int(request.args.get('unrelinked')) or 0}"
                  + (f" · пропущено (с тех пор менялись): {_to_int(request.args.get('skipped'))}"
                     if _to_int(request.args.get("skipped")) else "") + "</span></div>")
    relink_item = (f"<a class='button menu-item' href='{url_for('day_relink', date=date)}' "
                   "title='Заново сопоставить ники всех табов дня с игроками (сначала предпросмотр, можно вернуть)'>"
                   f"{icon('refresh')} Перепривязать ники табов</a>" if scans else "")
    if archived:
        banner += (f"<div class='banner' style='background:var(--soft);color:var(--dim)'>{icon('archive')}"
                   "<span>День в архиве: не входит в список дней, итоги и EFF игроков</span></div>")
        archive_ctl = (f"<form method='post' action='{url_for('day_unarchive', date=date)}'>"
                       f"<button class='primary'>{icon('undo')} Вернуть из архива</button></form>")
    else:
        confirm_text = (f"Убрать день {fmt_date_ru(date)} в архив?\n\n"
                        "Он пропадёт из списка дней, суммарных гранат/дней и EFF игроков. "
                        "Вернуть можно в разделе «Архив».")
        archive_ctl = (
            "<details class='menu'><summary aria-label='Действия с днём' title='Действия с днём'>"
            f"{icon('more')}</summary><div class='menu-pop'>{relink_item}"
            f"<form method='post' action='{url_for('day_archive', date=date)}' data-confirm='{esc(confirm_text)}'>"
            f"<button class='danger wide menu-item'>{icon('archive')} Удалить день (в архив)</button></form></div></details>")

    chips = ("<div class='chips'>"
             f"<span class='chip'>Игроков: {len(krows)}</span>"
             + (f"<span class='chip' title='Есть в отчёте дня, но нет на табе: не играли, в итоги не входят'>"
                f"Не в табе: {len(skipped_rows)}</span>" if skipped_rows else "")
             + f"<span class='chip'>Гранат: {sum(k['total_grenades'] or 0 for k in krows)}"
             "</span>"
             "<span class='chip'>Войс: "
             + fmt_sec(sum(k["voice_seconds"] or 0 for k in krows)) + "</span></div>")

    tab_head = ("<th class='sortable hide-sm' data-sort='number'>У</th>"
                "<th class='sortable hide-sm' data-sort='number'>С</th>"
                "<th class='sortable hide-sm' data-sort='number'>П</th>"
                "<th class='sortable hide-sm' data-sort='number'>Счёт</th>"
                "<th class='sortable hide-sm' data-sort='number'>KDA</th>"
                "<th class='sortable' data-sort='number' title='EFF дня: каждый таб сравнивается отдельно — убийства 30%, KDA 30%, счёт 25%; гранаты 15% (нет гранат — только по табу)'>EFF</th>"
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

    guard_script = """<script>
(function(){var f=document.querySelector('form.day-edit');if(!f)return;var dirty=false;
f.addEventListener('input',function(){dirty=true});
f.addEventListener('submit',function(){dirty=false});f.addEventListener('reset',function(){dirty=false});
window.addEventListener('beforeunload',function(e){if(dirty){e.preventDefault();e.returnValue=''}});
document.addEventListener('keydown',function(e){if((e.ctrlKey||e.metaKey)&&'sSыЫ'.indexOf(e.key)>-1&&e.key.length===1){e.preventDefault();var b=f.querySelector('button.save');if(b)f.requestSubmit(b)}});
})();
</script>"""

    kv_block = (
        f"<div class='panel{' xscroll' if mode == 'stages' else ''}'><table id='day-table'><thead><tr>"
        "<th class='sortable hide-sm' data-sort='number'>#</th>"
        "<th class='nick sortable' data-sort='text'>Ник</th>"
        + tab_head + stage_cols
        + "<th class='sortable' data-sort='number'>Гранаты</th>"
        "<th class='sortable' data-sort='time'>Войс</th><th></th></tr></thead>"
        + f"<tbody>{''.join(rows_html)}</tbody></table></div>" + sort_script
        if krows else
        "<p class='hint'>Дневной отчёт за эту дату не найден "
        "(гранаты за этот день не импортировались).</p>")

    savebar = ("<div class='savebar' role='region' aria-label='Несохранённые изменения'><span class='cnt'></span>"
               "<button type='button' data-cancel>Отмена</button>"
               f"<button class='save' title='Ctrl+S'>{icon('check')} Сохранить</button></div>")
    toolbar = f"<div class='toolbar'>{toggle}{day_select}</div>" if krows else ""
    body = (f"<a class='back' href='{url_for('index')}'>{icon('arrow-left')} Обзор</a>"
            f"<div class='pagehead start'><div><div class='day-title'>{older}<h1>{fmt_date_ru(date)}</h1>{newer}</div>{chips}</div>"
            f"{archive_ctl}</div>{banner}{toolbar}"
            f"<form class='day-edit' method='post' action='{save_url}'>"
            f"<input type='hidden' name='mode' value='{mode}'>"
            + kv_block + tabs_details + savebar + "</form>"
            + skipped_block + guard_script)
    return render(date, body)


def _valid_day(date: str, status: int = 400) -> str:
    try:
        return player_store.parse_match_date(date)
    except ValueError:
        abort(status)


@app.post("/day/<date>/archive")
def day_archive(date: str):
    """Спрятать день КВ: табы, гранаты и войс дня перестают учитываться везде в панели."""
    date = _valid_day(date)
    with closing(player_store.connect(DB_PATH)) as con:
        con.execute("INSERT OR IGNORE INTO archived_days(match_date) VALUES(?)", (date,))
    return redirect(url_for("index", archived=date), code=303)


@app.post("/day/<date>/unarchive")
def day_unarchive(date: str):
    date = _valid_day(date)
    with closing(player_store.connect(DB_PATH)) as con:
        con.execute("DELETE FROM archived_days WHERE match_date=?", (date,))
    return redirect(url_for("day", date=date, restored=1), code=303)


@app.get("/day/<date>/relink")
def day_relink(date: str):
    """Предпросмотр перепривязки ников табов дня (или одного таба) к игрокам. Ничего не меняет."""
    date = _valid_day(date)
    scan_id = _to_int(request.args.get("scan"))
    with closing(player_store.connect(DB_PATH)) as con:
        scans = q(con, """SELECT id, stage, map, scanned_at FROM scans
                          WHERE match_date=? ORDER BY COALESCE(stage,99), id""", (date,))
        if scan_id is not None and not any(s["id"] == scan_id for s in scans):
            abort(404)
        plan = player_store.plan_scan_relink(con, date, scan_id)
        history = player_store.relink_history(con, date)
    if scan_id is not None:
        s = next(s for s in scans if s["id"] == scan_id)
        scope = "таб: " + " · ".join(filter(None, [
            f"этап {stage_label(s['stage'])}" if s["stage"] else "без этапа",
            esc(s["map"] or ""), esc((s["scanned_at"] or "")[:16])]))
    else:
        scope = f"все табы дня ({len(scans)})"
    action = url_for("day_relink_apply", date=date)

    group_rows = "".join(
        "<tr>"
        f"<td class='c'><input type='checkbox' name='pick' value='{esc(g['key'])}'{'' if g['conflict'] else ' checked'}></td>"
        f"<td class='nick'>{esc(g['nick'])}</td>"
        f"<td class='l'>{esc(g['old_nick']) if g['old_nick'] else '<span class=dim>не привязан</span>'}</td>"
        f"<td class='l'><b>→ {esc(g['new_nick'])}</b></td><td>{len(g['rows'])}</td>"
        "<td class='nick dim'>"
        + (f"<span class='flag'>конфликт:</span> ник ещё записан алиасом у {esc(', '.join(g['also']))}; "
           "по основному нику отдаём игроку с таким именем. Галочка снята — включите, если это верно"
           if g["conflict"] else "")
        + "</td></tr>"
        for g in plan["groups"])
    changes = sum(len(g["rows"]) for g in plan["groups"])
    kept_rows = sum(k["rows"] for k in plan["kept"])
    if plan["groups"]:
        form = (
            f"<form method='post' action='{action}' data-confirm='Перепривязать выбранные ники? Это можно вернуть кнопкой «Вернуть».'>"
            + (f"<input type='hidden' name='scan' value='{scan_id}'>" if scan_id is not None else "")
            + "<div class='panel'><table><thead><tr><th class='c'></th><th class='nick'>Ник на табе</th><th class='l'>Сейчас</th>"
              "<th class='l'>Станет</th><th>Строк</th><th class='nick'>Заметка</th></tr></thead>"
              f"<tbody>{group_rows}</tbody></table></div>"
            f"<p class='actions' style='margin-top:12px'><button class='save'>{icon('refresh')} Применить выбранное</button>"
            f"<a class='button' href='{url_for('day', date=date)}'>Отмена</a></p></form>")
    else:
        form = ("<p class='hint'>Менять нечего: все ники, которые можно определить однозначно, "
                f"уже привязаны правильно. <a href='{url_for('day', date=date)}'>К дню</a></p>")
    kept_block = ""
    if plan["kept"]:
        kept_block = (
            f"<details><summary>Останутся как есть ({kept_rows} строк): ник не опознан или у него несколько владельцев</summary>"
            "<div class='scroll'><table><thead><tr><th class='nick'>Ник на табе</th><th>Строк</th>"
            "<th class='nick'>Почему</th></tr></thead><tbody>"
            + "".join(f"<tr><td class='nick'>{esc(k['nick'])}</td><td>{k['rows']}</td>"
                      f"<td class='nick dim'>{esc(k['reason'])}</td></tr>" for k in plan["kept"])
            + "</tbody></table></div></details>")
    hist_block = ""
    if history:
        undo = url_for("day_relink_undo", date=date)
        hist_block = (
            "<h2>История перепривязок дня</h2><div class='panel'><table><thead><tr><th>Когда (МСК)</th>"
            "<th class='nick'>Ники</th><th>Строк</th><th></th></tr></thead><tbody>"
            + "".join(
                f"<tr><td>{esc(h['at'])}</td><td class='nick'>{esc(h['nicks'])}</td><td>{h['rows']}</td><td>"
                + ("<span class='dim'>возвращено</span>" if h["undone"] else
                   f"<form method='post' action='{undo}' data-confirm='Вернуть прежнюю привязку этих строк?' style='margin:0'>"
                   f"<input type='hidden' name='batch' value='{esc(h['batch'])}'>"
                   f"<button>{icon('undo')} Вернуть</button></form>")
                + "</td></tr>" for h in history)
            + "</tbody></table></div>")
    body = (
        f"<a class='back' href='{url_for('day', date=date)}'>{icon('arrow-left')} {fmt_date_ru(date)}</a>"
        f"<div class='pagehead'><div><h1>Перепривязка ников</h1>"
        f"<div class='subtitle'>{fmt_date_ru(date)} · {scope}</div></div></div>"
        "<p class='hint'>Меняется только то, к какому игроку привязана строка таба. Цифры, отчёты, войс и составы "
        "не трогаются, ничего не удаляется и не сливается. Применяйте к дням, когда ник уже принадлежал новому "
        "игроку: в более старых днях тот же ник мог быть другим человеком. Всё можно вернуть.</p>"
        f"<div class='chips'><span class='chip'>Строк в табах: {plan['total']}</span>"
        f"<span class='chip'>Уже верно: {plan['unchanged']}</span>"
        f"<span class='chip'>Изменится: {changes}</span>"
        f"<span class='chip'>Останутся как есть: {kept_rows}</span></div>"
        + form + kept_block + hist_block)
    return render(f"Перепривязка · {date}", body)


@app.post("/day/<date>/relink")
def day_relink_apply(date: str):
    date = _valid_day(date)
    scan_id = _to_int(request.form.get("scan"))
    _batch, moved = player_store.apply_scan_relink(DB_PATH, date, set(request.form.getlist("pick")), scan_id)
    return redirect(url_for("day", date=date, relinked=moved), code=303)


@app.post("/day/<date>/relink/undo")
def day_relink_undo(date: str):
    date = _valid_day(date)
    restored, skipped = player_store.undo_scan_relink(DB_PATH, request.form.get("batch", ""))
    return redirect(url_for("day", date=date, unrelinked=restored, skipped=skipped), code=303)


@app.get("/archive")
def archive():
    with closing(player_store.connect(DB_PATH)) as con:
        player_store.prepare_tab_filter(con)
        played = player_store.played_filter("r.match_date", "p.player_id")
        rows = q(con, f"""
            SELECT a.match_date date, a.archived_at,
                   (SELECT COUNT(*) FROM scans s WHERE s.match_date=a.match_date) tabs,
                   COALESCE(NULLIF((SELECT COUNT(*) FROM kv_tab_roster tr WHERE tr.match_date=a.match_date), 0),
                            (SELECT COUNT(DISTINCT p.player_id) FROM kv_daily_reports r
                               JOIN kv_daily_players p ON p.report_id=r.id
                               WHERE r.match_date=a.match_date AND {played})) players,
                   (SELECT SUM(p.total_grenades) FROM kv_daily_reports r
                      JOIN kv_daily_players p ON p.report_id=r.id
                      WHERE r.match_date=a.match_date AND {played}) grenades,
                   (SELECT SUM(p.voice_seconds) FROM kv_daily_reports r
                      JOIN kv_daily_players p ON p.report_id=r.id
                      WHERE r.match_date=a.match_date AND {played}) voice
            FROM archived_days a ORDER BY a.match_date DESC""")
    trs = "".join(
        f"<tr><td class='nick'><a href='{url_for('day', date=r['date'])}'>{fmt_date_ru(r['date'])}</a></td>"
        f"<td>{r['tabs'] or 0}</td><td>{r['players'] or 0}</td>"
        f"<td class='total'>{r['grenades'] or 0}</td><td>{fmt_sec(r['voice'])}</td>"
        f"<td class='dim'>{fmt_date_ru(str(r['archived_at'])[:10])}</td>"
        f"<td class='act'><form method='post' action='{url_for('day_unarchive', date=r['date'])}' style='margin:0'>"
        f"<button>{icon('undo')} Вернуть</button></form></td></tr>"
        for r in rows)
    table = ("<div class='panel'><table><thead><tr><th>Дата</th><th>Табов</th><th>Игроков</th>"
             "<th>Гранаты</th><th>Войс</th><th>В архиве с</th><th></th></tr></thead>"
             f"<tbody>{trs}</tbody></table></div>" if rows else
             "<p class='hint'>Архив пуст. Сюда попадают дни, убранные кнопкой «Удалить день» на странице дня.</p>")
    body = (f"<a class='back' href='{url_for('index')}'>{icon('arrow-left')} Обзор</a>"
            "<div class='pagehead'><div><h1>Архив дней КВ</h1>"
            "<div class='subtitle'>Эти дни не учитываются в списке дней, итогах и EFF игроков. "
            "Данные не удалены, день можно вернуть.</div></div></div>" + table)
    return render("Архив", body)


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
    date = _valid_day(date)
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
            removed = con.execute("DELETE FROM scans WHERE id=?", (del_scan,)).rowcount
            con.execute("COMMIT")
            flash = {"deleted": 1} if removed else {}      # такого таба нет: нечего подтверждать
            return redirect(url_for("day", date=date, mode=mode, **flash), code=303)

        report = one(con, """SELECT id,stage_count FROM kv_daily_reports WHERE match_date=?
                             ORDER BY id DESC LIMIT 1""", (date,))

        # удаление строки игрока (гранаты+войс за день)
        del_row = _to_int(form.get("__del_row"))
        if del_row is not None and report:
            gone = one(con, """SELECT player_id FROM kv_daily_players
                               WHERE report_id=? AND row_no=?""", (report["id"], del_row))
            if gone is None:                                # строку уже убрали (устаревшая страница)
                con.execute("ROLLBACK")
                return redirect(url_for("day", date=date, mode=mode), code=303)
            pid = gone["player_id"]
            con.execute("DELETE FROM kv_daily_grenade_stages WHERE report_id=? AND row_no=?",
                        (report["id"], del_row))
            con.execute("DELETE FROM kv_daily_players WHERE report_id=? AND row_no=?",
                        (report["id"], del_row))
            if pid is not None:
                con.execute("""DELETE FROM grenade_session_stats
                               WHERE player_id=? AND match_date=?""", (pid, date))
            con.execute("COMMIT")
            return redirect(url_for("day", date=date, deleted=1, mode=mode), code=303)

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
                if str(value).strip():
                    num = _to_int(value)
                    if num is None or num < 0:
                        continue
                else:
                    num = None              # очистили поле: вернуть «не отсканировано», а не оставить старый 0
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
    return redirect(url_for("day", date=date, saved=1, mode=mode), code=303)


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
