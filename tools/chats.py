"""Чаты: личные сообщения бота с игроками клана → раздел «Чаты» в админ-панели.

Что умеет:

* бот сохраняет в базу (таблица ``dm_messages``) всё, что игроки пишут ему в личку, и всё, что бот
  пишет им сам (автоматические ЛС неявившимся, сообщения из панели);
* страница «Чаты» — все игроки по отрядам; у кого есть переписка, видно последнее сообщение и число
  непрочитанных; клик открывает переписку;
* в переписке можно написать игроку от имени бота — сообщение уходит ему в личку и попадает в историю;
* кнопка «Загрузить историю из Discord» подтягивает старые сообщения, написанные до включения чатов.

Принимать ЛС боту не нужны дополнительные права: ``Intents.default()`` уже включает ``dm_messages``,
а содержимое личных сообщений Discord отдаёт всегда.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Iterable

from flask import abort, jsonify, request, url_for

LOG = logging.getLogger(__name__)

MSK = timezone(timedelta(hours=3))        # Москва без перехода на летнее время
MAX_LEN = 2000                            # лимит Discord на одно сообщение
PAGE = 300                                # сколько последних сообщений показываем в переписке
SYNC_LIMIT = 200                          # сколько сообщений тянет «Загрузить историю из Discord»
SEND_TIMEOUT = 30                         # сколько секунд панель ждёт ответа Discord
SQUAD_IDS_SQL = "(squad_id BETWEEN 1 AND 6 OR squad_id=99)"      # отряды 1–6 и «Чемпионы»

# Мост «панель → бот». Заполняет install_runtime, когда бот уже собран; без бота (панель запущена
# отдельно) он пуст: историю смотреть можно, писать игрокам — нет.
_STATE: dict[str, Any] = {"bridge": None}


class ChatError(Exception):
    """Ошибка, текст которой можно показать человеку в панели."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


# ------------------------------------------------------------------- время --

def _utc_iso(moment: datetime | None = None) -> str:
    """Время в UTC в едином формате: строки такого вида правильно сортируются."""
    moment = moment or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f+00:00")


def _norm_iso(value: Any) -> str:
    """Строка времени из Discord (или datetime) → наш формат; при мусоре — «сейчас»."""
    if isinstance(value, datetime):
        return _utc_iso(value)
    try:
        return _utc_iso(datetime.fromisoformat(str(value).replace("Z", "+00:00")))
    except (TypeError, ValueError):
        return _utc_iso()


def _msk(iso: str | None) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(iso))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(MSK)


def _fmt_clock(iso: str | None) -> str:
    moment = _msk(iso)
    return moment.strftime("%H:%M") if moment else "—"


def _fmt_full(iso: str | None) -> str:
    moment = _msk(iso)
    return moment.strftime("%d.%m.%Y %H:%M:%S МСК") if moment else ""


def _fmt_when(iso: str | None) -> str:
    """Для списка: сегодня — «14:05», раньше — «21.11»."""
    moment = _msk(iso)
    if moment is None:
        return ""
    if moment.date() == datetime.now(MSK).date():
        return moment.strftime("%H:%M")
    return moment.strftime("%d.%m")


# -------------------------------------------------------------------- база --

def _schema(con: sqlite3.Connection) -> None:
    con.executescript("""
        -- Одна строка = одно личное сообщение. discord_id — собеседник бота (игрок), direction —
        -- кто написал: 'in' игрок боту, 'out' бот игроку. Номера сообщений Discord (snowflake)
        -- хранятся текстом: в JavaScript такие числа теряют точность.
        CREATE TABLE IF NOT EXISTS dm_messages(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id TEXT NOT NULL UNIQUE,
            discord_id TEXT NOT NULL,
            channel_id TEXT NOT NULL DEFAULT '',
            direction TEXT NOT NULL CHECK(direction IN ('in','out')),
            source TEXT NOT NULL DEFAULT 'discord' CHECK(source IN ('discord','panel','bot')),
            author_name TEXT NOT NULL DEFAULT '',
            content TEXT NOT NULL DEFAULT '',
            attachments_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(attachments_json)),
            created_at TEXT NOT NULL,
            edited_at TEXT,
            deleted INTEGER NOT NULL DEFAULT 0 CHECK(deleted IN (0,1)),
            seen INTEGER NOT NULL DEFAULT 0 CHECK(seen IN (0,1)),
            updated_at TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_dm_chat ON dm_messages(discord_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_dm_channel ON dm_messages(channel_id);
    """)


def _connect(db_path: Any) -> sqlite3.Connection:
    import player_store
    con = player_store.connect(db_path)
    _schema(con)
    return con


def message_fields(message: Any) -> dict[str, Any]:
    """Сообщение Discord → поля для базы (без привязки к discord.py: только атрибуты)."""
    attachments = []
    for item in getattr(message, "attachments", None) or []:
        attachments.append({"name": str(getattr(item, "filename", "") or "файл"),
                            "url": str(getattr(item, "url", "") or ""),
                            "type": str(getattr(item, "content_type", "") or ""),
                            "size": int(getattr(item, "size", 0) or 0)})
    content = str(getattr(message, "content", "") or "")
    stickers = [str(getattr(s, "name", "") or "стикер") for s in getattr(message, "stickers", None) or []]
    if stickers:
        content = (content + "\n" if content else "") + "[стикер: " + ", ".join(stickers) + "]"
    author = message.author
    return {"message_id": str(message.id),
            "author_name": str(getattr(author, "display_name", None) or getattr(author, "name", "") or ""),
            "content": content, "attachments": attachments,
            "created_at": getattr(message, "created_at", None)}


