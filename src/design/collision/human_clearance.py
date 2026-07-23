"""Human Clearance(v0.7 Phase 6-5)—— 人體活動空間模擬。

碰撞引擎只保證「家具沒有互相重疊」,但「沒撞到」不等於「用得了」:衣櫃貼牆
沒撞到任何東西,可是門前只有 300mm,人根本打不開門;書桌塞進角落沒撞到,
但椅子拉不出來。本模組替每種家具劃出**使用時人體要占的活動區**(開門、拉椅、
起身、通行),再看那塊區域是不是:

    1. 有一部分根本落在房間外(牆把活動空間吃掉了)→ 缺人體空間,扣分。
    2. 被別的家具佔住(活動區重疊到其他家具)→ 活動被擋,扣分。

⚠️ **獨立模組、唯讀、純軟分數**(比照整個 Phase 6):只回 0~100 分,**不**改
spec、**不**能讓非法位置變合法。合法與否永遠由 FurnitureCollisionEngine(硬閘門)
決定;本模組只在「所有 collision 合法的位置」之間,偏好挑「人真的用得了」的那個。

活動區沿用 fixtures.py 的座標約定:貼牆家具原點=貼牆邊中點、朝局部 +Y 伸出
深度 d;故 front=局部 +Y 側、back=局部 -Y 側、side=局部 ±X 兩側。中心原點家具
(餐桌等)front/back 沿 ±Y、side 沿 ±X(四面)。

一條規則 = HumanClearanceRule(source_type, front/side/back_clearance mm)。type 是
canonical 類別(bed_double→bed、basin→sink、Counter→kitchen_counter…見 HUMAN_TYPE)。

典型用法::

    ev = HumanClearanceEvaluator()
    res = ev.evaluate_human_clearance(candidate, room, placed_furniture)
    res.score            # 0~100
    res.blocked_regions  # 被擋/缺空間的活動區(front/back/left/right)

⚠️ 各家具的淨空值為常見人因做法,非任何規範——見模組結尾 PENDING。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from shapely.geometry import Polygon

from src.design.report import JsonReport
from src.drafting.fixtures import (
    FIXTURE_SIZES,
    Counter,
    FixturePlacement,
    counter_footprint,
    fixture_footprint,
)
from src.drafting.fixtures import _CENTER_ORIGIN as CENTER_ORIGIN

# 活動區被吃掉多少比例才算「這塊被擋」/「違反」。
BLOCK_THRESHOLD = 0.25       # 進 blocked_regions
VIOLATION_THRESHOLD = 0.50   # 進 violations(過半被擋)

# 圖塊名 → 人體淨空規則用的 canonical 類別。未列者用原名(fallback)。
HUMAN_TYPE = {
    "bed_single": "bed", "bed_double": "bed",
    "wardrobe": "wardrobe",
    "desk": "desk",
    "sofa3": "sofa", "armchair": "sofa",
    "table4": "dining_table",
    "toilet": "toilet",
    "bathtub": "shower",
    "basin": "sink",
    "fridge": "fridge",
}


def human_type(obj) -> str:
    """一個家具的人體淨空 canonical 類別。"""
    if isinstance(obj, Counter):
        return "kitchen_counter"
    if isinstance(obj, FixturePlacement):
        return HUMAN_TYPE.get(obj.name, obj.name)
    if hasattr(obj, "type"):                     # PairTarget 之類
        return obj.type
    if hasattr(obj, "name"):
        return HUMAN_TYPE.get(obj.name, obj.name)
    return ""


@dataclass
class HumanClearanceRule(JsonReport):
    """一種家具的人體活動淨空(mm)。0 = 該側不需要活動區。

    front_clearance  正面(局部 +Y)要留的活動深度:開門 / 拉椅 / 起身 / 通行。
    side_clearance   兩側(局部 ±X)各要留的活動寬度:上下床 / 側身通過。
    back_clearance   背面(局部 -Y)要留的活動深度(少數家具需要)。
    """

    source_type: str
    front_clearance: float = 0.0
    side_clearance: float = 0.0
    back_clearance: float = 0.0

    @property
    def needs_clearance(self) -> bool:
        return (self.front_clearance > 0 or self.side_clearance > 0
                or self.back_clearance > 0)

    def to_dict(self) -> dict:
        return {
            "source_type": self.source_type,
            "front_clearance": float(self.front_clearance),
            "side_clearance": float(self.side_clearance),
            "back_clearance": float(self.back_clearance),
        }


# 預設規則(常見人因值,非規範;見 PENDING)。
HUMAN_CLEARANCE_RULES: dict[str, HumanClearanceRule] = {
    "bed": HumanClearanceRule("bed", front_clearance=500.0, side_clearance=600.0),
    "wardrobe": HumanClearanceRule("wardrobe", front_clearance=900.0),
    "desk": HumanClearanceRule("desk", front_clearance=750.0),      # 椅子拉開
    "chair": HumanClearanceRule("chair", front_clearance=450.0),
    "sofa": HumanClearanceRule("sofa", front_clearance=900.0),
    "dining_table": HumanClearanceRule("dining_table", front_clearance=900.0,
                                       side_clearance=900.0,
                                       back_clearance=900.0),         # 四面坐人
    "toilet": HumanClearanceRule("toilet", front_clearance=600.0,
                                 side_clearance=200.0),
    "shower": HumanClearanceRule("shower", front_clearance=600.0),
    "sink": HumanClearanceRule("sink", front_clearance=600.0),
    "washer": HumanClearanceRule("washer", front_clearance=600.0),   # 開門取衣
    "fridge": HumanClearanceRule("fridge", front_clearance=900.0),   # 開門+站立
    "kitchen_counter": HumanClearanceRule("kitchen_counter",
                                          front_clearance=1000.0),    # 工作走道
}


def get_rule(source_type: str) -> HumanClearanceRule | None:
    return HUMAN_CLEARANCE_RULES.get(source_type)


# ── 幾何:活動區 / 佔地 ────────────────────────────────────────────────────
def _rect(x0, y0, x1, y1):
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


def _transform(local, rotation, insert):
    a = math.radians(rotation)
    ca, sa = math.cos(a), math.sin(a)
    ix, iy = insert
    return [(ix + x * ca - y * sa, iy + x * sa + y * ca) for x, y in local]


def clearance_regions(placement, rule: HumanClearanceRule) -> list[tuple[str, Polygon]]:
    """一件家具依它的規則,要保持淨空的各塊活動區(世界座標多邊形)。"""
    if isinstance(placement, Counter):
        return _counter_regions(placement, rule)
    name = placement.name
    if name not in FIXTURE_SIZES:
        return []
    w, d = FIXTURE_SIZES[name]
    hw = w / 2
    if name in CENTER_ORIGIN:                    # 原點=中心:±d/2 / ±w/2
        yf0, yf1 = d / 2, d / 2 + rule.front_clearance
        yb0, yb1 = -d / 2 - rule.back_clearance, -d / 2
        ys0, ys1 = -d / 2, d / 2
    else:                                        # 原點=貼牆邊,朝 +Y 伸 d
        yf0, yf1 = d, d + rule.front_clearance
        yb0, yb1 = -rule.back_clearance, 0.0
        ys0, ys1 = 0.0, d
    out: list[tuple[str, list]] = []
    if rule.front_clearance > 0:
        out.append(("front", _rect(-hw, yf0, hw, yf1)))
    if rule.back_clearance > 0:
        out.append(("back", _rect(-hw, yb0, hw, yb1)))
    if rule.side_clearance > 0:
        s = rule.side_clearance
        out.append(("left", _rect(-hw - s, ys0, -hw, ys1)))
        out.append(("right", _rect(hw, ys0, hw + s, ys1)))
    return [(nm, Polygon(_transform(loc, placement.rotation, placement.insert)))
            for nm, loc in out]


def _counter_regions(counter: Counter, rule: HumanClearanceRule):
    """流理台的活動區:走道在檯面「深度反向」那側(人站的地方)。"""
    if rule.front_clearance <= 0:
        return []
    (x1, y1), (x2, y2) = counter.start, counter.end
    length = counter.length
    ux, uy = (x2 - x1) / length, (y2 - y1) / length
    nx, ny = -uy, ux                             # 左手側 = 檯面深度方向
    ax, ay = uy, -ux                             # 走道側 = 深度反向
    c = rule.front_clearance
    poly = Polygon([(x1, y1), (x2, y2),
                    (x2 + ax * c, y2 + ay * c), (x1 + ax * c, y1 + ay * c)])
    return [("front", poly)]


def _footprint_poly(obj):
    """一件既有家具的佔地多邊形(算「活動區被佔住」用);非家具回 None。"""
    if isinstance(obj, Counter):
        return Polygon(counter_footprint(obj))
    if isinstance(obj, FixturePlacement):
        return Polygon(fixture_footprint(obj))
    return None


# ── 查詢結果 ────────────────────────────────────────────────────────────────
@dataclass
class HumanClearanceResult(JsonReport):
    """一次人體活動空間評分的結果。

    score           0~100(各活動區被擋比例的平均;沒有活動區 → 100,不扣分)。
    violations      過半被擋的活動區說明(reason 字串)。
    blocked_regions 明顯被擋(≥ BLOCK_THRESHOLD)的活動區名稱(front/back/left/right)。
    reasons         每塊活動區一行文字(供 debug)。
    """

    score: float = 100.0
    violations: list = field(default_factory=list)
    blocked_regions: list = field(default_factory=list)
    reasons: list = field(default_factory=list)

    def __bool__(self) -> bool:
        return not self.violations

    def to_dict(self) -> dict:
        return {
            "score": round(self.score, 1),
            "violations": list(self.violations),
            "blocked_regions": list(self.blocked_regions),
            "reasons": list(self.reasons),
        }

    def __str__(self) -> str:
        if not self.reasons:
            return f"HumanClearance: {self.score:.0f}(無需活動區)"
        return f"HumanClearance: {self.score:.0f} · " + " · ".join(self.reasons)


# ── Evaluator ──────────────────────────────────────────────────────────────
class HumanClearanceEvaluator:
    """對一個候選擺位評「人體活動空間」。**唯讀**:不改任何東西。"""

    def __init__(self, rules: dict[str, HumanClearanceRule] | None = None):
        self.rules = dict(rules) if rules is not None else dict(HUMAN_CLEARANCE_RULES)

    def evaluate_human_clearance(self, placement, room,
                                 placed_furniture) -> HumanClearanceResult:
        """placement 這個候選,在 room 裡、與 placed_furniture 這些既有家具並存,
        人體活動空間夠不夠、有沒有被佔?

        placed_furniture:既有家具清單(FixturePlacement / Counter;其餘略過)。
        """
        res = HumanClearanceResult()
        rule = self.rules.get(human_type(placement))
        if rule is None or not rule.needs_clearance:
            return res                            # 這種家具不需要活動區 → 跳過
        regions = clearance_regions(placement, rule)
        if not regions:
            return res

        room_poly = Polygon(room.points)
        others = []
        for f in placed_furniture:
            if f is placement:
                continue
            fp = _footprint_poly(f)
            if fp is not None and not fp.is_empty:
                others.append(fp)

        zone_scores = []
        for name, zone in regions:
            za = zone.area
            if za <= 0:
                continue
            inside = zone.intersection(room_poly)
            outside_area = za - inside.area       # 落在房間外的部分
            occupied = 0.0
            for o in others:
                occupied += inside.intersection(o).area
            occupied = min(occupied, inside.area)  # 多件重疊時不重複計
            frac = max(0.0, min(1.0, (outside_area + occupied) / za))
            zone_scores.append(100.0 * (1.0 - frac))

            if frac >= BLOCK_THRESHOLD:
                res.blocked_regions.append(name)
            cause = ("缺人體空間" if outside_area >= occupied else "被家具佔住")
            res.reasons.append(f"{name}: 擋 {frac * 100:.0f}%({cause})")
            if frac >= VIOLATION_THRESHOLD:
                res.violations.append(f"{name} 活動區{cause}({frac * 100:.0f}%)")

        res.score = sum(zone_scores) / len(zone_scores) if zone_scores else 100.0
        return res


# =============================================================================
# PENDING(待確認假設彙整)
# =============================================================================
# 1. 各家具的 front/side/back 淨空(mm)為常見人因/居家做法,非任何規範:衣櫃/
#    冰箱開門+站立 900、書桌拉椅 750、沙發前 900、餐桌四面坐人 900、床側上下床
#    600、馬桶前 600、流理台工作走道 1000…待確認。
# 2. 活動區以「矩形」近似:實際開門是扇形、拉椅是局部,矩形略保守但穩定可算。
# 3. 佔用只算 FixturePlacement / Counter 的佔地;門迴轉/柱/窗等由 collision 與
#    其他 Phase 6 模組負責,不在人體淨空的扣分範圍(避免與硬閘門重複判定)。
# 4. kitchen_counter 走道取檯面深度反向側;L 型雙段各自算,交由呼叫端分段傳入。
# =============================================================================
