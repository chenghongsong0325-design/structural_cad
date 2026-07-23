"""Walkway Analyzer(v0.7 Phase 6-3)—— 分析走道的淨寬、主次與是否被擋。

⚠️ **唯讀**:只分析,不改 spec、不碰 Generator。

與 Phase 5-3 Corridor Analyzer 的差別:
  * corridor.py 用**最小外接矩形短邊**當寬度——直線走道準,**L 形會被高估**
    (實測 19 個走道有 10 個是 L 形)。
  * 本模組用**掃描線量真正的最小淨寬**(沿走道逐段量橫斷面),L 形也準,而且
    會扣掉侵入走道的障礙(家具 footprint、門迴轉),算出「被擋之後還剩多寬」。

分析:
  * 主走道 / 次走道 —— 依「服務幾個房間」排序,最會分流的是主走道。
  * 最小淨寬 —— 所有走道裡最窄的淨寬。

回傳 WalkwayReport,每段走道帶:width(淨寬)/ blocked(是否被擋窄到不足)/
length(走道長度)。

⚠️ 走道 = kind=="corridor" 的房間。小宅把動線融入客廳、沒有獨立走道
(實測 81/100 層無走道),那種樓層回空的 WalkwayReport(沒有走道可分析)。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from shapely.geometry import LineString, Polygon

from src.design.collision.geometry import door_swing_obstacles, fixture_obstacles
from src.design.connectivity import build_graphs
from src.design.report import JsonReport

# 走道淨寬下限(mm)。低於此值視為被擋(通過困難)。
# 實測走道最小淨寬約 1200mm;取 750(單人可過的下限)→ 現況零誤判,又擋得住
# 真正被家具塞窄的走道。
MIN_WALKWAY_WIDTH = 750.0
# 掃描線步長(mm)。
SCAN_STEP = 100.0
# 障礙侵入面積門檻(mm²):超過才算真的侵入走道(濾邊緣觸碰)。
INTRUDE_TOL = 100.0


@dataclass
class WalkwaySegment(JsonReport):
    """一段走道的量測。"""

    name: str
    role: str                       # "main"(主走道)/ "secondary"(次走道)
    width: float                    # 最小淨寬(扣障礙後,mm)
    raw_width: float                # 幾何最小寬(未扣障礙,mm)
    length: float                   # 走道長度(mm)
    serves: int                     # 直接連通的房間數
    blocked: bool = False
    block_reason: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name, "role": self.role,
            "width": round(self.width, 1),
            "raw_width": round(self.raw_width, 1),
            "length": round(self.length, 1),
            "serves": self.serves,
            "blocked": self.blocked,
            "block_reason": self.block_reason,
        }


@dataclass
class WalkwayReport(JsonReport):
    """一層樓的走道分析結果(唯讀產物)。"""

    walkways: list = field(default_factory=list)     # list[WalkwaySegment]

    @property
    def has_walkway(self) -> bool:
        return bool(self.walkways)

    @property
    def main(self) -> WalkwaySegment | None:
        return next((w for w in self.walkways if w.role == "main"), None)

    @property
    def secondary(self) -> list:
        return [w for w in self.walkways if w.role == "secondary"]

    @property
    def min_width(self) -> float | None:
        return min((w.width for w in self.walkways), default=None)

    @property
    def blocked(self) -> list:
        return [w for w in self.walkways if w.blocked]

    @property
    def ok(self) -> bool:
        """沒有走道被擋窄就算通過。"""
        return not self.blocked

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "has_walkway": self.has_walkway,
            "min_width": (round(self.min_width, 1)
                          if self.min_width is not None else None),
            "count": len(self.walkways),
            "walkways": [w.to_dict() for w in self.walkways],
        }

    def summary(self) -> str:
        if not self.has_walkway:
            return "WalkwayReport:此層無獨立走道(動線融入客廳)"
        mw = f"{self.min_width:.0f}mm" if self.min_width is not None else "—"
        head = (f"WalkwayReport:走道 {len(self.walkways)} 段(最窄淨寬 {mw})→ "
                f"{'PASS' if self.ok else 'FAIL'}")
        lines = [head]
        for w in self.walkways:
            tag = "主走道" if w.role == "main" else "次走道"
            b = f" ⚠️ 被擋({w.block_reason})" if w.blocked else ""
            lines.append(f"  {tag} {w.name}:淨寬 {w.width:.0f} × 長 "
                         f"{w.length:.0f}mm,服務 {w.serves} 房{b}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# 掃描線量最小淨寬
# ---------------------------------------------------------------------------
def _is_axis_aligned(poly: Polygon) -> bool:
    pts = list(poly.exterior.coords)
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if abs(x0 - x1) > 1e-6 and abs(y0 - y1) > 1e-6:
            return False
    return True


def _scan_min_width(free, bbox, step: float = SCAN_STEP) -> float:
    """沿 x、y 兩個方向掃橫斷面,回最小的斷面長度(= 最小淨寬)。

    走道是軸對齊矩形/L 形:橫向掃描線切過某條「臂」時,得到的線段長度就是該臂
    的寬;沿臂方向的線段則很長(不影響最小值)。障礙已從 free 扣除,故被塞窄
    的斷面會直接反映成較短的線段。"""
    minx, miny, maxx, maxy = bbox
    best = float("inf")
    # 水平掃描線(固定 y)→ 量到「垂直臂」的寬
    y = miny + step / 2
    while y < maxy:
        seg = free.intersection(LineString([(minx - 1, y), (maxx + 1, y)]))
        best = min(best, _shortest_part(seg))
        y += step
    # 垂直掃描線(固定 x)→ 量到「水平臂」的寬
    x = minx + step / 2
    while x < maxx:
        seg = free.intersection(LineString([(x, miny - 1), (x, maxy + 1)]))
        best = min(best, _shortest_part(seg))
        x += step
    return 0.0 if best == float("inf") else best


def _shortest_part(geom) -> float:
    """一條掃描線 ∩ 區域可能是多段;回最短那段的長度(空的回 +inf,不影響 min)。

    ⚠️ 只在該掃描位置**有**線段時才計入——完全在區域外的掃描線回 +inf。"""
    if geom.is_empty:
        return float("inf")
    if geom.geom_type == "LineString":
        return geom.length
    if geom.geom_type in ("MultiLineString", "GeometryCollection"):
        lens = [g.length for g in geom.geoms
                if g.geom_type == "LineString" and g.length > 0]
        return min(lens) if lens else float("inf")
    return float("inf")


# ---------------------------------------------------------------------------
# Analyzer
# ---------------------------------------------------------------------------
def analyze_walkways(spec) -> WalkwayReport:
    """分析走道,回一份 WalkwayReport。**唯讀**,不改 spec。"""
    g = build_graphs(spec)
    corr_idx = [i for i, k in enumerate(g.kinds) if k == "corridor"]
    if not corr_idx:
        return WalkwayReport()

    obstacles = [o for o in fixture_obstacles(spec)] + door_swing_obstacles(spec)

    segs = []
    for i in corr_idx:
        poly = Polygon(spec.rooms[i].points)
        bbox = poly.bounds
        # 幾何最小寬(未扣障礙)
        if _is_axis_aligned(poly):
            raw = _scan_min_width(poly, bbox)
        else:
            raw = _min_rect_short(poly)              # 斜走道退回外接矩形短邊
        # 扣掉侵入走道的障礙後的淨寬
        intruders = [o.poly for o in obstacles
                     if o.poly.intersection(poly).area > INTRUDE_TOL]
        if intruders and _is_axis_aligned(poly):
            free = poly
            for ob in intruders:
                free = free.difference(ob)
            clear = _scan_min_width(free, bbox)
        else:
            clear = raw
        length = poly.area / raw if raw > 0 else 0.0
        serves = len(g.room_graph[i])
        blocked = clear < MIN_WALKWAY_WIDTH
        reason = ""
        if blocked:
            reason = (f"障礙塞窄至 {clear:.0f}mm" if intruders
                      else f"走道本身僅 {clear:.0f}mm")
        segs.append((serves, WalkwaySegment(
            name=spec.rooms[i].name, role="secondary",
            width=clear, raw_width=raw, length=length, serves=serves,
            blocked=blocked, block_reason=reason)))

    # 服務最多房間的是主走道,其餘為次走道
    segs.sort(key=lambda s: s[0], reverse=True)
    if segs:
        segs[0][1].role = "main"
    return WalkwayReport(walkways=[s for _, s in segs])


def _min_rect_short(poly: Polygon) -> float:
    rect = poly.minimum_rotated_rectangle
    if not hasattr(rect, "exterior"):
        return 0.0
    pts = list(rect.exterior.coords)[:4]
    if len(pts) < 4:
        return 0.0
    import math
    a = math.dist(pts[0], pts[1])
    b = math.dist(pts[1], pts[2])
    return min(a, b)
