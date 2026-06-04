"""Autospawn worker: drains the autospawn queue and spawns detached daemons.

When the user picks an offline / new project via /proj, commands._cmd_pick_proj
writes a request to chat_sessions/_autospawn_queue.jsonl. This worker reads
it and spawns `python -m agent_bridge.backends.claude_code.daemon <alias> <cwd>`
detached from the web_server process.

Dedup: we keep a set of aliases already spawned in this process lifetime so
a duplicated queue entry doesn't double-spawn. The set is per-process; a
web_server restart re-arms it.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess

from ..core import sessions as sx
from ..core.paths import AUTOSPAWN_QUEUE_FILE, ROOT
from ..core.pid_track import _pid_alive


log = logging.getLogger("web.autospawn")

_autospawn_running: set[str] = set()  # aliases already spawned in this lifetime


def _spawn_daemon_detached(alias: str, cwd: str) -> int | None:
    """Spawn the daemon detached from web_server. Returns PID or None on failure.

    Windows: DETACHED + CREATE_NEW_PROCESS_GROUP + CREATE_NO_WINDOW so the
    daemon survives web_server restart and doesn't pop a console window.
    Unix: start_new_session to detach from web_server's process group.
    """
    log_path = sx.session_dir(alias) / "daemon_stdout.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        f = open(log_path, "a", encoding="utf-8", errors="replace")
    except Exception as e:
        log.warning("autospawn[%s]: could not open log: %s", alias, e)
        return None
    kwargs: dict = {
        "stdout": f,
        "stderr": subprocess.STDOUT,
        "stdin": subprocess.DEVNULL,
        "cwd": str(ROOT),
        "close_fds": True,
    }
    if os.name == "nt":
        DETACHED = 0x00000008
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        CREATE_NO_WINDOW = 0x08000000
        kwargs["creationflags"] = DETACHED | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
    else:
        kwargs["start_new_session"] = True
    try:
        proc = subprocess.Popen(
            ["python", "-m", "agent_bridge.backends.claude_code.daemon", alias, cwd],
            **kwargs,
        )
        log.info("autospawn[%s]: spawned pid=%s cwd=%s", alias, proc.pid, cwd)
        return proc.pid
    except Exception as e:
        log.warning("autospawn[%s]: spawn failed: %s", alias, e)
        return None


async def autospawn_worker():
    """Drain the autospawn queue periodically."""
    log.info("autospawn worker starting")
    try:
        while True:
            await asyncio.sleep(0.5)
            if not AUTOSPAWN_QUEUE_FILE.exists():
                continue
            try:
                lines = AUTOSPAWN_QUEUE_FILE.read_text(encoding="utf-8").splitlines()
            except Exception:
                continue
            if not lines:
                continue
            # Read-then-truncate. Race-safe enough: a writer that lands between
            # read and truncate just adds work for the next loop iteration.
            try:
                AUTOSPAWN_QUEUE_FILE.write_text("", encoding="utf-8")
            except Exception:
                pass
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                alias = rec.get("alias")
                cwd = rec.get("cwd")
                if not alias or not cwd:
                    continue
                if alias in _autospawn_running:
                    log.info("autospawn[%s]: already spawned in this lifetime, skip", alias)
                    continue
                m = sx.load_meta_for(alias) or {}
                daemon_pid = m.get("daemon_pid")
                if daemon_pid and _pid_alive(daemon_pid):
                    log.info("autospawn[%s]: daemon pid=%s already alive, skip", alias, daemon_pid)
                    _autospawn_running.add(alias)
                    continue
                pid = _spawn_daemon_detached(alias, cwd)
                if pid:
                    _autospawn_running.add(alias)
    except asyncio.CancelledError:
        log.info("autospawn worker cancelled")
        raise
    except Exception as e:
        log.exception("autospawn worker crashed: %s", e)
