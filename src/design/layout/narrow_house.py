"""Narrow-Frontage Townhouse(v0.7 Phase N1)—— 窄面寬透天產生器。

台灣常見的透天厝面寬只有 4~7 米、往深發展(12~20 米),房間**前後串聯**(不是
兩帶式的左右並排),**單樓梯**,兩側是與鄰房共用的界牆(不開窗)、只有前後與**中段
採光天井**採光。既有的 layout_generator 是「兩帶式」(南客廳帶+北臥室帶並排+東側
服務核),天生要 ≥10 米寬,做不出窄面寬透天。本模組是另一套骨架。

⚠️ 座標:x=面寬(東西),y=進深(南北);**前(臨路/入口)在 y 小的南側**,後院在
y 大的北側。使用者給的是**建築物尺寸**(不是基地);基地=建築+四周退縮,反推得到。

⚠️ 重用:牆/門/窗一律交給 bsp_layout.rooms_to_spec 由房間矩形推導(不重造)。本模組
只負責「窄深 envelope 怎麼切成前後串聯房間 + 中段核」與門窗的收尾修正。

骨架:
  * 前室(客廳 / 前臥,滿面寬,南向採光)
  * 中段核 = 天井(西,採光豎井)+ 樓梯間(內含單跑直梯,西側留通道)+ 浴室
    (東,樓上;面寬夠寬時擺得進核,讓後室維持滿寬)
  * 後室(1F=餐廳+廚房;樓上=滿面寬後臥,面寬不足時浴室退回後室東南角、後臥 L 形)
  * 垂直核**每層同位** → 樓梯天生上下對齊(符合柱網原則)

收尾修正(_fix_openings):去界牆窗、去天井門(採光井不設走入門)、浴室只留 1 門、
1F 加臨路前門。

N1 範圍:多層 + 單樓梯 + 天井 + 浴室 + 家具(Phase 6 擺位),能畫 DXF、能被 Phase 6
評分與 room_circulation 檢查。地下室/結構柱另議。

典型用法::

    floors = generate_narrow_building(7000, 12000, floors=3)   # 建築 7×12、三層
    for label, spec in floors:
        draw_floor_plan(msp, spec, layers)
"""
from __future__ import annotations

from shapely.geometry import Point, Polygon

from src.drafting.apartment_plan import DoorPlacement
from src.drafting.door_window import Door
from src.drafting.stair import Stair
from src.drafting.wall import Opening

from src.design.layout.bsp_layout import rooms_to_spec

# 建築線退縮(mm),對齊 HouseBrief.setback,反推基地用。
SETBACK = 2000.0

# 面寬 / 進深 合理範圍(mm)。窄面寬透天的定義域。
# ⚠️ 下限 5m:此骨架每層要放「兩臥室 + 浴室 + 天井 + 樓梯」,實測 <5m 就塞不下
# (浴室/後臥擠爆、動線斷)。真正 4~5m 的超窄透天是「單間進深串聯 + 浴室塞樓梯旁」
# 的另一種更緊的骨架,列為之後的工作。
MIN_WIDTH, MAX_WIDTH = 5000.0, 7000.0
MIN_DEPTH = 10500.0             # 三段(前+核+後)放得下 + 樓上後室夠住的下限

# 樓梯:單跑直梯,貼樓梯間東牆;西側留通道(前後動線)。
STAIR_W = 1100.0
STAIR_TREAD = 260.0
WALL_GAP = 75.0
MIN_PASSAGE = 900.0
STAIRWELL_W = STAIR_W + WALL_GAP + MIN_PASSAGE      # 樓梯間面寬(梯+通道)

# 採光天井(窄屋核只放天井+樓梯時的天井寬)。
WELL_W_MIN, WELL_W_MAX = 1400.0, 2200.0

# 浴室:面寬夠時擺進中段核(東);不足時退回後室東南角。
BATH_MIN_W, BATH_MAX_W = 1500.0, 2400.0
# 核放得下浴室的最小面寬(天井+樓梯間+浴室)。
CORE_BATH_MIN_W = STAIRWELL_W + WELL_W_MIN + BATH_MIN_W

