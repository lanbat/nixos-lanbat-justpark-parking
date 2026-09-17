"""Live view of the cars Frigate is currently tracking on the driveway.

``frigate/events`` carries position and stationary state; ``frigate/tracked_object_update``
carries the plate. Both are keyed on the same tracked object id, so this keeps one
merged record per car until Frigate ends the object or it goes stale.
"""

from __future__ import annotations

import time
from dataclasses import replace

from parking_guard.slots import ParkedCar

DEFAULT_TTL_SECONDS = 900.0


class ObjectTracker:
    def __init__(self, ttl_seconds: float = DEFAULT_TTL_SECONDS) -> None:
        self.ttl_seconds = ttl_seconds
        self._cars: dict[str, ParkedCar] = {}
        self._seen: dict[str, float] = {}

    def observe(self, event_type: str, car: ParkedCar, now: float | None = None) -> ParkedCar | None:
        ts = now if now is not None else time.time()
        if event_type == "end":
            self.forget(car.object_id)
            return None

        known = self._cars.get(car.object_id)
        if known is not None and not car.plate and known.plate:
            car = replace(car, plate=known.plate, score=car.score if car.score is not None else known.score)

        self._cars[car.object_id] = car
        self._seen[car.object_id] = ts
        self.prune(ts)
        return car

    def set_plate(self, object_id: str, plate: str, score: float | None = None, now: float | None = None) -> bool:
        """Attach a plate seen on the LPR topic. Returns False for unknown objects."""
        car = self._cars.get(object_id)
        if car is None or not plate:
            return False
        self._cars[object_id] = replace(car, plate=plate, score=score if score is not None else car.score)
        self._seen[object_id] = now if now is not None else time.time()
        return True

    def get(self, object_id: str) -> ParkedCar | None:
        return self._cars.get(object_id)

    def cars(self) -> list[ParkedCar]:
        return list(self._cars.values())

    def forget(self, object_id: str) -> None:
        self._cars.pop(object_id, None)
        self._seen.pop(object_id, None)

    def prune(self, now: float | None = None) -> None:
        ts = now if now is not None else time.time()
        stale = [key for key, seen in self._seen.items() if ts - seen > self.ttl_seconds]
        for key in stale:
            self.forget(key)
