"""SMS over any HTTP provider — the last rung of the ladder.

The request is described by configuration rather than code so BulkSMS, Plivo, AWS
End User Messaging, Twilio or a local aggregator can all be used without a new
adapter: a URL, a method, an auth header or basic credentials, and a body template
whose string values may contain ``{to}``, ``{text}`` and ``{from}``.
"""

from __future__ import annotations

import json
from typing import Any

import requests

from parking_guard.contacts import Contact
from parking_guard.messaging.base import AUDIENCE_BOTH, Channel, ChannelError, Message

DEFAULT_BODY_TEMPLATE = '{"to": "{to}", "body": "{text}", "from": "{from}"}'
ID_KEYS = ("id", "messageId", "message_id", "sid", "MessageId")


class SmsCloudChannel(Channel):
    name = "sms"
    audience = AUDIENCE_BOTH

    def __init__(
        self,
        *,
        url: str,
        method: str = "POST",
        auth_header: str = "",
        auth_value: str = "",
        username: str = "",
        password: str = "",
        content_type: str = "json",
        body_template: str = DEFAULT_BODY_TEMPLATE,
        sender: str = "",
        timeout: float = 20.0,
    ) -> None:
        self.url = url
        self.method = method.upper()
        self.auth_header = auth_header
        self.auth_value = auth_value
        self.username = username
        self.password = password
        self.content_type = content_type.lower()
        self.body_template = body_template or DEFAULT_BODY_TEMPLATE
        self.sender = sender
        self.timeout = timeout

    def send(self, contact: Contact, message: Message) -> str:
        to = contact.endpoint(self.name)
        if not to:
            raise ChannelError("no phone number for this booking")

        try:
            template = json.loads(self.body_template)
        except json.JSONDecodeError as exc:
            raise ChannelError(f"sms body template is not valid JSON: {exc}") from exc

        payload = _render(template, {"to": to, "text": message.text(), "from": self.sender})
        headers = {self.auth_header: self.auth_value} if self.auth_header and self.auth_value else {}
        auth = (self.username, self.password) if self.username else None

        kwargs: dict[str, Any] = {"headers": headers, "auth": auth, "timeout": self.timeout}
        if self.content_type == "form":
            kwargs["data"] = payload
        else:
            kwargs["json"] = payload

        try:
            response = requests.request(self.method, self.url, **kwargs)
            response.raise_for_status()
            body = response.json()
        except requests.RequestException as exc:
            raise ChannelError(f"sms send failed: {exc}") from exc
        except ValueError:
            return ""

        return _extract_id(body)


def _render(value: Any, values: dict[str, str]) -> Any:
    if isinstance(value, str):
        rendered = value
        for key, replacement in values.items():
            rendered = rendered.replace("{" + key + "}", replacement)
        return rendered
    if isinstance(value, dict):
        return {key: _render(item, values) for key, item in value.items()}
    if isinstance(value, list):
        return [_render(item, values) for item in value]
    return value


def _extract_id(body: Any) -> str:
    if isinstance(body, dict):
        for key in ID_KEYS:
            if body.get(key):
                return str(body[key])
        for nested in body.values():
            found = _extract_id(nested)
            if found:
                return found
    if isinstance(body, list) and body:
        return _extract_id(body[0])
    return ""
