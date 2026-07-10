"""陽台(Balcony)與電梯(Elevator)—— ROADMAP 階段 B3。

設計(沿用專案模式:資料模型與畫圖分離、圖層走 layers 對照表):

  * 兩者的「牆」都以 balcony_walls()/elevator_walls() 回傳 Wall 清單,
    由生產線(draw_floor_plan)併入整張圖的牆聯集(draw_walls_joined)——
    這樣陽台矮牆貼上建築外牆、電梯井貼上隔間牆時,交角自動接乾淨,
    不會像「浮貼上去」的兩張皮。
  * Balcony:突出建築外的矩形,貼建築那一邊(attach)不畫牆,其餘三邊畫
    陽台矮牆(厚 BALCONY_WALL_THICKNESS=100);欄杆線沿三邊牆的中心線畫
    一條折線(HANDRAIL 層);「陽台」文字標籤(A-TEXT)。
  * Elevator:井道四面 RC 牆(厚 ELEVATOR_WALL_THICKNESS=200),開門面
    (door_side)留門洞(電梯門是橫拉門,平面只畫洞口、不畫開門弧);
    井道內畫轎廂符號 = 內縮矩形 + 兩條對角線(OTHER 層,仿真實圖的
    「矩形打叉」畫法)。

典型用法(生產線內部;單獨用見 tests)::

    balcony = Balcony(origin=(8300, 800), width=2400, depth=1200, attach="north")
    elevator = Elevator(origin=(6600, 4800), width=1400, depth=2200, door_side="east")
    spec.balconies.append(balcony); spec.elevators.append(elevator)
    # draw_floor_plan 會自動:牆併聯集 + draw_balcony_railing + draw_elevator_symbol

⚠️ 待確認假設見模組結尾 PENDING 區塊。
"""
from __future__ import annotations

from dataclasses import dataclass

from ezdxf.enums import TextEntityAlignment

from src.drafting.wall import (
    BALCONY_WALL_THICKNESS,
    ELEVATOR_WALL_THICKNESS,
    Opening,
    Wall,
)

Point = tuple[float, float]

_SIDES = ("north", "south", "east", "west")

# 電梯轎廂符號:內縮 = 半個井壁厚 + 淨距(mm)。待確認。
CAR_CLEARANCE = 100


# ---------------------------------------------------------------------------
# 陽台
# ---------------------------------------------------------------------------
@dataclass
class Balcony:
    """一座矩形陽台。

    origin: 矩形最小角(世界座標);矩形以「牆中心線」定義。
    width/depth: 沿 X / Y 的尺寸(mm)。
    attach: 貼建築的那一邊("north"=矩形北邊貼建築…),該邊不畫牆;
            建議把該邊放在建築外牆「中心線」上,聯集時自然接合。
    """

    origin: Point
    width: float
    depth: float
    attach: str = "north"
    wall_thickness: float = BALCONY_WALL_THICKNESS

    def __post_init__(self) -> None:
        if self.attach not in _SIDES:
            raise ValueError(f"attach 只能是 {_SIDES},收到 {self.attach!r}")

    def _corners(self) -> dict[str, tuple[Point, Point]]:
        """四邊(中心線)的起訖點。"""
        x0, y0 = self.origin
        x1, y1 = x0 + self.width, y0 + self.depth
        return {
            "south": ((x0, y0), (x1, y0)),
            "east": ((x1, y0), (x1, y1)),
            "north": ((x1, y1), (x0, y1)),
            "west": ((x0, y1), (x0, y0)),
        }

    def free_edges(self) -> list[tuple[Point, Point]]:
        """未貼建築的三邊(欄杆/矮牆所在),依序相連。"""
        order = {"north": ["west", "south", "east"],   # 貼北 → 西/南/東三邊
                 "south": ["west", "north", "east"],
                 "east": ["north", "west", "south"],
                 "west": ["south", "east", "north"]}
        edges = self._corners()
        return [edges[s] for s in order[self.attach]]


def balcony_walls(balcony: Balcony) -> list[Wall]:
    """陽台三邊矮牆(貼建築那邊不畫),供併入整張圖的牆聯集。"""
    return [
        Wall(start=a, end=b, thickness=balcony.wall_thickness)
        for a, b in balcony.free_edges()
    ]