ENTRY_WIDTH = 1000.0            # 臨路大門寬

# 三段進深:(段名, 分配權重, 最小進深mm)。前室 / 中段核 / 後室。
ZONES = [
    ("front", 0.38, 3300.0),
    ("core", 0.24, 3000.0),
    ("rear", 0.38, 3200.0),
]


def _split_depth(total_d: float, zones) -> list[float]:
    mins = [z[2] for z in zones]
    extra = max(0.0, total_d - sum(mins))
    wsum = sum(z[1] for z in zones) or 1.0
    return [m + extra * z[1] / wsum for z, m in zip(zones, mins)]


def _well_width(W: float) -> float:
    return min(max(W * 0.30, WELL_W_MIN), WELL_W_MAX)


def _rect(x0, y0, x1, y1):
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


def _core_widths(W: float, with_east: bool):
    """中段核各段面寬 (天井, 樓梯間, 東格);東格=0 表示核裝不下(超窄屋)。"""
    if with_east and W >= CORE_BATH_MIN_W:
        east = min(max((W - STAIRWELL_W) * 0.4, BATH_MIN_W), BATH_MAX_W)
        east = min(east, W - STAIRWELL_W - WELL_W_MIN)     # 保住天井最小寬
        return W - STAIRWELL_W - east, STAIRWELL_W, east
    return _well_width(W), W - _well_width(W), 0.0


def _stair(x_east, y1, y2, label):
    """樓梯間內單跑直梯:貼東牆(x_east,西側留通道),往北跑。"""
    length = (y2 - y1) - 2 * WALL_GAP
    steps = max(2, int(length / STAIR_TREAD))
    return Stair(origin=(x_east - WALL_GAP - STAIR_W, y1 + WALL_GAP),
                 width=STAIR_W, length=length, direction="north",
                 steps=steps, tread=STAIR_TREAD, label=label)


def _core(bx0, bx1, y1, y2, label, east_kind, east_name):
    """中段核 → (房間清單, 樓梯);天井(西)| 樓梯間 | 東側服務格(浴室/儲藏)。

    east 格每層同寬同位 → 樓梯 origin 固定 → 上下對齊。"""
    W = bx1 - bx0
    well, sw, east_w = _core_widths(W, with_east=True)
    xw = bx0 + well                     # 天井東緣 = 樓梯間西牆
    xs = xw + sw                        # 樓梯間東緣(= 東格西牆)
    rooms = [("patio", "天井", _rect(bx0, y1, xw, y2)),
             ("stair_hall", "樓梯間", _rect(xw, y1, xs, y2))]
    if east_w > 0:
        rooms.append((east_kind, east_name, _rect(xs, y1, bx1, y2)))
    return rooms, _stair(xs, y1, y2, label)


def _floor_rooms(level, top, bx0, by0, bx1, by1):
    """一層的房間矩形 + 樓梯。"""
    d_front, d_core, _ = _split_depth(by1 - by0, ZONES)
    y1, y2 = by0 + d_front, by0 + d_front + d_core
    label = "下" if level == top else "上"

    # 核每層同構(天井+樓梯間+東格)→ 樓梯上下對齊;1F 東格=儲藏(梯下收納,不擠家具),
    # 樓上=浴室(暗區,天井旁)。
    if level == 1:                                  # 1F:客廳 / 核 / 餐廳|廚房
        core, stair = _core(bx0, bx1, y1, y2, label, "storage", "儲藏")
        xm = (bx0 + bx1) / 2                         # 後段左右分:餐廳(西)| 廚房(東)
        rooms = [("living", "客廳", _rect(bx0, by0, bx1, y1)), *core,
                 ("dining", "餐廳", _rect(bx0, y2, xm, by1)),
                 ("kitchen", "廚房", _rect(xm, y2, bx1, by1))]
        return rooms, stair

    core, stair = _core(bx0, bx1, y1, y2, label, "bathroom", "浴室")
    return [("bedroom", f"前臥室{level}F", _rect(bx0, by0, bx1, y1)), *core,
            ("bedroom", f"後臥室{level}F", _rect(bx0, y2, bx1, by1))], stair


