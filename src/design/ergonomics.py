"""Residential planning targets and evidence from actual, final geometry (mm).

These are planning recommendations, not Taiwan building-code approval. Keep
the established collision and circulation gates independent from this report.
"""
from dataclasses import replace

from shapely.affinity import affine_transform
from shapely.geometry import Polygon
from shapely.ops import unary_union

from src.design.collision.human_clearance import (
    HUMAN_CLEARANCE_RULES, clearance_regions, human_type,
)
from src.design.report import JsonReport
from src.drafting.fixtures import Counter, FixturePlacement, counter_footprint, fixture_footprint

KITCHEN_SOURCE = "https://media.nkba.org/uploads/2022/05/Kitchen-Planning-Guidelines.pdf"
BATH_SOURCE = "https://media.nkba.org/uploads/2022/05/Bath-Planning-Guidelines.pdf"
KITCHEN_ONE_COOK_MM = 1067.0
KITCHEN_MULTI_COOK_MM = 1219.0
BATH_FRONT_MM = 762.0
TOILET_CENTER_SIDE_MM = 457.0
SCOPE = "一般住宅平面操作空間建議；不等同台灣法規、無障礙或施工審查"
LABELS = {
    "bed_double": "雙人床", "bed_single": "單人床", "wardrobe": "衣櫃",
    "closet_rail": "吊衣櫃", "desk": "書桌", "sofa3": "三人沙發",
    "sofa2": "二人沙發", "armchair": "單人沙發", "table4": "四人餐桌",
    "table2": "靠牆二人餐桌", "toilet": "馬桶", "basin": "洗手台",
    "basin_small": "小洗手台", "shower": "淋浴間", "bathtub": "浴缸",
    "fridge": "冰箱", "kitchen_counter": "流理台",
}
SIDES = {"front": "前方", "back": "後方", "left": "左側", "right": "右側"}


def planning_rules():
    """Reuse baseline rules, overriding only externally verified targets."""
    rules = dict(HUMAN_CLEARANCE_RULES)
    rules["kitchen_counter"] = replace(rules["kitchen_counter"], front_clearance=KITCHEN_ONE_COOK_MM)
    for kind in ("toilet", "sink", "shower"):
        rules[kind] = replace(rules[kind], front_clearance=BATH_FRONT_MM)
    # The NKBA dimension starts at the toilet CENTERLINE, not its body edge.
    from src.drafting.fixtures import FIXTURE_SIZES
    rules["toilet"] = replace(rules["toilet"],
                            side_clearance=max(0, TOILET_CENTER_SIDE_MM - FIXTURE_SIZES["toilet"][0] / 2))
    return rules


def rule_basis(kind):
    if kind == "kitchen_counter":
        return {"kind": "published_recommendation", "source_url": KITCHEN_SOURCE,
                "reference": "NKBA Kitchen Planning Guideline 6，單人工作走道；多人建議 1219 mm"}
    if kind in ("toilet", "sink", "shower"):
        return {"kind": "published_recommendation", "source_url": BATH_SOURCE,
                "reference": "NKBA Bathroom Planning Guideline 4；馬桶側向另依 Guideline 20 的中心線尺寸"}
    return {"kind": "project_target", "source_url": "",
            "reference": "專題既有住宅配置目標，尚非經核實的通用標準"}


def footprint(item):
    if isinstance(item, Counter):
        return Polygon(counter_footprint(item))
    return Polygon(fixture_footprint(item))


