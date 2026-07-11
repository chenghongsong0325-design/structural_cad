"""衛浴廚具設備 + 家具圖塊 —— ROADMAP 階段 B4。

做法比照 door_window:每種設備定義一次 DXF 圖塊(實際尺寸,mm),之後每個
都是一次插入(位置 + 旋轉);圖塊內部實體掛圖層 "0",插入時繼承 blockref
的圖層(OTHER,細線 0.15mm——競賽規範無家具專層,見 PENDING)。

圖塊座標約定(方便「靠牆擺放」):
  * 原點 = 「貼牆邊」的中點,設備朝局部 +Y 伸出(床頭/沙發背/馬桶水箱
    都在原點那一側)。靠牆時 rotation:
        南牆(朝北伸) 0° / 北牆 180° / 東牆 90° / 西牆 270°
  * 桌椅組(table4)例外:原點 = 桌子中心(不靠牆的家具)。

支援的圖塊(FIXTURE_BUILDERS 的 key):
    toilet(馬桶) basin(洗手台) bathtub(浴缸)
    bed_single(單人床) bed_double(雙人床)
    table4(方桌+四椅) sofa3(三人沙發) wardrobe(衣櫃)

流理台(Counter):長度隨廚房而變,不做成固定圖塊,改用參數式繪製——
start→end 為靠牆邊,檯面往行進方向「左手側」伸出 depth;L 型 = 兩段
Counter 相接。可選 sink=True 在中點畫水槽圓。

典型用法::

    from src.drafting.fixtures import FixturePlacement, Counter, place_fixture, draw_counter

    place_fixture(msp, FixturePlacement("toilet", (13925, 3600), rotation=90), layers)
    draw_counter(msp, Counter(start=(13925, 4560), end=(13925, 6940), sink=False), layers)

⚠️ 待確認假設見模組結尾 PENDING 區塊(各設備尺寸、圖層歸屬、符號畫法)。
"""
from __future__ import annotations

import math
from dataclasses import dataclass

Point = tuple[float, float]


# ---------------------------------------------------------------------------
# 圖塊定義(單位 mm,實際尺寸;原點=貼牆邊中點,朝 +Y 伸出)
# ---------------------------------------------------------------------------
def _build_toilet(blk) -> None:
    """馬桶 380×700:水箱矩形 + 橢圓馬桶座。"""
    blk.add_lwpolyline([(-190, 0), (190, 0), (190, 170), (-190, 170)],
                       close=True, dxfattribs={"layer": "0"})
    blk.add_ellipse(center=(0, 430), major_axis=(0, 260), ratio=170 / 260,
                    dxfattribs={"layer": "0"})


def _build_basin(blk) -> None:
    """洗手台 500×450:檯面矩形 + 內橢圓面盆。"""
    blk.add_lwpolyline([(-250, 0), (250, 0), (250, 450), (-250, 450)],
                       close=True, dxfattribs={"layer": "0"})
    blk.add_ellipse(center=(0, 225), major_axis=(180, 0), ratio=140 / 180,
                    dxfattribs={"layer": "0"})


def _build_bathtub(blk) -> None:
    """浴缸 1600×750:外框 + 內框(壁厚 75)+ 排水圓。"""
    blk.add_lwpolyline([(-800, 0), (800, 0), (800, 750), (-800, 750)],
                       close=True, dxfattribs={"layer": "0"})
    blk.add_lwpolyline([(-725, 75), (725, 75), (725, 675), (-725, 675)],
                       close=True, dxfattribs={"layer": "0"})
    blk.add_circle((-600, 375), radius=50, dxfattribs={"layer": "0"})


def _build_bed(blk, width: float, pillows: int) -> None:
    """床(床頭貼牆):外框 + 枕頭 + 被摺線。"""
    hw = width / 2
    blk.add_lwpolyline([(-hw, 0), (hw, 0), (hw, 2000), (-hw, 2000)],
                       close=True, dxfattribs={"layer": "0"})
    if pillows == 1:
        blk.add_lwpolyline([(-hw + 100, 80), (hw - 100, 80),
                            (hw - 100, 480), (-hw + 100, 480)],
                           close=True, dxfattribs={"layer": "0"})
    else:
        for cx in (-hw / 2, hw / 2):
            blk.add_lwpolyline([(cx - 300, 80), (cx + 300, 80),
                                (cx + 300, 480), (cx - 300, 480)],
                               close=True, dxfattribs={"layer": "0"})
    blk.add_line((-hw, 560), (hw, 560), dxfattribs={"layer": "0"})   # 被摺線


def _build_table4(blk) -> None:
    """方桌 800×800 + 四張椅子 380×380(原點=桌心)。"""
    blk.add_lwpolyline([(-400, -400), (400, -400), (400, 400), (-400, 400)],
                       close=True, dxfattribs={"layer": "0"})
    for cx, cy in ((0, 590), (0, -590), (590, 0), (-590, 0)):
        blk.add_lwpolyline(
            [(cx - 190, cy - 190), (cx + 190, cy - 190),
             (cx + 190, cy + 190), (cx - 190, cy + 190)],
            close=True, dxfattribs={"layer": "0"})


