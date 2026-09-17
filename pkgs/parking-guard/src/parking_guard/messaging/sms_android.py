"""SMS through an Android handset on the LAN (capcom6 android-sms-gateway, local mode).

The phone holds an ordinary SIM and exposes a small REST API on the local network,
so messages and replies never leave the LAN and cost whatever the SIM plan costs.
"""

from __future__ import annotations

import requests

from parking_guard.contacts import Contact
from parking_guard.messaging.base import AUDIENCE_BOTH, Channel, ChannelError, Message


class AndroidSmsChannel(Channel):
    name = "sms"
    audience = AUDIENCE_BOTH

    def __init__(
        self,
        *,
        base_url: str,
        username: str = "",
        password: str = "",
        timeout: float = 20.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.timeout = timeout

    def send(self, contact: Contact, message: Message) -> str:
        to = contact.endpoint(self.name)
        if not to:
            raise ChannelError("no phone number for this booking")

        auth = (self.username, self.password) if self.username else None
        try:
            response = requests.post(
                f"{self.base_url}/message",
                json={"message": message.text(), "phoneNumbers": [to]},
                auth=auth,
                timeout=self.timeout,
            )
            response.raise_for_status()
            body = response.json()
        except requests.RequestException as exc:
            raise ChannelError(f"android sms gateway send failed: {exc}") from exc
        except ValueError:
            return ""

        return str(body.get("id", "")) if isinstance(body, dict) else ""

    def inbox(self) -> list[dict[str, str]]:
        """Replies waiting on the handset, so a texted 'OK' can close an incident."""
        auth = (self.username, self.password) if self.username else None
        try:
            response = requests.get(f"{self.base_url}/inbox", auth=auth, timeout=self.timeout)
            response.raise_for_status()
            body = response.json()
        except (requests.RequestException, ValueError):
            return []

        replies: list[dict[str, str]] = []
        for entry in body if isinstance(body, list) else []:
            if not isinstance(entry, dict):
                continue
            replies.append(
                {
                    "id": str(entry.get("id", "")),
                    "from": str(entry.get("phoneNumber", "") or entry.get("from", "")),
                    "text": str(entry.get("message", "") or entry.get("text", "")),
                }
            )
        return replies
