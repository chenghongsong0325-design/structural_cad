"""Hello CAD(標準版)—— 用「繪圖標準系統」畫柱、梁、軸線。

跟最初的 hello_cad 不同,這一版不再把圖層寫死在程式裡,而是:
  1. 讀取繪圖標準設定檔(config/standards/default.yaml)
  2. 用標準系統一次建立所有圖層 / 文字型 / 標註型
  3. 把柱、梁、軸線分別掛到「正確的圖層」上
  4. 存成 output/hello_cad.dxf

這示範了往後所有製圖程式的基本流程:先套標準,再畫圖。
"""
from __future__ import annotations

import sys
from pathlib import Path

# 直接用 `python src/drafting/hello_cad.py` 執行時,sys.path[0] 是本檔所在資料夾
# (src/drafting),不含專案根,會 import 不到 src 套件。先把專案根補進去。
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from ezdxf.enums import TextEntityAlignment

from src.drafting.gridlines import build_grid_system, draw_grid, draw_grid_dimensions
from src.standards.loader import apply_standard, load_standard, new_document


# ---------------------------------------------------------------------------
# 這張示範圖的參數(全部 mm)
# ---------------------------------------------------------------------------
# 樓層前綴:示範「同一套標準套到某一層」。實務上這裡會是外部參考(xref)檔名,
# 例如 "2F建築底圖";設成 "" 就不加前綴、圖層名直接是代碼。
FLOOR_PREFIX = "2F建築底圖"

SPAN = 6000          # 柱心到柱心的距離(柱距),約 6 公尺
COLUMN_SIZE = 500    # 柱斷面 500×500(方柱)
BEAM_WIDTH = 240     # 梁寬(對應下面斷面標示 240×235 的「寬」)
BEAM_DEPTH = 235     # 梁深/梁高(對應斷面標示的「深」)

# 標註/文字高度(模型單位 mm)。250 對應 1:100 出圖(紙上約 2.5mm)。
# 待確認:實際字高待業主確認;標準檔的文字型高度目前設 0(可變),故在這裡帶入。
TEXT_HEIGHT = 250


def _add_rectangle(msp, cx: float, cy: float, width: float, height: float, layer: str):
    """以 (cx, cy) 為中心,畫一個 width×height 的封閉矩形(掛在指定圖層)。"""
    hw, hh = width / 2, height / 2
    points = [
        (cx - hw, cy - hh),
        (cx + hw, cy - hh),
        (cx + hw, cy + hh),
        (cx - hw, cy + hh),
    ]
    msp.add_lwpolyline(points, close=True, dxfattribs={"layer": layer})


def build_hello_cad():
    """建立並回傳一份「套好標準、畫好柱/梁/軸線」的 DXF 文件(尚未存檔)。"""

    # (1) 載入標準,建立文件,套用標準。
    #     apply_standard 會回傳 {代碼 -> 完整圖層名},後面掛幾何時就用這個對照表,
    #     這樣不論有沒有樓層前綴,程式碼都不用改。
    standard = load_standard()
    doc = new_document()  # ezdxf.new("R2010", setup=True),已含 CENTER 等標準線型
    layers = apply_standard(doc, standard, prefix=FLOOR_PREFIX)

    msp = doc.modelspace()

    # 兩根柱子的中心位置:x=0 與 x=SPAN,都在軸線 y=0 上。
    col_x = [0, SPAN]

    # ── 軸網(掛在 AXIS / S-TEXTB 圖層,由 gridlines 模組產生)───────────────
    #    X 方向兩條軸線(數字編號 1、2)對應兩根柱心;
    #    Y 方向只有一條軸線(y_spacings=[] → 單一軸線在 0,編號 A)對應柱列所在的那條軸。
    grid = build_grid_system(x_spacings=[SPAN], y_spacings=[])
    draw_grid(msp, grid, layers)
    draw_grid_dimensions(msp, grid, layers)

    text_b = layers["S-TEXTB"]

    # ── 柱(掛在 COLUMN 圖層:紅色實線)────────────────────────────────────
    column = layers["COLUMN"]
    for x in col_x:
        _add_rectangle(msp, x, 0, COLUMN_SIZE, COLUMN_SIZE, column)

    # ── 梁(掛在 S-RCBMB 圖層:青色;梁主要輪廓)──────────────────────────
    #    梁沿 y=0 連接兩柱,寬 = BEAM_WIDTH;只畫柱內側之間的段落。
    beam = layers["S-RCBMB"]
    inner_left = col_x[0] + COLUMN_SIZE / 2
    inner_right = col_x[1] - COLUMN_SIZE / 2
    beam_len = inner_right - inner_left
    _add_rectangle(
        msp,
        cx=(inner_left + inner_right) / 2,
        cy=0,
        width=beam_len,
        height=BEAM_WIDTH,
        layer=beam,
    )

    # ── 梁斷面標示文字(掛 S-TEXTB,用標準定義的「寬×深」格式)──────────────
    #    beam_section_format 來自設定檔,例如 "{width}×{depth}" → "240×235"。
    beam_label = standard.beam_section_format.format(width=BEAM_WIDTH, depth=BEAM_DEPTH)
    msp.add_text(
        beam_label,
        height=TEXT_HEIGHT,
        dxfattribs={"layer": text_b, "style": "STRUCT"},
    ).set_placement((SPAN / 2, BEAM_WIDTH / 2 + 200), align=TextEntityAlignment.BOTTOM_CENTER)

    # ── 標題(掛 S-TEXTB)──────────────────────────────────────────────────
    #    放在軸線編號圈(X 方向圈心在 y = -extension - bubble_offset = -3200,半徑 350)
    #    下方,避免重疊。
    mtext = msp.add_mtext(
        "Hello CAD — 柱 / 梁 / 軸線(標準版)",
        dxfattribs={"layer": text_b, "style": "STRUCT", "char_height": TEXT_HEIGHT},
    )
    mtext.set_location((-1200, -4200))

    return doc, standard, layers


def main() -> None:
    """執行:套標準、畫圖、存成 DXF。"""

    output_dir = _PROJECT_ROOT / "output"
    output_dir.mkdir(exist_ok=True)
    out_path = output_dir / "hello_cad.dxf"

    doc, standard, layers = build_hello_cad()
    doc.saveas(out_path)

    print(f"[OK] 已產生 DXF:{out_path}")
    print(f"     使用標準:{standard.name}(共 {len(standard.layers)} 個圖層代碼)")
    print(f"     樓層前綴:{FLOOR_PREFIX or '(無)'}")
    print("     柱掛在 :", layers["COLUMN"])
    print("     梁掛在 :", layers["S-RCBMB"])
    print("     軸線掛在:", layers["AXIS"])


if __name__ == "__main__":
    main()
