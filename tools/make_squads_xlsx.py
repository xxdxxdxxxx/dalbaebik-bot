"""Explicit tech-sheet export from SQLite (never reads players.json)."""
import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from explicit_roster_io import export_tech_xlsx

parser = argparse.ArgumentParser()
parser.add_argument("--db", type=Path, default=ROOT / "scan_stats.sqlite3")
parser.add_argument("--out", type=Path, default=ROOT / "squads.xlsx")
parser.add_argument("--guild-id", type=int, required=True)
args = parser.parse_args()
print(export_tech_xlsx(args.db, args.out, args.guild_id))
