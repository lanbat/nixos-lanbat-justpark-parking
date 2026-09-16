"""Runtime configuration from environment and JSON file."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class GuardConfig:
    state_dir: Path
    db_path: Path
    csv_import_path: Path
    mqtt_host: str
    mqtt_port: int
    mqtt_user: str
    mqtt_password_file: Path
    mqtt_topic_lpr: str
    mqtt_topic_prefix: str
    cameras: tuple[str, ...]
    min_score: float
    grace_seconds: float
    cooldown_seconds: float
    resident_plates: tuple[str, ...]
    nvr_domain: str


def load_guard_config(config_path: Path) -> GuardConfig:
    with config_path.open(encoding="utf-8") as handle:
        raw = json.load(handle)

    state_dir = Path(raw.get("stateDir", "/var/lib/parking-guard"))
    return GuardConfig(
        state_dir=state_dir,
        db_path=state_dir / "parking-guard.db",
        csv_import_path=state_dir / "import" / "bookings.csv",
        mqtt_host=raw.get("mqttHost", "127.0.0.1"),
        mqtt_port=int(raw.get("mqttPort", 1883)),
        mqtt_user=raw.get("mqttUser", "parking-guard"),
        mqtt_password_file=Path(raw.get("mqttPasswordFile", "/run/agenix/parking-guard-mqtt-pass")),
        mqtt_topic_lpr=raw.get("mqttTopicLpr", "frigate/tracked_object_update"),
        mqtt_topic_prefix=raw.get("mqttTopicPrefix", "parking-guard"),
        cameras=tuple(raw.get("cameras", ["c1"])),
        min_score=float(raw.get("minScore", 0.8)),
        grace_seconds=float(raw.get("graceMinutes", 5)) * 60,
        cooldown_seconds=float(raw.get("cooldownMinutes", 30)) * 60,
        resident_plates=tuple(raw.get("residentPlates", [])),
        nvr_domain=str(raw.get("nvrDomain", "")),
    )


def read_secret_file(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8").strip()


def load_notify_config() -> dict[str, str]:
    path = Path(os.environ.get("PARKING_GUARD_NOTIFY_ENV", "/run/agenix/parking-guard-notify-env"))
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values
