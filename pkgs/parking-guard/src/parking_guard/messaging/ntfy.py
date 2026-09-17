"""ntfy — operator channel only.

Clients are never sent to a public ntfy topic: the topic name is the only secret,
so anything published there should stay free of names, numbers and plates. ntfy's
other role in this system is the inbound ack relay, which lives in ``ack.py``.
"""

from __future__ import annotations

import requests

from parking_guard.contacts import Contact
from parking_guard.messaging.base import AUDIENCE_OWNER, Channel, ChannelError, Message


class NtfyChannel(Channel):
    name = "ntfy"
    audience = AUDIENCE_OWNER

    def __init__(
        self,
        *,
        base_url: str = "https://ntfy.sh",
        topic: str,
        token: str = "",
        priority: str = "high",
        timeout: float = 15.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.topic = topic
        self.token = token
        self.priority = priority
        self.timeout = timeout

    def available_for(self, contact: Contact) -> bool:
        return bool(self.topic)

    def send(self, contact: Contact, message: Message) -> str:
        if not self.topic:
            raise ChannelError("no ntfy topic configured")

        headers = {
            "Title": message.title,
            "Priority": self.priority,
            "Tags": "car",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if message.review_url:
            headers["Actions"] = f"view, Open Frigate, {message.review_url}"

        try:
            response = requests.post(
                f"{self.base_url}/{self.topic}",
                data=message.body.encode("utf-8"),
                headers=headers,
                timeout=self.timeout,
            )
            response.raise_for_status()
            body = response.json()
        except requests.RequestException as exc:
            raise ChannelError(f"ntfy send failed: {exc}") from exc
        except ValueError:
            return ""

        return str(body.get("id", "")) if isinstance(body, dict) else ""
