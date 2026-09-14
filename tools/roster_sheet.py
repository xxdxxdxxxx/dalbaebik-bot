"""Spreadsheet-like roster board backed by SQLite roster positions."""
from __future__ import annotations
import html, sqlite3
from contextlib import closing
from flask import request
ORDER=(1,2,3,99,4,5,6);LABEL={1:'1',2:'2',3:'3',99:'Замены',4:'4',5:'5',6:'6'}
CSS=r'''
.sheet-help{color:var(--dim);margin:3px 0 14px}.sheet-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:0;border:1px solid var(--line);border-radius:9px;overflow:hidden;background:var(--panel)}.sheet-col{min-width:0;border-right:1px solid var(--line)}.sheet-col:nth-child(4),.sheet-col:last-child{border-right:0}.sheet-col.bottom{border-top:1px solid var(--line)}.sheet-head{height:38px;display:grid;place-items:center;background:var(--soft);border-bottom:1px solid var(--line);font-size:14px;font-weight:800;text-align:center}.sheet-cell{display:block;width:100%;height:36px;border:0;border-bottom:1px solid var(--line);border-radius:0;background:var(--panel);color:var(--text);font:650 13px/36px -apple-system,BlinkMacSystemFont,"Segoe UI",Arial,sans-serif;text-align:center;padding:0 8px;outline:0}.sheet-cell:last-child{border-bottom:0}.sheet-cell:focus{background:var(--blue2);box-shadow:inset 0 0 0 1px var(--blue)}.sheet-cell.dragging{opacity:.4}.sheet-cell.drag-over{background:var(--blue2);box-shadow:inset 0 0 0 2px var(--blue)}.sheet-status{height:20px;margin-top:8px;color:var(--dim);font-size:11px;text-align:right}.sheet-status.ok{color:var(--green)}.sheet-status.err{color:var(--red)}@media(max-width:760px){.sheet-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.sheet-col:nth-child(2n){border-right:0}.sheet-col:nth-child(n+3){border-top:1px solid var(--line)}}@media(max-width:430px){.sheet-grid{grid-template-columns:1fr}.sheet-col{border-right:0!important;border-top:1px solid var(--line)}.sheet-col:first-child{border-top:0}}
'''
JS=r'''<script>
(()=>{const cells=[...document.querySelectorAll('.sheet-cell')],status=document.getElementById('sheet-status');let source=null,busy=false;const post=(url,data)=>fetch(url,{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:new URLSearchParams(data)});const save=async c=>{if(busy)return;status.textContent='Сохранение…';status.className='sheet-status';try{const r=await post('/squads/cell',{squad:c.dataset.squad,slot:c.dataset.slot,text:c.value});if(!r.ok)throw new Error(await r.text());status.textContent='Сохранено';status.className='sheet-status ok'}catch(e){status.textContent=e.message||'Ошибка сохранения';status.className='sheet-status err'}};cells.forEach(c=>{c.addEventListener('change',()=>save(c));c.addEventListener('keydown',e=>{if(e.key==='Enter'){e.preventDefault();c.blur()}});c.addEventListener('dragstart',e=>{source=c;c.classList.add('dragging');e.dataTransfer.effectAllowed='move';e.dataTransfer.setData('text/plain','cell')});c.addEventListener('dragend',()=>{cells.forEach(x=>x.classList.remove('dragging','drag-over'));source=null});c.addEventListener('dragover',e=>{if(!source||source===c)return;e.preventDefault();cells.forEach(x=>x.classList.remove('drag-over'));c.classList.add('drag-over')});c.addEventListener('drop',async e=>{if(!source||source===c)return;e.preventDefault();const a=source,b=c,av=a.value,bv=b.value;a.value=bv;b.value=av;busy=true;try{const r=await post('/squads/cells/swap',{a_squad:a.dataset.squad,a_slot:a.dataset.slot,b_squad:b.dataset.squad,b_slot:b.dataset.slot});if(!r.ok)throw new Error(await r.text());status.textContent='Места поменяны';status.className='sheet-status ok'}catch(err){a.value=av;b.value=bv;status.textContent=err.message||'Ошибка';status.className='sheet-status err'}finally{busy=false;cells.forEach(x=>x.classList.remove('dragging','drag-over'))}})});})();
</script>'''
def _gid(c):
 r=c.execute('SELECT guild_id FROM bot_guild_config ORDER BY (guild_id=0),guild_id LIMIT 1').fetchone();return int(r[0]) if r else None