def draw_balcony_railing(msp, balcony: Balcony, layers: dict[str, str],
                         text_height: float = 250) -> None:
    """欄杆線(三邊牆中心線的連續折線,HANDRAIL)+「陽台」文字(A-TEXT)。"""
    edges = balcony.free_edges()
    pts = [edges[0][0]] + [b for _, b in edges]      # 三邊首尾相連的折線
    msp.add_lwpolyline(pts, dxfattribs={"layer": layers["HANDRAIL"]})

    x0, y0 = balcony.origin
    cx, cy = x0 + balcony.width / 2, y0 + balcony.depth / 2
    msp.add_text(
        "陽台", height=text_height,
        dxfattribs={"layer": layers["A-TEXT"], "style": "STRUCT"},
    ).set_placement((cx, cy), align=TextEntityAlignment.MIDDLE_CENTER)


# ---------------------------------------------------------------------------
# 電梯
# ---------------------------------------------------------------------------
@dataclass
class Elevator:
    """一座電梯井(平面)。

    origin: 井道「牆中心線」矩形的最小角(世界座標)。
    width/depth: 沿 X / Y 的中心線尺寸(mm)。
    door_side: 開門面("east" = 東面牆留門洞…);電梯門為橫拉門,只留洞口。
    door_width: 門洞寬(mm)。
    """

    origin: Point
    width: float
    depth: float
    door_side: str = "south"
    door_width: float = 900
    wall_thickness: float = ELEVATOR_WALL_THICKNESS

    def __post_init__(self) -> None:
        if self.door_side not in _SIDES:
            raise ValueError(f"door_side 只能是 {_SIDES},收到 {self.door_side!r}")


def elevator_walls(elev: Elevator) -> list[Wall]:
    """井道四面 RC 牆(開門面留門洞,置中),供併入整張圖的牆聯集。"""
    x0, y0 = elev.origin
    x1, y1 = x0 + elev.width, y0 + elev.depth
    sides = {
        "south": ((x0, y0), (x1, y0), elev.width),
        "east": ((x1, y0), (x1, y1), elev.depth),
        "north": ((x1, y1), (x0, y1), elev.width),
        "west": ((x0, y1), (x0, y0), elev.depth),
    }
    walls = []
    for name, (a, b, length) in sides.items():
        openings = []
        if name == elev.door_side:
            openings = [Opening(length / 2, elev.door_width, "door")]
        walls.append(Wall(start=a, end=b, thickness=elev.wall_thickness,
                          openings=openings))
    return walls


def draw_elevator_symbol(msp, elev: Elevator, layers: dict[str, str]) -> None:
    """井道內的轎廂符號:內縮矩形 + 兩條對角線(仿真實圖「矩形打叉」)。"""
    inset = elev.wall_thickness / 2 + CAR_CLEARANCE
    x0, y0 = elev.origin
    x1, y1 = x0 + elev.width, y0 + elev.depth
    a, b = (x0 + inset, y0 + inset), (x1 - inset, y1 - inset)
    if b[0] <= a[0] or b[1] <= a[1]:
        raise ValueError("電梯井太小,內縮後轎廂符號畫不下")

    layer = layers["OTHER"]
    msp.add_lwpolyline(
        [a, (b[0], a[1]), b, (a[0], b[1])], close=True, dxfattribs={"layer": layer}
    )
    msp.add_line(a, b, dxfattribs={"layer": layer})
    msp.add_line((a[0], b[1]), (b[0], a[1]), dxfattribs={"layer": layer})


# =============================================================================
# PENDING(待確認假設彙整)
# =============================================================================
# 1. 陽台:矮牆厚 100(競賽構造尺寸「陽台牆厚10」);欄杆線畫在矮牆中心線
#    (單線)。有的圖畫雙線欄杆或在外緣;「+20 陽台」的標高標註之後在
#    文字/標高模組補。attach 邊建議放在建築外牆中心線上以便聯集接合。待確認。
# 2. 電梯:井壁 RC 20cm(競賽構造尺寸「電梯間 RC 牆厚20」);門洞置中、寬 900,
#    橫拉門不畫開門弧;轎廂符號=內縮(半壁厚+100)矩形+對角線打叉,掛 OTHER 層
#    (規範無電梯專層)。實務轎廂符號另有畫導軌/門檻線者。待確認。
# 3. 兩者的牆由生產線併入牆聯集:與建築牆自然接角,但也代表它們不進
#    dim_chains 的外圍細部尺寸(dim_chains 只讀 spec.walls)。陽台/電梯的
#    細部尺寸之後視需要另標。待確認。
# =============================================================================