def store_message(db_path: Any, *, discord_id: Any, message_id: Any, direction: str, source: str,
                  content: str, created_at: Any = None, channel_id: Any = "", author_name: str = "",
                  attachments: list[dict[str, Any]] | None = None, seen: bool = False) -> bool:
    """Сохранить сообщение. Повтор того же сообщения Discord ничего не дублирует (вернёт False).

    Сообщение, которое отправила панель, могло прийти в базу раньше от слушателя ``on_message`` как
    «бот»: тогда метка источника уточняется до «панель»."""
    now = _utc_iso()
    with closing(_connect(db_path)) as con:
        cur = con.execute(
            """INSERT INTO dm_messages(message_id,discord_id,channel_id,direction,source,author_name,content,
                   attachments_json,created_at,seen,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(message_id) DO NOTHING""",
            (str(message_id), str(discord_id), str(channel_id or ""), direction, source, author_name or "",
             content or "", json.dumps(attachments or [], ensure_ascii=False), _norm_iso(created_at),
             1 if (seen or direction == "out") else 0, now))
        created = cur.rowcount == 1
        if not created and source == "panel":
            con.execute("UPDATE dm_messages SET source='panel',updated_at=? WHERE message_id=? AND source<>'panel'",
                        (now, str(message_id)))
        return created


def apply_edit(db_path: Any, message_id: Any, content: str, edited_at: Any = None) -> bool:
    """Игрок поправил сообщение. Обновление без смены текста (раскрылась ссылка и т. п.) не в счёт."""
    now = _utc_iso()
    with closing(_connect(db_path)) as con:
        cur = con.execute("UPDATE dm_messages SET content=?,edited_at=?,updated_at=? WHERE message_id=? AND content<>?",
                          (content or "", _norm_iso(edited_at), now, str(message_id), content or ""))
        return cur.rowcount > 0


def mark_deleted(db_path: Any, message_ids: Iterable[Any]) -> int:
    """Сообщение удалили в Discord: текст остаётся в истории, строка помечается «удалено»."""
    now = _utc_iso()
    with closing(_connect(db_path)) as con:
        count = 0
        for mid in message_ids:
            count += con.execute("UPDATE dm_messages SET deleted=1,updated_at=? WHERE message_id=? AND deleted=0",
                                 (now, str(mid))).rowcount
        return count


def channel_user(db_path: Any, channel_id: Any) -> int | None:
    """Кто собеседник бота в этом личном канале (если мы уже переписывались)."""
    with closing(_connect(db_path)) as con:
        row = con.execute("SELECT discord_id FROM dm_messages WHERE channel_id=? LIMIT 1", (str(channel_id),)).fetchone()
    return int(row[0]) if row else None


def _mark_seen(con: sqlite3.Connection, discord_id: str) -> None:
    con.execute("UPDATE dm_messages SET seen=1 WHERE discord_id=? AND direction='in' AND seen=0", (discord_id,))


def _digits(value: Any) -> str:
    """Только цифры: отпечаток уходит в адрес запроса, а «+» и «:» там портятся."""
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def _chat_version(con: sqlite3.Connection, discord_id: str) -> str:
    """Отпечаток переписки: меняется при новом, изменённом и удалённом сообщении."""
    row = con.execute("SELECT COUNT(*),COALESCE(MAX(updated_at),''),COALESCE(SUM(deleted),0) "
                      "FROM dm_messages WHERE discord_id=?", (discord_id,)).fetchone()
    return f"{row[0]}-{_digits(row[1])}-{row[2]}"


def _list_version(con: sqlite3.Connection) -> str:
    row = con.execute("SELECT COUNT(*),COALESCE(MAX(updated_at),''),"
                      "COALESCE(SUM(CASE WHEN direction='in' AND seen=0 THEN 1 ELSE 0 END),0) FROM dm_messages").fetchone()
    return f"{row[0]}-{_digits(row[1])}-{row[2]}"


# ------------------------------------------------------------------ данные --

def _roster_rows(admin: Any, con: sqlite3.Connection) -> list[dict[str, Any]]:
    """Игроки отрядов 1–6 и «Чемпионов» с их Discord-привязкой (у игрока может быть не одна)."""
    return admin.q(con, f"""
        SELECT p.id AS pid, p.canonical_nick AS nick, r.squad_id AS squad,
               d.discord_id AS did, d.discord_name AS dname, d.discord_username AS uname
        FROM players p
        JOIN (SELECT player_id, MIN(squad_id) AS squad_id, slot FROM roster_memberships
              WHERE active=1 AND {SQUAD_IDS_SQL} GROUP BY player_id) r ON r.player_id=p.id
        LEFT JOIN discord_bindings d ON d.player_id=p.id
        ORDER BY CASE WHEN r.squad_id=99 THEN 7 ELSE r.squad_id END, COALESCE(r.slot,999),
                 p.canonical_nick COLLATE NOCASE, d.discord_id""")