def _schema(c):c.execute("CREATE TABLE IF NOT EXISTS roster_board_cells(guild_id INTEGER NOT NULL,squad_id INTEGER NOT NULL,slot INTEGER NOT NULL,text TEXT NOT NULL DEFAULT '',updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,PRIMARY KEY(guild_id,squad_id,slot))")
def _exact(ps,c,text):
 ids=ps._identity_ids(c,text)
 if len(ids)!=1:return None
 p=next(iter(ids));return p if c.execute('SELECT 1 FROM discord_bindings WHERE player_id=?',(p,)).fetchone() else None
def _seed(a,c,g):
 _schema(c);c.execute("INSERT OR IGNORE INTO roster_board_cells(guild_id,squad_id,slot,text) SELECT r.guild_id,r.squad_id,r.slot,p.canonical_nick FROM roster_memberships r JOIN players p ON p.id=r.player_id WHERE r.guild_id=? AND r.active=1 AND (r.squad_id BETWEEN 1 AND 6 OR r.squad_id=99) AND r.slot IS NOT NULL",(g,))
def _set(a,c,g,s,slot,text):
 text=' '.join(str(text or '').strip().split());old=c.execute('SELECT player_id FROM roster_memberships WHERE guild_id=? AND squad_id=? AND slot=? AND active=1',(g,s,slot)).fetchone();oldpid=int(old[0]) if old else None;p=_exact(a.player_store,c,text) if text else None
 if oldpid is not None and oldpid!=p:c.execute('UPDATE roster_memberships SET active=0,deactivated_at=COALESCE(deactivated_at,CURRENT_TIMESTAMP),updated_at=CURRENT_TIMESTAMP WHERE guild_id=? AND player_id=?',(g,oldpid))
 if p is not None:
  previous=c.execute('SELECT squad_id,slot FROM roster_memberships WHERE guild_id=? AND player_id=? AND active=1',(g,p)).fetchone()
  if previous and (int(previous[0])!=s or int(previous[1])!=slot):c.execute("UPDATE roster_board_cells SET text='',updated_at=CURRENT_TIMESTAMP WHERE guild_id=? AND squad_id=? AND slot=?",(g,int(previous[0]),int(previous[1])))
  c.execute('UPDATE roster_memberships SET active=0,squad_id=NULL,slot=NULL,updated_at=CURRENT_TIMESTAMP WHERE guild_id=? AND player_id=?',(g,p));c.execute('DELETE FROM roster_removals WHERE guild_id=? AND player_id=?',(g,p));c.execute('INSERT INTO roster_memberships(guild_id,player_id,squad_id,slot,active,deactivated_at) VALUES(?,?,?,?,1,NULL) ON CONFLICT(guild_id,player_id) DO UPDATE SET squad_id=excluded.squad_id,slot=excluded.slot,active=1,deactivated_at=NULL,updated_at=CURRENT_TIMESTAMP',(g,p,s,slot))
 c.execute('INSERT INTO roster_board_cells(guild_id,squad_id,slot,text) VALUES(?,?,?,?) ON CONFLICT(guild_id,squad_id,slot) DO UPDATE SET text=excluded.text,updated_at=CURRENT_TIMESTAMP',(g,s,slot,text))
