"""Single SQLite-backed roster board for the local admin panel."""
from __future__ import annotations

import html
import sqlite3
from contextlib import closing

from flask import request, url_for

ORDER = (1, 2, 3, 99, 4, 5, 6)
TOP = (1, 2, 3, 99)
BOTTOM = (4, 5, 6)
NOTE_COLUMNS = (1, 2, 3, 99)
LABELS = {1: "1", 2: "2", 3: "3", 99: "Замены", 4: "4", 5: "5", 6: "6"}
SLOTS = range(1, 6)
NOTE_ROWS = (1, 2)

CSS = r"""
.layout-squads{max-width:940px}.sheet-help{color:var(--dim);margin:3px 0 14px}.squads-zone{border:1px solid var(--line);border-radius:9px;overflow:hidden;background:var(--panel)}.sheet-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));background:var(--panel)}.sheet-grid-bottom{width:75%;grid-template-columns:repeat(3,minmax(0,1fr));border-top:1px solid var(--line)}.sheet-col{min-width:0;border-right:1px solid var(--line)}.sheet-col:last-child{border-right:0}.sheet-grid-bottom .sheet-col:last-child{border-right:1px solid var(--line)}.sheet-head{height:38px;display:grid;place-items:center;background:var(--soft);border-bottom:1px solid var(--line);font-size:14px;font-weight:800;text-align:center}.sheet-cell,.sheet-note{display:block;width:100%;height:36px;border:0;border-bottom:1px solid rgba(127,127,127,.17);border-radius:0;background:var(--panel);color:var(--text);font:550 14px/36px -apple-system,BlinkMacSystemFont,"Segoe UI",Arial,sans-serif;text-align:center;padding:0 8px;outline:0}.sheet-cell:last-child,.sheet-note:last-child{border-bottom:0}.sheet-cell:focus,.sheet-note:focus{background:var(--blue2);box-shadow:inset 0 0 0 1px var(--blue)}.sheet-cell.dragging{opacity:.4}.sheet-cell.drag-over{background:var(--blue2);box-shadow:inset 0 0 0 2px var(--blue)}.notes-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));margin-top:16px;border:1px solid var(--line);border-radius:9px;overflow:hidden;background:var(--panel)}.note-col{min-width:0;border-right:1px solid var(--line)}.note-col:last-child{border-right:0}.sheet-status{height:20px;margin-top:8px;color:var(--dim);font-size:11px;text-align:right}.sheet-status.ok{color:var(--green)}.sheet-status.err{color:var(--red)}
@media(max-width:760px){.sheet-grid,.notes-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.sheet-grid-bottom{width:100%}.sheet-col:nth-child(2n),.note-col:nth-child(2n){border-right:0}.sheet-col:nth-child(n+3),.note-col:nth-child(n+3){border-top:1px solid var(--line)}}@media(max-width:430px){.sheet-grid,.notes-grid{grid-template-columns:1fr}.sheet-col,.note-col{border-right:0!important;border-top:1px solid var(--line)}.sheet-col:first-child,.note-col:first-child{border-top:0}}
"""

