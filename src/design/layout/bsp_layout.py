"""BSP Layout → FloorPlanSpec(v0.7 Phase 7.1a)—— 把「刀位」切出的房間變成能畫的圖。

`src.ml.bsp.assemble()` 用 10 個刀位 + 2 開關,切出**保證不重疊、剛好鋪滿**的房間
多邊形(公尺)。但那只是「房間分區」,沒有牆/門/窗,無法評分也無法出 DXF。本模組
補上這一段:

    θ(刀位)→ assemble 房間 → 推牆(共用邊=內牆、外框=外牆)→ 門/窗 → FloorPlanSpec

如此一來,隨機刀位就能產生**結構不同**的合法平面,交給 Phase 6 評分、交給 DXF 輸出
(Phase 7 的多樣生成引擎)。

⚠️ 7.1a 範圍:牆/門/窗 → 能畫 DXF 的合法 spec(**不含家具、不含柱**;家具是 7.1b、
搜尋是 7.1c)。牆由房間邊界幾何推得(不靠既有產生器的骨架),故任何 BSP 分區都適用。

⚠️ 座標:assemble 用公尺;spec 用 mm(本模組轉換)。房間放在「建築範圍」內
(退縮後),site_boundary 仍是整塊基地。
"""
from __future__ import annotations

from shapely.geometry import Polygon

from src.drafting.apartment_plan import (
    DoorPlacement,
    FloorPlanSpec,
    WindowPlacement,
)
from src.drafting.door_window import Door, Window
from src.drafting.room import Room
from src.drafting.wall import (
    EXTERIOR_WALL_THICKNESS,
    INTERIOR_WALL_THICKNESS,
    Opening,
    Wall,
)
from src.ml.bsp import N_FLAGS, N_THETA, assemble, building_rect

# BSP slot → 房間用途 / 顯示名。
SLOT_KIND = {
    "master": "bedroom", "bedA": "bedroom", "bedB": "bedroom", "bedC": "bedroom",
    "ensuite": "bathroom", "corridor": "corridor", "foyer": "foyer",
    "living": "living", "dining": "dining", "bath": "bathroom", "kitchen": "kitchen",
}
SLOT_NAME = {
    "master": "主臥室", "bedA": "臥室A", "bedB": "臥室B", "bedC": "臥室C",
    "ensuite": "主臥浴", "corridor": "走道", "foyer": "玄關",
    "living": "客廳", "dining": "餐廳", "bath": "浴廁", "kitchen": "廚房",
}

# 幾何容差(mm):座標吸附、共線判定。
SNAP = 1.0
# 門 / 窗 尺寸(mm)。
DOOR_WIDTH = 850.0
WINDOW_WIDTH = 1200.0
# 房間要留多少邊長才值得開門/窗(避免在超短共用邊硬塞)。
MIN_SHARED_FOR_DOOR = DOOR_WIDTH + 300.0
MIN_EDGE_FOR_WINDOW = WINDOW_WIDTH + 300.0

# 門優先開向哪種鄰室(越前面越優先)。
PUBLIC_PRIORITY = ("corridor", "foyer", "living", "dining", "kitchen")


def _snap(v: float) -> float:
    return round(v / SNAP) * SNAP


def _largest_poly(geom):
    """房間可能被切成 MultiPolygon(刀位讓某房分裂)——取面積最大的那塊。"""
    if geom.geom_type == "Polygon":
        return geom
    return max(geom.geoms, key=lambda g: g.area)


# ── 幾何小工具 ──────────────────────────────────────────────────────────────
def _axis_edges(poly: Polygon):
    """一個軸對齊多邊形 → 它的邊:('V', x, y0, y1) 垂直 / ('H', y, x0, x1) 水平。"""
    coords = list(poly.exterior.coords)
    out = []
    for (x1, y1), (x2, y2) in zip(coords, coords[1:]):
        x1, y1, x2, y2 = _snap(x1), _snap(y1), _snap(x2), _snap(y2)
        if abs(x1 - x2) <= SNAP and abs(y1 - y2) > SNAP:      # 垂直
            out.append(("V", x1, min(y1, y2), max(y1, y2)))
        elif abs(y1 - y2) <= SNAP and abs(x1 - x2) > SNAP:    # 水平
            out.append(("H", y1, min(x1, x2), max(x1, x2)))
    return out


def _union_intervals(intervals):
    """把 [(a,b)…] 重疊/相接的區間聯集成最少段。"""
    if not intervals:
        return []
    intervals = sorted(intervals)
    merged = [list(intervals[0])]
    for a, b in intervals[1:]:
        if a <= merged[-1][1] + SNAP:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return [(a, b) for a, b in merged]