def _build_sofa3(blk) -> None:
    """三人沙發 2000×850(背貼牆):背 + 坐墊 + 兩扶手。"""
    blk.add_lwpolyline([(-1000, 0), (1000, 0), (1000, 200), (-1000, 200)],
                       close=True, dxfattribs={"layer": "0"})          # 背
    blk.add_lwpolyline([(-850, 200), (850, 200), (850, 750), (-850, 750)],
                       close=True, dxfattribs={"layer": "0"})          # 坐墊
    for sx in (-1, 1):
        blk.add_lwpolyline(
            [(sx * 850, 0), (sx * 1000, 0), (sx * 1000, 850), (sx * 850, 850)],
            close=True, dxfattribs={"layer": "0"})                     # 扶手


def _build_wardrobe(blk) -> None:
    """衣櫃 1500×600(背貼牆):外框 + 吊桿線 + 斜線記號。"""
    blk.add_lwpolyline([(-750, 0), (750, 0), (750, 600), (-750, 600)],
                       close=True, dxfattribs={"layer": "0"})
    blk.add_line((-750, 300), (750, 300), dxfattribs={"layer": "0"})   # 吊桿
    blk.add_line((-750, 0), (750, 600), dxfattribs={"layer": "0"})     # 斜線記號


FIXTURE_BUILDERS = {
    "toilet": _build_toilet,
    "basin": _build_basin,
    "bathtub": _build_bathtub,
    "bed_single": lambda blk: _build_bed(blk, 1000, pillows=1),
    "bed_double": lambda blk: _build_bed(blk, 1600, pillows=2),
    "table4": _build_table4,
    "sofa3": _build_sofa3,
    "wardrobe": _build_wardrobe,
}


def _block_name(name: str) -> str:
    return f"FX_{name.upper()}"


def create_fixture_block(doc, name: str) -> str:
    """建立(或取得)一種設備圖塊,回傳圖塊名。未知種類報錯。"""
    if name not in FIXTURE_BUILDERS:
        raise ValueError(
            f"未知設備 {name!r},支援:{sorted(FIXTURE_BUILDERS)}"
        )
    block_name = _block_name(name)
    if block_name not in doc.blocks:
        FIXTURE_BUILDERS[name](doc.blocks.new(block_name))
    return block_name


# ---------------------------------------------------------------------------
# 放置
# ---------------------------------------------------------------------------
@dataclass
class FixturePlacement:
    """一件設備/家具的放置:種類 + 插入點(貼牆邊中點)+ 旋轉(度,逆時針)。"""

    name: str
    insert: Point
    rotation: float = 0.0


def place_fixture(msp, placement: FixturePlacement, layers: dict[str, str]):
    """插入一件設備圖塊(掛 OTHER 層),回傳 blockref。"""
    block_name = create_fixture_block(msp.doc, placement.name)
    return msp.add_blockref(
        block_name, placement.insert,
        dxfattribs={"layer": layers["OTHER"], "rotation": placement.rotation},
    )


# ---------------------------------------------------------------------------
# 流理台(參數式:長度隨廚房而變)
# ---------------------------------------------------------------------------
@dataclass
class Counter:
    """一段流理台:start→end 為靠牆邊,檯面往行進方向「左手側」伸出 depth。

    L 型 = 兩段 Counter 相接(共用轉角點)。sink=True 在段中點畫水槽圓。
    """

    start: Point
    end: Point
    depth: float = 600
    sink: bool = False

    @property
    def length(self) -> float:
        return math.hypot(self.end[0] - self.start[0], self.end[1] - self.start[1])

    def __post_init__(self) -> None:
        if self.length <= 0:
            raise ValueError("Counter 的起訖點不能相同")


def draw_counter(msp, counter: Counter, layers: dict[str, str]) -> None:
    """畫一段流理台:檯面矩形(+ 可選水槽圓),掛 OTHER 層。"""
    (x1, y1), (x2, y2) = counter.start, counter.end
    length = counter.length
    ux, uy = (x2 - x1) / length, (y2 - y1) / length
    nx, ny = -uy, ux                                   # 左手側
    d = counter.depth
    msp.add_lwpolyline(
        [(x1, y1), (x2, y2), (x2 + nx * d, y2 + ny * d), (x1 + nx * d, y1 + ny * d)],
        close=True, dxfattribs={"layer": layers["OTHER"]},
    )
    if counter.sink:
        mx, my = (x1 + x2) / 2 + nx * d / 2, (y1 + y2) / 2 + ny * d / 2
        msp.add_circle((mx, my), radius=180, dxfattribs={"layer": layers["OTHER"]})


# =============================================================================
# PENDING(待確認假設彙整)
# =============================================================================
# 1. 設備尺寸(mm)皆為市面常見值,非任何規範:馬桶 380×700、洗手台 500×450、
#    浴缸 1600×750、單人床 1000×2000、雙人床 1600×2000、方桌 800×800+椅 380、
#    三人沙發 2000×850、衣櫃 1500×600、流理台深 600、水槽圓 r180。待確認。
# 2. 圖層:全掛 OTHER(其他,0.15mm 細線)——競賽圖層規定沒有家具/設備專層。
#    真實事務所常設 FURN/P-FIXT 等專層,要拆layer時在 default.yaml 加層+改此處。
# 3. 符號畫法(枕頭/被摺線/衣櫃斜線/沙發三件式)為常見簡化畫法,各事務所
#    圖例略異。馬桶/面盆用橢圓,床/沙發用直角矩形(未做圓角)。待確認。
# 4. 靠牆擺放由呼叫端負責算插入點(牆內面座標)與旋轉;本模組不自動吸附牆面。
#    之後 C1 自動設計時可加「貼某道牆的中點/偏移」的輔助函式。
# =============================================================================