# ── 開口收尾:去界牆窗 / 去天井門 / 浴室單門 / 加前門 ────────────────────────
def _is_party_wall(wall, bx0, bx1) -> bool:
    (sx, _), (ex, _) = wall.start, wall.end
    if abs(sx - ex) > 1.0:
        return False
    return abs(sx - bx0) < 50.0 or abs(sx - bx1) < 50.0


def _door_kinds(spec, dp) -> list:
    """一扇門兩側鄰接的房間 kind(往牆法線兩側各探 150mm)。"""
    w = spec.walls[dp.wall_index]
    op = w.openings[dp.opening_index]
    cx, cy = w.point_at(op.position)
    nx, ny = w.normal_vector
    out = []
    for s in (1, -1):
        p = Point(cx + nx * 150 * s, cy + ny * 150 * s)
        for r in spec.rooms:
            if Polygon(r.points).contains(p):
                out.append(r.kind)
                break
    return out


def _remove_openings(spec, targets: set):
    """安全刪除一組 (wall_index, opening_index):重建各牆洞口序列並重映射門/窗索引。"""
    if not targets:
        return
    by_wall: dict = {}
    for wi, oi in targets:
        by_wall.setdefault(wi, set()).add(oi)
    remap: dict = {}
    for wi, wall in enumerate(spec.walls):
        drop = by_wall.get(wi, set())
        kept = []
        for oi, op in enumerate(wall.openings):
            if oi in drop:
                remap[(wi, oi)] = None
            else:
                remap[(wi, oi)] = len(kept)
                kept.append(op)
        wall.openings = kept
    spec.doors = [dp for dp in spec.doors
                  if remap.get((dp.wall_index, dp.opening_index)) is not None]
    for dp in spec.doors:
        dp.opening_index = remap[(dp.wall_index, dp.opening_index)]
    spec.windows = [wp for wp in spec.windows
                    if remap.get((wp.wall_index, wp.opening_index)) is not None]
    for wp in spec.windows:
        wp.opening_index = remap[(wp.wall_index, wp.opening_index)]


# 浴室留門時,偏好開向的公共鄰室(越前面越優先)。
_BATH_DOOR_PREF = ("stair_hall", "corridor", "living", "dining", "kitchen")


def _fix_openings(spec, bx0, by0, bx1, level):
    """去重複門、去界牆窗、去天井門、浴室只留 1 門;1F 補臨路前門。"""
    remove: set = set()

    # 重複門:同一位置被開了兩扇(相鄰兩室互開)→ 只留一扇
    seen: dict = {}
    for dp in spec.doors:
        w = spec.walls[dp.wall_index]
        px, py = w.point_at(w.openings[dp.opening_index].position)
        key = (round(px / 10), round(py / 10))
        if key in seen:
            remove.add((dp.wall_index, dp.opening_index))
        else:
            seen[key] = True

    # 界牆(東西共用牆)不開窗
    for wi, w in enumerate(spec.walls):
        if _is_party_wall(w, bx0, bx1):
            for oi, op in enumerate(w.openings):
                if op.kind == "window":
                    remove.add((wi, oi))

    # 天井不設走入門(採光豎井)
    for dp in spec.doors:
        if "patio" in _door_kinds(spec, dp):
            remove.add((dp.wall_index, dp.opening_index))

    # 每間浴室只留 1 門(偏好開向公共鄰室,其餘刪掉)
    for room in [r for r in spec.rooms if r.kind == "bathroom"]:
        bp = Polygon(room.points)
        adj = [dp for dp in spec.doors
               if bp.exterior.distance(
                   Point(spec.walls[dp.wall_index].point_at(
                       spec.walls[dp.wall_index]
                       .openings[dp.opening_index].position))) < 50.0]
        if len(adj) > 1:
            def _pref(dp):
                ks = set(_door_kinds(spec, dp)) - {"bathroom"}
                return next((i for i, k in enumerate(_BATH_DOOR_PREF)
                             if k in ks), len(_BATH_DOOR_PREF))
            adj.sort(key=_pref)
            for dp in adj[1:]:
                remove.add((dp.wall_index, dp.opening_index))

    _remove_openings(spec, remove)

    if level == 1:                                  # 臨路大門:南向外牆西段
        _add_front_door(spec, bx0, by0, bx1)


