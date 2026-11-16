"""Шмот игроков для локальной админ-панели: страница «Шмот».

Игроки из отрядов, внутри отряда — в том же порядке мест, что на странице «Отряды»;
справа от ника — слоты по категориям:
Гаусс · Кувалда · Био · Тяж · Огнемёт · Скорость. Клик по слоту открывает выбор предмета
и заточки (+0…+15), «Скорость» — двузначное число 10–99 (39, 40, 25…) или прочерк (пусто).

Как добавить или поменять предмет — см. ITEMS и COLUMNS ниже; иконка лежит в tools/gear_icons/
(достаточно заменить файл с тем же именем). Данные — в таблице player_gear базы бота.
"""
from __future__ import annotations

import html
import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any, NamedTuple

from flask import abort, jsonify, request, send_from_directory, url_for

ICON_DIR = Path(__file__).resolve().parent / "gear_icons"
MAX_LEVEL = 15           # заточка +0 … +15
SPEED_RE = re.compile(r"[1-9][0-9]")        # скорость: ровно две цифры, 10…99 (только ASCII-цифры)
SPEED_HINT = "Скорость — двузначное число (10–99) или прочерк"

# Цвет рамки слота по редкости: легендарный — золотой, исключительный — красный.
RARITY = {
    "legendary": ("Легендарный", "#CC9933"),
    "exceptional": ("Исключительный", "#933"),
}


class Item(NamedTuple):
    name: str
    rarity: str
    icon: str            # файл в tools/gear_icons/


class Column(NamedTuple):
    key: str
    title: str
    items: tuple[str, ...]      # ключи из ITEMS; пусто у текстовой колонки

    @property
    def is_text(self) -> bool:
        return not self.items


ITEMS: dict[str, Item] = {
    "gauss": Item("Гаусс", "legendary", "gauss.webp"),
    "hammer": Item("Кувалда", "legendary", "hammer.webp"),
    "assault": Item("Штурма", "legendary", "assault.webp"),
    "zivk": Item("Зивка", "legendary", "zivk.webp"),
    "tank": Item("SBA ТАНК", "exceptional", "tank.webp"),
    "chief": Item("Вождь", "exceptional", "chief.webp"),
    "flamer": Item("Огнемёт", "exceptional", "flamer.webp"),
}

COLUMNS: tuple[Column, ...] = (
    Column("gauss", "Гаусс", ("gauss",)),
    Column("hammer", "Кувалда", ("hammer",)),
    Column("bio", "Био", ("assault", "zivk")),
    Column("heavy", "Тяж", ("tank", "chief")),
    Column("flamer", "Огнемёт", ("flamer",)),
    Column("speed", "Скорость", ()),
)
COLUMN_BY_KEY = {c.key: c for c in COLUMNS}
ICON_FILES = {i.icon for i in ITEMS.values()}


# ------------------------------------------------------------------- данные --

def _schema(con: sqlite3.Connection) -> None:
    # Без FOREIGN KEY: при слиянии дублей игроков (player_store._merge_player) строки
    # шмота переезжают к оставшемуся игроку, а не блокируют удаление дубля.
    con.execute("""CREATE TABLE IF NOT EXISTS player_gear(
        player_id INTEGER NOT NULL,
        slot TEXT NOT NULL,
        item TEXT NOT NULL DEFAULT '',
        level INTEGER NOT NULL DEFAULT 0 CHECK(level BETWEEN 0 AND 15),
        text TEXT NOT NULL DEFAULT '',
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY(player_id, slot))""")


def ordered_roster(admin: Any, con: sqlite3.Connection) -> list[dict[str, Any]]:
    """Игроки из отрядов 1–6 и Чемпионов (как в `admin.squad_roster`), но внутри отряда — в порядке мест
    со страницы «Отряды» (slot 1, 2, 3…), а не по алфавиту. Без места (редкость) — в конце отряда, по нику."""
    return admin.q(con, """
        SELECT p.id, p.canonical_nick, rm.squad_id, rm.slot
        FROM players p
        JOIN roster_memberships rm ON rm.player_id=p.id AND rm.active=1
             AND (rm.squad_id BETWEEN 1 AND 6 OR rm.squad_id=99)
        WHERE rm.id=(SELECT r2.id FROM roster_memberships r2
                     WHERE r2.player_id=p.id AND r2.active=1 AND (r2.squad_id BETWEEN 1 AND 6 OR r2.squad_id=99)
                     ORDER BY r2.squad_id, r2.slot, r2.id LIMIT 1)
        ORDER BY CASE WHEN rm.squad_id=99 THEN 7 ELSE rm.squad_id END,
                 rm.slot IS NULL, rm.slot, p.canonical_nick COLLATE NOCASE""")


