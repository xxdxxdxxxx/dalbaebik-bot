"""Asyncio file watcher with stable-save detection, debounce, and retry."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

FileFingerprint = tuple[int, int]


def file_fingerprint(path: Path | str) -> FileFingerprint | None:
    """Return a cheap fingerprint, or None when the file is unavailable."""
    try:
        stat = Path(path).stat()
    except OSError:
        return None
    return stat.st_mtime_ns, stat.st_size


@dataclass
class StableFileDebouncer:
    """Pure state machine: emit a stable fingerprint once."""

    debounce_seconds: float = 2.0
    last_seen: FileFingerprint | None = None
    candidate_since: float | None = None
    last_successful: FileFingerprint | None = None

    def observe(self, fingerprint: FileFingerprint | None, now: float) -> FileFingerprint | None:
        if fingerprint is None:
            self.last_seen = None
            self.candidate_since = None
            return None
        if self.last_seen is None:
            # Establish a startup baseline; only later saves are events.
            self.last_seen = fingerprint
            self.last_successful = fingerprint
            self.candidate_since = now
            return None
        if fingerprint != self.last_seen:
            self.last_seen = fingerprint
            self.candidate_since = now
            return None
        if fingerprint == self.last_successful:
            return None
        if self.candidate_since is None:
            self.candidate_since = now
            return None
        if now - self.candidate_since >= self.debounce_seconds:
            return fingerprint
        return None

    def mark_success(self, fingerprint: FileFingerprint) -> None:
        self.last_successful = fingerprint


async def watch_file(
    path: Path | str,
    sync_callback: Callable[[], Awaitable[bool]],
    *,
    poll_seconds: float = 0.5,
    debounce_seconds: float = 2.0,
    retry_seconds: float = 3.0,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Poll for completed saves; retry the same fingerprint on failure."""
    state = StableFileDebouncer(max(0.0, debounce_seconds))
    loop = asyncio.get_running_loop()
    next_attempt = 0.0
    while stop_event is None or not stop_event.is_set():
        now = loop.time()
        fingerprint = await asyncio.to_thread(file_fingerprint, path)
        ready = state.observe(fingerprint, now)
        if ready is not None and now >= next_attempt:
            try:
                ok = bool(await sync_callback())
            except asyncio.CancelledError:
                raise
            except Exception:
                ok = False
            if ok:
                state.mark_success(ready)
            else:
                next_attempt = now + max(poll_seconds, retry_seconds)
        await asyncio.sleep(max(0.01, poll_seconds))
