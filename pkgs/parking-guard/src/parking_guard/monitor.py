"""Turns a stream of Frigate object updates into incidents and resolutions."""

from __future__ import annotations

import logging
import time
from typing import Any, Callable

from parking_guard.config import GuardConfig
from parking_guard.dispatcher import Dispatcher
from parking_guard.enforcement import ComplianceWatcher, EnforcementGate
from parking_guard.mqtt_handler import parse_tracked_object
from parking_guard.slots import Evaluation, evaluate
from parking_guard.store import Store
from parking_guard.tracker import ObjectTracker

log = logging.getLogger(__name__)

Publisher = Callable[[str, dict[str, Any], bool], None]


class MisparkingMonitor:
    def __init__(
        self,
        *,
        store: Store,
        config: GuardConfig,
        dispatcher: Dispatcher,
        tracker: ObjectTracker | None = None,
        gate: EnforcementGate | None = None,
        watcher: ComplianceWatcher | None = None,
        publish: Publisher | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.store = store
        self.config = config
        self.dispatcher = dispatcher
        self.tracker = tracker or ObjectTracker()
        self.gate = gate or EnforcementGate(store, config)
        self.watcher = watcher or ComplianceWatcher(config.enforcement.resolve_grace_seconds)
        self.publish = publish or (lambda suffix, payload, retain=False: None)
        self.clock = clock

    def handle_event(self, event_type: str, payload: dict[str, Any], now: float | None = None) -> None:
        if not self.config.misparking_enabled:
            return

        car = parse_tracked_object(
            payload,
            frame_width=self.config.frame_width,
            frame_height=self.config.frame_height,
            object_labels=self.config.object_labels,
        )
        if car is None:
            return
        if self.config.cameras and car.camera and car.camera not in self.config.cameras:
            return

        ts = now if now is not None else self.clock()

        if event_type == "end":
            self.tracker.forget(car.object_id)
            self.watcher.forget(car.object_id)
            self.dispatcher.resolve_object(car.object_id, now=ts)
            self.publish_occupancy(ts)
            return

        self.tracker.observe(event_type, car, now=ts)
        evaluation = evaluate(self.tracker.cars(), self.config.layout, self.config.rules)

        for object_id in self.watcher.observe(evaluation, ts):
            self.dispatcher.resolve_object(object_id, now=ts)

        for assessment in evaluation.assessments:
            if assessment.compliant or not self.gate.should_raise(assessment, ts):
                continue
            self._raise_incident(assessment, ts)

        self.publish_occupancy(ts, evaluation)

    def _raise_incident(self, assessment: Any, ts: float) -> None:
        violation = assessment.primary
        car = self.tracker.get(assessment.car.object_id) or assessment.car
        booking_ref = (
            self.store.booking_ref_for(car.plate, ts, self.config.grace_seconds) if car.plate else ""
        )

        incident_id, created = self.store.open_incident(
            plate=car.plate,
            object_id=car.object_id,
            kind=violation.kind,
            slot=violation.slot,
            detail=violation.detail,
            camera=car.camera,
            frigate_id=car.object_id,
            booking_ref=booking_ref,
            now=ts,
        )
        if not created:
            return

        self.publish(
            "violation",
            {
                "incident_id": incident_id,
                "plate": car.plate,
                "kind": violation.kind,
                "slot": violation.slot,
                "detail": violation.detail,
                "camera": car.camera,
                "ts": ts,
            },
            False,
        )
        log.warning(
            "incident %s: %s (%s) for %s",
            incident_id,
            violation.kind,
            violation.detail,
            car.plate or car.object_id,
        )
        self.dispatcher.notify(incident_id, now=ts)

    def publish_occupancy(self, now: float, evaluation: Evaluation | None = None) -> dict[str, Any]:
        if evaluation is None:
            evaluation = evaluate(self.tracker.cars(), self.config.layout, self.config.rules)

        payload = evaluation.occupancy_payload(self.config.layout)
        payload["enforcing"] = self.gate.active(now)
        payload["ts"] = now
        self.publish("occupancy", payload, True)

        for slot_name, state in payload["slots"].items():
            self.store.set_slot_state(
                slot=slot_name,
                plate=str(state.get("plate", "")),
                compliant=bool(state.get("compliant", True)),
                now=now,
            )
        return payload

    def note_plate(self, object_id: str, plate: str, score: float | None = None) -> None:
        """Called when the LPR topic names a car the event topic is already tracking."""
        self.tracker.set_plate(object_id, plate, score)
