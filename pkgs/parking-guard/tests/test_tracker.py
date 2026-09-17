from parking_guard.mqtt_handler import parse_tracked_object
from parking_guard.tracker import ObjectTracker

FRAME = {"frame_width": 1920, "frame_height": 1080}


def _event(event_type="update", **overrides):
    after = {
        "id": "1607123955.475377-mxklsc",
        "camera": "c1",
        "label": "car",
        "box": [480, 540, 960, 1080],
        "current_zones": ["bay_front_left"],
        "stationary": True,
        "motionless_count": 45,
        "recognized_license_plate": "AB12CDE",
        "recognized_license_plate_score": 0.93,
    }
    after.update(overrides)
    return {"type": event_type, "before": dict(after), "after": after}


def test_parse_tracked_object_normalizes_box_and_zones():
    car = parse_tracked_object(_event(), **FRAME)
    assert car is not None
    assert car.rect == (0.25, 0.5, 0.5, 1.0)
    assert car.zones == ("bay_front_left",)
    assert car.stationary
    assert car.motionless_count == 45
    assert car.plate == "AB12CDE"


def test_parse_tracked_object_skips_other_labels():
    assert parse_tracked_object(_event(label="person"), **FRAME) is None


def test_parse_tracked_object_tolerates_missing_box_and_zones():
    car = parse_tracked_object(_event(box=None, current_zones=None), **FRAME)
    assert car is not None
    assert car.rect == (0.0, 0.0, 0.0, 0.0)
    assert car.zones == ()


def test_tracker_keeps_plate_when_a_later_event_omits_it():
    tracker = ObjectTracker()
    tracker.observe("new", parse_tracked_object(_event(), **FRAME), now=100.0)
    tracker.observe(
        "update",
        parse_tracked_object(_event(recognized_license_plate=None), **FRAME),
        now=110.0,
    )
    assert tracker.cars()[0].plate == "AB12CDE"


def test_tracker_accepts_plate_from_the_lpr_topic():
    tracker = ObjectTracker()
    car = parse_tracked_object(_event(recognized_license_plate=None), **FRAME)
    tracker.observe("new", car, now=100.0)
    assert tracker.set_plate(car.object_id, "XY34 ZZZ", score=0.8, now=101.0)
    assert not tracker.set_plate("unknown-object", "XY34 ZZZ")
    assert tracker.cars()[0].plate == "XY34 ZZZ"


def test_tracker_drops_objects_on_end_and_after_ttl():
    tracker = ObjectTracker(ttl_seconds=60.0)
    car = parse_tracked_object(_event(), **FRAME)
    tracker.observe("new", car, now=100.0)
    tracker.prune(now=200.0)
    assert tracker.cars() == []

    tracker.observe("new", car, now=300.0)
    tracker.observe("end", car, now=310.0)
    assert tracker.cars() == []
