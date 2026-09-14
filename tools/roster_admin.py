"""Compact SQLite roster editor for the local admin panel."""
from __future__ import annotations
import html, sqlite3
from contextlib import closing
from flask import redirect, request, url_for

ORDER=(1,2,3,99,4,5,6)
LABEL={1:'1',2:'2',3:'3',99:'Замены',4:'4',5:'5',6:'6'}
CSS=r'''
.layout-squads{max-width:940px}.roster-head{margin-bottom:16px}.roster-head h1{margin-bottom:3px}
.roster-grid{display:grid;grid-template-columns:repeat(12,minmax(0,1fr));gap:8px;align-items:start}
.squad-card{grid-column:span 4;background:var(--panel);border:1px solid var(--line);border-radius:10px;overflow:hidden;min-width:0}
.squad-card.top{grid-column:span 3}.squad-head{height:38px;display:flex;align-items:center;justify-content:space-between;padding:0 12px;background:var(--soft);border-bottom:1px solid var(--line);font-size:14px;font-weight:800}
.squad-count{color:var(--dim);font-size:11px}.member-list{list-style:none;margin:0;padding:5px}.member{border-bottom:1px solid var(--line)}.member:last-child{border:0}
.member summary{list-style:none;cursor:pointer;min-height:34px;padding:7px 8px;display:flex;align-items:center;justify-content:space-between;gap:8px;border-radius:7px;font-weight:650}.member summary::-webkit-details-marker{display:none}.member summary:hover{background:var(--blue2);color:var(--blue)}.member summary:after{content:'···';color:var(--dim)}.member[open] summary:after{content:'×'}
.edit{padding:5px 8px 9px;display:grid;gap:6px}.edit label,.add label{display:grid;gap:3px;color:var(--dim);font-size:10px;font-weight:700;text-transform:uppercase}.edit input,.edit select,.add input,.add select{width:100%;min-height:32px;border:1px solid var(--line);border-radius:7px;background:var(--bg);color:var(--text);padding:0 8px}.acts{display:flex;gap:6px;justify-content:flex-end}.empty{padding:15px;text-align:center;color:var(--dim)}
.addbox{margin-top:10px;background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:0 12px}.addbox>summary{cursor:pointer;min-height:42px;display:flex;align-items:center;font-weight:750;color:var(--blue)}.add{display:grid;grid-template-columns:2fr 2fr 1fr auto;gap:8px;align-items:end;padding-bottom:12px}.msg{margin-bottom:10px;padding:9px 11px;border:1px solid var(--line);border-radius:8px;background:var(--panel)}.msg.ok{color:var(--green)}.msg.err{color:var(--red)}
@media(max-width:760px){.roster-grid{grid-template-columns:repeat(2,1fr)}.squad-card,.squad-card.top{grid-column:span 1}.add{grid-template-columns:1fr}.member summary{min-height:44px}}@media(max-width:430px){.roster-grid{grid-template-columns:1fr}}
'''

def gid(con):
 r=con.execute('SELECT guild_id FROM bot_guild_config ORDER BY (guild_id=0),guild_id LIMIT 1').fetchone(); return int(r[0]) if r else None

def norm(ps,v): return ps._norm(' '.join(str(v or '').strip().split()))
def squad(v):
 try: s=int(v)
 except: raise ValueError('Выберите отряд')
 if s not in ORDER: raise ValueError('Неизвестный отряд')
 return s

def did(v):
 v=str(v or '').strip()
 if not v.isdigit() or not 15<=len(v)<=22: raise ValueError('Discord ID должен состоять из 15–22 цифр')
 return v

def nextslot(con,g,s): return int(con.execute('SELECT COALESCE(MAX(slot),0)+1 FROM roster_memberships WHERE guild_id=? AND squad_id=? AND active=1',(g,s)).fetchone()[0])
def opts(sel): return ''.join(f"<option value='{s}'{' selected' if s==sel else ''}>{LABEL[s]}</option>" for s in ORDER)
def exact(ps,con,n):
 n=norm(ps,n); out={int(r['id']) for r in con.execute('SELECT id,canonical_nick FROM players') if norm(ps,r['canonical_nick'])==n}; out|={int(r['player_id']) for r in con.execute('SELECT alias,player_id FROM player_aliases') if norm(ps,r['alias'])==n}; return out

