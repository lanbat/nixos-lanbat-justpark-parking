"""Driveway slot model and misparking rules.

The driveway holds four cars on a busy day: two rows of two, every car pulled up
as close to the house (and therefore the camera) as it can get. A car that stops
short, sits across two bays, blocks the aisle, or drifts onto a neighbour's
property costs somebody else their space.

Two evaluation modes:

``zones``
    Trust Frigate's own zone membership (``current_zones``). Frigate tests the
    bottom-center of the bounding box, so a ``forwardZone`` drawn over just the
    near-house part of a bay answers "did they pull up far enough". Straddling
    cannot be seen from a single point, so that rule is skipped.

``geometry``
    Use our own normalized polygons against the raw ``box``, which measures how
    much of the car sits in each bay and how far short of the forward line it
    stopped.

``hybrid`` (default) runs both and takes the union of what each can prove.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Sequence

from parking_guard.geometry import Polygon, Rect, bottom_center, coverage, point_in_polygon

MODE_ZONES = "zones"
MODE_GEOMETRY = "geometry"
MODE_HYBRID = "hybrid"

FORBIDDEN_AREA = "forbidden_area"
BLOCKING_AISLE = "blocking_aisle"
OUTSIDE_BAY = "outside_bay"
STRADDLING = "straddling"
NOT_PULLED_FORWARD = "not_pulled_forward"

# Most serious first — the primary violation drives the message wording.
VIOLATION_PRIORITY = (
    FORBIDDEN_AREA,
    BLOCKING_AISLE,
    OUTSIDE_BAY,
    STRADDLING,
    NOT_PULLED_FORWARD,
)


@dataclass(frozen=True)
class Slot:
    name: str
    label: str = ""
    row: int = 0
    zone: str = ""
    forward_zone: str = ""
    polygon: Polygon = ()
    forward_line: float | None = None
    pair: str = ""

    @property
    def display(self) -> str:
        return self.label or self.name


@dataclass(frozen=True)
class Area:
    """A no-parking region: a neighbour's frontage, the aisle, a gate, a bin store."""

    name: str
    label: str = ""
    kind: str = "forbidden"
    zone: str = ""
    polygon: Polygon = ()

    @property
    def display(self) -> str:
        return self.label or self.name


@dataclass(frozen=True)
class SlotRules:
    mode: str = MODE_HYBRID
    min_slot_coverage: float = 0.4
    straddle_coverage: float = 0.25
    forward_tolerance: float = 0.08
    settle_frames: int = 30
    forward_axis: str = "down"


@dataclass(frozen=True)
class Layout:
    slots: tuple[Slot, ...] = ()
    areas: tuple[Area, ...] = ()

    def slot(self, name: str) -> Slot | None:
        for slot in self.slots:
            if slot.name == name:
                return slot
        return None


@dataclass(frozen=True)
class ParkedCar:
    object_id: str
    plate: str = ""
    camera: str = ""
    rect: Rect = (0.0, 0.0, 0.0, 0.0)
    zones: tuple[str, ...] = ()
    stationary: bool = False
    motionless_count: int = 0
    score: float | None = None

    def settled(self, rules: SlotRules) -> bool:
        return self.stationary and self.motionless_count >= rules.settle_frames


@dataclass(frozen=True)
class Violation:
    kind: str
    object_id: str
    plate: str
    slot: str = ""
    detail: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "object_id": self.object_id,
            "plate": self.plate,
            "slot": self.slot,
            "detail": self.detail,
        }


@dataclass
class CarAssessment:
    car: ParkedCar
    slot: str = ""
    coverages: dict[str, float] = field(default_factory=dict)
    violations: list[Violation] = field(default_factory=list)

    @property
    def compliant(self) -> bool:
        return not self.violations

    @property
    def primary(self) -> Violation | None:
        for kind in VIOLATION_PRIORITY:
            for violation in self.violations:
                if violation.kind == kind:
                    return violation
        return None


@dataclass
class Evaluation:
    assessments: list[CarAssessment] = field(default_factory=list)
    occupancy: dict[str, str] = field(default_factory=dict)

    @property
    def violations(self) -> list[Violation]:
        return [v for a in self.assessments for v in a.violations]

    def occupancy_payload(self, layout: Layout) -> dict[str, object]:
        slots: dict[str, object] = {}
        for slot in layout.slots:
            plate = self.occupancy.get(slot.name, "")
            assessment = self._assessment_for_slot(slot.name)
            slots[slot.name] = {
                "label": slot.display,
                "row": slot.row,
                "occupied": bool(plate),
                "plate": plate,
                "compliant": assessment.compliant if assessment else True,
            }
        used = sum(1 for slot in layout.slots if self.occupancy.get(slot.name))
        return {
            "slots": slots,
            "occupied": used,
            "free": max(0, len(layout.slots) - used),
            "violations": [v.as_dict() for v in self.violations],
        }

    def _assessment_for_slot(self, name: str) -> CarAssessment | None:
        for assessment in self.assessments:
            if assessment.slot == name:
                return assessment
        return None


def evaluate(cars: Iterable[ParkedCar], layout: Layout, rules: SlotRules) -> Evaluation:
    """Assess every settled car, then apply the rules that need the whole picture."""
    assessments = [_assess_car(car, layout, rules) for car in cars if car.settled(rules)]

    occupancy: dict[str, str] = {}
    for assessment in assessments:
        if assessment.slot and assessment.slot not in occupancy:
            occupancy[assessment.slot] = assessment.car.plate or assessment.car.object_id

    for assessment in assessments:
        _apply_pull_forward_rule(assessment, layout, rules, occupancy)

    return Evaluation(assessments=assessments, occupancy=occupancy)


