"""SQLite allowlist, contacts, incidents, audit log, and cooldown tracking."""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from parking_guard.plates import normalize_plate

SCHEMA_VERSION = 2

STATUS_OPEN = "open"
STATUS_NOTIFIED = "notified"
STATUS_ACKED = "acked"
STATUS_RESOLVED = "resolved"
STATUS_ESCALATED = "escalated"
STATUS_EXHAUSTED = "exhausted"

CLOSED_STATUSES = (STATUS_RESOLVED,)


@dataclass(frozen=True)
class Booking:
    plate: str
    plate_normalized: str
    start_ts: float
    end_ts: float
    booking_ref: str
    source: str
    name: str = ""
    phone: str = ""
    email: str = ""


class Store:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # The dispatcher and ack listeners run in their own threads.
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._init_schema()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _init_schema(self) -> None:
        with self._lock:
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
            self._migrate()

    def _migrate(self) -> None:
        current = int(self.get_meta("schema_version", "1") or 1)
        if current < 2:
            self._migrate_to_v2()
        self.set_meta("schema_version", str(SCHEMA_VERSION))

    def _migrate_to_v2(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
            CREATE TABLE IF NOT EXISTS contacts (
              plate_normalized TEXT NOT NULL,
              booking_ref TEXT NOT NULL,
              name TEXT NOT NULL DEFAULT '',
              phone TEXT NOT NULL DEFAULT '',
              whatsapp TEXT NOT NULL DEFAULT '',
              email TEXT NOT NULL DEFAULT '',
              telegram_chat_id TEXT NOT NULL DEFAULT '',
              source TEXT NOT NULL DEFAULT '',
              expires_ts REAL NOT NULL,
              updated_ts REAL NOT NULL,
              PRIMARY KEY (plate_normalized, booking_ref)
            );
            CREATE INDEX IF NOT EXISTS idx_contacts_plate ON contacts (plate_normalized);

            CREATE TABLE IF NOT EXISTS incidents (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              plate_normalized TEXT NOT NULL,
              plate_display TEXT NOT NULL,
              object_id TEXT NOT NULL,
              booking_ref TEXT NOT NULL DEFAULT '',
              kind TEXT NOT NULL,
              slot TEXT NOT NULL DEFAULT '',
              detail TEXT NOT NULL DEFAULT '',
              camera TEXT NOT NULL DEFAULT '',
              frigate_id TEXT NOT NULL DEFAULT '',
              status TEXT NOT NULL,
              ladder_index INTEGER NOT NULL DEFAULT 0,
              escalated INTEGER NOT NULL DEFAULT 0,
              opened_ts REAL NOT NULL,
              updated_ts REAL NOT NULL,
              next_action_ts REAL,
              acked_ts REAL,
              resolved_ts REAL
            );
            CREATE INDEX IF NOT EXISTS idx_incidents_status ON incidents (status);
            CREATE INDEX IF NOT EXISTS idx_incidents_object ON incidents (object_id, kind, status);

            CREATE TABLE IF NOT EXISTS notifications (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              incident_id INTEGER NOT NULL,
              channel TEXT NOT NULL,
              audience TEXT NOT NULL,
              target TEXT NOT NULL DEFAULT '',
              token TEXT UNIQUE,
              status TEXT NOT NULL,
              error TEXT NOT NULL DEFAULT '',
              delivery_id TEXT NOT NULL DEFAULT '',
              consumed INTEGER NOT NULL DEFAULT 0,
              sent_ts REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_notifications_incident ON notifications (incident_id);
            CREATE INDEX IF NOT EXISTS idx_notifications_token ON notifications (token);

            CREATE TABLE IF NOT EXISTS acks (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              incident_id INTEGER NOT NULL,
              notification_id INTEGER,
              channel TEXT NOT NULL,
              token TEXT NOT NULL DEFAULT '',
              ts REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS slot_state (
              slot TEXT PRIMARY KEY,
              plate_display TEXT NOT NULL DEFAULT '',
              object_id TEXT NOT NULL DEFAULT '',
              compliant INTEGER NOT NULL DEFAULT 1,
              updated_ts REAL NOT NULL
            );
            """
            )
            self._conn.commit()

    # ------------------------------------------------------------------ allowlist

    def replace_allowlist(self, bookings: Iterable[Booking]) -> int:
        now = time.time()
        with self._lock:
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
        with self._lock:
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

        row = self._active_booking_row(normalized, now, grace_seconds)
        if row is None:
            return False, "no_active_booking"
        return True, f"booking:{row['booking_ref']}:{row['source']}"

    def booking_ref_for(self, plate: str, now: float, grace_seconds: float = 0.0) -> str:
        normalized = normalize_plate(plate)
        if not normalized:
            return ""
        row = self._active_booking_row(normalized, now, grace_seconds)
        return str(row["booking_ref"]) if row is not None else ""

    def _active_booking_row(self, normalized: str, now: float, grace_seconds: float) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(
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

    # --------------------------------------------------------------------- events

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
        with self._lock:
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
        with self._lock:
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
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO cooldowns (plate_normalized, last_alert_ts) VALUES (?, ?)
                ON CONFLICT(plate_normalized) DO UPDATE SET last_alert_ts = excluded.last_alert_ts
                """,
                (normalized, ts),
            )
            self._conn.commit()

    # ------------------------------------------------------------------- contacts

    def upsert_contact(
        self,
        *,
        plate: str,
        booking_ref: str,
        name: str = "",
        phone: str = "",
        whatsapp: str = "",
        email: str = "",
        telegram_chat_id: str = "",
        source: str = "",
        expires_ts: float,
        now: float | None = None,
    ) -> None:
        ts = now if now is not None else time.time()
        normalized = normalize_plate(plate)
        if not normalized:
            return
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO contacts (
                  plate_normalized, booking_ref, name, phone, whatsapp, email,
                  telegram_chat_id, source, expires_ts, updated_ts
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(plate_normalized, booking_ref) DO UPDATE SET
                  name = CASE WHEN excluded.name != '' THEN excluded.name ELSE contacts.name END,
                  phone = CASE WHEN excluded.phone != '' THEN excluded.phone ELSE contacts.phone END,
                  whatsapp = CASE WHEN excluded.whatsapp != '' THEN excluded.whatsapp ELSE contacts.whatsapp END,
                  email = CASE WHEN excluded.email != '' THEN excluded.email ELSE contacts.email END,
                  telegram_chat_id = CASE
                    WHEN excluded.telegram_chat_id != '' THEN excluded.telegram_chat_id
                    ELSE contacts.telegram_chat_id END,
                  source = excluded.source,
                  expires_ts = MAX(excluded.expires_ts, contacts.expires_ts),
                  updated_ts = excluded.updated_ts
                """,
                (
                    normalized,
                    booking_ref,
                    name,
                    phone,
                    whatsapp,
                    email,
                    telegram_chat_id,
                    source,
                    expires_ts,
                    ts,
                ),
            )
            self._conn.commit()

    def get_contact(self, plate: str, booking_ref: str = "") -> dict[str, Any] | None:
        normalized = normalize_plate(plate)
        if not normalized:
            return None
        with self._lock:
            if booking_ref:
                row = self._conn.execute(
                    "SELECT * FROM contacts WHERE plate_normalized = ? AND booking_ref = ?",
                    (normalized, booking_ref),
                ).fetchone()
                if row is not None:
                    return dict(row)
            row = self._conn.execute(
                "SELECT * FROM contacts WHERE plate_normalized = ? ORDER BY updated_ts DESC LIMIT 1",
                (normalized,),
            ).fetchone()
        return dict(row) if row is not None else None

    def set_telegram_chat_id(self, plate: str, chat_id: str, now: float | None = None) -> None:
        normalized = normalize_plate(plate)
        ts = now if now is not None else time.time()
        with self._lock:
            self._conn.execute(
                "UPDATE contacts SET telegram_chat_id = ?, updated_ts = ? WHERE plate_normalized = ?",
                (chat_id, ts, normalized),
            )
            self._conn.commit()

    def purge_expired_contacts(self, now: float | None = None) -> int:
        ts = now if now is not None else time.time()
        with self._lock:
            cur = self._conn.execute("DELETE FROM contacts WHERE expires_ts < ?", (ts,))
            self._conn.commit()
            return cur.rowcount

    # ------------------------------------------------------------------ incidents

    def open_incident(
        self,
        *,
        plate: str,
        object_id: str,
        kind: str,
        slot: str = "",
        detail: str = "",
        camera: str = "",
        frigate_id: str = "",
        booking_ref: str = "",
        now: float | None = None,
    ) -> tuple[int, bool]:
        """Return ``(incident_id, created)``; an open incident is reused, not duplicated."""
        ts = now if now is not None else time.time()
        with self._lock:
            row = self._conn.execute(
                """
                SELECT id FROM incidents
                WHERE object_id = ? AND kind = ? AND status NOT IN (?)
                ORDER BY id DESC LIMIT 1
                """,
                (object_id, kind, STATUS_RESOLVED),
            ).fetchone()
            if row is not None:
                return int(row["id"]), False

            cur = self._conn.execute(
                """
                INSERT INTO incidents (
                  plate_normalized, plate_display, object_id, booking_ref, kind, slot, detail,
                  camera, frigate_id, status, ladder_index, escalated, opened_ts, updated_ts, next_action_ts
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, ?, ?, ?)
                """,
                (
                    normalize_plate(plate),
                    plate,
                    object_id,
                    booking_ref,
                    kind,
                    slot,
                    detail,
                    camera,
                    frigate_id,
                    STATUS_OPEN,
                    ts,
                    ts,
                    ts,
                ),
            )
            self._conn.commit()
            return int(cur.lastrowid), True

    def last_incident_ts(self, plate: str, kind: str = "") -> float | None:
        """When this plate was last pulled up about the same thing, resolved or not."""
        normalized = normalize_plate(plate)
        if not normalized:
            return None
        query = "SELECT MAX(opened_ts) AS ts FROM incidents WHERE plate_normalized = ?"
        params: list[Any] = [normalized]
        if kind:
            query += " AND kind = ?"
            params.append(kind)
        with self._lock:
            row = self._conn.execute(query, params).fetchone()
        return float(row["ts"]) if row is not None and row["ts"] is not None else None

    def get_incident(self, incident_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM incidents WHERE id = ?", (incident_id,)).fetchone()
        return dict(row) if row is not None else None

    def update_incident(self, incident_id: int, now: float | None = None, **fields: Any) -> None:
        if not fields:
            return
        ts = now if now is not None else time.time()
        fields["updated_ts"] = ts
        assignments = ", ".join(f"{key} = ?" for key in fields)
        with self._lock:
            self._conn.execute(
                f"UPDATE incidents SET {assignments} WHERE id = ?",
                (*fields.values(), incident_id),
            )
            self._conn.commit()

    def incidents_due(self, now: float | None = None) -> list[dict[str, Any]]:
        ts = now if now is not None else time.time()
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM incidents
                WHERE status NOT IN (?, ?)
                  AND next_action_ts IS NOT NULL
                  AND next_action_ts <= ?
                ORDER BY opened_ts
                """,
                (STATUS_RESOLVED, STATUS_EXHAUSTED, ts),
            ).fetchall()
        return [dict(row) for row in rows]

    def open_incidents(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM incidents WHERE status != ? ORDER BY opened_ts",
                (STATUS_RESOLVED,),
            ).fetchall()
        return [dict(row) for row in rows]

    def resolve_incidents_for_object(self, object_id: str, now: float | None = None) -> list[int]:
        ts = now if now is not None else time.time()
        with self._lock:
            rows = self._conn.execute(
                "SELECT id FROM incidents WHERE object_id = ? AND status != ?",
                (object_id, STATUS_RESOLVED),
            ).fetchall()
            ids = [int(row["id"]) for row in rows]
            if ids:
                self._conn.executemany(
                    """
                    UPDATE incidents
                    SET status = ?, resolved_ts = ?, updated_ts = ?, next_action_ts = NULL
                    WHERE id = ?
                    """,
                    [(STATUS_RESOLVED, ts, ts, incident_id) for incident_id in ids],
                )
                self._conn.commit()
        return ids

    # -------------------------------------------------------------- notifications

    def record_notification(
        self,
        *,
        incident_id: int,
        channel: str,
        audience: str,
        target: str = "",
        token: str = "",
        status: str = "sent",
        error: str = "",
        delivery_id: str = "",
        now: float | None = None,
    ) -> int:
        ts = now if now is not None else time.time()
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO notifications (
                  incident_id, channel, audience, target, token, status, error, delivery_id, sent_ts
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    incident_id,
                    channel,
                    audience,
                    target,
                    token or None,
                    status,
                    error,
                    delivery_id,
                    ts,
                ),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def update_notification(self, notification_id: int, **fields: Any) -> None:
        if not fields:
            return
        assignments = ", ".join(f"{key} = ?" for key in fields)
        with self._lock:
            self._conn.execute(
                f"UPDATE notifications SET {assignments} WHERE id = ?",
                (*fields.values(), notification_id),
            )
            self._conn.commit()

    def notification_for_token(self, token: str) -> dict[str, Any] | None:
        if not token:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM notifications WHERE token = ?",
                (token,),
            ).fetchone()
        return dict(row) if row is not None else None

    def consume_token(self, token: str) -> dict[str, Any] | None:
        """Claim an ack token exactly once. Returns the notification row, or None."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM notifications WHERE token = ? AND consumed = 0",
                (token,),
            ).fetchone()
            if row is None:
                return None
            self._conn.execute(
                "UPDATE notifications SET consumed = 1 WHERE id = ?",
                (row["id"],),
            )
            self._conn.commit()
        return dict(row)

    def record_ack(
        self,
        *,
        incident_id: int,
        channel: str,
        notification_id: int | None = None,
        token: str = "",
        now: float | None = None,
    ) -> None:
        ts = now if now is not None else time.time()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO acks (incident_id, notification_id, channel, token, ts)
                VALUES (?, ?, ?, ?, ?)
                """,
                (incident_id, notification_id, channel, token, ts),
            )
            self._conn.commit()

    def latest_unconsumed_notification(self, target: str, channels: tuple[str, ...] = ()) -> dict[str, Any] | None:
        """Used for free-text replies ('OK'), which carry no token of their own."""
        if not target:
            return None
        query = """
            SELECT n.* FROM notifications n
            JOIN incidents i ON i.id = n.incident_id
            WHERE n.target = ? AND n.consumed = 0 AND n.token IS NOT NULL
              AND n.status = 'sent'
              AND i.status NOT IN (?, ?)
        """
        params: list[Any] = [target, STATUS_RESOLVED, STATUS_EXHAUSTED]
        if channels:
            placeholders = ", ".join("?" for _ in channels)
            query += f" AND n.channel IN ({placeholders})"
            params.extend(channels)
        query += " ORDER BY n.id DESC LIMIT 1"

        with self._lock:
            row = self._conn.execute(query, params).fetchone()
        return dict(row) if row is not None else None

    def notifications_for_incident(self, incident_id: int) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM notifications WHERE incident_id = ? ORDER BY id",
                (incident_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    # ----------------------------------------------------------------- slot state

    def set_slot_state(
        self,
        *,
        slot: str,
        plate: str = "",
        object_id: str = "",
        compliant: bool = True,
        now: float | None = None,
    ) -> None:
        ts = now if now is not None else time.time()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO slot_state (slot, plate_display, object_id, compliant, updated_ts)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(slot) DO UPDATE SET
                  plate_display = excluded.plate_display,
                  object_id = excluded.object_id,
                  compliant = excluded.compliant,
                  updated_ts = excluded.updated_ts
                """,
                (slot, plate, object_id, 1 if compliant else 0, ts),
            )
            self._conn.commit()

    def slot_states(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM slot_state ORDER BY slot").fetchall()
        return [dict(row) for row in rows]

    # ----------------------------------------------------------------------- meta

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            self._conn.commit()

    def get_meta(self, key: str, default: str = "") -> str:
        with self._lock:
            row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return default if row is None else str(row["value"])
