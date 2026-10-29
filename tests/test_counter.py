from app.counter import Detection, LineCounter, LineSpec
from app.geometry import segments_intersect, side_of_line

H_LINE = LineSpec(id="L1", name="gate", a=(0.0, 100.0), b=(200.0, 100.0))


def walk(counter, tid, points, cls=0, start=0):
    events = []
    for i, p in enumerate(points):
        events += counter.update(start + i, [H_LINE], [Detection(tid, cls, p)])
    return events


def test_side_and_deadband():
    assert side_of_line((0, 0), (10, 0), (5, 5)) == 1
    assert side_of_line((0, 0), (10, 0), (5, -5)) == -1
    assert side_of_line((0, 0), (10, 0), (5, 1), deadband=2) == 0


def test_segments_intersect():
    assert segments_intersect((0, 0), (10, 10), (0, 10), (10, 0))
    assert not segments_intersect((0, 0), (1, 1), (5, 5), (6, 0))
    assert segments_intersect((0, 0), (10, 0), (5, 0), (15, 0))  # collinear overlap


def test_single_crossing_in_and_out():
    c = LineCounter(deadband_px=2)
    # y increases downward; normal (-dy, dx) = (0, 200) -> +y side is "in"
    ev = walk(c, 1, [(50, 80), (50, 95), (50, 105), (50, 120)])
    assert [e.direction for e in ev] == ["in"]
    ev = walk(c, 1, [(50, 110), (50, 90)], start=10)
    assert [e.direction for e in ev] == ["out"]
    assert c.totals() == {"in": 1, "out": 1, "total": 2}


def test_jitter_on_line_is_ignored():
    c = LineCounter(deadband_px=3)
    ev = walk(c, 1, [(50, 90), (50, 101), (50, 99), (50, 102), (50, 98)])
    assert ev == []


def test_repeat_crossing_counted_once_per_direction():
    c = LineCounter(deadband_px=1)
    ev = walk(c, 7, [(50, 90), (50, 110), (50, 90), (50, 110), (50, 90)])
    assert [e.direction for e in ev] == ["in", "out"]


def test_passing_beside_line_extent_is_ignored():
    c = LineCounter()
    ev = walk(c, 3, [(300, 80), (300, 120)])
    assert ev == []


def test_majority_class_vote():
    c = LineCounter()
    for i, (cls, y) in enumerate([(2, 80), (2, 85), (7, 90), (2, 120)]):
        ev = c.update(i, [H_LINE], [Detection(1, cls, (50, y))])
    assert ev[0].cls == 2
    assert c.per_class() == {2: {"in": 1, "out": 0}}


def test_moving_line_resets_side_history():
    c = LineCounter()
    c.update(0, [H_LINE], [Detection(1, 0, (50, 80))])
    moved = LineSpec(id="L1", name="gate", a=(0, 60), b=(200, 60))
    # Point is now on the "in" side of the moved line; must not count as a crossing.
    assert c.update(1, [moved], [Detection(1, 0, (50, 80))]) == []