def install(a):
 if getattr(a.app,'_roster_ui',False): return
 a.app._roster_ui=True
 a.PAGE=a.PAGE.replace('</style>',CSS.replace('{','{{').replace('}','}}')+'\n</style>')
 def render(title,body):
  p=request.path; layout='layout-squads' if p.startswith('/squads') else ('layout-day' if p.startswith('/day/') else ('layout-players' if p.startswith('/player') else 'layout-overview'))
  nav=f"<a class='{'active' if p=='/' or p.startswith('/day/') else ''}' href='{url_for('index')}'>Обзор</a><a class='{'active' if p.startswith('/player') else ''}' href='{url_for('players')}'>Игроки</a><a class='{'active' if p.startswith('/squads') else ''}' href='{url_for('squads')}'>Отряды</a>"
  page=a.PAGE.replace('<a class="{ov}" href="{home}">Обзор</a><a class="{pl}" href="{players}">Игроки</a>',nav)
  return page.format(title=html.escape(title),body=body,home=url_for('index'),players=url_for('players'),layout=layout,ov='',pl='')
 a.render=render

 @a.app.get('/squads')
 def squads():
  with closing(a.player_store.connect(a.DB_PATH)) as con:
   g=gid(con); rows=[] if g is None else a.q(con,'SELECT p.id,p.canonical_nick,r.squad_id,r.slot,d.discord_id FROM roster_memberships r JOIN players p ON p.id=r.player_id LEFT JOIN discord_bindings d ON d.player_id=p.id WHERE r.guild_id=? AND r.active=1 AND (r.squad_id BETWEEN 1 AND 6 OR r.squad_id=99) ORDER BY r.squad_id,r.slot,p.canonical_nick COLLATE NOCASE',(g,))
  groups={s:[] for s in ORDER}
  for r in rows: groups.get(int(r['squad_id']),[]).append(r)
  cards=[]
  for s in ORDER:
   items=[]
   for r in groups[s]:
    pid=int(r['id']); n=a.esc(r['canonical_nick']); d=a.esc(r['discord_id'] or '')
    items.append(f"<li class='member'><details><summary>{n}</summary><form class='edit' method='post' action='{url_for('roster_save',player_id=pid)}'><label>Ник<input name='nick' required maxlength='64' value='{n}'></label><label>Discord ID<input name='discord_id' required pattern='[0-9]{{15,22}}' value='{d}'></label><label>Отряд<select name='squad'>{opts(s)}</select></label><div class='acts'><button class='danger' formaction='{url_for('roster_remove',player_id=pid)}' onclick=\"return confirm('Убрать игрока? История сохранится.')\">Убрать</button><button class='save'>Сохранить</button></div></form></details></li>")
   cards.append(f"<section class='squad-card {'top' if s in (1,2,3,99) else ''}'><div class='squad-head'><span>{LABEL[s]}</span><span class='squad-count'>{len(groups[s])}</span></div><ul class='member-list'>{''.join(items) or '<li class=empty>Нет игроков</li>'}</ul></section>")
  msg=f"<div class='msg err'>{a.esc(request.args['error'])}</div>" if request.args.get('error') else ("<div class='msg ok'>Состав сохранён</div>" if request.args.get('ok') else '')
  body="<div class='roster-head'><h1>Отряды</h1><div class='subtitle'>Нажмите на игрока, чтобы изменить или переместить.</div></div>"+msg+f"<div class='roster-grid'>{''.join(cards)}</div><details class='addbox'><summary>＋ Добавить игрока</summary><form class='add' method='post' action='{url_for('roster_add')}'><label>Игровой ник<input name='nick' required maxlength='64'></label><label>Discord ID<input name='discord_id' required pattern='[0-9]{{15,22}}'></label><label>Отряд<select name='squad'>{opts(1)}</select></label><button class='save'>Добавить</button></form></details>"
  return a.render('Отряды',body)

 def fail(e): return redirect(url_for('squads',error=str(e)))
 @a.app.post('/squads/add')
 def roster_add():
  try:
   n=' '.join(request.form.get('nick','').split()); d=did(request.form.get('discord_id')); s=squad(request.form.get('squad'))
   if not n: raise ValueError('Введите игровой ник')
   with closing(a.player_store.connect(a.DB_PATH)) as con:
    con.execute('BEGIN IMMEDIATE'); g=gid(con)
    if g is None: raise ValueError('Сначала выполните /setup')
    bd=con.execute('SELECT player_id FROM discord_bindings WHERE discord_id=?',(d,)).fetchone(); ids=exact(a.player_store,con,n); np=next(iter(ids)) if len(ids)==1 else None; dp=int(bd[0]) if bd else None
    if len(ids)>1 or (np and dp and np!=dp): raise ValueError('Ник или Discord ID конфликтует с другим игроком')
    p=dp or np
    if p is None: con.execute('INSERT INTO players(canonical_nick) VALUES(?)',(n,)); p=int(con.execute('SELECT last_insert_rowid()').fetchone()[0])
    con.execute('DELETE FROM discord_bindings WHERE player_id=?',(p,)); con.execute('INSERT INTO discord_bindings(discord_id,player_id) VALUES(?,?)',(d,p)); con.execute('INSERT OR IGNORE INTO player_guilds(player_id,guild_id) VALUES(?,?)',(p,g)); con.execute('DELETE FROM roster_removals WHERE guild_id=? AND player_id=?',(g,p)); sl=nextslot(con,g,s); con.execute('INSERT INTO roster_memberships(guild_id,player_id,squad_id,slot,active,deactivated_at) VALUES(?,?,?,?,1,NULL) ON CONFLICT(guild_id,player_id) DO UPDATE SET squad_id=excluded.squad_id,slot=excluded.slot,active=1,deactivated_at=NULL,updated_at=CURRENT_TIMESTAMP',(g,p,s,sl)); con.commit()
   return redirect(url_for('squads',ok=1))
  except (ValueError,sqlite3.IntegrityError) as e: return fail(e)

 @a.app.post('/squads/<int:player_id>/save')
 def roster_save(player_id):
  try:
   n=' '.join(request.form.get('nick','').split()); d=did(request.form.get('discord_id')); s=squad(request.form.get('squad'))
   with closing(a.player_store.connect(a.DB_PATH)) as con:
    con.execute('BEGIN IMMEDIATE'); g=gid(con); cur=con.execute('SELECT squad_id,slot FROM roster_memberships WHERE guild_id=? AND player_id=? AND active=1',(g,player_id)).fetchone()
    if not n or cur is None: raise ValueError('Игрок не найден')
    ids=exact(a.player_store,con,n); b=con.execute('SELECT player_id FROM discord_bindings WHERE discord_id=?',(d,)).fetchone()
    if (ids and ids!={player_id}) or (b and int(b[0])!=player_id): raise ValueError('Ник или Discord ID уже занят')
    old=con.execute('SELECT canonical_nick FROM players WHERE id=?',(player_id,)).fetchone()[0]
    if norm(a.player_store,old)!=norm(a.player_store,n): con.execute('INSERT OR IGNORE INTO player_aliases(alias,player_id) VALUES(?,?)',(old,player_id))
    con.execute('UPDATE players SET canonical_nick=? WHERE id=?',(n,player_id)); con.execute('DELETE FROM discord_bindings WHERE player_id=?',(player_id,)); con.execute('INSERT INTO discord_bindings(discord_id,player_id) VALUES(?,?)',(d,player_id)); sl=int(cur[1]) if int(cur[0])==s and cur[1] is not None else nextslot(con,g,s); con.execute('UPDATE roster_memberships SET squad_id=?,slot=?,updated_at=CURRENT_TIMESTAMP WHERE guild_id=? AND player_id=?',(s,sl,g,player_id)); con.commit()
   return redirect(url_for('squads',ok=1))
  except (ValueError,sqlite3.IntegrityError) as e: return fail(e)

 @a.app.post('/squads/<int:player_id>/remove')
 def roster_remove(player_id):
  try:
   with closing(a.player_store.connect(a.DB_PATH)) as con:
    con.execute('BEGIN IMMEDIATE'); g=gid(con); con.execute("INSERT INTO roster_removals(guild_id,player_id,reason) VALUES(?,?,'admin_panel') ON CONFLICT(guild_id,player_id) DO UPDATE SET removed_at=CURRENT_TIMESTAMP,reason='admin_panel'",(g,player_id)); con.execute('UPDATE roster_memberships SET active=0,deactivated_at=COALESCE(deactivated_at,CURRENT_TIMESTAMP),updated_at=CURRENT_TIMESTAMP WHERE guild_id=? AND player_id=?',(g,player_id)); con.commit()
   return redirect(url_for('squads',ok=1))
  except (ValueError,sqlite3.IntegrityError) as e: return fail(e)
