"""Equal-column roster layout and drag-to-swap behavior."""
from __future__ import annotations
import sqlite3
from contextlib import closing
from flask import request

CSS=r'''
.roster-grid{grid-template-columns:repeat(4,minmax(0,1fr))}.squad-card,.squad-card.top{grid-column:span 1}.squad-count{display:none}.member summary{color:var(--text)!important;cursor:grab;user-select:none}.member summary:active{cursor:grabbing}.member.dragging{opacity:.42}.member.drag-over{border-radius:7px;background:var(--blue2);outline:1px solid var(--blue)}
@media(max-width:760px){.roster-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.squad-card,.squad-card.top{grid-column:span 1}}@media(max-width:430px){.roster-grid{grid-template-columns:1fr}}
'''
SCRIPT=r'''<script>
(()=>{let source=null;const playerId=el=>{const f=el.closest('.member').querySelector('form[action$="/save"]'),m=f&&f.action.match(/\/squads\/(\d+)\/save$/);return m?m[1]:null};document.querySelectorAll('.member summary').forEach(s=>s.draggable=true);document.addEventListener('dragstart',e=>{const s=e.target.closest('.member summary');if(!s)return;source=playerId(s);s.closest('.member').classList.add('dragging');e.dataTransfer.effectAllowed='move';e.dataTransfer.setData('text/plain',source||'')});document.addEventListener('dragend',()=>{document.querySelectorAll('.member').forEach(x=>x.classList.remove('dragging','drag-over'));source=null});document.addEventListener('dragover',e=>{const t=e.target.closest('.member');if(!t)return;e.preventDefault();document.querySelectorAll('.drag-over').forEach(x=>x.classList.remove('drag-over'));if(!t.classList.contains('dragging'))t.classList.add('drag-over')});document.addEventListener('drop',async e=>{const t=e.target.closest('.member');if(!t||!source)return;e.preventDefault();const target=playerId(t);if(!target||target===source)return;const body=new URLSearchParams({source,target}),r=await fetch('/squads/swap',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body});if(r.ok)location.reload();else alert(await r.text()||'Не удалось поменять игроков местами')})})();
</script>'''

def install(admin):
 if getattr(admin.app,'_roster_drag_installed',False): return
 admin.app._roster_drag_installed=True
 admin.PAGE=admin.PAGE.replace('</style>',CSS.replace('{','{{').replace('}','}}')+'\n</style>')
 base=admin.render
 def render(title,body):
  if request.path.startswith('/squads'): body+=SCRIPT
  return base(title,body)
 admin.render=render
 @admin.app.post('/squads/swap')
 def roster_swap():
  try:
   source=int(request.form.get('source','')); target=int(request.form.get('target',''))
   if source==target:return '',204
   with closing(admin.player_store.connect(admin.DB_PATH)) as con:
    con.execute('BEGIN IMMEDIATE'); guild=con.execute('SELECT guild_id FROM bot_guild_config ORDER BY (guild_id=0),guild_id LIMIT 1').fetchone()
    if guild is None: raise ValueError('Сервер Discord не настроен')
    guild_id=int(guild[0]); rows=list(con.execute('SELECT player_id,squad_id,slot FROM roster_memberships WHERE guild_id=? AND active=1 AND player_id IN (?,?)',(guild_id,source,target)))
    if len(rows)!=2: raise ValueError('Один из игроков уже не состоит в составе')
    pos={int(r['player_id']):(r['squad_id'],r['slot']) for r in rows}
    con.execute('UPDATE roster_memberships SET active=0,squad_id=NULL,slot=NULL,updated_at=CURRENT_TIMESTAMP WHERE guild_id=? AND player_id IN (?,?)',(guild_id,source,target))
    for player_id,other in ((source,target),(target,source)):
     squad_id,slot=pos[other];con.execute('UPDATE roster_memberships SET squad_id=?,slot=?,active=1,deactivated_at=NULL,updated_at=CURRENT_TIMESTAMP WHERE guild_id=? AND player_id=?',(squad_id,slot,guild_id,player_id))
    con.commit()
   return '',204
  except (ValueError,sqlite3.IntegrityError) as exc:return str(exc),400
