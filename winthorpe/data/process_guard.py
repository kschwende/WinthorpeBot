"""Process self-guard — prevent orphaned MCP server processes (#6).

A stdio MCP server should die with the Claude Code session that spawned it. When
it doesn't (a leaked orphan), the stale process squats the single tastytrade
DXLink streamer slot and the next session's stream can't connect (see the
stream-orphan runbook).

``set_parent_death_signal()`` ties this process's lifetime to its parent's via
PR_SET_PDEATHSIG, so an abandoned server exits on its own. This is the SAFE fix:
it only affects THIS process. Reaping *sibling* processes is deliberately NOT
done — a starting server cannot tell a leaked orphan from a live concurrent
session, and killing the wrong one would take down a real session.

``find_sibling_servers()`` is a read-only diagnostic to surface any orphan that
still slips through (e.g. one started before this guard shipped). It never kills.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)


def set_parent_death_signal() -> bool:
    """On Linux, request SIGTERM to be delivered to THIS process when its parent
    dies (PR_SET_PDEATHSIG), so an abandoned stdio server exits instead of
    leaking and squatting the DXLink slot. Best-effort; returns True if armed."""
    try:
        import ctypes
        import signal

        PR_SET_PDEATHSIG = 1
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        rc = libc.prctl(PR_SET_PDEATHSIG, signal.SIGTERM, 0, 0, 0)
        if rc != 0:
            logger.warning("prctl(PR_SET_PDEATHSIG) returned %s; parent-death guard not armed", rc)
            return False
        logger.info("parent-death signal armed (server will exit if its parent dies)")
        return True
    except Exception:
        logger.warning("could not arm parent-death signal (non-Linux / no libc?)",
                       exc_info=True)
        return False


def find_sibling_servers(self_pid: Optional[int] = None) -> list[int]:
    """Read-only: PIDs of OTHER ``winthorpe.mcp.server`` processes — potential
    orphans squatting the DXLink slot. NEVER kills anything; for diagnostics only.
    Returns [] on a system without /proc."""
    me = self_pid if self_pid is not None else os.getpid()
    out: list[int] = []
    try:
        entries = os.listdir("/proc")
    except FileNotFoundError:
        return out
    for entry in entries:
        if not entry.isdigit():
            continue
        pid = int(entry)
        if pid == me:
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                cmd = f.read().replace(b"\x00", b" ").decode("utf-8", "ignore")
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue
        if "winthorpe.mcp.server" in cmd:
            out.append(pid)
    return out