def load_gear(con: sqlite3.Connection) -> dict[tuple[int, str], dict[str, Any]]:
    """(player_id, категория) → {'item', 'level', 'text'}; пустые слоты в базе не хранятся."""
    return {(int(r["player_id"]), str(r["slot"])): {"item": str(r["item"]), "level": int(r["level"]),
                                                     "text": str(r["text"])}
            for r in con.execute("SELECT player_id,slot,item,level,text FROM player_gear")}


def clean_speed(value: Any) -> str:
    """Скорость игрока: двузначное число 10–99 или пусто. «-», «—», «--» — это прочерк = пусто."""
    text = str(value if value is not None else "").strip()
    if not text.strip("-—–−"):
        return ""
    if not SPEED_RE.fullmatch(text):
        raise ValueError(SPEED_HINT)
    return text


def parse_level(value: Any) -> int:
    try:
        level = int(str(value if value is not None else "").strip() or 0)
    except ValueError:
        raise ValueError(f"Заточка — число от 0 до {MAX_LEVEL}") from None
    if not 0 <= level <= MAX_LEVEL:
        raise ValueError(f"Заточка — число от 0 до {MAX_LEVEL}")
    return level


def set_slot(con: sqlite3.Connection, player_id: int, slot: str, item: Any = "", level: Any = 0,
             text: Any = "") -> dict[str, Any]:
    """Записать слот игрока. Пустой предмет / пустой текст удаляют запись. Возвращает итог для страницы."""
    column = COLUMN_BY_KEY.get(slot)
    if column is None:
        raise ValueError("Неизвестная категория")
    if con.execute("SELECT 1 FROM players WHERE id=?", (player_id,)).fetchone() is None:
        raise ValueError("Игрок не найден")
    if column.is_text:
        value, key, lv = clean_speed(text), "", 0
    else:
        value, key = "", str(item or "").strip()
        if key and key not in column.items:
            raise ValueError(f"Этот предмет не подходит для «{column.title}»")
        lv = parse_level(level) if key else 0
    if not (key or value):
        con.execute("DELETE FROM player_gear WHERE player_id=? AND slot=?", (player_id, slot))
    else:
        con.execute("""INSERT INTO player_gear(player_id,slot,item,level,text) VALUES(?,?,?,?,?)
            ON CONFLICT(player_id,slot) DO UPDATE SET item=excluded.item,level=excluded.level,
            text=excluded.text,updated_at=CURRENT_TIMESTAMP""", (player_id, slot, key, lv, value))
    return {"item": key, "level": lv, "text": value}


# ----------------------------------------------------------------- страница --

