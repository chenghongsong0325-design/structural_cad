"""Layout Validation Layer(v0.7 Phase 5-1)—— 只檢查,不修改。

定位:接在 Generator → Collision Engine 之後,對成品 `FloorPlanSpec` 做
「格局層級」的健檢,產出一份 `LayoutReport`。

⚠️ 本層是**唯讀**的:不改 spec、不 Auto Fix、不碰 Generator / Room Program。
它回答的是「這張圖的格局在拓樸上說不說得通」,而不是「怎麼修」。

與既有檢核的分工:
  * `validate_spec()` —— 生成流程的守門(法規/採光/家具/開口壓柱),會擋圖。
  * `collision`      —— 家具與障礙的偵測與修復。
  * **本模組**        —— 房間多邊形、房間重疊、連通性(孤立房/門/走道)的
    獨立複驗;不接進生成流程,故對現有輸出零影響。

五項檢查:
  1. Room Polygon 是否封閉(頂點數/自交/零面積)
  2. Room 是否重疊
  3. 是否存在孤立 Room(從入口走不到)
  4. Door 是否可連通(門是否真的落在房間邊界上)
  5. Corridor 是否中斷(走道是否連成一體)

典型用法::

    from src.design.layout_validation import validate_layout

    report = validate_layout(spec)
    if not report.ok:
        print(report.summary())
"""
from __future__ import annotations

from dataclasses import dataclass, field

from shapely.geometry import LineString
from shapely.geometry import Point as SPoint
from shapely.geometry import Polygon
from shapely.ops import substring, unary_union

# 房間重疊容差(mm²)——與 validate_spec 的房間重疊判準一致。
OVERLAP_TOL = 1.0
# 兩房要算「相鄰」,共用邊界至少這麼長(mm);更短的只是角碰角。
MIN_SHARE = 100.0
# 沒有牆遮蔽的共用邊界要有這麼寬才算「走得過去」(mm)。門寬約 800~900。
MIN_PASSAGE = 700.0
# 牆中心線緩衝(mm):房間邊界與牆中心線共線,用細緩衝判斷該段有沒有牆。
WALL_EPS = 2.0
# 判斷點是否落在某條邊界上的距離門檻(mm)。
ON_BOUNDARY_TOL = 1.0
# 不要求「必須走得到」的房型:天井/挑空是室外空井,不是動線終點。
UNREACHABLE_EXEMPT = {"patio"}


@dataclass
class LayoutIssue:
    """一則檢查結果。severity:"error"(格局不成立)/ "warn"(可疑,待人判斷)。"""

    check: str
    severity: str
    message: str

    def __str__(self) -> str:                       # 方便 print
        return f"[{self.severity}] {self.check}:{self.message}"


@dataclass
class LayoutReport:
    """一次格局健檢的結果(唯讀產物)。"""

    issues: list[LayoutIssue] = field(default_factory=list)
    rooms: int = 0
    doors: int = 0
    corridors: int = 0

    @property
    def errors(self) -> list[LayoutIssue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self) -> list[LayoutIssue]:
        return [i for i in self.issues if i.severity == "warn"]

    @property
    def ok(self) -> bool:
        """沒有 error 就算通過(warn 只是提醒)。"""
        return not self.errors

    def summary(self) -> str:
        head = (f"LayoutReport:房間 {self.rooms} · 門 {self.doors} · "
                f"走道 {self.corridors} → "
                f"{'PASS' if self.ok else 'FAIL'}"
                f"(error {len(self.errors)} · warn {len(self.warnings)})")
        return "\n".join([head] + [f"  {i}" for i in self.issues])


# ---------------------------------------------------------------------------
# 幾何輔助(全部唯讀)
# ---------------------------------------------------------------------------
def _room_polys(spec) -> list[Polygon]:
    """房間多邊形;頂點不足以構成面時回**空多邊形**(不丟例外——退化的房間
    要由 check_polygons 報成問題,而不是讓整個驗證器爆掉)。"""
    out = []
    for r in spec.rooms:
        try:
            out.append(Polygon(r.points))
        except (ValueError, TypeError):
            out.append(Polygon())
    return out