def _add_front_door(spec, bx0, by0, bx1):
    """在南向(臨路)外牆上加一扇大門,避開既有洞口。"""
    for wi, w in enumerate(spec.walls):
        (sx, sy), (ex, ey) = w.start, w.end
        if abs(sy - by0) > 50 or abs(ey - by0) > 50 or abs(sx - ex) < 1:
            continue                                # 只找南向水平外牆
        length = w.length
        taken = [(op.position - op.width / 2, op.position + op.width / 2)
                 for op in w.openings]
        for pos in (length * 0.22, length * 0.78, length * 0.5):
            a, b = pos - ENTRY_WIDTH / 2, pos + ENTRY_WIDTH / 2
            if a < 0 or b > length:
                continue
            if all(b < t0 or a > t1 for t0, t1 in taken):    # 不撞既有洞口
                w.openings.append(Opening(pos, ENTRY_WIDTH, "door"))
                spec.doors.append(DoorPlacement(
                    wi, len(w.openings) - 1, Door(hinge="left", swing="in")))
                return
        return                                      # 這面牆塞不下就算了(罕見)


def _build_floor(level, top, W, D, floor_label, furnish=True):
    """組一層 spec(房間 → 牆/門/窗 + 樓梯 + 開口收尾 + 家具)。"""
    bx0 = by0 = SETBACK
    bx1, by1 = SETBACK + W, SETBACK + D
    site_w, site_d = W + 2 * SETBACK, D + 2 * SETBACK
    rooms, stair = _floor_rooms(level, top, bx0, by0, bx1, by1)
    spec = rooms_to_spec(rooms, (bx0, by0, bx1, by1), site_w, site_d,
                         setback=SETBACK)
    _fix_openings(spec, bx0, by0, bx1, level)
    spec.stairs = [stair]
    spec.floor_label = floor_label
    # 建築外框當「單跨」記進格線(不放柱):讓 metrics/摘要讀得到建築尺寸與院深。
    spec.x_spacings = [W]
    spec.y_spacings = [D]
    spec.grid_origin = (bx0, by0)
    if furnish:                                     # 家具:沿用 Phase 6 擺位(必合法)
        from src.design.layout.auto_furnish import furnish_spec
        furnish_spec(spec)
    return spec


def _check_dims(W, D):
    if not MIN_WIDTH <= W <= MAX_WIDTH:
        raise ValueError(
            f"窄面寬透天面寬需 {MIN_WIDTH/1000:.1f}~{MAX_WIDTH/1000:.1f}m,收到 "
            f"{W/1000:.1f}m(更寬請用一般兩帶式產生器)")
    if D < MIN_DEPTH:
        raise ValueError(
            f"窄面寬透天進深需 ≥{MIN_DEPTH/1000:.1f}m,收到 {D/1000:.1f}m")


def generate_narrow_building(building_w_mm: float, building_d_mm: float, *,
                             floors: int = 3, bedrooms: int = 3,
                             furnish: bool = True):
    """窄面寬透天多層 → [(樓層標示, FloorPlanSpec)]。

    每層共用同一垂直核(天井+樓梯間[+浴室]),樓梯上下對齊,並配家具(Phase 6 擺位)。
    building_w/d 是**建築物**尺寸,基地由退縮反推。頂層樓梯標「下」,其餘標「上」。"""
    W, D = float(building_w_mm), float(building_d_mm)
    _check_dims(W, D)
    floors = max(1, int(floors))
    return [(f"{lv}F", _build_floor(lv, floors, W, D, f"{lv}F", furnish))
            for lv in range(1, floors + 1)]


def generate_narrow_house(building_w_mm: float, building_d_mm: float, *,
                          bedrooms: int = 3, floor_label: str = "1F",
                          furnish: bool = True):
    """窄面寬透天單層 1F(便捷入口,回單一 FloorPlanSpec;含樓梯核+家具)。"""
    W, D = float(building_w_mm), float(building_d_mm)
    _check_dims(W, D)
    return _build_floor(1, 1, W, D, floor_label, furnish)
