"""Furniture Pair Constraint(v0.7 Phase 6-4-2)—— 家具**之間**的關聯偏好。

Phase 6-4 的 FurnitureConstraint 講的是「單件家具自己的擺法」(背靠牆、留淨空);
本模組講的是「兩件家具/一件家具與空間之間的關係」:沙發要面向電視、床頭櫃要
貼著床、書桌要靠近窗、洗衣機要靠近陽台、冰箱要在廚房……

⚠️ **唯讀、純軟偏好**(比照整個 Phase 6):只回「這個候選位置符不符合家具關係」
的 0~100 分,**不**改 spec、**不**能讓非法位置變合法。合法與否永遠由
FurnitureCollisionEngine(硬閘門)決定;本模組只在「所有 collision 合法的位置」
之間,幫 optimizer 偏好挑出關係更合理的那個。

一條規則 = FurniturePairRule(source_type, target_type, relation, weight,
ideal_distance, max_distance)。type 是**類別**(canonical type),不是圖塊名——
sofa3→sofa、tv_cabinet→tv、bed_double→bed、table4→dining_table(見 FIXTURE_TYPE),
另有非家具目標 window/kitchen/balcony 等(由 optimizer 以 PairTarget 提供)。

支援的 relation(見 RELATIONS):
    NEAR       離目標越近越好(distance ≤ ideal 滿分,> max 扣到低分)。
    FAR        離目標越遠越好(NEAR 的反向)。
    FACE       正面朝向目標越準越好(用 source 的 rotation 算朝向向量)。
    ALIGN      兩者中心線越貼齊越好(某一軸的偏移越小越好)。
    CENTER     source 越靠房間中央越好(不需要 target)。
    LEFT_OF    source 越明確在目標左側(較小 x)越好。
    RIGHT_OF   source 越明確在目標右側(較大 x)越好。

評分:滿分 100。找不到 target 家具的規則**跳過、不扣分**。有 target 的規則各自
算 0~100,再依 weight 加權平均成總分。

典型用法::

    ev = FurniturePairEvaluator()                     # 用預設規則
    res = ev.evaluate_pair_constraints(candidate, room, placed_furniture)
    res.score        # 0~100
    res.violations   # 分數過低的規則
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from shapely.geometry import Polygon

from src.design.report import JsonReport
from src.drafting.fixtures import (
    Counter,
    FixturePlacement,
    counter_footprint,
    fixture_footprint,
)

# ── relation 語彙 ──────────────────────────────────────────────────────────
NEAR = "NEAR"
FAR = "FAR"
FACE = "FACE"
ALIGN = "ALIGN"
CENTER = "CENTER"
LEFT_OF = "LEFT_OF"
RIGHT_OF = "RIGHT_OF"
RELATIONS = frozenset({NEAR, FAR, FACE, ALIGN, CENTER, LEFT_OF, RIGHT_OF})

# 分數低於此值算「違反」(記進 violations)。
VIOLATION_THRESHOLD = 50.0

# 圖塊名 → 關聯規則用的 canonical 類別。未列者用原名(fallback)。
FIXTURE_TYPE = {
    "sofa3": "sofa", "armchair": "armchair",
    "tv_cabinet": "tv",
    "bed_single": "bed", "bed_double": "bed", "nightstand": "nightstand",
    "table4": "dining_table", "coffee_table": "coffee_table",
    "desk": "desk", "bookshelf": "bookshelf", "wardrobe": "wardrobe",
    "fridge": "fridge", "bar_stool": "bar_stool",
    "toilet": "toilet", "basin": "basin", "bathtub": "bathtub",
    "shoe_cabinet": "shoe_cabinet", "car": "car",
}


@dataclass
class PairTarget:
    """非家具(或簡化)目標:一個類別 + 代表點 + 可選朝向。

    optimizer 用它把「窗 / 廚房 / 陽台」等空間包成可比對的目標,讓
    desk→window、fridge→kitchen、washer→balcony 這類規則也能評分。"""

    type: str
    point: tuple
    rotation: float = 0.0


# ── 目標歸一:類別 / 代表點 / 朝向 ────────────────────────────────────────
def type_of(obj) -> str:
    """一個目標的 canonical 類別。"""
    if isinstance(obj, PairTarget):
        return obj.type
    if isinstance(obj, Counter):
        return "counter"
    if isinstance(obj, FixturePlacement):
        return FIXTURE_TYPE.get(obj.name, obj.name)
    if hasattr(obj, "kind"):                    # Room
        return obj.kind
    if hasattr(obj, "name"):
        return FIXTURE_TYPE.get(obj.name, obj.name)
    return ""


def point_of(obj) -> tuple:
    """一個目標的代表點(形心)。"""
    if isinstance(obj, PairTarget):
        return tuple(obj.point)
    if isinstance(obj, Counter):
        c = Polygon(counter_footprint(obj)).centroid
        return (c.x, c.y)
    if isinstance(obj, FixturePlacement):
        c = Polygon(fixture_footprint(obj)).centroid
        return (c.x, c.y)
    if hasattr(obj, "points"):                  # Room
        c = Polygon(obj.points).centroid
        return (c.x, c.y)
    if hasattr(obj, "insert"):
        return tuple(obj.insert)
    raise TypeError(f"無法取得 {obj!r} 的代表點")


def rotation_of(obj) -> float:
    return float(getattr(obj, "rotation", 0.0) or 0.0)


def _room_centroid(room) -> tuple:
    c = Polygon(room.points).centroid
    return (c.x, c.y)


def _clamp(v, lo=0.0, hi=100.0):
    return max(lo, min(hi, v))


# ── 規則 ────────────────────────────────────────────────────────────────────
@dataclass
class FurniturePairRule(JsonReport):
    """一條家具關聯規則。

    source_type    施加規則的家具類別(candidate 的類別要等於它才觸發)。
    target_type    要看的另一方類別(在 placed_furniture 裡找;CENTER 不需要)。
    relation       關係種類(RELATIONS 之一)。
    weight         加權平均時的權重。
    ideal_distance 理想距離(mm):NEAR 內側滿分、FAR 的門檻、ALIGN/方向的尺度。
    max_distance   距離上限(mm):NEAR 的衰減終點、FAR 的滿分點。
    """

    source_type: str
    target_type: str
    relation: str
    weight: float = 1.0
    ideal_distance: float = 1000.0
    max_distance: float = 3000.0

    def __post_init__(self):
        if self.relation not in RELATIONS:
            raise ValueError(f"未知 relation {self.relation!r},可用:{sorted(RELATIONS)}")

    @property
    def rid(self) -> str:
        return f"{self.source_type}->{self.target_type} {self.relation}"

    def to_dict(self) -> dict:
        return {
            "source_type": self.source_type,
            "target_type": self.target_type,
            "relation": self.relation,
            "weight": float(self.weight),
            "ideal_distance": float(self.ideal_distance),
            "max_distance": float(self.max_distance),
        }


# 預設規則:涵蓋題目列的六個例子 + 沙發面向電視 / 電視對齊沙發。
DEFAULT_PAIR_RULES: list[FurniturePairRule] = [
    FurniturePairRule("sofa", "tv", FACE, 1.0, 3000.0, 5000.0),
    FurniturePairRule("sofa", "tv", NEAR, 0.5, 2500.0, 4500.0),
    FurniturePairRule("tv", "sofa", ALIGN, 0.8, 500.0, 1500.0),
    FurniturePairRule("bed", "nightstand", NEAR, 1.0, 300.0, 900.0),
    FurniturePairRule("dining_table", "kitchen", NEAR, 0.6, 3000.0, 7000.0),
    FurniturePairRule("desk", "window", NEAR, 0.8, 800.0, 2500.0),
    FurniturePairRule("washer", "balcony", NEAR, 0.8, 1500.0, 4000.0),
    FurniturePairRule("fridge", "kitchen", NEAR, 0.7, 1000.0, 5000.0),
]


# ── 查詢結果 ────────────────────────────────────────────────────────────────
@dataclass
class PairConstraintResult(JsonReport):
    """一次家具關聯評分的結果。

    score          0~100 加權平均(沒有任何可評規則 → 100,不扣分)。
    reasons        每條被評規則一行文字(供 debug)。
    violations     分數 < VIOLATION_THRESHOLD 的規則 rid。
    matched_rules  真正被評到(有 target)的規則 rid。
    """

    score: float = 100.0
    reasons: list = field(default_factory=list)
    violations: list = field(default_factory=list)
    matched_rules: list = field(default_factory=list)

    def __bool__(self) -> bool:
        return not self.violations

    def to_dict(self) -> dict:
        return {
            "score": round(self.score, 1),
            "reasons": list(self.reasons),
            "violations": list(self.violations),
            "matched_rules": list(self.matched_rules),
        }

    def __str__(self) -> str:
        if not self.matched_rules:
            return f"PairConstraint: {self.score:.0f}(無適用規則)"
        return (f"PairConstraint: {self.score:.0f} · " +
                " · ".join(self.reasons))


# ── 各 relation 的評分 ─────────────────────────────────────────────────────
def _score_near(d, ideal, mx):
    if d <= ideal:
        return 100.0
    if d <= mx:
        return _clamp(100.0 - 50.0 * (d - ideal) / max(mx - ideal, 1e-6))
    return _clamp(50.0 - 50.0 * (d - mx) / max(mx, 1e-6))


def _score_far(d, ideal, mx):
    if d >= mx:
        return 100.0
    if d >= ideal:
        return _clamp(50.0 + 50.0 * (d - ideal) / max(mx - ideal, 1e-6))
    return _clamp(50.0 * d / max(ideal, 1e-6))


def _score_face(src_pt, src_rot, tgt_pt):
    sx, sy = src_pt
    tx, ty = tgt_pt
    dx, dy = tx - sx, ty - sy
    dist = math.hypot(dx, dy)
    if dist < 1e-6:
        return 100.0                            # 疊在一起,視為朝向滿足
    a = math.radians(src_rot)
    fx, fy = -math.sin(a), math.cos(a)          # 正面(局部 +Y)朝向向量
    cos = (fx * dx + fy * dy) / dist
    return _clamp(100.0 * cos)                  # cos<0(背對)→ 0


def _score_align(src_pt, tgt_pt, ideal):
    sx, sy = src_pt
    tx, ty = tgt_pt
    off = min(abs(sx - tx), abs(sy - ty))       # 較貼齊的那一軸的偏移
    return _clamp(100.0 * (1.0 - off / max(ideal, 1e-6)))


def _score_side(src_pt, tgt_pt, ideal, want_right):
    delta = src_pt[0] - tgt_pt[0]               # +x = 右側
    norm = max(-1.0, min(1.0, delta / max(ideal, 1e-6)))
    return _clamp(50.0 + 50.0 * norm) if want_right else _clamp(50.0 - 50.0 * norm)


def _score_rule(rule, src_pt, src_rot, tgt_pt) -> float:
    r = rule.relation
    if r == NEAR:
        return _score_near(math.dist(src_pt, tgt_pt), rule.ideal_distance,
                           rule.max_distance)
    if r == FAR:
        return _score_far(math.dist(src_pt, tgt_pt), rule.ideal_distance,
                          rule.max_distance)
    if r == FACE:
        return _score_face(src_pt, src_rot, tgt_pt)
    if r == ALIGN:
        return _score_align(src_pt, tgt_pt, rule.ideal_distance)
    if r == CENTER:
        return _score_near(math.dist(src_pt, tgt_pt), rule.ideal_distance,
                           rule.max_distance)
    if r == RIGHT_OF:
        return _score_side(src_pt, tgt_pt, rule.ideal_distance, True)
    if r == LEFT_OF:
        return _score_side(src_pt, tgt_pt, rule.ideal_distance, False)
    return 100.0                                # 不會到(__post_init__ 已擋)


# ── Evaluator ──────────────────────────────────────────────────────────────
class FurniturePairEvaluator:
    """對一個候選擺位評「家具關聯偏好」。**唯讀**:不改任何東西。"""

    def __init__(self, rules: list[FurniturePairRule] | None = None):
        self.rules = list(rules) if rules is not None else list(DEFAULT_PAIR_RULES)

    def evaluate_pair_constraints(self, placement, room,
                                  placed_furniture) -> PairConstraintResult:
        """placement 這個候選,在 room 裡、面對 placed_furniture 這些既有目標,
        符不符合家具關聯規則?

        placed_furniture:既有目標清單,元素可為 FixturePlacement / Counter /
        Room / PairTarget(混用皆可,由 type_of/point_of 歸一)。
        """
        src_type = type_of(placement)
        src_pt = point_of(placement)
        src_rot = rotation_of(placement)
        res = PairConstraintResult()

        wsum = 0.0
        acc = 0.0
        for rule in self.rules:
            if rule.source_type != src_type:
                continue

            if rule.relation == CENTER:          # 房間中央,不需要 target
                score = _score_rule(rule, src_pt, src_rot, _room_centroid(room))
            else:
                targets = [t for t in placed_furniture
                           if type_of(t) == rule.target_type]
                if not targets:                  # 沒有 target → 跳過、不扣分
                    continue
                # 多個同類目標時取「最滿足這條規則」的那個
                score = max(_score_rule(rule, src_pt, src_rot, point_of(t))
                            for t in targets)

            res.matched_rules.append(rule.rid)
            res.reasons.append(f"{rule.rid}: {score:.0f}")
            if score < VIOLATION_THRESHOLD:
                res.violations.append(rule.rid)
            acc += score * rule.weight
            wsum += rule.weight

        res.score = acc / wsum if wsum > 0 else 100.0
        return res