def _wall_cover(spec):
    """牆對「通行」的遮蔽範圍(細緩衝聯集)——判斷某段共用邊界走不走得過去。

    ⚠️ 關鍵:牆會擋人,但 **kind=="door" 的洞口要扣掉**——那是通道,不論它
    有沒有掛門扇(開放式餐廚、客廳與家庭廳的連通口都是「有洞口、無 Door
    Placement」,只看 spec.doors 會誤判成不連通)。窗不能走,故不扣。"""
    segs = []
    for w in spec.walls:
        if w.start == w.end:
            continue
        line = LineString([w.start, w.end])
        length = line.length
        gaps = sorted(
            (max(0.0, op.position - op.width / 2),
             min(length, op.position + op.width / 2))
            for op in w.openings if op.kind == "door")
        pos = 0.0
        for a, b in gaps:                            # 逐段留下「有牆」的部分
            if a > pos:
                segs.append(substring(line, pos, a))
            pos = max(pos, b)
        if pos < length:
            segs.append(substring(line, pos, length))
    buf = [s.buffer(WALL_EPS) for s in segs if s.length > 0]
    return unary_union(buf) if buf else None


def _door_points(spec) -> list:
    """每扇門在牆中心線上的中心點(世界座標)。"""
    pts = []
    for dp in spec.doors:
        w = spec.walls[dp.wall_index]
        op = w.openings[dp.opening_index]
        pts.append(SPoint(w.point_at(op.position)))
    return pts


def _shared_edge(a: Polygon, b: Polygon):
    """兩房共用的邊界(線);沒有共用邊回 None。"""
    if not a.intersects(b):
        return None
    shared = a.exterior.intersection(b.exterior)
    if shared.is_empty or shared.length < MIN_SHARE:
        return None
    return shared


def _connections(spec, polys, cover, door_pts) -> dict[int, set[int]]:
    """房間連通圖:door(門)或 open(無牆的共用邊界夠寬)都算連通。"""
    graph: dict[int, set[int]] = {i: set() for i in range(len(polys))}
    for i in range(len(polys)):
        for j in range(i + 1, len(polys)):
            shared = _shared_edge(polys[i], polys[j])
            if shared is None:
                continue
            linked = any(p.distance(shared) < ON_BOUNDARY_TOL for p in door_pts)
            if not linked:
                gap = shared.difference(cover) if cover is not None else shared
                linked = (not gap.is_empty) and gap.length >= MIN_PASSAGE
            if linked:
                graph[i].add(j)
                graph[j].add(i)
    return graph


def _entry_index(spec, polys, door_pts) -> int | None:
    """入口房:優先玄關,其次客廳,再其次「貼著最多門」的房間。"""
    for want in ("foyer", "living"):
        for i, r in enumerate(spec.rooms):
            if r.kind == want:
                return i
    best, best_n = None, 0
    for i, poly in enumerate(polys):
        if poly.is_empty:
            continue
        n = sum(1 for p in door_pts
                if poly.exterior.distance(p) < ON_BOUNDARY_TOL)
        if n > best_n:
            best, best_n = i, n
    return best


