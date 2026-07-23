"""geometry —— 把 FloorPlanSpec 的各元素轉成 Obstacle 的 provider。

重用既有幾何原語(不重造):家具佔地用 fixtures.fixture_footprint /
counter_footprint;柱用 apartment_plan.resolve_columns + members.column_corners;
門迴轉方塊把 validate_spec 原本 inline 的算法抽來這裡(單一來源)。

⚠️ Phase 1 的「作用中偵測集合」= 家具(movable)+ 門迴轉(static),與
validate_spec 現有檢核完全一致 → 接進流程零 regression。其餘 provider
(柱/天井/樓梯)已實作、備而不用,之後再納入。牆刻意不做成障礙:家具原點
就貼在牆邊,納入會把每件靠牆家具都誤判成撞牆。
"""
from __future__ import annotations

from shapely.geometry import Point as SPoint
from shapely.geometry import Polygon

from src.design.collision.obstacle import (
    COLUMN,
    DOOR_SWING,
    FURNITURE,
    STAIR,
    VOID,
    WALL,
    WINDOW,
    Obstacle,
)

# 窗前淨空深度(mm):窗台前這段距離內不該有高家具擋光/擋景。
WINDOW_CLEARANCE_MM = 300.0
from src.drafting.fixtures import (
    Counter,
    FixturePlacement,
    counter_footprint,
    fixture_collision_footprint,
    fixture_footprint,
)


# ── 房間歸屬(移動邊界用)──────────────────────────────────────────────────
def _room_polys(spec) -> list:
    """可放家具的房間多邊形(排除天井/挑空——那裡不該有家具)。"""
    from src.design.collision.obstacle import VOID  # noqa: F401(語意標註)
    out = []
    for r in spec.rooms:
        if r.kind == "patio":
            continue
        out.append(Polygon(r.points))
    return out


def _containing_room(pt, room_polys):
    """點落在哪個房間(找不到回 None)。家具形心用來定所屬房間。"""
    p = SPoint(pt)
    for poly in room_polys:
        if poly.contains(p) or poly.boundary.distance(p) < 1.0:
            return poly
    return None


# ── Provider:各元素 → Obstacle ────────────────────────────────────────────
def fixture_obstacles(spec) -> list[Obstacle]:
    """家具/流理台 → movable Obstacle(唯一可被修復器動的種類)。

    poly = 畫圖用完整 footprint(家具×家具、門迴轉用);collision_poly = 碰撞/
    穿牆判定用(桌椅組收緊)。room = 形心所屬房間(穿牆 barrier)。"""
    room_polys = _room_polys(spec)
    obs: list[Obstacle] = []
    for fx in spec.fixtures:
        if isinstance(fx, Counter):
            poly = cpoly = Polygon(counter_footprint(fx))
            tag = "counter"
        elif isinstance(fx, FixturePlacement):
            poly = Polygon(fixture_footprint(fx))
            cpoly = Polygon(fixture_collision_footprint(fx))
            tag = fx.name
        else:
            continue
        obs.append(Obstacle(
            poly=poly, kind=FURNITURE, movable=True, ref=fx, tag=tag,
            collision_poly=cpoly,
            room=_containing_room(poly.centroid, room_polys)))
    return obs


def wall_obstacles(spec) -> list[Obstacle]:
    """牆 → static Obstacle(kind=WALL),v0.6 Phase 2。

    ⚠️ 用「Room Polygon」當 barrier(方案 B):家具不得越過所屬房間的多邊形邊界
    (= 牆中心線)= 不得穿牆。故每間房(天井除外)產一個 kind=WALL 障礙,poly
    = 房間多邊形。detector 對 kind=WALL 用「家具 footprint 突出房間面積 > 容差」
    判定(不是家具×家具的 area>100),讓貼牆(微量突出)過、穿牆(越界)抓。"""
    return [Obstacle(poly=Polygon(r.points), kind=WALL, ref=r, tag=r.name,
                     room=Polygon(r.points))
            for r in spec.rooms if r.kind != "patio"]


def door_swing_polygon(spec, dp) -> Polygon:
    """一扇門的迴轉方塊(width×width,朝 swing 側)。

    ⚠️ 與 validate_spec 原本 inline 的算法完全一致(單一來源)。"""
    w = spec.walls[dp.wall_index]
    op = w.openings[dp.opening_index]
    cx, cy = w.point_at(op.position)
    ux, uy = w.unit_vector
    nx, ny = w.normal_vector
    s = 1.0 if dp.door.swing == "out" else -1.0
    h, e = op.width / 2, op.width
    return Polygon([
        (cx - ux * h, cy - uy * h),
        (cx + ux * h, cy + uy * h),
        (cx + ux * h + s * nx * e, cy + uy * h + s * ny * e),
        (cx - ux * h + s * nx * e, cy - uy * h + s * ny * e),
    ])


def door_swing_obstacles(spec) -> list[Obstacle]:
    """門迴轉範圍 → static Obstacle(家具不可壓)。"""
    obs: list[Obstacle] = []
    for dp in spec.doors:
        obs.append(Obstacle(
            poly=door_swing_polygon(spec, dp), kind=DOOR_SWING,
            ref=dp, tag=f"牆 {dp.wall_index} 的門",
            meta={"wall_index": dp.wall_index}))
    return obs


