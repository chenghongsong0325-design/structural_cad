"""標題欄 / 圖框產生器 —— 做成可重用的 DXF 圖塊(BLOCK)。

設計目標:
  * 用「圖塊 + 屬性(ATTRIB)」實作,而不是每次都把線和字重畫一遍——這是 AutoCAD
    製作標題欄的標準做法:圖塊定義一次,之後每張圖插入(INSERT)一次、填不同欄位值。
  * 欄位(圖名/圖號/比例/日期/繪製/審核)用「屬性定義(ATTDEF)」當可填空格,
    插入時用 add_auto_attribs 帶入實際文字,日後也能在 AutoCAD 裡直接雙擊修改。
  * 框線掛 S-THIN(結構細線)、文字掛 S-TEXTB;實際圖層由呼叫端的 layers 對照表決定,
    因此天然支援樓層前綴。

    ⚠️ 待確認:標題欄的實際尺寸、欄位配置、放在圖紙空間(paper space)還是模型空間,
       公司多半有固定的標準圖框。這裡的尺寸(模型單位 mm)與 2×3 版面是暫定值,
       方便示範;取得公司標準圖框後再改。

典型用法::

    from src.drafting.titleblock import TitleBlockData, insert_title_block

    data = TitleBlockData(
        drawing_name="二層結構平面圖", drawing_number="S-02", scale="1:100",
        date="2026-07-09", drawn_by="成弘", checked_by="—",
    )
    insert_title_block(msp, data, layers, insert=(x, y))
"""
from __future__ import annotations

from dataclasses import dataclass

from ezdxf.enums import TextEntityAlignment

Point = tuple[float, float]

BLOCK_NAME = "TITLEBLOCK"

# 欄位版面:(屬性標籤 tag, 顯示標題 label, 欄 col 0..1, 列 row 0..2;列 0 在最下)
_FIELDS: list[tuple[str, str, int, int]] = [
    ("DWG_NAME", "圖名", 0, 2),
    ("DWG_NO", "圖號", 1, 2),
    ("SCALE", "比例", 0, 1),
    ("DATE", "日期", 1, 1),
    ("DRAWN", "繪製", 0, 0),
    ("CHECKED", "審核", 1, 0),
]
_NCOLS = 2
_NROWS = 3


@dataclass
class TitleBlockData:
    """標題欄要填入的六個欄位值。"""

    drawing_name: str    # 圖名
    drawing_number: str  # 圖號
    scale: str           # 比例(如 "1:100")
    date: str            # 日期
    drawn_by: str        # 繪製
    checked_by: str      # 審核

    def as_attrib_values(self) -> dict[str, str]:
        """轉成 {屬性標籤 -> 值} 的對照表,供 add_auto_attribs 填入。"""
        return {
            "DWG_NAME": self.drawing_name,
            "DWG_NO": self.drawing_number,
            "SCALE": self.scale,
            "DATE": self.date,
            "DRAWN": self.drawn_by,
            "CHECKED": self.checked_by,
        }


def create_title_block_definition(
    doc,
    layers: dict[str, str],
    *,
    name: str = BLOCK_NAME,
    cell_width: float = 3000,
    cell_height: float = 800,
    text_height: float = 250,
    style: str = "STRUCT",
):
    """建立(或取得)標題欄圖塊定義。已存在就直接回傳,不重複建立。

    圖塊以 (0,0) 為左下角,往右上延伸;整體寬 = cell_width×2、高 = cell_height×3。
    每一格畫出「標題文字」+「可填屬性(ATTDEF)」。
    """

    if name in doc.blocks:
        return doc.blocks.get(name)

    line_layer = layers["S-THIN"]
    text_layer = layers["S-TEXTB"]

    blk = doc.blocks.new(name=name)
    total_w = cell_width * _NCOLS
    total_h = cell_height * _NROWS

    # 外框 + 內部格線。
    blk.add_lwpolyline(
        [(0, 0), (total_w, 0), (total_w, total_h), (0, total_h)],
        close=True,
        dxfattribs={"layer": line_layer},
    )
    for r in range(1, _NROWS):
        blk.add_line((0, r * cell_height), (total_w, r * cell_height), dxfattribs={"layer": line_layer})
    for c in range(1, _NCOLS):
        blk.add_line((c * cell_width, 0), (c * cell_width, total_h), dxfattribs={"layer": line_layer})

    # 每一格:左邊放標題文字,右邊放可填屬性。
    label_x_pad = 150
    value_x_pad = 1100
    for tag, label, col, row in _FIELDS:
        x0 = col * cell_width
        y0 = row * cell_height
        mid_y = y0 + cell_height / 2

        blk.add_text(
            label,
            height=text_height,
            dxfattribs={"layer": text_layer, "style": style},
        ).set_placement((x0 + label_x_pad, mid_y), align=TextEntityAlignment.MIDDLE_LEFT)

        attdef = blk.add_attdef(
            tag=tag,
            height=text_height,
            dxfattribs={"layer": text_layer, "style": style},
        )
        attdef.set_placement((x0 + value_x_pad, mid_y), align=TextEntityAlignment.MIDDLE_LEFT)

    return blk


def insert_title_block(
    msp,
    data: TitleBlockData,
    layers: dict[str, str],
    insert: Point = (0.0, 0.0),
    *,
    name: str = BLOCK_NAME,
):
    """把標題欄圖塊插入 modelspace 的 insert 位置,並填入 data 的欄位值。

    圖塊定義若尚未建立,會自動先建立(用同一份 layers 對照表)。
    回傳插入後的 blockref(Insert 實體)。
    """

    doc = msp.doc
    if name not in doc.blocks:
        create_title_block_definition(doc, layers, name=name)

    blockref = msp.add_blockref(name, insert, dxfattribs={"layer": layers["S-THIN"]})
    blockref.add_auto_attribs(data.as_attrib_values())
    return blockref