SCRIPT = r"""<script>
(()=>{const status=document.getElementById('sheet-status'),cells=[...document.querySelectorAll('.sheet-cell')];let source=null,busy=false;const post=(url,data)=>fetch(url,{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:new URLSearchParams(data)});const show=(text,kind='')=>{status.textContent=text;status.className='sheet-status '+kind};const save=async c=>{if(busy)return;show('Сохранение…');try{const r=await post('/squads/cell',{squad:c.dataset.squad,slot:c.dataset.slot,text:c.value});if(!r.ok)throw new Error(await r.text());show('Сохранено','ok')}catch(e){show(e.message||'Ошибка сохранения','err')}};cells.forEach(c=>{c.addEventListener('change',()=>save(c));c.addEventListener('keydown',e=>{if(e.key==='Enter'){e.preventDefault();c.blur()}});c.addEventListener('dragstart',e=>{source=c;c.classList.add('dragging');e.dataTransfer.effectAllowed='move';e.dataTransfer.setData('text/plain','cell')});c.addEventListener('dragend',()=>{cells.forEach(x=>x.classList.remove('dragging','drag-over'));source=null});c.addEventListener('dragover',e=>{if(!source||source===c)return;e.preventDefault();cells.forEach(x=>x.classList.remove('drag-over'));c.classList.add('drag-over')});c.addEventListener('drop',async e=>{if(!source||source===c)return;e.preventDefault();const a=source,b=c,av=a.value,bv=b.value;a.value=bv;b.value=av;busy=true;try{const r=await post('/squads/cells/swap',{a_squad:a.dataset.squad,a_slot:a.dataset.slot,b_squad:b.dataset.squad,b_slot:b.dataset.slot});if(!r.ok)throw new Error(await r.text());show('Места поменяны','ok')}catch(err){a.value=av;b.value=bv;show(err.message||'Ошибка','err')}finally{busy=false;cells.forEach(x=>x.classList.remove('dragging','drag-over'))}})});document.querySelectorAll('.sheet-note').forEach(c=>{c.addEventListener('keydown',e=>{if(e.key==='Enter'){e.preventDefault();c.blur()}});c.addEventListener('change',async()=>{show('Сохранение заметки…');try{const r=await post('/squads/note',{column:c.dataset.column,row:c.dataset.row,text:c.value});if(!r.ok)throw new Error(await r.text());show('Сохранено','ok')}catch(e){show(e.message||'Ошибка сохранения','err')}})})})();
</script>"""


def _schema(con):
    con.executescript("""
        CREATE TABLE IF NOT EXISTS roster_board_cells(
            guild_id INTEGER NOT NULL, squad_id INTEGER NOT NULL, slot INTEGER NOT NULL,
            text TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(guild_id,squad_id,slot));
        CREATE TABLE IF NOT EXISTS roster_board_notes(
            guild_id INTEGER NOT NULL, column_id INTEGER NOT NULL, row_no INTEGER NOT NULL,
            text TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(guild_id,column_id,row_no));
    """)


def _guild_id(con):
    row = con.execute("SELECT guild_id FROM bot_guild_config ORDER BY (guild_id=0),guild_id LIMIT 1").fetchone()
    return int(row[0]) if row else None


def _clean(value):
    return " ".join(str(value or "").strip().split())


def _seed(con, guild_id):
    _schema(con)
    con.execute("""INSERT OR IGNORE INTO roster_board_cells(guild_id,squad_id,slot,text)
        SELECT r.guild_id,r.squad_id,r.slot,p.canonical_nick
        FROM roster_memberships r JOIN players p ON p.id=r.player_id
        WHERE r.guild_id=? AND r.active=1 AND (r.squad_id BETWEEN 1 AND 6 OR r.squad_id=99)
          AND r.slot BETWEEN 1 AND 5""", (guild_id,))
    con.executemany("INSERT OR IGNORE INTO roster_board_cells(guild_id,squad_id,slot,text) VALUES(?,?,?,'')",
                    [(guild_id, squad_id, slot) for squad_id in ORDER for slot in SLOTS])


def _find_or_create(admin, con, guild_id, nickname):
    ids = admin.player_store._identity_ids(con, nickname)
    if len(ids) > 1:
        raise ValueError("Ник совпадает с несколькими игроками")
    if ids:
        player_id = next(iter(ids))
    else:
        con.execute("INSERT INTO players(canonical_nick) VALUES(?)", (nickname,))
        player_id = int(con.execute("SELECT last_insert_rowid()").fetchone()[0])
    con.execute("INSERT OR IGNORE INTO player_guilds(player_id,guild_id) VALUES(?,?)", (player_id, guild_id))
    return player_id


