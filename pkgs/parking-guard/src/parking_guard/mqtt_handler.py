"""MQTT subscriber for Frigate LPR / tracked objects and parking-guard publishers."""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable

import paho.mqtt.client as mqtt

from parking_guard.geometry import normalize_box
from parking_guard.slots import ParkedCar

log = logging.getLogger(__name__)

DEFAULT_OBJECT_LABELS = ("car",)


class MqttClient:
    def __init__(
        self,
        *,
        host: str,
        port: int,
        username: str,
        password: str,
        topic_lpr: str,
        topic_events: str = "",
        topic_prefix: str,
        on_lpr: Callable[[dict[str, Any]], None],
        on_event: Callable[[str, dict[str, Any]], None] | None = None,
        on_command: Callable[[str, str], None] | None = None,
    ) -> None:
        self.topic_lpr = topic_lpr
        self.topic_events = topic_events
        self.topic_prefix = topic_prefix.rstrip("/")
        self.on_lpr = on_lpr
        self.on_event = on_event
        self.on_command = on_command
        self._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        self._client.username_pw_set(username, password)
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message
        self._client.connect(host, port, keepalive=60)

    @property
    def command_topic(self) -> str:
        return f"{self.topic_prefix}/command/#"

    def loop_forever(self) -> None:
        self._client.loop_forever()

    def publish_json(self, suffix: str, payload: dict[str, Any], retain: bool = False) -> None:
        topic = f"{self.topic_prefix}/{suffix}"
        self._client.publish(topic, json.dumps(payload), qos=1, retain=retain)

    def publish_allowlist_count(self, count: int) -> None:
        self._client.publish(f"{self.topic_prefix}/allowlist/count", str(count), qos=1, retain=True)

    def publish_state(self, suffix: str, value: str) -> None:
        self._client.publish(f"{self.topic_prefix}/{suffix}", value, qos=1, retain=True)

    def _on_connect(self, client: mqtt.Client, userdata: Any, flags: Any, reason_code: Any, properties: Any = None) -> None:
        if reason_code != 0:
            log.error("mqtt connect failed: %s", reason_code)
            return
        client.subscribe(self.topic_lpr)
        log.info("subscribed to %s", self.topic_lpr)
        if self.topic_events and self.on_event is not None:
            client.subscribe(self.topic_events)
            log.info("subscribed to %s", self.topic_events)
        if self.on_command is not None:
            client.subscribe(self.command_topic)
            log.info("subscribed to %s", self.command_topic)

    def _on_message(self, client: mqtt.Client, userdata: Any, msg: mqtt.MQTTMessage) -> None:
        try:
            raw = msg.payload.decode("utf-8")
        except UnicodeDecodeError:
            return

        if self.on_command is not None and msg.topic.startswith(f"{self.topic_prefix}/command/"):
            self.on_command(msg.topic.rsplit("/", 1)[-1], raw.strip())
            return

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return

        if msg.topic == self.topic_events:
            if self.on_event is not None:
                self.on_event(str(payload.get("type", "update")), payload)
            return

        if payload.get("type") != "lpr":
            return
        self.on_lpr(payload)


def parse_tracked_object(
    payload: dict[str, Any],
    *,
    frame_width: int,
    frame_height: int,
    object_labels: tuple[str, ...] = DEFAULT_OBJECT_LABELS,
) -> ParkedCar | None:
    """Turn the ``after`` half of a ``frigate/events`` message into a ParkedCar."""
    after = payload.get("after") or payload.get("before") or {}
    if not isinstance(after, dict):
        return None

    label = str(after.get("label", ""))
    if object_labels and label not in object_labels:
        return None

    object_id = str(after.get("id", ""))
    if not object_id:
        return None

    box = after.get("box")
    try:
        rect = normalize_box(box, frame_width, frame_height) if box else (0.0, 0.0, 0.0, 0.0)
    except (TypeError, ValueError):
        rect = (0.0, 0.0, 0.0, 0.0)

    plate = str(after.get("recognized_license_plate") or "")
    plate_score = after.get("recognized_license_plate_score")

    return ParkedCar(
        object_id=object_id,
        plate=plate,
        camera=str(after.get("camera", "")),
        rect=rect,
        zones=tuple(str(zone) for zone in after.get("current_zones") or ()),
        stationary=bool(after.get("stationary", False)),
        motionless_count=int(after.get("motionless_count") or 0),
        score=float(plate_score) if plate_score is not None else None,
    )


def build_event_payload(
    *,
    plate: str,
    authorized: bool,
    reason: str,
    camera: str | None,
    frigate_id: str | None,
    score: float | None,
) -> dict[str, Any]:
    return {
        "plate": plate,
        "authorized": authorized,
        "reason": reason,
        "camera": camera,
        "frigate_id": frigate_id,
        "score": score,
        "ts": time.time(),
    }
