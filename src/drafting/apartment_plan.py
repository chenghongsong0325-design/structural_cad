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

from src.drafting.balcony_elevator import (
    Balcony,
    Elevator,
    balcony_walls,
    draw_balcony_railing,
    draw_elevator_symbol,
    elevator_walls,
)
from src.drafting.dim_chains import draw_dim_chains
from src.drafting.fixtures import (
    Counter,
    FixturePlacement,
    draw_counter,
    place_fixture,
)
from src.drafting.door_window import Door, Window
from src.drafting.gridlines import (
    GridSystem,
    build_grid_system,
    draw_grid,
    draw_grid_dimensions,
)
from src.drafting.members import Column, column_corners, draw_column
from src.drafting.annotations import draw_floor_label, place_north_arrow
from src.drafting.room import Room, draw_room_label, draw_room_tag
from src.drafting.stair import Stair, UStair, draw_stair, draw_u_stair
from src.drafting.titleblock import (
    A3_HEIGHT,
    A3_WIDTH,
    SHEET_MARGIN,
    CompetitionTitleData,
    TitleBlockData,
    competition_title_size,
    draw_sheet_border,
    insert_competition_title_block,
    insert_title_block,
)
from src.drafting.wall import Wall
from src.drafting.wall_join import draw_wall_hatch, draw_walls_joined

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
    # 牆體剖面線(B5):True 時 RC 牆(厚≥140)填 ANSI31 斜線、磚牆填 ANSI37。
    wall_hatch: bool = False
    # 樓層標示大字(如 "3F";空字串=不畫)與北向箭頭(B5)。insert=None 自動定位。
    floor_label: str = ""
    floor_label_insert: Optional[Point] = None
    north_arrow: bool = False
    north_arrow_insert: Optional[Point] = None
    # 標題欄:可放一般 TitleBlockData 或競賽格式 CompetitionTitleData。
    title_block: Optional[object] = None
    title_insert: Optional[Point] = None   # None = 自動放右下(有圖框則貼內框右下)
    # 圖紙外框(A3 橫式 1:100)。sheet=True 時畫外框;sheet_origin=None 自動定位。
    sheet: bool = False
    sheet_origin: Optional[Point] = None
    sheet_margin: float = SHEET_MARGIN

    # ── 尺寸鏈(B2):True = 四邊三層(細部/軸距/總長,dim_chains 模組),
    #    False = 只有上/右兩邊的單層軸距(gridlines.draw_grid_dimensions)。
    dim_chains: bool = False

    # ── 樓梯(B1)/ 電梯與陽台(B3)/ 設備家具(B4)──────────────────────────
    stairs: list = field(default_factory=list)      # Stair / UStair
    elevators: list = field(default_factory=list)   # Elevator(牆自動併聯集)
    balconies: list = field(default_factory=list)   # Balcony(牆自動併聯集)
    fixtures: list = field(default_factory=list)    # FixturePlacement / Counter


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


def _auto_sheet_origin(spec: FloorPlanSpec) -> Point:
    """A3 圖紙左下角座標:讓圖面落在圖紙左上區、右下留給標題欄。"""
    xs = [p[0] for p in spec.site_boundary]
    ys = [p[1] for p in spec.site_boundary]
    pad = 3000
    ox = min(xs) - pad - spec.sheet_margin
    oy = max(ys) + pad + spec.sheet_margin - A3_HEIGHT
    return (ox, oy)


def _title_insert(spec: FloorPlanSpec) -> Point:
    """標題欄自動位置。

    有圖框:貼齊圖框內框的右下角。
    無圖框:地界線右下角外側,間隙 2600 以避開 X 軸編號圈(軸線外伸 1200 +
            圈外推 2000 + 圈半徑 350 ≈ 3550)。待確認。
    """
    is_comp = isinstance(spec.title_block, CompetitionTitleData)
    tb_w, tb_h = competition_title_size() if is_comp else (6000.0, 2400.0)

    if spec.sheet:
        ox, oy = spec.sheet_origin or _auto_sheet_origin(spec)
        inner_right = ox + A3_WIDTH - spec.sheet_margin
        inner_bottom = oy + spec.sheet_margin
        return (inner_right - tb_w, inner_bottom)

    xs = [p[0] for p in spec.site_boundary]
    ys = [p[1] for p in spec.site_boundary]
    return (max(xs) - tb_w, min(ys) - tb_h - 2600)