def _set_cell(admin, con, guild_id, squad_id, slot, value):
    text = _clean(value)
    old = con.execute("SELECT player_id FROM roster_memberships WHERE guild_id=? AND squad_id=? AND slot=? AND active=1",
                      (guild_id, squad_id, slot)).fetchone()
    old_player = int(old[0]) if old else None
    player_id = _find_or_create(admin, con, guild_id, text) if text else None
    if old_player is not None and old_player != player_id:
        con.execute("UPDATE roster_memberships SET active=0,deactivated_at=COALESCE(deactivated_at,CURRENT_TIMESTAMP),updated_at=CURRENT_TIMESTAMP WHERE guild_id=? AND player_id=?",
                    (guild_id, old_player))
    if player_id is not None:
        previous = con.execute("SELECT squad_id,slot FROM roster_memberships WHERE guild_id=? AND player_id=? AND active=1",
                               (guild_id, player_id)).fetchone()
        if previous and previous[0] is not None and previous[1] is not None:
            previous_position = (int(previous[0]), int(previous[1]))
            if previous_position != (squad_id, slot):
                con.execute("UPDATE roster_board_cells SET text='',updated_at=CURRENT_TIMESTAMP WHERE guild_id=? AND squad_id=? AND slot=?",
                            (guild_id, *previous_position))
        con.execute("UPDATE roster_memberships SET active=0,squad_id=NULL,slot=NULL,updated_at=CURRENT_TIMESTAMP WHERE guild_id=? AND player_id=?",
                    (guild_id, player_id))
        con.execute("DELETE FROM roster_removals WHERE guild_id=? AND player_id=?", (guild_id, player_id))
        con.execute("""INSERT INTO roster_memberships(guild_id,player_id,squad_id,slot,active,deactivated_at)
            VALUES(?,?,?,?,1,NULL) ON CONFLICT(guild_id,player_id) DO UPDATE SET
            squad_id=excluded.squad_id,slot=excluded.slot,active=1,deactivated_at=NULL,updated_at=CURRENT_TIMESTAMP""",
                    (guild_id, player_id, squad_id, slot))
    con.execute("""INSERT INTO roster_board_cells(guild_id,squad_id,slot,text) VALUES(?,?,?,?)
        ON CONFLICT(guild_id,squad_id,slot) DO UPDATE SET text=excluded.text,updated_at=CURRENT_TIMESTAMP""",
                (guild_id, squad_id, slot, text))


def _column(values, squad_id):
    cells = "".join(
        f"<input class='sheet-cell' draggable='true' data-squad='{squad_id}' data-slot='{slot}' "
        f"value='{html.escape(values.get((squad_id, slot), ''), quote=True)}' aria-label='{LABELS[squad_id]}, место {slot}'>"
        for slot in SLOTS
    )
    return f"<section class='sheet-col'><div class='sheet-head'>{LABELS[squad_id]}</div>{cells}</section>"


