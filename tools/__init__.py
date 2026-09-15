"""Admin panel extensions."""
import sys

from . import admin_ui as admin_ui
from . import clanmap_sync, discord_roster_refresh, roster_board, status_messages, unbound_roster

roster_board.install(admin_ui)
clanmap_sync.install(admin_ui)


def install_runtime(runtime):
    """Attach hooks that require the completed bot runtime namespace."""
    if runtime is None or not hasattr(runtime, "upsert_status_messages"):
        return
    status_messages.install(runtime)
    unbound_roster.install(runtime)
    discord_roster_refresh.install(admin_ui, runtime)


install_runtime(sys.modules.get("__main__"))

__all__ = ["admin_ui", "install_runtime"]
