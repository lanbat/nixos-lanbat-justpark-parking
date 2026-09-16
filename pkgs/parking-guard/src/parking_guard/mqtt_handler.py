"""MQTT subscriber for Frigate LPR and parking-guard publishers."""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable

import paho.mqtt.client as mqtt

log = logging.getLogger(__name__)


class MqttClient:
    def __init__(
        self,
        *,
        host: str,
        port: int,
        username: str,
        password: str,
        topic_lpr: str,
        topic_prefix: str,
        on_lpr: Callable[[dict[str, Any]], None],
    ) -> None:
        self.topic_lpr = topic_lpr
        self.topic_prefix = topic_prefix.rstrip("/")
        self.on_lpr = on_lpr
        self._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        self._client.username_pw_set(username, password)
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message
        self._client.connect(host, port, keepalive=60)

    def loop_forever(self) -> None:
        self._client.loop_forever()

    def publish_json(self, suffix: str, payload: dict[str, Any]) -> None:
        topic = f"{self.topic_prefix}/{suffix}"
        self._client.publish(topic, json.dumps(payload), qos=1, retain=False)

    def publish_allowlist_count(self, count: int) -> None:
        self._client.publish(f"{self.topic_prefix}/allowlist/count", str(count), qos=1, retain=True)

    def _on_connect(self, client: mqtt.Client, userdata: Any, flags: Any, reason_code: Any, properties: Any = None) -> None:
        if reason_code != 0:
            log.error("mqtt connect failed: %s", reason_code)
            return
        client.subscribe(self.topic_lpr)
        log.info("subscribed to %s", self.topic_lpr)

    def _on_message(self, client: mqtt.Client, userdata: Any, msg: mqtt.MQTTMessage) -> None:
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return
        if payload.get("type") != "lpr":
            return
        self.on_lpr(payload)


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
