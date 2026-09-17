"""Who to tell when a car is parked badly.

JustPark's owner agreement says shared booking data "may include a Driver's name,
telephone number and car registration" — it does not promise an email address, and
the partner sessions API documents neither. So contacts are assembled from whatever
the booking source hands over, overlaid with a hand-maintained file, and a booking
with no reachable endpoint simply falls back to alerting the owner.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterable

from parking_guard.plates import normalize_plate
from parking_guard.store import Booking, Store

log = logging.getLogger(__name__)

CLIENT_CHANNEL_FIELDS = {
    "whatsapp": "whatsapp",
    "telegram": "telegram_chat_id",
    "email": "email",
    "sms": "phone",
}


@dataclass(frozen=True)
class Contact:
    plate: str = ""
    booking_ref: str = ""
    name: str = ""
    phone: str = ""
    whatsapp: str = ""
    email: str = ""
    telegram_chat_id: str = ""
    source: str = ""

    def endpoint(self, channel: str) -> str:
        field = CLIENT_CHANNEL_FIELDS.get(channel)
        if field is None:
            return ""
        return str(getattr(self, field, "") or "")

    def reachable_on(self, channel: str) -> bool:
        return bool(self.endpoint(channel))

    @property
    def reachable(self) -> bool:
        return any(self.reachable_on(channel) for channel in CLIENT_CHANNEL_FIELDS)

    @property
    def greeting(self) -> str:
        return self.name.split()[0] if self.name else "Hi"


def normalize_phone(value: str, default_country_code: str = "+44") -> str:
    """Best-effort E.164 for UK mobiles; anything already international is left alone."""
    digits = re.sub(r"[^\d+]", "", str(value or ""))
    if not digits:
        return ""
    if digits.startswith("+"):
        return digits
    if digits.startswith("00"):
        return "+" + digits[2:]
    if digits.startswith("0"):
        return default_country_code + digits[1:]
    return digits


def mask_endpoint(endpoint: str) -> str:
    """Audit-trail form of an endpoint: enough to match a reply, not enough to leak."""
    if not endpoint:
        return ""
    if "@" in endpoint:
        name, _, domain = endpoint.partition("@")
        return f"{name[:1]}***@{domain}"
    return f"***{endpoint[-4:]}" if len(endpoint) > 4 else "***"


def contact_from_booking(booking: Booking, default_country_code: str = "+44") -> Contact:
    phone = normalize_phone(booking.phone, default_country_code)
    return Contact(
        plate=booking.plate,
        booking_ref=booking.booking_ref,
        name=booking.name,
        phone=phone,
        whatsapp=phone,
        email=booking.email,
        source=booking.source,
    )


def load_overrides(path: Path) -> dict[str, Contact]:
    """Read contacts.yaml (or .json). Missing or malformed files are not fatal."""
    if not path.exists() or not path.is_file():
        return {}

    try:
        raw = _parse_contacts_file(path)
    except Exception as exc:  # noqa: BLE001 - a typo here must not stop enforcement
        log.warning("could not read contacts file %s: %s", path, exc)
        return {}

    overrides: dict[str, Contact] = {}
    for entry in _iter_entries(raw):
        plate = str(entry.get("plate", "")).strip()
        normalized = normalize_plate(plate)
        if not normalized:
            continue
        phone = normalize_phone(entry.get("phone", ""))
        overrides[normalized] = Contact(
            plate=plate,
            booking_ref=str(entry.get("booking_ref", "") or ""),
            name=str(entry.get("name", "") or ""),
            phone=phone,
            whatsapp=normalize_phone(entry.get("whatsapp", "")) or phone,
            email=str(entry.get("email", "") or ""),
            telegram_chat_id=str(entry.get("telegram_chat_id", "") or ""),
            source="override",
        )
    return overrides


def _parse_contacts_file(path: Path) -> Any:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        return json.loads(text)
    try:
        import yaml
    except ImportError:
        log.warning("pyyaml unavailable, reading %s as JSON", path)
        return json.loads(text)
    return yaml.safe_load(text)


def _iter_entries(raw: Any) -> Iterable[dict[str, Any]]:
    if raw is None:
        return []
    if isinstance(raw, dict):
        if "contacts" in raw:
            return _iter_entries(raw["contacts"])
        # Mapping form: { "AB12 CDE": { phone: ... } }
        entries = []
        for plate, value in raw.items():
            if isinstance(value, dict):
                entries.append({"plate": plate, **value})
        return entries
    if isinstance(raw, list):
        return [entry for entry in raw if isinstance(entry, dict)]
    return []


def merge(base: Contact, override: Contact) -> Contact:
    """Override wins field by field, but never replaces a value with a blank."""
    fields = {}
    for key in ("plate", "booking_ref", "name", "phone", "whatsapp", "email", "telegram_chat_id"):
        value = getattr(override, key) or getattr(base, key)
        fields[key] = value
    source = ",".join(part for part in (base.source, override.source) if part)
    return replace(base, **fields, source=source)


def store_booking_contacts(
    store: Store,
    bookings: Iterable[Booking],
    *,
    retention_days: float,
    default_country_code: str = "+44",
    now: float | None = None,
) -> int:
    """Persist contact details that came with the bookings, with an expiry attached."""
    ts = now if now is not None else time.time()
    stored = 0
    for booking in bookings:
        contact = contact_from_booking(booking, default_country_code)
        if not (contact.name or contact.phone or contact.email):
            continue
        store.upsert_contact(
            plate=booking.plate,
            booking_ref=booking.booking_ref,
            name=contact.name,
            phone=contact.phone,
            whatsapp=contact.whatsapp,
            email=contact.email,
            source=booking.source,
            expires_ts=booking.end_ts + retention_days * 86400,
            now=ts,
        )
        stored += 1
    return stored


def resolve(
    store: Store,
    plate: str,
    booking_ref: str = "",
    overrides: dict[str, Contact] | None = None,
) -> Contact | None:
    """Stored booking contact, overlaid with the manual file. None if nothing is known."""
    normalized = normalize_plate(plate)
    row = store.get_contact(plate, booking_ref)
    base = Contact(plate=plate, booking_ref=booking_ref)
    if row is not None:
        base = Contact(
            plate=plate,
            booking_ref=str(row.get("booking_ref", "") or ""),
            name=str(row.get("name", "") or ""),
            phone=str(row.get("phone", "") or ""),
            whatsapp=str(row.get("whatsapp", "") or "") or str(row.get("phone", "") or ""),
            email=str(row.get("email", "") or ""),
            telegram_chat_id=str(row.get("telegram_chat_id", "") or ""),
            source=str(row.get("source", "") or ""),
        )

    override = (overrides or {}).get(normalized)
    contact = merge(base, override) if override is not None else base
    return contact if contact.reachable or contact.name else None
