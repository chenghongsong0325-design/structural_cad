"""Layout Scoring Engine(v0.7 Phase 5-4)—— 把格局品質量化成分數。

⚠️ **唯讀**:只評分,不改 spec、不 Auto Optimize、不碰 Generator。

七個面向,各自算 0~100 的子分數,再依權重加權平均成總分:

    Connectivity        入口走得到多少房間(重用 Phase 5-2 Connectivity Graph)
    Circulation         動線品質:瓶頸/盡端/繞路程度(重用 Phase 5-3 Corridor)
    Privacy             私密房(臥室/衛浴)是否直接開向公共空間
    Natural Lighting    居室是否有窗、採光深度是否過深
    Area Utilization    樓地板效率:扣掉純走道後的可用面積比
    Furniture Coverage  該有家具的房間是否真的擺了
    Collision           家具與障礙的殘留碰撞(重用 Collision Engine)

權重可調(ScoreWeights),總分 = Σ(子分數 × 權重) / Σ(權重)。預設權重把
「走得到、不撞、有採光」這種硬需求排在「面積效率」這種軟指標前面。

⚠️ 分數是**相對指標**,用來比較不同方案、找退步,不是絕對品質保證——
硬性規範仍由 validate_spec 把關。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from shapely.geometry import Point as SPoint
from shapely.geometry import Polygon

from src.design.collision.detector import find_collisions
from src.design.collision.geometry import collect_active
from src.design.connectivity import UNREACHABLE_EXEMPT, analyze_connectivity
from src.design.corridor import analyze_corridors
from src.design.report import JsonReport

# ── 房型分類 ──────────────────────────────────────────────────────────────
# 需要天然採光的居室(浴廁/儲藏/走道不列入——無窗是常態)。
HABITABLE_KINDS = {"bedroom", "living", "dining", "study", "family"}
# 私密房:不該直接開向「訪客會到」的公共空間。
PRIVATE_KINDS = {"bedroom", "bathroom"}
# 公共空間(訪客會進入的對外空間)。
PUBLIC_KINDS = {"living", "dining", "kitchen", "foyer"}
# 半私密:自家人的空間,私密房開向這裡**不算侵犯隱私**。
# ⚠️ 家庭廳(family)刻意不列為 public——透天臥室層「家庭廳 + 四周臥室」是本
# 引擎的正常設計,實測 100 層中 bedroom→family 出現 75 次;把它當隱私失分會
# 懲罰正確的格局。真正該抓的是 bathroom→living(57)、bedroom→living(38)、
# bathroom→dining(6)這種私密房直開訪客空間。
SEMI_PRIVATE_KINDS = {"family", "study"}
# 純交通面積(不算可用樓地板)。
CIRCULATION_KINDS = {"corridor"}
# 各房型「該有」的家具(插入點落在房內才算)。
EXPECTED_FIXTURES = {
    "bedroom": ("bed_single", "bed_double"),
    "bathroom": ("toilet",),
    "living": ("sofa3",),
}

# 採光深度上限(mm),與 validate_spec 的 C1.5c 一致。
DAYLIGHT_DEPTH_MAX = 6000.0
# 繞路比 = 最遠步行距離 / 建築對角線。<= 此值不扣分,超過線性扣到 0。
DETOUR_IDEAL = 1.5
DETOUR_WORST = 3.0
# 單一瓶頸/盡端的扣分。
PENALTY_BOTTLENECK = 20.0
PENALTY_DEAD_END = 25.0
# 單一殘留碰撞的扣分。
PENALTY_COLLISION = 25.0


def _clamp(v: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, v))


@dataclass
class ScoreWeights(JsonReport):
    """七個面向的權重(可自由調整;總分會除以權重總和,故不必和為 1)。"""

    connectivity: float = 2.0
    circulation: float = 1.5
    privacy: float = 1.0
    lighting: float = 1.5
    utilization: float = 1.0
    furniture: float = 1.0
    collision: float = 2.0

    def as_map(self) -> dict:
        return {
            "connectivity": self.connectivity, "circulation": self.circulation,
            "privacy": self.privacy, "lighting": self.lighting,
            "utilization": self.utilization, "furniture": self.furniture,
            "collision": self.collision,
        }

    def to_dict(self) -> dict:
        return {k: float(v) for k, v in self.as_map().items()}


@dataclass
class ScoreItem(JsonReport):
    """單一面向的評分結果。"""

    name: str
    score: float                    # 0~100
    weight: float
    detail: str = ""

    def to_dict(self) -> dict:
        return {"name": self.name, "score": round(self.score, 1),
                "weight": float(self.weight), "detail": self.detail}


@dataclass
class LayoutScore(JsonReport):
    """一份格局的總評(唯讀產物)。"""

    items: list = field(default_factory=list)        # list[ScoreItem]
    total: float = 0.0

    @property
    def grade(self) -> str:
        for cut, g in ((90, "A"), (80, "B"), (70, "C"), (60, "D")):
            if self.total >= cut:
                return g
        return "F"

    def get(self, name: str) -> ScoreItem | None:
        return next((i for i in self.items if i.name == name), None)

    def to_dict(self) -> dict:
        return {
            "total": round(self.total, 1),
            "grade": self.grade,
            "items": [i.to_dict() for i in self.items],
        }

    def summary(self) -> str:
        head = f"LayoutScore:{self.total:.1f} / 100(等級 {self.grade})"
        return "\n".join([head] + [
            f"  {i.name:14} {i.score:5.1f} ×{i.weight:.1f}  {i.detail}"
            for i in self.items])


# ---------------------------------------------------------------------------
# 幾何輔助(唯讀)
# ---------------------------------------------------------------------------
def _openings_on(spec, poly: Polygon, kind: str) -> int:
    """房間邊界上某類洞口的數量。"""
    if poly.is_empty:
        return 0
    n = 0
    for w in spec.walls:
        for op in w.openings:
            if op.kind != kind:
                continue
            if poly.exterior.distance(SPoint(w.point_at(op.position))) < 1.0:
                n += 1
    return n


def _daylight_depth(spec, room, poly: Polygon) -> float | None:
    """沿窗法線方向的最小房間深度;沒有窗回 None(與 validate_spec 同演算)。"""
    depths = []
    for w in spec.walls:
        nx, ny = w.normal_vector
        for op in w.openings:
            if op.kind != "window":
                continue
            wx, wy = w.point_at(op.position)
            if poly.exterior.distance(SPoint((wx, wy))) >= 1.0:
                continue
            depths.append(max(abs((px - wx) * nx + (py - wy) * ny)
                              for px, py in room.points))
    return min(depths) if depths else None


def _fixture_points(spec):
    """每件家具的代表點(流理台取中點)。"""
    from src.drafting.fixtures import Counter, FixturePlacement
    out = []
    for fx in spec.fixtures:
        if isinstance(fx, Counter):
            out.append(("counter", ((fx.start[0] + fx.end[0]) / 2,
                                    (fx.start[1] + fx.end[1]) / 2)))
        elif isinstance(fx, FixturePlacement):
            out.append((fx.name, fx.insert))
    return out


def _building_diagonal(spec) -> float:
    w, d = sum(spec.x_spacings), sum(spec.y_spacings)
    return math.hypot(w, d)


# ---------------------------------------------------------------------------
# 七個面向
# ---------------------------------------------------------------------------
def score_connectivity(spec, polys) -> ScoreItem:
    """入口走得到的非豁免房間比例。走不到是硬傷,直接反映在比例上。"""
    rep = analyze_connectivity(spec)
    targets = [i for i, r in enumerate(spec.rooms)
               if r.kind not in UNREACHABLE_EXEMPT]
    if not targets:
        return ScoreItem("connectivity", 100.0, 0.0, "沒有需要連通的房間")
    bad = len(rep.dead_rooms) + len(rep.unreachable)
    score = _clamp(100.0 * (1 - bad / len(targets)))
    if rep.orphan_doors:
        score = _clamp(score - 10.0 * len(rep.orphan_doors))
    detail = (f"{len(targets) - bad}/{len(targets)} 間走得到"
              + (f",孤兒門 {len(rep.orphan_doors)}" if rep.orphan_doors else ""))
    return ScoreItem("connectivity", score, 0.0, detail)


def score_circulation(spec) -> ScoreItem:
    """動線:瓶頸與盡端各扣分,再看「最遠步行 / 建築對角線」的繞路程度。"""
    rep = analyze_corridors(spec)
    score = 100.0
    score -= PENALTY_BOTTLENECK * len(rep.bottlenecks)
    score -= PENALTY_DEAD_END * len(rep.dead_ends)
    diag = _building_diagonal(spec)
    ratio = rep.longest_distance / diag if diag > 0 and rep.longest_distance else 0.0
    if ratio > DETOUR_IDEAL:
        over = (ratio - DETOUR_IDEAL) / (DETOUR_WORST - DETOUR_IDEAL)
        score -= _clamp(over, 0.0, 1.0) * 40.0
    detail = (f"最遠 {rep.longest_distance/1000:.1f}m / 對角 {diag/1000:.1f}m "
              f"(繞路比 {ratio:.2f})"
              + (f",瓶頸 {len(rep.bottlenecks)}" if rep.bottlenecks else "")
              + (f",盡端 {len(rep.dead_ends)}" if rep.dead_ends else ""))
    return ScoreItem("circulation", _clamp(score), 0.0, detail)


def score_privacy(spec, polys) -> ScoreItem:
    """私密房(臥室/衛浴)是否直接開向公共空間。

    套內衛浴接主臥屬私密對私密,不扣分;衛浴門直開客廳才扣。"""
    from src.design.connectivity import build_graphs
    g = build_graphs(spec)
    private = [i for i, r in enumerate(spec.rooms) if r.kind in PRIVATE_KINDS]
    if not private:
        return ScoreItem("privacy", 100.0, 0.0, "沒有私密房間")
    exposed = []
    for i in private:
        if any(g.kinds[j] in PUBLIC_KINDS for j in g.room_graph[i]):
            exposed.append(g.names[i])
    score = _clamp(100.0 * (1 - len(exposed) / len(private)))
    detail = (f"{len(private) - len(exposed)}/{len(private)} 間不直開公共空間"
              + (f"(外露:{', '.join(exposed)})" if exposed else ""))
    return ScoreItem("privacy", score, 0.0, detail)


def score_lighting(spec, polys) -> ScoreItem:
    """居室是否有窗;有窗但採光深度超標再扣半分。"""
    rooms = [(r, p) for r, p in zip(spec.rooms, polys)
             if r.kind in HABITABLE_KINDS]
    if not rooms:
        return ScoreItem("lighting", 100.0, 0.0, "沒有需採光的居室")
    total = 0.0
    no_window, too_deep = [], []
    for r, p in rooms:
        if _openings_on(spec, p, "window") < 1:
            no_window.append(r.name)
            continue
        depth = _daylight_depth(spec, r, p)
        if depth is not None and depth > DAYLIGHT_DEPTH_MAX + 1.0:
            too_deep.append(r.name)
            total += 0.5                             # 有窗但太深,給半分
        else:
            total += 1.0
    score = _clamp(100.0 * total / len(rooms))
    detail = (f"{len(rooms) - len(no_window)}/{len(rooms)} 間有窗"
              + (f",無窗:{', '.join(no_window)}" if no_window else "")
              + (f",採光過深:{', '.join(too_deep)}" if too_deep else ""))
    return ScoreItem("lighting", score, 0.0, detail)


def score_utilization(spec, polys) -> ScoreItem:
    """樓地板效率:扣掉純走道後的可用面積比(走道越少越有效率)。"""
    total = sum(p.area for p in polys)
    if total <= 0:
        return ScoreItem("utilization", 0.0, 0.0, "沒有可用樓地板")
    circ = sum(p.area for r, p in zip(spec.rooms, polys)
               if r.kind in CIRCULATION_KINDS)
    ratio = 1 - circ / total
    detail = (f"可用 {ratio*100:.1f}%(走道 {circ/1e6:.1f}m² / "
              f"總 {total/1e6:.1f}m²)")
    return ScoreItem("utilization", _clamp(ratio * 100.0), 0.0, detail)


def score_furniture(spec, polys) -> ScoreItem:
    """該有家具的房間是否真的擺了(插入點落在房內)。"""
    pts = _fixture_points(spec)
    targets = [(r, p) for r, p in zip(spec.rooms, polys)
               if r.kind in EXPECTED_FIXTURES]
    if not targets:
        return ScoreItem("furniture", 100.0, 0.0, "沒有需配家具的房間")
    missing = []
    for r, p in targets:
        want = EXPECTED_FIXTURES[r.kind]
        ok = any(name in want and
                 (p.contains(SPoint(pt)) or p.exterior.distance(SPoint(pt)) < 200)
                 for name, pt in pts)
        if not ok:
            missing.append(r.name)
    score = _clamp(100.0 * (1 - len(missing) / len(targets)))
    detail = (f"{len(targets) - len(missing)}/{len(targets)} 間配齊"
              + (f",缺:{', '.join(missing)}" if missing else ""))
    return ScoreItem("furniture", score, 0.0, detail)


def score_collision(spec) -> ScoreItem:
    """殘留碰撞(家具×家具/門/牆/天井/樓梯/柱)。合格圖應為 0。"""
    cols = find_collisions(collect_active(spec))
    score = _clamp(100.0 - PENALTY_COLLISION * len(cols))
    detail = "無碰撞" if not cols else \
        f"殘留 {len(cols)} 處:" + ", ".join(f"{c.a.tag}×{c.b.kind}" for c in cols[:3])
    return ScoreItem("collision", score, 0.0, detail)


# ---------------------------------------------------------------------------
# 總分
# ---------------------------------------------------------------------------
def score_layout(spec, weights: ScoreWeights | None = None) -> LayoutScore:
    """七面向評分 + 加權總分。**唯讀**,不改 spec。

    總分 = Σ(子分數 × 權重) / Σ(權重)。"""
    w = weights or ScoreWeights()
    polys = [Polygon(r.points) if len(r.points) >= 3 else Polygon()
             for r in spec.rooms]

    items = [
        score_connectivity(spec, polys),
        score_circulation(spec),
        score_privacy(spec, polys),
        score_lighting(spec, polys),
        score_utilization(spec, polys),
        score_furniture(spec, polys),
        score_collision(spec),
    ]
    wmap = w.as_map()
    for it in items:
        it.weight = float(wmap.get(it.name, 0.0))

    wsum = sum(it.weight for it in items)
    total = (sum(it.score * it.weight for it in items) / wsum) if wsum else 0.0
    return LayoutScore(items=items, total=total)
