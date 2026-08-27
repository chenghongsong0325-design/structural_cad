"""圖面配件 —— 樓層標示、北向箭頭、門窗編號、剖切符號、牆厚標註。

前三項是 ROADMAP B5;後三項是 2026-08-03 對照丙級檢定參考圖(見
`src/design/gap_analysis.py`)補上的 —— 那三個版本的參考圖裡,連**沒有家具、
沒有室名的空殼圖**都有這些,可見它們比家具還基本:

  * 樓層標示:一個大字(如「3F」),掛 TEXT 層。
  * 北向箭頭:可重用圖塊(圓 + 指北針三角 + N 字),插入時可旋轉,掛 OTHER 層。
  * **門窗編號**:每個開口旁一個帶圈編號(D1/W2…),編號與門窗表**同一來源**
    (`schedule.opening_codes`),圖與表一定對得起來。
  * **剖切符號**:平面上標「剖面圖從這裡剖、往這邊看」——剖面圖是另一張,
    平面沒指過去的話兩張圖對不起來。
  * **牆厚標註**:引線 + 「15cm RC Wall」。牆厚本來就在 `Wall.thickness` 裡,
    以前只是沒寫到圖上。

⚠️ 待確認:北向箭頭樣式(圓+三角+N 為常見畫法之一,各事務所不同);樓層標示
   字高預設 1500(1:100 出圖紙上 15mm);門窗編號圈徑/剖切符號臂長為 1:100
   可讀的經驗值。
"""
from __future__ import annotations

import math
from typing import Optional

from ezdxf.enums import TextEntityAlignment

from src.drafting.label_space import LabelSpace, text_box

Point = tuple[float, float]

NORTH_ARROW_BLOCK = "NORTH_ARROW"
NORTH_ARROW_RADIUS = 600      # 圖塊定義的半徑(mm)。待確認

# 門窗編號:圈半徑、字高、離牆面的淨距(mm)。
TAG_RADIUS = 230.0
TAG_TEXT_H = 220.0
TAG_OFFSET = 320.0
# 剖切符號:線超出建築的長度、箭頭大小、字高(mm)。
# ⚠️ 超出量 + 代號的位置要留在**退縮帶(2m)之內**,否則剖切代號會跑到地界線外面。
CUT_EXTEND = 900.0
CUT_ARROW = 450.0
CUT_TEXT_H = 420.0
# 牆厚標註:引線第一段長、水平段長、字高(mm)。
NOTE_LEG = 900.0
NOTE_TAIL = 700.0
NOTE_TEXT_H = 250.0
# 牆厚 ≥ 這個算 RC(與 wall_join 的剖面線分類同一條界線)。
RC_THICKNESS = 140.0


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


# ---------------------------------------------------------------------------
# 門窗編號(圖上)
# ---------------------------------------------------------------------------
def _building_center(spec) -> Point:
    """房間外接矩形的中心 —— 用來決定編號往牆的哪一側擺(往外擺)。"""
    xs = [p[0] for r in spec.rooms for p in r.points]
    ys = [p[1] for r in spec.rooms for p in r.points]
    if not xs:
        return (0.0, 0.0)
    return ((min(xs) + max(xs)) / 2.0, (min(ys) + max(ys)) / 2.0)


def draw_opening_marks(msp, spec, layers: dict[str, str],
                       space: Optional[LabelSpace] = None) -> int:
    """每個門窗洞口旁畫一個帶圈編號(D1/W2…)。回畫了幾個。

    編號取自 `schedule.opening_codes`(與門窗表同一來源)。位置在洞口中心往
    **背向建築中心**那一側偏移 —— 外牆的編號就落在建築外面(和參考圖一樣),
    內牆的落在鄰室裡,不會壓到牆體本身。"""
    from src.drafting.schedule import opening_codes

    codes = opening_codes(spec)
    if not codes:
        return 0
    cx, cy = _building_center(spec)
    line_layer = layers["OTHER"]
    text_layer = layers["A-TEXT"]
    n = 0
    for (wi, oi), code in sorted(codes.items()):
        try:
            wall = spec.walls[wi]
            op = wall.openings[oi]
        except (IndexError, AttributeError):
            continue
        px, py = wall.point_at(op.position)
        nx, ny = wall.normal_vector
        # 往背離建築中心的那一側擺(法線可能指向任一側,用內積決定正負)
        sign = 1.0 if (px - cx) * nx + (py - cy) * ny >= 0 else -1.0
        base = wall.thickness / 2.0 + TAG_OFFSET + TAG_RADIUS

        # 候選位置:先照原本的距離,撞到字就一階一階往外退;外側全滿再試內側。
        # (使用者 2026-08-19:「字跟字不要黏在一起」——D1/D2 最常壓到室名與面積。)
        # 沿牆的單位向量 —— 退不開時可以沿著牆滑一點(編號還是貼著那個洞口)。
        ux, uy = -ny, nx
        cands = []
        for s_ in (sign, -sign):
            for k in (1.0, 1.7, 2.4, 3.1):
                for slide in (0.0, 1.0, -1.0, 2.0, -2.0):
                    tx = px + nx * base * k * s_ + ux * TAG_RADIUS * 2.2 * slide
                    ty = py + ny * base * k * s_ + uy * TAG_RADIUS * 2.2 * slide
                    cands.append((text_box(code, TAG_TEXT_H, tx, ty,
                                           TextEntityAlignment.MIDDLE_CENTER),
                                  (tx, ty)))
        picked = space.take(cands) if space is not None else None
        tx, ty = picked[1] if picked else cands[0][1]   # 都撞 → 照原位,寧可疊也不能不畫

        msp.add_circle((tx, ty), radius=TAG_RADIUS,
                       dxfattribs={"layer": line_layer})
        msp.add_text(
            code, height=TAG_TEXT_H,
            dxfattribs={"layer": text_layer, "style": "STRUCT"},
        ).set_placement((tx, ty), align=TextEntityAlignment.MIDDLE_CENTER)
        n += 1
    return n


