"""圖面表格(E4)—— 面積計算表 + 門窗表。

真實建築師交的圖,平面旁邊一定有表:每間房多大(面積計算表)、用了幾樘
什麼寬度的門窗(門窗表)。這是「圖看起來像不像真的」的最後一哩。

畫法:通用表格繪製器 _draw_table(標題列 + 表頭 + 資料列,格線 OTHER 層、
文字 A-TEXT 層),兩張表各自組資料列。放圖紙內框右側(draw_floor_plan 依
spec.schedules 開關呼叫,見 apartment_plan)。

典型用法::

    from src.drafting.schedule import draw_area_table, draw_opening_table

    h1 = draw_area_table(msp, spec, layers, origin=(x, y_top))
    draw_opening_table(msp, spec, layers, origin=(x, y_top - h1 - 800))

⚠️ 待確認假設見模組結尾 PENDING。
"""
from __future__ import annotations

from ezdxf.enums import TextEntityAlignment

Point = tuple[float, float]

ROW_H = 620          # 列高(mm,1:100 圖上 6.2mm)
TEXT_H = 260         # 表格文字高
TITLE_H = 320        # 表格標題文字高

# 欄寬(mm)
AREA_COLS = (2600, 1600)                 # 室名 | 面積
OPEN_COLS = (1000, 1000, 1500, 1000)     # 編號 | 類型 | 寬(cm) | 數量

TABLE_W = max(sum(AREA_COLS), sum(OPEN_COLS))   # 版面預留用(兩表同一直行)


def _draw_table(msp, origin: Point, col_widths: tuple, title: str,
                rows: list[list[str]], layers: dict[str, str]) -> float:
    """畫一張表(origin = 左上角),回傳總高度(mm)。

    rows[0] 是表頭;標題列橫跨整寬。格線 OTHER、文字 A-TEXT。
    """
    line_layer = layers["OTHER"]
    text_layer = layers["A-TEXT"]
    x0, y_top = origin
    w = sum(col_widths)
    n_rows = len(rows) + 1                       # +1 = 標題列
    h = n_rows * ROW_H

    # 外框 + 橫線。
    msp.add_lwpolyline(
        [(x0, y_top), (x0 + w, y_top), (x0 + w, y_top - h), (x0, y_top - h)],
        close=True, dxfattribs={"layer": line_layer})
    for i in range(1, n_rows):
        y = y_top - i * ROW_H
        msp.add_line((x0, y), (x0 + w, y), dxfattribs={"layer": line_layer})
    # 直線(標題列不分欄,從第二列開始畫)。
    cx = x0
    for cw in col_widths[:-1]:
        cx += cw
        msp.add_line((cx, y_top - ROW_H), (cx, y_top - h),
                     dxfattribs={"layer": line_layer})

    # 標題(置中)。
    msp.add_text(title, height=TITLE_H,
                 dxfattribs={"layer": text_layer, "style": "STRUCT"}
                 ).set_placement((x0 + w / 2, y_top - ROW_H / 2),
                                 align=TextEntityAlignment.MIDDLE_CENTER)
    # 資料列(各欄置中)。
    for r, row in enumerate(rows):
        y = y_top - (r + 1) * ROW_H - ROW_H / 2
        cx = x0
        for cw, cell in zip(col_widths, row):
            msp.add_text(str(cell), height=TEXT_H,
                         dxfattribs={"layer": text_layer, "style": "STRUCT"}
                         ).set_placement((cx + cw / 2, y),
                                         align=TextEntityAlignment.MIDDLE_CENTER)
            cx += cw
    return h


def draw_area_table(msp, spec, layers: dict[str, str], origin: Point) -> float:
    """面積計算表:逐室名稱+面積(m²)+合計。天井/中庭是戶外不列入。

    回傳表格總高度(mm),供呼叫端往下疊下一張表。
    """
    rooms = [r for r in spec.rooms if r.kind != "patio"]
    rows = [["室名", "面積(m²)"]]
    rows += [[r.name, f"{r.area_m2:.1f}"] for r in rooms]
    rows.append(["合計", f"{sum(r.area_m2 for r in rooms):.1f}"])
    return _draw_table(msp, origin, AREA_COLS, "面積計算表", rows, layers)


def draw_opening_table(msp, spec, layers: dict[str, str], origin: Point) -> float:
    """門窗表:門/窗各依洞口寬度分組編號(D1、D2…/W1、W2…,寬的排前面)。

    寬度從 DoorPlacement/WindowPlacement 指到的牆洞口讀(圖與表同一資料
    來源,不會對不上)。回傳表格總高度(mm)。
    """
    def widths(placements):
        out: dict[float, int] = {}
        for p in placements:
            w = spec.walls[p.wall_index].openings[p.opening_index].width
            out[w] = out.get(w, 0) + 1
        return sorted(out.items(), key=lambda kv: -kv[0])   # 寬 → 窄

    rows = [["編號", "類型", "寬度(cm)", "數量"]]
    for i, (w, n) in enumerate(widths(spec.doors), start=1):
        rows.append([f"D{i}", "門", f"{w / 10:.0f}", str(n)])
    for i, (w, n) in enumerate(widths(spec.windows), start=1):
        rows.append([f"W{i}", "窗", f"{w / 10:.0f}", str(n)])
    return _draw_table(msp, origin, OPEN_COLS, "門窗表", rows, layers)


# =============================================================================
# PENDING(待確認假設彙整)
# =============================================================================
# 1. 面積 = 牆中心線圍出的面積(沿用 Room.area_m2),非室內淨面積;合計不含
#    天井(戶外)。與法規樓地板面積定義有出入,示意等級。待確認。
# 2. 門窗只依「寬度」分組(D1/D2…);真實門窗表還分材質/開法/防火時效,
#    且各層同編號要一致(現在一層一張表各編各的)。待確認。
# 3. 表格版面(列高 620/欄寬/字高)為 1:100 圖面可讀的經驗值。待確認。
# =============================================================================
