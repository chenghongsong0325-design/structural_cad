"""規則式格局產生器(C1)—— 需求 → FloorPlanSpec → 完整平面圖。

「說出需求就設計出圖」的核心引擎。階段 B 的圖面元素已全數完成,所以這裡
產出的是**完整的圖**:牆/門窗/家具設備/房型帶框標籤/四邊三層尺寸鏈/樓層
標示/北向箭頭/A3 圖框,一次到位。

支援兩種建築類型:

  1. HouseBrief(單戶住宅):兩帶式格局——北帶臥室區(主臥加大,≥3 房
     自動加主臥套衛)、南帶公共區(客廳西、餐廳中、東側服務核=浴廁+廚房)。
     走道規則(C1.5b):房間多才需要走道——≥3 房在兩帶之間留走道(臥室門
     開向走道、走道經正對大門軸線的通道口連通客廳);2 房動線融入客餐廳、
     預設不設(門直開客餐廳,被柱位擠壓時退回設走道);1 房不設。
     玄關(C1.5b):大門內側 2.2×1.5m 落塵區(短隔屏牆+鞋櫃),客廳挖角
     成 L 形。餐廳太窄自動併入客廳。家具設備依房型自動擺放。
  2. CorridorBrief(集合住宅):用 B6 的標準單元(place_unit)沿雙邊走廊
     重複 N 戶鏡射對排。

共同規則:
  * 軸網跨距自動決定(目標 4.5m、合理範圍內),柱在交點、隔間帶分界牆
    與軸線對齊(柱長在牆裡);臥室隔牆/服務核西牆距軸線 <0.6m 時吸附到
    軸線上「坐樑」(C1.5c)。
  * 機能規則(C1.5c):居室採光深度 ≤6m(客廳太寬自動補南窗)、家具不
    互撞不擋門(方桌自動讓開大門迴轉)。
  * 門窗自動配置且自動躲柱:洞口撞柱先平移、窗塞不下自動縮寬(最小 800)、
    加分項的窗塞不下就略過、必要的門塞不下則明確報錯「設計不成立」。
  * validate_spec() 檢核每份產出(房間不重疊/面積覆蓋/每房有門/臥室客廳
    有窗/洞口不壓柱),generate 完自動跑,不過就報錯——大量出圖、張張合格。

典型用法::

    from src.design.layout_generator import HouseBrief, CorridorBrief, generate_floor_plan

    spec = generate_floor_plan(HouseBrief(site_width=16000, site_depth=14000, bedrooms=3))
    spec = generate_floor_plan(CorridorBrief(units_per_row=6))
    draw_floor_plan(msp, spec, layers)   # 直接出圖

⚠️ 待確認假設見模組結尾 PENDING 區塊(設計規則常數、家具擺放規則等)。
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Optional, Union

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from shapely.geometry import Point as SPoint
from shapely.geometry import Polygon
from shapely.ops import unary_union

from src.drafting.apartment_plan import (
    DoorPlacement,
    FloorPlanSpec,
    WindowPlacement,
    build_grid,
    resolve_columns,
)
from src.drafting.door_window import Door, Window
from src.drafting.fixtures import Counter, FixturePlacement
from src.drafting.room import Room
from src.drafting.titleblock import CompetitionTitleData
# _t_rotation/_t_swing:B6 已驗證的鏡射語意(家具轉角/門向翻轉),單一來源。
from src.drafting.unit import UnitSpec, _t_rotation, _t_swing, one_room_unit, place_unit
from src.drafting.wall import (
    EXTERIOR_WALL_THICKNESS as EXT,
    INTERIOR_WALL_THICKNESS as INT,
    Opening,
    Wall,
)

Point = tuple[float, float]

# ── 設計規則常數(mm)——待確認:皆為合理經驗值 ────────────────────────────
MIN_BEDROOM_WIDTH = 2800     # 臥室最小淨寬
MASTER_RATIO = 1.35          # 主臥室寬度加大倍率
MIN_DINING_WIDTH = 2700      # 餐廳最小寬,低於此併入客廳成「客餐廳」
SERVICE_WIDTH_RANGE = (2600, 3400)   # 服務核(廚房+浴廁)寬度範圍
BATH_DEPTH_RANGE = (2000, 2800)      # 浴廁進深範圍
NORTH_BAND_RANGE = (3600, 5500)      # 臥室帶進深範圍
TARGET_BAY = 6000            # 軸網目標跨距(經濟跨距 6~9m 的下緣)
BAY_RANGE = (2, 4)           # X 向跨數上下限
BAY_SPAN_LIMITS = (3000, 9000)   # 單跨合理範圍(結構經濟)
BAY_RATIO_MAX = 1.6          # 跨距 max/min 上限(柱網要規則、近似等距)
GRID_SNAP_TOL = 1500         # 軸線向主要隔牆吸附的容差(柱藏進牆交點)
DOOR_WIDTH = 900
ENTRY_DOOR_WIDTH = 1000
WINDOW_WIDTHS = {"bedroom": 1500, "living": 1800, "dining": 1200,
                 "kitchen": 1200, "bathroom": 800}
ROOM_CODES = {"living": "X03", "dining": "X04", "bedroom": "X05",
              "kitchen": "X07", "bathroom": "X08", "corridor": "X00",
              "stair": "X01", "elevator": "X02", "storage": "X09",
              "foyer": "X10"}
ENSUITE_W, ENSUITE_D = 1800, 2000     # 主臥套房衛浴尺寸(≥3房自動加)
CORE_W = 3100                          # 集合住宅端部逃生核(樓梯+電梯)的開間寬
HALL_DEPTH = 1200                      # 單戶走道進深(臥室帶與公共帶之間,C1.5b)
PASSAGE_WIDTH = 1500                   # 走道↔客廳的開放通道寬(正對大門軸線)
WALL_SNAP_TOL = 600                    # 隔間牆距軸線小於此值就吸附坐樑(C1.5c)
DAYLIGHT_DEPTH_MAX = 6000              # 居室採光深度上限(距窗最遠 6m,C1.5c)
FOYER_W, FOYER_D = 2200, 1500          # 玄關落塵區(大門內側,C1.5b)
COLUMN_CLEARANCE = 300       # 洞口與柱面的最小淨距(柱要避開開口部,不貼門窗)


# ---------------------------------------------------------------------------
# 需求
# ---------------------------------------------------------------------------
@dataclass
class HouseBrief:
    """單戶住宅需求:基地寬深 + 臥室數(1~4)+ 方位約束(選填)。

    方位約束(C2:「主臥要在西南角,廚房靠北」):
      * master_corner:主臥落在哪個角落——"NW"(預設格局)/"NE"/"SW"/"SE"。
      * kitchen_side:廚房靠哪一側——"N"/"S"/"E"/"W"(預設格局在東南)。
    實作方式是把整張圖左右/上下鏡射(門向/家具/流理台跟著翻),所以兩個
    約束可能衝突(例:主臥 NW 佔北帶 → 廚房必在南,再要求廚房靠北就
    不成立),衝突會明確報錯。
    """

    site_width: float
    site_depth: float
    bedrooms: int = 3
    setback: float = 2000
    column_size: float = 500
    floor_label: str = "1F"
    master_corner: Optional[str] = None   # "NW"/"NE"/"SW"/"SE"
    kitchen_side: Optional[str] = None    # "N"/"S"/"E"/"W"


@dataclass
class CorridorBrief:
    """集合住宅需求:標準單元 × 每排戶數,雙邊走廊鏡射對排。"""

    units_per_row: int = 4
    unit: Optional[UnitSpec] = None      # None → 1房型(one_room_unit)
    corridor_width: float = 1800
    setback: float = 2000
    column_size: float = 500
    floor_label: str = "3F"


Brief = Union[HouseBrief, CorridorBrief]


# ---------------------------------------------------------------------------
# 工具:洞口躲柱
# ---------------------------------------------------------------------------
def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _find_clear_position(desired: float, width: float, lo: float, hi: float,
                         blocked: list[tuple[float, float]],
                         step: float = 100) -> Optional[float]:
    """在 [lo,hi] 找離 desired 最近、洞口區間不碰 blocked 的位置;無解回 None。"""
    half = width / 2
    lo_c, hi_c = lo + half, hi - half
    if lo_c > hi_c:
        return None

    def clear(pos: float) -> bool:
        a, b = pos - half, pos + half
        return all(b <= s or a >= e for s, e in blocked)

    offsets = [0.0]
    n = 1
    while n * step <= (hi_c - lo_c):
        offsets += [n * step, -n * step]
        n += 1
    for off in offsets:
        pos = _clamp(desired + off, lo_c, hi_c)
        if clear(pos):
            return pos
    return None


def _blocked(col_positions: list[float], col: float) -> list[tuple[float, float]]:
    r = col / 2 + COLUMN_CLEARANCE
    return [(p - r, p + r) for p in col_positions]


def _plan_x_grid(bx0: float, W: float, majors: list[float]) -> list[float]:
    """單戶 X 向軸網:等分起手、軸線吸附主要隔牆、跨數擇優(柱網原則)。

    對每個候選跨數(目標跨距的前後一跨、等分跨距在合理範圍):
      * 中間軸線距最近主要隔牆(臥室隔牆/服務核西牆)≤GRID_SNAP_TOL、
        且挪過去之後跨距仍規則(range 內、max/min ≤ 上限)→ 軸線挪到牆位,
        柱正好站在隔牆交點、被牆包住。
      * 挪不過去的中間軸線是「孤柱列」(柱凸在房間邊的牆上,難看)。
    選孤柱列最少的方案;平手取跨距最接近 TARGET_BAY 者。
    """
    n0 = round(W / TARGET_BAY)
    best: Optional[tuple] = None
    for nxc in (n0, n0 - 1, n0 + 1):
        if not BAY_RANGE[0] <= nxc <= BAY_RANGE[1]:
            continue
        span = W / nxc
        if not BAY_SPAN_LIMITS[0] <= span <= BAY_SPAN_LIMITS[1]:
            continue
        grid = [bx0 + i * span for i in range(nxc + 1)]
        orphans = 0
        for gi in range(1, nxc):
            m = min(majors, key=lambda v: abs(v - grid[gi]))
            if abs(m - grid[gi]) <= GRID_SNAP_TOL:
                trial = grid[:gi] + [m] + grid[gi + 1:]
                spans = [trial[j + 1] - trial[j] for j in range(nxc)]
                if (min(spans) >= BAY_SPAN_LIMITS[0]
                        and max(spans) <= BAY_SPAN_LIMITS[1]
                        and max(spans) / min(spans) <= BAY_RATIO_MAX):
                    grid = trial
                    continue
            if abs(m - grid[gi]) > 1:
                orphans += 1
        score = (orphans, abs(span - TARGET_BAY))
        if best is None or score < best[0]:
            best = (score, grid)
    if best is None:
        raise ValueError(f"建築寬 {W/1000:.1f}m 找不到合理柱網(跨距需 3~9m)")
    return best[1]


# ---------------------------------------------------------------------------
# 產生器:單戶住宅(兩帶式)
# ---------------------------------------------------------------------------
def _generate_house(brief: HouseBrief) -> FloorPlanSpec:
    if not 1 <= brief.bedrooms <= 4:
        raise ValueError(f"支援 1~4 間臥室,收到 {brief.bedrooms}")

    bx0 = by0 = brief.setback
    bx1 = brief.site_width - brief.setback
    by1 = brief.site_depth - brief.setback
    W, D = bx1 - bx0, by1 - by0
    if W < 8000 or D < 7000:
        raise ValueError(
            f"建築範圍 {W/1000:.1f}m×{D/1000:.1f}m 太小(至少約 8m×7m)")

    # ── 分區 ─────────────────────────────────────────────────────────
    dn = _clamp(D * 0.5, *NORTH_BAND_RANGE)     # 臥室帶進深
    ds = D - dn
    yd = by0 + ds                                # 帶分界牆 y


    ratios = [MASTER_RATIO] + [1.0] * (brief.bedrooms - 1)
    bed_w = [W * r / sum(ratios) for r in ratios]
    if min(bed_w) < MIN_BEDROOM_WIDTH:
        raise ValueError(
            f"{brief.bedrooms} 房分下來最窄 {min(bed_w)/1000:.2f}m"
            f"(需 ≥{MIN_BEDROOM_WIDTH/1000:.1f}m),請加大基地或減臥室")
    bed_x = [bx0]
    for w in bed_w:
        bed_x.append(bed_x[-1] + w)

    ws = _clamp(W * 0.25, *SERVICE_WIDTH_RANGE)  # 服務核寬
    sx = bx1 - ws

    # ── 軸網(先定;柱網原則:規則等距、柱藏牆交點、孤柱列最少——
    # 使用者 2026-07-12 定調,見 _plan_x_grid)────────────────────────
    grid_x = _plan_x_grid(bx0, W, bed_x[1:-1] + [sx])
    nx = len(grid_x) - 1
    col = brief.column_size

    # C1.5c:隔間坐樑(補正)——隔牆距軸線 <WALL_SNAP_TOL 就吸附到軸線上
    # (牆下有樑);吸附後仍須守 最小房寬 / 主臥最大 / 套衛放得下。
    for i in range(1, brief.bedrooms):
        g = min(grid_x, key=lambda v: abs(v - bed_x[i]))
        if 0 < abs(g - bed_x[i]) < WALL_SNAP_TOL:
            trial = bed_x[:i] + [g] + bed_x[i + 1:]
            widths = [trial[j + 1] - trial[j] for j in range(brief.bedrooms)]
            ok = (min(widths) >= MIN_BEDROOM_WIDTH
                  and widths[0] >= max(widths))
            if brief.bedrooms >= 3:
                ok = ok and widths[0] >= ENSUITE_W + 2700
            if ok:
                bed_x = trial
    # 服務核西牆同樣吸附軸線(吸附後寬度仍須在合理範圍)。
    g = min(grid_x, key=lambda v: abs(v - sx))
    if (0 < abs(g - sx) < WALL_SNAP_TOL
            and SERVICE_WIDTH_RANGE[0] <= bx1 - g <= SERVICE_WIDTH_RANGE[1]):
        sx, ws = g, bx1 - g
    wl_zone = W - ws
    merged_dining = wl_zone * 0.45 < MIN_DINING_WIDTH
    living_e = sx if merged_dining else bx0 + wl_zone * 0.55
    bath_d = _clamp(ds * 0.45, *BATH_DEPTH_RANGE)
    yb = by0 + bath_d

    # ── 軸網(X 向已在上面先定)──────────────────────────────────────
    grid_y = [by0, yd, by1]

    blocked_s = _blocked([gx - bx0 for gx in grid_x], col)
    blocked_n = _blocked([bx1 - gx for gx in grid_x], col)
    blocked_e = _blocked([gy - by0 for gy in grid_y], col)
    blocked_w = _blocked([by1 - gy for gy in grid_y], col)
    blocked_band = _blocked([gx - bx0 for gx in grid_x], col)   # 帶分界牆(4)

    # C1.5b:走道 =「連接多個獨立房間的共用動線」,房間多才需要——
    # ≥3 房必設(3+ 扇臥室門不宜全開進客餐廳);2 房動線融入客餐廳、
    # 預設不設,但東臥門若被服務核+柱位擠到只能開進廚房,退回設走道;
    # 1 房必不設。(集合住宅的走廊是另一套,見 _generate_corridor。)
    has_hall = brief.bedrooms >= 3
    pos_direct: Optional[float] = None           # 2房直開客餐廳時的東臥門位
    if brief.bedrooms == 2:
        pos_direct = _find_clear_position(
            (bed_x[-2] + sx) / 2 - bx0, DOOR_WIDTH,
            bed_x[-2] - bx0 + 150, sx - bx0 - 150, blocked_band)
        if pos_direct is None:
            has_hall = True
    yc = yd - (HALL_DEPTH if has_hall else 0)    # 走道下緣(=公共帶上緣)

    # C1.5b:走道東端 x_he——東側臥室在服務核上方,門要落在走道正面。
    # 先找東臥「最靠西」的可用門位(躲柱),走道東端跟著門走:必要時越過
    # 服務核西牆、吃進廚房上方一角(廚房變 L 形,門改由走道端進出)。
    x_he = sx
    pos_e: Optional[float] = None
    if has_hall:
        lo_e = bed_x[-2] - bx0 + 150                 # 讓開臥室隔牆
        pos_e = _find_clear_position(lo_e + DOOR_WIDTH / 2, DOOR_WIDTH,
                                     lo_e, W, blocked_band)
        if pos_e is None:
            raise ValueError("東側臥室找不到可開門的位置(柱擋住),請調整基地")
        x_he = max(sx, bx0 + pos_e + DOOR_WIDTH / 2 + 150)
        if x_he > sx:
            for gx in grid_x:                        # 端牆讓開柱
                if abs(x_he - gx) < 600:
                    x_he = gx + 600
            if bx1 - x_he < 1500:
                raise ValueError(
                    f"走道端部吃進廚房太深(剩 {(bx1-x_he)/1000:.1f}m <1.5m),請加大基地")
    has_notch = has_hall and x_he > sx               # 走道是否吃進廚房角

    # ── 牆 ───────────────────────────────────────────────────────────
    walls = [
        Wall((bx0, by0), (bx1, by0), EXT),    # 0 南
        Wall((bx1, by0), (bx1, by1), EXT),    # 1 東
        Wall((bx1, by1), (bx0, by1), EXT),    # 2 北
        Wall((bx0, by1), (bx0, by0), EXT),    # 3 西
        Wall((bx0, yd), (bx1, yd), INT),      # 4 帶分界牆
        # 5 服務核西牆(走道吃進廚房角時,只到走道下緣,以免切斷走道)。
        Wall((sx, by0), (sx, yc if has_notch else yd), INT),
        Wall((sx, yb), (bx1, yb), INT),       # 6 浴廁/廚房分界
    ]
    for i in range(1, brief.bedrooms):        # 7.. 臥室隔牆
        walls.append(Wall((bed_x[i], yd), (bed_x[i], by1), INT))

    doors: list[DoorPlacement] = []
    windows: list[WindowPlacement] = []

    def add_opening(wi: int, desired: float, width: float, kind: str,
                    lo: float, hi: float, blocked, optional=False) -> Optional[int]:
        widths = [width]
        if kind == "window":
            w = width - 200
            while w >= 800:
                widths.append(w)
                w -= 200
        for w in widths:
            pos = _find_clear_position(desired, w, lo, hi, blocked)
            if pos is not None:
                walls[wi].openings.append(Opening(pos, w, kind))
                return len(walls[wi].openings) - 1
        if optional:
            return None
        raise ValueError(f"牆段 [{lo:.0f},{hi:.0f}] 塞不下{kind}(寬{width:.0f}),設計不成立")

    # 大門(南牆、客廳段中央)。
    living_cx = (bx0 + living_e) / 2
    op = add_opening(0, living_cx - bx0, ENTRY_DOOR_WIDTH, "door", 0, living_e - bx0, blocked_s)
    doors.append(DoorPlacement(0, op, Door(hinge="left", swing="out")))

    # 玄關(C1.5b:大門內側 2.2×1.5m 落塵區)——跟著大門實際位置走
    # (大門躲柱偏移玄關就跟著偏);東側立短隔屏牆與客廳區隔,西側貼到
    # 外牆就靠牆。客廳淺時玄關深度自動縮(留 ≥0.7m 通行)。
    entry_x = bx0 + walls[0].openings[0].position
    fy1 = by0 + min(FOYER_D, yc - by0 - 700)
    fx0 = max(bx0, entry_x - FOYER_W / 2)
    fx1 = min(living_e, entry_x + FOYER_W / 2)
    walls.append(Wall((fx1, by0), (fx1, fy1), INT))    # 玄關短隔屏牆

    # 主臥套房衛浴(C1.5a:≥3 房自動加,位於主臥西南角)——先算範圍,
    # 臥室門/西窗要讓開它。
    has_ensuite = brief.bedrooms >= 3
    ex1 = ey1 = 0.0
    if has_ensuite:
        if dn < ENSUITE_D + 2200 or bed_w[0] < ENSUITE_W + 2700:
            raise ValueError(
                f"主臥({bed_w[0]/1000:.1f}m 寬 × {dn/1000:.1f}m 深)放不下"
                f"套房衛浴({ENSUITE_W/1000:.1f}×{ENSUITE_D/1000:.1f}m),請加大基地")
        ex1, ey1 = bx0 + ENSUITE_W, yd + ENSUITE_D

    # 臥室:門在帶分界牆(有走道時門要落在走道正面 x ≤ x_he;主臥的門
    # 讓開套衛牆段;東臥用預先算好的門位)、窗在北牆。
    for i in range(brief.bedrooms):
        x_l, x_r = bed_x[i], bed_x[i + 1]
        cx = (x_l + x_r) / 2
        if has_hall and i == brief.bedrooms - 1:
            door_lo, door_hi, door_desired = pos_e - 450, pos_e + 450, pos_e
        elif not has_hall and pos_direct is not None and i == brief.bedrooms - 1:
            # 2 房無走道:東臥門直開客餐廳(位置已預先算好,避開廚房正面)。
            door_lo, door_hi = pos_direct - 450, pos_direct + 450
            door_desired = pos_direct
        elif i == 0 and has_ensuite:
            door_lo, door_hi = ex1 - bx0, x_r - bx0
            door_desired = (ex1 + x_r) / 2 - bx0
        else:
            door_lo, door_hi, door_desired = x_l - bx0, x_r - bx0, cx - bx0
        op = add_opening(4, door_desired, DOOR_WIDTH, "door",
                         door_lo, door_hi, blocked_band)
        doors.append(DoorPlacement(4, op, Door(hinge="left", swing="out")))
        op = add_opening(2, bx1 - cx, WINDOW_WIDTHS["bedroom"], "window",
                         bx1 - x_r, bx1 - x_l, blocked_n)
        windows.append(WindowPlacement(2, op))

    # ── 套衛的牆/門/窗(範圍已在上面算好)──────────────────────────────
    if has_ensuite:
        # 套衛東牆(門開向浴內 = +n 西側)與北牆。
        walls.append(Wall((ex1, yd), (ex1, ey1), INT,
                          openings=[Opening(ENSUITE_D / 2, 750, "door")]))
        doors.append(DoorPlacement(len(walls) - 1, 0, Door(hinge="left", swing="out")))
        walls.append(Wall((bx0, ey1), (ex1, ey1), INT))
        # 套衛西窗(對外採光/通風;在主臥西窗下方的牆段)。
        op = add_opening(3, by1 - (yd + ENSUITE_D / 2), 800, "window",
                         by1 - ey1, by1 - yd, blocked_w)
        windows.append(WindowPlacement(3, op))

    # 走道南牆(兩段,中間留通道口正對大門軸線 → 進門直達走道;C1.5b)。
    if has_hall:
        for seg_a, seg_b in ((bx0, living_cx - PASSAGE_WIDTH / 2),
                             (living_cx + PASSAGE_WIDTH / 2, x_he)):
            if seg_b - seg_a > 1:
                walls.append(Wall((seg_a, yc), (seg_b, yc), INT))
    # 走道端牆(吃進廚房角時):廚房門開在這裡(走道 → 廚房)。
    if has_notch:
        walls.append(Wall((x_he, yc), (x_he, yd), INT,
                          openings=[Opening(HALL_DEPTH / 2, DOOR_WIDTH, "door")]))
        doors.append(DoorPlacement(len(walls) - 1, 0, Door(hinge="left", swing="in")))

    # 主臥西窗(有套衛時讓開套衛牆段)、客廳西窗。
    master_hi = (by1 - ey1) if has_ensuite else (by1 - yd)
    op = add_opening(3, master_hi / 2, WINDOW_WIDTHS["bedroom"], "window",
                     0, master_hi, blocked_w)
    windows.append(WindowPlacement(3, op))
    living_cy = (by0 + yc) / 2
    op = add_opening(3, by1 - living_cy, WINDOW_WIDTHS["living"], "window",
                     by1 - yc, by1 - by0, blocked_w)
    windows.append(WindowPlacement(3, op))

    # C1.5c:客廳太寬(只靠西窗會超過採光深度上限)→ 南牆補窗
    # (玄關隔屏牆以東的客廳段)。
    if living_e - bx0 > DAYLIGHT_DEPTH_MAX:
        op = add_opening(0, (living_cx + living_e) / 2 - bx0,
                         WINDOW_WIDTHS["living"], "window",
                         fx1 + 150 - bx0, living_e - bx0, blocked_s)
        windows.append(WindowPlacement(0, op))

    # 餐廳南窗(加分項)。
    if not merged_dining:
        op = add_opening(0, (living_e + sx) / 2 - bx0, WINDOW_WIDTHS["dining"], "window",
                         living_e - bx0, sx - bx0, blocked_s, optional=True)
        if op is not None:
            windows.append(WindowPlacement(0, op))

    # 浴廁門+東窗、廚房門+東窗。
    bath_cy = (by0 + yb) / 2
    op = add_opening(5, bath_cy - by0, 800, "door", 0, yb - by0, [])
    doors.append(DoorPlacement(5, op, Door(hinge="left", swing="in")))
    op = add_opening(1, bath_cy - by0, WINDOW_WIDTHS["bathroom"], "window",
                     0, yb - by0, blocked_e)
    windows.append(WindowPlacement(1, op))
    # 廚房門:走道吃進廚房角時已開在走道端牆;否則開在服務核西牆,
    # 並讓開走道南牆搭在西牆上的 T 形交接點(牆頭不能戳進門洞)。
    kitchen_cy = (yb + yd) / 2
    if not has_notch:
        junction = [(yc - by0 - 150, yc - by0 + 150)] if has_hall else []
        op = add_opening(5, kitchen_cy - by0, DOOR_WIDTH, "door",
                         yb - by0, yd - by0, junction)
        doors.append(DoorPlacement(5, op, Door(hinge="left", swing="in")))
    op = add_opening(1, kitchen_cy - by0, WINDOW_WIDTHS["kitchen"], "window",
                     yb - by0, yd - by0, blocked_e)
    windows.append(WindowPlacement(1, op))

    # ── 房間 ─────────────────────────────────────────────────────────
    rooms: list[Room] = []
    # 客廳(或客餐廳)挖掉西南側的玄關角 → L 形;玄關自成一室(開放,無門)。
    lv_e = sx if merged_dining else living_e
    if fx0 <= bx0:            # 玄關貼西外牆
        lv_pts = [(fx1, by0), (lv_e, by0), (lv_e, yc), (bx0, yc), (bx0, fy1), (fx1, fy1)]
    else:
        lv_pts = [(bx0, by0), (fx0, by0), (fx0, fy1), (fx1, fy1), (fx1, by0),
                  (lv_e, by0), (lv_e, yc), (bx0, yc)]
    rooms.append(Room("客餐廳" if merged_dining else "客廳", lv_pts,
                      kind="living", code=ROOM_CODES["living"]))
    rooms.append(Room("玄關", [(fx0, by0), (fx1, by0), (fx1, fy1), (fx0, fy1)],
                      kind="foyer", code=ROOM_CODES["foyer"]))
    if not merged_dining:
        rooms.append(Room("餐廳", [(living_e, by0), (sx, by0), (sx, yc), (living_e, yc)],
                          kind="dining", code=ROOM_CODES["dining"]))
    if has_hall:
        rooms.append(Room("走道", [(bx0, yc), (x_he, yc), (x_he, yd), (bx0, yd)],
                          kind="corridor", code=ROOM_CODES["corridor"]))
    rooms.append(Room("浴廁", [(sx, by0), (bx1, by0), (bx1, yb), (sx, yb)],
                      kind="bathroom", code=ROOM_CODES["bathroom"]))
    if has_notch:
        # 廚房 L 形(西北角讓給走道端)。
        rooms.append(Room("廚房",
                          [(sx, yb), (bx1, yb), (bx1, yd), (x_he, yd),
                           (x_he, yc), (sx, yc)],
                          kind="kitchen", code=ROOM_CODES["kitchen"]))
    else:
        rooms.append(Room("廚房", [(sx, yb), (bx1, yb), (bx1, yd), (sx, yd)],
                          kind="kitchen", code=ROOM_CODES["kitchen"]))
    bed_names = ["主臥室", "臥室A", "臥室B", "臥室C"]
    for i in range(brief.bedrooms):
        if i == 0 and has_ensuite:
            # 主臥 L 形(西南角讓給套衛)+ 主臥浴。
            rooms.append(Room("主臥室",
                              [(ex1, yd), (bed_x[1], yd), (bed_x[1], by1),
                               (bx0, by1), (bx0, ey1), (ex1, ey1)],
                              kind="bedroom", code=ROOM_CODES["bedroom"]))
            rooms.append(Room("主臥浴",
                              [(bx0, yd), (ex1, yd), (ex1, ey1), (bx0, ey1)],
                              kind="bathroom", code=ROOM_CODES["bathroom"]))
        else:
            rooms.append(Room(bed_names[i],
                              [(bed_x[i], yd), (bed_x[i + 1], yd),
                               (bed_x[i + 1], by1), (bed_x[i], by1)],
                              kind="bedroom", code=ROOM_CODES["bedroom"]))

    # ── 家具設備(依房型規則自動擺放)──────────────────────────────────
    fixtures: list = []
    # 臥室:床(主臥雙人、其餘單人)頭靠北牆;衣櫃貼東側牆、緊鄰北牆角。
    for i in range(brief.bedrooms):
        x_l, x_r = bed_x[i], bed_x[i + 1]
        cx = (x_l + x_r) / 2
        bed = "bed_double" if i == 0 else "bed_single"
        fixtures.append(FixturePlacement(bed, (cx, by1 - 75), 180))
        inner = 75 if i == brief.bedrooms - 1 else 60   # 排尾靠外牆(150)
        fixtures.append(FixturePlacement("wardrobe", (x_r - inner, by1 - 75 - 750), 90))
    # 玄關:鞋櫃貼短隔屏牆內側(讓開大門迴轉方塊)。
    fixtures.append(FixturePlacement(
        "shoe_cabinet", (fx1 - 60, by0 + (fy1 - by0) / 2), 90))
    # 客廳:沙發背靠西牆;方桌居中偏東,桌組(含椅)半徑 780——會伸進
    # 大門迴轉方塊或玄關就東移讓開;連東移都放不下就不擺(C1.5b/c)。
    fixtures.append(FixturePlacement("sofa3", (bx0 + 75, living_cy), 270))
    tx = (bx0 + living_e) / 2 + 500
    if living_cy - 780 < by0 + ENTRY_DOOR_WIDTH:
        tx = max(tx, entry_x + ENTRY_DOOR_WIDTH / 2 + 780 + 150)
    if living_cy - 780 < fy1:
        tx = max(tx, fx1 + 780 + 150)
    if tx + 780 <= living_e - 60:
        fixtures.append(FixturePlacement("table4", (tx, living_cy), 0))
    # 餐廳(獨立時):餐桌。
    if not merged_dining:
        fixtures.append(FixturePlacement("table4", ((living_e + sx) / 2, living_cy), 0))
    # 浴廁:馬桶+洗手台靠東牆(避開西側門的迴轉)。
    fixtures.append(FixturePlacement("toilet", (bx1 - 75, by0 + bath_d - 500), 90))
    fixtures.append(FixturePlacement("basin", (bx1 - 75, by0 + 600), 90))
    # 主臥浴(套衛):馬桶+洗手台貼西牆(門在東側,迴轉已讓開)。
    if has_ensuite:
        fixtures.append(FixturePlacement("toilet", (bx0 + 75, yd + 550), 270))
        fixtures.append(FixturePlacement("basin", (bx0 + 75, yd + 1450), 270))
    # 廚房:L 型流理台(東牆段 + 北段,水槽在北段;北段東端讓開轉角、
    # 西端讓開廚房門迴轉——門在走道端牆或服務核西牆,皆以 x_he 為準)。
    north_end = x_he + 1000
    if (bx1 - 675) - north_end >= 600:
        fixtures.append(Counter(start=(bx1 - 75, yb + 60), end=(bx1 - 75, yd - 60)))
        fixtures.append(Counter(start=(bx1 - 675, yd - 60), end=(north_end, yd - 60), sink=True))
    else:                                   # 北段太短 → 一字型,水槽改東段
        fixtures.append(Counter(start=(bx1 - 75, yb + 60), end=(bx1 - 75, yd - 60), sink=True))

    spec = FloorPlanSpec(
        site_boundary=[(0, 0), (brief.site_width, 0),
                       (brief.site_width, brief.site_depth), (0, brief.site_depth)],
        setback=brief.setback,
        x_spacings=[grid_x[i + 1] - grid_x[i] for i in range(nx)],
        y_spacings=[ds, dn],
        grid_origin=(bx0, by0),
        column_size=col,
        walls=walls, rooms=rooms, doors=doors, windows=windows, fixtures=fixtures,
        dim_chains=True, sheet=True,
        floor_label=brief.floor_label, north_arrow=True,
        title_block=CompetitionTitleData(),
    )
    return spec


# ---------------------------------------------------------------------------
# 產生器:集合住宅(單元重複,B6)
# ---------------------------------------------------------------------------
def _generate_corridor(brief: CorridorBrief) -> FloorPlanSpec:
    """集合住宅:兩端逃生核 + 單元×N 鏡射對排 + 雙邊走廊。

    C1.5a:走廊兩端各設一個「核」開間(寬 CORE_W)——北側樓梯間(折返梯,
    門通走廊 → 兩個逃生方向)、南側西端為 電梯廳+電梯、東端為儲藏室。
    走廊縱貫全樓(含核開間,= 梯廳兼走廊)。
    """
    from src.drafting.balcony_elevator import Elevator
    from src.drafting.stair import UStair

    if not 2 <= brief.units_per_row <= 10:
        raise ValueError(f"每排 2~10 戶,收到 {brief.units_per_row}")

    unit = brief.unit or one_room_unit()
    n = brief.units_per_row
    x0 = y0 = brief.setback
    y_corr = y0 + unit.depth                    # 走廊下緣
    y_top = y_corr + brief.corridor_width       # 走廊上緣
    xw = x0 + CORE_W                            # 西核/單元分界
    bx1 = xw + n * unit.width + CORE_W          # 建築東緣
    xe = bx1 - CORE_W                           # 單元/東核分界
    by1 = y_top + unit.depth
    y_hall = y_corr - 2200                      # 電梯廳/儲藏分界(西核)

    walls = [
        Wall((x0, y0), (x0, by1), EXT),                   # 0 西端牆
        Wall((bx1, y0), (bx1, by1), EXT),                 # 1 東端牆
        # 核/單元分戶牆(走廊段留空 → 走廊縱貫)。
        Wall((xw, y0), (xw, y_corr), EXT),                # 2
        Wall((xw, y_top), (xw, by1), EXT),                # 3
        Wall((xe, y0), (xe, y_corr), EXT),                # 4
        Wall((xe, y_top), (xe, by1), EXT),                # 5
        # 核開間的南北外牆段(樓梯間開對外窗:採光/排煙)。
        Wall((x0, by1), (xw, by1), EXT,
             openings=[Opening(CORE_W / 2, 1200, "window")]),   # 6 北W
        Wall((xe, by1), (bx1, by1), EXT,
             openings=[Opening(CORE_W / 2, 1200, "window")]),   # 7 北E
        Wall((x0, y0), (xw, y0), EXT),                    # 8 南W
        Wall((xe, y0), (bx1, y0), EXT),                   # 9 南E
        # 樓梯間南牆(門通走廊,開向梯間)。
        Wall((x0, y_top), (xw, y_top), EXT,
             openings=[Opening(CORE_W / 2, 900, "door")]),      # 10 W
        Wall((xe, y_top), (bx1, y_top), EXT,
             openings=[Opening(CORE_W / 2, 900, "door")]),      # 11 E
        # 西核:電梯廳/儲藏分界(門進儲藏);東核:儲藏北牆(門通走廊)。
        Wall((x0, y_hall), (xw, y_hall), INT,
             openings=[Opening(850, 800, "door")]),             # 12
        Wall((xe, y_corr), (bx1, y_corr), INT,
             openings=[Opening(CORE_W / 2, 800, "door")]),      # 13
    ]
    doors = [
        DoorPlacement(10, 0, Door(hinge="left", swing="out")),  # 西梯間(+n=北=梯間)
        DoorPlacement(11, 0, Door(hinge="left", swing="out")),  # 東梯間
        DoorPlacement(12, 0, Door(hinge="left", swing="in")),   # 西儲藏(南側)
        DoorPlacement(13, 0, Door(hinge="left", swing="in")),   # 東儲藏(南側)
    ]
    windows = [WindowPlacement(6, 0), WindowPlacement(7, 0)]    # 梯間對外窗

    rooms = [
        Room("走廊", [(x0, y_corr), (bx1, y_corr), (bx1, y_top), (x0, y_top)],
             kind="corridor", code=ROOM_CODES["corridor"]),
        Room("樓梯間", [(x0, y_top), (xw, y_top), (xw, by1), (x0, by1)],
             kind="stair", code=ROOM_CODES["stair"]),
        Room("樓梯間", [(xe, y_top), (bx1, y_top), (bx1, by1), (xe, by1)],
             kind="stair", code=ROOM_CODES["stair"]),
        Room("電梯廳", [(x0, y_hall), (xw - 1400, y_hall), (xw - 1400, y_corr), (x0, y_corr)],
             kind="hall", code=ROOM_CODES["elevator"]),
        Room("電梯", [(xw - 1400, y_hall), (xw, y_hall), (xw, y_corr), (xw - 1400, y_corr)],
             kind="elevator", code=ROOM_CODES["elevator"]),
        Room("儲藏室", [(x0, y0), (xw, y0), (xw, y_hall), (x0, y_hall)],
             kind="storage", code=ROOM_CODES["storage"]),
        Room("儲藏室", [(xe, y0), (bx1, y0), (bx1, y_corr), (xe, y_corr)],
             kind="storage", code=ROOM_CODES["storage"]),
    ]

    # 折返梯 × 2(每端一座,靠梯間北牆,入口在南、留出門的迴轉):
    # 11 級 × 260 = 2860 run + 平台 1200 = 4060 深。
    stair_len = 4060
    stairs = [
        UStair(origin=(x0 + (CORE_W - 2500) / 2, by1 - 75 - stair_len),
               width=2500, length=stair_len, direction="north",
               steps_per_flight=11, tread=260, label="上"),
        UStair(origin=(xe + (CORE_W - 2500) / 2, by1 - 75 - stair_len),
               width=2500, length=stair_len, direction="north",
               steps_per_flight=11, tread=260, label="上"),
    ]
    # 電梯(西核,貼分戶牆;門開西面通電梯廳,電梯廳與走廊開放連通)。
    elevators = [Elevator(origin=(xw - 1400, y_hall), width=1400, depth=2200,
                          door_side="west")]

    spec = FloorPlanSpec(
        site_boundary=[(0, 0), (bx1 + brief.setback, 0),
                       (bx1 + brief.setback, by1 + brief.setback),
                       (0, by1 + brief.setback)],
        setback=brief.setback,
        x_spacings=[CORE_W] + [unit.width] * n + [CORE_W],
        y_spacings=[unit.depth, brief.corridor_width, unit.depth],
        grid_origin=(x0, y0),
        column_size=brief.column_size,
        walls=walls, rooms=rooms, doors=doors, windows=windows,
        stairs=stairs, elevators=elevators,
        dim_chains=True, sheet=True,
        floor_label=brief.floor_label, north_arrow=True,
        title_block=CompetitionTitleData(),
    )
    for i in range(n):
        ux = xw + i * unit.width
        place_unit(spec, unit, origin=(ux, y_top))                 # 上排
        place_unit(spec, unit, origin=(ux, y0), mirror_y=True)     # 下排(對排)
    return spec


# ---------------------------------------------------------------------------
# 方位約束(C2):整張圖鏡射
# ---------------------------------------------------------------------------
# 預設格局(不鏡射)的方位:主臥在西北角(臥室帶西端)、廚房在東南
# (公共帶服務核)。約束靠「左右翻 / 上下翻整張圖」達成:
#   主臥要在東 → 左右翻;主臥要在南 / 廚房要在北 → 上下翻。
_CORNERS = ("NW", "NE", "SW", "SE")
_SIDES = ("N", "S", "E", "W")


def _resolve_mirrors(brief: HouseBrief) -> tuple[bool, bool]:
    """方位約束 → (左右翻?, 上下翻?);互相矛盾就明確報錯。"""
    need: dict[str, tuple[bool, str]] = {}   # axis -> (要不要翻, 誰要求的)

    def ask(axis: str, value: bool, source: str) -> None:
        if axis in need and need[axis][0] != value:
            raise ValueError(
                f"方位約束衝突:「{source}」與「{need[axis][1]}」無法同時成立"
                f"(臥室帶與公共帶南北各佔一半,主臥和廚房必在異側)")
        need[axis] = (value, source)

    if brief.master_corner is not None:
        c = brief.master_corner.upper()
        if c not in _CORNERS:
            raise ValueError(f"master_corner 需為 {_CORNERS},收到 {brief.master_corner!r}")
        ask("x", "E" in c, f"主臥在{c}")
        ask("y", "S" in c, f"主臥在{c}")
    if brief.kitchen_side is not None:
        s = brief.kitchen_side.upper()
        if s not in _SIDES:
            raise ValueError(f"kitchen_side 需為 {_SIDES},收到 {brief.kitchen_side!r}")
        if s in ("E", "W"):
            ask("x", s == "W", f"廚房靠{s}")
        else:
            ask("y", s == "N", f"廚房靠{s}")

    return (need.get("x", (False, ""))[0], need.get("y", (False, ""))[0])


def _mirror_spec(spec: FloorPlanSpec, mx: bool, my: bool) -> FloorPlanSpec:
    """把整張單戶 spec 左右(mx)/上下(my)鏡射——牆/房間/門窗/家具全翻。

    鏡射軸 = 基地中心(單戶的建築置中於基地,翻完 grid_origin 不變)。
    鏡射語意與 B6 place_unit 同一套:洞口位置(距牆起點的距離)是等距
    變換、不變;奇數次鏡射翻門的開向(out↔in);家具旋轉照 _t_rotation;
    流理台交換起訖點保住檯面側。翻完仍會跑 validate_spec 整套檢核。
    """
    xs = [p[0] for p in spec.site_boundary]
    ys = [p[1] for p in spec.site_boundary]
    sx2, sy2 = min(xs) + max(xs), min(ys) + max(ys)   # x' = sx2 - x

    def tp(p: Point) -> Point:
        x, y = p
        return (sx2 - x if mx else x, sy2 - y if my else y)

    mirrored = mx != my            # 奇數次鏡射(左右手翻轉)

    walls = [Wall(start=tp(w.start), end=tp(w.end), thickness=w.thickness,
                  openings=[Opening(op.position, op.width, op.kind)
                            for op in w.openings])
             for w in spec.walls]
    doors = [DoorPlacement(dp.wall_index, dp.opening_index,
                           Door(hinge=dp.door.hinge,
                                swing=_t_swing(mirrored, dp.door.swing),
                                width=dp.door.width))
             for dp in spec.doors]
    windows = [WindowPlacement(wp.wall_index, wp.opening_index,
                               Window(lines=wp.window.lines, width=wp.window.width))
               for wp in spec.windows]
    rooms = [Room(name=r.name, points=[tp(p) for p in r.points],
                  kind=r.kind, code=r.code, note=r.note)
             for r in spec.rooms]
    fixtures: list = []
    for fx in spec.fixtures:
        if isinstance(fx, Counter):
            a, b = tp(fx.start), tp(fx.end)
            if mirrored:
                a, b = b, a
            fixtures.append(Counter(start=a, end=b, depth=fx.depth, sink=fx.sink))
        else:
            fixtures.append(FixturePlacement(
                name=fx.name, insert=tp(fx.insert),
                rotation=_t_rotation(mx, my, fx.rotation)))

    return replace(
        spec, walls=walls, doors=doors, windows=windows, rooms=rooms,
        fixtures=fixtures,
        x_spacings=list(reversed(spec.x_spacings)) if mx else spec.x_spacings,
        y_spacings=list(reversed(spec.y_spacings)) if my else spec.y_spacings,
    )


def generate_floor_plan(brief: Brief) -> FloorPlanSpec:
    """需求 → FloorPlanSpec(已通過 validate_spec,可直接餵 draw_floor_plan)。"""
    if isinstance(brief, HouseBrief):
        spec = _generate_house(brief)
        mx, my = _resolve_mirrors(brief)          # 方位約束(C2)
        if mx or my:
            spec = _mirror_spec(spec, mx, my)
    elif isinstance(brief, CorridorBrief):
        spec = _generate_corridor(brief)
    else:
        raise TypeError(f"未知需求型別:{type(brief).__name__}")

    problems = validate_spec(spec)
    if problems:
        raise ValueError("產生的設計未通過檢核:\n  - " + "\n  - ".join(problems))
    return spec


# ---------------------------------------------------------------------------
# 設計檢核
# ---------------------------------------------------------------------------
def validate_spec(spec: FloorPlanSpec) -> list[str]:
    """檢核設計是否合理,回傳問題清單(空 = 通過)。

    檢查:房間互不重疊、面積合計=建築面積、每個 臥室/浴廁/廚房 有門、
    臥室/客廳 有窗、所有洞口不壓柱(含淨距)。另含法規/動線規則:
    浴室無窗須標機械排風(C1.5a)、多戶建築 ≥2 樓梯間(C1.5a)、
    有走道時臥室門須通走道、臥室門不得開進廚房(C1.5b)、
    居室採光深度 ≤6m、家具互不重疊且不擋門的迴轉(C1.5c)。
    """
    problems: list[str] = []

    bx0, by0 = spec.grid_origin
    bw, bd = sum(spec.x_spacings), sum(spec.y_spacings)
    building = Polygon([(bx0, by0), (bx0 + bw, by0),
                        (bx0 + bw, by0 + bd), (bx0, by0 + bd)])

    polys = [Polygon(r.points) for r in spec.rooms]
    for i in range(len(polys)):
        for j in range(i + 1, len(polys)):
            if polys[i].intersection(polys[j]).area > 1.0:
                problems.append(f"房間重疊:{spec.rooms[i].name}×{spec.rooms[j].name}")
    union = unary_union(polys)
    if abs(union.area - building.area) > building.area * 1e-6:
        problems.append(
            f"房間面積合計 {union.area/1e6:.2f}m² ≠ 建築 {building.area/1e6:.2f}m²")

    def openings_on(room_poly: Polygon, kind: str) -> int:
        count = 0
        for w in spec.walls:
            for op in w.openings:
                if op.kind != kind:
                    continue
                if room_poly.boundary.distance(SPoint(w.point_at(op.position))) < 1.0:
                    count += 1
        return count

    for room, poly in zip(spec.rooms, polys):
        if room.kind in ("bedroom", "bathroom", "kitchen", "stair", "storage"):
            if openings_on(poly, "door") < 1:
                problems.append(f"{room.name} 沒有門")
        if room.kind in ("bedroom", "living"):
            if openings_on(poly, "window") < 1:
                problems.append(f"{room.name} 沒有窗")
        # C1.5a:浴室要有對外窗,否則必須標示機械排風。
        if room.kind == "bathroom":
            if openings_on(poly, "window") < 1 and "排風" not in room.note:
                problems.append(f"{room.name} 無窗且未標示機械排風")

    # C1.5a:多戶建築(≥2 個居住單元)必須有 ≥2 座樓梯間(兩個逃生方向)。
    # 用 kind="living" 的數量判斷戶數:單戶只有一個客廳,不觸發。
    n_units = sum(1 for r in spec.rooms if r.kind == "living")
    if n_units >= 2:
        n_stairs = sum(1 for r in spec.rooms if r.kind == "stair")
        if n_stairs < 2:
            problems.append(f"多戶建築只有 {n_stairs} 座樓梯間(逃生需 ≥2)")

    # C1.5b:建築內有走道/走廊時,每間臥室至少要有一扇門開向它
    # (臥室門不得只直開客餐廳)。
    corridor_polys = [p for r, p in zip(spec.rooms, polys) if r.kind == "corridor"]
    if corridor_polys:
        for room, poly in zip(spec.rooms, polys):
            if room.kind != "bedroom":
                continue
            reaches = any(
                poly.boundary.distance(SPoint(w.point_at(op.position))) < 1.0
                and any(cp.boundary.distance(SPoint(w.point_at(op.position))) < 1.0
                        for cp in corridor_polys)
                for w in spec.walls for op in w.openings if op.kind == "door")
            if not reaches:
                problems.append(f"{room.name} 的門未通走道")

    # C1.5b:臥室門不得開進廚房(動線常識;套衛的門開進浴室是刻意的,不管)。
    kitchen_polys = [p for r, p in zip(spec.rooms, polys) if r.kind == "kitchen"]
    for room, poly in zip(spec.rooms, polys):
        if room.kind != "bedroom":
            continue
        for w in spec.walls:
            for op in w.openings:
                if op.kind != "door":
                    continue
                pt = SPoint(w.point_at(op.position))
                if poly.boundary.distance(pt) < 1.0 and any(
                        kp.boundary.distance(pt) < 1.0 for kp in kitchen_polys):
                    problems.append(f"{room.name} 的門開進廚房")

    # 柱網規則性(結構經濟;使用者定調:柱網儘量規則、近似等距、跨度合理)。
    # X 向查範圍+均勻度;Y 向只查上限(走廊 1.8m 這種窄跨是刻意的)。
    xs = spec.x_spacings
    if min(xs) < BAY_SPAN_LIMITS[0] or max(xs) > BAY_SPAN_LIMITS[1]:
        problems.append(
            f"X 向跨距 {min(xs)/1000:.1f}~{max(xs)/1000:.1f}m "
            f"超出 {BAY_SPAN_LIMITS[0]/1000:.0f}~{BAY_SPAN_LIMITS[1]/1000:.0f}m")
    elif max(xs) / min(xs) > BAY_RATIO_MAX:
        problems.append(
            f"X 向柱網不規則(跨距 max/min = {max(xs)/min(xs):.2f} "
            f"> {BAY_RATIO_MAX})")
    if max(spec.y_spacings) > BAY_SPAN_LIMITS[1]:
        problems.append(f"Y 向跨距 {max(spec.y_spacings)/1000:.1f}m 超過 9m")

    # C1.5b:玄關(落塵區)必須貼著一扇門——大門要開進玄關。
    for room, poly in zip(spec.rooms, polys):
        if room.kind == "foyer" and openings_on(poly, "door") < 1:
            problems.append(f"{room.name} 沒貼任何門(玄關應在大門內側)")

    # C1.5c:採光深度——臥室/客廳沿「窗的法線方向」的房間深度,至少要有
    # 一扇窗 ≤ 上限(離窗太遠的角落照不到光)。無窗的房間上面已抓、不重複報。
    for room, poly in zip(spec.rooms, polys):
        if room.kind not in ("bedroom", "living"):
            continue
        depths = []
        for w in spec.walls:
            nx_, ny_ = w.normal_vector
            for op in w.openings:
                if op.kind != "window":
                    continue
                wx, wy = w.point_at(op.position)
                if poly.boundary.distance(SPoint((wx, wy))) >= 1.0:
                    continue
                depths.append(max(abs((px - wx) * nx_ + (py - wy) * ny_)
                                  for px, py in room.points))
        if depths and min(depths) > DAYLIGHT_DEPTH_MAX + 1.0:
            problems.append(
                f"{room.name} 採光深度 {min(depths)/1000:.1f}m "
                f"超過上限 {DAYLIGHT_DEPTH_MAX/1000:.0f}m(需增開窗)")

    # C1.5c:家具碰撞——家具/流理台互不重疊、不擋任何門的迴轉方塊。
    from src.drafting.fixtures import counter_footprint, fixture_footprint

    fps: list[tuple[str, Polygon]] = []
    for fx in spec.fixtures:
        if isinstance(fx, FixturePlacement):
            fps.append((fx.name, Polygon(fixture_footprint(fx))))
        elif isinstance(fx, Counter):
            fps.append(("counter", Polygon(counter_footprint(fx))))
    for i in range(len(fps)):
        for j in range(i + 1, len(fps)):
            if fps[i][1].intersection(fps[j][1]).area > 100:
                problems.append(f"家具重疊:{fps[i][0]}×{fps[j][0]}")
    for dp in spec.doors:
        w = spec.walls[dp.wall_index]
        op = w.openings[dp.opening_index]
        cx_, cy_ = w.point_at(op.position)
        ux, uy = w.unit_vector
        nx_, ny_ = w.normal_vector
        s = 1.0 if dp.door.swing == "out" else -1.0   # 門扇掃過哪一側
        h, e = op.width / 2, op.width
        square = Polygon([
            (cx_ - ux * h, cy_ - uy * h),
            (cx_ + ux * h, cy_ + uy * h),
            (cx_ + ux * h + s * nx_ * e, cy_ + uy * h + s * ny_ * e),
            (cx_ - ux * h + s * nx_ * e, cy_ - uy * h + s * ny_ * e),
        ])
        for name, fp in fps:
            if square.intersection(fp).area > 100:
                problems.append(
                    f"家具 {name} 擋住門的迴轉(牆 {dp.wall_index} 的門)")

    # 洞口不壓柱。
    columns = resolve_columns(spec, build_grid(spec))
    r = spec.column_size / 2 + COLUMN_CLEARANCE
    for w in spec.walls:
        ux, uy = (None, None)
        for op in w.openings:
            if ux is None:
                ux, uy = w.unit_vector
                nx_, ny_ = w.normal_vector
            for c in columns:
                cx, cy = c.center
                if abs((cx - w.start[0]) * nx_ + (cy - w.start[1]) * ny_) \
                        > w.thickness / 2 + c.width / 2:
                    continue
                col_pos = (cx - w.start[0]) * ux + (cy - w.start[1]) * uy
                if not (op.position + op.width / 2 <= col_pos - r
                        or op.position - op.width / 2 >= col_pos + r):
                    problems.append(
                        f"洞口壓柱:牆 {w.start}->{w.end} 位置 {op.position:.0f} "
                        f"的{op.kind}與柱 {c.center} 衝突")
    return problems


# ---------------------------------------------------------------------------
# 示範:三份不同需求 → 三張完整的圖
# ---------------------------------------------------------------------------
DEMO_BRIEFS: list[tuple[str, Brief]] = [
    ("gen_house_2br", HouseBrief(site_width=12000, site_depth=11000, bedrooms=2)),
    ("gen_house_3br", HouseBrief(site_width=18000, site_depth=14000, bedrooms=3)),
    ("gen_corridor6", CorridorBrief(units_per_row=6)),
]


def main() -> None:
    from src.drafting.apartment_plan import draw_floor_plan
    from src.standards.loader import apply_standard, load_standard, new_document

    out_dir = _PROJECT_ROOT / "output"
    out_dir.mkdir(exist_ok=True)
    for name, brief in DEMO_BRIEFS:
        spec = generate_floor_plan(brief)
        std = load_standard()
        doc = new_document()
        layers = apply_standard(doc, std)
        draw_floor_plan(doc.modelspace(), spec, layers)
        doc.saveas(out_dir / f"{name}.dxf")
        print(f"[OK] {name}.dxf: {len(spec.rooms)} 室 {len(spec.doors)} 門 "
              f"{len(spec.windows)} 窗 {len(spec.fixtures)} 件設備")


if __name__ == "__main__":
    main()


# =============================================================================
# PENDING(待確認假設彙整)
# =============================================================================
# 1. 設計規則常數(臥室最小寬 2.8m、主臥 ×1.35、服務核 2.6~3.4m、目標跨距
#    4.5m、走廊 1.8m、走道 1.2m、通道口 1.5m 等)皆為經驗值。
# 2. 單戶固定「兩帶式、朝南入口」;≥3 房自動加主臥套衛(C1.5a)。走道規則
#    (C1.5b):≥3 房必設、2 房預設不設(擠壓時退回)、1 房不設——依使用者
#    2026-07-12 定調「走道=連接多獨立房間的共用動線,房間多才需要;小宅
#    動線融入客廳」。玄關已加(C1.5b:2.2×1.5m、隔屏牆+鞋櫃,跟著大門
#    實際位置走)。變化維度(朝向/陽台/樓梯電梯核)之後 C1.5b/c 加深。
#    走道通道口固定正對大門「理想位置」;大門若因躲柱被平移,兩者會小錯位。
# 3. 家具擺放規則:床頭靠北外牆、衣櫃貼東側牆近北角、沙發背靠西牆、
#    L 型流理台沿東+北……皆為簡化規則。碰撞檢核已加(C1.5c):家具互不
#    重疊、不擋門的迴轉方塊(以洞口寬×寬的方形近似扇形);「通道走得過去」
#    的通行寬度檢核未做。方桌會自動讓開大門迴轉、擺不下就不擺。
# 4. 集合住宅:單元固定用 one_room_unit(可自帶 UnitSpec);兩端逃生核已加
#    (C1.5a),核開間寬 CORE_W=3.1m、梯段/平台尺寸為經驗值;每戶陽台未加
#    (C1.5b 後續)。
# 5. validate_spec 已含簡化法規/機能檢核(通風/逃生 C1.5a、動線 C1.5b、
#    採光深度 6m/家具碰撞/柱網規則性 C1.5c);真正的法規檢討(採光「面積」
#    比、走廊寬法規、步行逃生距離)未做。採光深度上限 6m 為簡化經驗值
#    (技術規則實為窗高相關)。隔間坐樑吸附容差 0.6m、軸線向隔牆吸附容差
#    0.9m、跨距均勻度上限 1.6 皆為經驗值;僅 X 向吸附(Y 向的帶分界牆
#    天生在軸線上),走道南牆/浴廚分界等次要牆不吸附。
# 6. 柱網(使用者 2026-07-12 定調的結構原則)已落實:等距起手、跨距 3~9m、
#    跨數擇優(孤柱列最少)、軸線挪到主要隔牆(柱藏牆交點)、柱距門窗淨距
#    300。孤柱列無法全消:1 房(無隔牆可吸)、及挪軸會讓跨距失衡(>1.6)
#    的格局會留 1 列孤柱(凸在帶分界牆上)——規則性優先是刻意取捨。
#    未做:上下樓層柱位對齊(單層示範不適用)、停車場柱距配車位、集合住宅
#    柱網(目前 = 單元寬 4m 一跨,偏小;實務常兩戶一跨 8m,需同時對齊
#    分戶牆——待改)。
# =============================================================================
