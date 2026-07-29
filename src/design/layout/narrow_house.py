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

import itertools

from shapely.geometry import Point, Polygon

from src.drafting.apartment_plan import DoorPlacement
from src.drafting.door_window import Door
from src.drafting.stair import UStair
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

# 樓梯:U 形折返梯(填滿樓梯間)。中段核只有 ~3.6m 深,單跑直梯爬一層會太陡
# (需 ~4.7m 才緩);折返梯上一段+平台轉身+再上一段,同深度內每階升高正常(~178mm)。
STAIR_TREAD = 260.0
WALL_GAP = 75.0
STAIR_WELL_GAP = 100.0                              # 兩梯段間的梯井縫
STAIRWELL_W = 2075.0                                # 樓梯間面寬(容兩梯段,各 ~910)
FLOOR_HEIGHT = 3200.0                               # 層高(算級數/每階升高用)
MAX_RISER = 190.0                                   # 每階升高上限(住宅舒適下限步距)

# 採光天井(窄屋核只放天井+樓梯時的天井寬)。
WELL_W_MIN, WELL_W_MAX = 1400.0, 2200.0

# 浴室:面寬夠時擺進中段核(東);不足時退回後室東南角。
BATH_MIN_W, BATH_MAX_W = 1500.0, 2400.0
# 核放得下浴室的最小面寬(天井+樓梯間+浴室)。
CORE_BATH_MIN_W = STAIRWELL_W + WELL_W_MIN + BATH_MIN_W

ENTRY_WIDTH = 1000.0            # 臨路大門寬
INTERIOR_DOOR_WIDTH = 850.0     # 補內門寬(對齊 bsp_layout.DOOR_WIDTH)
# 補門保證的例外:機電豎管、採光天井本來就不設走入門(封閉服務豎井)。
NO_DOOR_KINDS = {"pipe_shaft", "patio"}
# 補門時偏好接的鄰室(越公共/動線越優先);越前面優先度越高。
_DOOR_NEIGHBOR_PREF = ("corridor", "foyer", "stair_hall", "living", "dining",
                       "kitchen", "stair")

