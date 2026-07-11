"""圖面配件 —— 樓層標示(大字)與北向箭頭(ROADMAP B5 的第 3 項)。

  * 樓層標示:一個大字(如「3F」),掛 TEXT 層——真實建案圖在圖面角落
    放大大的樓層代號。
  * 北向箭頭:做成可重用圖塊(圓 + 指北針三角 + N 字),插入時可旋轉
    (圖面的北不朝上時轉 rotation),掛 OTHER 層。

⚠️ 待確認:北向箭頭樣式(圓+三角+N 為常見畫法之一,各事務所不同);
   樓層標示字高預設 1500(1:100 出圖紙上 15mm)。
"""
from __future__ import annotations

from ezdxf.enums import TextEntityAlignment

Point = tuple[float, float]

NORTH_ARROW_BLOCK = "NORTH_ARROW"
NORTH_ARROW_RADIUS = 600      # 圖塊定義的半徑(mm)。待確認


def draw_floor_label(msp, text: str, insert: Point, layers: dict[str, str],
                     text_height: float = 1500) -> None:
    """樓層標示大字(如「3F」),掛 TEXT 層,置中於 insert。"""
    msp.add_text(
        text, height=text_height,
        dxfattribs={"layer": layers["TEXT"], "style": "STRUCT"},
    ).set_placement(insert, align=TextEntityAlignment.MIDDLE_CENTER)


def create_north_arrow_block(doc, *, name: str = NORTH_ARROW_BLOCK,
                             radius: float = NORTH_ARROW_RADIUS):
    """建立(或取得)北向箭頭圖塊:圓 + 指北三角 + N 字(內部實體掛 "0")。"""
    if name in doc.blocks:
        return doc.blocks.get(name)
    blk = doc.blocks.new(name)
    blk.add_circle((0, 0), radius=radius, dxfattribs={"layer": "0"})
    # 指北三角(尖端朝 +Y)。
    blk.add_lwpolyline(
        [(0, radius * 0.85), (-radius * 0.32, -radius * 0.45),
         (0, -radius * 0.1), (radius * 0.32, -radius * 0.45)],
        close=True, dxfattribs={"layer": "0"},
    )
    blk.add_text(
        "N", height=radius * 0.45, dxfattribs={"layer": "0", "style": "STRUCT"},
    ).set_placement((0, radius * 1.35), align=TextEntityAlignment.MIDDLE_CENTER)
    return blk


def place_north_arrow(msp, insert: Point, layers: dict[str, str],
                      rotation: float = 0.0, scale: float = 1.0):
    """插入北向箭頭(OTHER 層);rotation = 圖面北方偏離正上方的角度(度)。"""
    create_north_arrow_block(msp.doc)
    return msp.add_blockref(
        NORTH_ARROW_BLOCK, insert,
        dxfattribs={"layer": layers["OTHER"], "rotation": rotation,
                    "xscale": scale, "yscale": scale},
    )
