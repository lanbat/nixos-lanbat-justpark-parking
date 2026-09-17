"""Walks an incident down the notification ladder until somebody responds.

Rungs are tried in configured order — WhatsApp first, SMS last, because SMS is the
only rung that costs money per message. A rung with no endpoint for this client
(no linked Telegram chat, no email address) is skipped immediately rather than
burning its timeout, and a rung that errors falls through to the next one at once.
When the ladder runs out, the owner hears about it instead.
"""

from __future__ import annotations

import logging
import secrets as secrets_module
import threading
import time
from typing import Any, Callable

from parking_guard.config import GuardConfig
from parking_guard.contacts import Contact, mask_endpoint, resolve
from parking_guard.messaging import AUDIENCE_CLIENT, AUDIENCE_OWNER, Channel, ChannelError
from parking_guard.messaging.telegram import TelegramChannel
from parking_guard.notify import build_client_message, build_owner_message
from parking_guard.store import (
    STATUS_ACKED,
    STATUS_ESCALATED,
    STATUS_EXHAUSTED,
    STATUS_NOTIFIED,
    STATUS_OPEN,
    STATUS_RESOLVED,
    Store,
)

log = logging.getLogger(__name__)

TOKEN_BYTES = 12
TICK_SECONDS = 15.0

Publisher = Callable[[str, dict[str, Any]], None]
OverridesProvider = Callable[[], dict[str, Contact]]