def _assess_car(car: ParkedCar, layout: Layout, rules: SlotRules) -> CarAssessment:
    assessment = CarAssessment(car=car)

    forbidden = _matching_areas(car, layout, rules, kind="forbidden")
    if forbidden:
        for area in forbidden:
            assessment.violations.append(
                Violation(
                    kind=FORBIDDEN_AREA,
                    object_id=car.object_id,
                    plate=car.plate,
                    detail=area.display,
                )
            )
        return assessment

    for area in _matching_areas(car, layout, rules, kind="aisle"):
        assessment.violations.append(
            Violation(
                kind=BLOCKING_AISLE,
                object_id=car.object_id,
                plate=car.plate,
                detail=area.display,
            )
        )

    assessment.coverages = _slot_coverages(car, layout, rules)
    ranked = sorted(assessment.coverages.items(), key=lambda item: item[1], reverse=True)
    best = ranked[0] if ranked else ("", 0.0)

    if not best[0] or best[1] < rules.min_slot_coverage:
        if not assessment.violations:
            assessment.violations.append(
                Violation(
                    kind=OUTSIDE_BAY,
                    object_id=car.object_id,
                    plate=car.plate,
                    detail=_nearest_hint(ranked),
                )
            )
        return assessment

    assessment.slot = best[0]

    if _uses_geometry(rules):
        straddled = [
            name
            for name, value in ranked[1:]
            if value >= rules.straddle_coverage and name != assessment.slot
        ]
        if straddled:
            slots = ", ".join(_slot_display(layout, name) for name in straddled)
            assessment.violations.append(
                Violation(
                    kind=STRADDLING,
                    object_id=car.object_id,
                    plate=car.plate,
                    slot=assessment.slot,
                    detail=slots,
                )
            )

    return assessment


def _apply_pull_forward_rule(
    assessment: CarAssessment,
    layout: Layout,
    rules: SlotRules,
    occupancy: dict[str, str],
) -> None:
    slot = layout.slot(assessment.slot) if assessment.slot else None
    if slot is None:
        return

    car = assessment.car

    if _uses_zones(rules) and slot.forward_zone and slot.zone:
        if slot.forward_zone not in car.zones:
            assessment.violations.append(
                Violation(
                    kind=NOT_PULLED_FORWARD,
                    object_id=car.object_id,
                    plate=car.plate,
                    slot=slot.name,
                    detail=f"not inside {slot.forward_zone}",
                )
            )
            return

    if _uses_geometry(rules) and slot.forward_line is not None:
        near_edge = _near_house_edge(car.rect, rules)
        shortfall = _shortfall(near_edge, slot.forward_line, rules)
        if shortfall > rules.forward_tolerance:
            assessment.violations.append(
                Violation(
                    kind=NOT_PULLED_FORWARD,
                    object_id=car.object_id,
                    plate=car.plate,
                    slot=slot.name,
                    detail=f"{shortfall:.2f} short of the forward line",
                )
            )
            return

    # A car tucked in the back row while the bay in front of it stands empty is
    # the same problem seen from the other end of the driveway.
    if slot.pair and not occupancy.get(slot.pair):
        assessment.violations.append(
            Violation(
                kind=NOT_PULLED_FORWARD,
                object_id=car.object_id,
                plate=car.plate,
                slot=slot.name,
                detail=f"{_slot_display(layout, slot.pair)} is empty in front",
            )
        )


def _slot_coverages(car: ParkedCar, layout: Layout, rules: SlotRules) -> dict[str, float]:
    coverages: dict[str, float] = {}
    for slot in layout.slots:
        value = 0.0
        if _uses_geometry(rules) and slot.polygon:
            value = coverage(car.rect, slot.polygon)
        if _uses_zones(rules) and slot.zone and slot.zone in car.zones:
            # Frigate already decided the car's ground point is in this bay; treat
            # that as a full match so zone-only deployments still assign a slot.
            value = max(value, 1.0)
        if value > 0.0:
            coverages[slot.name] = value
    return coverages


def _matching_areas(car: ParkedCar, layout: Layout, rules: SlotRules, *, kind: str) -> list[Area]:
    matches: list[Area] = []
    ground = bottom_center(car.rect)
    for area in layout.areas:
        if area.kind != kind:
            continue
        if _uses_zones(rules) and area.zone and area.zone in car.zones:
            matches.append(area)
            continue
        if _uses_geometry(rules) and area.polygon and point_in_polygon(ground, area.polygon):
            matches.append(area)
    return matches


def _near_house_edge(rect: Rect, rules: SlotRules) -> float:
    x1, y1, x2, y2 = rect
    return y2 if rules.forward_axis == "down" else y1


def _shortfall(near_edge: float, forward_line: float, rules: SlotRules) -> float:
    if rules.forward_axis == "down":
        return max(0.0, forward_line - near_edge)
    return max(0.0, near_edge - forward_line)


def _slot_display(layout: Layout, name: str) -> str:
    slot = layout.slot(name)
    return slot.display if slot else name


def _nearest_hint(ranked: Sequence[tuple[str, float]]) -> str:
    if not ranked or not ranked[0][0]:
        return "no marked bay"
    name, value = ranked[0]
    return f"only {value:.0%} inside {name}"


def _uses_zones(rules: SlotRules) -> bool:
    return rules.mode in (MODE_ZONES, MODE_HYBRID)


def _uses_geometry(rules: SlotRules) -> bool:
    return rules.mode in (MODE_GEOMETRY, MODE_HYBRID)
