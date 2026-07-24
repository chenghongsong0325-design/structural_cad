"""Global Layout Scoring(v0.7 Phase 6-7)—— 整棟住宅層級的總評分引擎。

LayoutScoreEngine 把一份 FloorPlanSpec 讀成 12 個 0~100 的子分數,加權收斂成
overall_score,再對照門檻給 grade(A+/A/B/C/D)。LayoutBenchmark 收多份 Layout,
可排序、算平均、輸出 JSON/CSV,拿來比較不同方案。

12 個子分數:
    furniture          需要家具的房間有沒有被佈置(佈置涵蓋率)。
    collision          既有家具通過事前碰撞查詢的比例。
    walkway            走道是否夠寬、沒被擋(無獨立走道視為合格)。
    human_clearance    家具的人體活動空間(開門/拉椅/通行)平均分。
    constraint         單件家具擺放偏好(靠牆/朝向/前方淨空)平均分。
    pair_constraint    家具之間的關聯偏好(沙發面向電視…)平均分。
    room_semantic      房間功能語意(該有的家具有沒有、有沒有不該出現)平均分。
    space_efficiency   功能空間佔比(非走道/天井的房間面積 / 全部房間面積)。
    furniture_density  家具密度(太空/太擠都扣,理想帶滿分)平均分。
    symmetry           家具在房間裡的擺放平衡(形心偏離房中心的程度)平均分。
    natural_lighting   採光:可居住房間中「有窗」的比例。
    window_usage       窗未被高家具擋住的比例(窗的可用性)。

⚠️ **唯讀、純評分**:重用 Phase 6-1~6-6 的既有 evaluator,不改 spec、不接進生成
流程,合法與否仍由 FurnitureCollisionEngine 決定。子分數是啟發式,權重可調。

⚠️ 這裡的多方案報表刻意**不**取名 LayoutReport——那個名字已被
`layout_validation.LayoutReport`(單張圖的檢核報告)佔用;本模組的報表輸出改由
LayoutBenchmark.to_json() / to_csv() 提供,語意是「多份 Layout 的評分排行」。

⚠️ 子分數的定義與權重為啟發式,非任何規範——見模組結尾 PENDING。
"""
from __future__ import annotations

import csv
import io
import math
from dataclasses import dataclass, field

from shapely.geometry import Point as SPoint
from shapely.geometry import Polygon

from src.design.collision.furniture_constraint import evaluate_constraint
from src.design.collision.furniture_engine import (
    TALL_FIXTURES,
    WINDOW_BLOCK_RATIO,
    FurnitureCollisionEngine,
)
from src.design.collision.furniture_pair_constraint import (
    FurniturePairEvaluator,
    PairTarget,
)
from src.design.collision.geometry import window_obstacles
from src.design.collision.human_clearance import HumanClearanceEvaluator
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

# 12 個子分數的固定順序(報表欄位、加權都依此)。
SCORE_ITEMS = (
    "furniture", "collision", "walkway", "human_clearance", "constraint",
    "pair_constraint", "room_semantic", "space_efficiency", "furniture_density",
    "symmetry", "natural_lighting", "window_usage",
)

# 各子分數在 overall 的權重(啟發式,可由呼叫端覆蓋)。
DEFAULT_LAYOUT_WEIGHTS: dict[str, float] = {
    "furniture": 1.0,
    "collision": 2.0,
    "walkway": 1.5,
    "human_clearance": 1.0,
    "constraint": 1.0,
    "pair_constraint": 0.8,
    "room_semantic": 1.2,
    "space_efficiency": 1.0,
    "furniture_density": 0.8,
    "symmetry": 0.6,
    "natural_lighting": 1.0,
    "window_usage": 0.8,
}

# grade 門檻(overall_score ≥ 門檻 → 該等第)。
GRADE_THRESHOLDS = (("A+", 90.0), ("A", 80.0), ("B", 70.0), ("C", 60.0))

# 可居住(需採光)的房間 kind。
HABITABLE_KINDS = frozenset({
    "living", "dining", "bedroom", "kitchen", "study", "family",
    "single", "master",
})
# 非功能空間(算空間效率時當「非功能」的分母那側)。
CIRCULATION_KINDS = frozenset({"corridor", "patio", "stair"})

# 家具密度的理想帶(家具佔地 / 房間面積);帶內滿分,過空/過擠往下掉。
DENSITY_LOW = 0.15
DENSITY_HIGH = 0.40


def _clamp(v, lo=0.0, hi=100.0):
    return max(lo, min(hi, v))


