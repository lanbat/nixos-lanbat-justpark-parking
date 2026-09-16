"""SQLite allowlist, audit log, and cooldown tracking."""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from parking_guard.plates import normalize_plate


@dataclass(frozen=True)
class Booking:
    plate: str
    plate_normalized: str
    start_ts: float
    end_ts: float
    booking_ref: str
    source: str


class Store:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def close(self) -> None:
        self._conn.close()

    def _init_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS allowlist (
              plate_normalized TEXT NOT NULL,
              plate_display TEXT NOT NULL,
              start_ts REAL NOT NULL,
              end_ts REAL NOT NULL,
              booking_ref TEXT NOT NULL,
              source TEXT NOT NULL,
              PRIMARY KEY (plate_normalized, booking_ref, start_ts)
            );
            CREATE INDEX IF NOT EXISTS idx_allowlist_window
              ON allowlist (plate_normalized, start_ts, end_ts);

            CREATE TABLE IF NOT EXISTS events (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              ts REAL NOT NULL,
              plate_display TEXT NOT NULL,
              plate_normalized TEXT NOT NULL,
              authorized INTEGER NOT NULL,
              reason TEXT NOT NULL,
              camera TEXT,
              frigate_id TEXT,
              score REAL
            );

            CREATE TABLE IF NOT EXISTS cooldowns (
              plate_normalized TEXT PRIMARY KEY,
              last_alert_ts REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS meta (
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL
            );
            """
        )
        self._conn.commit()

    def replace_allowlist(self, bookings: Iterable[Booking]) -> int:
        now = time.time()
        cur = self._conn.cursor()
        cur.execute("DELETE FROM allowlist WHERE end_ts < ?", (now,))
        cur.execute("DELETE FROM allowlist")
        rows = list(bookings)
        cur.executemany(
            """
            INSERT INTO allowlist (
              plate_normalized, plate_display, start_ts, end_ts, booking_ref, source
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    b.plate_normalized,
                    b.plate,
                    b.start_ts,
                    b.end_ts,
                    b.booking_ref,
                    b.source,
                )
                for b in rows
            ],
        )
        self._conn.commit()
        self.set_meta("allowlist_count", str(len(rows)))
        self.set_meta("allowlist_synced_at", str(now))
        return len(rows)

    def active_booking_count(self, now: float | None = None) -> int:
        ts = now if now is not None else time.time()
        row = self._conn.execute(
            "SELECT COUNT(*) AS c FROM allowlist WHERE start_ts <= ? AND end_ts >= ?",
            (ts, ts),
        ).fetchone()
        return int(row["c"])

    def is_authorized(
        self,
        plate: str,
        now: float,
        grace_seconds: float,
    ) -> tuple[bool, str]:
        normalized = normalize_plate(plate)
        if not normalized:
            return False, "empty_plate"

        row = self._conn.execute(
            """
            SELECT booking_ref, source FROM allowlist
            WHERE plate_normalized = ?
              AND start_ts <= ?
              AND end_ts >= ?
            ORDER BY end_ts DESC
            LIMIT 1
            """,
            (normalized, now + grace_seconds, now - grace_seconds),
        ).fetchone()

        if row is None:
            return False, "no_active_booking"
        return True, f"booking:{row['booking_ref']}:{row['source']}"

    def record_event(
        self,
        *,
        plate: str,
        authorized: bool,
        reason: str,
        camera: str | None,
        frigate_id: str | None,
        score: float | None,
        ts: float | None = None,
    ) -> None:
        normalized = normalize_plate(plate)
        self._conn.execute(
            """
            INSERT INTO events (
              ts, plate_display, plate_normalized, authorized, reason, camera, frigate_id, score
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ts if ts is not None else time.time(),
                plate,
                normalized,
                1 if authorized else 0,
                reason,
                camera,
                frigate_id,
                score,
            ),
        )
        self._conn.commit()

    def should_alert(self, plate: str, cooldown_seconds: float, now: float | None = None) -> bool:
        ts = now if now is not None else time.time()
        normalized = normalize_plate(plate)
        row = self._conn.execute(
            "SELECT last_alert_ts FROM cooldowns WHERE plate_normalized = ?",
            (normalized,),
        ).fetchone()
        if row is None:
            return True
        return (ts - float(row["last_alert_ts"])) >= cooldown_seconds

    def mark_alerted(self, plate: str, now: float | None = None) -> None:
        ts = now if now is not None else time.time()
        normalized = normalize_plate(plate)
        self._conn.execute(
            """
            INSERT INTO cooldowns (plate_normalized, last_alert_ts) VALUES (?, ?)
            ON CONFLICT(plate_normalized) DO UPDATE SET last_alert_ts = excluded.last_alert_ts
            """,
            (normalized, ts),
        )
        self._conn.commit()

    def set_meta(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self._conn.commit()

    def get_meta(self, key: str, default: str = "") -> str:
        row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return default if row is None else str(row["value"])
