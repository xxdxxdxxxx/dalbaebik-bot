"""Admin panel extensions."""
from . import admin_ui as admin_ui
from . import roster_admin, roster_layout, roster_sheet
roster_admin.install(admin_ui)
roster_layout.install(admin_ui)
roster_sheet.install(admin_ui)
__all__ = ["admin_ui"]
