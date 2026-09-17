"""WhatsApp via a self-hosted WAHA session.

WAHA drives a linked WhatsApp Web session over an outbound socket, so replies arrive
without any inbound port and plain free text works instead of an approved template.
It is unofficial and carries a real risk of the number being banned, so it is off
unless WAHA_URL is configured.
"""

from __future__ import annotations

import requests

from parking_guard.contacts import Contact
from parking_guard.messaging.base import AUDIENCE_BOTH, Channel, ChannelError, Message


class WahaChannel(Channel):
    name = "whatsapp"
    audience = AUDIENCE_BOTH

    def __init__(
        self,
        *,
        base_url: str,
        session: str = "default",
        api_key: str = "",
        timeout: float = 15.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.session = session
        self.api_key = api_key
        self.timeout = timeout

    def send(self, contact: Contact, message: Message) -> str:
        number = contact.endpoint(self.name).lstrip("+")
        if not number:
            raise ChannelError("no WhatsApp number for this booking")

        headers = {"X-Api-Key": self.api_key} if self.api_key else {}
        try:
            response = requests.post(
                f"{self.base_url}/api/sendText",
                headers=headers,
                json={
                    "session": self.session,
                    "chatId": f"{number}@c.us",
                    "text": message.text(),
                },
                timeout=self.timeout,
            )
            response.raise_for_status()
            body = response.json()
        except requests.RequestException as exc:
            raise ChannelError(f"waha send failed: {exc}") from exc
        except ValueError:
            return ""

        if isinstance(body, dict):
            return str(body.get("id") or body.get("_serialized") or "")
        return ""

    def poll_replies(self, limit: int = 50) -> list[dict[str, str]]:
        """Read recent inbound messages so acks can be picked up without a webhook."""
        headers = {"X-Api-Key": self.api_key} if self.api_key else {}
        try:
            response = requests.get(
                f"{self.base_url}/api/{self.session}/chats/messages",
                headers=headers,
                params={"limit": limit, "downloadMedia": "false"},
                timeout=self.timeout,
            )
            response.raise_for_status()
            body = response.json()
        except (requests.RequestException, ValueError):
            return []

        replies: list[dict[str, str]] = []
        for entry in body if isinstance(body, list) else []:
            if not isinstance(entry, dict) or entry.get("fromMe"):
                continue
            replies.append(
                {
                    "id": str(entry.get("id", "")),
                    "from": str(entry.get("from", "")).split("@", 1)[0],
                    "text": str(entry.get("body", "")),
                }
            )
        return replies