def _stats(con: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    """discord_id → сколько сообщений, сколько непрочитанных и какое последнее."""
    out: dict[str, dict[str, Any]] = {}
    for row in con.execute("""SELECT discord_id, COUNT(*) AS n,
            SUM(CASE WHEN direction='in' AND seen=0 AND deleted=0 THEN 1 ELSE 0 END) AS unread
            FROM dm_messages GROUP BY discord_id"""):
        out[row["discord_id"]] = {"n": row["n"], "unread": row["unread"] or 0, "last": None}
    for row in con.execute("""SELECT m.discord_id,m.direction,m.content,m.created_at,m.deleted,m.attachments_json
            FROM dm_messages m WHERE m.id=(SELECT m2.id FROM dm_messages m2 WHERE m2.discord_id=m.discord_id
                                           ORDER BY m2.created_at DESC, m2.id DESC LIMIT 1)"""):
        out[row["discord_id"]]["last"] = dict(row)
    return out


def _who(admin: Any, con: sqlite3.Connection, did: str) -> dict[str, Any] | None:
    """Кто такой собеседник: игрок по привязке или просто автор сообщений."""
    row = admin.one(con, """SELECT p.id AS pid, p.canonical_nick AS nick, d.discord_name AS dname,
            d.discord_username AS uname FROM discord_bindings d JOIN players p ON p.id=d.player_id
            WHERE d.discord_id=?""", (did,))
    if row:
        found = admin.one(con, f"SELECT MIN(squad_id) AS s FROM roster_memberships WHERE player_id=? AND active=1 "
                               f"AND {SQUAD_IDS_SQL}", (row["pid"],))
        row["squad"] = found["s"] if found else None
        return row
    seen = admin.one(con, "SELECT author_name FROM dm_messages WHERE discord_id=? AND direction='in' "
                          "ORDER BY created_at DESC LIMIT 1", (did,))
    if seen is None and not con.execute("SELECT 1 FROM dm_messages WHERE discord_id=? LIMIT 1", (did,)).fetchone():
        return None
    name = (seen or {}).get("author_name") or f"ID {did}"
    return {"pid": None, "nick": name, "dname": None, "uname": None, "squad": None}


def _can_message(con: sqlite3.Connection, did: str) -> bool:
    """Писать можно привязанным игрокам и тем, с кем уже была переписка (не любому ID)."""
    return bool(con.execute("SELECT 1 FROM discord_bindings WHERE discord_id=?", (did,)).fetchone()
                or con.execute("SELECT 1 FROM dm_messages WHERE discord_id=? LIMIT 1", (did,)).fetchone())


# ------------------------------------------------------------------ вёрстка --

CSS = r"""
.ch-group{margin-bottom:14px}
.ch-ghead{padding:9px 14px;font-size:12px;font-weight:700;letter-spacing:.04em;text-transform:uppercase;color:var(--dim);background:var(--soft);border-bottom:1px solid var(--line)}
.ch-row{display:flex;align-items:center;gap:12px;padding:10px 14px;border-bottom:1px solid var(--line);color:inherit}
.ch-row:last-child{border-bottom:0}
a.ch-row:hover{background:var(--soft);color:inherit}
.ch-row.off{color:var(--dim)}
.ch-row .nick{flex:none;width:200px;font-weight:650;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ch-row .last{flex:1;min-width:0;color:var(--dim);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ch-row.unread .last{color:var(--text);font-weight:600}
.ch-row .when{flex:none;font-size:12px;color:var(--dim);white-space:nowrap}
.ch-badge{flex:none;min-width:20px;height:20px;padding:0 6px;border-radius:10px;background:var(--accent);color:var(--on-accent);font-size:11px;font-weight:700;display:inline-grid;place-items:center}
.ch-chat{display:flex;flex-direction:column;height:clamp(420px,calc(100vh - 250px),780px)}
.ch-msgs{flex:1;min-height:0;overflow-y:auto;padding:14px 16px;display:flex;flex-direction:column;gap:6px}
.ch-msgs:focus{outline:none}
/* короткая переписка держится у поля ввода; margin-top:auto безопаснее, чем justify-content:flex-end
   (тот «съедает» верх прокрутки), а распорка снизу заменяет padding-bottom, который прокрутка flex-контейнера не учитывает */
.ch-msgs>:first-child{margin-top:auto}
.ch-msgs::after{content:"";flex:none;height:8px}
.ch-msg{max-width:min(620px,82%);padding:8px 12px 6px;border-radius:12px;border-top-left-radius:4px;background:var(--soft);align-self:flex-start}
.ch-msg.out{align-self:flex-end;border-top-left-radius:12px;border-top-right-radius:4px;background:var(--blue2)}
.ch-txt{white-space:pre-wrap;overflow-wrap:anywhere;word-break:break-word}
.ch-msg.del .ch-txt{text-decoration:line-through;opacity:.6}
.ch-meta{margin-top:3px;display:flex;flex-wrap:wrap;gap:4px 8px;font-size:11px;color:var(--dim)}
.ch-files{margin-top:4px;display:flex;flex-direction:column;gap:2px;font-size:13px}
.ch-files a{color:var(--blue);overflow-wrap:anywhere}
.ch-day{align-self:center;margin:10px 0 4px;font-size:12px;color:var(--dim)}
.ch-more{align-self:center;font-size:13px;margin-bottom:6px}
.ch-empty{margin:auto;max-width:380px;text-align:center;color:var(--dim)}
.ch-composer{display:flex;gap:8px;align-items:flex-end;padding:10px 12px;border-top:1px solid var(--line);background:var(--panel)}
.ch-composer textarea{flex:1;font:inherit;font-size:13px;line-height:1.4;min-height:38px;max-height:160px;resize:vertical;padding:8px 11px;border:1px solid var(--line2);border-radius:var(--r);background:var(--panel);color:var(--text)}
.ch-composer textarea:disabled{opacity:.55}
.ch-side{display:flex;flex-direction:column;align-items:flex-end;gap:4px}
.ch-count{font-size:11px;color:var(--dim)}
.ch-count.over{color:var(--red);font-weight:700}
.ch-hint{padding:0 14px 10px;font-size:12px;color:var(--dim);background:var(--panel)}
@media(max-width:700px){.ch-row .nick{width:110px}.ch-msg{max-width:92%}}
"""

LIST_JS = r"""<script>
(function(){
var cur=__VER__, VURL=__VURL__;
var flt=document.getElementById('ch-flt'), only=document.getElementById('ch-only');
function apply(){var q=flt?(flt.value||'').toLowerCase().trim():'',o=only&&only.checked;
  document.querySelectorAll('.ch-row').forEach(function(r){
    var ok=(r.getAttribute('data-q')||'').indexOf(q)>-1&&(!o||r.getAttribute('data-has')==='1');r.style.display=ok?'':'none'});
  document.querySelectorAll('.ch-group').forEach(function(g){
    var n=0;g.querySelectorAll('.ch-row').forEach(function(r){if(r.style.display!=='none')n++});g.style.display=n?'':'none'})}
if(flt){try{flt.value=sessionStorage.getItem('chats-flt')||''}catch(e){}
  flt.addEventListener('input',function(){try{sessionStorage.setItem('chats-flt',flt.value)}catch(e){}apply()})}
if(only){try{only.checked=sessionStorage.getItem('chats-only')==='1'}catch(e){}
  only.addEventListener('change',function(){try{sessionStorage.setItem('chats-only',only.checked?'1':'0')}catch(e){}apply()})}
apply();
function refresh(){
  return fetch(location.href,{credentials:'same-origin',cache:'no-store'}).then(function(r){return r.ok?r.text():null}).then(function(t){
    if(!t)return;var doc=new DOMParser().parseFromString(t,'text/html');
    ['ch-head','ch-list'].forEach(function(id){var n=doc.getElementById(id),o=document.getElementById(id);if(n&&o)o.innerHTML=n.innerHTML});
    var l=document.getElementById('ch-list');if(l&&l.getAttribute('data-ver'))cur=l.getAttribute('data-ver');apply()}).catch(function(){})}
function poll(){if(document.hidden)return;
  fetch(VURL,{credentials:'same-origin',cache:'no-store'}).then(function(r){return r.ok?r.json():null})
  .then(function(j){if(j&&j.v&&j.v!==cur)refresh()}).catch(function(){})}
setInterval(poll,5000);document.addEventListener('visibilitychange',function(){if(!document.hidden)poll()});
})();
</script>"""

CHAT_JS = r"""<script>
(function(){
var box=document.getElementById('ch-msgs'), ver=__VER__, FEED=__FEED__, SEND=__SEND__, SYNC=__SYNC__, N=__N__, MAX=__MAX__;
var form=document.getElementById('ch-form'), ta=document.getElementById('ch-text'), btn=document.getElementById('ch-send');
var cnt=document.getElementById('ch-count'), syncBtn=document.getElementById('ch-sync'), busy=false;
function nearBottom(){return box.scrollHeight-box.scrollTop-box.clientHeight<90}
function toBottom(){box.scrollTop=box.scrollHeight}
toBottom();
function poll(force){
  if(busy||(document.hidden&&!force))return Promise.resolve();
  return fetch(FEED+'?n='+N+'&v='+encodeURIComponent(ver),{credentials:'same-origin',cache:'no-store'})
    .then(function(r){return r.ok?r.json():null})
    .then(function(j){if(!j||!j.v)return;
      if(j.html!=null){var stick=nearBottom();box.innerHTML=j.html;if(stick)toBottom()}
      ver=j.v}).catch(function(){})}
setInterval(poll,3000);document.addEventListener('visibilitychange',function(){if(!document.hidden)poll()});
function jsonOf(r){return r.json().catch(function(){return {}}).then(function(j){return {ok:r.ok,j:j}})}
function count(){if(!ta||!btn)return;var n=ta.value.length;
  if(cnt){cnt.textContent=n+' / '+MAX;cnt.classList.toggle('over',n>MAX)}
  btn.disabled=ta.disabled||!ta.value.trim()||n>MAX}
if(ta){ta.addEventListener('input',count);count();
  ta.addEventListener('keydown',function(e){
    if(e.key==='Enter'&&!e.shiftKey&&!e.isComposing){e.preventDefault();if(!btn.disabled)form.requestSubmit()}})}
if(form)form.addEventListener('submit',function(e){
  e.preventDefault();if(busy||!ta.value.trim())return;busy=true;btn.disabled=true;
  fetch(SEND,{method:'POST',credentials:'same-origin',headers:{'Accept':'application/json'},body:new FormData(form)})
    .then(jsonOf)
    .then(function(x){if(!x.ok||!x.j.ok)throw new Error((x.j&&x.j.error)||'Не удалось отправить');
      ta.value='';busy=false;return poll(true).then(toBottom)})
    .catch(function(err){window.toast(err.message||'Ошибка отправки','err')})
    .then(function(){busy=false;count();ta.focus()})});
if(syncBtn)syncBtn.addEventListener('click',function(){
  syncBtn.disabled=true;
  fetch(SYNC,{method:'POST',credentials:'same-origin',headers:{'Accept':'application/json'}}).then(jsonOf)
    .then(function(x){if(!x.ok||!x.j.ok)throw new Error((x.j&&x.j.error)||'Не удалось загрузить историю');
      window.toast(x.j.added?('Добавлено сообщений: '+x.j.added):'Новых сообщений в Discord нет');return poll(true)})
    .catch(function(err){window.toast(err.message||'Ошибка','err')})
    .then(function(){syncBtn.disabled=false})});
})();
</script>"""


def _preview(last: dict[str, Any] | None) -> str:
    if not last:
        return ""
    if last.get("deleted"):
        text = "сообщение удалено"
    else:
        text = " ".join(str(last.get("content") or "").split())
        if not text:
            text = "📎 вложение" if json.loads(last.get("attachments_json") or "[]") else "пустое сообщение"
    if len(text) > 110:
        text = text[:110].rstrip() + "…"
    return ("Бот: " if last.get("direction") == "out" else "") + text


def _row_html(admin: Any, entry: dict[str, Any], stats: dict[str, dict[str, Any]]) -> str:
    esc = admin.esc
    did = entry.get("did")
    nick = entry["nick"]
    extra = entry.get("tag") or ""
    query = " ".join(str(x or "") for x in (nick, entry.get("dname"), entry.get("uname"))).lower()
    if not did:
        return (f"<div class='ch-row off' data-q='{esc(query)}' data-has='0'><span class='nick'>{esc(nick)}</span>"
                "<span class='last'>Нет привязки Discord — написать нельзя</span></div>")
    st = stats.get(did) or {"n": 0, "unread": 0, "last": None}
    last = st["last"]
    unread = int(st["unread"])
    badge = f"<span class='ch-badge' title='Непрочитанных: {unread}'>{unread}</span>" if unread else ""
    when = _fmt_when(last["created_at"]) if last else ""
    text = esc(_preview(last)) if last else "Переписки пока нет"
    cls = "ch-row" + (" unread" if unread else "")
    return (f"<a class='{cls}' href='{url_for('chat_view', did=did)}' data-q='{esc(query)}' data-has='{1 if st['n'] else 0}'>"
            f"<span class='nick'>{esc(nick)}{extra}</span><span class='last'>{text}</span>"
            f"<span class='when'>{esc(when)}</span>{badge}</a>")


def _render_list(admin: Any) -> str:
    esc = admin.esc
    with closing(_connect(admin.DB_PATH)) as con:
        roster = _roster_rows(admin, con)
        stats = _stats(con)
        version = _list_version(con)
        bound = {r["did"]: r for r in admin.q(con, "SELECT d.discord_id AS did, p.canonical_nick AS nick, "
                                              "d.discord_name AS dname, d.discord_username AS uname "
                                              "FROM discord_bindings d JOIN players p ON p.id=d.player_id")}
        authors = {r["discord_id"]: r["author_name"] for r in con.execute(
            "SELECT discord_id,author_name,MAX(created_at) FROM dm_messages WHERE direction='in' GROUP BY discord_id")}

    # группы по отрядам в порядке доски; игрок с двумя Discord-аккаунтами — два чата, ник подписан
    groups: dict[int, list[dict[str, Any]]] = {}
    per_player: dict[int, int] = {}
    for row in roster:
        per_player[row["pid"]] = per_player.get(row["pid"], 0) + 1
    in_roster: set[str] = set()
    for row in roster:
        entry = dict(row)
        if row["did"]:
            in_roster.add(row["did"])
            if per_player[row["pid"]] > 1:
                entry["tag"] = f" <span class='source-badge'>{esc(row['uname'] or row['dname'] or row['did'])}</span>"
        groups.setdefault(row["squad"], []).append(entry)

    sections = []
    for squad in sorted(groups, key=lambda s: 7 if s == 99 else s):
        rows = "".join(_row_html(admin, e, stats) for e in groups[squad])
        sections.append(f"<section class='panel ch-group'><div class='ch-ghead'>{esc(admin.squad_label(squad))}</div>{rows}</section>")

    others = []
    for did, st in stats.items():
        if did in in_roster:
            continue
        info = bound.get(did)
        nick = info["nick"] if info else (authors.get(did) or f"ID {did}")
        others.append({"nick": nick, "did": did, "dname": info and info["dname"], "uname": info and info["uname"]})
    others.sort(key=lambda e: (stats[e["did"]]["last"] or {}).get("created_at", ""), reverse=True)
    if others:
        rows = "".join(_row_html(admin, e, stats) for e in others)
        sections.append(f"<section class='panel ch-group'><div class='ch-ghead'>Вне отрядов</div>{rows}</section>")

    dialogs = sum(1 for st in stats.values() if st["n"])
    unread = sum(int(st["unread"]) for st in stats.values())
    if not sections:
        sections.append("<div class='hintbox'>В отрядах пока никого нет. Игроков можно добавить на странице «Отряды».</div>")
    if stats == {}:
        sections.insert(0, "<div class='hintbox'>Переписок пока нет. Сообщения, которые игроки напишут боту в личку, "
                           "появятся здесь сами — и сохранятся в базе. Откройте чат игрока, чтобы написать ему первым "
                           "или подтянуть старую переписку из Discord.</div>")

    head = ("<div id='ch-head'><div class='pagehead'><div><h1>Чаты</h1>"
            "<div class='subtitle'>Личные сообщения бота с игроками</div></div></div>"
            f"<div class='chips'><span class='chip'>Диалогов: <b>{dialogs}</b></span>"
            f"<span class='chip'>Непрочитано: <b>{unread}</b></span></div></div>")
    toolbar = ("<div class='toolbar'><label class='search'>" + admin.icon("search")
               + "<input id='ch-flt' type='search' placeholder='Поиск по нику…' aria-label='Поиск игрока'></label>"
               "<label class='toggle'><input type='checkbox' id='ch-only'> Только с перепиской</label></div>")
    body = (head + toolbar + f"<div id='ch-list' data-ver='{esc(version)}'>" + "".join(sections) + "</div>"
            + LIST_JS.replace("__VER__", json.dumps(version)).replace("__VURL__", json.dumps(url_for("chats_version"))))
    return admin.render("Чаты", body)


def _safe_url(value: Any) -> str | None:
    text = str(value or "")
    return text if text.startswith(("https://", "http://")) else None


def _bubble(admin: Any, row: dict[str, Any]) -> str:
    esc = admin.esc
    out = row["direction"] == "out"
    cls = "ch-msg" + (" out" if out else "") + (" del" if row["deleted"] else "")
    text = str(row["content"] or "")
    try:
        files = json.loads(row["attachments_json"] or "[]")
    except ValueError:
        files = []
    parts = [f"<div class='ch-txt'>{esc(text)}</div>"] if text else []
    links = []
    for item in files if isinstance(files, list) else []:
        name = esc(item.get("name") or "файл")
        url = _safe_url(item.get("url"))
        links.append(f"<a href='{esc(url)}' target='_blank' rel='noopener noreferrer'>📎 {name}</a>" if url
                     else f"<span class='dim'>📎 {name}</span>")
    if links:
        parts.append("<div class='ch-files'>" + "".join(links) + "</div>")
    if not parts:
        parts.append("<div class='ch-txt dim'>[пустое сообщение]</div>")
    meta = [f"<span title='{esc(_fmt_full(row['created_at']))}'>{esc(_fmt_clock(row['created_at']))}</span>"]
    if out:
        meta.append(f"<span>{'из панели' if row['source'] == 'panel' else 'бот'}</span>")
    if row["edited_at"] and not row["deleted"]:
        meta.append("<span>изменено</span>")
    if row["deleted"]:
        meta.append("<span>удалено в Discord</span>")
    parts.append("<div class='ch-meta'>" + "".join(meta) + "</div>")
    return f"<div class='{cls}'>{''.join(parts)}</div>"


def _messages_html(admin: Any, rows: list[dict[str, Any]], total: int, limit: int, did: str, can_send: bool) -> str:
    if not rows:
        hint = ("Сообщений пока нет. Напишите игроку первым — сообщение придёт ему в личку от имени бота."
                if can_send else "Сообщений пока нет.")
        return (f"<div class='ch-empty'>{hint}<br><br>Если игрок уже писал боту раньше, нажмите "
                "«Загрузить историю из Discord»: сохраняются только сообщения, пришедшие после включения чатов.</div>")
    out = []
    if total > len(rows):
        step = limit + PAGE
        out.append(f"<a class='ch-more' href='{url_for('chat_view', did=did, n=step)}'>"
                   f"Показаны последние {len(rows)} из {total} · показать раньше</a>")
    day = None
    for row in rows:
        moment = _msk(row["created_at"])
        key = moment.strftime("%Y-%m-%d") if moment else ""
        if key != day:
            day = key
            out.append(f"<div class='ch-day'>{admin.esc(admin.fmt_date_ru(key) if key else '')}</div>")
        out.append(_bubble(admin, row))
    return "".join(out)


def _limit_arg() -> int:
    try:
        return max(20, min(5000, int(request.args.get("n", PAGE))))
    except (TypeError, ValueError):
        return PAGE


def _load_messages(con: sqlite3.Connection, did: str, limit: int) -> tuple[list[dict[str, Any]], int]:
    total = con.execute("SELECT COUNT(*) FROM dm_messages WHERE discord_id=?", (did,)).fetchone()[0]
    rows = [dict(r) for r in con.execute(
        "SELECT * FROM (SELECT * FROM dm_messages WHERE discord_id=? ORDER BY created_at DESC, id DESC LIMIT ?) "
        "ORDER BY created_at, id", (did, limit))]
    return rows, int(total)


def _valid_id(did: str) -> str:
    if not (did.isascii() and did.isdigit() and 1 <= len(did) <= 20):
        abort(404)
    return did


def _render_chat(admin: Any, did: str, limit: int) -> str:
    esc = admin.esc
    with closing(_connect(admin.DB_PATH)) as con:
        who = _who(admin, con, did)
        if who is None:                      # нет ни привязки, ни переписки — такого чата нет
            abort(404)
        _mark_seen(con, did)                 # открыли переписку — всё входящее прочитано
        rows, total = _load_messages(con, did, limit)
        version = _chat_version(con, did)
    can_send = _STATE["bridge"] is not None

    title = esc(who["nick"])
    if who["pid"]:
        title = f"<a href='{url_for('player', player_id=who['pid'])}'>{title}</a>"
    sub = [admin.squad_label(who["squad"])] if who.get("squad") else []
    handle = who.get("uname") or who.get("dname")
    if handle:
        sub.append(f"@{handle}" if who.get("uname") else handle)
    sub.append(f"ID {did}")
    sync = (f"<button type='button' id='ch-sync'{'' if can_send else ' disabled'} "
            f"title='Подтянуть из Discord последние {SYNC_LIMIT} сообщений'>{admin.icon('refresh')} Загрузить историю из Discord</button>")
    head = (f"<div class='pagehead'><div><h1>{title}</h1><div class='subtitle'>{esc(' · '.join(sub))}</div></div>"
            f"<div class='actions'><a class='button' href='{url_for('chats')}'>{admin.icon('arrow-left')} Все чаты</a>{sync}</div></div>")
    placeholder = "Сообщение игроку…" if can_send else "Бот не запущен — отправка недоступна"
    composer = (f"<form id='ch-form' class='ch-composer' action='{url_for('chat_send', did=did)}' method='post'>"
                f"<textarea id='ch-text' name='text' rows='1' placeholder='{esc(placeholder)}' aria-label='Сообщение игроку'"
                f"{'' if can_send else ' disabled'}></textarea>"
                f"<div class='ch-side'><span id='ch-count' class='ch-count'></span>"
                f"<button id='ch-send' class='primary' type='submit' disabled>{admin.icon('send')} Отправить</button></div></form>"
                "<div class='ch-hint'>Enter — отправить, Shift+Enter — новая строка. Сообщение придёт игроку в личку от имени бота "
                "и сохранится в истории.</div>")
    panel = (f"<div class='panel ch-chat'><div id='ch-msgs' class='ch-msgs' tabindex='0'>"
             f"{_messages_html(admin, rows, total, limit, did, can_send)}</div>{composer}</div>")
    js = (CHAT_JS.replace("__VER__", json.dumps(version)).replace("__FEED__", json.dumps(url_for("chat_feed", did=did)))
          .replace("__SEND__", json.dumps(url_for("chat_send", did=did))).replace("__SYNC__", json.dumps(url_for("chat_sync", did=did)))
          .replace("__N__", str(limit)).replace("__MAX__", str(MAX_LEN)))
    return admin.render(f"Чат: {who['nick']}", head + panel + js)


# ------------------------------------------------------------------- роуты --

def install(admin: Any) -> None:
    if getattr(admin.app, "_chats_installed", False):
        return
    admin.app._chats_installed = True
    admin.add_css(CSS)
    admin.ICON_PATHS.setdefault("send", "<path d='M22 2L11 13'/><path d='M22 2l-7 20-4-9-9-4z'/>")
    admin.register_nav("Чаты", "chats", order=60, prefixes=("/chats",), super_only=True)
    admin.register_super_only("/chats")     # и страницы, и /feed, /send, /sync — только для мега-админа

    @admin.app.get("/chats")
    def chats():
        return _render_list(admin)

    @admin.app.get("/chats/version")
    def chats_version():
        with closing(_connect(admin.DB_PATH)) as con:
            return jsonify(v=_list_version(con))

    @admin.app.get("/chats/<did>")
    def chat_view(did: str):
        return _render_chat(admin, _valid_id(did), _limit_arg())

    @admin.app.get("/chats/<did>/feed")
    def chat_feed(did: str):
        """Переписка для автообновления: если отпечаток тот же, что у страницы, отдаём только его."""
        did = _valid_id(did)
        limit = _limit_arg()
        with closing(_connect(admin.DB_PATH)) as con:
            version = _chat_version(con, did)
            if version == request.args.get("v", ""):
                return jsonify(v=version)
            _mark_seen(con, did)             # вкладка открыта и опрашивает — значит, это читают
            rows, total = _load_messages(con, did, limit)
        html = _messages_html(admin, rows, total, limit, did, _STATE["bridge"] is not None)
        return jsonify(v=version, html=html)

    def fail(message: str, status: int):
        return jsonify(ok=False, error=message), status

    @admin.app.post("/chats/<did>/send")
    def chat_send(did: str):
        did = _valid_id(did)
        text = (request.form.get("text") or "").replace("\r\n", "\n").strip()
        if not text:
            return fail("Введите сообщение", 400)
        if len(text) > MAX_LEN:
            return fail(f"Слишком длинно: {len(text)} из {MAX_LEN} символов", 400)
        with closing(_connect(admin.DB_PATH)) as con:
            allowed = _can_message(con, did)
        if not allowed:
            return fail("Этот пользователь не привязан ни к одному игроку", 404)
        bridge = _STATE["bridge"]
        if bridge is None:
            return fail("Бот не запущен: писать игрокам можно, только пока он работает", 503)
        try:
            bridge.send(int(did), text)
        except ChatError as exc:
            return fail(str(exc), exc.status)
        except Exception:
            LOG.exception("chat send failed")
            return fail("Не удалось отправить сообщение", 500)
        return jsonify(ok=True)

    @admin.app.post("/chats/<did>/sync")
    def chat_sync(did: str):
        did = _valid_id(did)
        with closing(_connect(admin.DB_PATH)) as con:
            allowed = _can_message(con, did)
        if not allowed:
            return fail("Этот пользователь не привязан ни к одному игроку", 404)
        bridge = _STATE["bridge"]
        if bridge is None:
            return fail("Бот не запущен: историю можно подтянуть, только пока он работает", 503)
        try:
            added = bridge.history(int(did), SYNC_LIMIT)
        except ChatError as exc:
            return fail(str(exc), exc.status)
        except Exception:
            LOG.exception("chat history sync failed")
            return fail("Не удалось загрузить историю", 500)
        return jsonify(ok=True, added=int(added))


# ------------------------------------------------------------------- Discord --

def _discord_error(exc: Exception) -> ChatError:
    """Ошибку Discord → понятный текст. Классы discord.py не нужны: хватает status/code."""
    status = getattr(exc, "status", None)
    code = getattr(exc, "code", None)
    if code == 50007 or status == 403:
        return ChatError("Не удалось: у игрока закрыты личные сообщения (или он заблокировал бота).", 409)
    if code == 10013 or status == 404:
        return ChatError("Пользователь не найден в Discord.", 404)
    return ChatError(f"Ошибка Discord: {exc}", 502)


def install_runtime(runtime: Any) -> None:
    """Слушатели личных сообщений и мост для отправки из панели. Вызывается, когда бот уже собран."""
    bot = getattr(runtime, "bot", None)
    if bot is None or getattr(bot, "_chats_installed", False):
        return
    bot._chats_installed = True
    db_path = runtime.PLAYER_DB_PATH
    log = getattr(runtime, "log", None) or (lambda msg, level="info": LOG.info(msg))
    channel_users: dict[int, int] = {}          # личный канал → собеседник (чтобы не спрашивать Discord снова)

    def is_me(user: Any) -> bool:
        me = getattr(bot, "user", None)
        return me is not None and getattr(user, "id", None) == me.id

    async def partner_of(message: Any) -> int | None:
        """Игрок, с которым этот личный чат. Входящее — автор; своё сообщение — получатель канала.
        Discord не всегда присылает получателя в событии, тогда берём его из базы или из API."""
        if not is_me(message.author):
            return int(message.author.id)
        channel = message.channel
        recipient = getattr(channel, "recipient", None)
        if recipient is not None:
            return int(recipient.id)
        cid = int(channel.id)
        if cid in channel_users:
            return channel_users[cid]
        uid = await asyncio.to_thread(channel_user, db_path, cid)
        if uid is None:
            try:
                full = await bot.fetch_channel(cid)
            except Exception:
                return None
            recipient = getattr(full, "recipient", None)
            uid = int(recipient.id) if recipient is not None else None
        if uid is not None:
            channel_users[cid] = uid
        return uid

    async def save(message: Any, *, partner: int | None = None, seen: bool = False) -> bool:
        uid = partner if partner is not None else await partner_of(message)
        if uid is None:
            return False
        outgoing = is_me(message.author)
        return await asyncio.to_thread(
            store_message, db_path, discord_id=uid, channel_id=message.channel.id,
            direction="out" if outgoing else "in", source="bot" if outgoing else "discord", seen=seen,
            **message_fields(message))

    @bot.listen("on_message")
    async def _chats_on_message(message):
        if message.guild is not None:
            return
        if getattr(message.author, "bot", False) and not is_me(message.author):
            return
        try:
            await save(message)
        except Exception as exc:
            log(f"чаты: не удалось сохранить сообщение: {exc}", "err")

    @bot.listen("on_raw_message_edit")
    async def _chats_on_edit(payload):
        data = getattr(payload, "data", None) or {}
        if payload.guild_id is not None or "content" not in data:
            return
        try:
            await asyncio.to_thread(apply_edit, db_path, payload.message_id, data["content"], data.get("edited_timestamp"))
        except Exception as exc:
            log(f"чаты: не удалось обновить сообщение: {exc}", "err")

    @bot.listen("on_raw_message_delete")
    async def _chats_on_delete(payload):
        if payload.guild_id is not None:
            return
        try:
            await asyncio.to_thread(mark_deleted, db_path, [payload.message_id])
        except Exception as exc:
            log(f"чаты: не удалось пометить удалённое: {exc}", "err")

    async def resolve_user(uid: int) -> Any:
        for guild in list(getattr(bot, "guilds", None) or []):
            member = guild.get_member(uid)
            if member is not None:
                return member
        resolver = getattr(runtime, "resolve_user_for_dm", None)
        if resolver is not None:
            return await resolver(None, uid)
        try:
            return await bot.fetch_user(uid)
        except Exception:
            return None

    async def send_dm(uid: int, text: str) -> dict[str, str]:
        user = await resolve_user(uid)
        if user is None:
            raise ChatError("Пользователь не найден в Discord.", 404)
        try:
            sent = await user.send(text)
        except Exception as exc:
            raise _discord_error(exc) from exc
        # пишем в базу здесь, а не в запросе панели: если панель не дождалась ответа Discord,
        # сообщение всё равно уже ушло и должно быть в истории
        fields = message_fields(sent)
        await asyncio.to_thread(store_message, db_path, discord_id=uid, channel_id=sent.channel.id,
                                direction="out", source="panel", seen=True, **fields)
        return {"message_id": fields["message_id"]}

    async def pull_history(uid: int, limit: int) -> int:
        user = await resolve_user(uid)
        if user is None:
            raise ChatError("Пользователь не найден в Discord.", 404)
        added = 0
        try:
            async for message in user.history(limit=limit):
                if getattr(message.author, "bot", False) and not is_me(message.author):
                    continue
                if await save(message, partner=uid, seen=True):
                    added += 1
        except Exception as exc:
            raise _discord_error(exc) from exc
        return added

    def run(factory: Any, *args: Any) -> Any:
        """Выполнить корутину в цикле бота и дождаться результата (вызывается из потока панели)."""
        try:
            loop = bot.loop
        except Exception:
            loop = None
        if loop is None or not loop.is_running():
            raise ChatError("Бот сейчас не подключён к Discord.", 503)
        future = asyncio.run_coroutine_threadsafe(factory(*args), loop)
        try:
            return future.result(timeout=SEND_TIMEOUT)
        except (concurrent.futures.TimeoutError, TimeoutError):
            raise ChatError("Discord долго не отвечает. Проверьте чат: сообщение могло дойти.", 504)

    _STATE["bridge"] = SimpleNamespace(send=lambda uid, text: run(send_dm, uid, text),
                                       history=lambda uid, limit: run(pull_history, uid, limit))
