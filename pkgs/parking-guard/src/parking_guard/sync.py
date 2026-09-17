"""Allowlist sync — JustPark API, CSV import, and resident plates."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from parking_guard.config import load_guard_config
from parking_guard.contacts import store_booking_contacts
from parking_guard.justpark.client import (
    JustParkClient,
    bookings_from_resident_plates,
    load_config_from_env,
    sessions_to_bookings,
)
from parking_guard.justpark.csv_import import import_csv
from parking_guard.store import Store

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)


def run_sync(config_path: Path) -> int:
    cfg = load_guard_config(config_path)
    store = Store(cfg.database_dsn)

    bookings = []

    jp_config = load_config_from_env()
    if jp_config is not None:
        try:
            client = JustParkClient(jp_config)
            sessions = client.fetch_active_sessions()
            bookings.extend(sessions_to_bookings(sessions))
            log.info("justpark api: %s sessions", len(sessions))
        except Exception as exc:
            log.warning("justpark api sync failed: %s", exc)

    csv_bookings = import_csv(cfg.csv_import_path)
    if csv_bookings:
        bookings.extend(csv_bookings)
        log.info("csv import: %s bookings from %s", len(csv_bookings), cfg.csv_import_path)

    if cfg.resident_plates:
        bookings.extend(bookings_from_resident_plates(list(cfg.resident_plates)))
        log.info("resident plates: %s", len(cfg.resident_plates))

    count = store.replace_allowlist(bookings)
    log.info("allowlist updated: %s entries", count)

    contacts = store_booking_contacts(
        store,
        bookings,
        retention_days=cfg.contacts.retention_days,
        default_country_code=cfg.contacts.default_country_code,
    )
    purged = store.purge_expired_contacts()
    log.info("contacts stored: %s, expired contacts purged: %s", contacts, purged)

    store.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sync JustPark / CSV allowlist")
    parser.add_argument(
        "--config",
        default="/etc/parking-guard/config.json",
        type=Path,
        help="Path to parking-guard JSON config",
    )
    args = parser.parse_args(argv)
    return run_sync(args.config)


if __name__ == "__main__":
    raise SystemExit(main())
