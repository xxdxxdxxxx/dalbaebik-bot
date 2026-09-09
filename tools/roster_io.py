"""CLI for explicit roster import/export."""
import argparse
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from explicit_roster_io import export_tech_xlsx, import_roster_xlsx
p=argparse.ArgumentParser(); sub=p.add_subparsers(dest="op",required=True)
i=sub.add_parser("import"); i.add_argument("xlsx",type=Path); i.add_argument("--db",type=Path,default=ROOT/"scan_stats.sqlite3"); i.add_argument("--guild-id",type=int,required=True); i.add_argument("--apply",action="store_true"); i.add_argument("--expected-revision")
e=sub.add_parser("export-tech"); e.add_argument("xlsx",type=Path); e.add_argument("--db",type=Path,default=ROOT/"scan_stats.sqlite3"); e.add_argument("--guild-id",type=int,required=True)
a=p.parse_args(); print(import_roster_xlsx(a.db,a.xlsx,a.guild_id,dry_run=not a.apply,expected_revision=a.expected_revision) if a.op=="import" else export_tech_xlsx(a.db,a.xlsx,a.guild_id))
