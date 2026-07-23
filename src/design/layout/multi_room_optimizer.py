"""Multi-room Optimization(v0.7 Phase 6-8)—— 從「單房挑家具擺位」擴展到「整棟

依序最佳化」。Phase 6-2 的 FurniturePlacementOptimizer 一次只幫一件家具挑位;本
模組把它套到整棟:依功能優先序逐房、逐件重新擺放,全部做完再用 Phase 6-7 的
LayoutScoreEngine 打整棟總分。

房間處理順序(功能優先序):

    bedroom → bathroom → kitchen → dining → living → study → laundry → balcony

每個房間裡的每件家具,都用 FurniturePlacementOptimizer 在「當下的佈局」上重挑
最佳位(綜合 collision 硬閘門 + walkway/human_clearance/constraint/pair_constraint/
room_semantic 等軟分數)。前面房間排好的家具,會成為後面房間最佳化時看到的
障礙與關聯目標(貪婪、逐件更新)。

⚠️ **唯讀 w.r.t. 輸入**:在輸入 spec 的**深拷貝**上作業,原 spec 一個位元都不動;
也**不接進生成流程**,故對 DXF/PNG/Benchmark 生成零影響。合法與否永遠由
FurnitureCollisionEngine(硬閘門)決定——最佳化只在合法位置裡挑,不會產生
非法佈局(挑不到合法位就保留原位)。

⚠️ 流理台(Counter)是參數式、非固定圖塊,optimizer 無法重擺,**保留原位**。

典型用法::

    res = MultiRoomOptimizer(spec).optimize()
    res.overall_score      # 整棟總分(Phase 6-7)
    res.grade              # A+ ~ D
    res.room_scores        # 每房的擺放/語意分數
    res.spec               # 最佳化後的深拷貝(要畫圖自取,不影響原 spec)
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field

from shapely.geometry import Polygon

from src.design.collision.placement_optimizer import (
    FurniturePlacementOptimizer,
    PlacementWeights,
)
from src.design.layout.global_score import (
    LayoutScore,
    LayoutScoreEngine,
)
from src.design.report import JsonReport
from src.design.semantic.room_semantic import (
    RoomSemanticEvaluator,
    canonical_room,
    get_room_rule,
)
from src.drafting.fixtures import (
    Counter,
    FixturePlacement,
    counter_footprint,
    fixture_footprint,
)

# 房間處理優先序(canonical kind → 序);未列者排在最後、按出現順序。
ROOM_ORDER = (
    "bedroom", "bathroom", "kitchen", "dining", "living", "study",
    "laundry", "balcony",
)
_ORDER_INDEX = {k: i for i, k in enumerate(ROOM_ORDER)}


def _priority(kind: str) -> int:
    return _ORDER_INDEX.get(canonical_room(kind), len(ROOM_ORDER))


@dataclass
class RoomScore(JsonReport):
    """單一房間最佳化後的成績。"""

    room: str
    kind: str
    furniture_count: int = 0
    replaced: int = 0                                # 成功重擺的件數
    avg_placement: float = 100.0                     # 重擺件的 best.total 平均
    semantic: float = 100.0                          # 房間語意分數(最終佈局)

    def to_dict(self) -> dict:
        return {
            "room": self.room,
            "kind": self.kind,
            "furniture_count": self.furniture_count,
            "replaced": self.replaced,
            "avg_placement": round(self.avg_placement, 1),
            "semantic": round(self.semantic, 1),
        }


@dataclass
class MultiRoomResult(JsonReport):
    """整棟依序最佳化的結果。"""

    overall_score: float = 0.0
    grade: str = "D"
    room_scores: list = field(default_factory=list)  # list[RoomScore]
    processed_rooms: list = field(default_factory=list)  # 依處理順序的房名
    global_score: LayoutScore | None = None
    spec: object = None                              # 最佳化後的深拷貝

    def to_dict(self) -> dict:
        return {
            "overall_score": round(self.overall_score, 1),
            "grade": self.grade,
            "processed_rooms": list(self.processed_rooms),
            "room_scores": [r.to_dict() for r in self.room_scores],
            "global_score": self.global_score.to_dict() if self.global_score
            else None,
        }

    def summary(self) -> str:
        head = (f"MultiRoomResult:整棟 {self.overall_score:.1f} 分 "
                f"[{self.grade}] · {len(self.room_scores)} 房")
        lines = [head]
        for rs in self.room_scores:
            lines.append(f"  {rs.room}({rs.kind}):重擺 {rs.replaced}/"
                         f"{rs.furniture_count} · 擺位 {rs.avg_placement:.0f} · "
                         f"語意 {rs.semantic:.0f}")
        return "\n".join(lines)

    def __str__(self) -> str:
        return self.summary()


class MultiRoomOptimizer:
    """整棟住宅的逐房逐件家具最佳化。**唯讀 w.r.t. 輸入**:在深拷貝上作業。"""

    def __init__(self, spec, *,
                 weights: PlacementWeights | None = None,
                 layout_weights: dict | None = None):
        self.source = spec
        self.spec = copy.deepcopy(spec)              # 全程在拷貝上動,原 spec 不變
        self.weights = weights or PlacementWeights()
        self.layout_engine = LayoutScoreEngine(layout_weights)
        self.semantic_eval = RoomSemanticEvaluator()

    # ── 主流程 ────────────────────────────────────────────────────────────
    def optimize(self) -> MultiRoomResult:
        result = MultiRoomResult()
        rooms = sorted(enumerate(self.spec.rooms),
                       key=lambda iv: (_priority(iv[1].kind), iv[0]))
        for _, room in rooms:
            if get_room_rule(room.kind) is None:     # 沒有語意規則的房間不最佳化
                continue
            rs = self._optimize_room(room)
            result.room_scores.append(rs)
            result.processed_rooms.append(room.name)

        gs = self.layout_engine.score(self.spec, name="optimized")
        result.global_score = gs
        result.overall_score = gs.overall_score
        result.grade = gs.grade
        result.spec = self.spec
        return result

    # ── 單房最佳化 ────────────────────────────────────────────────────────
    def _in_room(self, room, poly):
        """房內的所有家具(FixturePlacement + Counter,以形心判定)。"""
        out = []
        for f in self.spec.fixtures:
            if isinstance(f, FixturePlacement):
                c = Polygon(fixture_footprint(f)).centroid
            elif isinstance(f, Counter):
                c = Polygon(counter_footprint(f)).centroid
            else:
                continue
            if poly.contains(c):
                out.append(f)
        return out

    def _optimize_room(self, room) -> RoomScore:
        poly = Polygon(room.points)
        contents = self._in_room(room, poly)
        # 只有固定圖塊(FixturePlacement)能重擺;流理台(Counter)保留原位。
        pieces = [f for f in contents if isinstance(f, FixturePlacement)]
        rs = RoomScore(room=room.name, kind=canonical_room(room.kind),
                       furniture_count=len(contents))

        totals = []
        for piece in pieces:
            # 每件都在「當下佈局」上重建 optimizer(前面排好的家具會被看見)。
            opt = FurniturePlacementOptimizer(self.spec)
            res = opt.place(piece.name, room, weights=self.weights, ignore=piece)
            if res.found:
                piece.insert = res.best.insert
                piece.rotation = res.best.rotation
                rs.replaced += 1
                totals.append(res.best.total)

        rs.avg_placement = sum(totals) / len(totals) if totals else 100.0
        # 語意用「房內全部家具」(含流理台)——重擺後重新收一次。
        rs.semantic = self.semantic_eval.evaluate_room_semantics(
            room, self._in_room(room, poly)).score
        return rs