# 三段進深:(段名, 分配權重, 最小進深mm)。前室 / 中段核 / 後室。
ZONES = [
    ("front", 0.38, 3300.0),
    ("core", 0.24, 3500.0),        # 容得下 U 梯(梯跑 + 轉身平台)
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


def _stair(x_west, x_east, y1, y2, label):
    """樓梯間內 U 形折返梯(填滿 [x_west,x_east]×[y1,y2]),往北上。

    級數由層高回推、每階升高 ≤MAX_RISER(住宅舒適);分兩段折返,故梯跑只需一半深度,
    塞得進淺的中段核。"""
    total = max(4, -(-int(FLOOR_HEIGHT) // int(MAX_RISER)))      # ceil,爬完一層的總級數
    spf = max(2, -(-total // 2))                                 # 每段級數(向上取半)
    return UStair(origin=(x_west + WALL_GAP, y1 + WALL_GAP),
                  width=(x_east - x_west) - 2 * WALL_GAP,
                  length=(y2 - y1) - 2 * WALL_GAP, direction="north",
                  steps_per_flight=spf, tread=STAIR_TREAD,
                  well_gap=STAIR_WELL_GAP, label=label)


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
    return rooms, _stair(xw, xs, y1, y2, label)


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


def _fix_openings(spec, bx0, by0, bx1, level, party_walls: bool = True):
    """去重複門、去界牆窗、去天井門、浴室只留 1 門;1F 補臨路前門。

    party_walls:東西外牆是不是與鄰房共用的界牆(透天=True,不開窗)。獨棟(中庭
    骨架)給 False → 四面都可開窗,否則整棟只剩前後採光、房間會變暗。"""
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

    # 界牆(東西共用牆)不開窗——獨棟不適用(party_walls=False 時四面都能開)
    if party_walls:
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
        # 先試慣用比例,再沿牆掃描 → 一定落在「不撞洞口 且 離每間房角落夠遠」的位置。
        step = 100.0
        n = max(1, int(length / step))
        cands = [length * 0.22, length * 0.78, length * 0.5]
        cands += [i * step for i in range(1, n)]
        for clear in DOOR_CLEAR_STEPS:              # 先求舒適淨距,不行再放寬
            for pos in cands:
                a, b = pos - ENTRY_WIDTH / 2, pos + ENTRY_WIDTH / 2
                if a < 0 or b > length:
                    continue
                if not all(b < t0 or a > t1 for t0, t1 in taken):   # 撞既有洞口
                    continue
                if not _door_pos_ok(spec, w, pos, ENTRY_WIDTH, clear):
                    continue                        # 卡在房間角落
                w.openings.append(Opening(pos, ENTRY_WIDTH, "door"))
                spec.doors.append(DoorPlacement(
                    wi, len(w.openings) - 1, Door(hinge="left", swing="in")))
                return
        return                                      # 這面牆塞不下就算了(罕見)


DOOR_TOUCH_TOL = 60.0           # 門洞中心離房間邊界多近算「這扇門開在這間房上」
# 門洞兩端離「垂直方向的牆(房間角落)」的最小淨距(mm)。門擠在角落時,人走不進
# 那個角(房內可站區是牆內縮半個通行寬,角落구域接不上門)→ 動線檢查會判不通。
DOOR_CORNER_CLEAR = 350.0
# 分級退讓:先求舒適淨距,擺不下就放寬;最後一級是「動線仍走得通」的物理下限
# (房內可站區是牆內縮半個通行寬,門離角落太近就接不上那塊 → 判動線不通)。
DOOR_CLEAR_STEPS = (350.0, 250.0, 150.0, 100.0)
DOOR_CORNER_MIN = 90.0          # 檢查器判定「卡死在角落」的門檻(動線下限 ~67mm)


def _room_edge_span(room_poly, wall, along_start):
    """牆與這間房相鄰的那一段在牆上的範圍 → (lo, hi) 沿牆座標;不相鄰回 None。"""
    (sx, sy), (ex, ey) = wall.start, wall.end
    rx0, ry0, rx1, ry1 = room_poly.bounds
    if abs(sx - ex) < 1.0:                              # 垂直牆
        if not (abs(sx - rx0) < DOOR_TOUCH_TOL or abs(sx - rx1) < DOOR_TOUCH_TOL):
            return None
        lo, hi = max(min(sy, ey), ry0), min(max(sy, ey), ry1)
    elif abs(sy - ey) < 1.0:                            # 水平牆
        if not (abs(sy - ry0) < DOOR_TOUCH_TOL or abs(sy - ry1) < DOOR_TOUCH_TOL):
            return None
        lo, hi = max(min(sx, ex), rx0), min(max(sx, ex), rx1)
    else:
        return None
    if hi - lo <= 0:
        return None
    return abs(lo - along_start), abs(hi - along_start)


def _door_pos_ok(spec, wall, pos: float, width: float,
                 clear: float = DOOR_CORNER_CLEAR) -> bool:
    """這個門洞位置合不合格:對**兩側每一間房**都要離房間角落 ≥ clear。

    這條規則所有開門路徑共用(前門/補門/接通用門),避免「門卡在牆角、人走不進去」
    ——那正是動線檢查會判不通、但看圖不明顯的錯誤。"""
    (sx, sy), (ex, ey) = wall.start, wall.end
    along_start = sy if abs(sx - ex) < 1.0 else sx
    px, py = wall.point_at(pos)
    a, b = pos - width / 2, pos + width / 2
    for room in spec.rooms:
        poly = Polygon(room.points)
        if poly.exterior.distance(Point(px, py)) > DOOR_TOUCH_TOL:
            continue                                    # 這扇門不在這間房的邊界上
        span = _room_edge_span(poly, wall, along_start)
        if span is None:
            continue
        lo, hi = min(span), max(span)
        if a < lo + clear - 1e-6 or b > hi - clear + 1e-6:
            return False                                # 太靠近這間房的角落
    return True
# 需要對外採光通風的房間(補窗保證的對象);走道/樓梯/儲藏/豎井不強制。
WINDOW_KINDS = {"living", "dining", "kitchen", "bedroom", "master_bedroom",
                "study", "elder_room", "bathroom"}


def _ensure_room_windows(spec, bx0, by0, bx1, by1, party_walls: bool = True) -> int:
    """保證居室有窗:沒窗的房間在「前後外牆」或「天井側」補一扇。

    為什麼需要:透天是共壁,東西外牆不能開窗(_fix_openings 會刪),但配窗時是挑
    「最長的外牆邊」——深長的客廳最長邊正是東側共壁,窗開了又被刪 → 房間全暗。
    這裡在刪窗收尾後補回:優先前後外牆(真採光面),其次開向天井(台灣透天的
    標準做法:內側廚衛靠天井採光通風)。回補了幾扇窗。"""
    from src.design.layout.bsp_layout import MIN_EDGE_FOR_WINDOW, WINDOW_WIDTH
    from src.drafting.apartment_plan import WindowPlacement
    from src.drafting.door_window import Window

    patios = [Polygon(r.points) for r in spec.rooms if r.kind == "patio"]
    added = 0
    for room in spec.rooms:
        if room.kind not in WINDOW_KINDS:
            continue
        poly = Polygon(room.points)
        if any(op.kind == "window"
               and poly.exterior.distance(Point(*w.point_at(op.position))) < 60
               for w in spec.walls for op in w.openings):
            continue                                    # 已有窗
        rx0, ry0, rx1, ry1 = poly.bounds
        best = None                                     # (rank, -邊長, wi, lo, hi, along)
        for wi, w in enumerate(spec.walls):
            (sx, sy), (ex, ey) = w.start, w.end
            vertical = abs(sx - ex) < 1.0
            if vertical:
                if not (abs(sx - rx0) < 60 or abs(sx - rx1) < 60):
                    continue
                lo, hi, along = (max(min(sy, ey), ry0), min(max(sy, ey), ry1), sy)
                # 透天:東西向=共壁不開窗;獨棟:東西外牆也是採光面
                on_ext_ns = (not party_walls
                             and (abs(sx - bx0) < 60 or abs(sx - bx1) < 60))
                mid_pt = lambda m: (sx, m)              # noqa: E731
            else:
                if not (abs(sy - ry0) < 60 or abs(sy - ry1) < 60):
                    continue
                lo, hi, along = (max(min(sx, ex), rx0), min(max(sx, ex), rx1), sx)
                on_ext_ns = abs(sy - by0) < 60 or abs(sy - by1) < 60
                mid_pt = lambda m: (m, sy)              # noqa: E731
            if hi - lo < MIN_EDGE_FOR_WINDOW:
                continue
            if on_ext_ns:
                rank = 0                                # 前後外牆:真正的採光面
            else:
                mx, my = mid_pt((lo + hi) / 2)          # 牆另一側是不是天井?
                near = [Point(mx + dx, my + dy)
                        for dx, dy in ((250, 0), (-250, 0), (0, 250), (0, -250))]
                if any(p.contains(q) for p in patios for q in near):
                    rank = 1                            # 天井側:內間的採光通風井
                else:
                    continue                            # 內牆:開窗無意義
            cand = (rank, -(hi - lo), wi, lo, hi, along)
            if best is None or cand[:2] < best[:2]:
                best = cand
        if best is None:
            continue                                    # 真的沒採光面(內間,由 critique 回報)
        _rank, _l, wi, lo, hi, along = best
        w = spec.walls[wi]
        taken = [(op.position - op.width / 2, op.position + op.width / 2)
                 for op in w.openings]
        for frac in (0.5, 0.35, 0.65, 0.25, 0.75):
            m = lo + (hi - lo) * frac
            pos = abs(m - along)
            a, b = pos - WINDOW_WIDTH / 2, pos + WINDOW_WIDTH / 2
            if a < 0 or b > w.length:
                continue
            if all(b < t0 or a > t1 for t0, t1 in taken):
                w.openings.append(Opening(pos, WINDOW_WIDTH, "window"))
                spec.windows.append(
                    WindowPlacement(wi, len(w.openings) - 1, Window()))
                added += 1
                break
    return added


def _door_touching_rooms(spec, wall, op, polys):
    """這個門洞貼到哪幾間房(2 間=內門、1 間=外門)。"""
    p = Point(*wall.point_at(op.position))
    return [i for i, (_r, poly) in enumerate(polys)
            if poly.exterior.distance(p) < DOOR_TOUCH_TOL]


def _room_components(spec):
    """靠門/通道互通的房間分群 → [set(房間index)](豎井/天井不算,它們本來就封閉)。

    這是「從大門走不走得到」的判準:一層若分成兩群,代表其中一群只能從室外進,
    室內走不過去——真實住宅不會這樣。"""
    polys = [(r, Polygon(r.points)) for r in spec.rooms]
    live = [i for i, (r, _p) in enumerate(polys) if r.kind not in NO_DOOR_KINDS]
    adj = {i: set() for i in live}
    for w in spec.walls:
        for op in w.openings:
            if op.kind != "door":
                continue
            touch = [i for i in _door_touching_rooms(spec, w, op, polys)
                     if i in adj]
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


def _wall_covering(spec, kind, line, a, b):
    """找出覆蓋共用邊 (kind, line, [a,b]) 的那道牆 → (wall_index, 沿牆位置) 或 None。"""
    mid = (a + b) / 2
    for wi, w in enumerate(spec.walls):
        (sx, sy), (ex, ey) = w.start, w.end
        if kind == "V":
            if abs(sx - ex) > 1.0 or abs(sx - line) > DOOR_TOUCH_TOL:
                continue
            lo, hi, start_along = min(sy, ey), max(sy, ey), sy
        else:
            if abs(sy - ey) > 1.0 or abs(sy - line) > DOOR_TOUCH_TOL:
                continue
            lo, hi, start_along = min(sx, ex), max(sx, ex), sx
        if lo - SNAP_TOL <= mid <= hi + SNAP_TOL:
            return wi, start_along
    return None


SNAP_TOL = 1.0


def _open_door_on_wall(spec, wi, start_along, a, b) -> bool:
    """在牆 wi 的 [a,b] 段開一扇內門(避開既有洞口)。成功回 True。"""
    w = spec.walls[wi]
    taken = [(op.position - op.width / 2, op.position + op.width / 2)
             for op in w.openings]
    for clear in DOOR_CLEAR_STEPS:                  # 連通是硬需求,淨距可分級退讓
        for frac in (0.5, 0.35, 0.65, 0.25, 0.75, 0.45, 0.55):
            m = a + (b - a) * frac
            pos = abs(m - start_along)
            lo, hi = pos - INTERIOR_DOOR_WIDTH / 2, pos + INTERIOR_DOOR_WIDTH / 2
            if lo < 0 or hi > w.length:
                continue
            if not all(hi < t0 or lo > t1 for t0, t1 in taken):
                continue
            if not _door_pos_ok(spec, w, pos, INTERIOR_DOOR_WIDTH, clear):
                continue                            # 不卡牆角
            w.openings.append(Opening(pos, INTERIOR_DOOR_WIDTH, "door"))
            spec.doors.append(DoorPlacement(wi, len(w.openings) - 1, Door()))
            return True
    return False


def _ensure_floor_connected(spec, max_new_doors: int = 8) -> int:
    """保證整層室內連通:從大門進來走得到每一間房(不必繞到室外)。

    分成多群時,挑「跨群、共用牆最長、且鄰室最公共」的一對開一扇門,重複到只剩一群。
    這是比「每間房有門」更強的保證——房間各自有門但彼此不通(例:柱線牆把一層切兩半,
    客廳只能從室外進)在真實住宅是錯的。回補了幾扇門。"""
    from src.design.layout.bsp_layout import _shared_edge

    added = 0
    for _ in range(max_new_doors):
        comps = _room_components(spec)
        if len(comps) <= 1:
            break
        best = None                       # (rank, -共用邊長, se, wi, start_along)
        for ca, cb in itertools.combinations(comps, 2):
            for i in ca:
                for j in cb:
                    ri, rj = spec.rooms[i], spec.rooms[j]
                    se = _shared_edge(Polygon(ri.points), Polygon(rj.points))
                    if se is None or se[4] < INTERIOR_DOOR_WIDTH + 200.0:
                        continue
                    cover = _wall_covering(spec, se[0], se[1], se[2], se[3])
                    if cover is None:
                        continue
                    # 兩邊挑「比較公共」的那間當優先度(走道/客廳優先,臥室/浴廁最後)
                    rank = min(_DOOR_NEIGHBOR_PREF.index(r.kind)
                               if r.kind in _DOOR_NEIGHBOR_PREF
                               else len(_DOOR_NEIGHBOR_PREF)
                               for r in (ri, rj))
                    cand = (rank, -se[4], se, cover[0], cover[1])
                    if best is None or cand[:2] < best[:2]:
                        best = cand
        if best is None:                  # 沒有夠長的共用牆可開(極罕見)
            break
        _rank, _len, se, wi, start_along = best
        if not _open_door_on_wall(spec, wi, start_along, se[2], se[3]):
            break
        added += 1
    return added


def _ensure_room_doors(spec, bx0, by0, bx1, level):
    """保證每間「可進入」的房間至少有一扇門/通道(機電豎管、天井除外)。

    沒門的房間 → 在與鄰室共用的內牆上補一扇門,優先接動線/公共空間(免得補到另一間
    孤立房或會被刪門的豎井);找不到內牆,只有 1F 才退而在南向前牆開(當入口;樓上外牆
    開門會變成通往空中,禁止)。不管建築尺寸,保證「進得了建築、進得了每個房間」。

    ⚠️ 要在所有刪門收尾(含 graph_layout 的管道間去門)之後才呼叫,否則剛補的門或
       既有門可能又被刪掉。"""
    from src.design.layout.room_circulation import _room_openings
    for room in spec.rooms:
        if room.kind in NO_DOOR_KINDS:
            continue
        if _room_openings(spec, Polygon(room.points)):     # 已有門/開放通道
            continue
        _add_interior_door(spec, room, bx0, by0, bx1, level)


def _add_interior_door(spec, room, bx0, by0, bx1, level):
    """給一間沒門的房間補一扇門:挑一道邊界內牆(接最公共/動線的鄰室)開洞。"""
    import math

    rp = Polygon(room.points)
    cx, cy = rp.centroid.x, rp.centroid.y
    rminx, rminy, rmaxx, rmaxy = rp.bounds
    need = INTERIOR_DOOR_WIDTH + 200.0                       # 邊夠長才開得下門
    best = None                                             # (rank, -overlap, wi, pos)

    for wi, w in enumerate(spec.walls):
        (sx, sy), (ex, ey) = w.start, w.end
        vertical = abs(sx - ex) < 1.0
        if vertical:
            if not (abs(sx - rminx) < 60 or abs(sx - rmaxx) < 60):
                continue                                    # 非本房左右邊界牆
            lo, hi = max(min(sy, ey), rminy), min(max(sy, ey), rmaxy)
            start_along, midpt_at = sy, lambda m: (sx, m)
        else:
            if not (abs(sy - rminy) < 60 or abs(sy - rmaxy) < 60):
                continue                                    # 非本房上下邊界牆
            lo, hi = max(min(sx, ex), rminx), min(max(sx, ex), rmaxx)
            start_along, midpt_at = sx, lambda m: (m, sy)
        if hi - lo < need:
            continue

        # 段內找一個不撞既有洞口的位置(中點優先,再試三七分)
        taken = [(op.position - op.width / 2, op.position + op.width / 2)
                 for op in w.openings]
        pos = None
        for clear in DOOR_CLEAR_STEPS:
            for frac in (0.5, 0.35, 0.65, 0.45, 0.55):
                m = lo + (hi - lo) * frac
                p = abs(m - start_along)
                a, b = p - INTERIOR_DOOR_WIDTH / 2, p + INTERIOR_DOOR_WIDTH / 2
                if not all(b < t0 or a > t1 for t0, t1 in taken):
                    continue
                if not _door_pos_ok(spec, w, p, INTERIOR_DOOR_WIDTH, clear):
                    continue                        # 不卡牆角
                pos, mid = p, m
                break
            if pos is not None:
                break
        if pos is None:
            continue

        mx, my = midpt_at(mid)
        dx, dy = mx - cx, my - cy
        L = math.hypot(dx, dy) or 1.0
        nb = Point(mx + dx / L * 300.0, my + dy / L * 300.0)  # 牆外側 → 鄰室
        neighbor = next((r for r in spec.rooms if r is not room
                         and Polygon(r.points).contains(nb)), None)
        if neighbor is None:                # 外牆:寧可接室內鄰室,也不要多開一道前門
            rank = 9 if (level == 1 and abs(my - by0) < 60) else 99  # 樓上外牆禁開
        elif neighbor.kind in NO_DOOR_KINDS:                # 豎井/天井:開門無意義
            rank = 99
        elif neighbor.kind in _DOOR_NEIGHBOR_PREF:
            rank = _DOOR_NEIGHBOR_PREF.index(neighbor.kind)
        else:
            rank = 8                                        # 一般房(臥室/浴廁等),末位候選
        cand = (rank, -(hi - lo), wi, pos)
        if best is None or cand[:2] < best[:2]:
            best = cand

    if best is None or best[0] >= 99:                       # 只剩通往空中/豎井 → 寧可不開
        return
    _, _, wi, pos = best
    w = spec.walls[wi]
    w.openings.append(Opening(pos, INTERIOR_DOOR_WIDTH, "door"))
    spec.doors.append(DoorPlacement(wi, len(w.openings) - 1, Door()))


def _build_floor(level, top, W, D, floor_label, furnish=True):
    """組一層 spec(房間 → 牆/門/窗 + 樓梯 + 開口收尾 + 家具)。"""
    bx0 = by0 = SETBACK
    bx1, by1 = SETBACK + W, SETBACK + D
    site_w, site_d = W + 2 * SETBACK, D + 2 * SETBACK
    rooms, stair = _floor_rooms(level, top, bx0, by0, bx1, by1)
    spec = rooms_to_spec(rooms, (bx0, by0, bx1, by1), site_w, site_d,
                         setback=SETBACK)
    _fix_openings(spec, bx0, by0, bx1, level)
    _ensure_floor_connected(spec)                    # 從大門走得到每一間房
    _ensure_room_doors(spec, bx0, by0, bx1, level)   # 保證每房都有門(不管尺寸)
    _ensure_room_windows(spec, bx0, by0, bx1, by1)   # 居室補窗(前後外牆/天井側)
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
