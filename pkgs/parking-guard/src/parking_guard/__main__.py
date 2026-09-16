"""Main parking-guard evaluator — Frigate LPR MQTT → allowlist check → alert."""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path
from typing import Any

from parking_guard.config import load_guard_config, load_notify_config, read_secret_file
from parking_guard.mqtt_handler import MqttClient, build_event_payload
from parking_guard.notify import format_alert_message, send_alert
from parking_guard.store import Store

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)


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
    notify = load_notify_config()

    password = read_secret_file(cfg.mqtt_password_file)
    if not password:
        log.error("mqtt password file missing: %s", cfg.mqtt_password_file)
        return 1

    mqtt_client: MqttClient | None = None

    def handle_lpr(payload: dict[str, Any]) -> None:
        nonlocal mqtt_client
        camera = str(payload.get("camera", ""))
        if cfg.cameras and camera not in cfg.cameras:
            return

        score = payload.get("score")
        if score is not None and float(score) < cfg.min_score:
            return

        plate = str(payload.get("plate", "")).strip()
        if not plate:
            return

        now = time.time()
        authorized, reason = store.is_authorized(plate, now, cfg.grace_seconds)
        event = build_event_payload(
            plate=plate,
            authorized=authorized,
            reason=reason,
            camera=camera or None,
            frigate_id=str(payload.get("id")) if payload.get("id") else None,
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

        if mqtt_client is None:
            return
        mqtt_client.publish_json("event", event)

        if authorized:
            return

        if not store.should_alert(plate, cfg.cooldown_seconds, now):
            log.info("cooldown active for %s", plate)
            return

        store.mark_alerted(plate, now)
        mqtt_client.publish_json("alert", event)
        message = format_alert_message(event, cfg.nvr_domain)
        send_alert("Unauthorized parking", message, notify)
        log.warning("unauthorized plate %s on %s (%s)", plate, camera, reason)

    mqtt_client = MqttClient(
        host=cfg.mqtt_host,
        port=cfg.mqtt_port,
        username=cfg.mqtt_user,
        password=password,
        topic_lpr=cfg.mqtt_topic_lpr,
        topic_prefix=cfg.mqtt_topic_prefix,
        on_lpr=handle_lpr,
    )

    count = store.active_booking_count()
    mqtt_client.publish_allowlist_count(count)
    log.info("started — allowlist has %s active bookings", count)

    try:
        mqtt_client.loop_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
