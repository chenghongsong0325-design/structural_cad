"""Bedroom access regressions from the user's 4.5 x 14 m townhouse."""
from types import SimpleNamespace

import pytest
from shapely.geometry import Point, Polygon

from src.design.collision.geometry import door_swing_polygon
from src.design.layout.narrow_house import generate_narrow_building
from src.design.layout.plan_check import check_building
from src.design.layout.room_circulation import PASSAGE_WIDTH, analyze_room
from src.drafting.door_window import Door
from src.drafting.fixtures import fixture_footprint
from src.drafting.wall import Opening, Wall


def test_sliding_door_keeps_access_without_a_fictitious_swing():
    wall = Wall((0, 0), (4000, 0), openings=[Opening(2000, 1200, "door")])
    dp = SimpleNamespace(wall_index=0, opening_index=0, door=Door(sliding=True))
    spec = SimpleNamespace(walls=[wall])
    clearance = door_swing_polygon(spec, dp)
    assert clearance.covers(Point(2000, PASSAGE_WIDTH - 1))
    assert clearance.covers(Point(2000, -PASSAGE_WIDTH + 1))
    assert not clearance.covers(Point(2000, 1000))
    dp.door.sliding = False
    dp.door.swing = "out"
    assert door_swing_polygon(spec, dp).covers(Point(2000, 1000))


@pytest.mark.parametrize("seed", [0, 1, 2, 3])
def test_master_double_bed_keeps_entry_side_open(seed):
    floors = generate_narrow_building(4500, 14000, floors=2, seed=seed)
    spec = floors[1][1]
    room = next(r for r in spec.rooms if r.kind == "master_bedroom")
    poly = Polygon(room.points)
    beds = [f for f in spec.fixtures if getattr(f, "name", "") == "bed_double"
            and poly.contains(Polygon(fixture_footprint(f)).centroid)]
    assert len(beds) == 1  # Do not repair the layout by shrinking/deleting the bed.
    bed = beds[0]
    stairs = Polygon(next(r for r in spec.rooms if r.kind == "stair_hall").points)
    entries = [Point(*w.point_at(op.position)) for w in spec.walls for op in w.openings
               if op.kind == "door" and poly.boundary.distance(Point(*w.point_at(op.position))) < 1
               and stairs.distance(Point(*w.point_at(op.position))) < 1]
    assert len(entries) == 1
    x0, _, x1, _ = poly.bounds
    mid = (x0 + x1) / 2
    assert (bed.insert[0] - mid) * (entries[0].x - mid) < 0
    # The headboard stays at the wall; the circulation repair must not float it.
    assert min(abs(bed.insert[0] - x0), abs(bed.insert[0] - x1)) <= 100
    stands = [f for f in spec.fixtures if getattr(f, "name", "") == "nightstand"
              and poly.contains(Polygon(fixture_footprint(f)).centroid)]
    assert len(stands) == 2
    assert all(f.rotation == bed.rotation for f in stands)
    assert analyze_room(spec, room).ok
    assert check_building(floors).ok


def test_mirrored_entrance_places_headboard_on_the_other_wall():
    from src.design.layout.auto_furnish import furnish_spec
    from src.design.layout_generator import _mirror_spec

    spec = generate_narrow_building(4500, 14000, floors=2, seed=0,
                                    furnish=False)[1][1]
    spec = _mirror_spec(spec, True, False)
    furnish_spec(spec)
    room = next(r for r in spec.rooms if r.kind == "master_bedroom")
    poly = Polygon(room.points)
    bed = next(f for f in spec.fixtures if getattr(f, "name", "") == "bed_double"
               and poly.contains(Polygon(fixture_footprint(f)).centroid))
    assert bed.rotation == 90.0
    assert abs(bed.insert[0] - poly.bounds[2]) <= 100
    assert analyze_room(spec, room).ok