def column_obstacles(spec) -> list[Obstacle]:
    """柱斷面 → static Obstacle(kind=COLUMN),v0.6 Phase 3-3。

    ⚠️ **偵測用,不修復**:本 repo 的柱 100% 藏在牆內(柱網原則),家具貼牆
    必然壓到柱伸進室內的那半邊——Step 0 實測 941 件家具中 283 件(30%)如此,
    最深 175mm,**全部合法**。故柱:
      * 用**穿入深度** > COLUMN_TOLERANCE_MM 判定(不是 area>100),讓合法
        貼牆穿入穩過、只抓真正壓在柱上的家具;
      * engine **不對柱碰撞做 try_move/try_drop**,也**不把柱放進 blockers**
        ——柱完全不影響家具移動,純粹回報。"""
    from src.drafting.apartment_plan import build_grid, resolve_columns
    from src.drafting.members import column_corners
    obs: list[Obstacle] = []
    for c in resolve_columns(spec, build_grid(spec)):
        obs.append(Obstacle(poly=Polygon(column_corners(c)), kind=COLUMN,
                            ref=c, tag="柱"))
    return obs


def void_obstacles(spec) -> list[Obstacle]:
    """天井/挑空 → static Obstacle(kind=VOID),v0.6 Phase 3-1。

    ⚠️ 硬障礙(與門迴轉同語意):家具不得掉進天井/挑空——那裡沒有樓板。
    detector 用「重疊面積 > OVERLAP_TOL」判定(不是牆的突出判定):天井是
    正向禁區,家具壓到多少就是錯多少,沒有「貼著算合法」的餘地。

    ⚠️ 這裡補的是一個真破口:_room_polys 排除 patio,故形心落在天井的家具
    room=None、連穿牆都驗不到;加入 VOID 後這種家具才會被抓出來。"""
    return [Obstacle(poly=Polygon(r.points), kind=VOID, ref=r, tag=r.name)
            for r in spec.rooms if r.kind == "patio"]


def window_obstacles(spec, depth: float = WINDOW_CLEARANCE_MM) -> list[Obstacle]:
    """窗前淨空區 → static Obstacle(kind=WINDOW),v0.7 Phase 6-1。

    每扇窗在**室內側**拉一塊 (窗寬 × depth) 的矩形。室內側靠「往法線方向探
    100mm 看落在哪個房間」判定,不必假設牆的內外方向。

    ⚠️ **只給 FurnitureCollisionEngine 用,不在 collect_active 裡**:實測把窗前
    淨空當成通用硬障礙會擋掉 369~449 件家具——床頭靠窗、沙發靠窗、流理台在窗下
    (水槽對窗)都是**正確**擺法。真正該擋的只有「高家具擋窗」,那個判斷在
    furniture_engine 以 TALL_FIXTURES 篩選,不在這裡。"""
    polys = _room_polys(spec)
    out: list[Obstacle] = []
    for w in spec.walls:
        ux, uy = w.unit_vector
        nx, ny = w.normal_vector
        for op in w.openings:
            if op.kind != "window":
                continue
            cx, cy = w.point_at(op.position)
            side = 0
            for s in (1, -1):                       # 哪一側是室內
                probe = SPoint(cx + nx * 100 * s, cy + ny * 100 * s)
                if any(p.contains(probe) for p in polys if not p.is_empty):
                    side = s
                    break
            if side == 0:
                continue                            # 兩側都不是房間(外牆對戶外)
            half = op.width / 2
            dx, dy = nx * depth * side, ny * depth * side
            out.append(Obstacle(
                poly=Polygon([
                    (cx - ux * half, cy - uy * half),
                    (cx + ux * half, cy + uy * half),
                    (cx + ux * half + dx, cy + uy * half + dy),
                    (cx - ux * half + dx, cy - uy * half + dy)]),
                kind=WINDOW, ref=op, tag="窗前淨空"))
    return out


def stair_obstacles(spec) -> list[Obstacle]:
    """樓梯佔地矩形 → static Obstacle(kind=STAIR),v0.6 Phase 3-2。

    ⚠️ 硬障礙(與天井同語意):家具不得壓在梯段上——那是垂直動線,壓到就
    擋住上下樓。detector 用「重疊面積 > OVERLAP_TOL」判定。

    幾何用**軸對齊 bbox**:origin 一律是樓梯間矩形的最小 x/y 角,依行進方向
    決定寬深對應(南北向 → width×length,東西向 → length×width)。"""
    obs: list[Obstacle] = []
    for st in spec.stairs:
        ox, oy = st.origin
        if st.direction in ("north", "south"):
            w, d = st.width, st.length
        else:
            w, d = st.length, st.width
        obs.append(Obstacle(
            poly=Polygon([(ox, oy), (ox + w, oy), (ox + w, oy + d), (ox, oy + d)]),
            kind=STAIR, ref=st, tag="樓梯"))
    return obs


# ── 作用中集合:家具 + 門迴轉 + 牆 + 天井 + 樓梯 ───────────────────────────
def collect_active(spec) -> list[Obstacle]:
    """接進生成流程時實際偵測的 Obstacle 集合。

    Phase 1   = 家具(movable) + 門迴轉(static)。
    Phase 2   = 再加牆(kind=WALL,以 Room Polygon 為 barrier,判穿牆)。
    Phase 3-1 = 再加天井(kind=VOID,硬障礙,判家具掉進挑空)。
    Phase 3-2 = 再加樓梯(kind=STAIR,硬障礙,判家具壓住梯段)。
    Phase 3-3 = 再加柱(kind=COLUMN,**只偵測不修復**,見 column_obstacles)。"""
    return (fixture_obstacles(spec) + door_swing_obstacles(spec)
            + wall_obstacles(spec) + void_obstacles(spec)
            + stair_obstacles(spec) + column_obstacles(spec))
