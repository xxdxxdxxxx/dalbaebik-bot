"""Admin panel extensions."""
import sys
from . import admin_ui as admin_ui
from . import roster_admin, roster_layout, roster_sheet, roster_slot_limit, roster_notes, status_messages
roster_admin.install(admin_ui)
roster_layout.install(admin_ui)
roster_sheet.install(admin_ui)
roster_slot_limit.install(roster_sheet)
roster_notes.install(admin_ui)
_runtime = sys.modules.get("__main__")
if _runtime is not None and hasattr(_runtime, "upsert_status_messages"):
    status_messages.install(_runtime)
__all__ = ["admin_ui"]
