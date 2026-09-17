import json

from parking_guard.config import load_guard_config, load_notify_config
from parking_guard.slots import MODE_HYBRID, MODE_ZONES

CONFIG = {
    "stateDir": "/var/lib/parking-guard",
    "cameras": ["driveway"],
    "frameWidth": 2560,
    "frameHeight": 1440,
    "evaluationMode": "zones",
    "settleFrames": 20,
    "slots": [
        {
            "name": "front_left",
            "label": "Front left",
            "row": 0,
            "zone": "bay_front_left",
            "forwardZone": "bay_front_left_forward",
            "polygon": "0.2,0.6,0.55,0.6,0.55,1.0,0.2,1.0",
            "forwardLine": 0.95,
        },
        {"name": "rear_left", "row": 1, "pair": "front_left", "polygon": [[0.2, 0.2], [0.55, 0.6]]},
    ],
    "areas": [
        {"name": "neighbour", "label": "next door", "zone": "neighbour_drive"},
        {"name": "aisle", "kind": "aisle", "polygon": "0,0.2,0.2,0.2,0.2,1,0,1"},
    ],
    "enforcement": {"whenBookingsAtLeast": 3, "repeatMinutes": 45},
    "notify": {
        "clientLadder": ["whatsapp", {"channel": "telegram", "timeoutSeconds": 90}, "email", "sms"],
        "ownerChannels": ["ntfy"],
        "ackGraceMinutes": 5,
        "siteName": "21 Example Road",
    },
    "ack": {"topic": "pg-ack-secret"},
    "contacts": {"retentionDays": 3, "defaultCountryCode": "+44"},
}


def _write(tmp_path, payload):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_defaults_apply_to_a_minimal_config(tmp_path):
    cfg = load_guard_config(_write(tmp_path, {}))
    assert cfg.mqtt_topic_events == "frigate/events"
    assert cfg.rules.mode == MODE_HYBRID
    assert [step.channel for step in cfg.notify.client_ladder] == ["whatsapp", "telegram", "email", "sms"]
    assert cfg.notify.owner_channels == ("ntfy", "pushover", "telegram")
    assert not cfg.misparking_enabled  # no bays configured yet


def test_full_config_builds_layout_rules_and_ladder(tmp_path):
    cfg = load_guard_config(_write(tmp_path, CONFIG))

    assert cfg.frame_width == 2560
    assert cfg.rules.mode == MODE_ZONES
    assert cfg.rules.settle_frames == 20
    assert cfg.misparking_enabled

    front = cfg.layout.slot("front_left")
    assert front.zone == "bay_front_left"
    assert front.forward_zone == "bay_front_left_forward"
    assert front.polygon[0] == (0.2, 0.6)
    assert front.forward_line == 0.95
    assert cfg.layout.slot("rear_left").pair == "front_left"

    assert [area.kind for area in cfg.layout.areas] == ["forbidden", "aisle"]

    assert cfg.enforcement.when_bookings_at_least == 3
    assert cfg.enforcement.repeat_seconds == 2700

    assert [(s.channel, s.timeout_seconds) for s in cfg.notify.client_ladder] == [
        ("whatsapp", 180.0),
        ("telegram", 90.0),
        ("email", 180.0),
        ("sms", 180.0),
    ]
    assert cfg.notify.ack_grace_seconds == 300
    assert cfg.notify.site_name == "21 Example Road"
    assert cfg.contacts.retention_days == 3


def test_ack_links_and_stream_url(tmp_path):
    cfg = load_guard_config(_write(tmp_path, CONFIG))
    assert cfg.ack.enabled
    assert cfg.ack.link("tok123") == "https://ntfy.sh/pg-ack-secret/trigger?message=tok123"
    assert cfg.ack.stream_url == "https://ntfy.sh/pg-ack-secret/json"


def test_ack_is_disabled_without_a_topic(tmp_path):
    cfg = load_guard_config(_write(tmp_path, {}))
    assert not cfg.ack.enabled
    assert cfg.ack.link("tok123") == ""


def test_notify_env_merges_both_secret_files(tmp_path, monkeypatch):
    notify_env = tmp_path / "notify"
    notify_env.write_text("# comment\nPUSHOVER_TOKEN=abc\nTELEGRAM_BOT_TOKEN=bot\n", encoding="utf-8")
    messaging_env = tmp_path / "messaging"
    messaging_env.write_text('WHATSAPP_TOKEN="quoted"\nSMS_API_URL=https://x\n', encoding="utf-8")

    monkeypatch.setenv("PARKING_GUARD_NOTIFY_ENV", str(notify_env))
    monkeypatch.setenv("PARKING_GUARD_MESSAGING_ENV", str(messaging_env))

    values = load_notify_config()
    assert values["PUSHOVER_TOKEN"] == "abc"
    assert values["WHATSAPP_TOKEN"] == "quoted"
    assert values["SMS_API_URL"] == "https://x"


def test_notify_env_is_empty_when_no_files_exist(tmp_path, monkeypatch):
    monkeypatch.setenv("PARKING_GUARD_NOTIFY_ENV", str(tmp_path / "missing"))
    monkeypatch.setenv("PARKING_GUARD_MESSAGING_ENV", str(tmp_path / "missing2"))
    assert load_notify_config() == {}