def grade_of(score: float) -> str:
    """overall_score → 等第(A+/A/B/C/D)。"""
    for name, thr in GRADE_THRESHOLDS:
        if score >= thr:
            return name
    return "D"


def _footprint(obj):
    if isinstance(obj, Counter):
        return Polygon(counter_footprint(obj))
    if isinstance(obj, FixturePlacement):
        return Polygon(fixture_footprint(obj))
    return None


# ── 結果模型 ────────────────────────────────────────────────────────────────
@dataclass
class LayoutScore(JsonReport):
    """一份 Layout 的總評分。"""

    name: str = ""
    overall_score: float = 0.0
    grade: str = "D"
    sub_scores: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "overall_score": round(self.overall_score, 1),
            "grade": self.grade,
            "sub_scores": {k: round(self.sub_scores.get(k, 0.0), 1)
                           for k in SCORE_ITEMS},
        }

    def summary(self) -> str:
        head = (f"LayoutScore:{self.name or '(未命名)'} → "
                f"{self.overall_score:.1f} 分 [{self.grade}]")
        parts = " · ".join(f"{k} {self.sub_scores.get(k, 0.0):.0f}"
                           for k in SCORE_ITEMS)
        return f"{head}\n  {parts}"

    def __str__(self) -> str:
        return self.summary()


