import time

from parking_guard.enforcement import ComplianceWatcher, EnforcementGate
from parking_guard.slots import (
    FORBIDDEN_AREA,
    NOT_PULLED_FORWARD,
    CarAssessment,
    Evaluation,
    ParkedCar,
    Violation,
)
from parking_guard.store import Booking, Store

from test_dispatcher import NOW, _config


def _assessment(kind=NOT_PULLED_FORWARD, plate="AB12 CDE", object_id="obj1"):
    car = ParkedCar(object_id=object_id, plate=plate, stationary=True, motionless_count=99)
    return CarAssessment(
        car=car,
        slot="front_left",
        violations=[Violation(kind=kind, object_id=object_id, plate=plate, slot="front_left")],
    )


def _booking(plate, ref):
    return Booking(
        plate=plate,
        plate_normalized=plate.replace(" ", ""),
        start_ts=NOW - 600,
        end_ts=NOW + 3600,
        booking_ref=ref,
        source="test",
    )


def test_quiet_day_does_not_nag(tmp_path):
    store = Store(tmp_path / "test.db")
    store.replace_allowlist([_booking("AB12 CDE", "b1")])
    gate = EnforcementGate(store, _config())

    assert not gate.active(NOW)
    assert not gate.should_raise(_assessment(), NOW)
    store.close()


def test_pressure_from_multiple_bookings_turns_enforcement_on(tmp_path):
    store = Store(tmp_path / "test.db")
    store.replace_allowlist([_booking("AB12 CDE", "b1"), _booking("XY34 ZZZ", "b2")])
    gate = EnforcementGate(store, _config())

    assert gate.active(NOW)
    assert gate.should_raise(_assessment(), NOW)
    store.close()


def test_matchday_override_from_home_assistant(tmp_path):
    store = Store(tmp_path / "test.db")
    gate = EnforcementGate(store, _config())

    gate.set_override("on")
    assert gate.state == "on"
    assert gate.active(NOW)

    gate.set_override("off")
    assert not gate.active(NOW)

    gate.set_override("auto")
    assert gate.state == "auto"
    assert not gate.active(NOW)
    store.close()


def test_neighbours_property_is_always_enforced(tmp_path):
    store = Store(tmp_path / "test.db")
    gate = EnforcementGate(store, _config())

    assert not gate.active(NOW)
    assert gate.should_raise(_assessment(kind=FORBIDDEN_AREA), NOW)
    store.close()


def test_repeat_window_stops_the_same_complaint_twice(tmp_path):
    store = Store(tmp_path / "test.db")
    store.replace_allowlist([_booking("AB12 CDE", "b1"), _booking("XY34 ZZZ", "b2")])
    gate = EnforcementGate(store, _config())

    assert gate.should_raise(_assessment(), NOW)
    store.open_incident(plate="AB12 CDE", object_id="obj1", kind=NOT_PULLED_FORWARD, now=NOW)

    assert not gate.should_raise(_assessment(object_id="obj2"), NOW + 60)
    assert gate.should_raise(_assessment(object_id="obj2"), NOW + 1801)
    store.close()


def test_compliant_assessment_never_raises(tmp_path):
    store = Store(tmp_path / "test.db")
    gate = EnforcementGate(store, _config())
    gate.set_override("on")
    assert not gate.should_raise(CarAssessment(car=ParkedCar(object_id="obj1")), NOW)
    store.close()


def test_watcher_waits_out_the_grace_before_resolving():
    watcher = ComplianceWatcher(grace_seconds=60)
    good = Evaluation(assessments=[CarAssessment(car=ParkedCar(object_id="obj1"))])

    assert watcher.observe(good, NOW) == []
    assert watcher.observe(good, NOW + 30) == []
    assert watcher.observe(good, NOW + 61) == ["obj1"]


def test_watcher_restarts_the_clock_when_the_car_misbehaves_again():
    watcher = ComplianceWatcher(grace_seconds=60)
    good = Evaluation(assessments=[CarAssessment(car=ParkedCar(object_id="obj1"))])
    bad = Evaluation(assessments=[_assessment()])

    watcher.observe(good, NOW)
    watcher.observe(bad, NOW + 30)
    assert watcher.observe(good, NOW + 61) == []
    assert watcher.observe(good, NOW + 122) == ["obj1"]


def test_watcher_forgets_cars_that_drove_away():
    watcher = ComplianceWatcher(grace_seconds=60)
    good = Evaluation(assessments=[CarAssessment(car=ParkedCar(object_id="obj1"))])
    watcher.observe(good, NOW)

    assert watcher.observe(Evaluation(), NOW + 61) == []
    assert watcher.observe(good, NOW + 62) == []


def test_gate_uses_wall_clock_by_default(tmp_path):
    store = Store(tmp_path / "test.db")
    gate = EnforcementGate(store, _config())
    gate.set_override("on")
    assert gate.active() is True
    assert gate.should_raise(_assessment(), time.time())
    store.close()
