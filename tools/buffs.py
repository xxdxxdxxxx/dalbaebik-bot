"""Бафы клана: заявки из Discord-канала → общая таблица в админ-панели → история выдачи.

Игроки пишут в постоянный канал в любое время по шаблону:

    Ник
    100 топотов, 10 убийц
    3 хилки

Первая строка — игровой ник, дальше «число + название» (через запятую или с новой строки).
Бот читает канал (команда /buffs_channel). Панель показывает доску — статичную таблицу: у каждого
игрока его последняя заявка, она висит, пока сообщение есть в канале (удалили в Discord — заявка
исчезла). На КВ у игрока ставят галочку «выдано» — она сразу пишется в историю (ник, сколько и каких
бафов, день выдачи). Со следующего дня выдачи (в 05:00 МСК) галочки сбрасываются, а игроки остаются
на доске — их можно отметить снова. В истории — итоги по каждому баффу.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

try:  # админ-панель можно запустить и без discord.py
    import discord
    from discord import app_commands
except ImportError:  # pragma: no cover
    discord = app_commands = None  # type: ignore[assignment]

from flask import jsonify, redirect, request, url_for

LOG = logging.getLogger(__name__)

MSK = timezone(timedelta(hours=3))   # Москва без перехода на летнее время

# Колонки таблицы — в таком порядке. key нужен только внутри базы.
BUFFS = (
    ("shiza", "Шизы"),
    ("topot", "Топоты"),
    ("killer", "Убийцы"),
    ("heal", "Хилки"),
    ("napalm", "Напалмы"),
    ("smoke", "Смоки"),
    ("mine", "Мины"),
)
BUFF_KEYS = tuple(key for key, _ in BUFFS)

# Как игроки называют баффы. Слово подходит, если начинается с одной из основ
# (падежи и опечатки вроде «убиц» ловятся сами). Новое название — добавить основу сюда.
BUFF_STEMS: dict[str, tuple[str, ...]] = {
    "shiza": ("шиз",),
    "topot": ("топот", "топт"),
    "killer": ("убийц", "убиц", "убий"),
    "heal": ("хил", "подорожник", "зелен"),     # «зелень», «зелени», «зеленки», «ящика зелени»
    "napalm": ("напалм",),
    "smoke": ("смок", "дым", "завес"),            # смок = завеса = дым
}
# «мин» слишком короткое слово для основы («минута», «минимум»), поэтому только точные формы.
_MINE_FORMS = frozenset({"мин", "мины", "мина", "минов", "мину", "мине", "минам", "минами"})

_FILLERS = frozenset({"шт", "штук", "штуки", "штука", "x", "х"})
_FILLER_STEMS = ("ящик",)    # «3 ящика зелени», «напалми 4 ящика» — число то же, что и без ящиков
_MAX_QTY = 100_000
_MAX_NICK = 64

_TOKEN = re.compile(r"\d+|[^\W\d_]+")
_PAREN = re.compile(r"\([^)]*\)|\[[^\]]*\]")
_SEGMENT_SPLIT = re.compile(r"[,;\n]+")
_MARKDOWN = re.compile(r"\*+|__|~~|`+|\|\||[\u200b\u200c\u200d\ufeff]")
_DISCORD_TOKENS = re.compile(r"<(?:@[!&]?|#)\d+>|<a?:\w+:\d+>")
_LIST_MARK = re.compile(r"^\s*(?:\d{1,2}[.)]|[-–—•>])\s+")
_NICK_LABEL = re.compile(r"^(?:ник(?:нейм)?|nick(?:name)?)\s*[:=\-–—]\s*", re.IGNORECASE)
_BUFF_CHUNK = re.compile(r"^(?:\d+[xх×]?|[^\W\d_]+)$")


def _is_filler(word: str) -> bool:
    w = word.casefold().replace("ё", "е")
    return w in _FILLERS or w.startswith(_FILLER_STEMS)


def _distance(a: str, b: str) -> int:
    """Расстояние Дамерау — Левенштейна: вставка, пропуск, замена и перестановка соседних букв."""
    prev2: list[int] | None = None
    prev = list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        cur = [i] + [0] * len(b)
        for j in range(1, len(b) + 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (a[i - 1] != b[j - 1]))
            if prev2 is not None and i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                cur[j] = min(cur[j], prev2[j - 2] + 1)
        prev2, prev = prev, cur
    return prev[-1]


_NOT_BUFFS = ("топор", "тополь")     # настоящие слова, похожие на «топот»


def _fuzzy_key(w: str) -> str | None:
    """Слово с опечаткой («тапоты», «напалн», «смкои») → баф, если оно почти совпало с одной основой.

    Сравнивается начало слова с основой (окончания не важны). Короткие основы («шиз», «хил») и слова
    короче четырёх букв не угадываем — слишком много случайных совпадений."""
    if len(w) < 4 or w.startswith(_NOT_BUFFS):
        return None
    best: tuple[int, str] | None = None
    tie = False
    for key, stems in BUFF_STEMS.items():
        for stem in stems:
            if len(stem) < 4:
                continue
            limit = 1 if len(stem) <= 5 else 2
            d = min(_distance(w[:n], stem) for n in range(max(len(stem) - 1, 1), len(stem) + 2))
            if d > limit:
                continue
            if best is None or d < best[0]:
                best, tie = (d, key), False
            elif d == best[0] and key != best[1]:
                tie = True
    return best[1] if best is not None and not tie else None


def buff_key(word: str) -> str | None:
    """Название баффа из слова игрока («топотов» → topot) или None. Пара опечаток прощается."""
    w = word.casefold().replace("ё", "е")
    for key, stems in BUFF_STEMS.items():
        if w.startswith(stems):
            return key
    if w in _MINE_FORMS:
        return "mine"
    return _fuzzy_key(w) if w.isalpha() else None


@dataclass
class Parsed:
    nick: str | None
    counts: dict[str, int] = field(default_factory=dict)
    unrecognized: list[str] = field(default_factory=list)

    @property
    def status(self) -> str:
        if not self.counts:
            return "unparsed"
        return "partial" if self.unrecognized else "ok"


def _clean_line(line: str) -> str:
    line = _DISCORD_TOKENS.sub(" ", line)
    line = _MARKDOWN.sub("", line)
    line = _LIST_MARK.sub("", line)
    return " ".join(line.split())


def _add(counts: dict[str, int], unknown: list[str], qty: int, word: str) -> None:
    key = buff_key(word)
    if key is not None and qty == 0:
        return
    if key is None or qty > _MAX_QTY:
        unknown.append(f"{qty} {word}")
        return
    counts[key] = counts.get(key, 0) + qty


def _parse_segment(tokens: list[str], counts: dict[str, int], unknown: list[str]) -> None:
    # Лишние слова в начале («бафы», «хочу») не мешают.
    start = 0
    while start < len(tokens) and not tokens[start].isdigit() and buff_key(tokens[start]) is None:
        start += 1
    tokens = tokens[start:]
    if not tokens:
        return
    i = 0
    if tokens[0].isdigit():                       # «100 топотов»
        while i < len(tokens):
            if not tokens[i].isdigit():
                i += 1
                continue
            qty, j = int(tokens[i]), i + 1
            while j < len(tokens) and _is_filler(tokens[j]):
                j += 1
            if j < len(tokens) and not tokens[j].isdigit():
                _add(counts, unknown, qty, tokens[j])
                i = j + 1
            else:
                unknown.append(tokens[i])
                i += 1
    else:                                         # «топоты 100»
        while i < len(tokens):
            tok, j = tokens[i], i + 1
            if tok.isdigit():
                unknown.append(tok)
                i += 1
                continue
            while j < len(tokens) and _is_filler(tokens[j]):
                j += 1
            if buff_key(tok) is not None:
                if j < len(tokens) and tokens[j].isdigit():
                    _add(counts, unknown, int(tokens[j]), tok)
                    i = j + 1
                    continue
                unknown.append(tok)               # баф назван, а числа нет
            i += 1


def _parse_body(text: str) -> tuple[dict[str, int], list[str]]:
    counts: dict[str, int] = {}
    unknown: list[str] = []
    for segment in _SEGMENT_SPLIT.split(_PAREN.sub(" ", text)):
        tokens = _TOKEN.findall(segment)
        if tokens:
            _parse_segment(tokens, counts, unknown)
    return counts, unknown


def _is_buff_line(line: str) -> bool:
    """Первая строка — это уже список бафов (ник забыли), а не ник вроде «Шиза2008»."""
    chunks = [c.strip(".:!?-–—") for c in re.split(r"[\s,;]+", line)]
    chunks = [c for c in chunks if c]
    if not chunks or not all(_BUFF_CHUNK.match(c) for c in chunks):
        return False
    return bool(_parse_body(line)[0])


def _split_nick_and_buffs(line: str) -> tuple[str, str] | None:
    """«Danilka_suhav 100 топотов, 3 напалма» → («Danilka_suhav», «100 топотов, 3 напалма»).

    Ник — всё до первого отдельного числа, дальше обязано разбираться как список бафов.
    Иначе первая строка остаётся ником целиком («Игрок 2008» и подобные)."""
    words = line.split()
    for i in range(1, len(words)):
        if words[i].isdigit():
            if _parse_body(" ".join(words[i:]))[0]:
                return " ".join(words[:i]), " ".join(words[i:])
            return None
    return None


def parse_message(text: str) -> Parsed:
    """Ник — первая строка, остальное — «число + баф». Ничего не нашлось — status 'unparsed'."""
    lines = [ln for ln in (_clean_line(x) for x in str(text or "").splitlines()) if ln]
    if not lines:
        return Parsed(None)
    nick: str | None = None
    body = lines
    if not _is_buff_line(lines[0]):
        first = _NICK_LABEL.sub("", lines[0])
        inline = _split_nick_and_buffs(first)
        if inline is not None:                       # ник и бафы написали в одну строку
            first, body = inline[0], [inline[1], *lines[1:]]
        else:
            body = lines[1:]
        nick = first.strip(" ,;:")[:_MAX_NICK] or None
    counts, unknown = _parse_body("\n".join(body))
    return Parsed(nick, counts, unknown)


# ------------------------------------------------------------------- время --

ISSUE_DAY_START_HOUR = 5     # «день выдачи» меняется в 05:00 МСК: выдача после полуночи ещё идёт на вчерашнее КВ


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(moment: datetime) -> datetime:
    return moment.replace(tzinfo=timezone.utc) if moment.tzinfo is None else moment


def _iso(moment: datetime) -> str:
    return _aware(moment).astimezone(timezone.utc).isoformat(timespec="microseconds")


def msk_date(moment: datetime) -> str:
    return _aware(moment).astimezone(MSK).date().isoformat()


def issue_day_for(moment: datetime) -> str:
    """День выдачи, в который попадает галочка. До 05:00 по Москве это ещё вчерашний день."""
    return (_aware(moment).astimezone(MSK) - timedelta(hours=ISSUE_DAY_START_HOUR)).date().isoformat()


def _fmt_time(iso: str | None) -> str:
    try:
        return datetime.fromisoformat(str(iso)).astimezone(MSK).strftime("%H:%M")
    except (TypeError, ValueError):
        return ""


def _fmt_dm(iso: str | None) -> str:
    try:
        return datetime.fromisoformat(str(iso)).astimezone(MSK).strftime("%d.%m")
    except (TypeError, ValueError):
        return ""


# ---------------------------------------------------------------------- база --

_ISSUED_DDL = """CREATE TABLE IF NOT EXISTS buff_issued(
    id INTEGER PRIMARY KEY,
    buff_day TEXT NOT NULL,
    nick_key TEXT NOT NULL,
    player_id INTEGER,
    nick TEXT NOT NULL,
    counts_json TEXT NOT NULL CHECK(json_valid(counts_json)),
    total INTEGER NOT NULL,
    issued_at TEXT NOT NULL)"""


def _migrate_issued(con: sqlite3.Connection) -> None:
    """В первой версии у buff_issued было UNIQUE(buff_day, nick_key): две выдачи одному нику за
    день не помещались. Пересобираем таблицу без этого ограничения, данные сохраняются."""
    def has_unique() -> bool:
        row = con.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='buff_issued'").fetchone()
        return row is not None and "UNIQUE(buff_day,nick_key)" in "".join(str(row[0]).split())

    if not has_unique():
        return
    con.execute("BEGIN IMMEDIATE")
    try:
        if has_unique():      # другой процесс мог успеть раньше
            con.execute("DROP INDEX IF EXISTS idx_buff_issued_day")
            con.execute("ALTER TABLE buff_issued RENAME TO buff_issued_old")
            con.execute(_ISSUED_DDL)
            con.execute("""INSERT INTO buff_issued(id,buff_day,nick_key,player_id,nick,counts_json,total,issued_at)
                SELECT id,buff_day,nick_key,player_id,nick,counts_json,total,issued_at FROM buff_issued_old""")
            con.execute("DROP TABLE buff_issued_old")
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise


def _schema(con: sqlite3.Connection) -> None:
    con.executescript("""
        CREATE TABLE IF NOT EXISTS buff_settings(
            guild_id INTEGER PRIMARY KEY,
            channel_id INTEGER NOT NULL,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        -- Каждое сообщение канала (buff_day — календарная дата сообщения по Москве). Доска
        -- собирается из них на лету, поэтому правка и удаление сообщения в Discord сразу видны.
        CREATE TABLE IF NOT EXISTS buff_messages(
            message_id INTEGER PRIMARY KEY,
            guild_id INTEGER,
            channel_id INTEGER NOT NULL,
            author_id INTEGER,
            author_name TEXT NOT NULL DEFAULT '',
            buff_day TEXT NOT NULL,
            created_at TEXT NOT NULL,
            edited_at TEXT,
            nick TEXT NOT NULL DEFAULT '',
            counts_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(counts_json)),
            unrecognized TEXT NOT NULL DEFAULT '',
            raw_text TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL CHECK(status IN ('ok','partial','unparsed')),
            deleted INTEGER NOT NULL DEFAULT 0 CHECK(deleted IN (0,1)),
            updated_at TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_buff_messages_created ON buff_messages(created_at);
    """)
    con.execute(_ISSUED_DDL)       # история выдачи: одна строка = одна выдача игроку
    _migrate_issued(con)
    con.executescript("""
        CREATE INDEX IF NOT EXISTS idx_buff_issued_day ON buff_issued(buff_day);
        CREATE INDEX IF NOT EXISTS idx_buff_issued_at ON buff_issued(issued_at);
    """)


def configured_channels(db_path: Any) -> dict[int, int]:
    """guild_id → id канала бафов."""
    import player_store
    player_store.ensure_schema(db_path)
    with closing(player_store.connect(db_path)) as con:
        _schema(con)
        return {int(r[0]): int(r[1]) for r in con.execute("SELECT guild_id,channel_id FROM buff_settings")}


def set_channel(db_path: Any, guild_id: int, channel_id: int) -> None:
    import player_store
    player_store.ensure_schema(db_path)
    with closing(player_store.connect(db_path)) as con:
        _schema(con)
        con.execute("""INSERT INTO buff_settings(guild_id,channel_id) VALUES(?,?)
            ON CONFLICT(guild_id) DO UPDATE SET channel_id=excluded.channel_id,
            updated_at=CURRENT_TIMESTAMP""", (int(guild_id), int(channel_id)))


def clear_channel(db_path: Any, guild_id: int) -> None:
    import player_store
    player_store.ensure_schema(db_path)
    with closing(player_store.connect(db_path)) as con:
        _schema(con)
        con.execute("DELETE FROM buff_settings WHERE guild_id=?", (int(guild_id),))


def _bound_nick(con: sqlite3.Connection, author_id: Any) -> str | None:
    """Ник игрока по привязке его Discord-аккаунта (если в сообщении забыли ник)."""
    if not author_id:
        return None
    row = con.execute("""SELECT p.canonical_nick FROM discord_bindings d
        JOIN players p ON p.id=d.player_id WHERE d.discord_id=?""", (str(author_id),)).fetchone()
    return str(row[0]) if row else None


def _ingest_one(con: sqlite3.Connection, rec: dict[str, Any]) -> str:
    text = str(rec.get("content") or "")
    if not text.strip():
        return "skipped"
    message_id = int(rec["message_id"])
    existing = con.execute("""SELECT raw_text,deleted,nick,counts_json,unrecognized,status
        FROM buff_messages WHERE message_id=?""", (message_id,)).fetchone()
    parsed = parse_message(text)
    nick = parsed.nick
    if not nick and parsed.counts:
        nick = _bound_nick(con, rec.get("author_id")) or str(rec.get("author_name") or "").strip()
    counts_json = json.dumps(parsed.counts, ensure_ascii=False, sort_keys=True)
    if existing is not None and existing["raw_text"] == text and not existing["deleted"]:
        # Повторное чтение истории не «будит» таблицу. Но если парсер стал понимать больше (новые
        # названия, прощение опечаток), старые сообщения разбираются заново — заявка и галочка те же.
        if (existing["nick"], existing["counts_json"], existing["unrecognized"], existing["status"]) == (
                (nick or "")[:_MAX_NICK], counts_json, ", ".join(parsed.unrecognized), parsed.status):
            return "same"
        con.execute("""UPDATE buff_messages SET nick=?,counts_json=?,unrecognized=?,status=?,updated_at=?
            WHERE message_id=?""", ((nick or "")[:_MAX_NICK], counts_json, ", ".join(parsed.unrecognized),
                                    parsed.status, _iso(_now()), message_id))
        return parsed.status
    now = _iso(_now())
    created = rec["created_at"]
    edited = rec.get("edited_at")
    con.execute("""INSERT INTO buff_messages(message_id,guild_id,channel_id,author_id,author_name,
            buff_day,created_at,edited_at,nick,counts_json,unrecognized,raw_text,status,deleted,updated_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,0,?)
        ON CONFLICT(message_id) DO UPDATE SET edited_at=excluded.edited_at,
            author_name=excluded.author_name, nick=excluded.nick, counts_json=excluded.counts_json,
            unrecognized=excluded.unrecognized, raw_text=excluded.raw_text, status=excluded.status,
            deleted=0, updated_at=excluded.updated_at""",
                (message_id, rec.get("guild_id"), int(rec["channel_id"]), rec.get("author_id"),
                 str(rec.get("author_name") or ""), msk_date(created), _iso(created),
                 _iso(edited) if edited else None, (nick or "")[:_MAX_NICK], counts_json,
                 ", ".join(parsed.unrecognized), text, parsed.status, now))
    return parsed.status


def ingest_messages(db_path: Any, records: Iterable[dict[str, Any]]) -> dict[str, int]:
    """Сохранить сообщения канала. Идемпотентно: повтор того же текста ничего не меняет."""
    import player_store
    player_store.ensure_schema(db_path)
    stats = {"ok": 0, "partial": 0, "unparsed": 0, "same": 0, "skipped": 0}
    with closing(player_store.connect(db_path)) as con:
        _schema(con)
        con.execute("BEGIN IMMEDIATE")
        try:
            for rec in records:
                stats[_ingest_one(con, rec)] += 1
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
    return stats


def mark_deleted(db_path: Any, message_ids: Iterable[int]) -> int:
    import player_store
    ids = [int(x) for x in message_ids]
    if not ids:
        return 0
    player_store.ensure_schema(db_path)
    with closing(player_store.connect(db_path)) as con:
        _schema(con)
        con.execute("BEGIN IMMEDIATE")
        try:
            changed = 0
            for message_id in ids:
                changed += con.execute(
                    "UPDATE buff_messages SET deleted=1,updated_at=? WHERE message_id=? AND deleted=0",
                    (_iso(_now()), message_id)).rowcount
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
    return changed


def reconcile_deleted(db_path: Any, channel_id: int, seen_ids: Iterable[int], window_start: int = 0) -> int:
    """Убрать заявки сообщений, которых больше нет в канале (удалили, пока бот был выключен).

    seen_ids — всё, что бот сейчас видит в канале; window_start — id самого старого прочитанного
    сообщения (0, если дошли до начала канала). Сообщения старше окна не трогаем."""
    import player_store
    seen = {int(x) for x in seen_ids}
    if not seen:
        return 0       # пустой ответ — скорее нет прав на историю, чем пустой канал: ничего не удаляем
    player_store.ensure_schema(db_path)
    with closing(player_store.connect(db_path)) as con:
        _schema(con)
        known = [int(r[0]) for r in con.execute(
            "SELECT message_id FROM buff_messages WHERE channel_id=? AND deleted=0 AND message_id>=?",
            (int(channel_id), int(window_start)))]
    return mark_deleted(db_path, [m for m in known if m not in seen])


# ----------------------------------------------------------- доска / история --

def _identity(admin: Any, con: sqlite3.Connection, cache: dict[str, tuple], nick: str) -> tuple:
    """(player_id|None, как показывать, ключ строки). Ник сопоставляется с базой игроков
    при каждом показе, поэтому алиасы и новые игроки подхватываются задним числом."""
    hit = cache.get(nick)
    if hit is not None:
        return hit
    store = admin.player_store
    match = store.match_player_in_connection(con, nick)
    result: tuple | None = None
    if match.player_id is not None:
        row = con.execute("SELECT canonical_nick FROM players WHERE id=?", (match.player_id,)).fetchone()
        if row is not None:
            result = (int(match.player_id), str(row[0]), f"p{int(match.player_id)}")
    if result is None:
        result = (None, nick, "n:" + (store._norm(nick) or nick.casefold()))
    cache[nick] = result
    return result


def _sum_counts(items: Iterable[dict[str, int]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for counts in items:
        for key, value in counts.items():
            out[key] = out.get(key, 0) + int(value)
    return out


def _total(counts: dict[str, int]) -> int:
    return sum(int(v) for v in counts.values())


UNPARSED_SHOWN = 30


def _collect(admin: Any, con: sqlite3.Connection, now: datetime):
    """Доска — статичная таблица: у каждого игрока его последняя заявка. Она висит, пока сообщение
    есть в канале; удалили сообщение в Discord — заявки нет (история выдачи при этом остаётся).

    Галочка относится только к текущему дню выдачи: заявка «выдана», если после неё (и после её
    правки) сегодня была выдача. Со сменой дня (05:00 МСК) галочки сами сбрасываются, а игроки
    остаются на доске — в следующий КВ их можно отметить снова. Сообщение после выдачи — новая
    заявка, она снова без галочки. Читаются только сообщения текущего канала бафов."""
    today = issue_day_for(now)
    in_channel = "channel_id IN (SELECT channel_id FROM buff_settings)"
    cache: dict[str, tuple] = {}
    requests: dict[str, dict[str, Any]] = {}
    # достаточно последнего сообщения под каждым написанием ника: старые перекрыты более новыми
    for m in con.execute(f"""SELECT * FROM buff_messages WHERE message_id IN (
                SELECT MAX(message_id) FROM buff_messages
                WHERE deleted=0 AND status!='unparsed' AND {in_channel} GROUP BY lower(nick))
            ORDER BY created_at, message_id"""):
        pid, display, key = _identity(admin, con, cache, m["nick"])
        requests[key] = {
            "key": key, "pid": pid, "nick": display, "typed": m["nick"],
            "counts": json.loads(m["counts_json"]), "unrecognized": m["unrecognized"],
            "created_at": m["created_at"], "requested_at": max(m["created_at"], m["edited_at"] or ""),
            "author": m["author_name"], "issued": False, "issue_id": None, "issued_at": None, "earlier": {},
        }
    # выдачи только сегодняшнего дня: вчерашние — уже история и галочку не держат
    today_events: dict[str, list[dict[str, Any]]] = {}
    if requests:
        for i in con.execute("SELECT * FROM buff_issued WHERE buff_day=? ORDER BY issued_at, id", (today,)):
            key = _identity(admin, con, cache, i["nick"])[2]
            today_events.setdefault(key, []).append(
                {"id": int(i["id"]), "at": i["issued_at"], "counts": json.loads(i["counts_json"])})

    rows: list[dict[str, Any]] = []
    for key, req in requests.items():
        events = today_events.get(key, [])
        last = events[-1] if events else None
        done = last is not None and last["at"] > req["requested_at"]
        if done:
            req.update(issued=True, issue_id=last["id"], issued_at=last["at"])
            events = events[:-1]
        req["earlier"] = _sum_counts(e["counts"] for e in events)
        rows.append(req)
    rows.sort(key=lambda r: (r["nick"].casefold(), r["key"]))

    unparsed = list(con.execute(
        f"""SELECT * FROM buff_messages WHERE deleted=0 AND status='unparsed' AND {in_channel}
            ORDER BY created_at DESC, message_id DESC LIMIT {UNPARSED_SHOWN}"""))
    unparsed_total = con.execute(
        f"SELECT COUNT(*) FROM buff_messages WHERE deleted=0 AND status='unparsed' AND {in_channel}").fetchone()[0]
    return rows, unparsed, unparsed_total, today


def _version(con: sqlite3.Connection, now: datetime) -> str:
    """Отпечаток доски: меняется при новом/изменённом/удалённом сообщении, любой галочке и
    при смене дня выдачи (тогда выданные вчера заявки уходят с доски)."""
    # число удалённых считаем отдельно: удаление не должно зависеть от точности и хода часов
    m = con.execute("SELECT COUNT(*),COALESCE(SUM(deleted),0),COALESCE(MAX(updated_at),'') FROM buff_messages").fetchone()
    i = con.execute("SELECT COUNT(*),COALESCE(MAX(id),0) FROM buff_issued").fetchone()
    return f"{issue_day_for(now)}|{m[0]}|{m[1]}|{m[2]}|{i[0]}|{i[1]}"


CSS = r"""
.bt td.bc,.bt th.bh{width:66px;min-width:56px}.bt td.bc{font-weight:650}.bt td.bc .z{color:var(--dim);opacity:.4;font-weight:400}
.bt tfoot td{border-top:1px solid var(--line);border-bottom:0;background:var(--soft);font-weight:750}.bt tfoot tr.rest td{background:transparent;border-top:0;color:var(--dim);font-weight:650}
.bt td.nick .who{font-weight:650}.bt td.nick .typed,.bt td.nick .when{color:var(--dim);font-size:11px;margin-left:6px;font-weight:400}
.bt td.tk,.bt th.tk{width:56px;text-align:center;position:sticky;right:0;background:var(--panel);border-left:1px solid var(--line)}
.bt th.tk{background:var(--soft);z-index:3}.bt tfoot td.tk{background:var(--soft)}.bt tfoot tr.rest td.tk{background:var(--panel)}
.bt tbody tr:hover td.tk{background:var(--blue2)}.bt tr.done td:not(.tk){color:var(--dim)}
button.tick{width:30px;min-height:28px;height:28px;padding:0;border-radius:8px;color:var(--dim);background:transparent;font-size:15px;line-height:1;opacity:.55}
button.tick:hover{opacity:1;border-color:var(--green);color:var(--green);background:transparent}button.tick.on{background:var(--green);border-color:var(--green);color:#fff;opacity:1}
button.tick:disabled{opacity:.3}form.tickform{margin:0}
.bdays{margin-top:2px;font-size:11px;font-weight:400;white-space:normal;color:var(--dim);line-height:1.5}.bdays span{margin-right:8px;display:inline-block}
.bt td.nick.wrap2{white-space:normal;max-width:none}.rawtext{white-space:pre-wrap;text-align:left;font-family:inherit;margin:0;max-width:520px;color:var(--dim)}
@media(max-width:760px){.bt td.nick,.bt th.nick{position:sticky;left:0;z-index:1;background:var(--panel);max-width:170px}
.bt th.nick{background:var(--soft);z-index:3}.bt tfoot td.nick{background:var(--soft)}.bt tfoot tr.rest td.nick{background:var(--panel)}
.bt tbody tr:hover td.nick{background:var(--blue2)}}
.hintbox{padding:14px 16px;border:1px dashed var(--line);border-radius:10px;color:var(--dim);margin:10px 0}.hintbox code{background:var(--soft);padding:1px 5px;border-radius:5px;color:var(--text)}
.bsearch{margin:2px 0 10px}
.metrics.bcat{grid-template-columns:repeat(7,1fr);margin-top:-8px}@media(max-width:760px){.metrics.bcat{grid-template-columns:repeat(2,1fr)}}
.bt tfoot tr.totals td{border-top:1px solid var(--line);border-bottom:0;background:var(--soft);font-weight:750}
.bt a.cp{cursor:copy;color:inherit;text-decoration:none}.bt a.cp:hover{color:var(--blue)}.bt a.cp:focus-visible{outline:1px solid var(--blue);outline-offset:2px;border-radius:3px}
.bt a.cp.copied{color:var(--green)}
.bpanel{width:fit-content;max-width:100%;overflow:visible}
.bt.bnow thead th{top:50px;box-shadow:0 1px 0 var(--line)}
.bt.bnow{width:auto}.bt.bnow td.nick,.bt.bnow th.nick{width:auto;min-width:240px;max-width:420px}
.bt.bnow td.bc,.bt.bnow th.bh{width:62px;min-width:56px}
@media(max-width:760px){.bpanel{width:100%;overflow-x:auto}.bt.bnow thead th{top:0}.bt.bnow td.nick,.bt.bnow th.nick{min-width:0}}
"""

JS = r"""<script>
(function(){
var cur=__VER__, VURL=__VURL__, busy=false, N=__N__;
function cell(sel){return document.querySelectorAll(sel)}
function recalc(){
  var tot=[],rest=[],i,done=0,all=0;for(i=0;i<N;i++){tot.push(0);rest.push(0)}
  cell('tr.brow').forEach(function(r){var d=r.classList.contains('done');all++;if(d)done++;
    r.querySelectorAll('td.bc').forEach(function(c,k){var n=+c.getAttribute('data-n')||0;tot[k]+=n;if(!d)rest[k]+=n})});
  cell('#brow-tot td.bc').forEach(function(c,k){c.textContent=tot[k]||'·'});
  cell('#brow-rest td.bc').forEach(function(c,k){c.textContent=rest[k]||'·'});
  var chip=document.getElementById('chip-done');if(chip)chip.textContent=done+' из '+all;
}
var flt=document.getElementById('flt');
function applyFilter(){var q=(flt.value||'').toLowerCase();
  cell('tr.brow').forEach(function(r){r.style.display=r.querySelector('td.nick').textContent.toLowerCase().indexOf(q)>-1?'':'none'})}
if(flt){try{flt.value=sessionStorage.getItem('buffs-flt')||''}catch(e){}applyFilter();   // поиск переживает автообновление
  flt.addEventListener('input',function(){try{sessionStorage.setItem('buffs-flt',flt.value)}catch(e){}applyFilter()})}
document.addEventListener('submit',function(e){
  var f=e.target;if(!f.classList||!f.classList.contains('tickform'))return;
  e.preventDefault();var b=f.querySelector('button');if(b.disabled)return;b.disabled=true;busy=true;
  fetch(f.action,{method:'POST',credentials:'same-origin',headers:{'Accept':'application/json'},body:new FormData(f)})
  .then(function(r){return r.json().then(function(j){return {ok:r.ok,j:j}})})
  .then(function(x){
    if(!x.ok||!x.j.ok)throw new Error((x.j&&x.j.error)||'Не удалось сохранить');
    if(x.j.before!==cur){location.reload();return}   // пока смотрели, что-то поменялось — перечитаем
    cur=x.j.after;var tr=f.closest('tr');
    tr.classList.toggle('done',x.j.issued);b.classList.toggle('on',x.j.issued);
    f.querySelector('[name=issued]').value=x.j.issued?'0':'1';
    b.title=x.j.issued?'Выдано сейчас · нажмите, чтобы снять отметку':'Отметить: бафы выданы';recalc();
  })
  .catch(function(err){alert(err.message||'Ошибка. Обновите страницу.')})
  .then(function(){b.disabled=false;busy=false});
});
function copyText(t){
  if(navigator.clipboard&&window.isSecureContext)return navigator.clipboard.writeText(t);
  return new Promise(function(ok,fail){var a=document.createElement('textarea');a.value=t;a.setAttribute('readonly','');
    a.style.cssText='position:fixed;top:0;left:0;opacity:0';document.body.appendChild(a);a.select();
    var done=false;try{done=document.execCommand('copy')}catch(e){}document.body.removeChild(a);done?ok():fail(new Error('copy'))})}
function copyNick(el){var t=el.getAttribute('data-nick')||'';if(!t)return;
  copyText(t).then(function(){el.classList.add('copied');clearTimeout(el._t);el._t=setTimeout(function(){el.classList.remove('copied')},1200)})
  .catch(function(){window.prompt('Скопируйте ник (Ctrl+C):',t)})}
document.addEventListener('click',function(e){var a=e.target.closest&&e.target.closest('a.cp');if(a){e.preventDefault();copyNick(a)}});
document.addEventListener('keydown',function(e){if(e.key!=='Enter'&&e.key!==' ')return;var a=e.target.closest&&e.target.closest('a.cp');if(a){e.preventDefault();copyNick(a)}});
function poll(){if(document.hidden||busy)return;
  fetch(VURL,{credentials:'same-origin',cache:'no-store'}).then(function(r){return r.ok?r.json():null})
  .then(function(j){if(j&&j.v&&j.v!==cur)location.reload()}).catch(function(){})}
setInterval(poll,6000);document.addEventListener('visibilitychange',function(){if(!document.hidden)poll()});
})();
</script>"""


def _buff_cells(counts: dict[str, int]) -> str:
    out = []
    for key in BUFF_KEYS:
        n = int(counts.get(key) or 0)
        out.append(f"<td class='bc' data-n='{n}'>{n if n else '<span class=z>·</span>'}</td>")
    return "".join(out)


# «12 убийц», «70 топотов» — для подсказок (названия колонок стоят в именительном падеже)
BUFF_GENITIVE = {"shiza": "шиз", "topot": "топотов", "killer": "убийц", "heal": "хилок",
                 "napalm": "напалмов", "smoke": "смоков", "mine": "мин"}


def _counts_text(counts: dict[str, int]) -> str:
    return ", ".join(f"{int(counts[k])} {BUFF_GENITIVE[k]}" for k in BUFF_KEYS if int(counts.get(k) or 0) > 0)


def _row_html(admin: Any, row: dict[str, Any]) -> str:
    esc = admin.esc
    # ник — не ссылка на профиль: клик копирует его в буфер обмена (см. JS ниже)
    name = (f"<a class='cp' role='button' tabindex='0' data-nick='{esc(row['nick'])}' "
            f"title='Нажмите, чтобы скопировать ник'>{esc(row['nick'])}</a>")
    notes = ""
    if row["typed"] and row["typed"].casefold() != row["nick"].casefold():
        notes += f"<span class='typed'>({esc(row['typed'])})</span>"
    if row["pid"] is None:
        notes += " <span class='source-badge' title='Такого ника нет в базе игроков — проверьте написание'>нет в базе</span>"
    if row["earlier"]:
        notes += (f" <span class='source-badge' title='Сегодня уже выдано: {esc(_counts_text(row['earlier']))}'>"
                  "уже выдавали</span>")
    if row["unrecognized"]:
        notes += (f" <span class='flag' title='Не распознано: {esc(row['unrecognized'])}' "
                  "style='cursor:help'>⚠</span>")
    tip = " · ".join(filter(None, [f"{_fmt_dm(row['created_at'])} {_fmt_time(row['created_at'])}".strip(), row["author"]]))
    if row["issued"]:
        title = f"Выдано {_fmt_time(row['issued_at'])} · нажмите, чтобы снять отметку"
    else:
        title = "Отметить: бафы выданы"
    button = (f"<form class='tickform' method='post' action='{url_for('buffs_toggle')}'>"
              f"<input type='hidden' name='key' value='{esc(row['key'])}'>"
              f"<input type='hidden' name='issued' value='{0 if row['issued'] else 1}'>"
              f"<button class='tick{' on' if row['issued'] else ''}' title='{esc(title)}' "
              "aria-label='Бафы выданы'>✓</button></form>")
    return (f"<tr class='brow{' done' if row['issued'] else ''}'>"
            f"<td class='nick' title='{esc(tip)}'><span class='who'>{name}</span>{notes}</td>"
            f"{_buff_cells(row['counts'])}<td class='tk'>{button}</td></tr>")


def _render_board(admin: Any) -> str:
    esc = admin.esc
    now = _now()
    with closing(admin.player_store.connect(admin.DB_PATH)) as con:
        _schema(con)
        rows, unparsed, unparsed_total, today = _collect(admin, con, now)
        version = _version(con, now)
        has_channel = bool(con.execute("SELECT 1 FROM buff_settings LIMIT 1").fetchone())

    done = sum(1 for r in rows if r["issued"])
    totals = {k: sum(int(r["counts"].get(k) or 0) for r in rows) for k in BUFF_KEYS}
    rest = {k: sum(int(r["counts"].get(k) or 0) for r in rows if not r["issued"]) for k in BUFF_KEYS}

    def foot(row_id: str, label: str, values: dict[str, int], css: str = "") -> str:
        cells = "".join(f"<td class='bc'>{values[k] or '·'}</td>" for k in BUFF_KEYS)
        return f"<tr id='{row_id}'{css}><td class='nick'>{label}</td>{cells}<td class='tk'></td></tr>"

    head = ("<th class='nick'>Ник</th>" + "".join(f"<th class='bh'>{esc(t)}</th>" for _, t in BUFFS)
            + "<th class='tk' title='Бафы выданы'>✓</th>")
    if rows:
        table = ("<input id='flt' class='bsearch' placeholder='Поиск по нику…' aria-label='Поиск игрока'>"
                 "<div class='panel bpanel'><table class='tight bt bnow'><thead><tr>" + head + "</tr></thead><tbody>"
                 + "".join(_row_html(admin, r) for r in rows) + "</tbody><tfoot>"
                 + foot("brow-tot", "Итого", totals) + foot("brow-rest", "Осталось выдать", rest, " class='rest'")
                 + "</tfoot></table></div>")
    elif has_channel:
        table = "<div class='hintbox'>Заявок на бафы нет. Новые сообщения из канала появятся здесь сами.</div>"
    else:
        table = ("<div class='hintbox'>Канал бафов не задан. В Discord выполните "
                 "<code>/buffs_channel</code> и выберите канал — бот прочитает его и покажет заявки здесь.</div>")

    bad = ""
    if unparsed:
        items = "".join(
            f"<tr><td class='dim'>{esc(_fmt_dm(m['created_at']))} {esc(_fmt_time(m['created_at']))}</td>"
            f"<td class='nick'>{esc(m['author_name'])}</td>"
            f"<td><pre class='rawtext'>{esc(m['raw_text'])}</pre></td></tr>" for m in unparsed)
        more = f", показаны последние {len(unparsed)}" if unparsed_total > len(unparsed) else ""
        bad = (f"<details><summary>Не распознано ({unparsed_total}{more})</summary><div class='panel'><table>"
               "<thead><tr><th>Когда</th><th class='nick'>Автор</th><th>Текст</th></tr></thead>"
               f"<tbody>{items}</tbody></table></div></details>")

    chips = ("<div class='chips'>"
             f"<span class='chip'>В таблице: {len(rows)}</span>"
             f"<span class='chip'>Выдано: <b id='chip-done'>{done} из {len(rows)}</b></span></div>")
    body = ("<div class='pagehead'><div><h1>Бафы</h1>"
            f"<div class='subtitle'>Выдача: {admin.fmt_date_ru(today)}</div></div>"
            f"<div class='actions'><a class='button' href='{url_for('buffs_history')}'>📜 История</a></div></div>"
            + chips + table + bad
            + JS.replace("__VER__", json.dumps(version)).replace("__VURL__", json.dumps(url_for("buffs_version")))
            .replace("__N__", str(len(BUFFS))))
    return admin.render("Бафы", body)


def _history_data(admin: Any, con: sqlite3.Connection):
    cache: dict[str, tuple] = {}
    entries = []
    for r in con.execute("SELECT * FROM buff_issued ORDER BY buff_day DESC, issued_at, id"):
        pid, display, key = _identity(admin, con, cache, r["nick"])
        if pid is None and r["player_id"]:
            row = con.execute("SELECT canonical_nick FROM players WHERE id=?", (r["player_id"],)).fetchone()
            if row is not None:
                pid, display, key = int(r["player_id"]), str(row[0]), f"p{int(r['player_id'])}"
        entries.append({"id": int(r["id"]), "day": r["buff_day"], "key": key, "pid": pid, "nick": display,
                        "counts": json.loads(r["counts_json"]), "at": r["issued_at"]})
    return entries


def _render_history(admin: Any, view: str) -> str:
    esc = admin.esc
    with closing(admin.player_store.connect(admin.DB_PATH)) as con:
        _schema(con)
        entries = _history_data(admin, con)
    grand = sum(_total(e["counts"]) for e in entries)
    days = sorted({e["day"] for e in entries}, reverse=True)
    players: dict[str, dict[str, Any]] = {}
    for e in entries:
        p = players.setdefault(e["key"], {"nick": e["nick"], "pid": e["pid"], "days": set(),
                                          "counts": {k: 0 for k in BUFF_KEYS}})
        p["days"].add(e["day"])
        for k in BUFF_KEYS:
            p["counts"][k] += int(e["counts"].get(k) or 0)
    by_buff = {k: sum(int(e["counts"].get(k) or 0) for e in entries) for k in BUFF_KEYS}
    metrics = ("<div class='metrics'>"
               f"<div class='metric'><label>Дней выдачи</label><b>{len(days)}</b></div>"
               f"<div class='metric'><label>Игроков</label><b>{len(players)}</b></div>"
               f"<div class='metric'><label>Выдач</label><b>{len(entries)}</b></div></div>"
               "<div class='metrics bcat'>"
               + "".join(f"<div class='metric'><label>{esc(title)}</label><b>{by_buff[key]}</b></div>"
                         for key, title in BUFFS) + "</div>")
    toggle = ("<div class='seg'>"
              + ("<b>По игрокам</b>" if view == "players" else f"<a href='{url_for('buffs_history')}'>По игрокам</a>")
              + ("<b>По дням</b>" if view == "days" else f"<a href='{url_for('buffs_history', view='days')}'>По дням</a>")
              + "</div>")
    th = "".join(f"<th>{esc(t)}</th>" for _, t in BUFFS)

    def counts_cells(counts: dict[str, int]) -> str:
        return "".join(f"<td>{int(counts.get(k) or 0) or '<span class=dim>·</span>'}</td>" for k in BUFF_KEYS)

    def totals_row(lead: str, spare: str, tail: str = "") -> str:
        cells = "".join(f"<td><b>{by_buff[k]}</b></td>" if by_buff[k] else "<td><span class=dim>·</span></td>"
                        for k in BUFF_KEYS)
        return f"<tr class='totals'><td class='nick grow'>{lead}</td>{spare}{cells}<td class='total'>{grand}</td>{tail}</tr>"

    def name(pid: Any, nick: str) -> str:
        return f"<a href='{url_for('player', player_id=pid)}'>{esc(nick)}</a>" if pid is not None else esc(nick)

    if not entries:
        table = ("<div class='hintbox'>История пока пустая. Она наполняется, когда на странице "
                 f"<a href='{url_for('buffs')}'>«Бафы»</a> ставят галочки «выдано».</div>")
    elif view == "days":
        parts = []
        for d in days:
            todays = [e for e in entries if e["day"] == d]
            parts.append(f"<tr class='squad-title'><td colspan='{len(BUFFS) + 3}'>{admin.fmt_date_ru(d)}"
                         f"<span>{len(todays)} выдач · {sum(_total(e['counts']) for e in todays)} бафов</span></td></tr>")
            for e in todays:
                confirm = f"Убрать из истории выдачу «{e['nick']}» за {admin.fmt_date_ru(d)}?"
                parts.append(
                    f"<tr><td class='nick grow'>{name(e['pid'], e['nick'])}"
                    f"<span class='typed dim' style='font-size:11px;margin-left:6px'>{esc(_fmt_time(e['at']))}</span></td>"
                    f"{counts_cells(e['counts'])}<td class='total'>{_total(e['counts'])}</td>"
                    f"<td class='act'><form method='post' action='{url_for('buffs_issue_delete', issued_id=e['id'])}' "
                    f"data-confirm='{esc(confirm)}'><button class='quiet' title='Убрать эту выдачу из истории'>✕</button>"
                    "</form></td></tr>")
        table = ("<div class='panel'><table class='tight'><thead><tr><th class='nick grow'>Ник</th>" + th
                 + "<th>Всего</th><th></th></tr></thead><tbody>" + "".join(parts) + "</tbody><tfoot>"
                 + totals_row("Итого за всё время", "", "<td></td>") + "</tfoot></table></div>")
    else:
        body_rows = []
        for p in sorted(players.values(), key=lambda x: (-_total(x["counts"]), x["nick"].casefold())):
            dates = "".join(f"<span>{esc(admin.fmt_date_ru(d))}</span>" for d in sorted(p["days"], reverse=True))
            body_rows.append(
                f"<tr><td class='nick grow wrap2'><span class='who'>{name(p['pid'], p['nick'])}</span>"
                f"<div class='bdays'>{dates}</div></td><td>{len(p['days'])}</td>{counts_cells(p['counts'])}"
                f"<td class='total'>{_total(p['counts'])}</td></tr>")
        table = ("<div class='panel'><table class='tight bt'><thead><tr><th class='nick grow'>Ник</th>"
                 "<th title='Сколько дней игрок получал бафы'>Дней</th>" + th + "<th>Всего</th></tr></thead><tbody>"
                 + "".join(body_rows) + "</tbody><tfoot>"
                 + totals_row("Итого за всё время", "<td></td>") + "</tfoot></table></div>")
    body = (f"<a class='back' href='{url_for('buffs')}'>← Бафы</a>"
            "<div class='pagehead'><div><h1>История бафов</h1>"
            "<div class='subtitle'>Сколько и каких бафов выдали каждому игроку и в какие дни</div></div></div>"
            + metrics + toggle + table)
    return admin.render("История бафов", body)


# ------------------------------------------------------------- админ-панель --

def install(admin: Any) -> None:
    if getattr(admin.app, "_buffs_installed", False):
        return
    admin.app._buffs_installed = True
    admin.PAGE = admin.PAGE.replace("</style>", CSS.replace("{", "{{").replace("}", "}}") + "\n</style>", 1)
    base_render = admin.render

    def render(title: str, body: str) -> str:
        page = base_render(title, body)
        active = "active" if request.path.startswith("/buffs") else ""
        link = f"<a class='{active}' href='{url_for('buffs')}'>Бафы</a>"
        logout = "<form method=\"post\" action=\"/logout\""
        if "<!--navx-->" in page:
            return page.replace("<!--navx-->", link, 1)
        if logout in page:
            return page.replace(logout, link + logout, 1)
        return page.replace("</nav></div></header>", link + "</nav></div></header>", 1)

    admin.render = render

    @admin.app.get("/buffs")
    def buffs():
        return _render_board(admin)

    @admin.app.get("/buffs/version")
    def buffs_version():
        with closing(admin.player_store.connect(admin.DB_PATH)) as con:
            _schema(con)
            return jsonify(v=_version(con, _now()))

    @admin.app.post("/buffs/toggle")
    def buffs_toggle():
        """Поставить (issued=1) или снять (issued=0) отметку «выдано». Действие задаёт состояние, а
        не переключает: устаревшая страница у второго человека не снимет чужую галочку."""
        key = request.form.get("key", "")
        want = request.form.get("issued", "1") != "0"
        wants_json = "application/json" in request.headers.get("Accept", "")

        def fail(message: str, status: int):
            if wants_json:
                return jsonify(ok=False, error=message), status
            return message, status

        now = _now()
        with closing(admin.player_store.connect(admin.DB_PATH)) as con:
            _schema(con)
            con.execute("BEGIN IMMEDIATE")
            try:
                before = _version(con, now)
                rows, _, _, today = _collect(admin, con, now)
                row = next((r for r in rows if r["key"] == key), None)
                if row is None:
                    con.execute("ROLLBACK")
                    return fail("Заявки уже нет — обновите страницу", 404)
                if want and not row["issued"]:
                    counts = {k: int(v) for k, v in row["counts"].items() if int(v) > 0}
                    if not counts:
                        con.execute("ROLLBACK")
                        return fail("В заявке нет бафов", 400)
                    # выдача должна быть позже заявки, даже если часы сервера отстают от Discord
                    at = max(now, datetime.fromisoformat(row["requested_at"]) + timedelta(microseconds=1))
                    con.execute("""INSERT INTO buff_issued(buff_day,nick_key,player_id,nick,counts_json,total,issued_at)
                        VALUES(?,?,?,?,?,?,?)""",
                                (today, key, row["pid"], row["nick"],
                                 json.dumps(counts, ensure_ascii=False, sort_keys=True), _total(counts), _iso(at)))
                elif not want and row["issued"]:
                    con.execute("DELETE FROM buff_issued WHERE id=?", (row["issue_id"],))
                after = _version(con, now)
                con.execute("COMMIT")
            except Exception:
                try:
                    con.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
        if wants_json:
            return jsonify(ok=True, issued=want, before=before, after=after)
        return redirect(url_for("buffs"), code=303)

    @admin.app.get("/buffs/history")
    def buffs_history():
        view = request.args.get("view", "players")
        return _render_history(admin, view if view in {"players", "days"} else "players")

    @admin.app.post("/buffs/issued/<int:issued_id>/delete")
    def buffs_issue_delete(issued_id: int):
        with closing(admin.player_store.connect(admin.DB_PATH)) as con:
            _schema(con)
            con.execute("DELETE FROM buff_issued WHERE id=?", (issued_id,))
        return redirect(url_for("buffs_history", view="days"), code=303)


# ------------------------------------------------------------------- Discord --

def install_runtime(runtime: Any) -> None:
    """Подписки на сообщения канала и команды /buffs_*. Вызывается, когда бот уже собран."""
    bot = getattr(runtime, "bot", None)
    if bot is None or discord is None or getattr(bot, "_buffs_installed", False):
        return
    bot._buffs_installed = True
    db_path = runtime.PLAYER_DB_PATH
    state: dict[str, Any] = {"channels": None, "caught_up": False}

    async def channels() -> dict[int, int]:
        if state["channels"] is None:
            state["channels"] = await asyncio.to_thread(configured_channels, db_path)
        return state["channels"]

    def record(message: Any) -> dict[str, Any]:
        author = message.author
        return {"message_id": message.id, "guild_id": message.guild.id if message.guild else None,
                "channel_id": message.channel.id, "author_id": author.id,
                "author_name": getattr(author, "display_name", None) or author.name,
                "created_at": message.created_at, "edited_at": message.edited_at,
                "content": message.content}

    async def store(records: list[dict[str, Any]]) -> dict[str, int]:
        return await asyncio.to_thread(ingest_messages, db_path, records)

    async def sync_channel(channel: Any, limit: int) -> tuple[list[dict[str, Any]], dict[str, int], int]:
        """Прочитать последние сообщения, сохранить заявки и убрать заявки удалённых сообщений."""
        records: list[dict[str, Any]] = []
        seen: set[int] = set()
        async for message in channel.history(limit=limit):
            seen.add(message.id)
            if not message.author.bot and message.content.strip():
                records.append(record(message))
        stats = await store(records)
        window = 0 if len(seen) < limit else min(seen)       # дошли до начала канала или нет
        removed = await asyncio.to_thread(reconcile_deleted, db_path, channel.id, seen, window)
        return records, stats, removed

    def summary(stats: dict[str, int], total: int, removed: int) -> str:
        return (f"Прочитано сообщений: **{total}**\n"
                f"· распознано: **{stats['ok'] + stats['partial']}**"
                + (f" (в {stats['partial']} часть строк не разобрана)" if stats["partial"] else "") + "\n"
                f"· не распознано: **{stats['unparsed']}**\n"
                f"· уже были в базе: **{stats['same']}**"
                + (f"\n· убрано заявок удалённых сообщений: **{removed}**" if removed else ""))

    @bot.listen("on_message")
    async def _buffs_on_message(message):
        if message.author.bot or message.guild is None:
            return
        if (await channels()).get(message.guild.id) != message.channel.id:
            return
        try:
            await store([record(message)])
        except Exception as exc:
            runtime.log(f"бафы: не удалось сохранить сообщение: {exc}", "err")

    @bot.listen("on_raw_message_edit")
    async def _buffs_on_edit(payload):
        if payload.guild_id is None or (await channels()).get(payload.guild_id) != payload.channel_id:
            return
        channel = bot.get_channel(payload.channel_id)
        if channel is None:
            return
        try:
            message = await channel.fetch_message(payload.message_id)
            if not message.author.bot:
                await store([record(message)])
        except discord.HTTPException:
            return
        except Exception as exc:
            runtime.log(f"бафы: не удалось обновить сообщение: {exc}", "err")

    @bot.listen("on_raw_message_delete")
    async def _buffs_on_delete(payload):
        if payload.guild_id is None or (await channels()).get(payload.guild_id) != payload.channel_id:
            return
        try:
            await asyncio.to_thread(mark_deleted, db_path, [payload.message_id])
        except Exception as exc:
            runtime.log(f"бафы: не удалось пометить удалённое: {exc}", "err")

    @bot.listen("on_raw_bulk_message_delete")
    async def _buffs_on_bulk_delete(payload):
        if payload.guild_id is None or (await channels()).get(payload.guild_id) != payload.channel_id:
            return
        try:
            await asyncio.to_thread(mark_deleted, db_path, payload.message_ids)
        except Exception as exc:
            runtime.log(f"бафы: не удалось пометить удалённые: {exc}", "err")

    @bot.listen("on_ready")
    async def _buffs_on_ready():
        """Подхватить сообщения, написанные, пока бот был выключен."""
        if state["caught_up"]:
            return
        state["caught_up"] = True
        for channel_id in (await channels()).values():
            channel = bot.get_channel(channel_id)
            if channel is None:
                continue
            mine = channel.permissions_for(channel.guild.me)
            if not (mine.view_channel and mine.read_message_history):
                runtime.log(f"бафы: нет права читать историю канала {channel_id}", "warn")
                continue
            try:
                records, stats, removed = await sync_channel(channel, 1000)
                runtime.log(f"бафы: догнал канал · новых {stats['ok'] + stats['partial'] + stats['unparsed']}"
                            f" · убрано удалённых {removed}", "ok")
            except Exception as exc:
                runtime.log(f"бафы: не удалось прочитать канал: {exc}", "warn")

    async def scan_channel(interaction, channel, limit: int, title: str) -> None:
        mine = channel.permissions_for(interaction.guild.me)
        if not (mine.view_channel and mine.read_message_history):
            await interaction.followup.send(embed=runtime.make_reply_embed(
                "⚠️  Нет доступа к истории",
                f"{channel.mention}: боту нужны права «Просмотр канала» и «Читать историю сообщений».",
                color=runtime.COLOR_LIVE), ephemeral=True)
            return
        try:
            records, stats, removed = await sync_channel(channel, limit)
        except discord.HTTPException as exc:
            await interaction.followup.send(embed=runtime.make_reply_embed(
                "❌  Не удалось прочитать канал", str(exc), color=runtime.COLOR_ERR), ephemeral=True)
            return
        await interaction.followup.send(embed=runtime.make_reply_embed(
            title, f"Канал: {channel.mention}\n{summary(stats, len(records), removed)}\n\n"
                   "Таблица — в админ-панели, раздел «Бафы».", color=runtime.COLOR_OK), ephemeral=True)

    @bot.tree.command(name="buffs_channel", description="Канал, из которого бот читает заявки на бафы")
    @app_commands.describe(channel="Канал с сообщениями: первая строка — ник, дальше «100 топотов, 3 убийцы»")
    @app_commands.guild_only()
    async def cmd_buffs_channel(interaction: discord.Interaction, channel: discord.TextChannel):
        if not await runtime.require_bot_admin(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        await asyncio.to_thread(set_channel, db_path, interaction.guild.id, channel.id)
        (await channels())[interaction.guild.id] = channel.id
        await scan_channel(interaction, channel, 500, "✅  Канал бафов задан")

    @bot.tree.command(name="buffs_sync", description="Перечитать сообщения канала бафов заново")
    @app_commands.describe(limit="Сколько последних сообщений прочитать (по умолчанию 500)")
    @app_commands.guild_only()
    async def cmd_buffs_sync(interaction: discord.Interaction, limit: app_commands.Range[int, 1, 5000] = 500):
        if not await runtime.require_bot_admin(interaction):
            return
        channel_id = (await channels()).get(interaction.guild.id)
        channel = interaction.guild.get_channel(channel_id) if channel_id else None
        if channel is None:
            await interaction.response.send_message(embed=runtime.make_reply_embed(
                "❌  Канал бафов не задан", "Сначала `/buffs_channel`.", color=runtime.COLOR_ERR), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        await scan_channel(interaction, channel, int(limit), "🔄  Канал перечитан")

    @bot.tree.command(name="buffs_off", description="Перестать читать канал бафов (история остаётся)")
    @app_commands.guild_only()
    async def cmd_buffs_off(interaction: discord.Interaction):
        if not await runtime.require_bot_admin(interaction):
            return
        await asyncio.to_thread(clear_channel, db_path, interaction.guild.id)
        (await channels()).pop(interaction.guild.id, None)
        await interaction.response.send_message(embed=runtime.make_reply_embed(
            "✅  Чтение канала бафов выключено",
            "Таблица заявок очистится, история выдачи остаётся в админ-панели. Включить снова — `/buffs_channel`.",
            color=runtime.COLOR_OK), ephemeral=True)
