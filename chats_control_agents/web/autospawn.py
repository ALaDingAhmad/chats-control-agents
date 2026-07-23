"""Autospawn worker: drains the autospawn queue and spawns detached daemons.

When the user picks an offline / new project via /proj, commands._cmd_pick_proj
writes a request to chat_sessions/_autospawn_queue.jsonl. This worker reads
it and spawns `python -m <backend>.daemon <alias> <cwd>` (backend from meta)
detached from the web_server process.

Dedup is by *liveness*, not by "ever spawned". We skip a queue entry only
when a daemon for that alias is actually alive right now — either one we
spawned this lifetime (we remember its pid) or one recorded in meta.json by
another path. A daemon that has since exited must be re-spawnable, otherwise
"恢复旧会话" silently does nothing once the original daemon dies.

We still keep a per-process map (alias -> pid we spawned) purely to cover the
race window between spawn_daemon_detached returning and the daemon writing its
pid into meta.json: during that gap meta.daemon_pid is stale/None, so a
duplicated queue entry would double-spawn. The freshly-spawned pid is known
immediately, so checking it (not mere set membership) closes that window
without ever permanently blocking a re-spawn.
"""
from __future__ import annotations

import asyncio
import json
import logging

from ..core import sessions as sx
from ..core.paths import AUTOSPAWN_QUEUE_FILE
from ..core.pid_track import _pid_alive
from ..core.spawn import spawn_daemon_detached, watch_ready


log = logging.getLogger("web.autospawn")

_autospawn_pids: dict[str, int] = {}  # alias -> pid of the daemon we spawned

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
                # Skip only if a daemon is actually alive — one we spawned
                # (tracked pid, closes the spawn→meta-write race) or one
                # recorded in meta by another path. A dead daemon falls
                # through and gets re-spawned so 恢复旧会话 works.
                prev_pid = _autospawn_pids.get(alias)
                if prev_pid and _pid_alive(prev_pid):
                    log.info("autospawn[%s]: our daemon pid=%s still alive, skip", alias, prev_pid)
                    continue
                m = sx.load_meta_for(alias) or {}
                daemon_pid = m.get("daemon_pid")
                if daemon_pid and _pid_alive(daemon_pid):
                    log.info("autospawn[%s]: daemon pid=%s already alive, skip", alias, daemon_pid)
                    _autospawn_pids[alias] = daemon_pid
                    continue
                pid = spawn_daemon_detached(alias, cwd)
                if pid:
                    _autospawn_pids[alias] = pid
                    # Notify the user when the daemon actually becomes ready.
                    # See docs/入站路由.md "就绪通知".
                    asyncio.create_task(watch_ready(alias, pid))
    except asyncio.CancelledError:
        log.info("autospawn worker cancelled")
        raise
    except Exception as e:
        log.exception("autospawn worker crashed: %s", e)
