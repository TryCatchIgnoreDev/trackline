"""Pure 2D vector math used by the line-crossing counter.

All functions operate on plain ``(x, y)`` tuples in pixel space and have no
third-party dependencies, so they are trivially unit-testable.
"""

from __future__ import annotations

import math
from typing import Tuple

Point = Tuple[float, float]


def cross(o: Point, a: Point, b: Point) -> float:
    """Z-component of the cross product (a - o) x (b - o)."""
    return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])


def signed_distance(a: Point, b: Point, p: Point) -> float:
    """Signed perpendicular distance from ``p`` to the infinite line through a->b.

    Positive values lie on the side the normal ``n = (-dy, dx)`` points to.
    In image coordinates (y down) that is the right-hand side when walking a->b.
    """
    length = math.hypot(b[0] - a[0], b[1] - a[1])
    if length == 0.0:
        return 0.0
    return cross(a, b, p) / length


def side_of_line(a: Point, b: Point, p: Point, deadband: float = 0.0) -> int:
    """Return +1 / -1 for the side of a->b that ``p`` lies on, 0 inside the deadband."""
    d = signed_distance(a, b, p)
    if abs(d) <= deadband:
        return 0
    return 1 if d > 0 else -1


def _on_segment(p: Point, q: Point, r: Point) -> bool:
    """True if collinear point ``q`` lies on segment p-r."""
    return (
        min(p[0], r[0]) <= q[0] <= max(p[0], r[0])
        and min(p[1], r[1]) <= q[1] <= max(p[1], r[1])
    )


def _orientation(p: Point, q: Point, r: Point, eps: float = 1e-9) -> int:
    v = cross(p, q, r)
    if abs(v) <= eps:
        return 0
    return 1 if v > 0 else -1


def segments_intersect(p1: Point, p2: Point, q1: Point, q2: Point) -> bool:
    """Robust segment-segment intersection test (handles collinear cases)."""
    o1 = _orientation(p1, p2, q1)
    o2 = _orientation(p1, p2, q2)
    o3 = _orientation(q1, q2, p1)
    o4 = _orientation(q1, q2, p2)

    if o1 != o2 and o3 != o4:
        return True
    if o1 == 0 and _on_segment(p1, q1, p2):
        return True
    if o2 == 0 and _on_segment(p1, q2, p2):
        return True
    if o3 == 0 and _on_segment(q1, p1, q2):
        return True
    if o4 == 0 and _on_segment(q1, p2, q2):
        return True
    return False


def normal(a: Point, b: Point) -> Point:
    """Unit normal of a->b pointing to the positive ("IN") side."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    length = math.hypot(dx, dy) or 1.0
    return (-dy / length, dx / length)