# ── 推牆 ────────────────────────────────────────────────────────────────────
def _derive_walls(rooms_mm, build_rect_mm):
    """由房間邊界推出牆(合併共線段;外框=外牆厚、內部=內牆厚)。

    回 (walls, vlines, hlines):walls 是 list[Wall];vlines/hlines 供門窗定位
    (x→[(y0,y1,wall_index)] / y→[(x0,x1,wall_index)])。"""
    bx0, by0, bx1, by1 = build_rect_mm
    v_by_x: dict[float, list] = {}
    h_by_y: dict[float, list] = {}
    for _slot, poly in rooms_mm:
        for kind, c, a, b in _axis_edges(poly):
            (v_by_x if kind == "V" else h_by_y).setdefault(c, []).append((a, b))

    walls: list[Wall] = []
    vlines: dict[float, list] = {}
    hlines: dict[float, list] = {}

    def _is_ext_x(x):
        return abs(x - bx0) <= SNAP or abs(x - bx1) <= SNAP

    def _is_ext_y(y):
        return abs(y - by0) <= SNAP or abs(y - by1) <= SNAP

    for x, ivals in sorted(v_by_x.items()):
        for y0, y1 in _union_intervals(ivals):
            th = EXTERIOR_WALL_THICKNESS if _is_ext_x(x) else INTERIOR_WALL_THICKNESS
            walls.append(Wall(start=(x, y0), end=(x, y1), thickness=th))
            vlines.setdefault(x, []).append((y0, y1, len(walls) - 1))
    for y, ivals in sorted(h_by_y.items()):
        for x0, x1 in _union_intervals(ivals):
            th = EXTERIOR_WALL_THICKNESS if _is_ext_y(y) else INTERIOR_WALL_THICKNESS
            walls.append(Wall(start=(x0, y), end=(x1, y), thickness=th))
            hlines.setdefault(y, []).append((x0, x1, len(walls) - 1))
    return walls, vlines, hlines


# ── 主轉換 ──────────────────────────────────────────────────────────────────
def rooms_to_spec(named_rooms, build_rect_mm, site_w_mm: float,
                  site_d_mm: float, setback: float) -> FloorPlanSpec:
    """一組 (kind, name, 角點mm) 房間矩形 → 可畫 DXF 的 FloorPlanSpec(牆/門/窗)。

    共用的「房間 → 牆/門/窗」推導(不重造):牆由房間邊界幾何推得(外框=外牆、
    共用邊=內牆)、門開向最公共的鄰室、窗開在外框。BSP(bsp_to_spec)與其他「房間
    矩形」產生器(如窄面寬透天 narrow_house)都接這裡。**不含家具/柱**。"""
    rooms_mm = []
    rooms: list[Room] = []
    for kind, name, pts in named_rooms:
        rooms_mm.append((kind, Polygon(pts)))
        rooms.append(Room(name=name, points=pts, kind=kind))

    walls, vlines, hlines = _derive_walls(rooms_mm, build_rect_mm)
    doors = _place_doors(rooms_mm, walls, vlines, hlines)
    windows = _place_windows(rooms_mm, walls, vlines, hlines, build_rect_mm)

    return FloorPlanSpec(
        site_boundary=[(0.0, 0.0), (site_w_mm, 0.0),
                       (site_w_mm, site_d_mm), (0.0, site_d_mm)],
        setback=setback,
        walls=walls, rooms=rooms, doors=doors, windows=windows,
        column_centers=[],                  # 幾何產生器不放柱
    )


def bsp_to_spec(theta, flags, site_w_mm: float, site_d_mm: float,
                bedrooms: int) -> FloorPlanSpec:
    """θ(刀位)+ flags → 可畫 DXF 的 FloorPlanSpec(牆/門/窗;7.1a 不含家具/柱)。"""
    site_w_m, site_d_m = site_w_mm / 1000.0, site_d_mm / 1000.0
    rooms_m = assemble(theta, flags, site_w_m, site_d_m, bedrooms)

    # slot → (kind, name, 角點mm)。
    named_rooms = []
    for slot, poly in rooms_m:
        pts = [(_snap(x * 1000.0), _snap(y * 1000.0))
               for x, y in _largest_poly(poly).exterior.coords[:-1]]
        named_rooms.append((SLOT_KIND.get(slot, slot),
                            SLOT_NAME.get(slot, slot), pts))

    bx0, by0, bx1, by1 = (v * 1000.0 for v in building_rect(site_w_m, site_d_m))
    return rooms_to_spec(named_rooms, (bx0, by0, bx1, by1),
                         site_w_mm, site_d_mm, setback=bx0)


