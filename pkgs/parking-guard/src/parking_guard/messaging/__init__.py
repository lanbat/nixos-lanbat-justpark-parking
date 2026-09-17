"""Notification channels, built from the agenix-provided credentials."""

from __future__ import annotations

import logging

from parking_guard.contacts import Contact, normalize_phone
from parking_guard.messaging.base import (
    AUDIENCE_BOTH,
    AUDIENCE_CLIENT,
    AUDIENCE_OWNER,
    Channel,
    ChannelError,
    Message,
)
from parking_guard.messaging.email_smtp import EmailChannel
from parking_guard.messaging.ntfy import NtfyChannel
from parking_guard.messaging.pushover import PushoverChannel
from parking_guard.messaging.sms_android import AndroidSmsChannel
from parking_guard.messaging.sms_cloud import SmsCloudChannel
from parking_guard.messaging.telegram import TelegramChannel
from parking_guard.messaging.whatsapp_cloud import WhatsAppCloudChannel
from parking_guard.messaging.whatsapp_waha import WahaChannel

__all__ = [
    "AUDIENCE_BOTH",
    "AUDIENCE_CLIENT",
    "AUDIENCE_OWNER",
    "AndroidSmsChannel",
    "Channel",
    "ChannelError",
    "EmailChannel",
    "Message",
    "NtfyChannel",
    "PushoverChannel",
    "SmsCloudChannel",
    "TelegramChannel",
    "WahaChannel",
    "WhatsAppCloudChannel",
    "build_channels",
    "owner_contact",
]

log = logging.getLogger(__name__)


def build_channels(secrets: dict[str, str]) -> dict[str, Channel]:
    """One channel per name. WAHA wins over the Cloud API when both are configured."""
    channels: dict[str, Channel] = {}

    if secrets.get("WHATSAPP_TOKEN") and secrets.get("WHATSAPP_PHONE_NUMBER_ID"):
        channels["whatsapp"] = WhatsAppCloudChannel(
            token=secrets["WHATSAPP_TOKEN"],
            phone_number_id=secrets["WHATSAPP_PHONE_NUMBER_ID"],
            template=secrets.get("WHATSAPP_TEMPLATE", ""),
            language=secrets.get("WHATSAPP_TEMPLATE_LANGUAGE", "en_GB"),
        )

    if secrets.get("WAHA_URL"):
        channels["whatsapp"] = WahaChannel(
            base_url=secrets["WAHA_URL"],
            session=secrets.get("WAHA_SESSION", "default"),
            api_key=secrets.get("WAHA_API_KEY", ""),
        )

    if secrets.get("TELEGRAM_BOT_TOKEN"):
        channels["telegram"] = TelegramChannel(
            bot_token=secrets["TELEGRAM_BOT_TOKEN"],
            bot_username=secrets.get("TELEGRAM_BOT_USERNAME", ""),
        )

    if secrets.get("SMTP_HOST"):
        channels["email"] = EmailChannel(
            host=secrets["SMTP_HOST"],
            port=int(secrets.get("SMTP_PORT", "587") or 587),
            username=secrets.get("SMTP_USERNAME", ""),
            password=secrets.get("SMTP_PASSWORD", ""),
            sender=secrets.get("SMTP_FROM", ""),
            use_tls=_flag(secrets.get("SMTP_STARTTLS", "true")),
            use_ssl=_flag(secrets.get("SMTP_SSL", "false")),
        )

    if secrets.get("SMS_API_URL"):
        channels["sms"] = SmsCloudChannel(
            url=secrets["SMS_API_URL"],
            method=secrets.get("SMS_API_METHOD", "POST"),
            auth_header=secrets.get("SMS_API_AUTH_HEADER", ""),
            auth_value=secrets.get("SMS_API_AUTH_VALUE", ""),
            username=secrets.get("SMS_API_USERNAME", ""),
            password=secrets.get("SMS_API_PASSWORD", ""),
            content_type=secrets.get("SMS_API_CONTENT_TYPE", "json"),
            body_template=secrets.get("SMS_API_BODY", ""),
            sender=secrets.get("SMS_API_FROM", ""),
        )

    # The LAN handset is preferred when present: same message, no per-message billing.
    if secrets.get("ANDROID_SMS_URL"):
        channels["sms"] = AndroidSmsChannel(
            base_url=secrets["ANDROID_SMS_URL"],
            username=secrets.get("ANDROID_SMS_USERNAME", ""),
            password=secrets.get("ANDROID_SMS_PASSWORD", ""),
        )

    if secrets.get("NTFY_TOPIC"):
        channels["ntfy"] = NtfyChannel(
            base_url=secrets.get("NTFY_BASE_URL", "https://ntfy.sh"),
            topic=secrets["NTFY_TOPIC"],
            token=secrets.get("NTFY_TOKEN", ""),
        )

    if secrets.get("PUSHOVER_TOKEN") and secrets.get("PUSHOVER_USER"):
        channels["pushover"] = PushoverChannel(
            token=secrets["PUSHOVER_TOKEN"],
            user=secrets["PUSHOVER_USER"],
        )

    log.info("messaging channels available: %s", ", ".join(sorted(channels)) or "none")
    return channels


def owner_contact(secrets: dict[str, str]) -> Contact:
    """The operator's own endpoints, kept separate from any client's."""
    return Contact(
        name=secrets.get("OWNER_NAME", ""),
        phone=normalize_phone(secrets.get("OWNER_PHONE", "")),
        whatsapp=normalize_phone(secrets.get("OWNER_WHATSAPP", "") or secrets.get("OWNER_PHONE", "")),
        email=secrets.get("OWNER_EMAIL", ""),
        telegram_chat_id=secrets.get("TELEGRAM_CHAT_ID", ""),
        source="owner",
    )


def _flag(value: str) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes", "on")
