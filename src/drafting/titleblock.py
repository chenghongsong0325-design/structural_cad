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


# =============================================================================
# 競賽格式:A3 圖紙外框 + 競賽標題欄
# =============================================================================
# 圖紙外框:A3 橫式,比例 1:100 → 模型空間尺寸(mm)。
# (A3 = 420×297mm 紙張;1:100 出圖時,紙上 1mm = 模型 100mm。)
# ⚠️ 待確認:實際圖紙大小與比例、外框內縮距離,依競賽規定調整。
A3_WIDTH = 42000      # 420mm × 100
A3_HEIGHT = 29700     # 297mm × 100
SHEET_MARGIN = 1000   # 圖紙邊到繪圖框的內縮(紙上 10mm)。待確認


def draw_sheet_border(
    msp,
    layer: str,
    *,
    origin: Point = (0.0, 0.0),
    width: float = A3_WIDTH,
    height: float = A3_HEIGHT,
    margin: float = SHEET_MARGIN,
) -> tuple[list[Point], list[Point]]:
    """畫圖紙外框:A3 外緣 + 內縮 margin 的繪圖框(兩個矩形)。

    origin 是圖紙左下角的世界座標。回傳 (外框角點, 內框角點),方便呼叫端
    依內框擺放圖面與標題欄。
    """
    ox, oy = origin
    outer = [(ox, oy), (ox + width, oy), (ox + width, oy + height), (ox, oy + height)]
    inner = [
        (ox + margin, oy + margin),
        (ox + width - margin, oy + margin),
        (ox + width - margin, oy + height - margin),
        (ox + margin, oy + height - margin),
    ]
    msp.add_lwpolyline(outer, close=True, dxfattribs={"layer": layer})
    msp.add_lwpolyline(inner, close=True, dxfattribs={"layer": layer})
    return outer, inner


COMP_BLOCK_NAME = "TITLEBLOCK_COMP"

# 競賽標題欄的資料欄位(屬性標籤 tag, 顯示標題 label)。放進 4 列 × 2 欄位組。
_COMP_FIELDS: list[tuple[str, str]] = [
    ("DWG_NAME", "圖名"),
    ("SCALE", "比例"),
    ("EXAM_TIME", "檢定時間"),
    ("EXAM_DATE", "核定日期"),
    ("APPROVAL", "核定單位"),
    ("EXAM_NO", "檢定編號"),
    ("QUESTION_NO", "試題編號"),
    ("EXAMINEE", "應檢人簽名"),
]


@dataclass
class CompetitionTitleData:
    """競賽標題欄要填入的欄位值(全部有預設,可只填要的)。"""

    drawing_name: str = ""       # 圖名
    scale: str = "1:100"         # 比例
    exam_time: str = ""          # 檢定時間(如「繪圖 2 小時 30 分」)
    exam_date: str = ""          # 核定日期
    approval_unit: str = ""      # 核定單位
    exam_number: str = ""        # 檢定編號
    question_number: str = ""    # 試題編號
    examinee: str = ""           # 應檢人簽名
    # 頂端類別橫幅(固定考項名稱;不同考項可改)。
    category: str = "建築製圖應用－電繪項丙級技術士技能檢定"

    def as_attrib_values(self) -> dict[str, str]:
        return {
            "DWG_NAME": self.drawing_name,
            "SCALE": self.scale,
            "EXAM_TIME": self.exam_time,
            "EXAM_DATE": self.exam_date,
            "APPROVAL": self.approval_unit,
            "EXAM_NO": self.exam_number,
            "QUESTION_NO": self.question_number,
            "EXAMINEE": self.examinee,
            "CATEGORY": self.category,
        }


