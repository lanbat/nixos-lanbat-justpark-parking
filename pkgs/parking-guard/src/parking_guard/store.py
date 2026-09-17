"""PostgreSQL storage: allowlist, contacts, incidents, audit log, and cooldowns.

The database lives on the lanbat always-on PostgreSQL instance, declared by the
NixOS module as ``lanbat.postgresql.databases.parking-guard``. The service logs in
over the unix socket as the ``parking-guard`` system user, so there is no password
anywhere in this process.

Timestamps are epoch seconds (``double precision``) because everything upstream —
Frigate events, booking windows, ladder timeouts — is already ``time.time()``.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from parking_guard.plates import normalize_plate

log = logging.getLogger(__name__)

SCHEMA_VERSION = 2

STATUS_OPEN = "open"
STATUS_NOTIFIED = "notified"
STATUS_ACKED = "acked"
STATUS_RESOLVED = "resolved"
STATUS_ESCALATED = "escalated"
STATUS_EXHAUSTED = "exhausted"

CLOSED_STATUSES = (STATUS_RESOLVED,)

SCHEMA = """
CREATE TABLE IF NOT EXISTS allowlist (
  plate_normalized TEXT NOT NULL,
  plate_display TEXT NOT NULL,
  start_ts DOUBLE PRECISION NOT NULL,
  end_ts DOUBLE PRECISION NOT NULL,
  booking_ref TEXT NOT NULL,
  source TEXT NOT NULL,
  PRIMARY KEY (plate_normalized, booking_ref, start_ts)
);
CREATE INDEX IF NOT EXISTS idx_allowlist_window
  ON allowlist (plate_normalized, start_ts, end_ts);

CREATE TABLE IF NOT EXISTS events (
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  ts DOUBLE PRECISION NOT NULL,
  plate_display TEXT NOT NULL,
  plate_normalized TEXT NOT NULL,
  authorized BOOLEAN NOT NULL,
  reason TEXT NOT NULL,
  camera TEXT,
  frigate_id TEXT,
  score DOUBLE PRECISION
);