class Dispatcher:
    def __init__(
        self,
        *,
        store: Store,
        channels: dict[str, Channel],
        config: GuardConfig,
        owner: Contact,
        overrides: OverridesProvider | None = None,
        publish: Publisher | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.store = store
        self.channels = channels
        self.config = config
        self.owner = owner
        self.overrides = overrides or dict
        self.publish = publish or (lambda suffix, payload: None)
        self.clock = clock
        self._stop = threading.Event()
        # MQTT callbacks raise incidents on paho's network thread while the dispatcher
        # thread works the same queue, so sends are serialised.
        self._send_lock = threading.RLock()

    # ------------------------------------------------------------------ lifecycle

    def run_forever(self, interval: float = TICK_SECONDS) -> None:
        while not self._stop.wait(interval):
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - a bad tick must not kill the thread
                log.exception("dispatcher tick failed")

    def stop(self) -> None:
        self._stop.set()

    def tick(self, now: float | None = None) -> None:
        ts = now if now is not None else self.clock()
        for incident in self.store.incidents_due(ts):
            status = str(incident.get("status", ""))
            if status == STATUS_ACKED:
                self.escalate(incident, "acknowledged, but the car has not moved", now=ts)
            elif status in (STATUS_OPEN, STATUS_NOTIFIED):
                self.notify(int(incident["id"]), now=ts)
            elif status == STATUS_ESCALATED:
                self.store.update_incident(int(incident["id"]), status=STATUS_EXHAUSTED, next_action_ts=None, now=ts)

    # --------------------------------------------------------------------- notify

    def notify(self, incident_id: int, now: float | None = None) -> bool:
        """Send on the next usable rung. Returns True if a message went out."""
        with self._send_lock:
            return self._notify_locked(incident_id, now)

    def _notify_locked(self, incident_id: int, now: float | None = None) -> bool:
        ts = now if now is not None else self.clock()
        incident = self.store.get_incident(incident_id)
        if incident is None or incident["status"] in (STATUS_RESOLVED, STATUS_EXHAUSTED):
            return False

        # Another thread may have sent while this call waited for the lock; the rung
        # it chose owns the incident until its timeout expires.
        next_action = incident.get("next_action_ts")
        if incident["status"] == STATUS_NOTIFIED and next_action is not None and float(next_action) > ts:
            return False

        contact = resolve(
            self.store,
            str(incident["plate_display"]),
            str(incident.get("booking_ref", "") or ""),
            self.overrides(),
        )
        if contact is None or not contact.reachable:
            self.escalate(incident, "no contact details on file for this booking", now=ts)
            return False

        ladder = self.config.notify.client_ladder
        index = int(incident.get("ladder_index", 0) or 0)

        while index < len(ladder):
            step = ladder[index]
            channel = self.channels.get(step.channel)
            index += 1

            if channel is None or not channel.serves(AUDIENCE_CLIENT) or not channel.available_for(contact):
                continue

            token = secrets_module.token_urlsafe(TOKEN_BYTES)
            message = build_client_message(
                incident,
                site_name=self.config.notify.site_name,
                ack_url=self.config.ack.link(token),
                ack_token=token,
                telegram_link=self._telegram_invite(contact, token),
                greeting=contact.greeting,
            )

            # Record the token before sending: a client who taps the link the instant
            # it arrives must not find the token missing.
            notification_id = self.store.record_notification(
                incident_id=incident_id,
                channel=step.channel,
                audience=AUDIENCE_CLIENT,
                target=mask_endpoint(contact.endpoint(step.channel)),
                token=token,
                status="pending",
                now=ts,
            )

            try:
                delivery_id = channel.send(contact, message)
            except ChannelError as exc:
                log.warning("incident %s: %s send failed: %s", incident_id, step.channel, exc)
                # Retire the token with the failed attempt so it cannot acknowledge a
                # message the client never received.
                self.store.update_notification(
                    notification_id,
                    status="failed",
                    error=str(exc),
                    token=None,
                    consumed=1,
                )
                continue

            self.store.update_notification(notification_id, status="sent", delivery_id=delivery_id)
            self.store.update_incident(
                incident_id,
                status=STATUS_NOTIFIED,
                ladder_index=index,
                next_action_ts=ts + step.timeout_seconds,
                now=ts,
            )
            self.publish(
                "notification",
                {
                    "incident_id": incident_id,
                    "channel": step.channel,
                    "kind": incident["kind"],
                    "slot": incident.get("slot", ""),
                    "ts": ts,
                },
            )
            log.info("incident %s: notified client over %s", incident_id, step.channel)
            return True

        self.store.update_incident(incident_id, ladder_index=index, now=ts)
        self.escalate(incident, "the client did not respond on any channel", now=ts)
        return False

    def _telegram_invite(self, contact: Contact, token: str) -> str:
        """Offer a free channel for next time when the client has not linked one yet."""
        if contact.endpoint("telegram"):
            return ""
        channel = self.channels.get("telegram")
        if isinstance(channel, TelegramChannel):
            return channel.deep_link(token)
        return ""

    # -------------------------------------------------------------------- escalate

    def escalate(self, incident: dict[str, Any], reason: str, now: float | None = None) -> None:
        ts = now if now is not None else self.clock()
        incident_id = int(incident["id"])
        message = build_owner_message(incident, nvr_domain=self.config.nvr_domain, reason=reason)

        delivered = False
        for name in self.config.notify.owner_channels:
            channel = self.channels.get(name)
            if channel is None or not channel.serves(AUDIENCE_OWNER) or not channel.available_for(self.owner):
                continue
            try:
                delivery_id = channel.send(self.owner, message)
            except ChannelError as exc:
                log.warning("incident %s: owner channel %s failed: %s", incident_id, name, exc)
                self.store.record_notification(
                    incident_id=incident_id,
                    channel=name,
                    audience=AUDIENCE_OWNER,
                    status="failed",
                    error=str(exc),
                    now=ts,
                )
                continue
            delivered = True
            self.store.record_notification(
                incident_id=incident_id,
                channel=name,
                audience=AUDIENCE_OWNER,
                status="sent",
                delivery_id=delivery_id,
                now=ts,
            )

        rounds = int(incident.get("escalated", 0) or 0) + 1
        if rounds < self.config.notify.max_rounds:
            # Give the client another lap of the ladder before giving up on them.
            self.store.update_incident(
                incident_id,
                status=STATUS_OPEN,
                escalated=rounds,
                ladder_index=0,
                next_action_ts=ts + self.config.notify.escalate_after_seconds,
                now=ts,
            )
        else:
            self.store.update_incident(
                incident_id,
                status=STATUS_ESCALATED,
                escalated=rounds,
                next_action_ts=ts + self.config.notify.escalate_after_seconds,
                now=ts,
            )

        self.publish(
            "escalation",
            {
                "incident_id": incident_id,
                "plate": incident.get("plate_display", ""),
                "kind": incident.get("kind", ""),
                "reason": reason,
                "delivered": delivered,
                "ts": ts,
            },
        )
        log.warning("incident %s escalated to owner: %s", incident_id, reason)

    # ------------------------------------------------------------------------ ack

    def handle_ack(self, token: str, channel: str, now: float | None = None) -> int | None:
        """Claim an ack token. Unknown or already-used tokens are ignored."""
        ts = now if now is not None else self.clock()
        notification = self.store.consume_token(token)
        if notification is None:
            return None

        incident_id = int(notification["incident_id"])
        incident = self.store.get_incident(incident_id)
        if incident is None or incident["status"] == STATUS_RESOLVED:
            return incident_id

        self.store.record_ack(
            incident_id=incident_id,
            channel=channel,
            notification_id=int(notification["id"]),
            token=token,
            now=ts,
        )

        # A reply that arrives after the owner has already been pulled in is worth
        # logging, but it must not restart the ladder we deliberately finished.
        if incident["status"] in (STATUS_OPEN, STATUS_NOTIFIED):
            self.store.update_incident(
                incident_id,
                status=STATUS_ACKED,
                acked_ts=ts,
                next_action_ts=ts + self.config.notify.ack_grace_seconds,
                now=ts,
            )
        else:
            self.store.update_incident(incident_id, acked_ts=ts, now=ts)

        self.publish(
            "incident/ack",
            {"incident_id": incident_id, "channel": channel, "plate": incident.get("plate_display", ""), "ts": ts},
        )
        log.info("incident %s acknowledged over %s", incident_id, channel)
        return incident_id

    def link_telegram_chat(self, token: str, chat_id: str, now: float | None = None) -> str:
        """Remember a chat id after a /start deep link so Telegram works next time."""
        notification = self.store.notification_for_token(token)
        if notification is None:
            return ""
        incident = self.store.get_incident(int(notification["incident_id"]))
        if incident is None:
            return ""
        plate = str(incident["plate_display"])
        self.store.set_telegram_chat_id(plate, chat_id, now=now if now is not None else self.clock())
        log.info("linked telegram chat for %s", plate)
        return plate

    # ------------------------------------------------------------------- resolved

    def resolve_object(self, object_id: str, now: float | None = None) -> list[int]:
        ts = now if now is not None else self.clock()
        resolved = self.store.resolve_incidents_for_object(object_id, now=ts)
        for incident_id in resolved:
            self.publish("incident/resolved", {"incident_id": incident_id, "ts": ts})
            log.info("incident %s resolved — the car is parked correctly now", incident_id)
        return resolved
