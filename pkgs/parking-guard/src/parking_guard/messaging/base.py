"""Shared contract for notification channels.

Channels are audience-scoped: ntfy and Pushover are operator plumbing and never
address a paying client, while WhatsApp and Telegram serve both. A channel that
has no endpoint for a given contact reports itself unavailable so the dispatcher
can move down the ladder without waiting for a timeout.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from parking_guard.contacts import Contact

AUDIENCE_CLIENT = "client"
AUDIENCE_OWNER = "owner"
AUDIENCE_BOTH = "both"


class ChannelError(RuntimeError):
    """A send failed; the dispatcher should try the next rung."""


@dataclass(frozen=True)
class Message:
    title: str
    body: str
    ack_url: str = ""
    ack_token: str = ""
    ack_prompt: str = "I've moved it"
    incident_id: int = 0
    telegram_link: str = ""
    review_url: str = ""

    def text(self) -> str:
        lines = [self.body]
        if self.telegram_link:
            lines.append(f"Reply faster on Telegram: {self.telegram_link}")
        if self.ack_url:
            lines.append(f"{self.ack_prompt}: {self.ack_url}")
        return "\n\n".join(part for part in lines if part)


class Channel(ABC):
    name: str = ""
    audience: str = AUDIENCE_BOTH

    def serves(self, audience: str) -> bool:
        return self.audience in (audience, AUDIENCE_BOTH)

    def available_for(self, contact: Contact) -> bool:
        return bool(contact.endpoint(self.name))

    @abstractmethod
    def send(self, contact: Contact, message: Message) -> str:
        """Deliver the message and return a provider delivery id (may be empty)."""