CSS = r"""
.g-slot{position:relative;display:grid;place-items:center;width:36px;height:36px;min-height:0;margin:0 auto;padding:0;overflow:hidden;border:2px solid rgba(127,127,127,.4);border-radius:7px;background:repeating-linear-gradient(135deg,rgba(255,255,255,.05) 0 3px,transparent 3px 6px),#0f1419;color:#fff;cursor:pointer}
.g-slot:hover{background:repeating-linear-gradient(135deg,rgba(255,255,255,.09) 0 3px,transparent 3px 6px),#18202a}
.g-slot:focus-visible{outline:2px solid var(--blue);outline-offset:1px}
.g-slot:not(.empty){border-width:1.5px}.g-slot.empty{border-style:dashed}.g-slot.err{box-shadow:0 0 0 2px var(--red)}
.g-slot img{position:absolute;top:0;left:0;width:100%;height:100%;padding:1px;object-fit:contain;pointer-events:none;-webkit-user-drag:none}
.g-plus{font:400 17px/1 -apple-system,"Segoe UI",Arial,sans-serif;color:rgba(255,255,255,.3)}.g-slot:hover .g-plus{color:rgba(255,255,255,.65)}
.g-lv{position:absolute;right:2px;bottom:1px;font:700 10px/1 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;color:#fff;text-shadow:0 0 2px #000,0 0 3px #000,1px 1px 0 #000;pointer-events:none}
.g-mini{width:32px;height:32px;margin:0;flex:none;cursor:inherit}
/* Панель на всю ширину, как в «Игроках»: ник слева, иконки и скорость примерно по центру, справа свободное место (.g-f).
   overflow:visible, как у «Бафов», — иначе шапка не залипает при прокрутке страницы. */
.g-panel{overflow:visible}
/* table-layout:fixed: иконки и скорость — заданной ширины, пустая колонка справа (.g-f) — 25% таблицы, ник забирает остаток.
   Чем шире .g-f, тем левее блок категорий; чем уже — тем он правее. */
.gt{width:100%;table-layout:fixed}.gt td.nick,.gt th.nick{padding-left:8px}
.gt td.g-c,.gt th.g-c{width:58px;padding:4px 0}.gt th.g-c{font-size:9px;letter-spacing:0}
.gt td.g-f,.gt th.g-f{width:25%;padding:0}
/* Скорость: колонка по центру, число стоит ровно под заголовком «Скорость». */
.gt td.g-w,.gt th.g-w{width:74px;padding:4px 0;text-align:center}
.gt thead th{top:50px;box-shadow:0 1px 0 var(--line)}
@media(max-width:760px){.g-panel{overflow-x:auto}.gt{min-width:720px}.gt thead th{top:0}}
.g-sp{display:block;width:52px;height:30px;min-height:30px;margin:0 auto;padding:0 4px;border:1px solid transparent;border-radius:6px;background:transparent;color:var(--text);font:inherit;font-weight:650;text-align:center;font-variant-numeric:tabular-nums}
.g-sp::placeholder{color:var(--dim);opacity:.55}.g-sp:hover,.g-sp:focus{background:var(--panel);border-color:var(--line);outline:none}.g-sp:focus{border-color:var(--blue)}.g-sp.err{border-color:var(--red)}
.g-pop{position:absolute;z-index:60;width:216px;padding:8px;background:var(--panel);color:var(--text);border:1px solid var(--line);border-radius:10px;box-shadow:0 10px 30px rgba(0,0,0,.3)}.g-pop[hidden]{display:none}
.g-gh{display:flex;justify-content:space-between;align-items:baseline;gap:10px;margin:0 2px 6px;color:var(--dim);font-size:10px;font-weight:750;text-transform:uppercase;letter-spacing:.05em}.g-gh b{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--text);font-size:12px;text-transform:none;letter-spacing:0}
.g-opt{display:flex;align-items:center;justify-content:flex-start;gap:10px;width:100%;min-height:42px;padding:3px 6px;border:1px solid transparent;border-radius:8px;background:transparent;text-align:left;font-weight:700}
.g-opt:hover,.g-opt:focus-visible{background:var(--soft);outline:none}.g-opt.on{background:var(--blue2);border-color:var(--blue)}
.g-rr{display:block;font-size:10px;font-weight:650;line-height:1.2}.g-clear{min-height:32px;color:var(--dim);font-weight:650}
.g-lvl{display:flex;align-items:center;gap:6px;margin:8px 2px 0;padding-top:8px;border-top:1px solid var(--line);color:var(--dim);font-weight:650}
.g-lvl input{width:60px;margin-left:auto;text-align:center;font:inherit;font-weight:700;color:var(--text)}
.g-status{position:fixed;right:16px;bottom:16px;z-index:70;padding:6px 11px;border:1px solid var(--line);border-radius:8px;background:var(--panel);box-shadow:0 6px 18px rgba(0,0,0,.2);color:var(--dim);font-weight:650}.g-status:empty{display:none}.g-status.ok{color:var(--green)}.g-status.err{color:var(--red)}
"""

