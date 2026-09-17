"""WhatsApp via Meta's Cloud API.

Business-initiated messages outside the 24 hour service window must use an approved
template, and Meta only delivers replies to a public webhook — which this deployment
deliberately does not have. The template therefore carries a dynamic URL button whose
suffix is the ack token, so the tap lands on the ntfy relay instead.
"""

from __future__ import annotations

from typing import Any

import requests

from parking_guard.contacts import Contact
from parking_guard.messaging.base import AUDIENCE_BOTH, Channel, ChannelError, Message

GRAPH_VERSION = "v21.0"


class WhatsAppCloudChannel(Channel):
    name = "whatsapp"
    audience = AUDIENCE_BOTH

    def __init__(
        self,
        *,
        token: str,
        phone_number_id: str,
        template: str = "",
        language: str = "en_GB",
        graph_version: str = GRAPH_VERSION,
        timeout: float = 15.0,
    ) -> None:
        self.token = token
        self.phone_number_id = phone_number_id
        self.template = template
        self.language = language
        self.graph_version = graph_version
        self.timeout = timeout

    @property
    def url(self) -> str:
        return f"https://graph.facebook.com/{self.graph_version}/{self.phone_number_id}/messages"

    def send(self, contact: Contact, message: Message) -> str:
        to = contact.endpoint(self.name).lstrip("+")
        if not to:
            raise ChannelError("no WhatsApp number for this booking")

        payload = self._template_payload(to, contact, message) if self.template else self._text_payload(to, message)

        try:
            response = requests.post(
                self.url,
                headers={"Authorization": f"Bearer {self.token}"},
                json=payload,
                timeout=self.timeout,
            )
            response.raise_for_status()
            body = response.json()
        except requests.RequestException as exc:
            raise ChannelError(f"whatsapp cloud send failed: {exc}") from exc
        except ValueError:
            return ""

        messages = body.get("messages") or []
        return str(messages[0].get("id", "")) if messages else ""

    def _text_payload(self, to: str, message: Message) -> dict[str, Any]:
        return {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "text",
            "text": {"preview_url": True, "body": message.text()},
        }

    def _template_payload(self, to: str, contact: Contact, message: Message) -> dict[str, Any]:
        components: list[dict[str, Any]] = [
            {
                "type": "body",
                "parameters": [
                    {"type": "text", "text": contact.greeting},
                    {"type": "text", "text": message.body},
                ],
            }
        ]
        if message.ack_token:
            components.append(
                {
                    "type": "button",
                    "sub_type": "url",
                    "index": "0",
                    "parameters": [{"type": "text", "text": message.ack_token}],
                }
            )
        return {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "template",
            "template": {
                "name": self.template,
                "language": {"code": self.language},
                "components": components,
            },
        }
