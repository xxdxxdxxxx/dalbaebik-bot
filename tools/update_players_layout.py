#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ADMIN = ROOT / "tools" / "admin_ui.py"


def one(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected one match, found {count}")
    return text.replace(old, new, 1)


def main() -> None:
    text = ADMIN.read_text(encoding="utf-8")
    text = one(
        text,
        ".squad-title td{{padding:8px 6px 4px!important;background:var(--bg);color:var(--text);font-size:10px;font-weight:850;text-align:left;text-transform:uppercase;letter-spacing:.06em}}",
        ".squad-title td{{padding:8px 6px 4px!important;background:var(--bg);color:var(--dim);font-size:10px;font-weight:850;text-align:left;text-transform:uppercase;letter-spacing:.06em}}",
        "gray squad labels",
    )
    text = one(
        text,
        "            ORDER BY CASE WHEN rm.squad_id=99 THEN 0 ELSE rm.squad_id END,\n",
        "            ORDER BY CASE WHEN rm.squad_id=99 THEN 7 ELSE rm.squad_id END,\n",
        "champions SQL order",
    )
    text = one(
        text,
        "    for squad_id in [99, 1, 2, 3, 4, 5, 6]:\n",
        "    for squad_id in [1, 2, 3, 4, 5, 6, 99]:\n",
        "champions display order",
    )
    text = one(
        text,
        "<th title='Среднее текущего EFF по дням КВ'>EFF</th>",
        "<th class='sortable' title='Среднее текущего EFF по дням КВ' onclick='sortPlayerSquads(this)'>EFF</th>",
        "sortable EFF header",
    )
    old_script = "<script>function filterPlayers(value){const q=value.toLowerCase();document.querySelectorAll('.squad-group').forEach(group=>{let n=0;group.querySelectorAll('.player-row').forEach(row=>{const show=row.textContent.toLowerCase().includes(q);row.style.display=show?'':'none';if(show)n++});group.style.display=n?'':'none'})}</script>"
    new_script = "<script>function filterPlayers(value){const q=value.toLowerCase();document.querySelectorAll('.squad-group').forEach(group=>{let n=0;group.querySelectorAll('.player-row').forEach(row=>{const show=row.textContent.toLowerCase().includes(q);row.style.display=show?'':'none';if(show)n++});group.style.display=n?'':'none'})}function sortPlayerSquads(th){const dir=th.dataset.dir==='desc'?'asc':'desc';th.dataset.dir=dir;document.querySelectorAll('.squad-group').forEach(group=>{const rows=[...group.querySelectorAll('.player-row')];const value=row=>{const raw=row.cells[3].textContent.trim().replace(',','.');if(!raw||raw==='—')return null;const parsed=Number(raw);return Number.isFinite(parsed)?parsed:null};rows.sort((a,b)=>{const av=value(a),bv=value(b);if(av===null||bv===null)return av===bv?0:av===null?1:-1;return dir==='asc'?av-bv:bv-av});rows.forEach(row=>group.appendChild(row))})}</script>"
    text = one(text, old_script, new_script, "per-squad EFF sorting")
    compile(text, str(ADMIN), "exec")
    ADMIN.write_text(text, encoding="utf-8")
    print("OK: Players layout and EFF sorting updated")


if __name__ == "__main__":
    main()
