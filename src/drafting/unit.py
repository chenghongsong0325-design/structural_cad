"""標準單元重複組合 —— ROADMAP 階段 B6(真實建案的關鍵)。

真實集合住宅是「同一房型單元沿走廊重複幾十次(含鏡射對排)」。這個模組:

  1. UnitSpec:一個房型單元的完整描述——牆/門窗/房間/設備,全部用
     「以單元原點(左下角)為基準的局部座標」。
  2. place_unit(target, unit, origin, mirror_x, mirror_y):把單元
     平移(+鏡射)展開成世界座標,直接「併入」目標 FloorPlanSpec
     (牆的索引自動偏移,門窗照樣對到自己單元的牆)。

鏡射的正確性(重點,測試都有驗):
  * 牆:兩端點各自變換;洞口位置是「距牆起點的距離」,平移/鏡射都是等距
    變換,距離不變 → 洞口位置/寬度原樣保留。
  * 門的開向:鏡射會翻轉「左右手」——牆的 +n(行進方向左手側)在鏡射後
    對應到原本的右手側,所以「奇數次鏡射」時 swing 要翻(out↔in);
    hinge(靠起點/終點側)是距離概念,不用翻。
  * 家具旋轉:局部 +Y 朝向角 = 90°+r;mirror_x 把角度 α→180°−α ⇒ r'=−r;
    mirror_y 把 α→−α ⇒ r'=180°−r。兩者都鏡射 = 旋轉 180°(不翻)。
    (非對稱圖塊如浴缸排水孔,鏡射後只近似——見 PENDING。)
  * 流理台:檯面伸向行進方向「左手側」,奇數次鏡射會跑到錯的一側 →
    交換 start/end 修正。

典型用法::

    unit = one_room_unit()                      # 或自己組一個 UnitSpec
    spec = FloorPlanSpec(...)
    for i in range(4):
        place_unit(spec, unit, origin=(2000 + i * unit.width, 9800))          # 上排
        place_unit(spec, unit, origin=(2000 + i * unit.width, 2000), mirror_y=True)  # 下排(對排)
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

# 支援直接 `python src/drafting/unit.py` 執行(把專案根補進 sys.path)。
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from src.drafting.apartment_plan import (
    DoorPlacement,
    FloorPlanSpec,
    WindowPlacement,
)
from src.drafting.balcony_elevator import Balcony
from src.drafting.door_window import Door, Window
from src.drafting.fixtures import Counter, FixturePlacement
from src.drafting.room import Room
from src.drafting.wall import Opening, Wall

Point = tuple[float, float]


# ---------------------------------------------------------------------------
# 資料模型
# ---------------------------------------------------------------------------
@dataclass
class UnitSpec:
    """一個房型單元(局部座標,原點=單元左下角)。

    walls/rooms/doors/windows/fixtures 的意義與 FloorPlanSpec 相同,
    只是座標以單元原點為基準;doors/windows 的 wall_index 指「單元內」
    walls 清單的索引。
    """

    name: str                 # 房型名(如 "套房")
    width: float              # 單元寬(沿走廊方向,mm)
    depth: float              # 單元深(垂直走廊,mm)
    walls: list[Wall] = field(default_factory=list)
    rooms: list[Room] = field(default_factory=list)
    doors: list[DoorPlacement] = field(default_factory=list)
    windows: list[WindowPlacement] = field(default_factory=list)
    fixtures: list = field(default_factory=list)   # FixturePlacement / Counter
    balconies: list[Balcony] = field(default_factory=list)  # 對外側陽台(C1.5b)


# ---------------------------------------------------------------------------
# 座標/屬性變換
# ---------------------------------------------------------------------------
def _t_point(unit: UnitSpec, origin: Point, mx: bool, my: bool, p: Point) -> Point:
    x, y = p
    if mx:
        x = unit.width - x
    if my:
        y = unit.depth - y
    return (origin[0] + x, origin[1] + y)


def _t_rotation(mx: bool, my: bool, r: float) -> float:
    """家具旋轉角變換(局部 +Y 朝向角 = 90°+r 的鏡射結果反推)。"""
    if mx:
        r = -r
    if my:
        r = 180.0 - r
    return r % 360.0


def _t_swing(mirrored: bool, swing: str) -> str:
    """奇數次鏡射翻轉門的開向(牆的左右手互換)。"""
    if not mirrored:
        return swing
    return "in" if swing == "out" else "out"


_FLIP_X = {"east": "west", "west": "east"}      # mirror_x 翻左右
_FLIP_Y = {"north": "south", "south": "north"}  # mirror_y 翻上下


def _t_balcony(unit: UnitSpec, origin: Point, mx: bool, my: bool,
               bal: Balcony) -> Balcony:
    """陽台平移 + 鏡射:矩形是軸對齊,取變換後兩對角點的最小角當新原點;
    貼建築的邊(attach)隨鏡射翻面(mx 翻東西、my 翻南北)。"""
    c1 = _t_point(unit, origin, mx, my, bal.origin)
    c2 = _t_point(unit, origin, mx, my,
                  (bal.origin[0] + bal.width, bal.origin[1] + bal.depth))
    attach = bal.attach
    if mx:
        attach = _FLIP_X.get(attach, attach)
    if my:
        attach = _FLIP_Y.get(attach, attach)
    return Balcony(
        origin=(min(c1[0], c2[0]), min(c1[1], c2[1])),
        width=bal.width, depth=bal.depth, attach=attach,
        wall_thickness=bal.wall_thickness,
    )


def place_unit(
    target: FloorPlanSpec,
    unit: UnitSpec,
    origin: Point,
    *,
    mirror_x: bool = False,
    mirror_y: bool = False,
) -> None:
    """把一個單元展開(平移 + 鏡射)併入 target FloorPlanSpec。

    牆附加在 target.walls 末尾,單元內 doors/windows 的 wall_index
    自動加上偏移;每次呼叫都建立全新物件(同一 UnitSpec 可重複展開)。
    """
    mirrored = mirror_x != mirror_y            # 奇數次鏡射(左右手翻轉)
    tp = lambda p: _t_point(unit, origin, mirror_x, mirror_y, p)  # noqa: E731
    base = len(target.walls)

    # 牆(洞口為「距起點距離」,等距變換下不變,直接複製)。
    for w in unit.walls:
        target.walls.append(Wall(
            start=tp(w.start), end=tp(w.end), thickness=w.thickness,
            openings=[Opening(op.position, op.width, op.kind) for op in w.openings],
        ))

    # 門(索引偏移;奇數次鏡射翻 swing)/ 窗(索引偏移即可)。
    for dp in unit.doors:
        target.doors.append(DoorPlacement(
            base + dp.wall_index, dp.opening_index,
            Door(hinge=dp.door.hinge,
                 swing=_t_swing(mirrored, dp.door.swing),
                 width=dp.door.width),
        ))
    for wp in unit.windows:
        target.windows.append(WindowPlacement(
            base + wp.wall_index, wp.opening_index,
            Window(lines=wp.window.lines, width=wp.window.width),
        ))

    # 房間。
    for r in unit.rooms:
        target.rooms.append(Room(
            name=r.name, points=[tp(p) for p in r.points],
            kind=r.kind, code=r.code, note=r.note,
        ))

    # 設備家具(奇數次鏡射:流理台交換起訖點以保住檯面側邊)。
    for fx in unit.fixtures:
        if isinstance(fx, Counter):
            a, b = tp(fx.start), tp(fx.end)
            if mirrored:
                a, b = b, a
            target.fixtures.append(Counter(start=a, end=b, depth=fx.depth, sink=fx.sink))
        else:
            target.fixtures.append(FixturePlacement(
                name=fx.name, insert=tp(fx.insert),
                rotation=_t_rotation(mirror_x, mirror_y, fx.rotation),
            ))

    # 陽台(對外側;牆會在生產線併入聯集,貼建築那邊隨鏡射翻面)。
    for bal in unit.balconies:
        target.balconies.append(
            _t_balcony(unit, origin, mirror_x, mirror_y, bal))


# ---------------------------------------------------------------------------
# 示範:套房單元 + 雙邊走廊(4+4 戶鏡射對排)
# ---------------------------------------------------------------------------
def one_room_unit() -> UnitSpec:
    """套房單元 4m×6m(局部座標;走廊在南側 y=0,對外採光在北 y=6000)。

    「能住的家」版格局(C2 真實建案差距 A1/A3):
      * 南側服務帶(y 0~2000):西南角浴廁 1.8×2.0(貼走廊無窗 → 機械排風),
        東側玄關落塵區 2.2×1.1(大門進來先到玄關、鞋櫃貼隔戶牆)。
      * 玄關北面接開放起居室(套房主空間,含床/衣櫃 + 進門一側的一字型
        小廚房流理台含水槽);對外窗+工作陽台在北。
    廚房是開放式(不另隔牆、無獨立門),以流理台呈現——小套房實務畫法。
    西側牆為與鄰戶共用的分戶牆(磚 120);單元只帶「西牆」,連排時右鄰的
    西牆就是自己的東牆,排尾由呼叫端補端牆。
    """
    from src.drafting.wall import (
        EXTERIOR_WALL_THICKNESS as EXT,
        INTERIOR_WALL_THICKNESS as INT,
    )

    walls = [
        # 0 南牆(走廊側,RC150):入口門(玄關段,偏東讓開浴廁)
        Wall((0, 0), (4000, 0), EXT, openings=[Opening(2900, 900, "door")]),
        # 1 北牆(對外,RC150):窗(中央)
        Wall((0, 6000), (4000, 6000), EXT, openings=[Opening(2000, 1500, "window")]),
        # 2 西分戶牆(磚120)
        Wall((0, 0), (0, 6000), INT),
        # 3 浴廁東牆(磚120):浴廁門(從玄關進浴廁)
        Wall((1800, 0), (1800, 2000), INT, openings=[Opening(1000, 750, "door")]),
        # 4 浴廁北牆(磚120)
        Wall((0, 2000), (1800, 2000), INT),
    ]
    rooms = [
        # 浴廁在單元內側(貼走廊)無對外窗 → 依法需機械排風,標示於圖(C1.5a)。
        Room("浴廁", [(0, 0), (1800, 0), (1800, 2000), (0, 2000)],
             kind="bathroom", code="X08", note="機械排風"),
        # 玄關落塵區(大門內側 2.2×1.1;開放接起居室,無內門)。
        Room("玄關", [(1800, 0), (4000, 0), (4000, 1100), (1800, 1100)],
             kind="foyer", code="X10"),
        # 起居室(套房主空間,L 形:扣掉浴廁與玄關;開放式小廚房在東側)。
        Room("起居室", [(0, 2000), (1800, 2000), (1800, 1100), (4000, 1100),
                        (4000, 6000), (0, 6000)],
             kind="living", code="X06"),
    ]
    doors = [
        DoorPlacement(0, 0, Door(hinge="left", swing="out")),   # 入口,開向室內(+n)
        DoorPlacement(3, 0, Door(hinge="left", swing="out")),   # 浴廁門,開向浴廁
    ]
    windows = [WindowPlacement(1, 0)]
    fixtures = [
        FixturePlacement("toilet", (60, 1200), 270),     # 貼西牆,朝 +X
        FixturePlacement("basin", (900, 1940), 180),     # 貼浴廁北牆,朝 -Y
        FixturePlacement("shoe_cabinet", (3940, 650), 90),   # 玄關,貼東(隔戶)牆朝西
        FixturePlacement("bed_double", (2900, 5925), 180),   # 床頭貼北牆
        FixturePlacement("wardrobe", (60, 3000), 270),   # 貼西牆
        # 開放式小廚房:一字型流理台貼東牆(玄關北面、起居室東下角),含水槽。
        Counter(start=(3940, 1300), end=(3940, 2500), sink=True),
    ]
    # 對外(北)側工作陽台:放冷氣/曬衣,貼北牆(南邊不畫牆),外推 1.2m。
    balconies = [Balcony(origin=(800, 6000), width=2400, depth=1200, attach="south")]
    return UnitSpec(name="套房", width=4000, depth=6000, walls=walls,
                    rooms=rooms, doors=doors, windows=windows, fixtures=fixtures,
                    balconies=balconies)


def demo_corridor_spec() -> FloorPlanSpec:
    """雙邊走廊示範:套房 ×(上排 4 戶 + 下排 4 戶鏡射對排)。

    版面(世界座標):下排單元 y2000~8000(鏡射,採光朝南)、走廊 y8000~9800、
    上排單元 y9800~15800(採光朝北);單元寬 4000 × 4 戶 = 建築寬 16000。
    """
    from src.drafting.titleblock import CompetitionTitleData
    from src.drafting.wall import EXTERIOR_WALL_THICKNESS as EXT

    unit = one_room_unit()
    n = 4                       # 每排戶數
    x0, y0 = 2000, 2000        # 建築西南角
    corridor_w = 1800
    y_corr0 = y0 + unit.depth              # 走廊下緣 8000
    y_top = y_corr0 + corridor_w           # 上排原點 y 9800
    bx1 = x0 + n * unit.width              # 建築東緣 18000
    by1 = y_top + unit.depth               # 建築北緣 15800

    spec = FloorPlanSpec(
        site_boundary=[(0, 0), (bx1 + 2000, 0), (bx1 + 2000, by1 + 2000), (0, by1 + 2000)],
        setback=2000,
        x_spacings=[unit.width] * n,
        y_spacings=[unit.depth, corridor_w, unit.depth],
        grid_origin=(x0, y0),
        # 建築東西兩端的端牆(全高,含走廊端;單元只帶西分戶牆,排尾在此補)。
        walls=[
            Wall((x0, y0), (x0, by1), EXT),
            Wall((bx1, y0), (bx1, by1), EXT),
        ],
        rooms=[Room("走廊", [(x0, y_corr0), (bx1, y_corr0), (bx1, y_top), (x0, y_top)],
                    kind="corridor", code="X00")],
        dim_chains=True,
        floor_label="3F",
        north_arrow=True,
        sheet=True,
        title_block=CompetitionTitleData(),
    )

    for i in range(n):
        ux = x0 + i * unit.width
        place_unit(spec, unit, origin=(ux, y_top))                    # 上排
        place_unit(spec, unit, origin=(ux, y0), mirror_y=True)        # 下排(對排)
    return spec


def main() -> None:
    from src.drafting.apartment_plan import draw_floor_plan
    from src.standards.loader import apply_standard, load_standard, new_document

    spec = demo_corridor_spec()
    std = load_standard()
    doc = new_document()
    layers = apply_standard(doc, std)
    draw_floor_plan(doc.modelspace(), spec, layers)
    out = _PROJECT_ROOT / "output" / "corridor_demo.dxf"
    out.parent.mkdir(exist_ok=True)
    doc.saveas(out)
    print(f"[OK] {out.name}: {len(spec.rooms)} 室 / {len(spec.walls)} 牆 / "
          f"{len(spec.doors)} 門 / {len(spec.windows)} 窗 / {len(spec.fixtures)} 件設備")


if __name__ == "__main__":
    main()


# =============================================================================
# PENDING(待確認假設彙整)
# =============================================================================
# 1. 鏡射的家具用「旋轉近似」:非左右對稱的圖塊(浴缸排水孔、衣櫃斜線)鏡射後
#    只是旋轉、不是真鏡射。要完全正確可改用 blockref xscale=-1,但 FixturePlacement
#    需加 mirror 欄位。待確認是否在意。
# 2. 分戶牆策略:單元只帶「西牆」,連排時右鄰的西牆補上共用牆、排尾由呼叫端補
#    端牆——若單元各自帶兩側牆,聯集也能合併(僅重複)。待確認習慣。
# 3. 示範的套房格局(4×6m:西南浴廁 1.8×2.0、東南玄關 2.2×1.1、北面起居含
#    開放式廚房)為自行設計;走廊寬 1800(法規雙邊排室走廊常需 ≥1.6m)。待確認。
# 4. 垂直動線(樓梯/電梯核)未放進走廊示範——實際建案在兩端/中段,C1 組合時加。
# 5. 上下排單元的對外側各帶一座工作陽台(UnitSpec.balconies,局部座標;
#    place_unit 平移/鏡射時翻 attach 面)。目前陽台只在窗外(放冷氣/曬衣),
#    未另開對外拉門進出——起居室須保留那扇對外窗(採光檢核),故不改門。
#    陽台尺寸 2.4×1.2m 為暫定,實際依戶型/法規陽台深度另調。待確認。
# =============================================================================
