"""Pushover — operator channel only."""

from __future__ import annotations

import requests

from parking_guard.contacts import Contact
from parking_guard.messaging.base import AUDIENCE_OWNER, Channel, ChannelError, Message


class PushoverChannel(Channel):
    name = "pushover"
    audience = AUDIENCE_OWNER

    def __init__(self, *, token: str, user: str, timeout: float = 15.0) -> None:
        self.token = token
        self.user = user
        self.timeout = timeout

    def available_for(self, contact: Contact) -> bool:
        return bool(self.token and self.user)

    def send(self, contact: Contact, message: Message) -> str:
        data = {
            "token": self.token,
            "user": self.user,
            "title": message.title,
            "message": message.body,
        }
        if message.review_url:
            data["url"] = message.review_url
            data["url_title"] = "Open Frigate"

        try:
            response = requests.post(
                "https://api.pushover.net/1/messages.json",
                data=data,
                timeout=self.timeout,
            )
            response.raise_for_status()
            body = response.json()
        except requests.RequestException as exc:
            raise ChannelError(f"pushover send failed: {exc}") from exc
        except ValueError:
            return ""

        return str(body.get("request", "")) if isinstance(body, dict) else ""