# ── 引擎 ────────────────────────────────────────────────────────────────────
class LayoutScoreEngine:
    """把一份 FloorPlanSpec 評成 LayoutScore。**唯讀**:不改 spec。"""

    def __init__(self, weights: dict[str, float] | None = None):
        self.weights = dict(weights) if weights is not None \
            else dict(DEFAULT_LAYOUT_WEIGHTS)

    def score(self, spec, name: str = "") -> LayoutScore:
        sub = self.sub_scores(spec)
        wsum = sum(self.weights.get(k, 0.0) for k in SCORE_ITEMS) or 1.0
        overall = sum(sub[k] * self.weights.get(k, 0.0)
                      for k in SCORE_ITEMS) / wsum
        overall = _clamp(overall)
        return LayoutScore(name=name, overall_score=overall,
                           grade=grade_of(overall), sub_scores=sub)

    # ── 12 個子分數 ───────────────────────────────────────────────────────
    def sub_scores(self, spec) -> dict:
        ctx = _Context(spec)
        return {
            "furniture": self._furniture(ctx),
            "collision": self._collision(ctx),
            "walkway": self._walkway(ctx),
            "human_clearance": self._human(ctx),
            "constraint": self._constraint(ctx),
            "pair_constraint": self._pair(ctx),
            "room_semantic": self._semantic(ctx),
            "space_efficiency": self._space_efficiency(ctx),
            "furniture_density": self._density(ctx),
            "symmetry": self._symmetry(ctx),
            "natural_lighting": self._lighting(ctx),
            "window_usage": self._window_usage(ctx),
        }

    # 佈置涵蓋率:需要家具的房間(語意規則有 required 或 min_count>0)有沒有家具。
    @staticmethod
    def _furniture(ctx) -> float:
        need = [r for r in ctx.rooms
                if (rule := get_room_rule(r.kind)) is not None
                and (rule.required or rule.min_count > 0)]
        if not need:
            return 100.0
        furnished = sum(1 for r in need if ctx.fixtures_in(r))
        return _clamp(100.0 * furnished / len(need))

    # 既有家具通過事前碰撞查詢的比例。
    @staticmethod
    def _collision(ctx) -> float:
        checks = ctx.engine.check_existing()
        if not checks:
            return 100.0
        ok = sum(1 for _, res in checks if res.valid)
        return _clamp(100.0 * ok / len(checks))

    # 走道:無獨立走道視為合格(動線融入客廳);有則看被擋與淨寬。
    @staticmethod
    def _walkway(ctx) -> float:
        wr = ctx.walkway_report
        if wr is None or not wr.has_walkway:
            return 100.0
        n = len(wr.walkways) or 1
        base = 100.0 * (1.0 - len(wr.blocked) / n)
        if wr.min_width:
            base = min(base, _clamp(100.0 * wr.min_width / 900.0))
        return _clamp(base)

    def _human(self, ctx) -> float:
        ev = HumanClearanceEvaluator()
        return ctx.avg_over_fixtures(
            lambda fx, room, others:
            ev.evaluate_human_clearance(fx, room, others).score)

    def _constraint(self, ctx) -> float:
        def one(fx, room, others):
            if not isinstance(fx, FixturePlacement):
                return None
            res = evaluate_constraint(fx, room, engine=ctx.engine)
            s = 100.0
            if not res.wall_ok:
                s -= 40.0
            if not res.orientation_ok:
                s -= 20.0
            if not res.clearance_ok:
                s -= 40.0
            return _clamp(s)
        return ctx.avg_over_fixtures(one)

    def _pair(self, ctx) -> float:
        ev = FurniturePairEvaluator()
        targets = ctx.pair_targets
        def one(fx, room, others):
            placed = [t for t in targets if t is not fx]
            return ev.evaluate_pair_constraints(fx, room, placed).score
        return ctx.avg_over_fixtures(one)

    @staticmethod
    def _semantic(ctx) -> float:
        ev = RoomSemanticEvaluator()
        scores = []
        for r in ctx.rooms:
            if get_room_rule(r.kind) is None:
                continue
            scores.append(ev.evaluate_room_semantics(r, ctx.fixtures_in(r)).score)
        return sum(scores) / len(scores) if scores else 100.0

    # 空間效率:非走道/天井的房間面積佔全部房間面積比例。
    @staticmethod
    def _space_efficiency(ctx) -> float:
        total = sum(a for _, _, a in ctx.room_area)
        if total <= 0:
            return 100.0
        functional = sum(a for r, _, a in ctx.room_area
                         if r.kind not in CIRCULATION_KINDS)
        return _clamp(100.0 * functional / total)

    # 家具密度:理想帶內滿分,過空/過擠往下掉。
    @staticmethod
    def _density(ctx) -> float:
        scores = []
        for r, poly, area in ctx.room_area:
            if area <= 0 or get_room_rule(r.kind) is None:
                continue
            furn = sum(fp.area for fp in ctx.footprints_in(r))
            d = furn / area
            if d < DENSITY_LOW:
                s = 100.0 * d / DENSITY_LOW
            elif d <= DENSITY_HIGH:
                s = 100.0
            else:
                s = 100.0 - (d - DENSITY_HIGH) * 200.0
            scores.append(_clamp(s))
        return sum(scores) / len(scores) if scores else 100.0

    # 對稱/平衡:家具形心平均值離房間中心越近越好。
    @staticmethod
    def _symmetry(ctx) -> float:
        scores = []
        for r, poly, area in ctx.room_area:
            fps = ctx.footprints_in(r)
            if not fps or area <= 0:
                continue
            cx = sum(fp.centroid.x for fp in fps) / len(fps)
            cy = sum(fp.centroid.y for fp in fps) / len(fps)
            rc = poly.centroid
            reach = math.sqrt(area / math.pi)
            off = math.hypot(cx - rc.x, cy - rc.y) / reach if reach > 0 else 0.0
            scores.append(_clamp(100.0 * (1.0 - off)))
        return sum(scores) / len(scores) if scores else 100.0

    # 採光:可居住房間中「有窗」的比例。
    @staticmethod
    def _lighting(ctx) -> float:
        rooms = [r for r in ctx.rooms if r.kind in HABITABLE_KINDS]
        if not rooms:
            return 100.0
        lit = 0
        for r in rooms:
            poly = Polygon(r.points)
            if any(poly.contains(z.poly.centroid) for z in ctx.window_zones):
                lit += 1
        return _clamp(100.0 * lit / len(rooms))

    # 窗的可用性:未被高家具擋住的窗比例。
    @staticmethod
    def _window_usage(ctx) -> float:
        zones = ctx.window_zones
        if not zones:
            return 100.0
        tall = [fp for fx, fp in ctx.tall_footprints]
        blocked = 0
        for z in zones:
            za = z.poly.area
            if za <= 0:
                continue
            if any(z.poly.intersection(fp).area / za > WINDOW_BLOCK_RATIO
                   for fp in tall):
                blocked += 1
        return _clamp(100.0 * (1.0 - blocked / len(zones)))