SCRIPT = r"""<script>
(()=>{const cfg=JSON.parse(document.getElementById('gear-cfg').textContent),cols={};cfg.columns.forEach(c=>cols[c.key]=c);
const pop=document.getElementById('gear-pop'),stat=document.getElementById('gear-status'),MAXL=cfg.maxLevel;
let cur=null,lastLevel=MAXL,pending=0,chain=Promise.resolve(),timer=0;
const el=(tag,cls,text)=>{const e=document.createElement(tag);if(cls)e.className=cls;if(text!=null)e.textContent=text;return e};
const clamp=v=>Math.max(0,Math.min(MAXL,parseInt(v,10)||0));
const show=(t,k='')=>{clearTimeout(timer);stat.textContent=t;stat.className='g-status '+k;if(k==='ok')timer=setTimeout(()=>{stat.textContent=''},1600)};
const post=data=>fetch('/gear/set',{method:'POST',keepalive:true,credentials:'same-origin',headers:{'Content-Type':'application/x-www-form-urlencoded','Accept':'application/json'},body:new URLSearchParams(data)}).then(async r=>{let j=null;try{j=await r.json()}catch(e){}if(!r.ok||!j||!j.ok)throw new Error((j&&j.error)||('Ошибка '+r.status));return j});
const run=(label,job)=>{pending++;show(label);const next=chain.then(job);chain=next.catch(()=>{}).then(()=>{pending--});return next};
window.addEventListener('beforeunload',e=>{if(pending>0){e.preventDefault();e.returnValue=''}});
function paint(btn,item,level){const col=cols[btn.dataset.col],it=item?cfg.items[item]:null;level=it?level:0;
btn.dataset.item=it?item:'';btn.dataset.level=String(level);btn.replaceChildren();
if(it){btn.className='g-slot';btn.style.borderColor=it.color;const img=el('img');img.src=it.icon;img.alt='';img.draggable=false;btn.append(img);if(level>0)btn.append(el('span','g-lv','+'+level))}
else{btn.className='g-slot empty';btn.style.borderColor='';btn.append(el('span','g-plus','+'))}
const state=it?it.name+(level>0?' +'+level:''):'пусто';btn.title=col.title+': '+state;btn.setAttribute('aria-label',col.title+', '+btn.dataset.nick+': '+state)}
function save(btn,item,level){const prev={item:btn.dataset.item,level:+btn.dataset.level};paint(btn,item,level);
return run('Сохранение…',()=>post({player:btn.dataset.player,col:btn.dataset.col,item:item,level:level})).then(r=>{paint(btn,r.item,r.level);show('Сохранено','ok')},e=>{paint(btn,prev.item,prev.level);btn.classList.add('err');show((e.message||'Ошибка')+' — не сохранено','err')})}
function close(back){pop.hidden=true;const b=cur;cur=null;if(back&&b)b.focus()}
function place(btn){const r=btn.getBoundingClientRect(),sx=window.scrollX,sy=window.scrollY;pop.style.left='0px';pop.style.top='0px';
const w=pop.offsetWidth,h=pop.offsetHeight;let x=r.left+sx+r.width/2-w/2,y=r.bottom+sy+6;
x=Math.max(sx+8,Math.min(x,sx+document.documentElement.clientWidth-w-8));
if(r.bottom+h+12>window.innerHeight&&r.top-h-12>0)y=r.top+sy-h-6;pop.style.left=x+'px';pop.style.top=y+'px'}
function open(btn){cur=btn;const col=cols[btn.dataset.col],item=btn.dataset.item,lvl=item?+btn.dataset.level:lastLevel;pop.replaceChildren();
const head=el('div','g-gh');head.append(el('span',null,col.title),el('b',null,btn.dataset.nick));pop.append(head);
const input=el('input');input.type='number';input.min=0;input.max=MAXL;input.step=1;input.value=lvl;input.setAttribute('aria-label','Заточка');
let first=null;
col.items.forEach(k=>{const it=cfg.items[k],o=el('button','g-opt'+(k===item?' on':''));o.type='button';
const s=el('span','g-slot g-mini');s.style.borderColor=it.color;const im=el('img');im.src=it.icon;im.alt='';s.append(im);
const t=el('span');t.append(el('span',null,it.name));const rr=el('span','g-rr',it.rarityName);rr.style.color=it.color;t.append(rr);o.append(s,t);
o.addEventListener('click',()=>{lastLevel=clamp(input.value);close(true);save(btn,k,lastLevel)});pop.append(o);if(!first||k===item)first=o});
const clr=el('button','g-opt g-clear','✕  Пусто');clr.type='button';clr.addEventListener('click',()=>{close(true);save(btn,'',0)});pop.append(clr);
const row=el('label','g-lvl');row.append(el('span',null,'Заточка +'),input);pop.append(row);
input.addEventListener('change',()=>{const v=clamp(input.value);input.value=v;lastLevel=v;if(btn.dataset.item)save(btn,btn.dataset.item,v)});
input.addEventListener('keydown',e=>{if(e.key==='Enter'){e.preventDefault();input.dispatchEvent(new Event('change'));close(true)}});
pop.hidden=false;place(btn);(first||clr).focus()}
document.querySelector('.gt').addEventListener('click',e=>{const b=e.target.closest('.g-slot');if(!b)return;if(!pop.hidden&&cur===b)close(true);else open(b)});
document.addEventListener('mousedown',e=>{if(!pop.hidden&&!pop.contains(e.target)&&!e.target.closest('.g-slot'))close(false)});
document.addEventListener('keydown',e=>{if(e.key==='Escape'&&!pop.hidden)close(true)});
window.addEventListener('resize',()=>close(false));document.addEventListener('scroll',e=>{if(!pop.hidden&&e.target.classList&&e.target.classList.contains('g-panel'))close(false)},true);
document.querySelectorAll('.g-sp').forEach(i=>{i.addEventListener('input',()=>{const v=i.value.replace(/\D/g,'').slice(0,2);if(v!==i.value)i.value=v});
i.addEventListener('keydown',e=>{if(e.key==='Enter'){e.preventDefault();i.blur()}});
i.addEventListener('change',()=>{i.classList.remove('err');run('Сохранение…',()=>post({player:i.dataset.player,col:'speed',text:i.value})).then(r=>{i.value=r.text;show('Сохранено','ok')},err=>{i.classList.add('err');show((err.message||'Ошибка')+' — не сохранено','err')})})});
})();
</script>"""


