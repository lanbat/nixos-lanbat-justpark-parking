"""Main parking-guard evaluator.

Two Frigate topics feed one picture of the driveway: ``frigate/tracked_object_update``
answers "is this plate allowed", and ``frigate/events`` answers "did they park where
they should". Unauthorized plates alert the owner; badly parked bookings start an
incident that walks down the client notification ladder until it is acknowledged or
the car moves.
"""

from __future__ import annotations

import argparse
import logging
import threading
import time
from pathlib import Path
from typing import Any

from parking_guard.ack import start_listeners
from parking_guard.config import load_guard_config, load_notify_config, read_secret_file
from parking_guard.contacts import Contact, load_overrides
from parking_guard.dispatcher import Dispatcher
from parking_guard.messaging import AUDIENCE_OWNER, ChannelError, build_channels, owner_contact
from parking_guard.messaging.base import Message
from parking_guard.monitor import MisparkingMonitor
from parking_guard.mqtt_handler import MqttClient, build_event_payload
from parking_guard.notify import format_alert_message
from parking_guard.store import Store

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

OVERRIDES_RELOAD_SECONDS = 60.0


class OverridesCache:
    """Re-reads contacts.yaml when it changes, so edits land without a restart."""

    def __init__(self, path: Path, ttl: float = OVERRIDES_RELOAD_SECONDS) -> None:
        self.path = path
        self.ttl = ttl
        self._checked = 0.0
        self._mtime = 0.0
        self._value: dict[str, Contact] = {}

    def __call__(self) -> dict[str, Contact]:
        now = time.time()
        if now - self._checked < self.ttl:
            return self._value
        self._checked = now
        mtime = self.path.stat().st_mtime if self.path.exists() else 0.0
        if mtime != self._mtime:
            self._mtime = mtime
            self._value = load_overrides(self.path)
            log.info("loaded %s contact overrides from %s", len(self._value), self.path)
        return self._value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Parking guard LPR evaluator")
    parser.add_argument(
        "--config",
        default="/etc/parking-guard/config.json",
        type=Path,
    )
    args = parser.parse_args(argv)

    cfg = load_guard_config(args.config)
    store = Store(cfg.db_path)
    secrets = load_notify_config()
    channels = build_channels(secrets)
    owner = owner_contact(secrets)

    password = read_secret_file(cfg.mqtt_password_file)
    if not password:
        log.error("mqtt password file missing: %s", cfg.mqtt_password_file)
        return 1

    mqtt_client: MqttClient | None = None

    def publish(suffix: str, payload: dict[str, Any], retain: bool = False) -> None:
        if mqtt_client is not None:
            mqtt_client.publish_json(suffix, payload, retain=retain)

    dispatcher = Dispatcher(
        store=store,
        channels=channels,
        config=cfg,
        owner=owner,
        overrides=OverridesCache(cfg.contacts.overrides_path),
        publish=lambda suffix, payload: publish(suffix, payload),
    )
    monitor = MisparkingMonitor(store=store, config=cfg, dispatcher=dispatcher, publish=publish)

    def alert_owner(title: str, body: str) -> None:
        message = Message(title=title, body=body)
        for name in cfg.notify.owner_channels:
            channel = channels.get(name)
            if channel is None or not channel.serves(AUDIENCE_OWNER) or not channel.available_for(owner):
                continue
            try:
                channel.send(owner, message)
            except ChannelError as exc:
                log.warning("owner channel %s failed: %s", name, exc)

    def handle_lpr(payload: dict[str, Any]) -> None:
        camera = str(payload.get("camera", ""))
        if cfg.cameras and camera not in cfg.cameras:
            return

        score = payload.get("score")
        if score is not None and float(score) < cfg.min_score:
            return

        plate = str(payload.get("plate", "")).strip()
        if not plate:
            return

        frigate_id = str(payload.get("id")) if payload.get("id") else None
        if frigate_id:
            # Keep the misparking view in sync with whatever the LPR just read.
            monitor.note_plate(frigate_id, plate, float(score) if score is not None else None)

        now = time.time()
        authorized, reason = store.is_authorized(plate, now, cfg.grace_seconds)
        event = build_event_payload(
            plate=plate,
            authorized=authorized,
            reason=reason,
            camera=camera or None,
            frigate_id=frigate_id,
            score=float(score) if score is not None else None,
        )
        store.record_event(
            plate=plate,
            authorized=authorized,
            reason=reason,
            camera=camera or None,
            frigate_id=event.get("frigate_id"),
            score=event.get("score"),
            ts=now,
        )
        publish("event", event)

        if authorized:
            return

        if not store.should_alert(plate, cfg.cooldown_seconds, now):
            log.info("cooldown active for %s", plate)
            return

        store.mark_alerted(plate, now)
        publish("alert", event)
        alert_owner("Unauthorized parking", format_alert_message(event, cfg.nvr_domain))
        log.warning("unauthorized plate %s on %s (%s)", plate, camera, reason)

    def handle_command(command: str, value: str) -> None:
        if command == "enforce":
            monitor.gate.set_override(value)
            if mqtt_client is not None:
                mqtt_client.publish_state("enforce", monitor.gate.state)
        elif command == "resolve" and value:
            dispatcher.resolve_object(value)

    mqtt_client = MqttClient(
        host=cfg.mqtt_host,
        port=cfg.mqtt_port,
        username=cfg.mqtt_user,
        password=password,
        topic_lpr=cfg.mqtt_topic_lpr,
        topic_events=cfg.mqtt_topic_events if cfg.misparking_enabled else "",
        topic_prefix=cfg.mqtt_topic_prefix,
        on_lpr=handle_lpr,
        on_event=monitor.handle_event,
        on_command=handle_command,
    )

    threading.Thread(target=dispatcher.run_forever, name="dispatcher", daemon=True).start()
    listeners = start_listeners(dispatcher=dispatcher, channels=channels, ack_config=cfg.ack)

    purged = store.purge_expired_contacts()
    count = store.active_booking_count()
    mqtt_client.publish_allowlist_count(count)
    mqtt_client.publish_state("enforce", monitor.gate.state)
    log.info(
        "started — %s active bookings, %s bays, %s channels, %s ack listeners (%s stale contacts purged)",
        count,
        len(cfg.layout.slots),
        len(channels),
        len(listeners),
        purged,
    )

    try:
        mqtt_client.loop_forever()
    except KeyboardInterrupt:
        pass
    finally:
        dispatcher.stop()
        for listener in listeners:
            listener.stop()
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
