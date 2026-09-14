"""Two-row free-form notes below all roster squads."""
from __future__ import annotations
import html, sqlite3
from contextlib import closing
from flask import request
COLUMNS=(1,2,3,99)
CSS=r'''
.sheet-cell{border-bottom-color:rgba(127,127,127,.17)}.sheet-note-col{min-width:0;border-right:1px solid var(--line);border-top:10px solid var(--bg);border-bottom:10px solid var(--bg);background:var(--panel)}.sheet-note-col:last-child{border-right:0}.sheet-note{display:block;width:100%;height:36px;border:0;border-bottom:1px solid rgba(127,127,127,.17);border-radius:0;background:var(--panel);color:var(--text);font:650 13px/36px -apple-system,BlinkMacSystemFont,"Segoe UI",Arial,sans-serif;text-align:center;padding:0 8px;outline:0}.sheet-note:last-child{border-bottom:0}.sheet-note:focus{background:var(--blue2);box-shadow:inset 0 0 0 1px var(--blue)}@media(max-width:760px){.sheet-note-col{border-top-width:8px;border-bottom-width:8px}.sheet-note-col:nth-child(2n){border-right:0}}@media(max-width:430px){.sheet-note-col{border-right:0;border-top:8px solid var(--bg);border-bottom:0}}
'''
JS=r'''<script>
(()=>{const status=document.getElementById('sheet-status');document.querySelectorAll('.sheet-note').forEach(cell=>{cell.addEventListener('keydown',e=>{if(e.key==='Enter'){e.preventDefault();cell.blur()}});cell.addEventListener('change',async()=>{status.textContent='Сохранение заметки…';status.className='sheet-status';try{const body=new URLSearchParams({column:cell.dataset.column,row:cell.dataset.row,text:cell.value}),response=await fetch('/squads/note',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body});if(!response.ok)throw new Error(await response.text());status.textContent='Сохранено';status.className='sheet-status ok'}catch(error){status.textContent=error.message||'Ошибка сохранения';status.className='sheet-status err'}})})})();
</script>'''
def _schema(c):c.execute("CREATE TABLE IF NOT EXISTS roster_board_notes(guild_id INTEGER NOT NULL,column_id INTEGER NOT NULL,row_no INTEGER NOT NULL,text TEXT NOT NULL DEFAULT '',updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,PRIMARY KEY(guild_id,column_id,row_no))")
def _guild(c):
 r=c.execute('SELECT guild_id FROM bot_guild_config ORDER BY (guild_id=0),guild_id LIMIT 1').fetchone();return int(r[0]) if r else None
def install(a):
 if getattr(a.app,'_roster_notes_installed',False):return
 a.app._roster_notes_installed=True;a.PAGE=a.PAGE.replace('</style>',CSS.replace('{','{{').replace('}','}}')+'\n</style>');base=a.render
 def render(title,body):
  if request.path.startswith('/squads'):
   with closing(a.player_store.connect(a.DB_PATH)) as c:_schema(c);g=_guild(c);rows=[] if g is None else list(c.execute('SELECT column_id,row_no,text FROM roster_board_notes WHERE guild_id=?',(g,)))
   values={(int(r['column_id']),int(r['row_no'])):str(r['text']) for r in rows};notes=[]
   for column in COLUMNS:
    cells=''.join(f"<input class='sheet-note' data-column='{column}' data-row='{row}' value='{html.escape(values.get((column,row),''),quote=True)}' aria-label='Заметка {column}, строка {row}'>" for row in (1,2));notes.append(f"<section class='sheet-note-col'>{cells}</section>")
   marker="</div><div id='sheet-status' class='sheet-status'>";body=body.replace(marker,''.join(notes)+marker,1)+JS
  return base(title,body)
 a.render=render
 @a.app.post('/squads/note')
 def roster_note():
  try:
   column=int(request.form['column']);row=int(request.form['row'])
   if column not in COLUMNS or row not in (1,2):raise ValueError('Неверная ячейка заметки')
   text=' '.join(request.form.get('text','').strip().split())
   with closing(a.player_store.connect(a.DB_PATH)) as c:
    c.execute('BEGIN IMMEDIATE');_schema(c);g=_guild(c)
    if g is None:raise ValueError('Сервер Discord не настроен')
    c.execute('INSERT INTO roster_board_notes(guild_id,column_id,row_no,text) VALUES(?,?,?,?) ON CONFLICT(guild_id,column_id,row_no) DO UPDATE SET text=excluded.text,updated_at=CURRENT_TIMESTAMP',(g,column,row,text));c.commit()
   return '',204
  except (ValueError,sqlite3.IntegrityError) as exc:return str(exc),400
