"""Backend abstraction.

A *backend* is the "AI end" of agent-bridge — whatever process or service
actually turns a user message into a reply. Today: claude_channel (spawns a
child claude.exe per session, channels push model; daemon reads/writes the
inbox/outbox directly) and hermes_acp. Future: openclaw, direct Anthropic
API, local LLM, …  (claude_code + mcp_bridge was removed 2026-07-23.)

Backends are pluggable: the router picks one based on session config.
Sessions today are 1-1 with a backend instance; the abstraction allows
future N-1 (multiple aliases sharing a stateless API backend) but we don't
implement that yet.

This base class is intentionally loose — claude_channel today is process-typed
(daemon + child claude.exe), while a hypothetical openclaw
backend might be API-typed (stateless HTTP). The interface accommodates
both: `send` takes the session and the message, and returns when the agent
has replied. How that happens — file IO, HTTP call, RPC — is the backend's
secret. See docs/新增后端.md.
"""
from __future__ import annotations

from abc import ABC, abstractmethod


class Backend(ABC):
    """One AI execution engine. Backends are stateless from the bridge's POV;
    any per-session state they need lives under chat_sessions/<alias>/."""

    name: str  # short identifier, e.g. "claude_channel", "hermes_acp"

    @abstractmethod
    async def ensure_session(self, alias: str, cwd: str) -> bool:
        """Make sure a backend instance exists and is ready for this session.

        For process-typed backends (claude_channel) this may spawn a daemon if
        none is alive. For API-typed backends this can be a no-op.

        Returns True when the session is ready to accept send() calls.
        """

    @abstractmethod
    async def send(self, alias: str, text: str) -> None:
        """Hand a user message to the backend for this session.

        Does not block on the reply. The reply arrives asynchronously via
        the session's outbox.txt (or a backend-specific channel) and is
        picked up by the web server's poll / outbox watcher.
        """

    @abstractmethod
    def is_session_alive(self, alias: str) -> bool:
        """True if a backend instance is currently serving this session."""

    @abstractmethod
    async def end_session(self, alias: str) -> None:
        """Terminate the backend instance for this session. Keep on-disk
        state (history, meta) so /list still sees it."""

    def session_status(self, alias: str) -> dict:
        """Snapshot for dashboards: { online, pid, last_active, … }."""
        return {
            "backend": self.name,
            "alias": alias,
            "online": self.is_session_alive(alias),
        }
