"""Line-crossing counter built on centroid trajectories.

Algorithm (per track, per line):

1. Each frame the track's anchor point (box centroid or bottom-center) is
   classified against the line as side +1 / -1, or 0 if it lies within a small
   perpendicular *deadband* (suppresses jitter on the line itself).
2. We remember the last position where the track was confidently on a side
   (the "anchor"). When the current confident side differs from the anchor
   side, the motion segment anchor->current is tested for intersection with
   the finite line segment. Only real intersections count, so objects walking
   *around* the end of a line are ignored.
3. -1 -> +1 is an ``in`` crossing, +1 -> -1 is ``out``. Each track is counted
   at most once per (line, direction) to make ID-stable jitter harmless.
4. A track's class is the majority vote over its lifetime, which smooths
   detector class flicker.
"""

from __future__ import annotations

from collections import Counter as _Counter
from dataclasses import dataclass, field
from typing import Dict, Hashable, Iterable, List, Optional, Sequence, Set, Tuple

from .geometry import Point, segments_intersect, side_of_line


@dataclass
class LineSpec:
    """A counting line in pixel coordinates."""

    id: str
    name: str
    a: Point
    b: Point

    @property
    def key(self) -> Hashable:
        # Geometry is part of the key: moving a line invalidates side history.
        return (self.id, round(self.a[0], 1), round(self.a[1], 1), round(self.b[0], 1), round(self.b[1], 1))


@dataclass
class Detection:
    track_id: int
    cls: int
    point: Point


@dataclass
class CrossingEvent:
    frame: int
    track_id: int
    cls: int
    line_id: str
    line_name: str
    direction: str  # "in" | "out"


@dataclass
class _TrackState:
    last_seen: int
    votes: _Counter = field(default_factory=_Counter)
    anchors: Dict[Hashable, Tuple[int, Point]] = field(default_factory=dict)
    counted: Set[Tuple[str, str]] = field(default_factory=set)

    def majority_class(self) -> int:
        return self.votes.most_common(1)[0][0]


class LineCounter:
    """Stateful, single-threaded counter. Owned by the pipeline worker thread."""

    def __init__(self, deadband_px: float = 2.0, max_age: int = 90) -> None:
        self.deadband_px = deadband_px
        self.max_age = max_age
        self._tracks: Dict[int, _TrackState] = {}
        # counts[line_id][direction][cls] -> n
        self.counts: Dict[str, Dict[str, _Counter]] = {}

    # ------------------------------------------------------------------ state
    def reset_tracks(self) -> None:
        """Forget trajectories (e.g. after a model/tracker swap) but keep totals."""
        self._tracks.clear()

    def reset_all(self) -> None:
        self._tracks.clear()
        self.counts.clear()

    @property
    def active_tracks(self) -> int:
        return len(self._tracks)

    # ----------------------------------------------------------------- update
    def update(
        self,
        frame_idx: int,
        lines: Sequence[LineSpec],
        detections: Iterable[Detection],
    ) -> List[CrossingEvent]:
        events: List[CrossingEvent] = []

        for det in detections:
            st = self._tracks.get(det.track_id)
            if st is None:
                st = _TrackState(last_seen=frame_idx)
                self._tracks[det.track_id] = st
            st.last_seen = frame_idx
            st.votes[det.cls] += 1

            for line in lines:
                ev = self._step(st, det, line, frame_idx)
                if ev is not None:
                    events.append(ev)

        self._prune(frame_idx)
        return events

    def _step(self, st: _TrackState, det: Detection, line: LineSpec, frame_idx: int) -> Optional[CrossingEvent]:
        side = side_of_line(line.a, line.b, det.point, self.deadband_px)
        if side == 0:
            return None  # inside deadband: wait for a confident reading

        key = line.key
        prev = st.anchors.get(key)
        st.anchors[key] = (side, det.point)
        if prev is None or prev[0] == side:
            return None

        prev_side, prev_point = prev
        if not segments_intersect(prev_point, det.point, line.a, line.b):
            return None  # passed beside the line's extent

        direction = "in" if prev_side < 0 < side else "out"
        tag = (line.id, direction)
        if tag in st.counted:
            return None
        st.counted.add(tag)

        cls = st.majority_class()
        bucket = self.counts.setdefault(line.id, {"in": _Counter(), "out": _Counter()})
        bucket[direction][cls] += 1
        return CrossingEvent(frame_idx, det.track_id, cls, line.id, line.name, direction)

    def _prune(self, frame_idx: int) -> None:
        stale = [tid for tid, st in self._tracks.items() if frame_idx - st.last_seen > self.max_age]
        for tid in stale:
            del self._tracks[tid]

    # ---------------------------------------------------------------- reports
    def totals(self) -> Dict[str, int]:
        t_in = sum(sum(b["in"].values()) for b in self.counts.values())
        t_out = sum(sum(b["out"].values()) for b in self.counts.values())
        return {"in": t_in, "out": t_out, "total": t_in + t_out}

    def per_class(self) -> Dict[int, Dict[str, int]]:
        out: Dict[int, Dict[str, int]] = {}
        for bucket in self.counts.values():
            for direction in ("in", "out"):
                for cls, n in bucket[direction].items():
                    row = out.setdefault(cls, {"in": 0, "out": 0})
                    row[direction] += n
        return out

    def per_line(self) -> Dict[str, Dict[str, int]]:
        return {
            lid: {"in": sum(b["in"].values()), "out": sum(b["out"].values())}
            for lid, b in self.counts.items()
        }
