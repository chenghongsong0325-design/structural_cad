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


def _valid(opt, placement, ignore=None):
    if not opt.engine.check(placement, ignore=ignore).valid:
        return False
    poly = Polygon(fixture_footprint(placement))
    return all(poly.intersection(c.poly).area <= 1000 for c in opt.engine.columns)


def refine_room(spec, room):
    from src.design.layout.auto_furnish import _inner_room, _room_walkable

    poly = Polygon(room.points)
    here = [f for f in spec.fixtures if isinstance(f, FixturePlacement)
            and poly.covers(Polygon(fixture_footprint(f)).centroid)]
    human = HumanClearanceEvaluator()
    saved = [(f, f.insert, f.rotation) for f in here]
    inward_before = _inward_options(spec, room)
    if inward_before and not all(inward_before.values()):
        # Do not make a compact candidate look better by relying on a bathroom
        # door sweeping the public hall. Leave it to the layout fallback.
        return

    def quality():
        rows = [human.evaluate_human_clearance(f, room, spec.fixtures).score
                for f in here if f.name in ('toilet', 'basin', 'basin_small', 'table4', 'table2',
                                            'wardrobe', 'fridge', 'bed_double', 'bed_single')]
        return min(rows, default=100)

    def layout_quality():
        # Lexicographic minimax: improve the next bottleneck even if another
        # piece cannot move. Never worsen the room's worst activity space.
        return tuple(sorted(round(human.evaluate_human_clearance(f, room, spec.fixtures).score, 3)
                            for f in here if f.name in ('toilet','basin','basin_small','table4','table2',
                                                       'wardrobe','fridge','bed_double','bed_single')))

    inner = _inner_room(spec, room)
    toilet = next((f for f in here if f.name == 'toilet'), None)
    basin = next((f for f in here if f.name in ('basin', 'basin_small')), None)
    bath_valid = True
    if toilet and basin:
        opt = FurniturePlacementOptimizer(spec)
        bath_valid = all(_valid(opt,f,ignore=[toilet,basin]) for f in (toilet,basin))
    if toilet and basin and (quality() < 90 or not bath_valid):
        old = toilet.insert, toilet.rotation, basin.insert, basin.rotation
        best = quality() if bath_valid else -1
        spec.fixtures.remove(basin)
        candidates = FurniturePlacementOptimizer(spec).candidates(toilet.name, inner)
        for choice in candidates:
            toilet.insert, toilet.rotation = choice.insert, choice.rotation
            opt = FurniturePlacementOptimizer(spec)
            if not _valid(opt, toilet, ignore=toilet):
                continue
            for candidate in opt.candidates(basin.name, inner):
                if not _valid(opt, candidate):
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
    if sofa and tv and min(_facing(sofa,tv), _facing(tv,sofa)) < .98:
        old = sofa.insert, sofa.rotation, tv.insert, tv.rotation
        best = min(_facing(sofa, tv), _facing(tv, sofa))
        choices = [FixturePlacement(sofa.name, sofa.insert, sofa.rotation)]
        choices += FurniturePlacementOptimizer(spec).candidates(sofa.name, inner)
        spec.fixtures.remove(tv)
        for choice in choices:
            sofa.insert, sofa.rotation = choice.insert, choice.rotation
            opt = FurniturePlacementOptimizer(spec)
            if not _valid(opt, sofa, ignore=sofa):
                continue
            for candidate in opt.candidates(tv.name, inner):
                score = min(_facing(sofa, candidate), _facing(candidate, sofa))
                if score <= best + .001 or not _valid(opt, candidate):
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
        if f.name not in ('table4', 'table2', 'toilet', 'basin', 'basin_small', 'wardrobe', 'fridge', 'bed_double', 'bed_single'):
            continue
        best = layout_quality()
        if not best or best[0] >= 99:
            continue
        old = f.insert, f.rotation
        stands = [s for s in here if s.name == 'nightstand'] if f.name.startswith('bed_') else []
        stand_old = [(s, s.insert, s.rotation) for s in stands]
        opt = FurniturePlacementOptimizer(spec)
        group = [f] + stands
        candidates = opt.candidates(f.name, inner)
        if f.name in ('table4','table2'):
            candidates += [FixturePlacement(f.name,p.insert,90) for p in list(candidates)]
        for choice in candidates:
            if not _valid(opt, choice, ignore=group):
                continue
            f.insert, f.rotation = choice.insert, choice.rotation
            angle = math.radians(choice.rotation - old[1])
            ca, sa = math.cos(angle), math.sin(angle)
            for stand, pos, rot in stand_old:
                dx, dy = pos[0] - old[0][0], pos[1] - old[0][1]
                stand.insert = (choice.insert[0]+ca*dx-sa*dy, choice.insert[1]+sa*dx+ca*dy)
                stand.rotation = (rot + choice.rotation - old[1]) % 360
            from src.design.collision.furniture_engine import FurnitureCollisionEngine
            engine = FurnitureCollisionEngine(spec)
            if any(not engine.check(s, ignore=s).valid or any(Polygon(fixture_footprint(s)).intersection(c.poly).area > 1000 for c in engine.columns) for s in stands):
                continue
            score = layout_quality()
            if score > best and _room_walkable(spec, room):
                best, old = score, (f.insert, f.rotation)
                stand_old = [(s, s.insert, s.rotation) for s in stands]
        f.insert, f.rotation = old
        for stand, pos, rot in stand_old:
            stand.insert, stand.rotation = pos, rot


    inward_after = _inward_options(spec, room)
    if any(ok and not inward_after.get(key, False) for key, ok in inward_before.items()):
        for f, pos, rot in saved:
            f.insert, f.rotation = pos, rot


