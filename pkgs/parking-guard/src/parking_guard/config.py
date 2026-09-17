"""Runtime configuration from environment and JSON file."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from parking_guard.geometry import parse_polygon
from parking_guard.slots import MODE_HYBRID, Area, Layout, Slot, SlotRules

DEFAULT_CLIENT_LADDER = (
    ("whatsapp", 180.0),
    ("telegram", 120.0),
    ("email", 300.0),
    ("sms", 300.0),
)
DEFAULT_OWNER_CHANNELS = ("ntfy", "pushover", "telegram")


@dataclass(frozen=True)
class ContactsConfig:
    overrides_path: Path
    retention_days: float = 7.0
    default_country_code: str = "+44"


@dataclass(frozen=True)
class EnforcementConfig:
    enabled: bool = True
    when_bookings_at_least: int = 2
    always_enforce_forbidden: bool = True
    repeat_seconds: float = 1800.0
    resolve_grace_seconds: float = 60.0


@dataclass(frozen=True)
class LadderStep:
    channel: str
    timeout_seconds: float


@dataclass(frozen=True)
class NotifyConfig:
    client_ladder: tuple[LadderStep, ...] = ()
    owner_channels: tuple[str, ...] = DEFAULT_OWNER_CHANNELS
    ack_grace_seconds: float = 600.0
    escalate_after_seconds: float = 900.0
    max_rounds: int = 2
    site_name: str = "the driveway"


@dataclass(frozen=True)
class AckConfig:
    base_url: str = "https://ntfy.sh"
    topic: str = ""
    poll_interval_seconds: float = 25.0
    telegram_polling: bool = True

    @property
    def enabled(self) -> bool:
        return bool(self.topic)

    def link(self, token: str) -> str:
        if not self.enabled:
            return ""
        return f"{self.base_url.rstrip('/')}/{self.topic}/trigger?message={token}"

    @property
    def stream_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/{self.topic}/json"


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
    mqtt_topic_events: str
    mqtt_topic_prefix: str
    cameras: tuple[str, ...]
    min_score: float
    grace_seconds: float
    cooldown_seconds: float
    resident_plates: tuple[str, ...]
    nvr_domain: str
    frame_width: int = 1920
    frame_height: int = 1080
    object_labels: tuple[str, ...] = ("car",)
    layout: Layout = field(default_factory=Layout)
    rules: SlotRules = field(default_factory=SlotRules)
    enforcement: EnforcementConfig = field(default_factory=EnforcementConfig)
    notify: NotifyConfig = field(default_factory=NotifyConfig)
    ack: AckConfig = field(default_factory=AckConfig)
    contacts: ContactsConfig = field(
        default_factory=lambda: ContactsConfig(overrides_path=Path("/var/lib/parking-guard/contacts.yaml"))
    )

    @property
    def misparking_enabled(self) -> bool:
        return self.enforcement.enabled and bool(self.layout.slots or self.layout.areas)


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
        mqtt_topic_events=raw.get("mqttTopicEvents", "frigate/events"),
        mqtt_topic_prefix=raw.get("mqttTopicPrefix", "parking-guard"),
        cameras=tuple(raw.get("cameras", ["c1"])),
        min_score=float(raw.get("minScore", 0.8)),
        grace_seconds=float(raw.get("graceMinutes", 5)) * 60,
        cooldown_seconds=float(raw.get("cooldownMinutes", 30)) * 60,
        resident_plates=tuple(raw.get("residentPlates", [])),
        nvr_domain=str(raw.get("nvrDomain", "")),
        frame_width=int(raw.get("frameWidth", 1920)),
        frame_height=int(raw.get("frameHeight", 1080)),
        object_labels=tuple(raw.get("objectLabels", ["car"])),
        layout=_load_layout(raw),
        rules=_load_rules(raw),
        enforcement=_load_enforcement(raw.get("enforcement", {})),
        notify=_load_notify(raw.get("notify", {})),
        ack=_load_ack(raw.get("ack", {})),
        contacts=_load_contacts(raw.get("contacts", {}), state_dir),
    )


def _load_layout(raw: dict[str, Any]) -> Layout:
    slots = []
    for entry in raw.get("slots", []):
        forward_line = entry.get("forwardLine")
        slots.append(
            Slot(
                name=str(entry.get("name", "")),
                label=str(entry.get("label", "") or ""),
                row=int(entry.get("row", 0)),
                zone=str(entry.get("zone", "") or ""),
                forward_zone=str(entry.get("forwardZone", "") or ""),
                polygon=parse_polygon(entry.get("polygon")),
                forward_line=float(forward_line) if forward_line is not None else None,
                pair=str(entry.get("pair", "") or ""),
            )
        )

    areas = []
    for entry in raw.get("areas", []):
        areas.append(
            Area(
                name=str(entry.get("name", "")),
                label=str(entry.get("label", "") or ""),
                kind=str(entry.get("kind", "forbidden")),
                zone=str(entry.get("zone", "") or ""),
                polygon=parse_polygon(entry.get("polygon")),
            )
        )

    return Layout(slots=tuple(s for s in slots if s.name), areas=tuple(a for a in areas if a.name))


def _load_rules(raw: dict[str, Any]) -> SlotRules:
    return SlotRules(
        mode=str(raw.get("evaluationMode", MODE_HYBRID)),
        min_slot_coverage=float(raw.get("minSlotCoverage", 0.4)),
        straddle_coverage=float(raw.get("straddleCoverage", 0.25)),
        forward_tolerance=float(raw.get("forwardTolerance", 0.08)),
        settle_frames=int(raw.get("settleFrames", 30)),
        forward_axis=str(raw.get("forwardAxis", "down")),
    )


def _load_enforcement(raw: dict[str, Any]) -> EnforcementConfig:
    return EnforcementConfig(
        enabled=bool(raw.get("enabled", True)),
        when_bookings_at_least=int(raw.get("whenBookingsAtLeast", 2)),
        always_enforce_forbidden=bool(raw.get("alwaysEnforceForbidden", True)),
        repeat_seconds=float(raw.get("repeatMinutes", 30)) * 60,
        resolve_grace_seconds=float(raw.get("resolveGraceSeconds", 60)),
    )


def _load_notify(raw: dict[str, Any]) -> NotifyConfig:
    ladder_raw = raw.get("clientLadder")
    steps: list[LadderStep] = []
    if ladder_raw is None:
        steps = [LadderStep(channel=name, timeout_seconds=timeout) for name, timeout in DEFAULT_CLIENT_LADDER]
    else:
        default_timeout = float(raw.get("stepTimeoutSeconds", 180))
        for entry in ladder_raw:
            if isinstance(entry, str):
                steps.append(LadderStep(channel=entry, timeout_seconds=default_timeout))
            elif isinstance(entry, dict) and entry.get("channel"):
                steps.append(
                    LadderStep(
                        channel=str(entry["channel"]),
                        timeout_seconds=float(entry.get("timeoutSeconds", default_timeout)),
                    )
                )

    return NotifyConfig(
        client_ladder=tuple(steps),
        owner_channels=tuple(raw.get("ownerChannels", DEFAULT_OWNER_CHANNELS)),
        ack_grace_seconds=float(raw.get("ackGraceMinutes", 10)) * 60,
        escalate_after_seconds=float(raw.get("escalateAfterMinutes", 15)) * 60,
        max_rounds=int(raw.get("maxRounds", 2)),
        site_name=str(raw.get("siteName", "the driveway")),
    )


def _load_ack(raw: dict[str, Any]) -> AckConfig:
    return AckConfig(
        base_url=str(raw.get("baseUrl", "https://ntfy.sh")),
        topic=str(raw.get("topic", "") or ""),
        poll_interval_seconds=float(raw.get("pollIntervalSeconds", 25)),
        telegram_polling=bool(raw.get("telegramPolling", True)),
    )


def _load_contacts(raw: dict[str, Any], state_dir: Path) -> ContactsConfig:
    return ContactsConfig(
        overrides_path=Path(raw.get("overridesPath", state_dir / "contacts.yaml")),
        retention_days=float(raw.get("retentionDays", 7)),
        default_country_code=str(raw.get("defaultCountryCode", "+44")),
    )


def read_secret_file(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8").strip()


def load_notify_config() -> dict[str, str]:
    """Channel credentials, read from the agenix env file (or PARKING_GUARD_NOTIFY_ENV)."""
    values: dict[str, str] = {}
    paths = [
        Path(os.environ.get("PARKING_GUARD_NOTIFY_ENV", "/run/agenix/parking-guard-notify-env")),
        Path(os.environ.get("PARKING_GUARD_MESSAGING_ENV", "/run/agenix/parking-guard-messaging-env")),
    ]
    for path in paths:
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values
