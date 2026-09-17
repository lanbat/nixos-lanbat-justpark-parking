"""Receiving acknowledgements without opening a single inbound port.

Nothing on the internet can reach this host, so every ack path is something the
guard reaches out for:

* **ntfy relay** — each message carries ``https://ntfy.sh/<topic>/trigger?message=<token>``.
  Tapping it publishes to a topic the guard is already subscribed to over an
  outbound stream. The topic name is the only secret, so tokens are random,
  single-use, and the payload never contains a name, number or plate.
* **Telegram** — ``getUpdates`` long-polling picks up inline button callbacks and
  ``/start <token>`` deep links.
* **Replies** — WAHA and the LAN SMS handset are polled for free-text answers like
  "ok" or "moving it", matched back to the masked target of a recent message.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from typing import Any, Callable

import requests

from parking_guard.config import AckConfig
from parking_guard.contacts import mask_endpoint, normalize_phone
from parking_guard.dispatcher import Dispatcher
from parking_guard.messaging.telegram import ACK_CALLBACK_PREFIX, TelegramChannel

log = logging.getLogger(__name__)

ACK_WORDS = (
    "ok",
    "okay",
    "k",
    "yes",
    "y",
    "yep",
    "yeah",
    "sure",
    "done",
    "moved",
    "moving",
    "on it",
    "will do",
    "sorry",
)

RECONNECT_SECONDS = 10.0
TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{8,}")


def looks_like_ack(text: str) -> bool:
    cleaned = re.sub(r"[^a-z0-9 ]", "", str(text or "").strip().lower())
    if not cleaned:
        return False
    if cleaned in ACK_WORDS:
        return True
    return any(cleaned.startswith(word + " ") or f" {word} " in f" {cleaned} " for word in ACK_WORDS)


def extract_token(message: str) -> str:
    """Accept a bare token or one pasted inside a URL."""
    text = str(message or "").strip()
    if not text:
        return ""
    if "message=" in text:
        text = text.rsplit("message=", 1)[-1]
    text = text.split("&", 1)[0].split()[0] if text.split() else ""
    match = TOKEN_PATTERN.fullmatch(text)
    return match.group(0) if match else ""


class _Listener:
    """Small supervised loop: keep going, log, never take the process down."""

    name = "listener"

    def __init__(self) -> None:
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run_forever(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception:  # noqa: BLE001 - listeners must survive bad responses
                log.exception("%s failed, retrying", self.name)
                self._stop.wait(RECONNECT_SECONDS)

    def poll_once(self) -> None:  # pragma: no cover - overridden
        raise NotImplementedError


class NtfyAckListener(_Listener):
    """Holds an outbound JSON stream open and claims any token that arrives."""

    name = "ntfy ack listener"

    def __init__(self, *, config: AckConfig, dispatcher: Dispatcher) -> None:
        super().__init__()
        self.config = config
        self.dispatcher = dispatcher

    def poll_once(self) -> None:
        response = requests.get(
            self.config.stream_url,
            stream=True,
            timeout=(10, self.config.poll_interval_seconds * 4),
        )
        response.raise_for_status()
        for line in response.iter_lines(decode_unicode=True):
            if self._stop.is_set():
                return
            if not line:
                continue
            self.handle_line(line)
        # The stream ended cleanly; pause before reconnecting so a server that hangs
        # up immediately cannot spin this thread.
        self._stop.wait(RECONNECT_SECONDS)

    def handle_line(self, line: str) -> int | None:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return None
        if not isinstance(event, dict) or event.get("event") != "message":
            return None

        token = extract_token(str(event.get("message", "")))
        if not token:
            return None
        return self.dispatcher.handle_ack(token, "ntfy")


class TelegramAckListener(_Listener):
    """Inline button callbacks and /start deep links, via long-polling."""

    name = "telegram ack listener"

    def __init__(self, *, channel: TelegramChannel, dispatcher: Dispatcher, timeout: int = 25) -> None:
        super().__init__()
        self.channel = channel
        self.dispatcher = dispatcher
        self.timeout = timeout
        self.offset = 0

    def poll_once(self) -> None:
        updates = self.channel.get_updates(self.offset, timeout=self.timeout)
        if not updates:
            # get_updates swallows network errors and returns nothing straight away,
            # so back off rather than hammering the API.
            self._stop.wait(RECONNECT_SECONDS)
            return
        for update in updates:
            self.offset = max(self.offset, int(update.get("update_id", 0)) + 1)
            self.handle_update(update)

    def handle_update(self, update: dict[str, Any]) -> None:
        callback = update.get("callback_query")
        if isinstance(callback, dict):
            data = str(callback.get("data", ""))
            if data.startswith(ACK_CALLBACK_PREFIX):
                token = data[len(ACK_CALLBACK_PREFIX) :]
                incident_id = self.dispatcher.handle_ack(token, "telegram")
                reply = "Thanks — we'll keep an eye out." if incident_id else "Already noted, thank you."
                self.channel.answer_callback(str(callback.get("id", "")), reply)
            return

        message = update.get("message")
        if not isinstance(message, dict):
            return
        chat_id = str((message.get("chat") or {}).get("id", ""))
        text = str(message.get("text", "")).strip()

        if text.startswith("/start"):
            token = extract_token(text[len("/start") :])
            if token and chat_id:
                if self.dispatcher.link_telegram_chat(token, chat_id):
                    self.channel.send_plain(
                        chat_id,
                        "Thanks — we'll message you here about your parking from now on.",
                    )
                self.dispatcher.handle_ack(token, "telegram")
            return

        if looks_like_ack(text) and chat_id:
            notification = self.dispatcher.store.latest_unconsumed_notification(
                mask_endpoint(chat_id), channels=("telegram",)
            )
            if notification and notification.get("token"):
                self.dispatcher.handle_ack(str(notification["token"]), "telegram")


class ReplyAckListener(_Listener):
    """Free-text replies from WAHA or the LAN SMS handset, polled on a timer."""

    name = "reply ack listener"

    def __init__(
        self,
        *,
        fetch: Callable[[], list[dict[str, str]]],
        dispatcher: Dispatcher,
        channel_name: str,
        interval: float = 30.0,
    ) -> None:
        super().__init__()
        self.fetch = fetch
        self.dispatcher = dispatcher
        self.channel_name = channel_name
        self.interval = interval
        self._seen: set[str] = set()

    def poll_once(self) -> None:
        for reply in self.fetch():
            self.handle_reply(reply)
        self._stop.wait(self.interval)

    def handle_reply(self, reply: dict[str, str]) -> int | None:
        reply_id = str(reply.get("id", ""))
        if reply_id and reply_id in self._seen:
            return None
        if reply_id:
            self._seen.add(reply_id)

        text = str(reply.get("text", ""))
        token = extract_token(text)
        if token:
            claimed = self.dispatcher.handle_ack(token, self.channel_name)
            if claimed:
                return claimed

        if not looks_like_ack(text):
            return None

        target = mask_endpoint(normalize_phone(str(reply.get("from", ""))))
        notification = self.dispatcher.store.latest_unconsumed_notification(
            target, channels=(self.channel_name,)
        )
        if notification is None or not notification.get("token"):
            return None
        return self.dispatcher.handle_ack(str(notification["token"]), self.channel_name)


def start_listeners(
    *,
    dispatcher: Dispatcher,
    channels: dict[str, Any],
    ack_config: AckConfig,
) -> list[_Listener]:
    """Start whichever ack paths the configured channels can support."""
    listeners: list[_Listener] = []

    if ack_config.enabled:
        listeners.append(NtfyAckListener(config=ack_config, dispatcher=dispatcher))

    telegram = channels.get("telegram")
    if ack_config.telegram_polling and isinstance(telegram, TelegramChannel):
        listeners.append(TelegramAckListener(channel=telegram, dispatcher=dispatcher))

    whatsapp = channels.get("whatsapp")
    if hasattr(whatsapp, "poll_replies"):
        listeners.append(
            ReplyAckListener(fetch=whatsapp.poll_replies, dispatcher=dispatcher, channel_name="whatsapp")
        )

    sms = channels.get("sms")
    if hasattr(sms, "inbox"):
        listeners.append(ReplyAckListener(fetch=sms.inbox, dispatcher=dispatcher, channel_name="sms"))

    for listener in listeners:
        threading.Thread(target=listener.run_forever, name=listener.name, daemon=True).start()
        log.info("started %s", listener.name)

    return listeners
