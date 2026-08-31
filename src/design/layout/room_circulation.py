"""Room Circulation(v0.7 Phase 7.2a)—— 驗證「房間內部走得通」。

⚠️ **唯讀**:只分析,不改 spec、不碰 Generator。

破口:`walkway.py` 只量 `kind=="corridor"` 的房間;但小宅動線融入客廳、多數樓層
沒有獨立走道,家具最會擋路的正是客廳(玄關→陽台/各房的必經通道)。優化器的
`_score_walkway` 又只看「每件家具各自離門迴轉多遠」,抓不到「兩件家具合起來把唯一
通路夾死」。本模組補這段:**對每個房間,驗證所有出入口(門/通道)之間、以及到房內
每件家具的使用點,都有一條 ≥ 通行寬 的可走路徑**。

作法(純 shapely,不用格點):

    free = 房間多邊形 −(房內家具 footprint 聯集)     # 可站立的空地
    core = free.buffer(−W/2)                          # 一個「半徑 W/2 的人」走得到的地方
    core 的每個連通塊 = 一片彼此走得通的導航區

    每個目標(門內側點 / 家具使用點)吸附到最近的 core 塊。
      * 吸不上任何塊(> REACH_TOL)→ 那個目標被家具困住(旁邊沒 W 寬可站)。
      * 全部目標落在**同一塊** → 這房動線連通;落在不同塊 → 被家具切開。

⚠️ 門迴轉**不**當障礙:人本來就穿門洞而過(門扇 vs 家具的衝突另由 collision 硬閘門
管)。這裡的障礙只有家具——直接測「家具擋不擋路」。

典型用法::

    rep = analyze_room_circulation(spec)
    print(rep.summary())
    rep.ok            # 每個房間內部都走得通?
    rep.blocked       # 被家具擋死動線的房間
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from shapely.geometry import LineString, MultiPolygon, Point, Polygon
from shapely.ops import unary_union

from src.design.collision.geometry import fixture_obstacles
from src.design.report import JsonReport
from src.design.semantic.room_semantic import canonical_room

# 通行淨寬(mm):一個人側身/正身通過的下限。低於此的縫不算「走得通」。
# walkway.py 對獨立走道用 750(主動線);房內擠身通道取 600(單人可過)。
PASSAGE_WIDTH = 600.0
# 障礙侵入房間的面積門檻(mm²):濾掉只擦到邊界的家具。
INTRUDE_TOL = 100.0
# 判斷某開口/家具是否貼著這房的邊界容差(mm)。
ON_BOUNDARY_TOL = 50.0


@dataclass
class RoomCirculation(JsonReport):
    """一個房間的內部動線量測。"""

    name: str
    kind: str
    ok: bool
    components: int                     # free 侵蝕後的導航塊數(1=全連通)
    openings: int                       # 門/通道數
    targets: int                        # 受檢目標數(門內側點 + 家具使用點)
    isolated: list = field(default_factory=list)   # 被困住/被切開的目標標籤
    reason: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name, "kind": self.kind, "ok": self.ok,
            "components": self.components, "openings": self.openings,
            "targets": self.targets, "isolated": list(self.isolated),
            "reason": self.reason,
        }


@dataclass
class CirculationReport(JsonReport):
    """一份格局的房間內部動線分析(唯讀產物)。"""

    rooms: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(r.ok for r in self.rooms)

    @property
    def blocked(self) -> list:
        return [r for r in self.rooms if not r.ok]

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "count": len(self.rooms),
            "blocked": len(self.blocked),
            "rooms": [r.to_dict() for r in self.rooms],
        }

    def summary(self) -> str:
        head = (f"CirculationReport:{len(self.rooms)} 房 · "
                f"{len(self.blocked)} 房動線被家具擋 → "
                f"{'PASS' if self.ok else 'FAIL'}")
        lines = [head]
        for r in self.rooms:
            mark = "✅" if r.ok else "⚠️"
            extra = f" ← {r.reason}" if r.reason else ""
            lines.append(f"  {mark} {r.name}({r.kind}):門/通道 {r.openings}、"
                         f"目標 {r.targets}、導航塊 {r.components}{extra}")
        return "\n".join(lines)


# ── 目標點:門內側 + 家具使用點 ─────────────────────────────────────────────
# 邊界上沒有牆的那一段要這麼寬,才算「走得過去的開放通道」(mm)。
OPEN_PASSAGE_MIN = 750.0


def _open_passages(spec, room_poly: Polygon) -> list:
    """房間邊界上**根本沒有牆**的開口 → 開放通道中心點。

    為什麼需要:集合住宅的玄關與起居室之間是開放的(兩者之間不畫牆),
    這種連通不掛門扇、也沒有 Opening 物件,只認門的話會把開放起居室誤判成
    「沒門進不去」——實測 4 戶/排的標準層有 8 間起居室中招。

    只算「另一側是別的房間」的缺口:外牆上的缺口是外面,不是通道。"""
    from shapely.ops import unary_union

    walls = getattr(spec, "walls", None) or []
    if not walls:
        return []
    bodies = unary_union([
        LineString([w.start, w.end]).buffer(w.thickness / 2.0 + 5.0,
                                            cap_style=2, join_style=2)
        for w in walls])
    free = room_poly.exterior.difference(bodies)
    others = [Polygon(r.points) for r in getattr(spec, "rooms", [])
              if Polygon(r.points).equals(room_poly) is False]
    cx, cy = room_poly.centroid.x, room_poly.centroid.y
    out = []
    for geom in getattr(free, "geoms", [free]):
        if geom.is_empty or geom.length < OPEN_PASSAGE_MIN:
            continue
        mid = geom.interpolate(0.5, normalized=True)
        dx, dy = mid.x - cx, mid.y - cy
        n = math.hypot(dx, dy) or 1.0
        outside = Point(mid.x + dx / n * 200.0, mid.y + dy / n * 200.0)
        if any(p.contains(outside) for p in others):
            out.append(mid)
    return out


def _room_openings(spec, room_poly: Polygon) -> list:
    """房間邊界上的門/通道中心點(世界座標)。

    含三種:掛門扇的門、Opening(kind=="door")的開放通道、以及**沒有牆**的
    開放邊界(見 _open_passages)。「進不進得去」全專案以這個為準。"""
    pts = []
    for w in spec.walls:
        for op in w.openings:
            if op.kind != "door":
                continue
            cx, cy = w.point_at(op.position)
            if room_poly.exterior.distance(Point(cx, cy)) < ON_BOUNDARY_TOL:
                pts.append(Point(cx, cy))
    return pts + _open_passages(spec, room_poly)


def _room_furniture(spec, room_poly: Polygon) -> list:
    """房內家具障礙(footprint 與房間重疊超過門檻)。回 [(標籤, footprint)]。"""
    out = []
    for o in fixture_obstacles(spec):
        if o.poly.intersection(room_poly).area > INTRUDE_TOL:
            out.append((o.tag, o.poly))
    return out


def _room_stairs(spec, room_poly: Polygon) -> list:
    """房內**梯段**(含折返平台)的 footprint —— 那不是地板,人走不過去。

    ⚠️ 這條是 2026-08-28 補的,補的是一個很大的洞:本模組原本的障礙**只有家具**,
    於是「樓梯把樓梯間切成兩半、門開在走不到的那一半」完全看不見。而另一道關卡
    `plan_check.floor_split` 是拿**一間房當一個節點**去連通 —— 一間房被自己的樓梯
    切成兩半,它照樣算「同一塊」。兩道規則各自都在,樓梯剛好從中間漏掉。

    實測(使用者 2026-08-28 指著 7×12 的圖說「一定要走過廁所才能到廚房」):
    窄透天預設核 96 個樓層有 **38 個**、淺基地 70 個有 **26 個**,前後段之間
    根本走不通,而 plan_check 全部給過。

    `_stair_boxes` 是「梯段不能當地板走」的單一出處(它知道折返平台在半層高、
    起步平台在樓層高),所以直接借它,不要在這裡另外算一份。"""
    from src.design.layout.narrow_house import _stair_boxes
    return [b for b in _stair_boxes(spec)
            if b.intersection(room_poly).area > INTRUDE_TOL]


# 導航塊面積下限(mm²):小於此的視為侵蝕殘渣,不算真正可站的導航區。
COMP_MIN_AREA = 50_000.0


def _components_of(core) -> list:
    """core(可能是空 / Polygon / MultiPolygon)→ 導航塊多邊形清單(依面積大到小,
    濾掉侵蝕殘渣)。"""
    if core.is_empty:
        return []
    parts = list(core.geoms) if isinstance(core, MultiPolygon) else [core]
    parts = [g for g in parts if not g.is_empty and g.area >= COMP_MIN_AREA]
    return sorted(parts, key=lambda g: g.area, reverse=True)


def _touches(geom, comp, reach: float) -> bool:
    """geom(門點 / 家具 footprint)離某導航塊夠近(≤ reach)= 從那塊走得到。"""
    return comp.distance(geom) <= reach


# ── 單一房間 ────────────────────────────────────────────────────────────────
def analyze_room(spec, room, width: float = PASSAGE_WIDTH) -> RoomCirculation:
    """一個房間的內部動線。

    障礙=房內家具;核心 = free 侵蝕 W/2 後的導航區。判準:所有門、所有家具都能
    從**同一塊主導航區**走得到(離主塊 ≤ W/2+ε 即算走得到,方向不拘=靠牆家具從
    正面接得上)。門接不上→門被擋;家具接不上→被家具困住;主塊外還有大導航塊→
    可走空間被切開。"""
    kind = canonical_room(room.kind)
    room_poly = Polygon(room.points)
    furn = _room_furniture(spec, room_poly)
    openings = _room_openings(spec, room_poly)

    blocks = [p for _, p in furn] + _room_stairs(spec, room_poly)
    free = room_poly
    if blocks:
        free = room_poly.difference(unary_union(blocks))
    comps = _components_of(free.buffer(-width / 2))

    reach = width / 2 + 80.0
    isolated: list[str] = []
    reason = ""

    if not comps:
        ok = False
        reason = f"整房無 {width:.0f}mm 寬可站空間"
    else:
        main = comps[0]                              # 最大導航塊 = 主動線空間
        for i, p in enumerate(openings):
            if not _touches(p, main, reach):
                isolated.append(f"門{i + 1}")
        for tag, fp in furn:
            if not _touches(fp, main, reach):
                isolated.append(tag)
        # ⚠️ 只在「門/家具走不到主塊」時才算不通。單純存在一塊沒接上的**死角空地**
        # (沒困住任何門或家具)只是浪費坪效,不是動線斷掉——不判 fail(components
        # 仍記在報告裡供參考)。真正有害的切割一定會讓某個門或家具落在別塊 → 被上面
        # 的 reach 檢查抓成 isolated。
        ok = not isolated
        if isolated:
            reason = "無足夠通道走到:" + "、".join(dict.fromkeys(isolated))
        elif len(comps) > 1:
            reason = f"(可站空間有 {len(comps)} 塊,但門與家具都走得到,僅死角空地)"

    return RoomCirculation(
        name=room.name, kind=kind, ok=ok,
        components=len(comps), openings=len(openings),
        targets=len(openings) + len(furn), isolated=isolated, reason=reason)


# 儲藏空間短邊小於這個就是**壁櫃/櫥櫃**(開門拿東西),不是走得進去的房間。
CABINET_MAX_SIDE = 900.0


def _is_cabinet(room) -> bool:
    from shapely.geometry import Polygon
    if canonical_room(room.kind) not in ("storage", "utility"):
        return False
    x0, y0, x1, y1 = Polygon(room.points).bounds
    return min(x1 - x0, y1 - y0) < CABINET_MAX_SIDE


def analyze_room_circulation(spec, width: float = PASSAGE_WIDTH,
                             *, skip_kinds=("patio", "parking", "garage",
                                           "stair", "balcony",
                                           "pipe_shaft")) -> CirculationReport:
    """整個格局逐房檢查內部動線。**唯讀**。

    ⚠️ 壁櫃不是房間:深度不到 CABINET_MAX_SIDE 的儲藏空間(管道間旁的收納、走道
    邊的櫥櫃)是**開門拿東西**、不是走進去的,不要求 600mm 可站空間。"""
    rooms = []
    for room in spec.rooms:
        if canonical_room(room.kind) in skip_kinds or room.kind in skip_kinds:
            continue
        if _is_cabinet(room):
            continue
        try:
            rooms.append(analyze_room(spec, room, width))
        except (ValueError, TypeError):
            continue                    # 退化房間交上層報,不讓分析器爆掉
    return CirculationReport(rooms=rooms)


def circulation_ok(spec, width: float = PASSAGE_WIDTH) -> bool:
    """便捷判斷:所有房間內部都走得通?"""
    return analyze_room_circulation(spec, width).ok
