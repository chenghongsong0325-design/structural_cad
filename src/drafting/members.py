"""結構構件產生器 —— 柱、梁(平面圖)。

設計目標:
  * 跟 gridlines.py 同樣的分層方式:資料模型(Column/Beam)與畫圖函式分開,
    方便單元測試,也方便之後配筋圖等模組重複使用同一份構件資料。
  * 柱:矩形斷面、軸對齊(不處理旋轉),中心點 + 寬 + 深。
  * 梁:用「起點/終點 + 寬」描述中心線與斷面寬度,畫成沿梁軸線方向的矩形外框——
    不像 hello_cad.py 舊版只處理水平梁,這裡用向量運算,水平/垂直/斜向梁都能畫。
    梁的「深」(depth,即梁高)只用在標籤文字(寬×深),平面圖本身看不到高度。
  * 呼叫端(例如 hello_cad.py)自行決定梁的起訖點——通常是柱面(不是柱心)之間的
    淨跨,這個裁切邏輯跟柱子尺寸有關,留在呼叫端,本模組不假設柱與梁的關係。

典型用法::

    from src.drafting.members import Column, Beam, draw_column, draw_beam, \\
        column_label_text, draw_column_label, beam_label_text, draw_beam_label

    col = Column(center=(0, 0), width=500, depth=500)
    draw_column(msp, col, layers["COLUMN"])
    draw_column_label(msp, col, column_label_text(col), layers["S-TEXTB"])

    beam = Beam(start=(250, 0), end=(5750, 0), width=240, depth=235)
    draw_beam(msp, beam, layers["S-RCBMB"])
    label = beam_label_text(beam, standard.beam_section_format)
    draw_beam_label(msp, beam, label, layers["S-TEXTB"])
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from ezdxf.enums import TextEntityAlignment

Point = tuple[float, float]


# ---------------------------------------------------------------------------
# 資料模型
# ---------------------------------------------------------------------------
@dataclass
class Column:
    """一根柱(平面圖矩形斷面,軸對齊)。"""

    center: Point
    width: float   # 沿 X 方向的斷面寬度
    depth: float   # 沿 Y 方向的斷面寬度(方柱時 width == depth)


@dataclass
class Beam:
    """一根梁,用中心線起訖點 + 斷面寬描述。"""

    start: Point
    end: Point
    width: float   # 梁寬(垂直於梁軸線方向的斷面寬度)
    depth: float   # 梁深/梁高,僅用於標籤文字(如"240×235"),不影響平面圖繪製

    @property
    def length(self) -> float:
        (x1, y1), (x2, y2) = self.start, self.end
        return math.hypot(x2 - x1, y2 - y1)

    @property
    def midpoint(self) -> Point:
        (x1, y1), (x2, y2) = self.start, self.end
        return ((x1 + x2) / 2, (y1 + y2) / 2)

    @property
    def unit_vector(self) -> Point:
        """沿梁軸線方向的單位向量。"""
        length = self.length
        if length == 0:
            raise ValueError("Beam 的起點與終點不能相同(長度為 0)")
        (x1, y1), (x2, y2) = self.start, self.end
        return ((x2 - x1) / length, (y2 - y1) / length)

    @property
    def normal_vector(self) -> Point:
        """垂直於梁軸線方向的單位向量(逆時針轉 90 度)。"""
        ux, uy = self.unit_vector
        return (-uy, ux)


# ---------------------------------------------------------------------------
# 畫圖:柱
# ---------------------------------------------------------------------------
def draw_column(msp, column: Column, layer: str) -> None:
    """畫一根柱:以 column.center 為中心的 width×depth 矩形(軸對齊,封閉多義線)。"""

    cx, cy = column.center
    hw, hd = column.width / 2, column.depth / 2
    points = [
        (cx - hw, cy - hd),
        (cx + hw, cy - hd),
        (cx + hw, cy + hd),
        (cx - hw, cy + hd),
    ]
    msp.add_lwpolyline(points, close=True, dxfattribs={"layer": layer})


def column_label_text(column: Column, fmt: str = "{width}×{depth}") -> str:
    """依斷面標示格式(如"{width}×{depth}"),產生柱的斷面標籤文字。

    沿用跟梁相同的「寬×深」慣例;呼叫端通常傳 standard.beam_section_format,
    讓柱、梁的斷面標示格式一致。
    """

    return fmt.format(width=column.width, depth=column.depth)


def draw_column_label(
    msp,
    column: Column,
    text: str,
    layer: str,
    text_height: float = 250,
    offset: Point = (0.0, 0.0),
) -> None:
    """把柱斷面標籤文字放在柱中心(可用 offset 微調位置),文字置中對齊。

    預設放在柱心正中央;若跟軸線/梁重疊不易閱讀,可用 offset 往旁邊挪。
    """

    cx, cy = column.center
    position = (cx + offset[0], cy + offset[1])
    msp.add_text(
        text,
        height=text_height,
        dxfattribs={"layer": layer, "style": "STRUCT"},
    ).set_placement(position, align=TextEntityAlignment.MIDDLE_CENTER)


# ---------------------------------------------------------------------------
# 畫圖:梁
# ---------------------------------------------------------------------------
def draw_beam(msp, beam: Beam, layer: str) -> None:
    """畫一根梁:沿中心線方向、寬度為 beam.width 的矩形外框(封閉多義線)。

    用向量運算算出梁軸線兩側各 width/2 的四個角點,因此水平、垂直、斜向梁
    都能正確畫出來,不像舊版 hello_cad.py 只處理水平梁。
    """

    (x1, y1), (x2, y2) = beam.start, beam.end
    nx, ny = beam.normal_vector
    half_w = beam.width / 2

    points = [
        (x1 + nx * half_w, y1 + ny * half_w),
        (x2 + nx * half_w, y2 + ny * half_w),
        (x2 - nx * half_w, y2 - ny * half_w),
        (x1 - nx * half_w, y1 - ny * half_w),
    ]
    msp.add_lwpolyline(points, close=True, dxfattribs={"layer": layer})


def beam_label_text(beam: Beam, fmt: str = "{width}×{depth}") -> str:
    """依標準檔的斷面標示格式(如"{width}×{depth}"),產生梁的斷面標籤文字。"""

    return fmt.format(width=beam.width, depth=beam.depth)


def draw_beam_label(
    msp,
    beam: Beam,
    text: str,
    layer: str,
    text_height: float = 250,
    margin: float = 200,
) -> None:
    """把梁斷面標籤文字放在梁中心線的法線方向外側(梁寬一半 + margin),文字置中對齊。

    文字本身不隨梁的角度旋轉(維持水平,方便閱讀),只有位置跟著梁的法線方向偏移。
    """

    nx, ny = beam.normal_vector
    mx, my = beam.midpoint
    offset = beam.width / 2 + margin
    position = (mx + nx * offset, my + ny * offset)

    msp.add_text(
        text,
        height=text_height,
        dxfattribs={"layer": layer, "style": "STRUCT"},
    ).set_placement(position, align=TextEntityAlignment.BOTTOM_CENTER)
