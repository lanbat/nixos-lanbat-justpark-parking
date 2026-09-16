"""Optional direct notifications (Pushover / Telegram)."""

from __future__ import annotations

import logging
from typing import Any

import requests

log = logging.getLogger(__name__)


def send_alert(title: str, message: str, notify: dict[str, str]) -> None:
    if notify.get("PUSHOVER_TOKEN") and notify.get("PUSHOVER_USER"):
        _pushover(title, message, notify)
    if notify.get("TELEGRAM_BOT_TOKEN") and notify.get("TELEGRAM_CHAT_ID"):
        _telegram(title, message, notify)


def _pushover(title: str, message: str, notify: dict[str, str]) -> None:
    try:
        requests.post(
            "https://api.pushover.net/1/messages.json",
            data={
                "token": notify["PUSHOVER_TOKEN"],
                "user": notify["PUSHOVER_USER"],
                "title": title,
                "message": message,
            },
            timeout=15,
        ).raise_for_status()
    except requests.RequestException as exc:
        log.warning("pushover notify failed: %s", exc)


def _telegram(title: str, message: str, notify: dict[str, str]) -> None:
    token = notify["TELEGRAM_BOT_TOKEN"]
    chat_id = notify["TELEGRAM_CHAT_ID"]
    text = f"*{title}*\n{message}"
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
            timeout=15,
        ).raise_for_status()
    except requests.RequestException as exc:
        log.warning("telegram notify failed: %s", exc)


def format_alert_message(event: dict[str, Any], nvr_domain: str) -> str:
    plate = event.get("plate", "?")
    camera = event.get("camera", "?")
    score = event.get("score")
    frigate_id = event.get("frigate_id")
    lines = [f"Plate: {plate}", f"Camera: {camera}"]
    if score is not None:
        lines.append(f"Score: {score}")
    if frigate_id and nvr_domain:
        lines.append(f"Review: https://{nvr_domain}/review?id={frigate_id}")
    return "\n".join(lines)
