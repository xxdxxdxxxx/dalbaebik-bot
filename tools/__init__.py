"""Admin panel extensions."""
from . import admin_ui as admin_ui
from . import roster_admin, roster_layout, roster_sheet, roster_slot_limit
roster_admin.install(admin_ui)
roster_layout.install(admin_ui)
roster_sheet.install(admin_ui)
roster_slot_limit.install(roster_sheet)
__all__ = ["admin_ui"]
