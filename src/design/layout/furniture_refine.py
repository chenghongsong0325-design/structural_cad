"""Improve furniture groups without sacrificing collision or room access."""
import math

from shapely.geometry import Polygon

from src.drafting.fixtures import FixturePlacement, fixture_footprint
from src.design.collision.human_clearance import HumanClearanceEvaluator
from src.design.collision.furniture_pair_constraint import point_of
from src.design.collision.placement_optimizer import FurniturePlacementOptimizer


def _facing(a, b):
    x, y = point_of(a)
    u, v = point_of(b)
    d = math.hypot(u - x, v - y)
    t = math.radians(a.rotation)
    return (-math.sin(t) * (u - x) + math.cos(t) * (v - y)) / max(d, 1)


def refine_room(spec, room):
    from src.design.layout.auto_furnish import _inner_room, _room_walkable

    poly = Polygon(room.points)
    here = [f for f in spec.fixtures if isinstance(f, FixturePlacement)
            and poly.covers(Polygon(fixture_footprint(f)).centroid)]
    human = HumanClearanceEvaluator()

    def quality():
        rows = [human.evaluate_human_clearance(f, room, spec.fixtures).score
                for f in here if f.name in ('toilet', 'basin', 'basin_small', 'table4', 'table2')]
        return min(rows, default=100)

    inner = _inner_room(spec, room)
    toilet = next((f for f in here if f.name == 'toilet'), None)
    basin = next((f for f in here if f.name in ('basin', 'basin_small')), None)
    if toilet and basin and quality() < 90:
        old = toilet.insert, toilet.rotation, basin.insert, basin.rotation
        best = quality()
        spec.fixtures.remove(basin)
        candidates = FurniturePlacementOptimizer(spec).candidates(toilet.name, inner)
        for choice in candidates:
            toilet.insert, toilet.rotation = choice.insert, choice.rotation
            opt = FurniturePlacementOptimizer(spec)
            if not opt.engine.check(toilet, ignore=toilet).valid:
                continue
            for candidate in opt.candidates(basin.name, inner):
                if not opt.engine.check(candidate).valid:
                    continue
                basin.insert, basin.rotation = candidate.insert, candidate.rotation
                spec.fixtures.append(basin)
                score = quality()
                if score > best + .1 and _room_walkable(spec, room):
                    best = score
                    old = toilet.insert, toilet.rotation, basin.insert, basin.rotation
                spec.fixtures.remove(basin)
        toilet.insert, toilet.rotation, basin.insert, basin.rotation = old
        spec.fixtures.append(basin)
    # Reposition the TV first; if necessary try a different sofa wall as a group.
    sofa = next((f for f in here if f.name in ('sofa3', 'sofa2')), None)
    tv = next((f for f in here if f.name == 'tv_cabinet'), None)
    if sofa and tv:
        old = sofa.insert, sofa.rotation, tv.insert, tv.rotation
        best = min(_facing(sofa, tv), _facing(tv, sofa))
        choices = [FixturePlacement(sofa.name, sofa.insert, sofa.rotation)]
        choices += FurniturePlacementOptimizer(spec).candidates(sofa.name, inner)
        spec.fixtures.remove(tv)
        for choice in choices:
            sofa.insert, sofa.rotation = choice.insert, choice.rotation
            opt = FurniturePlacementOptimizer(spec)
            if not opt.engine.check(sofa, ignore=sofa).valid:
                continue
            for candidate in opt.candidates(tv.name, inner):
                score = min(_facing(sofa, candidate), _facing(candidate, sofa))
                if score <= best + .001 or not opt.engine.check(candidate).valid:
                    continue
                spec.fixtures.append(candidate)
                ok = _room_walkable(spec, room)
                spec.fixtures.remove(candidate)
                if ok:
                    best = score
                    old = sofa.insert, sofa.rotation, candidate.insert, candidate.rotation
            if best > .98:
                break
        sofa.insert, sofa.rotation, tv.insert, tv.rotation = old
        spec.fixtures.append(tv)

    # Keep the existing equipment, looking for a better shared operation area.
    for f in here:
        if f.name not in ('table4', 'table2', 'toilet', 'basin', 'basin_small'):
            continue
        best = quality()
        old = f.insert, f.rotation
        opt = FurniturePlacementOptimizer(spec)
        for choice in opt.candidates(f.name, inner):
            if not opt.engine.check(choice, ignore=f).valid:
                continue
            f.insert, f.rotation = choice.insert, choice.rotation
            score = quality()
            if score > best + .1 and _room_walkable(spec, room):
                best, old = score, (f.insert, f.rotation)
        f.insert, f.rotation = old
