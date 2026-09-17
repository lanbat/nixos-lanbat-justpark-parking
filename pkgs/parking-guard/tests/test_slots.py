from parking_guard.geometry import parse_polygon
from parking_guard.slots import (
    BLOCKING_AISLE,
    FORBIDDEN_AREA,
    MODE_ZONES,
    NOT_PULLED_FORWARD,
    OUTSIDE_BAY,
    STRADDLING,
    Area,
    Layout,
    ParkedCar,
    Slot,
    SlotRules,
    evaluate,
)


def _rect_polygon(x1, y1, x2, y2):
    return parse_polygon([[x1, y1], [x2, y1], [x2, y2], [x1, y2]])


LAYOUT = Layout(
    slots=(
        Slot(
            name="front_left",
            label="Front left",
            row=0,
            polygon=_rect_polygon(0.2, 0.6, 0.55, 1.0),
            forward_line=0.95,
        ),
        Slot(
            name="front_right",
            label="Front right",
            row=0,
            polygon=_rect_polygon(0.55, 0.6, 0.9, 1.0),
            forward_line=0.95,
        ),
        Slot(
            name="rear_left",
            label="Rear left",
            row=1,
            polygon=_rect_polygon(0.2, 0.2, 0.55, 0.6),
            forward_line=0.55,
            pair="front_left",
        ),
        Slot(
            name="rear_right",
            label="Rear right",
            row=1,
            polygon=_rect_polygon(0.55, 0.2, 0.9, 0.6),
            forward_line=0.55,
            pair="front_right",
        ),
    ),
    areas=(
        Area(name="neighbour", label="the neighbour's drive", polygon=_rect_polygon(0.9, 0.4, 1.0, 1.0)),
        Area(name="aisle", label="the shared aisle", kind="aisle", polygon=_rect_polygon(0.0, 0.2, 0.2, 1.0)),
    ),
)

RULES = SlotRules(settle_frames=10)


def _car(rect, plate="AB12 CDE", zones=(), motionless=30, stationary=True, object_id="obj1"):
    return ParkedCar(
        object_id=object_id,
        plate=plate,
        camera="c1",
        rect=rect,
        zones=zones,
        stationary=stationary,
        motionless_count=motionless,
    )


def _kinds(result):
    return {violation.kind for violation in result.violations}


def test_car_parked_neatly_and_pulled_forward_is_compliant():
    result = evaluate([_car((0.25, 0.65, 0.5, 0.97))], LAYOUT, RULES)
    assert result.violations == []
    assert result.occupancy == {"front_left": "AB12 CDE"}


def test_moving_car_is_not_assessed():
    moving = _car((0.25, 0.65, 0.5, 0.97), stationary=False)
    settling = _car((0.25, 0.65, 0.5, 0.97), motionless=2)
    assert evaluate([moving, settling], LAYOUT, RULES).assessments == []


def test_car_stopping_short_of_the_house_is_flagged():
    result = evaluate([_car((0.25, 0.55, 0.5, 0.85))], LAYOUT, RULES)
    assert _kinds(result) == {NOT_PULLED_FORWARD}
    assert result.assessments[0].slot == "front_left"


def test_car_across_two_bays_is_flagged_as_straddling():
    result = evaluate([_car((0.45, 0.65, 0.7, 0.97))], LAYOUT, RULES)
    assert STRADDLING in _kinds(result)
    assert "Front left" in result.violations[0].detail


def test_car_nowhere_near_a_bay_is_flagged():
    result = evaluate([_car((0.3, 0.02, 0.5, 0.15))], LAYOUT, RULES)
    assert _kinds(result) == {OUTSIDE_BAY}


def test_car_on_the_neighbours_property_short_circuits_other_rules():
    result = evaluate([_car((0.9, 0.5, 1.0, 0.9))], LAYOUT, RULES)
    assert _kinds(result) == {FORBIDDEN_AREA}
    assert result.violations[0].detail == "the neighbour's drive"


def test_car_left_in_the_aisle_is_flagged_once():
    result = evaluate([_car((0.02, 0.5, 0.18, 0.9))], LAYOUT, RULES)
    assert _kinds(result) == {BLOCKING_AISLE}


def test_rear_car_with_an_empty_bay_in_front_is_flagged():
    result = evaluate([_car((0.25, 0.25, 0.5, 0.57))], LAYOUT, RULES)
    assert _kinds(result) == {NOT_PULLED_FORWARD}
    assert "Front left" in result.violations[0].detail


def test_rear_car_is_fine_once_the_front_bay_is_taken():
    front = _car((0.25, 0.65, 0.5, 0.97), plate="AB12 CDE", object_id="front")
    rear = _car((0.25, 0.25, 0.5, 0.57), plate="XY34 ZZZ", object_id="rear")
    result = evaluate([front, rear], LAYOUT, RULES)
    assert result.violations == []
    assert result.occupancy == {"front_left": "AB12 CDE", "rear_left": "XY34 ZZZ"}


def test_zone_only_mode_uses_frigate_zone_membership():
    layout = Layout(
        slots=(
            Slot(name="front_left", zone="bay_front_left", forward_zone="bay_front_left_forward"),
        ),
        areas=(Area(name="neighbour", label="next door", zone="neighbour_drive"),),
    )
    rules = SlotRules(mode=MODE_ZONES, settle_frames=10)

    pulled_up = _car((0, 0, 0, 0), zones=("bay_front_left", "bay_front_left_forward"))
    assert evaluate([pulled_up], layout, rules).violations == []

    short = _car((0, 0, 0, 0), zones=("bay_front_left",))
    assert _kinds(evaluate([short], layout, rules)) == {NOT_PULLED_FORWARD}

    next_door = _car((0, 0, 0, 0), zones=("neighbour_drive",))
    assert _kinds(evaluate([next_door], layout, rules)) == {FORBIDDEN_AREA}


def test_occupancy_payload_reports_free_bays():
    result = evaluate([_car((0.25, 0.65, 0.5, 0.97))], LAYOUT, RULES)
    payload = result.occupancy_payload(LAYOUT)
    assert payload["occupied"] == 1
    assert payload["free"] == 3
    assert payload["slots"]["front_left"]["plate"] == "AB12 CDE"
    assert payload["slots"]["front_right"]["occupied"] is False
