"""Шмот игроков для локальной админ-панели: страница «Шмот».

Раскладка — та же доска, что на странице «Отряды»: сверху отряды 1·2·3, под ними 4·5·6, справа на всю высоту «Замены».
В каждом месте доски стоит одна строка: ник и справа от него слоты по категориям
Снайпы · Кувалда · Био · Тяж · Огнемёт · Скорость — всё на одном экране, без прокрутки. Клик по слоту открывает выбор
предмета и заточки (+0…+15), «Скорость» — двузначное число 10–99 (39, 40, 25…) или прочерк (пусто).

Ник можно перетащить на другое место (или на пустое): игроки меняются местами вместе со шмотом — шмот лежит в базе
у игрока, а не у места, а само перемещение делает тот же запрос `/squads/cells/swap`, что и на странице «Отряды».

У слота три состояния: не указано (пунктирная рамка, штриховка и «+»), «Пусто» — у игрока этого предмета нет (тот же
штрихованный фон, но без «+» и со сплошной рамкой; сохраняется как отметка NONE_KEY) и выбранный предмет.

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

from tools import roster_board

ICON_DIR = Path(__file__).resolve().parent / "gear_icons"
MAX_LEVEL = 15           # заточка +0 … +15
SPEED_RE = re.compile(r"[1-9][0-9]")        # скорость: ровно две цифры, 10…99 (только ASCII-цифры)
SPEED_HINT = "Скорость — двузначное число (10–99) или прочерк"
# Отметка «у игрока этого предмета нет»: лежит в player_gear.item вместо ключа предмета, заточка 0.
# Пустой item (нет записи) — это «не указано», а не «нет».
NONE_KEY = "none"

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
    show_name: bool = False     # в слоте вместо «+N» писать название предмета (белым, внизу иконки)
    short: str = ""             # короткая подпись над слотами на доске (по умолчанию — title)

    @property
    def label(self) -> str:
        return self.short or self.title

    @property
    def is_text(self) -> bool:
        return not self.items


ITEMS: dict[str, Item] = {
    "gauss": Item("Гаусс", "legendary", "gauss.webp"),
    # Новые снайперские винтовки — исключительные (красная рамка). Названия меняются здесь,
    # ключи sniper1…3 не трогать — они записаны в базе у игроков.
    "sniper1": Item("EBR", "exceptional", "sniper1.webp"),
    "sniper2": Item("pZB", "exceptional", "sniper2.webp"),
    "sniper3": Item("СВД-м", "exceptional", "sniper3.webp"),
    "hammer": Item("Кувалда", "legendary", "hammer.webp"),
    "assault": Item("Штурма", "legendary", "assault.webp"),
    "zivk": Item("Зивка", "legendary", "zivk.webp"),
    "tank": Item("SBA ТАНК", "exceptional", "tank.webp"),
    "chief": Item("Вождь", "exceptional", "chief.webp"),
    "flamer": Item("Огнемёт", "exceptional", "flamer.webp"),
}

COLUMNS: tuple[Column, ...] = (
    # Ключ gauss остался от прежней категории «Гаусс» — по нему шмот лежит в базе, переименовывать нельзя.
    Column("gauss", "Снайпы", ("gauss", "sniper1", "sniper2", "sniper3"), show_name=True, short="Снайп"),
    Column("hammer", "Кувалда", ("hammer",), short="Кувал"),
    Column("bio", "Био", ("assault", "zivk")),
    Column("heavy", "Тяж", ("tank", "chief")),
    Column("flamer", "Огнемёт", ("flamer",), short="Огнем"),
    Column("speed", "Скорость", (), short="Скор"),
)
COLUMN_BY_KEY = {c.key: c for c in COLUMNS}
ICON_FILES = {i.icon for i in ITEMS.values()}

# Колонки доски слева направо: (верхний отряд, нижний отряд). Как на «Отрядах»: 1 над 4, 2 над 5, 3 над 6;
# «Замены» — одна колонка на всю высоту (5 мест + 1 в полосе между рядами + 5 мест = 11).
BOARD_COLUMNS = tuple(zip(roster_board.TOP, roster_board.BOTTOM)) + ((roster_board.SUBS_ID, None),)
BOARD_ROWS = 12          # шапка + 5 мест + шапка/полоса + 5 мест


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
    """Записать слот игрока. Пустой предмет / пустой текст удаляют запись («не указано»); NONE_KEY сохраняет
    «предмета нет». Возвращает итог для страницы."""
    column = COLUMN_BY_KEY.get(slot)
    if column is None:
        raise ValueError("Неизвестная категория")
    if con.execute("SELECT 1 FROM players WHERE id=?", (player_id,)).fetchone() is None:
        raise ValueError("Игрок не найден")
    if column.is_text:
        value, key, lv = clean_speed(text), "", 0
    else:
        value, key = "", str(item or "").strip()
        if key and key != NONE_KEY and key not in column.items:
            raise ValueError(f"Этот предмет не подходит для «{column.title}»")
        lv = parse_level(level) if key and key != NONE_KEY else 0
    if not (key or value):
        con.execute("DELETE FROM player_gear WHERE player_id=? AND slot=?", (player_id, slot))
    else:
        con.execute("""INSERT INTO player_gear(player_id,slot,item,level,text) VALUES(?,?,?,?,?)
            ON CONFLICT(player_id,slot) DO UPDATE SET item=excluded.item,level=excluded.level,
            text=excluded.text,updated_at=CURRENT_TIMESTAMP""", (player_id, slot, key, lv, value))
    return {"item": key, "level": lv, "text": value}


def place_on_board(roster: list[dict[str, Any]]) -> tuple[dict[tuple[int, int], dict[str, Any]], list[dict[str, Any]]]:
    """Разложить игроков по местам доски: {(отряд, место): игрок} и список тех, кому места на доске нет
    (место не задано или вне доски) — их тоже показываем, чтобы шмот не пропадал из виду."""
    placed: dict[tuple[int, int], dict[str, Any]] = {}
    loose: list[dict[str, Any]] = []
    for row in roster:
        squad_id, slot = int(row["squad_id"]), row.get("slot")
        position = (squad_id, int(slot)) if slot is not None else None
        if position and position[1] in roster_board.slots_for(squad_id) and position not in placed:
            placed[position] = row
        else:
            loose.append(row)
    return placed, loose


# ----------------------------------------------------------------- страница --

CSS = r"""
/* Фон слота под иконкой: --g-bg, при наведении — --g-bg-hi. Светлее фон — лучше видны тёмные иконки
   (пробовали #222b36 / #2d3844 — не понравилось, вернули тёмный). Размер слота задаёт доска (--gs),
   а в окне выбора (там --gs нет) слот 36px, у .g-mini — 32px. */
