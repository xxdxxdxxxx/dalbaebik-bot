"""Keep the spreadsheet roster at exactly five visible slots per squad."""
from __future__ import annotations
import builtins

def install(roster_sheet):
    if getattr(roster_sheet, '_five_slot_limit', False):
        return
    roster_sheet._five_slot_limit = True
    # The sheet page uses range(1, maximum + 1) to render its cells. Capping that
    # module-local range preserves stored data while showing exactly slots 1–5.
    roster_sheet.range = lambda start, stop: builtins.range(start, min(stop, 6))