def create_competition_title_block(
    doc,
    layers: dict[str, str],
    *,
    name: str = COMP_BLOCK_NAME,
    label_width: float = 2400,
    value_width: float = 6600,
    row_height: float = 800,
    text_height: float = 250,
    style: str = "STRUCT",
):
    """建立(或取得)競賽標題欄圖塊。已存在直接回傳。

    版面(以 (0,0) 為左下角):頂端一列全寬「類別橫幅」,其下 4 列 × 2 欄位組,
    每個欄位 = 標題格(label_width)+ 可填值格(value_width,ATTDEF)。
        列(由上而下):圖名|比例 / 檢定時間|核定日期 / 核定單位|檢定編號 /
                      試題編號|應檢人簽名
    """
    if name in doc.blocks:
        return doc.blocks.get(name)

    line_layer = layers["OTHER"]
    text_layer = layers["TEXT"]

    field_col_w = label_width + value_width          # 一個欄位組的寬
    total_w = field_col_w * 2                        # 兩個欄位組
    n_rows = 4                                        # 資料列數
    data_h = row_height * n_rows
    total_h = data_h + row_height                     # + 頂端類別橫幅

    blk = doc.blocks.new(name=name)

    # 外框。
    blk.add_lwpolyline(
        [(0, 0), (total_w, 0), (total_w, total_h), (0, total_h)],
        close=True, dxfattribs={"layer": line_layer},
    )
    # 資料區橫線(y = row_height..data_h)。
    for r in range(1, n_rows):
        blk.add_line((0, r * row_height), (total_w, r * row_height), dxfattribs={"layer": line_layer})
    # 類別橫幅下緣(資料區頂線)。
    blk.add_line((0, data_h), (total_w, data_h), dxfattribs={"layer": line_layer})
    # 資料區直線:兩個欄位組之間,以及各欄位組內 label|value 分界。
    for x in (label_width, field_col_w, field_col_w + label_width):
        blk.add_line((x, 0), (x, data_h), dxfattribs={"layer": line_layer})

    # 類別橫幅(置中,ATTDEF CATEGORY)。
    cat = blk.add_attdef(tag="CATEGORY", height=text_height,
                         dxfattribs={"layer": text_layer, "style": style})
    cat.set_placement((total_w / 2, data_h + row_height / 2), align=TextEntityAlignment.MIDDLE_CENTER)

    # 8 個欄位:i//2 = 由上往下第幾列,i%2 = 左/右欄位組。
    for i, (tag, label) in enumerate(_COMP_FIELDS):
        col = i % 2
        row_from_top = i // 2
        row_from_bottom = (n_rows - 1) - row_from_top
        x0 = col * field_col_w
        mid_y = row_from_bottom * row_height + row_height / 2

        blk.add_text(label, height=text_height, dxfattribs={"layer": text_layer, "style": style}) \
           .set_placement((x0 + 150, mid_y), align=TextEntityAlignment.MIDDLE_LEFT)
        att = blk.add_attdef(tag=tag, height=text_height, dxfattribs={"layer": text_layer, "style": style})
        att.set_placement((x0 + label_width + 150, mid_y), align=TextEntityAlignment.MIDDLE_LEFT)

    return blk


def competition_title_size(
    *, label_width: float = 2400, value_width: float = 6600, row_height: float = 800,
) -> tuple[float, float]:
    """回傳競賽標題欄的 (寬, 高),方便呼叫端把它對齊到圖框右下角。"""
    return ((label_width + value_width) * 2, row_height * 5)


def insert_competition_title_block(
    msp,
    data: CompetitionTitleData,
    layers: dict[str, str],
    insert: Point = (0.0, 0.0),
    *,
    name: str = COMP_BLOCK_NAME,
):
    """插入競賽標題欄圖塊並填值。定義不存在會自動建立。回傳 blockref。"""
    doc = msp.doc
    if name not in doc.blocks:
        create_competition_title_block(doc, layers, name=name)
    blockref = msp.add_blockref(name, insert, dxfattribs={"layer": layers["OTHER"]})
    blockref.add_auto_attribs(data.as_attrib_values())
    return blockref