def measure_region(zone, side, target, free):
    """Conservative depth clear across the entire strip, capped at its target.

    This is not an area-average pretending to be a passage width. A narrow
    obstacle near the furniture is reported even if most of the strip is free.
    """
    blocked = zone.difference(free)
    ratio = max(0, min(1, blocked.area / zone.area)) if zone.area else 0
    depth = target
    if blocked.area > 1.0:
        points = list(zone.exterior.coords)[:4]
        start, end = {"front": (0, 3), "back": (3, 0),
                      "left": (1, 0), "right": (0, 1)}[side]
        a, b = points[start], points[end]
        ux, uy = (b[0] - a[0]) / target, (b[1] - a[1]) / target
        # Rotate/translate into coordinates whose y axis is clearance depth.
        local = affine_transform(blocked, [uy, -ux, ux, uy, 0, -ux*a[0]-uy*a[1]])
        depth = max(0, min(target, local.bounds[1]))
    return {"side": side, "label": SIDES[side], "target_mm": target,
            "available_mm": round(depth, 1), "blocked_percent": round(100*ratio, 1),
            "status": "meets_target" if depth >= target - 1 else "needs_review"}


class ErgonomicsReport(JsonReport):
    def __init__(self, items, unchecked):
        self.items = items
        self.unchecked = unchecked

    def to_dict(self):
        warnings = sum(item["status"] == "needs_review" for item in self.items)
        return {"version": "residential-ergonomics-v1", "scope": SCOPE,
                "status": "needs_review" if warnings else "meets_checked_targets" if self.items else "unverified",
                "checked": len(self.items), "warnings": warnings,
                "items": self.items, "unchecked_furniture": self.unchecked,
                "method": "由家具使用面量到第一個障礙，取整條活動帶的保守淨深；上限為建議值。扣除實牆、柱、梯段及其他家具。",
                "not_checked": ["個人身高、年齡與行動能力", "桌下腿部空間與櫃內伸手高度",
                                "家電與櫃門實際開啟尺寸", "無障礙迴轉、移位及完整逃生驗證"]}

    def summary(self):
        report = self.to_dict()
        return f"人因操作空間：{report['checked']} 件已檢查，{report['warnings']} 件待調整"


def analyze_ergonomics(building):
    from src.design.column_design import column_footprints
    from src.design.layout.narrow_house import _stair_boxes
    from src.design.layout.plan_check import _wall_solid_union

    rules = planning_rules()
    items, unchecked = [], []
    for fi, floor in enumerate(building.floors):
        spec = floor.spec
        fixtures = [f for f in spec.fixtures if isinstance(f, (FixturePlacement, Counter))]
        bodies = [footprint(f) for f in fixtures]
        rooms = [(ri, r, Polygon(r.points)) for ri, r in enumerate(spec.rooms)
                 if r.kind not in ("garage", "parking", "patio", "pipe_shaft", "balcony")]
        walls = _wall_solid_union(spec)
        static = ([] if walls is None else [walls]) + list(column_footprints(spec)) + list(_stair_boxes(spec))
        for index, (f, body) in enumerate(zip(fixtures, bodies)):
            room_match = next(((ri, r, p) for ri, r, p in rooms if p.covers(body.centroid)), None)
            if room_match is None:
                continue
            ri, room, room_poly = room_match
            kind = human_type(f)
            name = "kitchen_counter" if isinstance(f, Counter) else f.name
            rule = rules.get(kind)
            if rule is None:
                unchecked.append({"floor": floor.label, "fixture_index": index, "name": name})
                continue
            blocks = static + [p for j, p in enumerate(bodies) if j != index]
            free = room_poly.difference(unary_union(blocks))
            regions = []
            for side, zone in clearance_regions(f, rule):
                target = rule.side_clearance if side in ("left", "right") else getattr(rule, side+"_clearance")
                regions.append(measure_region(zone, side, target, free))
            items.append({"id": f"f{fi}-fixture{index}", "floor": floor.label,
                          "room_id": f"f{fi}-room{ri}", "room_index": ri, "fixture_index": index, "room": room.name,
                          "furniture": LABELS.get(name, name), "name": name,
                          "status": "needs_review" if any(z["status"] == "needs_review" for z in regions) else "meets_target",
                          "regions": regions, "basis": rule_basis(kind)})
    return ErgonomicsReport(items, unchecked)
