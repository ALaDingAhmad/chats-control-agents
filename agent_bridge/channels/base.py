"""Channel abstraction.

A *channel* is the "user-facing end" of agent-bridge — anywhere a real
person types a message that should reach the AI. Today: browser, WeChat
iLink Bot. Future: Feishu, Slack, Telegram, email, …

Each channel runs at least one long-lived task (inbound poller / webhook
listener) and exposes a way to send replies back. Channels are stateful
(connection, credentials) but most state should be persisted under
data/<channel-name>_state/ so a process restart resumes cleanly.

This base class is intentionally loose — we're not forcing the existing
weixin channel to adopt it immediately. It documents the contract any new
channel should follow. See docs/ADD_CHANNEL.md for a step-by-step.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional


class Channel(ABC):
    """One side of an inbound/outbound IM pipe."""

    name: str  # short identifier, used in URLs and log prefixes, e.g. "weixin"

    @abstractmethod
    async def start(self) -> None:
        """Start any background tasks (long-poll, webhook listener, etc.).

        Called from the web server's lifespan startup. Should return as soon
        as setup is done; long-lived work goes into asyncio tasks.
        """

    @abstractmethod
    async def stop(self) -> None:
        """Cancel background tasks and clean up connections."""

    @abstractmethod
    def is_connected(self) -> bool:
        """True if the channel currently has a usable credential / session."""

    @abstractmethod
    async def send(self, peer_id: str, text: str) -> bool:
        """Push a reply back to a specific user. Return True on success."""

    def status(self) -> dict:
        """Snapshot for dashboards: { connected, account_id, last_active, … }.
        Default returns minimal info; override to expose more.
        """
        return {
            "name": self.name,
            "connected": self.is_connected(),
        }


# ── Inbound message envelope ────────────────────────────────────────────
# Channels translate raw protocol messages into this shape before handing
# them to the router (core.commands + the active backend).
class InboundMessage:
    """Channel-agnostic inbound envelope.

    Attributes:
        channel:     channel name (e.g. "weixin", "browser")
        peer_id:     stable identifier of the sender in that channel
        text:        message body (plain text)
        context:     opaque per-message metadata the channel needs to thread
                     replies back (iLink context_token, Telegram chat_id, etc.)
    """
    __slots__ = ("channel", "peer_id", "text", "context")

    def __init__(self, channel: str, peer_id: str, text: str,
                 context: Optional[dict] = None):
        self.channel = channel
        self.peer_id = peer_id
        self.text = text
        self.context = context or {}
