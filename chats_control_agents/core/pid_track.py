"""Cross-platform PID liveness + daemon-spawned-child identification.

The daemon appends to chat_sessions/<alias>/spawned_pids.jsonl
every time it spawns a child claude.exe. This module reads those records and
filters to PIDs that are (a) alive AND (b) whose process create_time matches
the value we logged — that combination rules out PID-recycling collisions.

Cleanup tools use list_daemon_descendants() to know which processes are safe
to terminate (vs. the user's interactive claude.exe sessions, which were never
logged).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from .paths import ALIAS_RE, SESSIONS_ROOT, spawned_log_path


def _pid_alive(pid: int) -> bool:
    """True iff a process with this PID currently exists.

    Windows: os.kill(pid, 0) is unreliable — for unknown / dead PIDs Python
    raises SystemError instead of OSError. We use OpenProcess via ctypes and
    check GetExitCodeProcess for STILL_ACTIVE (259) for a clean answer.
    """
    if not pid:
        return False
    if os.name == "nt":
        try:
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                return False
            try:
                exit_code = ctypes.c_ulong(0)
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    return False
                return exit_code.value == 259  # STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _kill_pid(pid: int) -> bool:
    """Send SIGTERM (Unix) / TerminateProcess (Windows). Returns True if the
    request was issued (not whether the process actually died)."""
    if not pid:
        return False
    try:
        if os.name == "nt":
            import ctypes
            PROCESS_TERMINATE = 0x0001
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)
            if not handle:
                return False
            try:
                return bool(kernel32.TerminateProcess(handle, 1))
            finally:
                kernel32.CloseHandle(handle)
        import signal
        os.kill(pid, signal.SIGTERM)
        return True
    except Exception:
        return False


# ── Daemon child tracking ────────────────────────────────────────────────
def list_logged_child_records() -> list[dict]:
    """Read every spawned_pids.jsonl across all aliases. Records may include
    dead PIDs; caller decides what to do with those."""
    out: list[dict] = []
    if not SESSIONS_ROOT.exists():
        return out
    for entry in SESSIONS_ROOT.iterdir():
        if not entry.is_dir():
            continue
        alias = entry.name
        if not ALIAS_RE.match(alias):
            continue
        p = spawned_log_path(alias)
        if not p.exists():
            continue
        try:
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    rec["_alias"] = alias
                    out.append(rec)
                except Exception:
                    continue
        except Exception:
            continue
    return out


def list_daemon_child_pids() -> set[int]:
    """PIDs that are (a) alive AND (b) whose create_time matches the logged
    value. The create_time check rules out PID-recycling collisions.

    Falls back to PID-only check if psutil is not installed (less safe).
    """
    try:
        import psutil
    except ImportError:
        return {rec["pid"] for rec in list_logged_child_records()
                if rec.get("pid") and _pid_alive(rec["pid"])}

    result: set[int] = set()
    for rec in list_logged_child_records():
        pid = rec.get("pid")
        if not pid:
            continue
        logged_ct = rec.get("create_time")
        try:
            proc = psutil.Process(pid)
            if logged_ct is None:
                result.add(pid)
                continue
            if abs(proc.create_time() - logged_ct) < 1.0:
                result.add(pid)
        except psutil.NoSuchProcess:
            continue
        except Exception:
            continue
    return result


def is_daemon_child(pid: int) -> bool:
    return pid in list_daemon_child_pids()


def list_daemon_descendants() -> set[int]:
    """Daemon child PIDs plus all of their descendants (cmd.exe
    shims, etc.). Used by cleanup scripts to take down the whole tree."""
    try:
        import psutil
    except ImportError:
        return list_daemon_child_pids()
    roots = list_daemon_child_pids()
    out: set[int] = set(roots)
    for pid in roots:
        try:
            for child in psutil.Process(pid).children(recursive=True):
                out.add(child.pid)
        except Exception:
            continue
    return out