.g-slot{--g-bg:#0f1419;--g-bg-hi:#18202a;position:relative;display:grid;place-items:center;width:var(--gs,36px);height:var(--gs,36px);min-height:0;margin:0;padding:0;overflow:hidden;border:2px solid rgba(127,127,127,.4);border-radius:7px;background:repeating-linear-gradient(135deg,rgba(255,255,255,.05) 0 3px,transparent 3px 6px),var(--g-bg);color:#fff;cursor:pointer}
.g-slot:hover{background:repeating-linear-gradient(135deg,rgba(255,255,255,.09) 0 3px,transparent 3px 6px),var(--g-bg-hi)}
.g-slot:focus-visible{outline:2px solid var(--blue);outline-offset:1px}
.g-slot:not(.empty){border-width:1.5px}.g-slot.empty{border-style:dashed}.g-slot.err{box-shadow:0 0 0 2px var(--red)}
/* «Пусто» = у игрока этого предмета нет: фон со штриховкой такой же, как у остальных слотов (берётся из .g-slot),
   от «не указано» отличается только тем, что нет «+» и рамка сплошная тонкая, а не пунктирная. */
.g-slot.none{border-width:1px;border-style:solid;border-color:rgba(127,127,127,.3)}
.g-slot img{position:absolute;top:0;left:0;width:100%;height:100%;padding:1px;object-fit:contain;pointer-events:none;-webkit-user-drag:none}
.g-plus{font:400 17px/1 -apple-system,"Segoe UI",Arial,sans-serif;color:rgba(255,255,255,.3)}.g-slot:hover .g-plus{color:rgba(255,255,255,.65)}
.g-lv{position:absolute;right:2px;bottom:1px;font:700 clamp(8.5px,calc(var(--gs,36px) * .28),11px)/1 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;color:#fff;text-shadow:0 0 2px #000,0 0 3px #000,1px 1px 0 #000;pointer-events:none}
/* Название предмета вместо «+N» (колонки с show_name): белое, по центру внизу иконки, без обводки. */
.g-nm{position:absolute;left:0;right:0;bottom:2px;text-align:center;white-space:nowrap;pointer-events:none;font:600 clamp(7.5px,calc(var(--gs,36px) * .25),10px)/1 -apple-system,BlinkMacSystemFont,"Segoe UI",Arial,sans-serif;color:#fff}
.g-mini{width:32px;height:32px;margin:0;flex:none;cursor:inherit}

/* ── Доска: те же 3+3 отряда и «Замены» справа, что на странице «Отряды» ────────────────────────────────────────
   Всё считается от ширины и высоты окна, чтобы доска помещалась на экран без прокрутки:
   --row  — высота строки (от высоты окна: шапка страницы ≈ 170px, остальное делится на 12 строк);
   --gs   — размер слота (от ширины колонки: на ник остаётся не меньше ~100px; не больше 42px и не больше строки).
   #gear-root — контейнер, от его ширины (cqw) считается колонка. */
body.g-wide{--w:1700px}
body.g-wide .wrap{padding-bottom:20px}
.g-top{margin:2px 0 12px}
#gear-root{container-type:inline-size;--row:clamp(34px,calc((100vh - 168px) / 12),54px);--gs:32px;--gp:34px;--cols:minmax(0,1fr) repeat(5,var(--gs)) var(--gp)}
@supports (width:1cqw){#gear-root{--gs:clamp(26px,min(calc(var(--row) - 10px),calc((25cqw - 136px) / 6)),42px);--gp:calc(var(--gs) + 2px)}}
.g-panel{margin:0;overflow-x:auto;overflow-y:hidden}
.gb{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));grid-template-rows:repeat(12,var(--row));grid-auto-flow:column;min-width:1100px}
.gb>*{min-width:0;border-right:1px solid var(--line);border-bottom:1px solid var(--line)}
.gb>:nth-child(12n){border-bottom:0}.gb>:nth-last-child(-n+12){border-right:0}
/* Шапка колонки: слева «Отряд N», над слотами — названия категорий (те же колонки сетки, что и у строк). */
.gb-h,.g-row.filled{display:grid;grid-template-columns:var(--cols);column-gap:3px;align-items:center;padding:0 8px}
.gb-h{background:var(--soft);color:var(--dim);font-size:12px;font-weight:650}
.gb-t{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.gb-l{font-size:clamp(8px,calc(var(--gs) * .26),10px);font-weight:600;line-height:1;text-align:center;white-space:nowrap;letter-spacing:-.1px}
.g-row{position:relative;background:var(--panel)}
.g-row.filled:hover{background:var(--blue2)}
/* Ник — ручка для перетаскивания; точки слева видны всегда бледно, при наведении ярче. */
.g-nick{display:flex;align-items:center;min-width:0;height:100%;cursor:grab}.g-nick:active{cursor:grabbing}
.g-nick::before{content:"";flex:none;width:6px;height:12px;margin:0 5px 0 -2px;opacity:.3;background:radial-gradient(circle,var(--dim) 1px,transparent 1.6px) 0 0/3px 4px}
.g-row:hover .g-nick::before{opacity:.85}
.g-nick a{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--text);font-size:13px;font-weight:600}.g-nick a:hover{color:var(--blue)}
/* Перетаскивание: исходная строка бледнеет, цель подсвечивается, пустые места обозначаются пунктиром. */
.g-row.dragging{opacity:.4}
.gb.g-dragging .g-row:not(.filled){box-shadow:inset 0 0 0 1px var(--line2)}
.g-row.drag-over{background:var(--blue2);box-shadow:inset 0 0 0 2px var(--blue)!important}
/* Поиск: совпавшие обводятся, остальные тускнеют (строки не прячутся — доска остаётся на месте). */
.g-row.q-dim{opacity:.22}.g-row.q-hit{box-shadow:inset 0 0 0 2px var(--blue)}
/* Скорость: двузначное число под заголовком «Скорость». */
.g-sp{display:block;width:var(--gp,34px);height:calc(var(--gs,32px) - 2px);min-height:0;margin:0;padding:0;border:1px solid transparent;border-radius:6px;background:transparent;color:var(--text);font:inherit;font-weight:650;text-align:center;font-variant-numeric:tabular-nums}
.g-sp::placeholder{color:var(--dim);opacity:.55}.g-sp:hover,.g-sp:focus{background:var(--panel);border-color:var(--line);outline:none}.g-sp:focus{border-color:var(--blue)}.g-sp.err{border-color:var(--red)}
/* Игроки, которым на доске места нет (место не задано): ниже доски, в тех же колонках, без перетаскивания. */
.g-extra{margin:14px 0 0}.g-extra h2{margin:0 0 6px;color:var(--dim);font-size:12px;font-weight:650}
.gb-x{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:0 1px;min-width:1100px}
.gb-x .g-row{height:var(--row);border-bottom:1px solid var(--line)}.gb-x .g-nick{cursor:default}.gb-x .g-nick::before{display:none}
.g-pop{position:absolute;z-index:60;width:216px;padding:8px;background:var(--panel);color:var(--text);border:1px solid var(--line);border-radius:10px;box-shadow:0 10px 30px rgba(0,0,0,.3)}.g-pop[hidden]{display:none}
.g-gh{display:flex;justify-content:space-between;align-items:baseline;gap:10px;margin:0 2px 6px;color:var(--dim);font-size:12px;font-weight:600}.g-gh b{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--text);font-size:12px;text-transform:none;letter-spacing:0}
.g-opt{display:flex;align-items:center;justify-content:flex-start;gap:10px;width:100%;min-height:42px;padding:3px 6px;border:1px solid transparent;border-radius:8px;background:transparent;text-align:left;font-weight:700}
.g-opt:hover,.g-opt:focus-visible{background:var(--soft);outline:none}.g-opt.on{background:var(--blue2);border-color:var(--blue)}
.g-rr{display:block;font-size:10px;font-weight:650;line-height:1.2}.g-clear{min-height:32px;color:var(--dim);font-weight:650}.g-reset{min-height:28px;font-size:12px;font-weight:600}
.g-lvl{display:flex;align-items:center;gap:6px;margin:8px 2px 0;padding-top:8px;border-top:1px solid var(--line);color:var(--dim);font-weight:650}
.g-lvl input{width:60px;margin-left:auto;text-align:center;font:inherit;font-weight:700;color:var(--text)}
.g-slot.empty{opacity:.6}.g-slot.empty:hover,.g-slot.empty:focus-visible{opacity:1}
.g-status{position:fixed;right:16px;bottom:16px;z-index:70;padding:6px 11px;border:1px solid var(--line);border-radius:8px;background:var(--panel);box-shadow:0 6px 18px rgba(0,0,0,.2);color:var(--dim);font-weight:650}.g-status:empty{display:none}.g-status.ok{color:var(--green)}.g-status.err{color:var(--red)}
"""

SCRIPT = r"""<script>
(()=>{const cfg=JSON.parse(document.getElementById('gear-cfg').textContent),cols={};cfg.columns.forEach(c=>cols[c.key]=c);
const root=document.getElementById('gear-root'),board=document.getElementById('gear-board');
const pop=document.getElementById('gear-pop'),stat=document.getElementById('gear-status'),MAXL=cfg.maxLevel,NONE=cfg.noneKey;
let cur=null,lastLevel=MAXL,pending=0,chain=Promise.resolve(),timer=0;
const el=(tag,cls,text)=>{const e=document.createElement(tag);if(cls)e.className=cls;if(text!=null)e.textContent=text;return e};
const clamp=v=>Math.max(0,Math.min(MAXL,parseInt(v,10)||0));
const show=(t,k='')=>{clearTimeout(timer);stat.textContent=t;stat.className='g-status '+k;if(k==='ok')timer=setTimeout(()=>{stat.textContent=''},1600)};
const post=data=>fetch('/gear/set',{method:'POST',keepalive:true,credentials:'same-origin',headers:{'Content-Type':'application/x-www-form-urlencoded','Accept':'application/json'},body:new URLSearchParams(data)}).then(async r=>{let j=null;try{j=await r.json()}catch(e){}if(!r.ok||!j||!j.ok)throw new Error((j&&j.error)||('Ошибка '+r.status));return j});
const run=(label,job)=>{pending++;show(label);const next=chain.then(job);chain=next.catch(()=>{}).then(()=>{pending--});return next};
window.addEventListener('beforeunload',e=>{if(pending>0){e.preventDefault();e.returnValue=''}});
function paint(btn,item,level){const col=cols[btn.dataset.col],none=item===NONE,it=item&&!none?cfg.items[item]:null;level=it?level:0;
btn.dataset.item=it?item:(none?NONE:'');btn.dataset.level=String(level);btn.replaceChildren();
if(it){btn.className='g-slot';btn.style.borderColor=it.color;const img=el('img');img.src=it.icon;img.alt='';img.draggable=false;btn.append(img);if(col.showName)btn.append(el('span','g-nm',it.name));else if(level>0)btn.append(el('span','g-lv','+'+level))}
else if(none){btn.className='g-slot none';btn.style.borderColor=''}
else{btn.className='g-slot empty';btn.style.borderColor='';btn.append(el('span','g-plus','+'))}
const state=it?it.name+(level>0?' +'+level:''):(none?'нет':'не указано');btn.title=col.title+': '+state;btn.setAttribute('aria-label',col.title+', '+btn.dataset.nick+': '+state)}
function save(btn,item,level){const prev={item:btn.dataset.item,level:+btn.dataset.level};paint(btn,item,level);
return run('Сохранение…',()=>post({player:btn.dataset.player,col:btn.dataset.col,item:item,level:level})).then(r=>{paint(btn,r.item,r.level);show('Сохранено','ok')},e=>{paint(btn,prev.item,prev.level);btn.classList.add('err');show((e.message||'Ошибка')+' — не сохранено','err')})}
function close(back){pop.hidden=true;const b=cur;cur=null;if(back&&b)b.focus()}
function place(btn){const r=btn.getBoundingClientRect(),sx=window.scrollX,sy=window.scrollY;pop.style.left='0px';pop.style.top='0px';
const w=pop.offsetWidth,h=pop.offsetHeight;let x=r.left+sx+r.width/2-w/2,y=r.bottom+sy+6;
x=Math.max(sx+8,Math.min(x,sx+document.documentElement.clientWidth-w-8));
if(r.bottom+h+12>window.innerHeight&&r.top-h-12>0)y=r.top+sy-h-6;pop.style.left=x+'px';pop.style.top=y+'px'}
function open(btn){cur=btn;const col=cols[btn.dataset.col],item=btn.dataset.item,real=!!item&&item!==NONE,lvl=real?+btn.dataset.level:lastLevel;pop.replaceChildren();
const head=el('div','g-gh');head.append(el('span',null,col.title),el('b',null,btn.dataset.nick));pop.append(head);
const input=el('input');input.type='number';input.min=0;input.max=MAXL;input.step=1;input.value=lvl;input.setAttribute('aria-label','Заточка');
let first=null;
col.items.forEach(k=>{const it=cfg.items[k],o=el('button','g-opt'+(k===item?' on':''));o.type='button';
const s=el('span','g-slot g-mini');s.style.borderColor=it.color;const im=el('img');im.src=it.icon;im.alt='';s.append(im);
const t=el('span');t.append(el('span',null,it.name));const rr=el('span','g-rr',it.rarityName);rr.style.color=it.color;t.append(rr);o.append(s,t);
o.addEventListener('click',()=>{lastLevel=clamp(input.value);close(true);save(btn,k,lastLevel)});pop.append(o);if(!first||k===item)first=o});
const clr=el('button','g-opt g-clear'+(item===NONE?' on':''),'✕  Пусто');clr.type='button';clr.title='У игрока этого предмета нет';clr.addEventListener('click',()=>{close(true);save(btn,NONE,0)});pop.append(clr);
if(item){const rst=el('button','g-opt g-clear g-reset','↺  Сбросить');rst.type='button';rst.title='Вернуть «не указано»';rst.addEventListener('click',()=>{close(true);save(btn,'',0)});pop.append(rst)}
const row=el('label','g-lvl');row.append(el('span',null,'Заточка +'),input);pop.append(row);
input.addEventListener('change',()=>{const v=clamp(input.value);input.value=v;lastLevel=v;const it=btn.dataset.item;if(it&&it!==NONE)save(btn,it,v)});
input.addEventListener('keydown',e=>{if(e.key==='Enter'){e.preventDefault();input.dispatchEvent(new Event('change'));close(true)}});
pop.hidden=false;place(btn);(first||clr).focus()}
root.addEventListener('click',e=>{const b=e.target.closest('.g-slot');if(!b)return;if(!pop.hidden&&cur===b)close(true);else open(b)});
document.addEventListener('mousedown',e=>{if(!pop.hidden&&!pop.contains(e.target)&&!e.target.closest('.g-slot'))close(false)});
document.addEventListener('keydown',e=>{if(e.key==='Escape'&&!pop.hidden)close(true)});
window.addEventListener('resize',()=>close(false));document.addEventListener('scroll',e=>{if(!pop.hidden&&e.target.classList&&e.target.classList.contains('g-panel'))close(false)},true);
root.querySelectorAll('.g-sp').forEach(i=>{i.addEventListener('input',()=>{const v=i.value.replace(/\D/g,'').slice(0,2);if(v!==i.value)i.value=v});
i.addEventListener('keydown',e=>{if(e.key==='Enter'){e.preventDefault();i.blur()}});
i.addEventListener('change',()=>{i.classList.remove('err');run('Сохранение…',()=>post({player:i.dataset.player,col:'speed',text:i.value})).then(r=>{i.value=r.text;show('Сохранено','ok')},err=>{i.classList.add('err');show((err.message||'Ошибка')+' — не сохранено','err')})})});

/* Перетаскивание ника: игроки меняются местами вместе со строкой (слоты и скорость едут с ними), а на сервере то же
   самое делает запрос /squads/cells/swap со страницы «Отряды». Бросить можно и на пустое место. */
let src=null;
const asEl=n=>n&&(n.nodeType===1?n:n.parentElement);
const rowOf=e=>{const t=asEl(e.target);return t?t.closest('.g-row[data-squad]'):null};
const unmark=()=>{board.classList.remove('g-dragging');board.querySelectorAll('.dragging,.drag-over').forEach(r=>r.classList.remove('dragging','drag-over'))};
function swapRows(a,b){const ac=[...a.childNodes],bc=[...b.childNodes],ap=a.dataset.player,bp=b.dataset.player;
a.replaceChildren(...bc);b.replaceChildren(...ac);a.dataset.player=bp;b.dataset.player=ap;a.classList.toggle('filled',!!bp);b.classList.toggle('filled',!!ap)}
const postSwap=data=>fetch('/squads/cells/swap',{method:'POST',keepalive:true,credentials:'same-origin',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:new URLSearchParams(data)}).then(async r=>{if(!r.ok){const t=(await r.text()).replace(/<[^>]*>/g,' ').trim();throw new Error(t.slice(0,160)||('Ошибка '+r.status))}return r});
board.addEventListener('dragstart',e=>{const n=asEl(e.target)&&asEl(e.target).closest('.g-nick'),r=n&&n.closest('.g-row[data-squad]');
if(!r){e.preventDefault();return}
close(false);src=r;e.dataTransfer.effectAllowed='move';e.dataTransfer.setData('text/plain',n.textContent.trim());
try{e.dataTransfer.setDragImage(r,24,r.offsetHeight/2)}catch(err){}
setTimeout(()=>{if(src===r){r.classList.add('dragging');board.classList.add('g-dragging')}},0)});
board.addEventListener('dragend',()=>{src=null;unmark()});
board.addEventListener('dragover',e=>{const r=rowOf(e);if(!src||!r||r===src)return;e.preventDefault();e.dataTransfer.dropEffect='move';
board.querySelectorAll('.drag-over').forEach(x=>{if(x!==r)x.classList.remove('drag-over')});r.classList.add('drag-over')});
board.addEventListener('drop',e=>{const b=rowOf(e);if(!src||!b||b===src)return;e.preventDefault();const a=src;src=null;unmark();swapRows(a,b);
run('Перемещение…',()=>postSwap({a_squad:a.dataset.squad,a_slot:a.dataset.slot,b_squad:b.dataset.squad,b_slot:b.dataset.slot})).then(
()=>show('Места поменяны','ok'),err=>{show((err.message||'Ошибка')+' — места не изменены, обновляю страницу','err');setTimeout(()=>location.reload(),1500)})});

/* Поиск по нику: совпавшие строки обводятся, остальные тускнеют. */
window.filterPlayers=value=>{const q=value.trim().toLowerCase();
root.querySelectorAll('.g-row').forEach(r=>{const n=r.querySelector('.g-nick'),hit=!!n&&n.textContent.toLowerCase().includes(q);
r.classList.toggle('q-dim',!!q&&!hit);r.classList.toggle('q-hit',!!q&&hit)})};
})();
</script>"""


def _e(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def _slot_state(column: Column, entry: dict[str, Any] | None) -> tuple[str, int]:
    """Ключ предмета и заточка из записи; NONE_KEY — «предмета нет»; предмет, которого нет в настройках категории,
    считается неуказанным."""
    key = entry["item"] if entry else ""
    if key == NONE_KEY:
        return NONE_KEY, 0
    if key not in column.items:
        return "", 0
    return key, int(entry["level"])


def _slot_html(player_id: int, nick: str, column: Column, entry: dict[str, Any] | None, icon_url) -> str:
    key, level = _slot_state(column, entry)
    item = ITEMS.get(key)
    state = ("нет" if key == NONE_KEY else "не указано") if item is None else item.name + (f" +{level}" if level > 0 else "")
    attrs = (f"type='button' data-player='{player_id}' data-col='{column.key}' data-nick='{_e(nick)}' "
             f"data-item='{_e(key)}' data-level='{level}' title='{_e(column.title)}: {_e(state)}' "
             f"aria-label='{_e(column.title)}, {_e(nick)}: {_e(state)}'")
    if key == NONE_KEY:                      # предмета нет: тот же штрихованный фон, но без «+»
        return f"<button class='g-slot none' {attrs}></button>"
    if item is None:
        return f"<button class='g-slot empty' {attrs}><span class='g-plus'>+</span></button>"
    color = RARITY[item.rarity][1]
    if column.show_name:                     # «Снайпы»: внизу иконки название, заточка — только в подсказке и в окне выбора
        badge = f"<span class='g-nm'>{_e(item.name)}</span>"
    else:
        badge = f"<span class='g-lv'>+{level}</span>" if level > 0 else ""
    return (f"<button class='g-slot' style='border-color:{color}' {attrs}>"
            f"<img src='{_e(icon_url(item.icon))}' alt=''>{badge}</button>")


def _config(icon_url) -> str:
    cfg = {
        "maxLevel": MAX_LEVEL,
        "noneKey": NONE_KEY,
        "columns": [{"key": c.key, "title": c.title, "items": list(c.items), "showName": c.show_name} for c in COLUMNS],
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


def _squad_title(squad_id: int) -> str:
    return roster_board.LABELS[squad_id] if squad_id == roster_board.SUBS_ID else f"Отряд {squad_id}"


def _player_cells(pid: int, nick: str, gear: dict[tuple[int, str], dict[str, Any]], icons) -> str:
    """Слоты категорий и скорость одного игрока — в порядке COLUMNS, сразу после ника."""
    cells = []
    for column in COLUMNS:
        entry = gear.get((pid, column.key))
        if column.is_text:
            text = entry["text"] if entry else ""
            old_format = bool(text) and not SPEED_RE.fullmatch(text)     # запись из старой версии, до правила 10–99
            cls, tip = ("g-sp err", f" title='{_e(SPEED_HINT)}'") if old_format else ("g-sp", "")
            cells.append(f"<input class='{cls}' data-player='{pid}' placeholder='—' "
                         f"inputmode='numeric' autocomplete='off' maxlength='2' value='{_e(text)}'{tip} "
                         f"aria-label='Скорость, {_e(nick)}'>")
        else:
            cells.append(_slot_html(pid, nick, column, entry, icons))
    return "".join(cells)


def _row_html(squad_id: int, slot: int, player: dict[str, Any] | None, gear, icons) -> str:
    """Одно место доски: ник + слоты, а у пустого места — просто пустая ячейка (сюда можно бросить ник)."""
    pos = f"data-squad='{squad_id}' data-slot='{slot}'"
    if player is None:
        return f"<div class='g-row' {pos} data-player='' aria-label='{_e(_squad_title(squad_id))}, место {slot}: пусто'></div>"
    pid, nick = int(player["id"]), str(player["canonical_nick"])
    return (f"<div class='g-row filled' {pos} data-player='{pid}'>"
            f"<div class='g-nick' draggable='true' title='{_e(nick)}'>"
            f"<a href='{url_for('player', player_id=pid)}' draggable='false'>{_e(nick)}</a></div>"
            f"{_player_cells(pid, nick, gear, icons)}</div>")


def _head_html(squad_id: int) -> str:
    labels = "".join(f"<span class='gb-l' title='{_e(c.title)}'>{_e(c.label)}</span>" for c in COLUMNS)
    return f"<div class='gb-h'><span class='gb-t'>{_e(_squad_title(squad_id))}</span>{labels}</div>"


def _board_cells(squad_ids: tuple[int, int | None], placed, gear, icons) -> list[str]:
    """12 ячеек одной колонки доски сверху вниз. У отрядов: шапка, 5 мест, шапка нижнего отряда, 5 мест.
    У «Замен»: шапка, 5 мест, 6-е место в полосе между рядами, ещё 5 мест — линии мест идут вровень с отрядами."""
    top, bottom = squad_ids
    row = lambda squad, slot: _row_html(squad, slot, placed.get((squad, slot)), gear, icons)
    if bottom is None:
        return [_head_html(top)] + [row(top, slot) for slot in roster_board.SUBS_SLOTS]
    return ([_head_html(top)] + [row(top, slot) for slot in roster_board.SLOTS]
            + [_head_html(bottom)] + [row(bottom, slot) for slot in roster_board.SLOTS])


def render_body(admin: Any, roster: list[dict[str, Any]], gear: dict[tuple[int, str], dict[str, Any]]) -> str:
    icons = {name: _icon_url(name) for name in ICON_FILES}.__getitem__     # url иконки считаем один раз на страницу
    placed, loose = place_on_board(roster)
    head = (f"<div class='pagehead g-top'><h1>Шмот</h1><div class='toolbar' style='margin:0'>{admin._search_box()}</div></div>")
    if not roster:
        return head + "<p class='hint'>В отрядах пока никого нет.</p>"
    cells = "".join("".join(_board_cells(column, placed, gear, icons)) for column in BOARD_COLUMNS)
    extra = ""
    if loose:
        rows = []
        for player in loose:
            pid, nick = int(player["id"]), str(player["canonical_nick"])
            squad = _squad_title(int(player["squad_id"]))
            rows.append(f"<div class='g-row filled' data-player='{pid}'><div class='g-nick' title='{_e(squad)}, место не задано'>"
                        f"<a href='{url_for('player', player_id=pid)}' draggable='false'>{_e(nick)}</a></div>"
                        f"{_player_cells(pid, nick, gear, icons)}</div>")
        extra = (f"<section class='g-extra'><h2>Без места на доске</h2>"
                 f"<div class='panel g-panel'><div class='gb-x'>{''.join(rows)}</div></div></section>")
    return (head
            + f"<div id='gear-root'><div class='panel g-panel'><div id='gear-board' class='gb'>{cells}</div></div>{extra}</div>"
            + "<div id='gear-pop' class='g-pop' hidden role='dialog' aria-label='Выбор предмета'></div>"
            "<div id='gear-status' class='g-status' role='status' aria-live='polite'></div>"
            f"<script type='application/json' id='gear-cfg'>{_config(icons)}</script>{SCRIPT}")


# ------------------------------------------------------------- админ-панель --

def install(admin: Any) -> None:
    if getattr(admin.app, "_gear_installed", False):
        return
    admin.app._gear_installed = True
    admin.add_css(CSS)
    admin.register_nav("Шмот", "gear_page", order=50, prefixes=("/gear",))

    @admin.app.get("/gear")
    def gear_page():
        with closing(admin.player_store.connect(admin.DB_PATH)) as con:
            _schema(con)
            roster = ordered_roster(admin, con)
            gear = load_gear(con)
        return admin.render("Шмот", render_body(admin, roster, gear), body_class="g-wide")

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