# ---------------------------------------------------------------------------
# 生產線:一次畫完整張圖
# ---------------------------------------------------------------------------
def draw_floor_plan(msp, spec: FloorPlanSpec, layers: dict[str, str]) -> None:
    """依 FloorPlanSpec 畫出一整張平面圖(順序見模組說明)。"""

    # (0.5) 圖紙外框(A3 橫式 1:100),最外層先畫。
    if spec.sheet:
        origin = spec.sheet_origin or _auto_sheet_origin(spec)
        draw_sheet_border(msp, layers["OTHER"], origin=origin, margin=spec.sheet_margin)

    # (1) 地界線(BORDER,PHANTOM)。
    msp.add_lwpolyline(spec.site_boundary, close=True, dxfattribs={"layer": layers["BORDER"]})

    # (2) 建築線(ARCH,CENTER):地界線退縮 setback。
    msp.add_lwpolyline(building_line(spec), close=True, dxfattribs={"layer": layers["ARCH"]})

    # (3) 軸網 + 尺寸標註。
    #     開尺寸鏈時:編號圈退到最外層尺寸(2800)之外,並用 dim_chains 標四邊三層;
    #     否則維持單層軸距標註(上/右兩邊)。
    grid = build_grid(spec)
    if spec.dim_chains:
        draw_grid(msp, grid, layers, bubble_offset=2600)
        draw_dim_chains(msp, spec, layers)
    else:
        draw_grid(msp, grid, layers)
        draw_grid_dimensions(msp, grid, layers)

    # (4) 柱。
    columns = resolve_columns(spec, grid)
    for col in columns:
        draw_column(msp, col, layers["COLUMN"])

    # (5) 牆:整組聯集接角,柱範圍內不畫牆。
    #     陽台矮牆、電梯井牆一起併入聯集 → 與建築牆自然接角。
    all_walls = list(spec.walls)
    for elev in spec.elevators:
        all_walls += elevator_walls(elev)
    for bal in spec.balconies:
        all_walls += balcony_walls(bal)
    col_rings = [column_corners(c) for c in columns]
    if all_walls:
        draw_walls_joined(msp, all_walls, layers["A-WALL"], subtract=col_rings)

    # (5.5) 牆體剖面線(B5):RC 牆(厚≥140,外牆/電梯井)斜線、磚牆(內牆/陽台)
    #       交叉線;柱內不填。兩組交會處可能小範圍疊填(見 PENDING)。
    if spec.wall_hatch and all_walls:
        rc = [w for w in all_walls if w.thickness >= 140]
        brick = [w for w in all_walls if w.thickness < 140]
        if rc:
            draw_wall_hatch(msp, rc, layers["A-WALL"], subtract=col_rings,
                            pattern="ANSI31", scale=30)
        if brick:
            draw_wall_hatch(msp, brick, layers["A-WALL"], subtract=col_rings,
                            pattern="ANSI37", scale=30)

    # (6) 門窗:對齊到指定牆的指定洞口。
    for dp in spec.doors:
        wall = spec.walls[dp.wall_index]
        dp.door.place_in_wall(msp, wall, wall.openings[dp.opening_index], layers)
    for wp in spec.windows:
        wall = spec.walls[wp.wall_index]
        wp.window.place_in_wall(msp, wall, wall.openings[wp.opening_index], layers)

    # (7) 房間標註(名稱 + 面積)+ 房型帶框標籤(有 code 的房間)。
    for room in spec.rooms:
        draw_room_label(msp, room, layers["A-TEXT"], text_height=spec.room_text_height)
        draw_room_tag(msp, room, layers["A-TEXT"])

    # (7.5) 樓梯(踏步線/折斷線/方向箭頭,HANDRAIL 層;直梯或折返梯)。
    for stair in spec.stairs:
        if isinstance(stair, UStair):
            draw_u_stair(msp, stair, layers)
        else:
            draw_stair(msp, stair, layers)

    # (7.6) 電梯轎廂符號 + 陽台欄杆線(牆已在步驟 5 併入聯集)。
    for elev in spec.elevators:
        draw_elevator_symbol(msp, elev, layers)
    for bal in spec.balconies:
        draw_balcony_railing(msp, bal, layers)

    # (7.7) 衛浴廚具設備與家具(圖塊 / 參數式流理台,OTHER 層)。
    for fx in spec.fixtures:
        if isinstance(fx, Counter):
            draw_counter(msp, fx, layers)
        else:
            place_fixture(msp, fx, layers)

    # (8) 標題欄(競賽格式或一般格式)。
    if spec.title_block is not None:
        insert = spec.title_insert or _title_insert(spec)
        if isinstance(spec.title_block, CompetitionTitleData):
            insert_competition_title_block(msp, spec.title_block, layers, insert=insert)
        else:
            insert_title_block(msp, spec.title_block, layers, insert=insert)

    # (9) 圖面配件:樓層標示大字 + 北向箭頭(B5)。
    xs = [p[0] for p in spec.site_boundary]
    ys = [p[1] for p in spec.site_boundary]
    if spec.floor_label:
        if spec.floor_label_insert is not None:
            pos = spec.floor_label_insert
        elif spec.sheet:
            ox, oy = spec.sheet_origin or _auto_sheet_origin(spec)
            pos = (ox + spec.sheet_margin + 2200, oy + spec.sheet_margin + 1800)
        else:
            pos = (min(xs) + 1500, min(ys) - 5000)
        draw_floor_label(msp, spec.floor_label, pos, layers)
    if spec.north_arrow:
        if spec.north_arrow_insert is not None:
            pos = spec.north_arrow_insert
        elif spec.sheet:
            ox, oy = spec.sheet_origin or _auto_sheet_origin(spec)
            pos = (ox + A3_WIDTH - spec.sheet_margin - 2200,
                   oy + A3_HEIGHT - spec.sheet_margin - 2200)
        else:
            pos = (max(xs) + 2500, max(ys))
        place_north_arrow(msp, pos, layers)


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
        # ── 樓梯間(真實建築的樓梯必須有牆圍起來)────────────────────────
        # 9 樓梯間西牆 x=6600:樓梯間的門(y=2700,開向客廳)
        Wall((6600, 2000), (6600, 4800), INT,
             openings=[Opening(700, 900, "door")]),
        # 10 樓梯間東牆 x=8000(兼客廳/餐廳分界的南段)
        Wall((8000, 2000), (8000, 4800), INT),
        # 註:樓梯間北牆(y=4800)由電梯井的南牆(RC20)取代——電梯疊在樓梯間
        #    正上方(x6600~8000 × y4800~7000),牆由 spec.elevators 自動併入。
    ]

    # 房型代碼(仿真實建案:同類型同代碼)——X01樓梯間/X02電梯/X03客廳/
    # X04餐廳/X05臥室/X07廚房/X08浴廁。代碼規則待確認。
    rooms = [
        # 客廳(東側整條讓給 樓梯間+電梯 的垂直動線核)。
        Room("客廳", [(2000, 2000), (6600, 2000), (6600, 7000), (2000, 7000)],
             kind="living", code="X03"),
        Room("樓梯間", [(6600, 2000), (8000, 2000), (8000, 4800), (6600, 4800)],
             kind="stair", code="X01"),
        Room("電梯", [(6600, 4800), (8000, 4800), (8000, 7000), (6600, 7000)],
             kind="elevator", code="X02"),
        Room("餐廳", [(8000, 2000), (11000, 2000), (11000, 7000), (8000, 7000)],
             kind="dining", code="X04"),
        Room("浴廁", [(11000, 2000), (14000, 2000), (14000, 4500), (11000, 4500)],
             kind="bathroom", code="X08"),
        Room("廚房", [(11000, 4500), (14000, 4500), (14000, 7000), (11000, 7000)],
             kind="kitchen", code="X07"),
        Room("主臥室", [(2000, 7000), (7000, 7000), (7000, 12000), (2000, 12000)],
             kind="bedroom", code="X05"),
        Room("臥室A", [(7000, 7000), (10500, 7000), (10500, 12000), (7000, 12000)],
             kind="bedroom", code="X05"),
        Room("臥室B", [(10500, 7000), (14000, 7000), (14000, 12000), (10500, 12000)],
             kind="bedroom", code="X05"),
    ]

    doors = [
        DoorPlacement(0, 0, Door(hinge="left", swing="out")),   # 大門,開向客廳內
        DoorPlacement(4, 0, Door(hinge="left", swing="out")),   # 主臥門,開向主臥
        DoorPlacement(4, 1, Door(hinge="left", swing="out")),   # 臥室A門
        DoorPlacement(4, 2, Door(hinge="left", swing="out")),   # 臥室B門
        DoorPlacement(7, 0, Door(hinge="left", swing="in")),    # 浴廁門,開向浴廁
        DoorPlacement(7, 1, Door(hinge="left", swing="in")),    # 廚房門,開向廚房
        DoorPlacement(9, 0, Door(hinge="left", swing="out")),   # 樓梯間門,開向客廳
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
        # 樓梯:放在「樓梯間」牆內(x6600~8000 × y2000~4800,內淨空約 1280×2665)。
        # 直梯往北上樓,9 級 × 260 = 2340 ≤ 2500。
        stairs=[Stair(origin=(6680, 2150), width=1200, length=2500,
                      direction="north", steps=9, tread=260)],
        # 電梯:疊在樓梯間正上方(井道中心線 x6600~8000 × y4800~7000,RC20 牆),
        # 門洞開東面(通餐廳);井牆自動併入牆聯集。
        elevators=[Elevator(origin=(6600, 4800), width=1400, depth=2200,
                            door_side="east")],
        # 陽台:突出建築南側、正對餐廳窗(x8300~10700 × y800~2000,矮牆厚 100,
        # 北邊貼建築外牆中心線,牆自動併入聯集);欄杆線掛 HANDRAIL。
        balconies=[Balcony(origin=(8300, 800), width=2400, depth=1200,
                           attach="north")],
        # 設備家具(插入點=貼牆邊中點在「牆內面」上;旋轉:南牆0/北牆180/東牆90/西牆270)
        fixtures=[
            # 浴廁:馬桶+洗手台靠東牆、浴缸沿南牆(避開門的迴轉)
            FixturePlacement("toilet", (13925, 3700), 90),
            FixturePlacement("basin", (13925, 2800), 90),
            FixturePlacement("bathtub", (12800, 2075), 0),
            # 廚房:L 型流理台沿東牆+北牆(水槽在北段)
            Counter(start=(13925, 4560), end=(13925, 6940)),
            Counter(start=(13325, 6940), end=(11060, 6940), sink=True),
            # 主臥:雙人床床頭靠北牆、衣櫃靠東隔牆
            FixturePlacement("bed_double", (4200, 11925), 180),
            FixturePlacement("wardrobe", (6940, 8300), 90),
            # 臥室A/B:單人床床頭靠北牆;臥室B 加衣櫃靠西隔牆
            FixturePlacement("bed_single", (8750, 11925), 180),
            FixturePlacement("bed_single", (12250, 11925), 180),
            FixturePlacement("wardrobe", (10560, 9800), 270),
            # 客廳:沙發背靠西牆、方桌居中;餐廳:餐桌椅
            FixturePlacement("sofa3", (2075, 4800), 270),
            FixturePlacement("table4", (4200, 4800), 0),
            FixturePlacement("table4", (9500, 3600), 0),
        ],
        dim_chains=True,   # 四邊三層尺寸鏈(細部/軸距/總長)
        # 牆體剖面線預設關閉:1:100 施工/銷售平面圖(如真實建案圖)牆多為空心
        # 雙線、不填剖面線,保持清爽。填充僅用於結構圖/大比例詳圖。功能仍在,
        # 需要時把 wall_hatch 設 True。
        wall_hatch=False,
        floor_label="2F",  # 樓層標示大字
        north_arrow=True,  # 北向箭頭
        sheet=True,   # A3 橫式圖框
        # 競賽圖框:欄位標題保留、值一律留空(比照檢定發下的空白圖框,應檢人自填)。
        # 只保留頂端類別橫幅(考項名稱)。
        title_block=CompetitionTitleData(),
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