# ---------------------------------------------------------------------------
# LayoutValidator
# ---------------------------------------------------------------------------
class LayoutValidator:
    """對一份 FloorPlanSpec 做格局健檢。**唯讀**:不修改 spec 的任何欄位。"""

    def __init__(self, spec):
        self.spec = spec
        self.polys = _room_polys(spec)
        self.cover = _wall_cover(spec)
        self.door_pts = _door_points(spec)

    # ── 1. Room Polygon 是否封閉 ──────────────────────────────────────────
    def check_polygons(self) -> list[LayoutIssue]:
        out = []
        for r, poly in zip(self.spec.rooms, self.polys):
            if len(r.points) < 3:
                out.append(LayoutIssue(
                    "polygon", "error", f"{r.name} 只有 {len(r.points)} 個頂點,無法構成面"))
                continue
            if not poly.is_valid:
                out.append(LayoutIssue(
                    "polygon", "error", f"{r.name} 多邊形不合法(邊界自交)"))
            elif poly.area <= 0:
                out.append(LayoutIssue(
                    "polygon", "error", f"{r.name} 面積為 0(退化多邊形)"))
        return out

    # ── 2. Room 是否重疊 ──────────────────────────────────────────────────
    def check_overlap(self) -> list[LayoutIssue]:
        out = []
        rooms = self.spec.rooms
        for i in range(len(self.polys)):
            for j in range(i + 1, len(self.polys)):
                if not (self.polys[i].is_valid and self.polys[j].is_valid):
                    continue
                area = self.polys[i].intersection(self.polys[j]).area
                if area > OVERLAP_TOL:
                    out.append(LayoutIssue(
                        "overlap", "error",
                        f"{rooms[i].name} × {rooms[j].name} 重疊 {area/1e6:.2f}m²"))
        return out

    # ── 3. 是否存在孤立 Room ──────────────────────────────────────────────
    def check_isolated(self) -> list[LayoutIssue]:
        if not self.polys:
            return []
        graph = _connections(self.spec, self.polys, self.cover, self.door_pts)
        entry = _entry_index(self.spec, self.polys, self.door_pts)
        if entry is None:
            return [LayoutIssue("isolated", "warn", "找不到入口房間,略過連通判定")]
        seen, stack = {entry}, [entry]
        while stack:                                # BFS/DFS 走訪
            for nxt in graph[stack.pop()]:
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        out = []
        for i, r in enumerate(self.spec.rooms):
            if i in seen or r.kind in UNREACHABLE_EXEMPT:
                continue
            why = "沒有任何門或開口相連" if not graph[i] else "與入口不連通"
            out.append(LayoutIssue("isolated", "error", f"{r.name} 走不到({why})"))
        return out

    # ── 4. Door 是否可連通 ────────────────────────────────────────────────
    def check_doors(self) -> list[LayoutIssue]:
        out = []
        for dp, pt in zip(self.spec.doors, self.door_pts):
            touching = [r.name for r, poly in zip(self.spec.rooms, self.polys)
                        if not poly.is_empty
                        and poly.exterior.distance(pt) < ON_BOUNDARY_TOL]
            if not touching:
                out.append(LayoutIssue(
                    "door", "error",
                    f"牆 {dp.wall_index} 的門不在任何房間邊界上(連不到房間)"))
        return out

    # ── 5. Corridor 是否中斷 ──────────────────────────────────────────────
    def check_corridor(self) -> list[LayoutIssue]:
        corr = [p for r, p in zip(self.spec.rooms, self.polys)
                if r.kind == "corridor" and p.is_valid]
        if len(corr) < 2:
            return []                               # 0 或 1 段走道不會「中斷」
        merged = unary_union(corr)
        if merged.geom_type == "MultiPolygon":
            return [LayoutIssue(
                "corridor", "error",
                f"走道分成 {len(merged.geoms)} 段,彼此不相連(動線中斷)")]
        return []

    # ── 總檢 ──────────────────────────────────────────────────────────────
    def validate(self) -> LayoutReport:
        """跑完五項檢查,回一份 LayoutReport(不修改 spec)。"""
        issues: list[LayoutIssue] = []
        issues += self.check_polygons()
        issues += self.check_overlap()
        issues += self.check_isolated()
        issues += self.check_doors()
        issues += self.check_corridor()
        return LayoutReport(
            issues=issues,
            rooms=len(self.spec.rooms),
            doors=len(self.spec.doors),
            corridors=sum(1 for r in self.spec.rooms if r.kind == "corridor"))


def validate_layout(spec) -> LayoutReport:
    """對外入口:回一份 LayoutReport。**唯讀**,不改 spec。"""
    return LayoutValidator(spec).validate()
