"""完整結構平面圖示範 —— 把「軸網 + 多柱 + 多梁 + 圖框」組成一張圖。

這是把前面幾個模組(standards / gridlines / members / titleblock)組裝起來的示範:
  1. 套繪圖標準(圖層/線型/文字型/標註型)
  2. 產生軸網(多條軸線 + 自動編號 + 軸間尺寸標註)
  3. 在每個軸線交點放一根柱
  4. 在相鄰柱之間、沿軸線放梁(裁到柱面之間的淨跨),並標註斷面
  5. 插入標題欄圖塊,填入圖名/圖號/比例/日期/繪製/審核

相較於 hello_cad.py(只有兩柱單跨的最小示範),這支示範「一整層樓」的結構平面圖,
展示各模組如何協同產出一張可交付的圖。
"""
from __future__ import annotations

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from src.drafting.gridlines import GridSystem, build_grid_system, draw_grid, draw_grid_dimensions
from src.drafting.members import (
    Beam,
    Column,
    beam_label_text,
    draw_beam,
    draw_beam_label,
    draw_column,
)
from src.drafting.titleblock import TitleBlockData, insert_title_block
from src.standards.loader import apply_standard, load_standard, new_document

# ── 示範參數(全部 mm)───────────────────────────────────────────────────
FLOOR_PREFIX = "2F建築底圖"
X_SPACINGS = [6000, 6000]   # X 向兩跨 → 3 條軸線(1,2,3)
Y_SPACINGS = [5000, 5000]   # Y 向兩跨 → 3 條軸線(A,B,C)
COLUMN_SIZE = 500           # 方柱 500×500
BEAM_WIDTH = 240            # 梁寬
BEAM_DEPTH = 235            # 梁深/梁高(標籤用)
TEXT_HEIGHT = 250


def build_columns(grid: GridSystem, size: float) -> list[Column]:
    """在每個軸線交點放一根方柱。"""
    columns = []
    for xa in grid.x_axes:
        for ya in grid.y_axes:
            columns.append(Column(center=(xa.position, ya.position), width=size, depth=size))
    return columns


def build_beams(grid: GridSystem, width: float, depth: float, col_size: float) -> list[Beam]:
    """在相鄰軸線交點之間、沿軸線放梁,兩端裁到柱面(不含柱身)。"""
    half = col_size / 2
    beams: list[Beam] = []
    xs = [a.position for a in grid.x_axes]
    ys = [a.position for a in grid.y_axes]

    # 沿 X 向的梁(水平):同一條 Y 軸上,相鄰 X 之間。
    for y in ys:
        for x1, x2 in zip(xs, xs[1:]):
            beams.append(Beam(start=(x1 + half, y), end=(x2 - half, y), width=width, depth=depth))

    # 沿 Y 向的梁(垂直):同一條 X 軸上,相鄰 Y 之間。
    for x in xs:
        for y1, y2 in zip(ys, ys[1:]):
            beams.append(Beam(start=(x, y1 + half), end=(x, y2 - half), width=width, depth=depth))

    return beams


def build_floor_plan():
    """組出一份完整結構平面圖的 DXF 文件(尚未存檔),回傳 (doc, standard, layers)。"""

    standard = load_standard()
    doc = new_document()
    layers = apply_standard(doc, standard, prefix=FLOOR_PREFIX)
    msp = doc.modelspace()

    # (1) 軸網 + 編號 + 軸間尺寸。
    grid = build_grid_system(x_spacings=X_SPACINGS, y_spacings=Y_SPACINGS)
    draw_grid(msp, grid, layers)
    draw_grid_dimensions(msp, grid, layers)

    # (2) 柱:每個交點一根。
    for col in build_columns(grid, COLUMN_SIZE):
        draw_column(msp, col, layers["COLUMN"])

    # (3) 梁:相鄰柱之間,沿軸線;各自標註斷面。
    for beam in build_beams(grid, BEAM_WIDTH, BEAM_DEPTH, COLUMN_SIZE):
        draw_beam(msp, beam, layers["S-RCBMB"])
        label = beam_label_text(beam, standard.beam_section_format)
        draw_beam_label(msp, beam, label, layers["S-TEXTB"], text_height=TEXT_HEIGHT)

    # (4) 標題欄:放在平面圖右下方。
    xs = [a.position for a in grid.x_axes]
    ys = [a.position for a in grid.y_axes]
    tb_data = TitleBlockData(
        drawing_name="二層結構平面圖",
        drawing_number="S-02",
        scale="1:100",
        date="2026-07-09",
        drawn_by="成弘",
        checked_by="—",
    )
    # 標題欄寬 6000,對齊平面圖右緣,放在軸網下方(避開編號圈)。
    tb_insert = (max(xs) + 1200 - 6000, min(ys) - 8000)
    insert_title_block(msp, tb_data, layers, insert=tb_insert)

    return doc, standard, layers


def main() -> None:
    output_dir = _PROJECT_ROOT / "output"
    output_dir.mkdir(exist_ok=True)
    out_path = output_dir / "floor_plan.dxf"

    doc, standard, layers = build_floor_plan()
    doc.saveas(out_path)

    print(f"[OK] 已產生完整平面圖 DXF:{out_path}")
    print(f"     軸網:X {X_SPACINGS} / Y {Y_SPACINGS}")
    print(f"     圖層前綴:{FLOOR_PREFIX}")


if __name__ == "__main__":
    main()