# ---------------------------------------------------------------------------
# 基地註記(基地 / 建築 / 建蔽率 / 前後院)
# ---------------------------------------------------------------------------
LOT_NOTE_H = 260.0              # 基地註記字高(比室名小、比門窗編號大)
LOT_NOTE_GAP = 900.0            # 註記與地界線下緣的距離
LOT_NOTE_LINE = 380.0           # 行距


def draw_lot_note(msp, spec, layers: dict[str, str]) -> int:
    """把 `spec.lot_note` 寫在地界線**下方**(左對齊地界線左緣)。回畫了幾行。

    為什麼要有這段字:圖上只看得到建築尺寸的話,人會以為「基地尺寸被無視了」;
    而「這塊地為什麼只蓋這麼深」的答案是**建蔽率**,不寫出來沒有人看得出來
    (使用者 2026-08-25 指出參考圖的建蔽率其實高達 93%,那是舊市區街屋才有的
    密度 —— 這種判斷要看得到數字才做得出來)。

    ⚠️ 放在地界線外側,不進圖內,才不會跟室名/家具搶位置。
    """
    xs = [p[0] for p in spec.site_boundary]
    ys = [p[1] for p in spec.site_boundary]
    x0, y0 = min(xs), min(ys)
    lines = [ln for ln in str(spec.lot_note).splitlines() if ln.strip()]
    for i, line in enumerate(lines):
        msp.add_text(
            line, height=LOT_NOTE_H,
            dxfattribs={"layer": layers["A-TEXT"], "style": "STRUCT"},
        ).set_placement((x0, y0 - LOT_NOTE_GAP - i * LOT_NOTE_LINE),
                        align=TextEntityAlignment.TOP_LEFT)
    return len(lines)


# ---------------------------------------------------------------------------
# 剖切符號
# ---------------------------------------------------------------------------
def draw_section_mark(msp, spec, layers: dict[str, str], *, label: str = "A",
                      axis: str = "x", at: float | None = None,
                      look: int = 1) -> None:
    """平面上的剖切符號:剖切線 + 兩端箭頭 + 剖面代號(A—A)。

    axis="x":剖切線沿平面 X 橫過建築(對應 `section.draw_section(axis="x")`);
    "y" 則是縱向。at = 剖切位置(預設建築正中);look = +1/-1 決定往哪個方向看。

    為什麼要有:剖面圖是**另一張圖**,平面上沒有標「從這裡剖」的話,看圖的人
    對不起來 —— 參考圖三個版本(含 92 年的空殼圖)都有這個符號。"""
    if axis not in ("x", "y"):
        raise ValueError(f"axis 需為 'x' 或 'y',收到 {axis!r}")
    xs = [p[0] for r in spec.rooms for p in r.points]
    ys = [p[1] for r in spec.rooms for p in r.points]
    if not xs:
        return
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    line_layer = layers["OTHER"]
    text_layer = layers["A-TEXT"]
    sign = 1 if look >= 0 else -1

    if axis == "x":                      # 橫向剖切線(y 固定)
        cut = (y0 + y1) / 2.0 if at is None else at
        a = (x0 - CUT_EXTEND, cut)
        b = (x1 + CUT_EXTEND, cut)
        arrow = (0.0, float(sign))       # 視線方向(南北)
    else:                                # 縱向剖切線(x 固定)
        cut = (x0 + x1) / 2.0 if at is None else at
        a = (cut, y0 - CUT_EXTEND)
        b = (cut, y1 + CUT_EXTEND)
        arrow = (float(sign), 0.0)

    msp.add_line(a, b, dxfattribs={"layer": line_layer})
    ux, uy = arrow
    for end, inward in ((a, 1.0), (b, -1.0)):
        ex, ey = end
        # 箭頭:從端點往視線方向畫一段 + 兩撇
        hx, hy = ex + ux * CUT_ARROW, ey + uy * CUT_ARROW
        msp.add_line((ex, ey), (hx, hy), dxfattribs={"layer": line_layer})
        for s in (-1, 1):
            bx = hx - ux * CUT_ARROW * 0.45 + (-uy) * s * CUT_ARROW * 0.25
            by = hy - uy * CUT_ARROW * 0.45 + ux * s * CUT_ARROW * 0.25
            msp.add_line((hx, hy), (bx, by), dxfattribs={"layer": line_layer})
        # 代號:放在端點外側(沿剖切線再往外一點)
        if axis == "x":
            tx, ty = ex - inward * CUT_ARROW * 1.1, ey
        else:
            tx, ty = ex, ey - inward * CUT_ARROW * 1.1
        msp.add_text(
            label, height=CUT_TEXT_H,
            dxfattribs={"layer": text_layer, "style": "STRUCT"},
        ).set_placement((tx, ty), align=TextEntityAlignment.MIDDLE_CENTER)