# ── 門 / 窗 ──────────────────────────────────────────────────────────────────
def _shared_edge(a: Polygon, b: Polygon):
    """兩房共用的最長軸對齊邊 → ('V', x, y0, y1, len) / ('H', y, x0, x1, len);
    沒有夠長的共用邊回 None。"""
    inter = a.exterior.intersection(b.exterior)
    if inter.is_empty:
        return None
    geoms = list(getattr(inter, "geoms", [inter]))
    best, blen = None, 0.0
    for g in geoms:
        if g.geom_type == "LineString" and g.length > blen:
            best, blen = g, g.length
    if best is None:
        return None
    x0, y0, x1, y1 = best.bounds
    if abs(x1 - x0) <= SNAP:                                # 垂直共用邊
        return ("V", _snap(x0), _snap(y0), _snap(y1), blen)
    return ("H", _snap(y0), _snap(x0), _snap(x1), blen)


def _wall_for(kind, line, a, b, vlines, hlines):
    """找出覆蓋 (line, [a,b]) 這段的牆 index。"""
    mid = (a + b) / 2
    for lo, hi, wi in (vlines if kind == "V" else hlines).get(line, []):
        if lo - SNAP <= mid <= hi + SNAP:
            return wi
    return None


def _add_opening(walls, wi, kind, line, a, b, width, opening_kind):
    """在 walls[wi] 上、(line,[a,b]) 段的中點開一個 width 的洞口;回 opening_index。"""
    wall = walls[wi]
    mid = (a + b) / 2
    start_along = wall.start[1] if kind == "V" else wall.start[0]
    pos = mid - start_along
    wall.openings.append(Opening(position=pos, width=width, kind=opening_kind))
    return len(wall.openings) - 1


def _place_doors(rooms_mm, walls, vlines, hlines):
    """每個「私密房」開一扇門到最公共的鄰室(走道>玄關>客廳>餐廳>廚房>任意)。"""
    doors: list[DoorPlacement] = []
    for i, (slot, poly) in enumerate(rooms_mm):
        if slot in ("living", "foyer", "corridor"):        # 公共空間本身不需門
            continue
        best = None                                         # (priority, -len, edge)
        for j, (oslot, opoly) in enumerate(rooms_mm):
            if i == j:
                continue
            se = _shared_edge(poly, opoly)
            if se is None or se[4] < MIN_SHARED_FOR_DOOR:
                continue
            okind = SLOT_KIND.get(oslot, oslot)
            pri = PUBLIC_PRIORITY.index(okind) if okind in PUBLIC_PRIORITY \
                else len(PUBLIC_PRIORITY)
            cand = (pri, -se[4], se)
            if best is None or cand[:2] < best[:2]:
                best = cand
        if best is None:
            continue
        kind, line, a, b, _ = best[2]
        wi = _wall_for(kind, line, a, b, vlines, hlines)
        if wi is None:
            continue
        oi = _add_opening(walls, wi, kind, line, a, b, DOOR_WIDTH, "door")
        doors.append(DoorPlacement(wall_index=wi, opening_index=oi, door=Door()))
    return doors


def _place_windows(rooms_mm, walls, vlines, hlines, build_rect_mm):
    """每個有外牆臨接的房間,在最長那道外牆邊開一扇窗(走道/玄關除外)。"""
    bx0, by0, bx1, by1 = build_rect_mm
    windows: list[WindowPlacement] = []
    for slot, poly in rooms_mm:
        if slot in ("corridor", "foyer"):
            continue
        best = None                                         # (len, kind, line, a, b)
        for kind, c, a, b in _axis_edges(poly):
            ext = (kind == "V" and (abs(c - bx0) <= SNAP or abs(c - bx1) <= SNAP)) \
                or (kind == "H" and (abs(c - by0) <= SNAP or abs(c - by1) <= SNAP))
            if ext and (b - a) >= MIN_EDGE_FOR_WINDOW:
                if best is None or (b - a) > best[0]:
                    best = (b - a, kind, c, a, b)
        if best is None:
            continue
        _, kind, line, a, b = best
        wi = _wall_for(kind, line, a, b, vlines, hlines)
        if wi is None:
            continue
        oi = _add_opening(walls, wi, kind, line, a, b, WINDOW_WIDTH, "window")
        windows.append(WindowPlacement(wall_index=wi, opening_index=oi,
                                       window=Window()))
    return windows


# ── 隨機 θ 便利函式(給 7.1c 搜尋用)───────────────────────────────────────
def random_theta(rng):
    """回 (theta[10], flags[2]):給定一個 numpy Generator,抽一組隨機刀位。"""
    return rng.random(N_THETA), (rng.random(N_FLAGS) > 0.5).astype(float)
