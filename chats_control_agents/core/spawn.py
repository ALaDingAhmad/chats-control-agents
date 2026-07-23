"""Spawn detached daemons and revive dead ones on demand.

Pure OS / subprocess work. Anything that needs to bring a backend daemon up
calls in here.

Two entry points:
  - spawn_daemon_detached(alias, cwd): start one, return PID or None.
  - ensure_daemon_alive(alias): async; idempotent. Used by the router when
    an inbound message arrives and the session's daemon may be dead.
"""
from __future__ import annotations

import asyncio
import logging
import os
import subprocess
from datetime import datetime

from . import sessions as sx
from .paths import ROOT, loop_marker_fresh, loop_marker_path, outbox_path
from .pid_track import _pid_alive


log = logging.getLogger("core.spawn")

_HISTORICAL_CWD = str(ROOT.parent / "claude-code-account-switch")

# Total time we'll wait for ~/.claude/.session-ready-<alias> after spawn.
READY_NOTIFY_TIMEOUT_SECS = 120.0


def _write_outbox_notice(alias: str, body: str) -> None:
    """Write a one-shot notice into outbox.txt for channel watchers."""
    stamp = datetime.now().strftime("%H:%M:%S")
    p = outbox_path(alias)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"[{stamp}]\n{body}\n", encoding="utf-8")
        log.info("ready-notify[%s]: %s", alias, body[:80])
    except Exception as e:
        log.warning("ready-notify[%s]: write failed: %s", alias, e)


async def watch_ready(alias: str, daemon_pid: int) -> None:
    """Watch daemon liveness after spawn — FAILURE fallback only.

    成功提示（"✅ 已就绪"/"✅ 已接回"）统一由 daemon 自己发（见 docs/入站路由.md
    "就绪通知"）：daemon 首次就绪发"会话已就绪"、resume 就绪发"已接回+回顾"。
    watch_ready 只在 daemon 起不来时兜底告警——marker 出现就静默 return，不发成功
    提示（否则 resume 场景 watch_ready + _do_resume 各发一条，重复）。
    """
    deadline = asyncio.get_event_loop().time() + READY_NOTIFY_TIMEOUT_SECS
    marker = loop_marker_path(alias)
    while asyncio.get_event_loop().time() < deadline:
        if marker.exists():
            return  # 就绪成功 → 静默；成功提示由 daemon 发
        if not _pid_alive(daemon_pid):
            _write_outbox_notice(
                alias,
                f"⚠️ 会话 {alias!r} 启动后异常退出，见 chat_sessions/{alias}/daemon_stdout.log",
            )
            return
        await asyncio.sleep(1.0)
    _write_outbox_notice(
        alias,
        f"⚠️ 会话 {alias!r} 拉起超时（{int(READY_NOTIFY_TIMEOUT_SECS)}s 未就绪），"
        f"可能卡在 Claude TUI；见 chat_sessions/{alias}/daemon_stdout.log",
    )


_BACKEND_DAEMON_MODULES = {
    "hermes_acp": "chats_control_agents.backends.hermes_acp.daemon",
    "claude_channel": "chats_control_agents.backends.claude_channel.daemon",
}

# claude_code 已删除（2026-07-23）。老会话 meta.backend=="claude_code" 一律
# 回退到 claude_channel（也是 spawn child claude，能接上）。
_DEFAULT_BACKEND = "claude_channel"


def _resolve_daemon_module(alias: str) -> str:
    """Resolve daemon module from session meta; defaults to claude_channel."""
    meta = sx.load_meta_for(alias) or {}
    backend = meta.get("backend") or _DEFAULT_BACKEND
    mod = _BACKEND_DAEMON_MODULES.get(backend)
    if mod is None:
        log.warning("spawn[%s]: unknown/removed backend %r, falling back to %s",
                    alias, backend, _DEFAULT_BACKEND)
        return _BACKEND_DAEMON_MODULES[_DEFAULT_BACKEND]
    return mod


def spawn_daemon_detached(alias: str, cwd: str) -> int | None:
    """Spawn the daemon detached from this process."""
    stale = loop_marker_path(alias)
    if stale.exists():
        try:
            stale.unlink()
        except OSError:
            pass
    log_path = sx.session_dir(alias) / "daemon_stdout.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        f = open(log_path, "a", encoding="utf-8", errors="replace")
    except Exception as e:
        log.warning("spawn[%s]: could not open log: %s", alias, e)
        return None
    kwargs: dict = {
        "stdout": f,
        "stderr": subprocess.STDOUT,
        "stdin": subprocess.DEVNULL,
        "cwd": str(ROOT),
        "close_fds": True,
    }
    if os.name == "nt":
        detached = 0x00000008
        new_process_group = 0x00000200
        no_window = 0x08000000
        kwargs["creationflags"] = detached | new_process_group | no_window
    else:
        kwargs["start_new_session"] = True

    daemon_module = _resolve_daemon_module(alias)
    try:
        proc = subprocess.Popen(["python", "-m", daemon_module, alias, cwd], **kwargs)
        log.info("spawn[%s]: backend-module=%s pid=%s cwd=%s", alias, daemon_module, proc.pid, cwd)
        return proc.pid
    except Exception as e:
        log.warning("spawn[%s]: failed: %s", alias, e)
        return None


async def ensure_daemon_alive(alias: str) -> bool:
    """If alias's daemon is dead, spawn a new one and wait until alive.

    （原 bridge-owned 分支——活 bridge 不叠 daemon、看 chats-loop marker——已随
    claude_code 删除。现在会话只有 daemon 一种活法。）
    """
    m = sx.load_meta_for(alias) or {}
    pid = m.get("daemon_pid")
    if pid and _pid_alive(pid):
        return True

    cwd = m.get("cwd") or _HISTORICAL_CWD
    log.info("ensure[%s]: daemon pid=%s dead, respawning at cwd=%s", alias, pid, cwd)
    spawned_pid = spawn_daemon_detached(alias, cwd)
    if not spawned_pid:
        log.warning("ensure[%s]: spawn failed", alias)
        return False

    for _ in range(20):
        if _pid_alive(spawned_pid):
            log.info("ensure[%s]: daemon pid=%s alive", alias, spawned_pid)
            asyncio.create_task(watch_ready(alias, spawned_pid))
            return True
        await asyncio.sleep(0.1)
    log.warning("ensure[%s]: pid=%s never went live", alias, spawned_pid)
    return False
