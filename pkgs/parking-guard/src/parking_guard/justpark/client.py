"""JustPark ParkHub API client (partner enforcement / sessions)."""

from __future__ import annotations

import base64
import os
import time
from dataclasses import dataclass
from typing import Any

import requests

from parking_guard.justpark.csv_import import bookings_from_rows
from parking_guard.plates import normalize_plate


@dataclass(frozen=True)
class JustParkConfig:
    base_url: str
    api_key: str
    username: str
    password: str
    site_id: str


def load_config_from_env() -> JustParkConfig | None:
    api_key = os.environ.get("JUSTPARK_API_KEY", "").strip()
    username = os.environ.get("JUSTPARK_USERNAME", "").strip()
    password = os.environ.get("JUSTPARK_PASSWORD", "").strip()
    site_id = os.environ.get("JUSTPARK_SITE_ID", "").strip()
    if not all([api_key, username, password, site_id]):
        return None
    base_url = os.environ.get("JUSTPARK_BASE_URL", "https://partners.sandbox.parkhub.com").rstrip("/")
    return JustParkConfig(
        base_url=base_url,
        api_key=api_key,
        username=username,
        password=password,
        site_id=site_id,
    )


class JustParkClient:
    """Thin HTTP client — endpoint paths are configurable for sandbox drift."""

    def __init__(self, config: JustParkConfig, sessions_path: str = "/sessions") -> None:
        self.config = config
        self.sessions_path = sessions_path
        token = base64.b64encode(f"{config.username}:{config.password}".encode()).decode()
        self._headers = {
            "x-api-key": config.api_key,
            "Authorization": f"Basic {token}",
            "Accept": "application/json",
        }

    def fetch_active_sessions(self, since_ts: float | None = None) -> list[dict[str, Any]]:
        params: dict[str, str] = {"siteId": self.config.site_id}
        if since_ts is not None:
            params["since"] = str(int(since_ts))

        url = f"{self.config.base_url}{self.sessions_path}"
        response = requests.get(url, headers=self._headers, params=params, timeout=30)
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            for key in ("sessions", "data", "items"):
                if key in payload and isinstance(payload[key], list):
                    return payload[key]
        return []


PLATE_PATHS = (
    ("plate",),
    ("licensePlate",),
    ("vehicleRegistration",),
    ("vrm",),
    ("vehicle", "licensePlate"),
    ("vehicle", "plate"),
    ("identifiedCredentials", 0, "identifier", "id"),
)

NAME_PATHS = (
    ("customerName",),
    ("driverName",),
    ("customer", "name"),
    ("customer", "fullName"),
    ("driver", "name"),
    ("contact", "name"),
    ("user", "name"),
)

PHONE_PATHS = (
    ("customerPhone",),
    ("driverPhone",),
    ("phone",),
    ("mobile",),
    ("telephone",),
    ("customer", "phone"),
    ("customer", "mobile"),
    ("customer", "phoneNumber"),
    ("driver", "phone"),
    ("contact", "phone"),
    ("user", "phone"),
)

EMAIL_PATHS = (
    ("customerEmail",),
    ("driverEmail",),
    ("email",),
    ("customer", "email"),
    ("driver", "email"),
    ("contact", "email"),
    ("user", "email"),
)


def sessions_to_bookings(sessions: list[dict[str, Any]], source: str = "justpark-api") -> list:
    rows = []
    now = time.time()
    for session in sessions:
        plate = _first(session, PLATE_PATHS)
        if not plate:
            continue
        start = _parse_ts(
            session.get("start")
            or session.get("startTime")
            or session.get("startsAt")
            or session.get("actualStart"),
            now,
        )
        end = _parse_ts(
            session.get("end")
            or session.get("endTime")
            or session.get("endsAt")
            or session.get("actualEnd"),
            now + 3600,
        )
        ref = str(session.get("id") or session.get("bookingRef") or session.get("reference") or plate)
        rows.append(
            {
                "plate": str(plate),
                "start_ts": start,
                "end_ts": end,
                "booking_ref": ref,
                "source": source,
                # Contact details are not part of the documented enforcement schema,
                # so take them where a deployment happens to have them and move on.
                "name": _first(session, NAME_PATHS),
                "phone": _first(session, PHONE_PATHS),
                "email": _first(session, EMAIL_PATHS),
            }
        )
    return bookings_from_rows(rows)


def _first(session: dict[str, Any], paths: tuple[tuple[Any, ...], ...]) -> str:
    for path in paths:
        value: Any = session
        for key in path:
            if isinstance(key, int):
                if not isinstance(value, (list, tuple)) or len(value) <= key:
                    value = None
                    break
                value = value[key]
                continue
            if not isinstance(value, dict):
                value = None
                break
            value = value.get(key)
        if isinstance(value, (str, int, float)) and str(value).strip():
            return str(value).strip()
    return ""


def _parse_ts(value: Any, default: float) -> float:
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return default
    try:
        return float(text)
    except ValueError:
        from datetime import datetime

        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return default


def bookings_from_resident_plates(plates: list[str]) -> list:
    now = time.time()
    end = now + 365 * 24 * 3600
    rows = []
    for plate in plates:
        normalized = normalize_plate(plate)
        if not normalized:
            continue
        rows.append(
            {
                "plate": plate,
                "start_ts": now - 3600,
                "end_ts": end,
                "booking_ref": f"resident:{normalized}",
                "source": "resident",
            }
        )
    return bookings_from_rows(rows)
