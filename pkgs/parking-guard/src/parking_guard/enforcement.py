"""Decides when a badly parked car is actually worth a message.

On a quiet Tuesday a car parked across two bays harms nobody, so enforcement waits
until the driveway is under pressure: either enough bookings are live, or match day
has been switched on from Home Assistant. Parking on a neighbour's property is the
exception — that one is never acceptable.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable

from parking_guard.config import GuardConfig
from parking_guard.slots import FORBIDDEN_AREA, CarAssessment, Evaluation
from parking_guard.store import Store

log = logging.getLogger(__name__)

Publisher = Callable[[str, dict[str, Any]], None]


class EnforcementGate:
    def __init__(self, store: Store, config: GuardConfig) -> None:
        self.store = store
        self.config = config
        self.manual_override: bool | None = None

    def set_override(self, value: str) -> None:
        """``on``/``off`` force enforcement; ``auto`` hands control back to bookings."""
        cleaned = value.strip().lower()
        if cleaned in ("on", "true", "1", "yes", "matchday"):
            self.manual_override = True
        elif cleaned in ("off", "false", "0", "no"):
            self.manual_override = False
        else:
            self.manual_override = None
        log.info("enforcement override set to %s", self.manual_override)

    @property
    def state(self) -> str:
        if self.manual_override is None:
            return "auto"
        return "on" if self.manual_override else "off"

    def active(self, now: float | None = None) -> bool:
        if not self.config.enforcement.enabled:
            return False
        if self.manual_override is not None:
            return self.manual_override
        ts = now if now is not None else time.time()
        return self.store.active_booking_count(ts) >= self.config.enforcement.when_bookings_at_least

    def should_raise(self, assessment: CarAssessment, now: float | None = None) -> bool:
        violation = assessment.primary
        if violation is None:
            return False
        if violation.kind == FORBIDDEN_AREA and self.config.enforcement.always_enforce_forbidden:
            return True
        if not self.active(now):
            return False

        ts = now if now is not None else time.time()
        last = self.store.last_incident_ts(assessment.car.plate, violation.kind)
        if last is not None and ts - last < self.config.enforcement.repeat_seconds:
            log.debug("skipping %s for %s, still inside the repeat window", violation.kind, assessment.car.plate)
            return False
        return True


class ComplianceWatcher:
    """Waits for a car to stay parked properly before calling an incident resolved."""

    def __init__(self, grace_seconds: float) -> None:
        self.grace_seconds = grace_seconds
        self._compliant_since: dict[str, float] = {}

    def observe(self, evaluation: Evaluation, now: float) -> list[str]:
        settled: list[str] = []
        seen: set[str] = set()

        for assessment in evaluation.assessments:
            object_id = assessment.car.object_id
            seen.add(object_id)
            if not assessment.compliant:
                self._compliant_since.pop(object_id, None)
                continue
            since = self._compliant_since.setdefault(object_id, now)
            if now - since >= self.grace_seconds:
                settled.append(object_id)

        for object_id in list(self._compliant_since):
            if object_id not in seen:
                self._compliant_since.pop(object_id, None)

        return settled

    def forget(self, object_id: str) -> None:
        self._compliant_since.pop(object_id, None)
