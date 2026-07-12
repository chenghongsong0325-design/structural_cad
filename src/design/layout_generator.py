"""規則式格局產生器(C1)—— 需求 → FloorPlanSpec → 完整平面圖。

「說出需求就設計出圖」的核心引擎。階段 B 的圖面元素已全數完成,所以這裡
產出的是**完整的圖**:牆/門窗/家具設備/房型帶框標籤/四邊三層尺寸鏈/樓層
標示/北向箭頭/A3 圖框,一次到位。

支援兩種建築類型:

  1. HouseBrief(單戶住宅):兩帶式格局——北帶臥室區(主臥加大)、南帶
     公共區(客廳西、餐廳中、東側服務核=浴廁+廚房);餐廳太窄自動併入
     客廳。家具設備依房型自動擺放。
  2. CorridorBrief(集合住宅):用 B6 的標準單元(place_unit)沿雙邊走廊
     重複 N 戶鏡射對排。

共同規則:
  * 軸網跨距自動決定(目標 4.5m、合理範圍內),柱在交點、隔間帶分界牆
    與軸線對齊(柱長在牆裡)。
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
from dataclasses import dataclass
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
from src.drafting.unit import UnitSpec, one_room_unit, place_unit
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
TARGET_BAY = 4500            # 軸網目標跨距
BAY_RANGE = (2, 4)           # X 向跨數上下限
DOOR_WIDTH = 900
ENTRY_DOOR_WIDTH = 1000
WINDOW_WIDTHS = {"bedroom": 1500, "living": 1800, "dining": 1200,
                 "kitchen": 1200, "bathroom": 800}
ROOM_CODES = {"living": "X03", "dining": "X04", "bedroom": "X05",
              "kitchen": "X07", "bathroom": "X08", "corridor": "X00"}
COLUMN_CLEARANCE = 150       # 洞口與柱面的最小淨距


# ---------------------------------------------------------------------------
# 需求
# ---------------------------------------------------------------------------
@dataclass
class HouseBrief:
    """單戶住宅需求:基地寬深 + 臥室數(1~4)。"""

    site_width: float
    site_depth: float
    bedrooms: int = 3
    setback: float = 2000
    column_size: float = 500
    floor_label: str = "1F"


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
    wl_zone = W - ws
    merged_dining = wl_zone * 0.45 < MIN_DINING_WIDTH
    living_e = sx if merged_dining else bx0 + wl_zone * 0.55
    bath_d = _clamp(ds * 0.45, *BATH_DEPTH_RANGE)
    yb = by0 + bath_d

    # ── 軸網 ─────────────────────────────────────────────────────────
    nx = int(_clamp(round(W / TARGET_BAY), *BAY_RANGE))
    grid_x = [bx0 + i * (W / nx) for i in range(nx + 1)]
    col = brief.column_size
    grid_y = [by0, yd, by1]

    blocked_s = _blocked([gx - bx0 for gx in grid_x], col)
    blocked_n = _blocked([bx1 - gx for gx in grid_x], col)
    blocked_e = _blocked([gy - by0 for gy in grid_y], col)
    blocked_w = _blocked([by1 - gy for gy in grid_y], col)

    # ── 牆 ───────────────────────────────────────────────────────────
    walls = [
        Wall((bx0, by0), (bx1, by0), EXT),    # 0 南
        Wall((bx1, by0), (bx1, by1), EXT),    # 1 東
        Wall((bx1, by1), (bx0, by1), EXT),    # 2 北
        Wall((bx0, by1), (bx0, by0), EXT),    # 3 西
        Wall((bx0, yd), (bx1, yd), INT),      # 4 帶分界牆
        Wall((sx, by0), (sx, yd), INT),       # 5 服務核西牆
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

    # 臥室:門在帶分界牆、窗在北牆。
    for i in range(brief.bedrooms):
        x_l, x_r = bed_x[i], bed_x[i + 1]
        cx = (x_l + x_r) / 2
        op = add_opening(4, cx - bx0, DOOR_WIDTH, "door",
                         x_l - bx0, x_r - bx0, _blocked([gx - bx0 for gx in grid_x], col))
        doors.append(DoorPlacement(4, op, Door(hinge="left", swing="out")))
        op = add_opening(2, bx1 - cx, WINDOW_WIDTHS["bedroom"], "window",
                         bx1 - x_r, bx1 - x_l, blocked_n)
        windows.append(WindowPlacement(2, op))

    # 主臥西窗、客廳西窗。
    op = add_opening(3, by1 - (yd + by1) / 2, WINDOW_WIDTHS["bedroom"], "window",
                     0, by1 - yd, blocked_w)
    windows.append(WindowPlacement(3, op))
    living_cy = (by0 + yd) / 2
    op = add_opening(3, by1 - living_cy, WINDOW_WIDTHS["living"], "window",
                     by1 - yd, by1 - by0, blocked_w)
    windows.append(WindowPlacement(3, op))

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
    kitchen_cy = (yb + yd) / 2
    op = add_opening(5, kitchen_cy - by0, DOOR_WIDTH, "door", yb - by0, yd - by0, [])
    doors.append(DoorPlacement(5, op, Door(hinge="left", swing="in")))
    op = add_opening(1, kitchen_cy - by0, WINDOW_WIDTHS["kitchen"], "window",
                     yb - by0, yd - by0, blocked_e)
    windows.append(WindowPlacement(1, op))

    # ── 房間 ─────────────────────────────────────────────────────────
    rooms: list[Room] = []
    if merged_dining:
        rooms.append(Room("客餐廳", [(bx0, by0), (sx, by0), (sx, yd), (bx0, yd)],
                          kind="living", code=ROOM_CODES["living"]))
    else:
        rooms.append(Room("客廳", [(bx0, by0), (living_e, by0), (living_e, yd), (bx0, yd)],
                          kind="living", code=ROOM_CODES["living"]))
        rooms.append(Room("餐廳", [(living_e, by0), (sx, by0), (sx, yd), (living_e, yd)],
                          kind="dining", code=ROOM_CODES["dining"]))
    rooms.append(Room("浴廁", [(sx, by0), (bx1, by0), (bx1, yb), (sx, yb)],
                      kind="bathroom", code=ROOM_CODES["bathroom"]))
    rooms.append(Room("廚房", [(sx, yb), (bx1, yb), (bx1, yd), (sx, yd)],
                      kind="kitchen", code=ROOM_CODES["kitchen"]))
    bed_names = ["主臥室", "臥室A", "臥室B", "臥室C"]
    for i in range(brief.bedrooms):
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
    # 客廳:沙發背靠西牆、方桌居中偏東。
    fixtures.append(FixturePlacement("sofa3", (bx0 + 75, living_cy), 270))
    fixtures.append(FixturePlacement("table4", ((bx0 + living_e) / 2 + 500, living_cy), 0))
    # 餐廳(獨立時):餐桌。
    if not merged_dining:
        fixtures.append(FixturePlacement("table4", ((living_e + sx) / 2, living_cy), 0))
    # 浴廁:馬桶+洗手台靠東牆(避開西側門的迴轉)。
    fixtures.append(FixturePlacement("toilet", (bx1 - 75, by0 + bath_d - 500), 90))
    fixtures.append(FixturePlacement("basin", (bx1 - 75, by0 + 600), 90))
    # 廚房:L 型流理台(東牆段 + 北段,水槽在北段;北段東端讓開轉角、西端讓開門迴轉)。
    fixtures.append(Counter(start=(bx1 - 75, yb + 60), end=(bx1 - 75, yd - 60)))
    fixtures.append(Counter(start=(bx1 - 675, yd - 60), end=(sx + 1000, yd - 60), sink=True))

    spec = FloorPlanSpec(
        site_boundary=[(0, 0), (brief.site_width, 0),
                       (brief.site_width, brief.site_depth), (0, brief.site_depth)],
        setback=brief.setback,
        x_spacings=[W / nx] * nx,
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
    if not 2 <= brief.units_per_row <= 10:
        raise ValueError(f"每排 2~10 戶,收到 {brief.units_per_row}")

    unit = brief.unit or one_room_unit()
    n = brief.units_per_row
    x0 = y0 = brief.setback
    y_corr = y0 + unit.depth
    y_top = y_corr + brief.corridor_width
    bx1 = x0 + n * unit.width
    by1 = y_top + unit.depth

    spec = FloorPlanSpec(
        site_boundary=[(0, 0), (bx1 + brief.setback, 0),
                       (bx1 + brief.setback, by1 + brief.setback),
                       (0, by1 + brief.setback)],
        setback=brief.setback,
        x_spacings=[unit.width] * n,
        y_spacings=[unit.depth, brief.corridor_width, unit.depth],
        grid_origin=(x0, y0),
        column_size=brief.column_size,
        walls=[Wall((x0, y0), (x0, by1), EXT),      # 西端牆
               Wall((bx1, y0), (bx1, by1), EXT)],   # 東端牆
        rooms=[Room("走廊", [(x0, y_corr), (bx1, y_corr), (bx1, y_top), (x0, y_top)],
                    kind="corridor", code=ROOM_CODES["corridor"])],
        dim_chains=True, sheet=True,
        floor_label=brief.floor_label, north_arrow=True,
        title_block=CompetitionTitleData(),
    )
    for i in range(n):
        ux = x0 + i * unit.width
        place_unit(spec, unit, origin=(ux, y_top))                 # 上排
        place_unit(spec, unit, origin=(ux, y0), mirror_y=True)     # 下排(對排)
    return spec


def generate_floor_plan(brief: Brief) -> FloorPlanSpec:
    """需求 → FloorPlanSpec(已通過 validate_spec,可直接餵 draw_floor_plan)。"""
    if isinstance(brief, HouseBrief):
        spec = _generate_house(brief)
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
    臥室/客廳 有窗、所有洞口不壓柱(含淨距)。
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
        if room.kind in ("bedroom", "bathroom", "kitchen"):
            if openings_on(poly, "door") < 1:
                problems.append(f"{room.name} 沒有門")
        if room.kind in ("bedroom", "living"):
            if openings_on(poly, "window") < 1:
                problems.append(f"{room.name} 沒有窗")

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
#    4.5m、走廊 1.8m 等)皆為經驗值。
# 2. 單戶固定「兩帶式、朝南入口」;浴室 1 間;無垂直動線核(單層示範)。
#    變化維度(朝向/雙衛/樓梯電梯核/走道式)之後 C1.5/B2' 加深。
# 3. 家具擺放規則:床頭靠北外牆、衣櫃貼東側牆近北角、沙發背靠西牆、
#    L 型流理台沿東+北……皆為簡化規則;未做「家具不擋門窗」的完整碰撞檢查
#    (門的迴轉半徑有手動讓開,見程式註解)。
# 4. 集合住宅:單元固定用 one_room_unit(可自帶 UnitSpec);樓梯/電梯核
#    未放(實際建案在兩端/中段);走廊兩端目前以端牆封閉。
# 5. validate_spec 檢核的是幾何/開口正確性,尚未含法規檢討(採光面積比、
#    走廊寬法規、逃生距離)。
# =============================================================================