def install(a):
 if getattr(a.app,'_roster_sheet_installed',False):return
 a.app._roster_sheet_installed=True;a.PAGE=a.PAGE.replace('</style>',CSS.replace('{','{{').replace('}','}}')+'\n</style>')
 def page():
  with closing(a.player_store.connect(a.DB_PATH)) as c:
   g=_gid(c)
   if g is None:return a.render('Отряды','<h1>Отряды</h1><p>Сначала выполните /setup в Discord.</p>')
   _seed(a,c,g);rows=list(c.execute('SELECT squad_id,slot,text FROM roster_board_cells WHERE guild_id=?',(g,)));maximum=max([10,*[int(r['slot']) for r in rows]]);values={(int(r['squad_id']),int(r['slot'])):str(r['text']) for r in rows}
  cols=[]
  for i,s in enumerate(ORDER):
   cells=''.join(f"<input class='sheet-cell' draggable='true' data-squad='{s}' data-slot='{slot}' value='{html.escape(values.get((s,slot),''),quote=True)}' aria-label='{LABEL[s]}, место {slot}'>" for slot in range(1,maximum+1));cols.append(f"<section class='sheet-col {'bottom' if i>=4 else ''}'><div class='sheet-head'>{LABEL[s]}</div>{cells}</section>")
  body="<h1>Отряды</h1><div class='sheet-help'>Нажмите на ячейку и пишите. Пустая ячейка остаётся на месте. Известные ники обновляют настоящий состав, остальные можно использовать как заметки.</div><div class='sheet-grid'>"+''.join(cols)+"</div><div id='sheet-status' class='sheet-status'></div>"+JS
  return a.render('Отряды',body)
 a.app.view_functions['squads']=page
 @a.app.post('/squads/cell')
 def roster_cell():
  try:
   s=int(request.form['squad']);slot=int(request.form['slot'])
   if s not in ORDER or slot<1:raise ValueError('Неверная ячейка')
   with closing(a.player_store.connect(a.DB_PATH)) as c:c.execute('BEGIN IMMEDIATE');g=_gid(c);_seed(a,c,g);_set(a,c,g,s,slot,request.form.get('text',''));c.commit()
   return '',204
  except (ValueError,sqlite3.IntegrityError) as e:return str(e),400
 @a.app.post('/squads/cells/swap')
 def roster_cells_swap():
  try:
   x=(int(request.form['a_squad']),int(request.form['a_slot']));y=(int(request.form['b_squad']),int(request.form['b_slot']))
   if x[0] not in ORDER or y[0] not in ORDER or min(x[1],y[1])<1:raise ValueError('Неверные ячейки')
   with closing(a.player_store.connect(a.DB_PATH)) as c:
    c.execute('BEGIN IMMEDIATE');g=_gid(c);_seed(a,c,g);texts=[];players=[]
    for s,slot in (x,y):
     r=c.execute('SELECT text FROM roster_board_cells WHERE guild_id=? AND squad_id=? AND slot=?',(g,s,slot)).fetchone();texts.append(str(r[0]) if r else '');p=c.execute('SELECT player_id FROM roster_memberships WHERE guild_id=? AND squad_id=? AND slot=? AND active=1',(g,s,slot)).fetchone();players.append(int(p[0]) if p else None)
    for p in players:
     if p is not None:c.execute('UPDATE roster_memberships SET active=0,squad_id=NULL,slot=NULL,updated_at=CURRENT_TIMESTAMP WHERE guild_id=? AND player_id=?',(g,p))
    for (s,slot),text,p in ((x,texts[1],players[1]),(y,texts[0],players[0])):
     c.execute('INSERT INTO roster_board_cells(guild_id,squad_id,slot,text) VALUES(?,?,?,?) ON CONFLICT(guild_id,squad_id,slot) DO UPDATE SET text=excluded.text,updated_at=CURRENT_TIMESTAMP',(g,s,slot,text))
     if p is not None:c.execute('UPDATE roster_memberships SET squad_id=?,slot=?,active=1,deactivated_at=NULL,updated_at=CURRENT_TIMESTAMP WHERE guild_id=? AND player_id=?',(s,slot,g,p))
    c.commit()
   return '',204
  except (ValueError,sqlite3.IntegrityError) as e:return str(e),400
