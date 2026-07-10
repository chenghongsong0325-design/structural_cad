"""戶型組裝模組 —— 把既有模組串成「資料 → 完整平面圖」的生產線。

這是階段 A1(見 ROADMAP.md):用一份 FloorPlanSpec 資料,一次產出一整張
建築平面圖。串起來的模組:
    gridlines(軸網) + members(柱) + wall/wall_join(牆,接角+柱內不畫牆)
    + door_window(門窗) + room(房間標註) + titleblock(標題欄)

繪製順序(draw_floor_plan):
    地界線(BORDER,PHANTOM 線型) → 建築線(ARCH,CENTER 線型,由地界線退縮)
    → 軸網 + 軸距標註 → 柱 → 牆(draw_walls_joined,柱內不畫牆)
    → 門窗 → 房間標註 → 標題欄

尚未實作(FloorPlanSpec 已預留欄位,填了會明確報 NotImplementedError):
    樓梯(stairs)、電梯(elevators)、陽台(balconies)、衛浴廚具設備(fixtures)
    —— 依 ROADMAP 階段 B 逐項補上。

⚠️ 待確認假設(詳見模組結尾 PENDING 區塊):建築線退縮的計算方式、標題欄
   自動擺放位置、房間標註字高等。

典型用法::

    from src.drafting.apartment_plan import demo_spec, draw_floor_plan

    spec = demo_spec()                      # 或自己組一份 FloorPlanSpec
    std = load_standard()
    doc = new_document()
    layers = apply_standard(doc, std)
    draw_floor_plan(doc.modelspace(), spec, layers)
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# 支援直接 `python src/drafting/apartment_plan.py` 執行(把專案根補進 sys.path)。
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from shapely.geometry import Polygon

from src.drafting.door_window import Door, Window
from src.drafting.gridlines import (
    GridSystem,
    build_grid_system,
    draw_grid,
    draw_grid_dimensions,
)
from src.drafting.members import Column, column_corners, draw_column
from src.drafting.room import Room, draw_room_label
from src.drafting.titleblock import TitleBlockData, insert_title_block
from src.drafting.wall import Wall
from src.drafting.wall_join import draw_walls_joined

Point = tuple[float, float]


# ---------------------------------------------------------------------------
# 資料模型
# ---------------------------------------------------------------------------
@dataclass
class DoorPlacement:
    """一扇門放在哪:第幾道牆(wall_index)的第幾個洞口(opening_index)。"""

    wall_index: int
    opening_index: int
    door: Door = field(default_factory=Door)


@dataclass
class WindowPlacement:
    """一扇窗放在哪:同 DoorPlacement。"""

    wall_index: int
    opening_index: int
    window: Window = field(default_factory=Window)


@dataclass
class FloorPlanSpec:
    """一層樓的完整描述 —— draw_floor_plan 的唯一輸入。

    座標一律用世界座標(mm),所有元素自己記絕對位置;
    洞口仍沿用 Wall/Opening 的「距牆起點距離」定位。
    """

    # ── 基地 ──────────────────────────────────────────────────────────
    site_boundary: list[Point]        # 地界線(封閉多邊形角點,首點不重複)
    setback: float                    # 建築線退縮距離(由地界線向內偏移,mm)

    # ── 結構:軸網與柱 ─────────────────────────────────────────────────
    x_spacings: list[float] = field(default_factory=list)
    y_spacings: list[float] = field(default_factory=list)
    grid_origin: Point = (0.0, 0.0)   # 第一條 X/Y 軸線的世界座標
    column_size: float = 500          # 方柱邊長(競賽構造尺寸:柱 50×50cm)
    column_centers: Optional[list[Point]] = None
    # None = 柱自動放在「每個軸網交點」;給列表則只放指定位置。

    # ── 建築:牆 / 房間 / 門窗 ─────────────────────────────────────────
    walls: list[Wall] = field(default_factory=list)
    rooms: list[Room] = field(default_factory=list)
    doors: list[DoorPlacement] = field(default_factory=list)
    windows: list[WindowPlacement] = field(default_factory=list)
    room_text_height: float = 300     # 房間標註字高(待確認)

    # ── 圖面 ──────────────────────────────────────────────────────────
    title_block: Optional[TitleBlockData] = None
    title_insert: Optional[Point] = None   # None = 自動放地界線右下外側

    # ── 尚未實作(ROADMAP 階段 B 逐項補;填了會報 NotImplementedError)──
    stairs: list = field(default_factory=list)      # 尚未實作:樓梯(B1)
    elevators: list = field(default_factory=list)   # 尚未實作:電梯(B3)
    balconies: list = field(default_factory=list)   # 尚未實作:陽台(B3)
    fixtures: list = field(default_factory=list)    # 尚未實作:衛浴廚具(B4)


# ---------------------------------------------------------------------------
# 幾何輔助(純計算,可單獨測試)
# ---------------------------------------------------------------------------
def building_line(spec: FloorPlanSpec) -> list[Point]:
    """由地界線向內退縮 setback,得到建築線多邊形的角點。

    用 shapely 的負向緩衝(buffer(-setback), 斜接角),對矩形/凸多邊形基地
    都成立。退縮到消失(基地太小)會報錯。
    """
    poly = Polygon(spec.site_boundary).buffer(-spec.setback, join_style=2)
    if poly.is_empty:
        raise ValueError(f"建築線退縮 {spec.setback} 後基地消失,請檢查 setback 與基地大小")
    return [(x, y) for x, y in poly.exterior.coords[:-1]]


def build_grid(spec: FloorPlanSpec) -> GridSystem:
    """建立軸網並平移到 grid_origin(gridlines 的座標從 0 起算,這裡加上原點)。"""
    grid = build_grid_system(x_spacings=spec.x_spacings, y_spacings=spec.y_spacings)
    ox, oy = spec.grid_origin
    for axis in grid.x_axes:
        axis.position += ox
    for axis in grid.y_axes:
        axis.position += oy
    return grid


def resolve_columns(spec: FloorPlanSpec, grid: GridSystem) -> list[Column]:
    """決定柱的位置:column_centers=None 時放在每個軸網交點。"""
    if spec.column_centers is not None:
        centers = spec.column_centers
    else:
        centers = [
            (xa.position, ya.position)
            for xa in grid.x_axes
            for ya in grid.y_axes
        ]
    s = spec.column_size
    return [Column(center=c, width=s, depth=s) for c in centers]


def _default_title_insert(spec: FloorPlanSpec) -> Point:
    """標題欄自動位置:地界線右下角外側。

    間隙取 2600:要低於 X 軸編號圈的最低點(軸線外伸 1200 + 圈外推 2000 +
    圈半徑 350 ≈ 3550,再留餘裕),避免④之類的編號圈壓到標題欄。待確認。
    """
    xs = [p[0] for p in spec.site_boundary]
    ys = [p[1] for p in spec.site_boundary]
    return (max(xs) - 6000, min(ys) - 2400 - 2600)


# ---------------------------------------------------------------------------
# 生產線:一次畫完整張圖
# ---------------------------------------------------------------------------
def draw_floor_plan(msp, spec: FloorPlanSpec, layers: dict[str, str]) -> None:
    """依 FloorPlanSpec 畫出一整張平面圖(順序見模組說明)。"""

    # (0) 尚未實作的元素:明確擋下,避免使用者以為有畫。
    for name, items in (
        ("樓梯(stairs)", spec.stairs),
        ("電梯(elevators)", spec.elevators),
        ("陽台(balconies)", spec.balconies),
        ("衛浴廚具(fixtures)", spec.fixtures),
    ):
        if items:
            raise NotImplementedError(f"{name} 尚未實作(見 ROADMAP.md 階段 B)")

    # (1) 地界線(BORDER,PHANTOM)。
    msp.add_lwpolyline(spec.site_boundary, close=True, dxfattribs={"layer": layers["BORDER"]})

    # (2) 建築線(ARCH,CENTER):地界線退縮 setback。
    msp.add_lwpolyline(building_line(spec), close=True, dxfattribs={"layer": layers["ARCH"]})

    # (3) 軸網 + 軸距標註。
    grid = build_grid(spec)
    draw_grid(msp, grid, layers)
    draw_grid_dimensions(msp, grid, layers)

    # (4) 柱。
    columns = resolve_columns(spec, grid)
    for col in columns:
        draw_column(msp, col, layers["COLUMN"])

    # (5) 牆:整組聯集接角,柱範圍內不畫牆。
    if spec.walls:
        draw_walls_joined(
            msp,
            spec.walls,
            layers["A-WALL"],
            subtract=[column_corners(c) for c in columns],
        )

    # (6) 門窗:對齊到指定牆的指定洞口。
    for dp in spec.doors:
        wall = spec.walls[dp.wall_index]
        dp.door.place_in_wall(msp, wall, wall.openings[dp.opening_index], layers)
    for wp in spec.windows:
        wall = spec.walls[wp.wall_index]
        wp.window.place_in_wall(msp, wall, wall.openings[wp.opening_index], layers)

    # (7) 房間標註(名稱 + 面積)。
    for room in spec.rooms:
        draw_room_label(msp, room, layers["A-TEXT"], text_height=spec.room_text_height)

    # (8) 標題欄。
    if spec.title_block is not None:
        insert = spec.title_insert or _default_title_insert(spec)
        insert_title_block(msp, spec.title_block, layers, insert=insert)


# ---------------------------------------------------------------------------
# 示範戶型:16m×14m 基地、退縮 2m → 12m×10m 建築
# 三房兩廳一衛(+廚房):客廳/餐廳/廚房/浴廁/主臥室/臥室A/臥室B
# (自行設計的格局,非檢定考題)
# ---------------------------------------------------------------------------
def demo_spec() -> FloorPlanSpec:
    """回傳示範戶型的 FloorPlanSpec(座標單位 mm)。

    版面(世界座標;建築外圍 2000..14000 × 2000..12000):
        北側(上,y 7000..12000):主臥室(西) | 臥室A(中) | 臥室B(東)
        南側(下,y 2000..7000) :客廳(西,開放連餐廳) | 餐廳(中) | 廚房/浴廁(東)
    """
    from src.drafting.wall import (
        EXTERIOR_WALL_THICKNESS as EXT,   # RC 牆 15cm(競賽構造尺寸)
        INTERIOR_WALL_THICKNESS as INT,   # 1/2B 磚牆 12cm
        Opening,
    )

    walls = [
        # 0 南外牆:大門(客廳,x=5000)+ 餐廳窗(x=9000)
        Wall((2000, 2000), (14000, 2000), EXT,
             openings=[Opening(3000, 1000, "door"), Opening(7000, 1200, "window")]),
        # 1 東外牆:浴廁窗(y=3250)+ 廚房窗(y=5750)
        Wall((14000, 2000), (14000, 12000), EXT,
             openings=[Opening(1250, 800, "window"), Opening(3750, 1200, "window")]),
        # 2 北外牆:臥室A窗(x=8750)+ 臥室B窗(x=12250)
        Wall((14000, 12000), (2000, 12000), EXT,
             openings=[Opening(5250, 1500, "window"), Opening(1750, 1500, "window")]),
        # 3 西外牆:主臥窗(y=9500)+ 客廳窗(y=4500)
        Wall((2000, 12000), (2000, 2000), EXT,
             openings=[Opening(2500, 1500, "window"), Opening(7500, 1800, "window")]),
        # 4 走道牆 y=7000(臥室區/公共區分界):三扇房門
        Wall((2000, 7000), (14000, 7000), INT,
             openings=[Opening(2500, 900, "door"),   # 主臥門(x=4500)
                       Opening(6500, 900, "door"),   # 臥室A門(x=8500)
                       Opening(10000, 900, "door")]),  # 臥室B門(x=12000)
        # 5 主臥/臥室A 隔牆 x=7000
        Wall((7000, 7000), (7000, 12000), INT),
        # 6 臥室A/臥室B 隔牆 x=10500
        Wall((10500, 7000), (10500, 12000), INT),
        # 7 餐廳/(廚房+浴廁)隔牆 x=11000:浴廁門(y=3250)+ 廚房門(y=5750)
        Wall((11000, 2000), (11000, 7000), INT,
             openings=[Opening(1250, 800, "door"), Opening(3750, 900, "door")]),
        # 8 浴廁/廚房 隔牆 y=4500
        Wall((11000, 4500), (14000, 4500), INT),
    ]

    rooms = [
        Room("客廳", [(2000, 2000), (8000, 2000), (8000, 7000), (2000, 7000)], kind="living"),
        Room("餐廳", [(8000, 2000), (11000, 2000), (11000, 7000), (8000, 7000)], kind="dining"),
        Room("浴廁", [(11000, 2000), (14000, 2000), (14000, 4500), (11000, 4500)], kind="bathroom"),
        Room("廚房", [(11000, 4500), (14000, 4500), (14000, 7000), (11000, 7000)], kind="kitchen"),
        Room("主臥室", [(2000, 7000), (7000, 7000), (7000, 12000), (2000, 12000)], kind="bedroom"),
        Room("臥室A", [(7000, 7000), (10500, 7000), (10500, 12000), (7000, 12000)], kind="bedroom"),
        Room("臥室B", [(10500, 7000), (14000, 7000), (14000, 12000), (10500, 12000)], kind="bedroom"),
    ]

    doors = [
        DoorPlacement(0, 0, Door(hinge="left", swing="out")),   # 大門,開向客廳內
        DoorPlacement(4, 0, Door(hinge="left", swing="out")),   # 主臥門,開向主臥
        DoorPlacement(4, 1, Door(hinge="left", swing="out")),   # 臥室A門
        DoorPlacement(4, 2, Door(hinge="left", swing="out")),   # 臥室B門
        DoorPlacement(7, 0, Door(hinge="left", swing="in")),    # 浴廁門,開向浴廁
        DoorPlacement(7, 1, Door(hinge="left", swing="in")),    # 廚房門,開向廚房
    ]
    windows = [
        WindowPlacement(0, 1), WindowPlacement(1, 0), WindowPlacement(1, 1),
        WindowPlacement(2, 0), WindowPlacement(2, 1),
        WindowPlacement(3, 0), WindowPlacement(3, 1),
    ]

    return FloorPlanSpec(
        # 16m×14m 基地,建築線退縮 2m → 建築範圍 12m×10m。
        site_boundary=[(0, 0), (16000, 0), (16000, 14000), (0, 14000)],
        setback=2000,
        # 軸網:X 1~4(跨距 4m×3)、Y A~C(跨距 5m×2),原點在建築西南角。
        x_spacings=[4000, 4000, 4000],
        y_spacings=[5000, 5000],
        grid_origin=(2000, 2000),
        column_size=500,
        walls=walls,
        rooms=rooms,
        doors=doors,
        windows=windows,
        title_block=TitleBlockData(
            drawing_name="標準層平面圖",
            drawing_number="A-01",
            scale="1:100",
            date="2026-07-10",
            drawn_by="成弘",
            checked_by="—",
        ),
    )


def main() -> None:
    """產出示範戶型 DXF:output/apartment_demo.dxf。"""
    from src.standards.loader import apply_standard, load_standard, new_document

    std = load_standard()
    doc = new_document()
    layers = apply_standard(doc, std)
    spec = demo_spec()
    draw_floor_plan(doc.modelspace(), spec, layers)

    out = _PROJECT_ROOT / "output" / "apartment_demo.dxf"
    out.parent.mkdir(exist_ok=True)
    doc.saveas(out)
    print(f"[OK] {out}")
    print(f"     牆 {len(spec.walls)} 道、房間 {len(spec.rooms)} 間、"
          f"門 {len(spec.doors)} 扇、窗 {len(spec.windows)} 扇")


if __name__ == "__main__":
    main()


# =============================================================================
# PENDING(待確認假設彙整)
# =============================================================================
# 1. 建築線:由地界線「等距向內退縮 setback」(shapely 負向緩衝、斜接角)。
#    實務上前後院退縮距離常不同(如前 3m 後 2m);若需要,之後把 setback 改成
#    四向各自的值。待確認。
# 2. 標題欄自動位置:地界線右下角外側(往下 1000 間隙)。放圖紙內的固定位置
#    要等 A2(競賽格式圖框)做圖紙外框時一起處理。待確認。
# 3. 房間標註字高預設 300(比一般文字 250 大一號求醒目)。待確認。
# 4. 柱預設放「每個軸網交點」;實際設計可能取消部分柱(如陽台角),用
#    column_centers 指定即可。
# 5. 示範戶型的格局(房間尺寸、門窗位置)為自行設計的合理值,非任何規範;
#    「三房兩廳一衛」外加了廚房(兩廳格局實務上必有廚房)。
# 6. 樓梯/電梯/陽台/設備尚未實作:spec 有欄位但填了會報 NotImplementedError,
#    依 ROADMAP 階段 B 逐項補上。
# =============================================================================