# ---------------------------------------------------------------------------
# 牆厚標註
# ---------------------------------------------------------------------------
def wall_note_text(thickness: float) -> str:
    """牆厚 → 圖上的文字(參考圖寫法:「15cm RC Wall」)。"""
    cm = thickness / 10.0
    kind = "RC Wall" if thickness >= RC_THICKNESS else "磚牆"
    return f"{cm:.0f}cm {kind}"


def draw_wall_notes(msp, spec, layers: dict[str, str],
                    space: Optional[LabelSpace] = None) -> int:
    """每一種牆厚挑一道代表牆,拉引線寫厚度(如「15cm RC Wall」)。回畫了幾條。

    只挑代表牆(每種厚度最長的那道):每道牆都標會把圖蓋滿,參考圖也是各標一次。"""
    line_layer = layers["OTHER"]
    text_layer = layers["A-TEXT"]
    best: dict = {}
    for w in spec.walls:
        if getattr(w, "stair_guard", False):     # 導牆是配件,不是主結構
            continue
        t = round(float(w.thickness), 1)
        if t not in best or w.length > best[t].length:
            best[t] = w

    cx, cy = _building_center(spec)
    n = 0
    for t, wall in sorted(best.items(), key=lambda kv: -kv[0]):
        text = wall_note_text(t)
        # 候選:沿這道牆挑幾個下引線的點 × 幾種引線長度。原本只有「牆中點 +
        # 固定長度」一種,撞到室名/面積/軸網編號就只能疊上去 —— 這是實測裡
        # 最大宗的疊字來源(每層每個尺寸都撞「15cm RC Wall」× 軸網編號 B)。
        cands = []
        for frac in (0.5, 0.35, 0.65, 0.2, 0.8):
            px, py = wall.point_at(wall.length * frac)
            nx, ny = wall.normal_vector
            sign = 1.0 if (px - cx) * nx + (py - cy) * ny >= 0 else -1.0
            for leg in (NOTE_LEG, NOTE_LEG * 1.8, NOTE_LEG * 2.6):
                p1 = (px + nx * sign * leg, py + ny * sign * leg)
                tail = NOTE_TAIL if p1[0] >= cx else -NOTE_TAIL
                p2 = (p1[0] + tail, p1[1])
                align = (TextEntityAlignment.MIDDLE_LEFT if tail > 0
                         else TextEntityAlignment.MIDDLE_RIGHT)
                tp = (p2[0] + math.copysign(120.0, tail), p2[1])
                cands.append((text_box(text, NOTE_TEXT_H, tp[0], tp[1], align),
                              ((px, py), p1, p2, tp, align)))
        picked = space.take(cands) if space is not None else None
        (px, py), p1, p2, tp, align = picked[1] if picked else cands[0][1]

        msp.add_line((px, py), p1, dxfattribs={"layer": line_layer})
        msp.add_line(p1, p2, dxfattribs={"layer": line_layer})
        msp.add_text(
            text, height=NOTE_TEXT_H,
            dxfattribs={"layer": text_layer, "style": "STRUCT"},
        ).set_placement(tp, align=align)
        n += 1
    return n
