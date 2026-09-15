"""Refresh the two fixed Discord status messages after admin roster edits."""
from __future__ import annotations

import asyncio
import logging
import threading
from functools import wraps

LOG = logging.getLogger(__name__)
_TIMER: threading.Timer | None = None
_TIMER_LOCK = threading.Lock()


def _done(future) -> None:
    try:
        future.result()
        LOG.info("Discord roster messages refreshed")
    except Exception:
        LOG.exception("Discord roster message refresh failed")


def _submit(runtime) -> None:
    try:
        loop = runtime.bot.loop
        if loop is None or not loop.is_running():
            LOG.warning("Discord roster refresh skipped: bot loop is not running")
            return
        future = asyncio.run_coroutine_threadsafe(
            runtime.upsert_status_messages(None, force=True, parts=("online", "grenades")),
            loop,
        )
        future.add_done_callback(_done)
    except Exception:
        LOG.exception("Discord roster refresh could not be scheduled")


def schedule(runtime, delay: float = 2.5) -> None:
    """Collapse rapid cell edits into one edit of each Discord message."""
    global _TIMER
    with _TIMER_LOCK:
        if _TIMER is not None:
            _TIMER.cancel()
        _TIMER = threading.Timer(delay, _submit, args=(runtime,))
        _TIMER.daemon = True
        _TIMER.start()


def install(admin, runtime) -> None:
    if getattr(admin.app, "_discord_roster_refresh_installed", False):
        return
    admin.app._discord_roster_refresh_installed = True
    endpoints = ("roster_cell", "roster_cells_swap", "roster_swap", "roster_add", "roster_save", "roster_remove")
    for endpoint in endpoints:
        original = admin.app.view_functions.get(endpoint)
        if original is None:
            continue

        @wraps(original)
        def wrapped(*args, __original=original, **kwargs):
            result = __original(*args, **kwargs)
            status = result[1] if isinstance(result, tuple) and len(result) > 1 else getattr(result, "status_code", 200)
            if int(status) < 400:
                schedule(runtime)
            return result

        admin.app.view_functions[endpoint] = wrapped