def _e(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def _slot_state(column: Column, entry: dict[str, Any] | None) -> tuple[str, int]:
    """Ключ предмета и заточка из записи; предмет, которого нет в настройках категории, считается пустым."""
    key = entry["item"] if entry else ""
    if key not in column.items:
        return "", 0
    return key, int(entry["level"])


def _slot_html(player_id: int, nick: str, column: Column, entry: dict[str, Any] | None, icon_url) -> str:
    key, level = _slot_state(column, entry)
    item = ITEMS.get(key)
    state = "пусто" if item is None else item.name + (f" +{level}" if level > 0 else "")
    attrs = (f"type='button' data-player='{player_id}' data-col='{column.key}' data-nick='{_e(nick)}' "
             f"data-item='{_e(key)}' data-level='{level}' title='{_e(column.title)}: {_e(state)}' "
             f"aria-label='{_e(column.title)}, {_e(nick)}: {_e(state)}'")
    if item is None:
        return f"<button class='g-slot empty' {attrs}><span class='g-plus'>+</span></button>"
    color = RARITY[item.rarity][1]
    badge = f"<span class='g-lv'>+{level}</span>" if level > 0 else ""
    return (f"<button class='g-slot' style='border-color:{color}' {attrs}>"
            f"<img src='{_e(icon_url(item.icon))}' alt=''>{badge}</button>")


def _config(icon_url) -> str:
    cfg = {
        "maxLevel": MAX_LEVEL,
        "columns": [{"key": c.key, "title": c.title, "items": list(c.items)} for c in COLUMNS],
        "items": {key: {"name": i.name, "rarityName": RARITY[i.rarity][0], "color": RARITY[i.rarity][1],
                        "icon": icon_url(i.icon)} for key, i in ITEMS.items()},
    }
    return json.dumps(cfg, ensure_ascii=False).replace("</", "<\\/")


def _icon_url(filename: str) -> str:
    path = ICON_DIR / filename
    try:
        version = int(path.stat().st_mtime)
    except OSError:
        version = 0
    return url_for("gear_icon", name=filename, v=version)


def render_body(admin: Any, roster: list[dict[str, Any]], gear: dict[tuple[int, str], dict[str, Any]]) -> str:
    icons = {name: _icon_url(name) for name in ICON_FILES}.__getitem__     # url иконки считаем один раз на страницу
    groups: dict[int, list[dict[str, Any]]] = {squad_id: [] for squad_id in admin.SQUAD_ORDER}
    for row in roster:
        groups[int(row["squad_id"])].append(row)
    head = "<th class='nick'>Ник</th>" + "".join(
        f"<th class='g-c{' g-w' if c.is_text else ''}'>{_e(c.title)}</th>" for c in COLUMNS) + "<th class='g-f'></th>"
    bodies = []
    for squad_id in admin.SQUAD_ORDER:
        members = groups[squad_id]
        if not members:
            continue
        rows = []
        for r in members:
            pid, nick = int(r["id"]), str(r["canonical_nick"])
            cells = []
            for column in COLUMNS:
                entry = gear.get((pid, column.key))
                if column.is_text:
                    text = entry["text"] if entry else ""
                    old_format = bool(text) and not SPEED_RE.fullmatch(text)     # запись из старой версии, до правила 10–99
                    cls, tip = ("g-sp err", f" title='{_e(SPEED_HINT)}'") if old_format else ("g-sp", "")
                    cells.append(f"<td class='g-c g-w'><input class='{cls}' data-player='{pid}' placeholder='—' "
                                 f"inputmode='numeric' autocomplete='off' maxlength='2' value='{_e(text)}'{tip} "
                                 f"aria-label='Скорость, {_e(nick)}'></td>")
                else:
                    cells.append(f"<td class='g-c'>{_slot_html(pid, nick, column, entry, icons)}</td>")
            rows.append(f"<tr class='player-row'><td class='nick'><a href='{url_for('player', player_id=pid)}'>"
                        f"{_e(nick)}</a></td>{''.join(cells)}<td class='g-f'></td></tr>")
        bodies.append(f"<tbody class='squad-group'><tr class='squad-title'><td colspan='{len(COLUMNS) + 2}'>"
                      f"{_e(admin.squad_label(squad_id))}</td></tr>{''.join(rows)}</tbody>")
    if bodies:
        table = ("<div class='panel g-panel'><table class='tight vt gt'>"
                 f"<thead><tr>{head}</tr></thead>{''.join(bodies)}</table></div>")
    else:
        table = "<p class='hint'>В отрядах пока никого нет.</p>"
    return ("<div class='pagehead'><div><h1>Шмот</h1>"
            "</div></div>"
            "<input id='flt' placeholder='Поиск по нику…' aria-label='Поиск игрока' oninput=\"filterPlayers(this.value)\">"
            f"{table}<div id='gear-pop' class='g-pop' hidden role='dialog' aria-label='Выбор предмета'></div>"
            "<div id='gear-status' class='g-status' role='status' aria-live='polite'></div>"
            f"<script type='application/json' id='gear-cfg'>{_config(icons)}</script>"
            f"<script>{admin.FILTER_PLAYERS_JS}</script>{SCRIPT if bodies else ''}")


# ------------------------------------------------------------- админ-панель --

def install(admin: Any) -> None:
    if getattr(admin.app, "_gear_installed", False):
        return
    admin.app._gear_installed = True
    admin.PAGE = admin.PAGE.replace("</style>", CSS.replace("{", "{{").replace("}", "}}") + "\n</style>", 1)
    base_render = admin.render

    def render(title: str, body: str) -> str:
        page = base_render(title, body)
        active = "active" if request.path.startswith("/gear") else ""
        link = f"<a class='{active}' href='{url_for('gear_page')}'>Шмот</a>"
        logout = "<form method=\"post\" action=\"/logout\""
        if "<!--navx-->" in page:
            return page.replace("<!--navx-->", link, 1)
        if logout in page:
            return page.replace(logout, link + logout, 1)
        return page.replace("</nav></div></header>", link + "</nav></div></header>", 1)

    admin.render = render

    @admin.app.get("/gear")
    def gear_page():
        with closing(admin.player_store.connect(admin.DB_PATH)) as con:
            _schema(con)
            roster = ordered_roster(admin, con)
            gear = load_gear(con)
        return admin.render("Шмот", render_body(admin, roster, gear))

    @admin.app.post("/gear/set")
    def gear_set():
        try:
            try:
                player_id = int(request.form.get("player", ""))
            except ValueError:
                raise ValueError("Неверный игрок") from None
            with closing(admin.player_store.connect(admin.DB_PATH)) as con:
                _schema(con)
                con.execute("BEGIN IMMEDIATE")
                try:
                    result = set_slot(con, player_id, request.form.get("col", ""), request.form.get("item", ""),
                                      request.form.get("level", "0"), request.form.get("text", ""))
                    con.execute("COMMIT")
                except BaseException:
                    con.execute("ROLLBACK")
                    raise
            return jsonify(ok=True, **result)
        except ValueError as exc:
            return jsonify(ok=False, error=str(exc)), 400

    @admin.app.get("/gear/icon/<name>")
    def gear_icon(name: str):
        if name not in ICON_FILES:
            abort(404)
        return send_from_directory(ICON_DIR, name, max_age=30 * 24 * 3600)
