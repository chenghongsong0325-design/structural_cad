"""圖面正確性檢查器(硬性關卡)—— 保證輸出的平面圖「一定能用」。

前面各階段是「盡量畫好」;這一層是**把關**:一張圖只要違反下列任何一條硬規則,
就不算合格圖,產線會換個切法重生,不會輸出給使用者。這樣才不是修一個 bug 補一個
補丁,而是「以後的圖都不會有這些問題」。

硬錯誤(error,落實端一定救得動 → 不合格就重生)::

    room_no_door        可進入的房間沒有門(進不去)
    floor_split         同一層室內斷成好幾塊(要繞到室外才能過去)
    no_entry            1F 沒有對外大門(進不了建築)
    entry_upstairs      樓上外牆開門(門通往空中)
    furniture_in_wall   家具嵌進牆體(畫出來是穿牆)
    door_in_corner      門洞卡在房間角落(人走不進那個角)
    stair_blocks_door   門直接開在階梯上(缺起步平台,門扇會掃到踏step)
    stair_side_open     梯段有一側沒牆(人走上去會從旁邊掉下去)
    circulation_blocked 房間走不進去/家具擋死動線

設計警告(warning,要改「房間怎麼配」才救得動,由收斂迴圈回饋給 LLM)::

    room_no_daylight    居室是內間(四周無外牆也無天井)→ 沒窗
    room_oversize       居室大到不合理
    room_skinny         居室細長得像走廊

⚠️ 分界原則:**error = 同一份房間關係圖、換個切法就能解決**;warning = 非改設計
   (房間數/相鄰關係)不可。所以只有 error 拿來擋圖,warning 只回報。
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field

from shapely.geometry import LineString, Point, Polygon

# 這些「房間」是封閉服務豎井,本來就不設門窗,所有規則都跳過。
VOID_KINDS = {"pipe_shaft", "patio"}
# 需要對外採光的居室(內間沒光要回報)。
DAYLIGHT_KINDS = {"living", "dining", "bedroom", "master_bedroom", "study",
                  "elder_room"}
# 這些房間本來就細長,不檢查長寬比。
SKINNY_OK_KINDS = {"corridor", "pipe_shaft", "patio", "balcony", "storage",
                   "utility"}

EDGE_TOL = 60.0             # 貼邊/貼牆的容差(mm)
WALL_OVERLAP_TOL = 1000.0   # 家具與牆重疊超過這個面積(mm²)算穿牆
OVERSIZE_RATIO = 1.5        # 面積超過理想上限這麼多倍算過大
ASPECT_LIMIT = 2.8          # 居室長寬比超過這個算細長
STAIR_LANDING_MIN = 600.0   # 門與第一階之間至少要有這麼深的平地(起步平台)


def _stair_footprint(stair):
    """樓梯踏step 佔的矩形(世界座標)。拿不到幾何回 None。"""
    from shapely.geometry import box
    try:
        ox, oy = stair.origin
        w, ln = stair.width, stair.length
    except Exception:
        return None
    if getattr(stair, "direction", "north") in ("north", "south"):
        return box(ox, oy, ox + w, oy + ln)
    return box(ox, oy, ox + ln, oy + w)


@dataclass(frozen=True)
class PlanIssue:
    """一條檢查結果。severity: "error"(擋圖)/ "warning"(只回報)。"""

    severity: str
    code: str
    floor: str
    room: str
    detail: str

    def __str__(self) -> str:
        mark = "❌" if self.severity == "error" else "⚠️"
        who = f"{self.floor}/{self.room}" if self.room else self.floor
        return f"{mark} {who}:{self.detail}"


@dataclass
class PlanCheckReport:
    """整棟(或單層)的檢查報表。"""

    issues: list[PlanIssue] = field(default_factory=list)

    @property
    def errors(self) -> list[PlanIssue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self) -> list[PlanIssue]:
        return [i for i in self.issues if i.severity == "warning"]

    @property
    def ok(self) -> bool:
        """沒有硬錯誤 = 這張圖可以出。"""
        return not self.errors

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "n_errors": len(self.errors),
            "n_warnings": len(self.warnings),
            "issues": [asdict(i) for i in self.issues],
        }

    def to_json(self, **kw) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, **kw)

    def summary(self) -> str:
        if not self.issues:
            return "✅ 圖面檢查全過(無錯誤、無警告)"
        head = (f"{'✅' if self.ok else '❌'} 錯誤 {len(self.errors)} 項、"
                f"警告 {len(self.warnings)} 項")
        return "\n".join([head, *(str(i) for i in self.issues)])


# ── 幾何小工具 ──────────────────────────────────────────────────────────────
def _wall_bodies(spec) -> list:
    """每道牆的實體多邊形(中心線往兩側各推半個牆厚)。"""
    return [LineString([w.start, w.end]).buffer(w.thickness / 2.0,
                                                cap_style=2, join_style=2)
            for w in spec.walls]


def _openings_of(spec, poly: Polygon, kind: str) -> list:
    """房間邊界上的門洞/窗洞中心點。"""
    out = []
    for w in spec.walls:
        for op in w.openings:
            if op.kind != kind:
                continue
            p = Point(*w.point_at(op.position))
            if poly.exterior.distance(p) < EDGE_TOL:
                out.append(p)
    return out


def _on_envelope(x: float, y: float, env) -> bool:
    """這個點是不是落在建築外框上(= 對外開口)。"""
    return (abs(y - env[1]) < EDGE_TOL or abs(y - env[3]) < EDGE_TOL
            or abs(x - env[0]) < EDGE_TOL or abs(x - env[2]) < EDGE_TOL)


def _room_graph_components(spec) -> list[set]:
    """靠門互通的房間分群(豎井/天井不算)。斷成多塊 = 室內走不通。"""
    import itertools

    polys = [(r, Polygon(r.points)) for r in spec.rooms]
    live = [i for i, (r, _p) in enumerate(polys) if r.kind not in VOID_KINDS]
    adj = {i: set() for i in live}
    for w in spec.walls:
        for op in w.openings:
            if op.kind != "door":
                continue
            p = Point(*w.point_at(op.position))
            touch = [i for i in live
                     if polys[i][1].exterior.distance(p) < EDGE_TOL]
            for a, b in itertools.combinations(touch, 2):
                adj[a].add(b)
                adj[b].add(a)
    comps, seen = [], set()
    for i in live:
        if i in seen:
            continue
        grp, stack = {i}, [i]
        seen.add(i)
        while stack:
            k = stack.pop()
            for n in adj[k]:
                if n not in grp:
                    grp.add(n)
                    seen.add(n)
                    stack.append(n)
        comps.append(grp)
    return comps


# ── 單層檢查 ────────────────────────────────────────────────────────────────
def check_floor(spec, env, level: int, label: str = "") -> list[PlanIssue]:
    """檢查一層,回問題清單(error + warning)。env=(x0,y0,x1,y1) 建築外框。"""
    from src.design.layout.graph_layout import AREA_BAND
    from src.design.layout.room_circulation import analyze_room_circulation

    lb = label or getattr(spec, "floor_label", "") or f"{level}F"
    issues: list[PlanIssue] = []
    polys = [(r, Polygon(r.points)) for r in spec.rooms]
    patios = [p for r, p in polys if r.kind == "patio"]

    # ① 每間可進入的房間都要有門
    for r, poly in polys:
        if r.kind in VOID_KINDS:
            continue
        if not _openings_of(spec, poly, "door"):
            issues.append(PlanIssue("error", "room_no_door", lb, r.name,
                                    "沒有門,進不去"))

    # ② 同層室內要連通(不能要繞到室外)
    comps = _room_graph_components(spec)
    if len(comps) > 1:
        groups = [[spec.rooms[i].name for i in c] for c in comps]
        issues.append(PlanIssue("error", "floor_split", lb, "",
                                f"室內斷成 {len(comps)} 塊,彼此走不通:{groups}"))

    # ③ 對外門:1F 恰一扇、樓上不得有
    ext = [(w, op) for w in spec.walls for op in w.openings
           if op.kind == "door" and _on_envelope(*w.point_at(op.position), env)]
    if level == 1 and not ext:
        issues.append(PlanIssue("error", "no_entry", lb, "",
                                "沒有對外大門,進不了建築"))
    if level != 1 and ext:
        issues.append(PlanIssue("error", "entry_upstairs", lb, "",
                                f"樓上外牆開了 {len(ext)} 扇門(門會通往空中)"))

    # ④ 家具不得嵌進牆體
    from src.design.collision.geometry import fixture_obstacles
    bodies = _wall_bodies(spec)
    for o in fixture_obstacles(spec):
        overlap = sum(o.poly.intersection(b).area for b in bodies)
        if overlap > WALL_OVERLAP_TOL:
            issues.append(PlanIssue(
                "error", "furniture_in_wall", lb, getattr(o, "tag", "家具"),
                f"家具嵌進牆體 {overlap/1e6:.3f}㎡(畫出來是穿牆)"))

    # ⑤ 門不得卡在房間角落(人走不進那個角 → 動線判不通,看圖卻不明顯)
    from src.design.layout.narrow_house import DOOR_CORNER_MIN, _door_pos_ok
    for w in spec.walls:
        for op in w.openings:
            if op.kind != "door":
                continue
            if not _door_pos_ok(spec, w, op.position, op.width,
                                DOOR_CORNER_MIN):        # 動線走得通的物理下限
                px, py = w.point_at(op.position)
                issues.append(PlanIssue(
                    "error", "door_in_corner", lb, "",
                    f"門洞({px:.0f},{py:.0f})貼著房間角落,人走不進去"))

    # ⑥ 門不得直接開在階梯上(門與第一階之間要有起步平台可站)
    from src.design.layout.narrow_house import _door_clear_of_stairs
    if getattr(spec, "stairs", None):
        for w in spec.walls:
            for op in w.openings:
                if op.kind != "door":
                    continue
                if not _door_clear_of_stairs(spec, w, op.position, op.width):
                    p = Point(*w.point_at(op.position))
                    issues.append(PlanIssue(
                        "error", "stair_blocks_door", lb, "",
                        f"門洞({p.x:.0f},{p.y:.0f})正對階梯,開門就要踩上踏step"
                        f"(門前需有可站的平地)"))

    # ⑥b 梯段兩側都要有牆:走在階梯上,旁邊是空的就會掉下去(只有起步/折返平台
    #     那種平地才可以開口)。
    from src.design.layout.narrow_house import _flight_sides, _side_is_walled
    for st in getattr(spec, "stairs", None) or []:
        sides, _vertical = _flight_sides(st)
        for (p0, p1) in sides:
            if not _side_is_walled(spec, (p0, p1)):
                issues.append(PlanIssue(
                    "error", "stair_side_open", lb, "",
                    f"梯段側邊({p0[0]:.0f},{p0[1]:.0f})→({p1[0]:.0f},{p1[1]:.0f})"
                    f"沒有牆,人走在階梯上會掉下去"))

    # ⑦ 動線:每間房走得進去、家具沒擋死
    rep = analyze_room_circulation(spec)
    if not rep.ok:
        for rc in rep.blocked:
            issues.append(PlanIssue("error", "circulation_blocked", lb, rc.name,
                                    f"動線不通({rc.reason})"))

    # ⑧ 設計面警告(落實端救不動,回饋給 LLM 重設計)
    for r, poly in polys:
        if r.kind in VOID_KINDS:
            continue
        x0, y0, x1, y1 = poly.bounds
        if r.kind in DAYLIGHT_KINDS:
            lit = (abs(y0 - env[1]) < EDGE_TOL or abs(y1 - env[3]) < EDGE_TOL
                   or any(poly.distance(p) < EDGE_TOL for p in patios))
            if not lit:
                issues.append(PlanIssue("warning", "room_no_daylight", lb,
                                        r.name, "是內間,沒有對外採光面"))
        band = AREA_BAND.get(r.kind)
        if band and r.area_m2 > band[1] * OVERSIZE_RATIO:
            issues.append(PlanIssue("warning", "room_oversize", lb, r.name,
                                    f"{r.area_m2:.0f}㎡ 過大(理想 ≤{band[1]:.0f}㎡)"))
        if r.kind not in SKINNY_OK_KINDS:
            side = max(x1 - x0, y1 - y0), max(min(x1 - x0, y1 - y0), 1.0)
            ar = side[0] / side[1]
            if ar > ASPECT_LIMIT:
                issues.append(PlanIssue("warning", "room_skinny", lb, r.name,
                                        f"長寬比 {ar:.1f},細長得像走廊"))
    return issues


def check_building(floors, env) -> PlanCheckReport:
    """檢查整棟。floors = [(label, spec, ...)](多的欄位忽略)。"""
    issues: list[PlanIssue] = []
    for item in floors:
        label, spec = item[0], item[1]
        level = int(label[:-1]) if label[:-1].isdigit() else 1
        issues.extend(check_floor(spec, env, level, label))
    return PlanCheckReport(issues)
