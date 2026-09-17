"""Telegram bot channel, used for both clients and the operator.

Acks come back through ``getUpdates`` long-polling rather than a webhook: an inline
button sends ``ack:<token>`` as callback data, and a ``/start <token>`` deep link
records the client's chat id so later bookings can skip straight to Telegram.
"""

from __future__ import annotations

from typing import Any

import requests

from parking_guard.contacts import Contact
from parking_guard.messaging.base import AUDIENCE_BOTH, Channel, ChannelError, Message

ACK_CALLBACK_PREFIX = "ack:"


class TelegramChannel(Channel):
    name = "telegram"
    audience = AUDIENCE_BOTH

    def __init__(self, *, bot_token: str, bot_username: str = "", timeout: float = 15.0) -> None:
        self.bot_token = bot_token
        self.bot_username = bot_username
        self.timeout = timeout

    def api_url(self, method: str) -> str:
        return f"https://api.telegram.org/bot{self.bot_token}/{method}"

    def deep_link(self, token: str) -> str:
        if not self.bot_username or not token:
            return ""
        return f"https://t.me/{self.bot_username}?start={token}"

    def send(self, contact: Contact, message: Message) -> str:
        chat_id = contact.endpoint(self.name)
        if not chat_id:
            raise ChannelError("no Telegram chat linked for this booking")

        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": f"*{message.title}*\n{message.body}",
            "parse_mode": "Markdown",
        }
        if message.ack_token:
            payload["reply_markup"] = {
                "inline_keyboard": [
                    [{"text": message.ack_prompt, "callback_data": f"{ACK_CALLBACK_PREFIX}{message.ack_token}"}]
                ]
            }

        try:
            response = requests.post(self.api_url("sendMessage"), json=payload, timeout=self.timeout)
            response.raise_for_status()
            body = response.json()
        except requests.RequestException as exc:
            raise ChannelError(f"telegram send failed: {exc}") from exc
        except ValueError:
            return ""

        result = body.get("result") if isinstance(body, dict) else None
        return str(result.get("message_id", "")) if isinstance(result, dict) else ""

    def get_updates(self, offset: int, timeout: int = 25) -> list[dict[str, Any]]:
        try:
            response = requests.get(
                self.api_url("getUpdates"),
                params={"offset": offset, "timeout": timeout},
                timeout=timeout + 10,
            )
            response.raise_for_status()
            body = response.json()
        except (requests.RequestException, ValueError):
            return []
        result = body.get("result") if isinstance(body, dict) else None
        return result if isinstance(result, list) else []

    def answer_callback(self, callback_id: str, text: str) -> None:
        try:
            requests.post(
                self.api_url("answerCallbackQuery"),
                json={"callback_query_id": callback_id, "text": text},
                timeout=self.timeout,
            )
        except requests.RequestException:
            return

    def send_plain(self, chat_id: str, text: str) -> None:
        try:
            requests.post(
                self.api_url("sendMessage"),
                json={"chat_id": chat_id, "text": text},
                timeout=self.timeout,
            )
        except requests.RequestException:
            return