# ── 計算上下文(把重複幾何算一次)──────────────────────────────────────────
class _Context:
    def __init__(self, spec):
        self.spec = spec
        self.rooms = list(spec.rooms)
        self.engine = FurnitureCollisionEngine(spec)
        self.fixtures = [f for f in spec.fixtures
                         if isinstance(f, (FixturePlacement, Counter))]
        # 家具佔地 + 形心 + 所屬房間
        self._fp = {}
        self._centroid = {}
        for f in self.fixtures:
            fp = _footprint(f)
            self._fp[id(f)] = fp
            self._centroid[id(f)] = fp.centroid if fp is not None else None
        self.room_polys = [(r, Polygon(r.points)) for r in self.rooms]
        self.room_area = [(r, poly, poly.area) for r, poly in self.room_polys]
        self._room_of = {}
        for f in self.fixtures:
            c = self._centroid[id(f)]
            self._room_of[id(f)] = None
            if c is None:
                continue
            for r, poly in self.room_polys:
                if poly.contains(c):
                    self._room_of[id(f)] = r
                    break
        self.window_zones = window_obstacles(spec)
        self.tall_footprints = [
            (f, self._fp[id(f)]) for f in self.fixtures
            if isinstance(f, FixturePlacement) and f.name in TALL_FIXTURES
            and self._fp[id(f)] is not None]
        # 家具關聯目標:既有家具 + 窗 + 房間形心。
        self.pair_targets = list(self.fixtures)
        for z in self.window_zones:
            c = z.poly.centroid
            self.pair_targets.append(PairTarget("window", (c.x, c.y)))
        for r, poly in self.room_polys:
            c = poly.centroid
            self.pair_targets.append(PairTarget(r.kind, (c.x, c.y)))
        # walkway 報告(某些 spec 可能無法分析 → None)
        self.walkway_report = _safe_walkways(spec)

    def fixtures_in(self, room):
        return [f for f in self.fixtures if self._room_of[id(f)] is room]

    def footprints_in(self, room):
        return [self._fp[id(f)] for f in self.fixtures
                if self._room_of[id(f)] is room and self._fp[id(f)] is not None]

    def avg_over_fixtures(self, fn) -> float:
        """對每件「有所屬房間」的家具算 fn(fx, room, others),取平均。"""
        vals = []
        for f in self.fixtures:
            room = self._room_of[id(f)]
            if room is None:
                continue
            others = [g for g in self.fixtures if g is not f]
            v = fn(f, room, others)
            if v is not None:
                vals.append(v)
        return sum(vals) / len(vals) if vals else 100.0


def _safe_walkways(spec):
    from src.design.walkway import analyze_walkways
    try:
        return analyze_walkways(spec)
    except Exception:                                # pragma: no cover - 防禦
        return None


# ── 多方案基準 / 報表 ──────────────────────────────────────────────────────
@dataclass
class LayoutBenchmark(JsonReport):
    """收多份 Layout 的 LayoutScore,能排序、算平均、出 JSON/CSV。"""

    entries: list = field(default_factory=list)      # list[LayoutScore]

    @classmethod
    def from_specs(cls, specs, *, weights=None) -> "LayoutBenchmark":
        """specs 可為 {name: spec} 或 [(name, spec)] 或 [spec, …]。"""
        engine = LayoutScoreEngine(weights)
        bench = cls()
        if isinstance(specs, dict):
            items = list(specs.items())
        else:
            items = []
            for i, s in enumerate(specs):
                if isinstance(s, tuple):
                    items.append(s)
                else:
                    items.append((f"layout_{i}", s))
        for name, spec in items:
            bench.entries.append(engine.score(spec, name=name))
        return bench

    def add(self, score: LayoutScore) -> None:
        self.entries.append(score)

    def ranked(self) -> list:
        """依 overall_score 由高到低排序(穩定:同分保留加入順序)。"""
        return sorted(self.entries, key=lambda e: e.overall_score, reverse=True)

    def best(self) -> LayoutScore | None:
        return self.ranked()[0] if self.entries else None

    def average(self) -> float:
        if not self.entries:
            return 0.0
        return sum(e.overall_score for e in self.entries) / len(self.entries)

    def average_sub_scores(self) -> dict:
        if not self.entries:
            return {k: 0.0 for k in SCORE_ITEMS}
        n = len(self.entries)
        return {k: sum(e.sub_scores.get(k, 0.0) for e in self.entries) / n
                for k in SCORE_ITEMS}

    def to_dict(self) -> dict:
        return {
            "count": len(self.entries),
            "average": round(self.average(), 1),
            "average_sub_scores": {k: round(v, 1)
                                   for k, v in self.average_sub_scores().items()},
            "ranking": [e.to_dict() for e in self.ranked()],
        }

    def to_csv(self) -> str:
        """一列一份 Layout(依排名),欄位 = rank/name/overall/grade + 12 子分數。"""
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["rank", "name", "overall_score", "grade", *SCORE_ITEMS])
        for i, e in enumerate(self.ranked(), start=1):
            writer.writerow([
                i, e.name, round(e.overall_score, 1), e.grade,
                *[round(e.sub_scores.get(k, 0.0), 1) for k in SCORE_ITEMS],
            ])
        return buf.getvalue()

    def summary(self) -> str:
        head = (f"LayoutBenchmark:{len(self.entries)} 份 · "
                f"平均 {self.average():.1f} 分")
        lines = [head]
        for i, e in enumerate(self.ranked(), start=1):
            lines.append(f"  {i}. {e.name or '(未命名)'} "
                         f"{e.overall_score:.1f} [{e.grade}]")
        return "\n".join(lines)