CREATE TABLE IF NOT EXISTS cooldowns (
  plate_normalized TEXT PRIMARY KEY,
  last_alert_ts DOUBLE PRECISION NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS contacts (
  plate_normalized TEXT NOT NULL,
  booking_ref TEXT NOT NULL,
  name TEXT NOT NULL DEFAULT '',
  phone TEXT NOT NULL DEFAULT '',
  whatsapp TEXT NOT NULL DEFAULT '',
  email TEXT NOT NULL DEFAULT '',
  telegram_chat_id TEXT NOT NULL DEFAULT '',
  source TEXT NOT NULL DEFAULT '',
  expires_ts DOUBLE PRECISION NOT NULL,
  updated_ts DOUBLE PRECISION NOT NULL,
  PRIMARY KEY (plate_normalized, booking_ref)
);
CREATE INDEX IF NOT EXISTS idx_contacts_plate ON contacts (plate_normalized);

CREATE TABLE IF NOT EXISTS incidents (
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
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
  opened_ts DOUBLE PRECISION NOT NULL,
  updated_ts DOUBLE PRECISION NOT NULL,
  next_action_ts DOUBLE PRECISION,
  acked_ts DOUBLE PRECISION,
  resolved_ts DOUBLE PRECISION
);
CREATE INDEX IF NOT EXISTS idx_incidents_status ON incidents (status);
CREATE INDEX IF NOT EXISTS idx_incidents_object ON incidents (object_id, kind, status);

CREATE TABLE IF NOT EXISTS notifications (
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  incident_id BIGINT NOT NULL REFERENCES incidents (id) ON DELETE CASCADE,
  channel TEXT NOT NULL,
  audience TEXT NOT NULL,
  target TEXT NOT NULL DEFAULT '',
  token TEXT UNIQUE,
  status TEXT NOT NULL,
  error TEXT NOT NULL DEFAULT '',
  delivery_id TEXT NOT NULL DEFAULT '',
  consumed BOOLEAN NOT NULL DEFAULT FALSE,
  sent_ts DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_notifications_incident ON notifications (incident_id);
CREATE INDEX IF NOT EXISTS idx_notifications_token ON notifications (token);

CREATE TABLE IF NOT EXISTS acks (
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  incident_id BIGINT NOT NULL REFERENCES incidents (id) ON DELETE CASCADE,
  notification_id BIGINT,
  channel TEXT NOT NULL,
  token TEXT NOT NULL DEFAULT '',
  ts DOUBLE PRECISION NOT NULL
);

CREATE TABLE IF NOT EXISTS slot_state (
  slot TEXT PRIMARY KEY,
  plate_display TEXT NOT NULL DEFAULT '',
  object_id TEXT NOT NULL DEFAULT '',
  compliant BOOLEAN NOT NULL DEFAULT TRUE,
  updated_ts DOUBLE PRECISION NOT NULL
);
"""


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
    """One connection shared by the MQTT, dispatcher and ack-listener threads.

    psycopg connections are not safe for concurrent use, so every statement runs
    under a lock. The volume here is a handful of queries a minute, so a pool
    would buy nothing; what does matter is surviving a PostgreSQL restart, hence
    the reconnect-and-retry in :meth:`_run`.
    """

    def __init__(self, dsn: str, *, connect_timeout: int = 10) -> None:
        self.dsn = dsn
        self.connect_timeout = connect_timeout
        self._lock = threading.RLock()
        self._conn: psycopg.Connection | None = None
        self._init_schema()

    # ------------------------------------------------------------------ plumbing

    def _connect(self) -> psycopg.Connection:
        return psycopg.connect(
            self.dsn,
            autocommit=True,
            connect_timeout=self.connect_timeout,
            row_factory=dict_row,
        )

    def _connection(self) -> psycopg.Connection:
        if self._conn is None or self._conn.closed:
            self._conn = self._connect()
        return self._conn

    def _reset(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except psycopg.Error:
                pass
        self._conn = None

    def _run(
        self,
        query: str | sql.Composed | sql.SQL,
        params: Sequence[Any] = (),
        *,
        fetch: str | None = None,
    ) -> Any:
        """Run one statement, reconnecting once if the server went away."""
        with self._lock:
            for attempt in (0, 1):
                try:
                    with self._connection().cursor() as cur:
                        cur.execute(query, params)
                        if fetch == "one":
                            return cur.fetchone()
                        if fetch == "all":
                            return cur.fetchall()
                        return cur.rowcount
                except (psycopg.OperationalError, psycopg.InterfaceError):
                    self._reset()
                    if attempt:
                        raise
                    log.warning("lost the database connection, reconnecting")
            return None

    def close(self) -> None:
        with self._lock:
            self._reset()

    def _init_schema(self) -> None:
        with self._lock:
            with self._connection().cursor() as cur:
                cur.execute(SCHEMA)
            self.set_meta("schema_version", str(SCHEMA_VERSION))

    # ------------------------------------------------------------------ allowlist

    def replace_allowlist(self, bookings: Iterable[Booking]) -> int:
        now = time.time()
        rows = list(bookings)
        with self._lock:
            for attempt in (0, 1):
                try:
                    conn = self._connection()
                    with conn.transaction(), conn.cursor() as cur:
                        cur.execute("DELETE FROM allowlist")
                        cur.executemany(
                            """
                            INSERT INTO allowlist (
                              plate_normalized, plate_display, start_ts, end_ts, booking_ref, source
                            ) VALUES (%s, %s, %s, %s, %s, %s)
                            ON CONFLICT (plate_normalized, booking_ref, start_ts) DO NOTHING
                            """,
                            [
                                (b.plate_normalized, b.plate, b.start_ts, b.end_ts, b.booking_ref, b.source)
                                for b in rows
                            ],
                        )
                    break
                except (psycopg.OperationalError, psycopg.InterfaceError):
                    self._reset()
                    if attempt:
                        raise

        self.set_meta("allowlist_count", str(len(rows)))
        self.set_meta("allowlist_synced_at", str(now))
        return len(rows)

    def active_booking_count(self, now: float | None = None) -> int:
        ts = now if now is not None else time.time()
        row = self._run(
            "SELECT COUNT(*) AS c FROM allowlist WHERE start_ts <= %s AND end_ts >= %s",
            (ts, ts),
            fetch="one",
        )
        return int(row["c"]) if row else 0

    def is_authorized(self, plate: str, now: float, grace_seconds: float) -> tuple[bool, str]:
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

    def _active_booking_row(self, normalized: str, now: float, grace_seconds: float) -> dict[str, Any] | None:
        return self._run(
            """
            SELECT booking_ref, source FROM allowlist
            WHERE plate_normalized = %s
              AND start_ts <= %s
              AND end_ts >= %s
            ORDER BY end_ts DESC
            LIMIT 1
            """,
            (normalized, now + grace_seconds, now - grace_seconds),
            fetch="one",
        )

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
        self._run(
            """
            INSERT INTO events (
              ts, plate_display, plate_normalized, authorized, reason, camera, frigate_id, score
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                ts if ts is not None else time.time(),
                plate,
                normalize_plate(plate),
                bool(authorized),
                reason,
                camera,
                frigate_id,
                score,
            ),
        )

    def should_alert(self, plate: str, cooldown_seconds: float, now: float | None = None) -> bool:
        ts = now if now is not None else time.time()
        row = self._run(
            "SELECT last_alert_ts FROM cooldowns WHERE plate_normalized = %s",
            (normalize_plate(plate),),
            fetch="one",
        )
        if row is None:
            return True
        return (ts - float(row["last_alert_ts"])) >= cooldown_seconds

    def mark_alerted(self, plate: str, now: float | None = None) -> None:
        self._run(
            """
            INSERT INTO cooldowns (plate_normalized, last_alert_ts) VALUES (%s, %s)
            ON CONFLICT (plate_normalized) DO UPDATE SET last_alert_ts = EXCLUDED.last_alert_ts
            """,
            (normalize_plate(plate), now if now is not None else time.time()),
        )

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
        normalized = normalize_plate(plate)
        if not normalized:
            return
        self._run(
            """
            INSERT INTO contacts (
              plate_normalized, booking_ref, name, phone, whatsapp, email,
              telegram_chat_id, source, expires_ts, updated_ts
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (plate_normalized, booking_ref) DO UPDATE SET
              name = CASE WHEN EXCLUDED.name <> '' THEN EXCLUDED.name ELSE contacts.name END,
              phone = CASE WHEN EXCLUDED.phone <> '' THEN EXCLUDED.phone ELSE contacts.phone END,
              whatsapp = CASE WHEN EXCLUDED.whatsapp <> '' THEN EXCLUDED.whatsapp ELSE contacts.whatsapp END,
              email = CASE WHEN EXCLUDED.email <> '' THEN EXCLUDED.email ELSE contacts.email END,
              telegram_chat_id = CASE
                WHEN EXCLUDED.telegram_chat_id <> '' THEN EXCLUDED.telegram_chat_id
                ELSE contacts.telegram_chat_id END,
              source = EXCLUDED.source,
              expires_ts = GREATEST(EXCLUDED.expires_ts, contacts.expires_ts),
              updated_ts = EXCLUDED.updated_ts
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
                now if now is not None else time.time(),
            ),
        )

    def get_contact(self, plate: str, booking_ref: str = "") -> dict[str, Any] | None:
        normalized = normalize_plate(plate)
        if not normalized:
            return None
        if booking_ref:
            row = self._run(
                "SELECT * FROM contacts WHERE plate_normalized = %s AND booking_ref = %s",
                (normalized, booking_ref),
                fetch="one",
            )
            if row is not None:
                return row
        return self._run(
            "SELECT * FROM contacts WHERE plate_normalized = %s ORDER BY updated_ts DESC LIMIT 1",
            (normalized,),
            fetch="one",
        )

    def set_telegram_chat_id(self, plate: str, chat_id: str, now: float | None = None) -> None:
        self._run(
            "UPDATE contacts SET telegram_chat_id = %s, updated_ts = %s WHERE plate_normalized = %s",
            (chat_id, now if now is not None else time.time(), normalize_plate(plate)),
        )

    def purge_expired_contacts(self, now: float | None = None) -> int:
        return int(
            self._run(
                "DELETE FROM contacts WHERE expires_ts < %s",
                (now if now is not None else time.time(),),
            )
            or 0
        )

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
        """Return ``(incident_id, created)``. One statement, so two threads racing
        on the same car cannot both open an incident."""
        ts = now if now is not None else time.time()
        row = self._run(
            """
            WITH existing AS (
              SELECT id FROM incidents
              WHERE object_id = %(object_id)s AND kind = %(kind)s AND status <> %(resolved)s
              ORDER BY id DESC
              LIMIT 1
            ), inserted AS (
              INSERT INTO incidents (
                plate_normalized, plate_display, object_id, booking_ref, kind, slot, detail,
                camera, frigate_id, status, opened_ts, updated_ts, next_action_ts
              )
              SELECT %(plate_normalized)s, %(plate)s, %(object_id)s, %(booking_ref)s, %(kind)s,
                     %(slot)s, %(detail)s, %(camera)s, %(frigate_id)s, %(status)s, %(ts)s, %(ts)s, %(ts)s
              WHERE NOT EXISTS (SELECT 1 FROM existing)
              RETURNING id
            )
            SELECT id, TRUE AS created FROM inserted
            UNION ALL
            SELECT id, FALSE AS created FROM existing
            """,
            {
                "object_id": object_id,
                "kind": kind,
                "resolved": STATUS_RESOLVED,
                "plate_normalized": normalize_plate(plate),
                "plate": plate,
                "booking_ref": booking_ref,
                "slot": slot,
                "detail": detail,
                "camera": camera,
                "frigate_id": frigate_id,
                "status": STATUS_OPEN,
                "ts": ts,
            },
            fetch="one",
        )
        return int(row["id"]), bool(row["created"])

    def last_incident_ts(self, plate: str, kind: str = "") -> float | None:
        """When this plate was last pulled up about the same thing, resolved or not."""
        normalized = normalize_plate(plate)
        if not normalized:
            return None
        if kind:
            row = self._run(
                "SELECT MAX(opened_ts) AS ts FROM incidents WHERE plate_normalized = %s AND kind = %s",
                (normalized, kind),
                fetch="one",
            )
        else:
            row = self._run(
                "SELECT MAX(opened_ts) AS ts FROM incidents WHERE plate_normalized = %s",
                (normalized,),
                fetch="one",
            )
        return float(row["ts"]) if row is not None and row["ts"] is not None else None

    def get_incident(self, incident_id: int) -> dict[str, Any] | None:
        return self._run("SELECT * FROM incidents WHERE id = %s", (incident_id,), fetch="one")

    def update_incident(self, incident_id: int, now: float | None = None, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_ts"] = now if now is not None else time.time()
        statement = sql.SQL("UPDATE incidents SET {assignments} WHERE id = {id}").format(
            assignments=sql.SQL(", ").join(
                sql.SQL("{} = {}").format(sql.Identifier(key), sql.Placeholder(key)) for key in fields
            ),
            id=sql.Placeholder("__id"),
        )
        self._run(statement, {**fields, "__id": incident_id})

    def incidents_due(self, now: float | None = None) -> list[dict[str, Any]]:
        return list(
            self._run(
                """
                SELECT * FROM incidents
                WHERE status NOT IN (%s, %s)
                  AND next_action_ts IS NOT NULL
                  AND next_action_ts <= %s
                ORDER BY opened_ts
                """,
                (STATUS_RESOLVED, STATUS_EXHAUSTED, now if now is not None else time.time()),
                fetch="all",
            )
            or []
        )

    def open_incidents(self) -> list[dict[str, Any]]:
        return list(
            self._run(
                "SELECT * FROM incidents WHERE status <> %s ORDER BY opened_ts",
                (STATUS_RESOLVED,),
                fetch="all",
            )
            or []
        )

    def resolve_incidents_for_object(self, object_id: str, now: float | None = None) -> list[int]:
        ts = now if now is not None else time.time()
        rows = self._run(
            """
            UPDATE incidents
            SET status = %s, resolved_ts = %s, updated_ts = %s, next_action_ts = NULL
            WHERE object_id = %s AND status <> %s
            RETURNING id
            """,
            (STATUS_RESOLVED, ts, ts, object_id, STATUS_RESOLVED),
            fetch="all",
        )
        return [int(row["id"]) for row in rows or []]

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
        row = self._run(
            """
            INSERT INTO notifications (
              incident_id, channel, audience, target, token, status, error, delivery_id, sent_ts
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
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
                now if now is not None else time.time(),
            ),
            fetch="one",
        )
        return int(row["id"])

    def update_notification(self, notification_id: int, **fields: Any) -> None:
        if not fields:
            return
        statement = sql.SQL("UPDATE notifications SET {assignments} WHERE id = {id}").format(
            assignments=sql.SQL(", ").join(
                sql.SQL("{} = {}").format(sql.Identifier(key), sql.Placeholder(key)) for key in fields
            ),
            id=sql.Placeholder("__id"),
        )
        self._run(statement, {**fields, "__id": notification_id})

    def notification_for_token(self, token: str) -> dict[str, Any] | None:
        if not token:
            return None
        return self._run("SELECT * FROM notifications WHERE token = %s", (token,), fetch="one")

    def consume_token(self, token: str) -> dict[str, Any] | None:
        """Claim an ack token exactly once. A single statement, so a link tapped
        twice (or by two people) can only be claimed by one of them."""
        if not token:
            return None
        return self._run(
            """
            UPDATE notifications SET consumed = TRUE
            WHERE token = %s AND consumed = FALSE
            RETURNING *
            """,
            (token,),
            fetch="one",
        )

    def latest_unconsumed_notification(self, target: str, channels: tuple[str, ...] = ()) -> dict[str, Any] | None:
        """Used for free-text replies ('OK'), which carry no token of their own."""
        if not target:
            return None
        query = """
            SELECT n.* FROM notifications n
            JOIN incidents i ON i.id = n.incident_id
            WHERE n.target = %s AND n.consumed = FALSE AND n.token IS NOT NULL
              AND n.status = 'sent'
              AND i.status NOT IN (%s, %s)
        """
        params: list[Any] = [target, STATUS_RESOLVED, STATUS_EXHAUSTED]
        if channels:
            query += " AND n.channel = ANY(%s)"
            params.append(list(channels))
        query += " ORDER BY n.id DESC LIMIT 1"
        return self._run(query, params, fetch="one")

    def record_ack(
        self,
        *,
        incident_id: int,
        channel: str,
        notification_id: int | None = None,
        token: str = "",
        now: float | None = None,
    ) -> None:
        self._run(
            """
            INSERT INTO acks (incident_id, notification_id, channel, token, ts)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (incident_id, notification_id, channel, token, now if now is not None else time.time()),
        )

    def notifications_for_incident(self, incident_id: int) -> list[dict[str, Any]]:
        return list(
            self._run(
                "SELECT * FROM notifications WHERE incident_id = %s ORDER BY id",
                (incident_id,),
                fetch="all",
            )
            or []
        )

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
        self._run(
            """
            INSERT INTO slot_state (slot, plate_display, object_id, compliant, updated_ts)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (slot) DO UPDATE SET
              plate_display = EXCLUDED.plate_display,
              object_id = EXCLUDED.object_id,
              compliant = EXCLUDED.compliant,
              updated_ts = EXCLUDED.updated_ts
            """,
            (slot, plate, object_id, bool(compliant), now if now is not None else time.time()),
        )

    def slot_states(self) -> list[dict[str, Any]]:
        return list(self._run("SELECT * FROM slot_state ORDER BY slot", fetch="all") or [])

    # ----------------------------------------------------------------------- meta

    def set_meta(self, key: str, value: str) -> None:
        self._run(
            """
            INSERT INTO meta (key, value) VALUES (%s, %s)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
            """,
            (key, value),
        )

    def get_meta(self, key: str, default: str = "") -> str:
        row = self._run("SELECT value FROM meta WHERE key = %s", (key,), fetch="one")
        return default if row is None else str(row["value"])
