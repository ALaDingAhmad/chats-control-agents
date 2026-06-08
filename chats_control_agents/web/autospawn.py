"""Autospawn worker: drains the autospawn queue and spawns detached daemons.

When the user picks an offline / new project via /proj, commands._cmd_pick_proj
writes a request to chat_sessions/_autospawn_queue.jsonl. This worker reads
it and spawns `python -m chats_control_agents.backends.claude_code.daemon <alias> <cwd>`
detached from the web_server process.

Dedup: we keep a set of aliases already spawned in this process lifetime so
a duplicated queue entry doesn't double-spawn. The set is per-process; a
web_server restart re-arms it.
"""
from __future__ import annotations

import asyncio
import json
import logging

from ..core import sessions as sx
from ..core.paths import AUTOSPAWN_QUEUE_FILE
from ..core.pid_track import _pid_alive
from ..core.spawn import spawn_daemon_detached


log = logging.getLogger("web.autospawn")

_autospawn_running: set[str] = set()  # aliases already spawned in this lifetime

# Back-compat alias for callers still importing the underscore-prefixed name.
_spawn_daemon_detached = spawn_daemon_detached


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
                pid = spawn_daemon_detached(alias, cwd)
                if pid:
                    _autospawn_running.add(alias)
    except asyncio.CancelledError:
        log.info("autospawn worker cancelled")
        raise
    except Exception as e:
        log.exception("autospawn worker crashed: %s", e)
