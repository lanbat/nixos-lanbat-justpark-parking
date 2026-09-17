"""Polygon helpers for parking slot geometry.

Coordinates are normalized to 0..1 the same way Frigate's zone editor emits them,
so slot polygons can be copied straight out of the Frigate UI. Frigate's MQTT
``box`` is in pixels, hence :func:`normalize_box`.
"""

from __future__ import annotations

from typing import Iterable, Sequence

Point = tuple[float, float]
Polygon = tuple[Point, ...]
Rect = tuple[float, float, float, float]


def parse_polygon(raw: object) -> Polygon:
    """Accept Frigate's ``"x,y,x,y,..."`` string, a flat list, or a list of pairs."""
    if raw is None:
        return ()

    if isinstance(raw, str):
        parts = [p for p in raw.replace("\n", ",").split(",") if p.strip()]
        values = [float(p) for p in parts]
        return _pairs(values)

    if isinstance(raw, Sequence):
        items = list(raw)
        if not items:
            return ()
        if all(isinstance(item, (int, float)) for item in items):
            return _pairs([float(item) for item in items])
        points: list[Point] = []
        for item in items:
            if isinstance(item, Sequence) and len(item) == 2:
                points.append((float(item[0]), float(item[1])))
        return tuple(points)

    raise TypeError(f"unsupported polygon value: {raw!r}")


def _pairs(values: list[float]) -> Polygon:
    if len(values) % 2 != 0:
        raise ValueError("polygon needs an even number of coordinates")
    return tuple((values[i], values[i + 1]) for i in range(0, len(values), 2))


def polygon_area(polygon: Polygon) -> float:
    if len(polygon) < 3:
        return 0.0
    total = 0.0
    for i, (x1, y1) in enumerate(polygon):
        x2, y2 = polygon[(i + 1) % len(polygon)]
        total += x1 * y2 - x2 * y1
    return abs(total) / 2.0


def point_in_polygon(point: Point, polygon: Polygon) -> bool:
    """Ray casting; points exactly on an edge may fall either way."""
    if len(polygon) < 3:
        return False
    x, y = point
    inside = False
    for i, (x1, y1) in enumerate(polygon):
        x2, y2 = polygon[(i + 1) % len(polygon)]
        if (y1 > y) != (y2 > y):
            t = (y - y1) / (y2 - y1)
            if x < x1 + t * (x2 - x1):
                inside = not inside
    return inside


def clip_polygon(subject: Polygon, clipper: Polygon) -> Polygon:
    """Sutherland-Hodgman. The clipper must be convex; the subject need not be."""
    if len(subject) < 3 or len(clipper) < 3:
        return ()

    output = list(subject)
    orientation = _signed_area(clipper)
    edges = list(zip(clipper, clipper[1:] + clipper[:1]))
    if orientation < 0:
        edges = [(b, a) for a, b in reversed(edges)]

    for edge_start, edge_end in edges:
        if not output:
            return ()
        current = output
        output = []
        previous = current[-1]
        previous_inside = _is_left(edge_start, edge_end, previous) >= 0
        for point in current:
            inside = _is_left(edge_start, edge_end, point) >= 0
            if inside:
                if not previous_inside:
                    crossing = _intersect(previous, point, edge_start, edge_end)
                    if crossing is not None:
                        output.append(crossing)
                output.append(point)
            elif previous_inside:
                crossing = _intersect(previous, point, edge_start, edge_end)
                if crossing is not None:
                    output.append(crossing)
            previous = point
            previous_inside = inside

    return tuple(output)


def intersection_area(subject: Polygon, clipper: Polygon) -> float:
    return polygon_area(clip_polygon(subject, clipper))


def coverage(rect: Rect, polygon: Polygon) -> float:
    """Fraction of the rectangle's area that falls inside the polygon."""
    area = rect_area(rect)
    if area <= 0.0:
        return 0.0
    # The rectangle is convex, so clipping the (possibly concave) slot against it
    # is the safe direction for Sutherland-Hodgman.
    return min(1.0, intersection_area(polygon, rect_to_polygon(rect)) / area)


def rect_area(rect: Rect) -> float:
    x1, y1, x2, y2 = rect
    return max(0.0, x2 - x1) * max(0.0, y2 - y1)


def rect_to_polygon(rect: Rect) -> Polygon:
    x1, y1, x2, y2 = rect
    return ((x1, y1), (x2, y1), (x2, y2), (x1, y2))


def bottom_center(rect: Rect) -> Point:
    """The ground contact point Frigate uses for zone membership."""
    x1, y1, x2, y2 = rect
    return ((x1 + x2) / 2.0, y2)


def normalize_box(box: Iterable[float], frame_width: int, frame_height: int) -> Rect:
    values = [float(v) for v in box]
    if len(values) != 4:
        raise ValueError("box must be [x1, y1, x2, y2]")
    if frame_width <= 0 or frame_height <= 0:
        raise ValueError("frame dimensions must be positive")

    x1, y1, x2, y2 = values
    # Frigate emits pixels, but tolerate an already-normalized box.
    if max(values) <= 1.0:
        rect = (x1, y1, x2, y2)
    else:
        rect = (x1 / frame_width, y1 / frame_height, x2 / frame_width, y2 / frame_height)

    x1, y1, x2, y2 = (_clamp(v) for v in rect)
    return (min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def _signed_area(polygon: Polygon) -> float:
    total = 0.0
    for i, (x1, y1) in enumerate(polygon):
        x2, y2 = polygon[(i + 1) % len(polygon)]
        total += x1 * y2 - x2 * y1
    return total / 2.0


def _is_left(a: Point, b: Point, p: Point) -> float:
    return (b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0])


def _intersect(p1: Point, p2: Point, q1: Point, q2: Point) -> Point | None:
    r = (p2[0] - p1[0], p2[1] - p1[1])
    s = (q2[0] - q1[0], q2[1] - q1[1])
    denominator = r[0] * s[1] - r[1] * s[0]
    if abs(denominator) < 1e-12:
        return None
    t = ((q1[0] - p1[0]) * s[1] - (q1[1] - p1[1]) * s[0]) / denominator
    return (p1[0] + t * r[0], p1[1] + t * r[1])
