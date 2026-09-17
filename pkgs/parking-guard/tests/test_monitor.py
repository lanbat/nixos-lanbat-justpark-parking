import json

import pytest

from parking_guard.config import load_guard_config
from parking_guard.monitor import MisparkingMonitor
from parking_guard.store import STATUS_NOTIFIED, STATUS_RESOLVED, Booking, Store

from test_dispatcher import NOW, FakeChannel, _dispatcher, store  # noqa: F401

LAYOUT_CONFIG = {
    "cameras": ["driveway"],
    "frameWidth": 1000,
    "frameHeight": 1000,
    "evaluationMode": "geometry",
    "settleFrames": 10,
    "slots": [
        {
            "name": "front_left",
            "label": "Front left",
            "polygon": "0.2,0.6,0.55,0.6,0.55,1.0,0.2,1.0",
            "forwardLine": 0.95,
        },
        {
            "name": "front_right",
            "label": "Front right",
            "polygon": "0.55,0.6,0.9,0.6,0.9,1.0,0.55,1.0",
            "forwardLine": 0.95,
        },
    ],
    "areas": [{"name": "neighbour", "label": "next door", "polygon": "0.9,0.4,1.0,0.4,1.0,1.0,0.9,1.0"}],
    "enforcement": {"whenBookingsAtLeast": 2, "resolveGraceSeconds": 30},
    "notify": {"clientLadder": ["whatsapp", "sms"], "ownerChannels": ["ntfy"], "siteName": "the driveway"},
    "ack": {"topic": "acktopic"},
}


def _event(event_type, box, object_id="obj1", plate="AB12CDE", motionless=40):
    after = {
        "id": object_id,
        "camera": "driveway",
        "label": "car",
        "box": box,
        "current_zones": [],
        "stationary": True,
        "motionless_count": motionless,
        "recognized_license_plate": plate,
    }
    return {"type": event_type, "before": dict(after), "after": after}


@pytest.fixture
def config(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(LAYOUT_CONFIG), encoding="utf-8")
    return load_guard_config(path)


@pytest.fixture
def monitor(store, config):  # noqa: F811
    store.replace_allowlist(
        [
            Booking(
                plate="AB12 CDE",
                plate_normalized="AB12CDE",
                start_ts=NOW - 600,
                end_ts=NOW + 3600,
                booking_ref="b1",
                source="test",
            ),
            Booking(
                plate="XY34 ZZZ",
                plate_normalized="XY34ZZZ",
                start_ts=NOW - 600,
                end_ts=NOW + 3600,
                booking_ref="b2",
                source="test",
            ),
        ]
    )
    published = []
    dispatcher = _dispatcher(store, {"whatsapp": FakeChannel("whatsapp"), "ntfy": FakeChannel("ntfy")}, config=config)
    monitor = MisparkingMonitor(
        store=store,
        config=config,
        dispatcher=dispatcher,
        publish=lambda suffix, payload, retain=False: published.append((suffix, payload)),
        clock=lambda: NOW,
    )
    monitor.published = published
    return monitor


def test_a_car_parked_short_of_the_house_gets_messaged(monitor, store):  # noqa: F811
    monitor.handle_event("new", _event("new", [250, 550, 500, 850]), now=NOW)

    incidents = store.open_incidents()
    assert len(incidents) == 1
    assert incidents[0]["kind"] == "not_pulled_forward"
    assert incidents[0]["booking_ref"] == "b1"
    assert incidents[0]["status"] == STATUS_NOTIFIED

    assert len(monitor.dispatcher.channels["whatsapp"].sent) == 1
    assert any(suffix == "violation" for suffix, _ in monitor.published)


def test_a_tidy_car_is_never_messaged(monitor, store):  # noqa: F811
    monitor.handle_event("new", _event("new", [250, 650, 500, 970]), now=NOW)
    assert store.open_incidents() == []
    assert monitor.dispatcher.channels["whatsapp"].sent == []


def test_moving_car_is_ignored_until_it_settles(monitor, store):  # noqa: F811
    monitor.handle_event("new", _event("new", [250, 550, 500, 850], motionless=2), now=NOW)
    assert store.open_incidents() == []


def test_repeated_events_do_not_re_notify(monitor, store):  # noqa: F811
    monitor.handle_event("new", _event("new", [250, 550, 500, 850]), now=NOW)
    monitor.handle_event("update", _event("update", [250, 550, 500, 850]), now=NOW + 5)
    monitor.handle_event("update", _event("update", [250, 550, 500, 850]), now=NOW + 10)

    assert len(store.open_incidents()) == 1
    assert len(monitor.dispatcher.channels["whatsapp"].sent) == 1


def test_moving_the_car_forward_resolves_the_incident(monitor, store):  # noqa: F811
    monitor.handle_event("new", _event("new", [250, 550, 500, 850]), now=NOW)
    incident_id = store.open_incidents()[0]["id"]

    monitor.handle_event("update", _event("update", [250, 650, 500, 970]), now=NOW + 5)
    assert store.get_incident(incident_id)["status"] != STATUS_RESOLVED  # inside the grace

    monitor.handle_event("update", _event("update", [250, 650, 500, 970]), now=NOW + 40)
    assert store.get_incident(incident_id)["status"] == STATUS_RESOLVED


def test_driving_away_resolves_the_incident(monitor, store):  # noqa: F811
    monitor.handle_event("new", _event("new", [250, 550, 500, 850]), now=NOW)
    incident_id = store.open_incidents()[0]["id"]

    monitor.handle_event("end", _event("end", [250, 550, 500, 850]), now=NOW + 20)
    assert store.get_incident(incident_id)["status"] == STATUS_RESOLVED


def test_neighbours_drive_is_reported_even_with_one_booking(monitor, store):  # noqa: F811
    store.replace_allowlist([])
    monitor.handle_event("new", _event("new", [910, 500, 990, 900]), now=NOW)

    incidents = store.open_incidents()
    assert len(incidents) == 1
    assert incidents[0]["kind"] == "forbidden_area"
    assert incidents[0]["detail"] == "next door"


def test_other_cameras_and_other_objects_are_ignored(monitor, store):  # noqa: F811
    other_camera = _event("new", [250, 550, 500, 850])
    other_camera["after"]["camera"] = "back_garden"
    monitor.handle_event("new", other_camera, now=NOW)

    person = _event("new", [250, 550, 500, 850])
    person["after"]["label"] = "person"
    monitor.handle_event("new", person, now=NOW)

    assert store.open_incidents() == []


def test_occupancy_is_published_and_stored(monitor, store):  # noqa: F811
    monitor.handle_event("new", _event("new", [250, 650, 500, 970]), now=NOW)

    occupancy = [payload for suffix, payload in monitor.published if suffix == "occupancy"][-1]
    assert occupancy["occupied"] == 1
    assert occupancy["free"] == 1
    assert occupancy["enforcing"] is True
    assert occupancy["slots"]["front_left"]["plate"] == "AB12CDE"

    states = {row["slot"]: row for row in store.slot_states()}
    assert states["front_left"]["plate_display"] == "AB12CDE"
    assert states["front_right"]["plate_display"] == ""


def test_two_cars_sharing_one_bay_flags_the_straddler(monitor, store):  # noqa: F811
    monitor.handle_event("new", _event("new", [450, 650, 700, 970], object_id="obj2", plate="XY34ZZZ"), now=NOW)

    incidents = store.open_incidents()
    assert len(incidents) == 1
    assert incidents[0]["kind"] == "straddling"
    assert incidents[0]["plate_display"] == "XY34ZZZ"
