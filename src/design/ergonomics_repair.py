"""Bounded, transactional repair driven by final measured activity-space gaps.

Moves furniture only. Never removes/resizes equipment or changes rooms, walls,
openings or rules to make a report pass. Unresolved candidates remain visible.
"""
from dataclasses import dataclass, field
from types import SimpleNamespace
import math

from shapely.geometry import Polygon

from src.design.ergonomics import analyze_ergonomics, footprint, planning_rules
from src.design.collision.human_clearance import clearance_regions, human_type
from src.design.collision.placement_optimizer import FurniturePlacementOptimizer
from src.design.collision.furniture_pair_constraint import FurniturePairEvaluator
from src.design.report import JsonReport
from src.drafting.fixtures import Counter, FixturePlacement


@dataclass
class RepairReport(JsonReport):
    status: str = "unchanged"
    before: int = 0
    after: int = 0
    attempts: int = 0
    candidate_limit: int = 128
    limit_reached: bool = False
    changes: list = field(default_factory=list)
    remaining: list = field(default_factory=list)
    error_type: str | None = None

    def to_dict(self):
        return dict(status=self.status, before=self.before, after=self.after,
                    attempts=self.attempts, candidate_limit=self.candidate_limit,
                    limit_reached=self.limit_reached, changes=self.changes,
                    remaining=self.remaining, error_type=self.error_type,
                    scope="有限候選的家具位置／方向修正；保留尺寸、數量及隔間，未自動修改門位或牆線")

    def summary(self):
        return f"人因自動修正：{len(self.changes)} 次改善，待核對家具 {self.before} → {self.after}"


def _items(spec, label):
    return analyze_ergonomics(SimpleNamespace(floors=[SimpleNamespace(label=label,spec=spec)])).items


def _gaps(items):
    return {(i["fixture_index"], z["side"]): max(0, z["target_mm"]-z["available_mm"])
            for i in items for z in i["regions"]}


def _better(before, after):
    old, new = _gaps(before), _gaps(after)
    # Keep every measured side at least as usable (1 mm measurement tolerance).
    if old.keys() != new.keys() or any(new[k] > old[k]+1 for k in old):
        return False
    return sum(new.values()) < sum(old.values())-1


def _quality(items):
    gaps = list(_gaps(items).values())
    return (sum(g > 1 for g in gaps), sum(gaps))


def _measure_changes(before, after):
    old = {(i["fixture_index"],z["side"]): z["available_mm"] for i in before for z in i["regions"]}
    return [{"fixture_index":i["fixture_index"],"furniture":i["furniture"],
             "side":z["side"],"label":z["label"],"target_mm":z["target_mm"],
             "before_mm":old[(i["fixture_index"],z["side"])],"after_mm":z["available_mm"]}
            for i in after for z in i["regions"]
            if abs(z["available_mm"]-old[(i["fixture_index"],z["side"])]) > 1]


def _faults(spec, label):
    from src.design.layout.plan_check import check_floor
    level = int(label[:-1]) if label[:-1].isdigit() else 1
    # Do not use the historical _plan_faults helper: it swallows exceptions.
    return {(i.code, i.detail) for i in check_floor(spec, level=level, label=label)
            if i.severity == "error"}


def _companions(f, here):
    """Bedside tables belong to their nearest bed, never to every bed in a room."""
    if f.name.startswith("bed_"):
        beds = [x for x in here if x.name.startswith("bed_")]
        return [s for s in here if s.name == "nightstand"
                and min(beds, key=lambda b: math.dist(b.insert,s.insert)) is f
                and math.dist(f.insert,s.insert) <= 1700]
    if f.name in ("sofa2","sofa3"):
        sofas = [x for x in here if x.name in ("sofa2","sofa3")]
        return [t for t in here if t.name == "tv_cabinet"
                and min(sofas,key=lambda s: math.dist(s.insert,t.insert)) is f]
    return []


def _pose(f):
    return (f.insert, f.rotation)


def _restore(saved):
    for f, pos, rot in saved:
        f.insert, f.rotation = pos, rot