# ── 就地評分報表(不搬家具)────────────────────────────────────────────────
def _floor_list(spec):
    """把輸入攤成 [(label, floor_spec)]:BuildingSpec → 各層;FloorPlanSpec → 單層。"""
    if hasattr(spec, "rooms"):                       # FloorPlanSpec(單層)
        return [(getattr(spec, "floor_label", "") or "1F", spec)]
    return [(fl.label, fl.spec) for fl in spec.floors]  # BuildingSpec


def _centroid_in(fixture, poly) -> bool:
    if isinstance(fixture, Counter):
        c = Polygon(counter_footprint(fixture)).centroid
    elif isinstance(fixture, FixturePlacement):
        c = Polygon(fixture_footprint(fixture)).centroid
    else:
        return False
    return poly.contains(c)


def score_report(spec, *, name: str = "") -> dict:
    """把一份平面(FloorPlanSpec)或整棟(BuildingSpec)**就地評分,不搬家具**。

    ⚠️ 與 MultiRoomOptimizer / AutoLayoutEngine 不同:那些會重排家具(對已由
    產生器擺好的圖反而會打散、變差);本函式**只讀不動**,回傳純評分報表——
    產生器擺好的漂亮佈局原封不動,只給等第 + 12 項子分數 + 各房機能檢查。

    回傳 JSON-native dict:
        overall_score / grade / sub_scores / floors[] / rooms[]
    """
    engine = LayoutScoreEngine()
    sem = RoomSemanticEvaluator()
    floors = _floor_list(spec)
    multi = len(floors) > 1

    floor_out, gscores, rooms = [], [], []
    for label, fs in floors:
        gs = engine.score(fs, name=label)
        gscores.append(gs)
        furnishable = 0
        for r in fs.rooms:
            if get_room_rule(r.kind) is None:
                continue
            furnishable += 1
            poly = Polygon(r.points)
            items = [f for f in fs.fixtures if _centroid_in(f, poly)]
            rr = sem.evaluate_room_semantics(r, items)
            rooms.append({
                "room": (f"{label}/{r.name}" if multi else r.name),
                "kind": canonical_room(r.kind),
                "semantic": round(rr.score, 1),
                "furniture": len(items),
                "missing": list(rr.missing),
            })
        floor_out.append({
            "label": label,
            "overall": round(gs.overall_score, 1),
            "grade": gs.grade,
            "furnished_rooms": furnishable,
        })

    # 整棟 overall:有家具的樓層平均(避免車庫/機房拉低);sub_scores 各層平均。
    pool = [f["overall"] for f in floor_out if f["furnished_rooms"] > 0] \
        or [f["overall"] for f in floor_out]
    overall = sum(pool) / len(pool) if pool else 0.0
    n = len(gscores) or 1
    sub = {k: round(sum(g.sub_scores.get(k, 0.0) for g in gscores) / n, 1)
           for k in SCORE_ITEMS}

    return {
        "name": name,
        "overall_score": round(overall, 1),
        "grade": grade_of(overall),
        "sub_scores": sub,
        "floors": floor_out,
        "rooms": rooms,
    }


# =============================================================================
# PENDING(待確認假設彙整)
# =============================================================================
# 1. 12 個子分數的定義與 DEFAULT_LAYOUT_WEIGHTS 權重為啟發式,非任何規範;grade
#    門檻(A+≥90 / A≥80 / B≥70 / C≥60 / D)亦為暫定,待與真實評圖標準對齊。
# 2. furniture_density 的理想帶 [0.15, 0.40]、walkway 淨寬基準 900mm、window_usage
#    的 WINDOW_BLOCK_RATIO(沿用 furniture_engine)皆為常見值,待確認。
# 3. natural_lighting 以「窗前淨空區形心落在房內」判定該房有窗;採光量(面寬/
#    深度比、座向)未計入,之後可細化。
# 4. 子分數重用 Phase 6-1~6-6 的 evaluator,故其規則一改,這裡的分數同步變動——
#    這是刻意的(單一事實來源),但比較不同版本的分數時要留意。
# =============================================================================