def _inward_options(spec, room):
    from src.design.layout.door_rules import (_door_sides, PUBLIC_KINDS, BATH_KINDS,
        _swing_sector, _swing_obstacles, _other_door_sectors, SWING_OVERLAP_TOL)
    from src.design.layout.plan_check import building_env
    from src.drafting.door_window import Door
    if room.kind not in BATH_KINDS:
        return {}
    rooms=[(r,Polygon(r.points)) for r in spec.rooms]
    result={}
    for dp in spec.doors:
        if dp.door.sliding:continue
        w=spec.walls[dp.wall_index];op=w.openings[dp.opening_index]
        sides,_=_door_sides(spec,w,op,rooms,building_env(spec))
        for i,r in enumerate(sides):
            other=sides[1-i]
            if r is room and other is not None and other.kind in PUBLIC_KINDS:
                obs=_swing_obstacles(spec,w,op)+_other_door_sectors(spec,dp)
                result[id(dp)]=any(sum(_swing_sector(w,op,Door(hinge=h,swing='out' if i==0 else 'in')).intersection(o).area
                                       for o in obs)<=SWING_OVERLAP_TOL for h in ('left','right'))
    return result


def refine_spec(spec):
    """Rollback all moves if final collision/door/circulation faults increase."""
    from src.design.layout.fixture_fix import _plan_faults
    for room in spec.rooms:
        if room.kind not in ('parking', 'garage', 'patio', 'balcony', 'pipe_shaft'):
            before = _plan_faults(spec)
            saved = [(f, f.insert, f.rotation) for f in spec.fixtures if isinstance(f, FixturePlacement)]
            refine_room(spec, room)
            if _plan_faults(spec) > before:
                for f, pos, rot in saved:
                    f.insert, f.rotation = pos, rot


def usage_issues(building):
    """Report unresolved activity-space constraints on the actual final furniture."""
    issues = []
    labels = {'wardrobe':'衣櫃','fridge':'冰箱','bed_double':'雙人床','bed_single':'單人床',
              'table4':'餐桌','table2':'餐桌','toilet':'馬桶','basin':'洗手台','basin_small':'洗手台'}
    human = HumanClearanceEvaluator()
    for floor in building.floors:
        spec = floor.spec
        for room in spec.rooms:
            if room.kind in ('garage','parking','patio','pipe_shaft','balcony'):
                continue
            poly = Polygon(room.points)
            for f in spec.fixtures:
                if not isinstance(f,FixturePlacement) or f.name not in labels:
                    continue
                if not poly.covers(Polygon(fixture_footprint(f)).centroid):
                    continue
                res = human.evaluate_human_clearance(f,room,spec.fixtures)
                if res.violations:
                    issues.append({'floor':floor.label,'room':room.name,'furniture':labels[f.name],
                                   'detail':res.violations,'scope':'家具活動空間建議值，非法規判定'})
    return issues
