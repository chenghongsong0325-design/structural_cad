"""Auto Layout Engine(v0.7 Phase 6-9)—— 一鍵把整棟住宅的家具配置做到最好。

這是 Phase 6 的總入口:吃一份已生成的平面(單層 FloorPlanSpec 或整棟 BuildingSpec),
跑完整條家具智慧鏈,輸出「最佳 Layout + 分數」。

    FloorPlanSpec / BuildingSpec
        ↓  (Room Generator 已產出房間與初始家具——本引擎不改生成)
        ↓  Multi-room Optimization(Phase 6-8;內含 collision 硬閘門 +
        ↓    walkway / human_clearance / constraint / pair_constraint /
        ↓    room_semantic 軟分數逐房逐件重擺)
        ↓  Global Score(Phase 6-7;12 項子分數 → overall → grade)
        ↓
    AutoLayoutResult(overall_score / grade / room_layouts / placement_results)

API:
    engine.generate(spec)   單一權重配置跑一次,回 AutoLayoutResult。
    engine.optimize(spec)   試數種擺放權重配置,回 overall 最高的那個。
    engine.export_json()     上一次結果的 JSON。
    engine.export_report()   上一次結果的可讀文字報告。
    engine.benchmark(specs)  多份平面各自 generate,收成 LayoutBenchmark(可排序)。
    engine.top_n(specs, n)   直接回排名前 n 的 LayoutScore。

⚠️ **唯讀 w.r.t. 輸入、不接進生成流程**:MultiRoomOptimizer 在深拷貝上作業,原
spec / building 一個位元不動,對 DXF/PNG/Benchmark 生成零影響。合法與否永遠由
FurnitureCollisionEngine(硬閘門)決定——最佳化只在合法位置挑。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from shapely.geometry import Polygon

from src.design.collision.placement_optimizer import PlacementWeights
from src.design.layout.global_score import (
    SCORE_ITEMS,
    LayoutBenchmark,
    LayoutScore,
    grade_of,
)
from src.design.layout.multi_room_optimizer import MultiRoomOptimizer
from src.design.report import JsonReport
from src.design.semantic.room_semantic import get_room_rule
from src.drafting.fixtures import (
    Counter,
    FixturePlacement,
    counter_footprint,
    fixture_footprint,
)

# optimize() 試的擺放權重配置(啟發式搜尋:挑 overall 最高者)。
WEIGHT_PROFILES: dict[str, PlacementWeights] = {
    "balanced": PlacementWeights(),
    "clearance": PlacementWeights(human_clearance=0.6, walkway=2.0),
    "semantic": PlacementWeights(room_semantic=0.5, constraint=0.4,
                                 pair_constraint=0.4),
}


def _is_building(spec) -> bool:
    return hasattr(spec, "floors") and isinstance(getattr(spec, "floors"), list)


def _is_floor(spec) -> bool:
    return hasattr(spec, "rooms") and hasattr(spec, "fixtures")


# ── 結果模型 ────────────────────────────────────────────────────────────────
@dataclass
class AutoLayoutResult(JsonReport):
    """一次自動配置的結果。"""

    name: str = ""
    overall_score: float = 0.0
    grade: str = "D"
    room_layouts: dict = field(default_factory=dict)     # room → [furniture…]
    placement_results: list = field(default_factory=list)  # list[RoomScore]
    floor_scores: list = field(default_factory=list)     # [{label,overall,grade}]
    global_scores: list = field(default_factory=list)    # list[LayoutScore]
    layout_score: LayoutScore | None = None              # 整體代表分(可餵 benchmark)
    spec: object = None                                  # 最佳化後的深拷貝

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "overall_score": round(self.overall_score, 1),
            "grade": self.grade,
            "floor_scores": list(self.floor_scores),
            "room_count": len(self.room_layouts),
            "room_layouts": self.room_layouts,
            "placement_results": [r.to_dict() for r in self.placement_results],
            "global_scores": [g.to_dict() for g in self.global_scores],
        }

    def summary(self) -> str:
        head = (f"AutoLayoutResult:{self.name or '(未命名)'} → "
                f"{self.overall_score:.1f} 分 [{self.grade}] · "
                f"{len(self.room_layouts)} 房 · {len(self.floor_scores)} 層")
        lines = [head]
        for fs in self.floor_scores:
            lines.append(f"  [{fs['label']}] {fs['overall']:.1f} [{fs['grade']}]")
        for rs in self.placement_results:
            lines.append(f"    {rs.room}({rs.kind}):重擺 {rs.replaced}/"
                         f"{rs.furniture_count} · 語意 {rs.semantic:.0f}")
        return "\n".join(lines)

    def __str__(self) -> str:
        return self.summary()


# ── 引擎 ────────────────────────────────────────────────────────────────────
class AutoLayoutEngine:
    """整棟住宅自動家具配置。**唯讀 w.r.t. 輸入**。"""

    def __init__(self, *, weights: PlacementWeights | None = None,
                 layout_weights: dict | None = None):
        self.weights = weights or PlacementWeights()
        self.layout_weights = layout_weights
        self._last: AutoLayoutResult | None = None

    # ── 產生 / 最佳化 ─────────────────────────────────────────────────────
    def generate(self, spec, *, name: str = "") -> AutoLayoutResult:
        """單一權重配置跑一次完整鏈,回 AutoLayoutResult。"""
        if _is_building(spec):
            res = self._generate_building(spec, name=name)
        elif _is_floor(spec):
            res = self._generate_floor(spec, name=name)
        else:
            raise TypeError("spec 必須是 FloorPlanSpec 或 BuildingSpec")
        self._last = res
        return res

    def optimize(self, spec, *, name: str = "") -> AutoLayoutResult:
        """試數種擺放權重配置,回 overall_score 最高的那個(啟發式搜尋)。"""
        best: AutoLayoutResult | None = None
        for pname, w in WEIGHT_PROFILES.items():
            eng = AutoLayoutEngine(weights=w, layout_weights=self.layout_weights)
            res = eng.generate(spec, name=name)
            if best is None or res.overall_score > best.overall_score:
                best = res
        self._last = best
        return best

    # ── 單層 ──────────────────────────────────────────────────────────────
    def _generate_floor(self, floor_spec, *, name: str = "",
                        label: str = "1F") -> AutoLayoutResult:
        mr = MultiRoomOptimizer(floor_spec, weights=self.weights,
                                layout_weights=self.layout_weights).optimize()
        gs = mr.global_score
        res = AutoLayoutResult(
            name=name,
            overall_score=mr.overall_score,
            grade=mr.grade,
            room_layouts=_room_layouts(mr.spec),
            placement_results=list(mr.room_scores),
            floor_scores=[{"label": getattr(floor_spec, "floor_label", "") or label,
                           "overall": round(mr.overall_score, 1),
                           "grade": mr.grade}],
            global_scores=[gs],
            layout_score=LayoutScore(name=name, overall_score=gs.overall_score,
                                     grade=gs.grade,
                                     sub_scores=dict(gs.sub_scores)),
            spec=mr.spec,
        )
        return res

    # ── 整棟 ──────────────────────────────────────────────────────────────
    def _generate_building(self, building, *, name: str = "") -> AutoLayoutResult:
        floor_scores = []
        placement = []
        room_layouts: dict = {}
        global_scores = []
        opt_floors = []
        for fl in building.floors:
            mr = MultiRoomOptimizer(fl.spec, weights=self.weights,
                                    layout_weights=self.layout_weights).optimize()
            floor_scores.append({"label": fl.label,
                                 "overall": round(mr.overall_score, 1),
                                 "grade": mr.grade,
                                 "furnished_rooms": len(mr.processed_rooms)})
            placement.extend(mr.room_scores)
            global_scores.append(mr.global_score)
            room_layouts.update(_room_layouts(mr.spec, prefix=f"{fl.label}/"))
            opt_floors.append((fl, mr.spec))

        overall, sub = _aggregate(floor_scores, global_scores)
        res = AutoLayoutResult(
            name=name, overall_score=overall, grade=grade_of(overall),
            room_layouts=room_layouts, placement_results=placement,
            floor_scores=floor_scores, global_scores=global_scores,
            layout_score=LayoutScore(name=name, overall_score=overall,
                                     grade=grade_of(overall), sub_scores=sub),
            spec=_rebuild_building(building, opt_floors),
        )
        return res

    # ── 匯出 ──────────────────────────────────────────────────────────────
    def export_json(self) -> str:
        """上一次 generate/optimize 結果的 JSON 字串。"""
        return self._require_last().to_json()

    def export_report(self) -> str:
        """上一次結果的可讀文字報告。"""
        return self._require_last().summary()

    def _require_last(self) -> AutoLayoutResult:
        if self._last is None:
            raise RuntimeError("尚無結果,請先呼叫 generate() 或 optimize()")
        return self._last

    # ── 多方案比較 ────────────────────────────────────────────────────────
    def benchmark(self, specs) -> LayoutBenchmark:
        """多份平面各自 generate,收成 LayoutBenchmark(可排序/平均/JSON/CSV)。

        specs 可為 {name: spec} 或 [(name, spec)] 或 [spec, …]。"""
        bench = LayoutBenchmark()
        for nm, sp in _named(specs):
            res = self.generate(sp, name=nm)
            score = res.layout_score
            score.name = nm
            bench.add(score)
        return bench

    def top_n(self, specs, n: int) -> list:
        """回排名前 n 的 LayoutScore(overall 由高到低)。"""
        return self.benchmark(specs).ranked()[:n]


# ── 輔助 ────────────────────────────────────────────────────────────────────
def _room_layouts(floor_spec, *, prefix: str = "") -> dict:
    """把一層的最終家具依房間收成 {room: [furniture…]}(只收有語意規則的房)。"""
    out: dict = {}
    for r in floor_spec.rooms:
        if get_room_rule(r.kind) is None:
            continue
        poly = Polygon(r.points)
        items = []
        for f in floor_spec.fixtures:
            if isinstance(f, FixturePlacement):
                c = Polygon(fixture_footprint(f)).centroid
                if poly.contains(c):
                    items.append({
                        "name": f.name,
                        "insert": [round(f.insert[0], 1), round(f.insert[1], 1)],
                        "rotation": f.rotation})
            elif isinstance(f, Counter):
                c = Polygon(counter_footprint(f)).centroid
                if poly.contains(c):
                    items.append({
                        "name": "counter",
                        "start": [round(f.start[0], 1), round(f.start[1], 1)],
                        "end": [round(f.end[0], 1), round(f.end[1], 1)]})
        out[f"{prefix}{r.name}"] = items
    return out


def _aggregate(floor_scores, global_scores):
    """整棟 overall = 有家具的樓層 overall 平均;sub_scores = 各樓層平均。"""
    furnished = [fs["overall"] for fs in floor_scores
                 if fs.get("furnished_rooms", 0) > 0]
    pool = furnished or [fs["overall"] for fs in floor_scores]
    overall = sum(pool) / len(pool) if pool else 0.0
    if global_scores:
        n = len(global_scores)
        sub = {k: sum(g.sub_scores.get(k, 0.0) for g in global_scores) / n
               for k in SCORE_ITEMS}
    else:
        sub = {k: 0.0 for k in SCORE_ITEMS}
    return overall, sub


def _rebuild_building(building, opt_floors):
    """用最佳化後的各層 spec 組一個新的 BuildingSpec(原 building 不動)。"""
    from src.design.building_generator import BuildingSpec, FloorLevel
    floors = [FloorLevel(level=fl.level, elevation=fl.elevation, spec=opt_spec)
              for fl, opt_spec in opt_floors]
    return BuildingSpec(floors=floors, floor_height=building.floor_height)


def _named(specs):
    """把 specs 統一成 [(name, spec)]。"""
    if isinstance(specs, dict):
        return list(specs.items())
    out = []
    for i, s in enumerate(specs):
        if isinstance(s, tuple):
            out.append(s)
        else:
            out.append((f"layout_{i}", s))
    return out
