"""Luna / manual CSV import for booking allowlists."""

from __future__ import annotations

import csv
import time
from pathlib import Path

from parking_guard.plates import normalize_plate
from parking_guard.store import Booking


def bookings_from_rows(rows: list[dict]) -> list[Booking]:
    bookings: list[Booking] = []
    for row in rows:
        plate = str(row.get("plate", "")).strip()
        if not plate:
            continue
        bookings.append(
            Booking(
                plate=plate,
                plate_normalized=normalize_plate(plate),
                start_ts=float(row.get("start_ts", time.time())),
                end_ts=float(row.get("end_ts", time.time() + 3600)),
                booking_ref=str(row.get("booking_ref", plate)),
                source=str(row.get("source", "import")),
                name=str(row.get("name", "") or ""),
                phone=str(row.get("phone", "") or ""),
                email=str(row.get("email", "") or ""),
            )
        )
    return bookings


def import_csv(path: Path) -> list[Booking]:
    if not path.exists() or not path.is_file():
        return []

    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        rows: list[dict] = []
        for row in reader:
            plate = _field(row, ["plate", "registration", "vehicle_registration", "vrm"])
            if not plate:
                continue
            start = _field(row, ["start", "start_time", "starts_at", "from"])
            end = _field(row, ["end", "end_time", "ends_at", "to"])
            ref = _field(row, ["booking_ref", "reference", "id", "booking_id"]) or plate
            rows.append(
                {
                    "plate": plate,
                    "start_ts": _parse_optional_ts(start, default_past=True),
                    "end_ts": _parse_optional_ts(end, default_past=False),
                    "booking_ref": ref,
                    "source": "luna-csv",
                    "name": _field(row, ["name", "driver", "driver_name", "customer_name"]),
                    "phone": _field(row, ["phone", "mobile", "telephone", "driver_phone"]),
                    "email": _field(row, ["email", "driver_email", "customer_email"]),
                }
            )
    return bookings_from_rows(rows)


def _field(row: dict[str, str], keys: list[str]) -> str:
    for key in keys:
        for candidate in (key, key.lower(), key.upper()):
            if candidate in row and row[candidate]:
                return str(row[candidate]).strip()
    return ""


def _parse_optional_ts(value: str, default_past: bool) -> float:
    now = time.time()
    if not value:
        return now - 3600 if default_past else now + 3600
    try:
        return float(value)
    except ValueError:
        from datetime import datetime

        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return now - 3600 if default_past else now + 3600
