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

from src.design.collision.furniture_constraint import evaluate_constraint
from src.design.collision.furniture_engine import (
    TALL_FIXTURES,
    FurnitureCollisionEngine,
)
from src.design.collision.furniture_pair_constraint import (
    FurniturePairEvaluator,
    PairTarget,
)
from src.design.collision.human_clearance import HumanClearanceEvaluator
from src.design.semantic.room_semantic import RoomSemanticEvaluator
from src.design.collision.geometry import (
    WINDOW_CLEARANCE_MM,
    door_swing_obstacles,
    window_obstacles,
)
from src.design.report import JsonReport
from src.drafting.fixtures import (
    FIXTURE_SIZES,
    Counter,
    FixturePlacement,
    counter_footprint,
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
    """七個軟指標的權重(collision 是硬閘門,不在此列)。

    constraint(Phase 6-4-1)= evaluate_constraint 的**單件**家具偏好(靠牆 /
    朝向 / 前方淨空);pair_constraint(Phase 6-4-2)= 家具**之間**的關聯偏好
    (沙發面向電視、床頭櫃貼床、書桌靠窗…);human_clearance(Phase 6-5)= 人體
    活動空間(開門 / 拉椅 / 上下床 / 通行的活動區夠不夠、有沒有被佔);
    room_semantic(Phase 6-6)= 房間功能語意(這房間該有的家具有沒有、有沒有
    不該出現的)。這些都是**軟分數**,違反只扣分、不淘汰候選——合法與否仍全由
    collision 硬閘門決定。權重欄名與 score key 一致(比照 wall_distance)。
    """

    wall_distance: float = 1.5
    window_distance: float = 1.0
    walkway: float = 1.5
    symmetry: float = 1.0
    room_usability: float = 1.5
    constraint: float = 0.20
    pair_constraint: float = 0.15
    human_clearance: float = 0.20
    room_semantic: float = 0.15

    def as_map(self) -> dict:
        return {"wall_distance": self.wall_distance,
                "window_distance": self.window_distance,
                "walkway": self.walkway, "symmetry": self.symmetry,
                "room_usability": self.room_usability,
                "constraint": self.constraint,
                "pair_constraint": self.pair_constraint,
                "human_clearance": self.human_clearance,
                "room_semantic": self.room_semantic}

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
    constraint_score: float = 0.0
    pair_constraint_score: float = 0.0
    human_clearance_score: float = 0.0
    room_semantic_score: float = 0.0

    @property
    def found(self) -> bool:
        return self.best is not None

    def to_dict(self) -> dict:
        return {
            "name": self.name, "room": self.room,
            "found": self.found,
            "candidates": self.candidates,
            "valid_candidates": self.valid_candidates,
            "constraint_score": round(self.constraint_score, 1),
            "pair_constraint_score": round(self.pair_constraint_score, 1),
            "human_clearance_score": round(self.human_clearance_score, 1),
            "room_semantic_score": round(self.room_semantic_score, 1),
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
        # 家具關聯(Phase 6-4-2):既有家具 + 空間目標(窗/廚房/陽台…)算一次。
        self.pair_evaluator = FurniturePairEvaluator()
        self.pair_targets = self._build_pair_targets(spec)
        # 人體活動空間(Phase 6-5):既有家具當「佔用者」。
        self.human_evaluator = HumanClearanceEvaluator()
        # 房間功能語意(Phase 6-6):看整房家具清單(候選 + 該房既有)。
        self.semantic_evaluator = RoomSemanticEvaluator()
        self._room_fixtures: dict[int, list] = {}

    @staticmethod
    def _build_pair_targets(spec) -> list:
        """把 spec 的既有家具與空間包成 pair 目標:家具原樣;窗以形心、房間
        以形心 + 用途類別(kitchen/balcony/…)包成 PairTarget。"""
        targets: list = [fx for fx in spec.fixtures
                         if isinstance(fx, (FixturePlacement, Counter))]
        for o in window_obstacles(spec):
            c = o.poly.centroid
            targets.append(PairTarget("window", (c.x, c.y)))
        for r in spec.rooms:
            c = Polygon(r.points).centroid
            targets.append(PairTarget(r.kind, (c.x, c.y)))
        return targets

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

    def _score_constraint(self, placement, room) -> float:
        """家具擺放偏好(Phase 6-4)→ 0~100 軟分數。

        ⚠️ **軟分數,不是硬閘門**:違反偏好只扣分,合法與否仍全由 collision
        決定(collision 不通過的候選在 _score 早就被淘汰,根本走不到這裡)。

        重用 evaluate_constraint,把它的三個布林(靠牆 / 朝向 / 前方淨空)換算成
        扣分:滿分 100,背未靠偏好牆 -40、正面朝向不符 -20、前方淨空不足 -40。
        傳入 self.engine 讓「前方淨空被別的家具佔住」也能被扣分。"""
        res = evaluate_constraint(placement, room, engine=self.engine)
        score = 100.0
        if not res.wall_ok:
            score -= 40.0
        if not res.orientation_ok:
            score -= 20.0
        if not res.clearance_ok:
            score -= 40.0
        return _clamp(score)

    def _score_pair(self, placement, room, ignore) -> float:
        """家具關聯偏好(Phase 6-4-2)→ 0~100 軟分數。

        ⚠️ **軟分數,不是硬閘門**:違反只扣分;合法與否仍全由 collision 決定。

        用既有家具 + 空間目標(pair_targets)當 placed_furniture,略過 ignore
        指定的家具(重選自己時不拿自己當關聯對象)。"""
        skip = set()
        if ignore is not None:
            items = ignore if isinstance(ignore, (list, tuple, set)) else [ignore]
            skip = {id(x) for x in items}
        placed = [t for t in self.pair_targets
                  if id(t) not in skip and t is not placement]
        return self.pair_evaluator.evaluate_pair_constraints(
            placement, room, placed).score

    def _score_human(self, placement, room, ignore) -> float:
        """人體活動空間(Phase 6-5)→ 0~100 軟分數。

        ⚠️ **軟分數,不是硬閘門**:活動區缺/被佔只扣分;合法與否仍全由 collision
        決定。既有家具(spec.fixtures)當佔用者,略過 ignore 指定的那些。"""
        skip = set()
        if ignore is not None:
            items = ignore if isinstance(ignore, (list, tuple, set)) else [ignore]
            skip = {id(x) for x in items}
        placed = [f for f in self.spec.fixtures
                  if id(f) not in skip and f is not placement]
        return self.human_evaluator.evaluate_human_clearance(
            placement, room, placed).score

    def _existing_in_room(self, room) -> list:
        """該房間裡既有的家具(形心落在房內);每個房間算一次後快取。"""
        key = id(room)
        cached = self._room_fixtures.get(key)
        if cached is None:
            rp = Polygon(room.points)
            cached = []
            for f in self.spec.fixtures:
                if isinstance(f, Counter):
                    c = Polygon(counter_footprint(f)).centroid
                elif isinstance(f, FixturePlacement):
                    c = Polygon(fixture_footprint(f)).centroid
                else:
                    continue
                if rp.contains(c):
                    cached.append(f)
            self._room_fixtures[key] = cached
        return cached

    def _score_semantic(self, placement, room, ignore) -> float:
        """房間功能語意(Phase 6-6)→ 0~100 軟分數。

        ⚠️ **軟分數,不是硬閘門**:房間該有/不該有的家具只影響分數;合法與否仍全
        由 collision 決定。用「該房既有家具(略過 ignore)+ 這個候選」當整房清單評。"""
        skip = set()
        if ignore is not None:
            items = ignore if isinstance(ignore, (list, tuple, set)) else [ignore]
            skip = {id(x) for x in items}
        placements = [f for f in self._existing_in_room(room)
                      if id(f) not in skip and f is not placement]
        placements.append(placement)
        return self.semantic_evaluator.evaluate_room_semantics(
            room, placements).score

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
            "constraint": self._score_constraint(placement, room),
            "pair_constraint": self._score_pair(placement, room, ignore),
            "human_clearance": self._score_human(placement, room, ignore),
            "room_semantic": self._score_semantic(placement, room, ignore),
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
        if best is not None:
            result.constraint_score = best.scores.get("constraint", 0.0)
            result.pair_constraint_score = best.scores.get("pair_constraint", 0.0)
            result.human_clearance_score = best.scores.get("human_clearance", 0.0)
            result.room_semantic_score = best.scores.get("room_semantic", 0.0)
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
