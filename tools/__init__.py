"""Admin panel extensions."""
import sys

from . import admin_ui as admin_ui
from . import clanmap_sync, discord_roster_refresh, roster_board, status_messages, unbound_roster

roster_board.install(admin_ui)
clanmap_sync.install(admin_ui)
_runtime = sys.modules.get("__main__")
if _runtime is not None and hasattr(_runtime, "upsert_status_messages"):
    status_messages.install(_runtime)
    unbound_roster.install(_runtime)
    discord_roster_refresh.install(admin_ui, _runtime)

__all__ = ["admin_ui"]
