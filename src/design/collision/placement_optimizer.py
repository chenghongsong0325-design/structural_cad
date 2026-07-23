"""Furniture Placement Optimizer(v0.7 Phase 6-2)—— 幫一件家具挑最佳擺位。

⚠️ **唯讀、不接進生成流程**:產生多個候選擺位、逐一評分、回傳最高分,但**不**
把家具寫進 spec(要不要採用由呼叫端決定)。也不修改任何既有 API——重用 Phase 6-1
的 FurnitureCollisionEngine 當硬性守門,只新增評分與挑選。

流程 generate candidates → score → pick best:

    1. 沿房間四面牆(貼牆家具)或房間中央(中心原點家具)產生候選擺位。
    2. 每個候選先過 collision 硬閘門(FurnitureCollisionEngine.check):
       撞牆/柱/門/梯/天井/擋窗/撞家具 → 直接淘汰,連分都不用評。
    3. 存活的候選在五個軟指標上評分,加權成總分:
         wall_distance   貼牆程度(貼牆家具越貼越好)
         window_distance 不擋窗的程度
         walkway         遠離門迴轉/不擋動線的程度
         symmetry        沿牆置中/對齊的程度
         room_usability  留下的可用地坪(靠邊 → 中央開闊 → 好用)
    4. 回傳總分最高的候選。

⚠️ collision 是**硬指標**(不通過就淘汰),不是軟分數之一——這樣「最佳擺位」
永遠是合法的。五個軟指標是啟發式,用來在合法候選裡挑「比較好」的那個。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from shapely.geometry import Point as SPoint
from shapely.geometry import Polygon

from src.design.collision.furniture_engine import (
    TALL_FIXTURES,
    FurnitureCollisionEngine,
)
from src.design.collision.geometry import (
    WINDOW_CLEARANCE_MM,
    door_swing_obstacles,
    window_obstacles,
)
from src.design.report import JsonReport
from src.drafting.fixtures import (
    FIXTURE_SIZES,
    FixturePlacement,
    fixture_footprint,
)
from src.drafting.fixtures import _CENTER_ORIGIN as CENTER_ORIGIN

# 沿牆佈點的步長(mm)。
CANDIDATE_STEP = 300.0
# 貼牆家具:牆到家具背面這個距離內都算「貼牆」(mm)。
WALL_HUG_MM = 200.0
# 動線淨距的理想值(mm):離門迴轉超過這距離就給滿分。
WALKWAY_IDEAL_MM = 1200.0


@dataclass
class PlacementWeights(JsonReport):
    """五個軟指標的權重(collision 是硬閘門,不在此列)。"""

    wall_distance: float = 1.5
    window_distance: float = 1.0
    walkway: float = 1.5
    symmetry: float = 1.0
    room_usability: float = 1.5

    def as_map(self) -> dict:
        return {"wall_distance": self.wall_distance,
                "window_distance": self.window_distance,
                "walkway": self.walkway, "symmetry": self.symmetry,
                "room_usability": self.room_usability}

    def to_dict(self) -> dict:
        return {k: float(v) for k, v in self.as_map().items()}


@dataclass
class PlacementCandidate(JsonReport):
    """一個候選擺位 + 它的評分。"""

    name: str
    insert: tuple
    rotation: float
    valid: bool = False
    reject_reason: str = ""
    total: float = 0.0
    scores: dict = field(default_factory=dict)

    def placement(self) -> FixturePlacement:
        return FixturePlacement(self.name, self.insert, self.rotation)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "insert": [round(self.insert[0], 1), round(self.insert[1], 1)],
            "rotation": self.rotation,
            "valid": self.valid,
            "reject_reason": self.reject_reason,
            "total": round(self.total, 1),
            "scores": {k: round(v, 1) for k, v in self.scores.items()},
        }


@dataclass
class PlacementResult(JsonReport):
    """一次擺位最佳化的結果。"""

    name: str
    room: str = ""
    best: PlacementCandidate | None = None
    candidates: int = 0
    valid_candidates: int = 0

    @property
    def found(self) -> bool:
        return self.best is not None

    def to_dict(self) -> dict:
        return {
            "name": self.name, "room": self.room,
            "found": self.found,
            "candidates": self.candidates,
            "valid_candidates": self.valid_candidates,
            "best": self.best.to_dict() if self.best else None,
        }

    def summary(self) -> str:
        if not self.found:
            return (f"PlacementResult:{self.name} @ {self.room} → "
                    f"找不到合法擺位(候選 {self.candidates})")
        b = self.best
        return (f"PlacementResult:{self.name} @ {self.room} → "
                f"({b.insert[0]:.0f},{b.insert[1]:.0f}) rot {b.rotation:.0f}° "
                f"總分 {b.total:.1f}(合法候選 {self.valid_candidates}/"
                f"{self.candidates})\n  " +
                " · ".join(f"{k} {v:.0f}" for k, v in b.scores.items()))


def _clamp(v, lo=0.0, hi=100.0):
    return max(lo, min(hi, v))


def _room_rect(room):
    xs = [p[0] for p in room.points]
    ys = [p[1] for p in room.points]
    return min(xs), min(ys), max(xs), max(ys)


class FurniturePlacementOptimizer:
    """幫一件家具在指定房間裡挑最佳擺位。**唯讀**:不寫回 spec。

    障礙與軟指標所需的幾何(門迴轉、窗)在建構時算一次,重用給每次 place()。
    """

    def __init__(self, spec, *, step: float = CANDIDATE_STEP):
        self.spec = spec
        self.step = step
        self.engine = FurnitureCollisionEngine(spec)
        self.doors = door_swing_obstacles(spec)
        self.windows = window_obstacles(spec)

    # ── 產生候選 ──────────────────────────────────────────────────────────
    def candidates(self, name: str, room) -> list[FixturePlacement]:
        """沿房間四面牆(貼牆家具)或中央(中心原點家具)佈點。"""
        w, d = FIXTURE_SIZES.get(name, (600, 600))
        x0, y0, x1, y1 = _room_rect(room)
        out: list[FixturePlacement] = []

        if name in CENTER_ORIGIN:                       # 桌/茶几等:中央 + 幾個偏移
            cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
            for dx in (-self.step, 0.0, self.step):
                for dy in (-self.step, 0.0, self.step):
                    out.append(FixturePlacement(name, (cx + dx, cy + dy), 0.0))
            return out

        # 貼牆家具:原點在貼牆邊、朝 +Y 伸出;各牆的 rotation 對應如下。
        #   南牆 0°(朝北) / 北牆 180° / 東牆 90° / 西牆 270°
        half = w / 2
        span_x = _frange(x0 + half, x1 - half, self.step)
        span_y = _frange(y0 + half, y1 - half, self.step)
        for x in span_x:
            out.append(FixturePlacement(name, (x, y0), 0.0))      # 南牆
            out.append(FixturePlacement(name, (x, y1), 180.0))    # 北牆
        for y in span_y:
            out.append(FixturePlacement(name, (x1, y), 90.0))     # 東牆
            out.append(FixturePlacement(name, (x0, y), 270.0))    # 西牆
        return out

    # ── 評分 ──────────────────────────────────────────────────────────────
    def _score_wall_distance(self, poly, room_poly) -> float:
        """貼牆家具:footprint 到房間邊界越近越好。"""
        d = poly.centroid.distance(room_poly.exterior) \
            if room_poly.contains(poly.centroid) else 0.0
        edge = poly.exterior.distance(room_poly.exterior)
        return _clamp(100.0 * (1.0 - edge / WALL_HUG_MM)) if edge < WALL_HUG_MM \
            else _clamp(100.0 - edge / 50.0)

    def _score_window(self, name, poly) -> float:
        """不擋窗的程度:遮住窗前淨空越少越好(矮家具本就不擋,滿分)。"""
        if name not in TALL_FIXTURES or not self.windows:
            return 100.0
        worst = 0.0
        for o in self.windows:
            z = o.poly.area
            if z > 0:
                worst = max(worst, poly.intersection(o.poly).area / z)
        return _clamp(100.0 * (1.0 - worst))

    def _score_walkway(self, poly) -> float:
        """遠離門迴轉 = 不擋動線。離最近門迴轉越遠越好(到理想值封頂)。"""
        if not self.doors:
            return 100.0
        d = min(poly.distance(o.poly) for o in self.doors)
        return _clamp(100.0 * d / WALKWAY_IDEAL_MM)

    def _score_symmetry(self, poly, room, rotation) -> float:
        """沿牆置中的程度:家具中心投影到牆軸,離牆中點越近越對稱。"""
        x0, y0, x1, y1 = _room_rect(room)
        c = poly.centroid
        if rotation in (0.0, 180.0):                    # 貼南/北牆,沿 X 對齊
            mid, half = (x0 + x1) / 2, (x1 - x0) / 2
            off = abs(c.x - mid)
        else:                                           # 貼東/西牆,沿 Y 對齊
            mid, half = (y0 + y1) / 2, (y1 - y0) / 2
            off = abs(c.y - mid)
        return _clamp(100.0 * (1.0 - off / half)) if half > 0 else 100.0

    def _score_usability(self, poly, room_poly) -> float:
        """留下的可用地坪:家具越靠房間外緣、中央越開闊 = 越好用。

        用「家具形心到房間形心的距離 / 房間半徑」衡量靠邊程度。"""
        rc = room_poly.centroid
        fc = poly.centroid
        reach = math.sqrt(room_poly.area / math.pi)     # 房間等效半徑
        if reach <= 0:
            return 100.0
        return _clamp(100.0 * fc.distance(rc) / reach)

    def _score(self, placement, room, room_poly, ignore) -> PlacementCandidate:
        cand = PlacementCandidate(placement.name, placement.insert,
                                  placement.rotation)
        res = self.engine.check(placement, ignore=ignore)
        if not res.valid:                               # collision 硬閘門
            cand.valid = False
            cand.reject_reason = res.reason
            return cand
        poly = Polygon(fixture_footprint(placement))
        cand.valid = True
        cand.scores = {
            "wall_distance": self._score_wall_distance(poly, room_poly),
            "window_distance": self._score_window(placement.name, poly),
            "walkway": self._score_walkway(poly),
            "symmetry": self._score_symmetry(poly, room, placement.rotation),
            "room_usability": self._score_usability(poly, room_poly),
        }
        return cand

    # ── 挑最佳 ────────────────────────────────────────────────────────────
    def place(self, name: str, room, *, weights: PlacementWeights | None = None,
              ignore=None) -> PlacementResult:
        """在 room 裡幫 name 挑最佳擺位。room 可給 Room 物件或房間 index。

        ignore:評 collision 時要略過的既有家具(例如「這件家具原本就在這房」,
        重新選位時避免自己撞自己)。不寫回 spec。
        """
        if isinstance(room, int):
            room = self.spec.rooms[room]
        room_poly = Polygon(room.points)
        w = weights or PlacementWeights()
        wmap = w.as_map()
        wsum = sum(wmap.values()) or 1.0

        result = PlacementResult(name=name, room=room.name)
        best = None
        for placement in self.candidates(name, room):
            result.candidates += 1
            cand = self._score(placement, room, room_poly, ignore)
            if not cand.valid:
                continue
            result.valid_candidates += 1
            cand.total = sum(cand.scores[k] * wmap[k] for k in wmap) / wsum
            if best is None or cand.total > best.total:
                best = cand
        result.best = best
        return result


def _frange(lo: float, hi: float, step: float) -> list[float]:
    """含端點的等距取樣;lo > hi 時回中點,確保至少一個候選。"""
    if hi <= lo:
        return [(lo + hi) / 2]
    n = int((hi - lo) / step)
    pts = [lo + i * step for i in range(n + 1)]
    if pts[-1] < hi - 1e-6:
        pts.append(hi)
    return pts