def _move_group(f, group, saved, choice):
    pos, rot = saved[0][1:]
    angle = math.radians(choice.rotation-rot)
    ca, sa = math.cos(angle), math.sin(angle)
    for obj, point, old_rot in saved:
        dx,dy = point[0]-pos[0],point[1]-pos[1]
        obj.insert = (choice.insert[0]+ca*dx-sa*dy, choice.insert[1]+sa*dx+ca*dy)
        obj.rotation = (old_rot+choice.rotation-rot)%360


def _candidates(f, opt, inner, bad_zones):
    targeted = []
    body = footprint(f)
    for source, side, zone, missing in bad_zones:
        if source is not f and body.intersection(zone).area < 1:
            continue
        angle = math.radians(getattr(source,"rotation",0))
        front = (-math.sin(angle), math.cos(angle))
        if isinstance(source, Counter):
            (x,y),(u,v) = source.start,source.end
            length = math.hypot(u-x,v-y)
            front = (-(v-y)/length,(u-x)/length)
        dx,dy = {"front": front, "back": (-front[0],-front[1]),
                 "left": (-front[1],front[0]), "right": (front[1],-front[0])}[side]
        sign = -1 if source is f else 1
        targeted.append(FixturePlacement(f.name,(f.insert[0]+sign*dx*(missing+20),
                                                 f.insert[1]+sign*dy*(missing+20)),f.rotation))
    generic = opt.candidates(f.name, inner)
    # Center-origin tables need translation AND rotation choices, not nine points.
    if f.name in ("table4","table2","coffee_table"):
        generic += [FixturePlacement(f.name,c.insert,90) for c in list(generic)]
    generic.sort(key=lambda c: math.dist(c.insert,f.insert)+abs((c.rotation-f.rotation+180)%360-180)*5)
    seen = set()
    for c in targeted+generic:
        key = (tuple(round(x,1) for x in c.insert),c.rotation)
        if key not in seen and key != (tuple(round(x,1) for x in f.insert),f.rotation):
            seen.add(key)
            yield c