def install(admin):
    if getattr(admin.app, "_roster_board_installed", False):
        return
    admin.app._roster_board_installed = True
    admin.PAGE = admin.PAGE.replace("</style>", CSS.replace("{", "{{").replace("}", "}}") + "\n</style>")
    base_render = admin.render

    def render(title, body):
        page = base_render(title, body)
        active = "active" if request.path.startswith("/squads") else ""
        link = f"<a class='{active}' href='{url_for('squads')}'>Отряды</a>"
        return page.replace("</nav></div></header>", link + "</nav></div></header>", 1)

    admin.render = render

    @admin.app.get("/squads")
    def squads():
        with closing(admin.player_store.connect(admin.DB_PATH)) as con:
            guild_id = _guild_id(con)
            if guild_id is None:
                return admin.render("Отряды", "<h1>Отряды</h1><p>Сначала выполните /setup в Discord.</p>")
            _seed(con, guild_id)
            rows = list(con.execute("SELECT squad_id,slot,text FROM roster_board_cells WHERE guild_id=?", (guild_id,)))
            notes = list(con.execute("SELECT column_id,row_no,text FROM roster_board_notes WHERE guild_id=?", (guild_id,)))
        values = {(int(row["squad_id"]), int(row["slot"])): str(row["text"]) for row in rows}
        note_values = {(int(row["column_id"]), int(row["row_no"])): str(row["text"]) for row in notes}
        top = "".join(_column(values, squad_id) for squad_id in TOP)
        bottom = "".join(_column(values, squad_id) for squad_id in BOTTOM)
        note_columns = []
        for column in NOTE_COLUMNS:
            fields = "".join(
                f"<input class='sheet-note' data-column='{column}' data-row='{row}' "
                f"value='{html.escape(note_values.get((column, row), ''), quote=True)}' aria-label='Заметка {column}, строка {row}'>"
                for row in NOTE_ROWS
            )
            note_columns.append(f"<section class='note-col'>{fields}</section>")
        body = (
            "<h1>Отряды</h1><div class='sheet-help'>Нажмите на ячейку и пишите. Пустая ячейка остаётся на месте.</div>"
            f"<div class='squads-zone'><div class='sheet-grid'>{top}</div><div class='sheet-grid sheet-grid-bottom'>{bottom}</div></div>"
            f"<div class='notes-grid'>{''.join(note_columns)}</div><div id='sheet-status' class='sheet-status'></div>{SCRIPT}"
        )
        return admin.render("Отряды", body)

    @admin.app.post("/squads/cell")
    def roster_cell():
        try:
            squad_id, slot = int(request.form["squad"]), int(request.form["slot"])
            if squad_id not in ORDER or slot not in SLOTS:
                raise ValueError("Неверная ячейка")
            with closing(admin.player_store.connect(admin.DB_PATH)) as con:
                con.execute("BEGIN IMMEDIATE")
                guild_id = _guild_id(con)
                if guild_id is None:
                    raise ValueError("Сервер Discord не настроен")
                _seed(con, guild_id)
                _set_cell(admin, con, guild_id, squad_id, slot, request.form.get("text", ""))
                con.commit()
            return "", 204
        except (ValueError, sqlite3.IntegrityError) as exc:
            return str(exc), 400

    @admin.app.post("/squads/cells/swap")
    def roster_cells_swap():
        try:
            left = (int(request.form["a_squad"]), int(request.form["a_slot"]))
            right = (int(request.form["b_squad"]), int(request.form["b_slot"]))
            if left[0] not in ORDER or right[0] not in ORDER or left[1] not in SLOTS or right[1] not in SLOTS:
                raise ValueError("Неверные ячейки")
            with closing(admin.player_store.connect(admin.DB_PATH)) as con:
                con.execute("BEGIN IMMEDIATE")
                guild_id = _guild_id(con)
                if guild_id is None:
                    raise ValueError("Сервер Discord не настроен")
                _seed(con, guild_id)
                texts, players = [], []
                for squad_id, slot in (left, right):
                    row = con.execute("SELECT text FROM roster_board_cells WHERE guild_id=? AND squad_id=? AND slot=?",
                                      (guild_id, squad_id, slot)).fetchone()
                    texts.append(str(row[0]) if row else "")
                    row = con.execute("SELECT player_id FROM roster_memberships WHERE guild_id=? AND squad_id=? AND slot=? AND active=1",
                                      (guild_id, squad_id, slot)).fetchone()
                    players.append(int(row[0]) if row else None)
                for player_id in players:
                    if player_id is not None:
                        con.execute("UPDATE roster_memberships SET active=0,squad_id=NULL,slot=NULL,updated_at=CURRENT_TIMESTAMP WHERE guild_id=? AND player_id=?",
                                    (guild_id, player_id))
                for (squad_id, slot), text, player_id in ((left, texts[1], players[1]), (right, texts[0], players[0])):
                    con.execute("UPDATE roster_board_cells SET text=?,updated_at=CURRENT_TIMESTAMP WHERE guild_id=? AND squad_id=? AND slot=?",
                                (text, guild_id, squad_id, slot))
                    if player_id is not None:
                        con.execute("UPDATE roster_memberships SET squad_id=?,slot=?,active=1,deactivated_at=NULL,updated_at=CURRENT_TIMESTAMP WHERE guild_id=? AND player_id=?",
                                    (squad_id, slot, guild_id, player_id))
                con.commit()
            return "", 204
        except (ValueError, sqlite3.IntegrityError) as exc:
            return str(exc), 400

    @admin.app.post("/squads/note")
    def roster_note():
        try:
            column, row = int(request.form["column"]), int(request.form["row"])
            if column not in NOTE_COLUMNS or row not in NOTE_ROWS:
                raise ValueError("Неверная ячейка заметки")
            text = _clean(request.form.get("text", ""))
            with closing(admin.player_store.connect(admin.DB_PATH)) as con:
                con.execute("BEGIN IMMEDIATE")
                _schema(con)
                guild_id = _guild_id(con)
                if guild_id is None:
                    raise ValueError("Сервер Discord не настроен")
                con.execute("""INSERT INTO roster_board_notes(guild_id,column_id,row_no,text) VALUES(?,?,?,?)
                    ON CONFLICT(guild_id,column_id,row_no) DO UPDATE SET text=excluded.text,updated_at=CURRENT_TIMESTAMP""",
                            (guild_id, column, row, text))
                con.commit()
            return "", 204
        except (ValueError, sqlite3.IntegrityError) as exc:
            return str(exc), 400