def repair_building(building, *, max_attempts=128, max_moves=6):
    """Accept only measured improvements with no new plan/pair/physical faults."""
    if max_attempts < 0 or max_moves < 0:
        raise ValueError("修正次數不能為負")
    report = RepairReport(candidate_limit=max_attempts)
    saved_all = [(f,*_pose(f)) for floor in building.floors for f in floor.spec.fixtures
                 if isinstance(f,FixturePlacement)]
    initial = []
    try:
        initial = analyze_ergonomics(building).items
        report.before = sum(i["status"] == "needs_review" for i in initial)
        report.after = report.before
        if not initial:
            report.status = "unverified"
            return report
        rules = planning_rules()
        for floor in building.floors:
            spec = floor.spec
            fixtures = [f for f in spec.fixtures if isinstance(f,(FixturePlacement,Counter))]
            current = _items(spec,floor.label)
            baseline_faults = _faults(spec,floor.label)
            from src.design.layout.fixture_fix import _wall_union
            walls = _wall_union(spec)
            for _ in range(2):
                accepted = False
                for ri, room in enumerate(spec.rooms):
                    bad = [i for i in current if i["room_index"] == ri and i["status"] == "needs_review"]
                    if not bad or report.attempts >= max_attempts or len(report.changes) >= max_moves:
                        continue
                    poly = Polygon(room.points)
                    here = [f for f in fixtures if isinstance(f,FixturePlacement) and poly.covers(footprint(f).centroid)]
                    zones = []
                    for item in bad:
                        source = fixtures[item["fixture_index"]]
                        regions = dict(clearance_regions(source,rules[human_type(source)]))
                        zones += [(source,z["side"],regions[z["side"]],z["target_mm"]-z["available_mm"])
                                  for z in item["regions"] if z["status"] == "needs_review"]
                    # Fix the obstruction as well as the furniture that reported it.
                    movers = [f for f in here if f.name not in ("nightstand","tv_cabinet")
                              and any(s is f or footprint(f).intersection(z).area > 1 for s,_,z,_ in zones)]
                    movers.sort(key=lambda f: (any(s is f for s,_,_,_ in zones),footprint(f).area))
                    from src.design.layout.auto_furnish import _inner_room
                    from src.design.layout.furniture_refine import _inward_options
                    inner = _inner_room(spec,room)
                    inward_before = _inward_options(spec,room)
                    pair = FurniturePairEvaluator()
                    for f in movers:
                        if report.attempts >= max_attempts or len(report.changes) >= max_moves:
                            break
                        opt = FurniturePlacementOptimizer(spec)
                        group = [f]+_companions(f,here)
                        saved = [(g,*_pose(g)) for g in group]
                        pair_before = [pair.evaluate_pair_constraints(g,room,opt.pair_targets).score for g in here]
                        best, best_rows, best_pose = _quality(current), None, None
                        for count, choice in enumerate(_candidates(f,opt,inner,zones)):
                            if count >= 24 or report.attempts >= max_attempts:
                                break
                            report.attempts += 1
                            _move_group(f,group,saved,choice)
                            try:
                                if any(not poly.buffer(1).covers(footprint(g)) or
                                       not opt.engine.check(g,ignore=group).valid for g in group):
                                    continue
                                # Collision engine uses compressed table footprints;
                                # the drawn footprint must still clear walls/columns.
                                engine = opt.engine
                                if any((walls is not None and footprint(g).intersection(walls).area > 1000)
                                       or any(footprint(g).intersection(c.poly).area > 1000 for c in engine.columns)
                                       for g in group):
                                    continue
                                targets = opt._build_pair_targets(spec)
                                if any(pair.evaluate_pair_constraints(g,room,targets).score < old-.1
                                       for g,old in zip(here,pair_before)):
                                    continue
                                inward_after = _inward_options(spec,room)
                                if any(ok and not inward_after.get(key,False) for key,ok in inward_before.items()):
                                    continue
                                trial = _items(spec,floor.label)
                                if not _better(current,trial) or _quality(trial) >= best:
                                    continue
                                if not _faults(spec,floor.label).issubset(baseline_faults):
                                    continue
                                best, best_rows = _quality(trial), trial
                                best_pose = [(g,*_pose(g)) for g in group]
                            finally:
                                _restore(saved)
                        if best_pose is not None:
                            _restore(best_pose)
                            changed_ids = [id(g) for g in group if _pose(g) != next((p,r) for obj,p,r in saved if obj is g)]
                            report.changes.append({"floor":floor.label,"room_index":ri,"room":room.name,
                                                   "fixture_index":next(i for i,obj in enumerate(fixtures) if obj is f),
                                                   "furniture":f.name,
                                                   "before":_pose_from(saved[0]),"after":_pose_from(best_pose[0]),
                                                   "moved_count":len(changed_ids),
                                                   "measurements":_measure_changes(current,best_rows),
                                                   "shortfall_mm_before":round(_quality(current)[1],1),
                                                   "shortfall_mm_after":round(best[1],1)})
                            current, accepted = best_rows, True
                if not accepted:
                    break
        final = analyze_ergonomics(building).items
        report.after = sum(i["status"] == "needs_review" for i in final)
        report.status = "improved" if report.changes else "not_needed" if not report.before else "unchanged"
        report.limit_reached = bool(report.after) and (report.attempts >= max_attempts or len(report.changes) >= max_moves)
        report.remaining = [{"floor":i["floor"],"room":i["room"],"id":i["id"],
                             "furniture":i["furniture"],
                             "reason":"本次有限候選內未找到不犧牲其他活動空間、家具關係與通路的擺法；需核對家具組合、門位或隔間。"}
                            for i in final if i["status"] == "needs_review"]
    except Exception as exc:
        _restore(saved_all)
        report.status,report.error_type = "unverified",type(exc).__name__
        report.changes,report.after = [],report.before
    return report


def _pose_from(row):
    return {"insert":list(row[1]),"rotation":row[2]}
