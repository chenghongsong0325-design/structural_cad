"""剖面圖 / 立面圖 —— 把 BuildingSpec 的各層平面沿高度疊起來畫(D3,多樓層)。

之前全是平面圖(俯視);剖面/立面是「側看」——把樓層沿標高(Z)疊起來,
一眼看出樓層數、層高、柱上下貫通、地下室、屋頂。資料全部來自
building_generator 的 BuildingSpec(各層 elevation/label/spec + floor_height),
本模組只讀不改,用鴨子型別接收(不 import design 層,避免反向依賴)。

座標約定:圖面水平 u = 平面的某一方向(axis="x" → 平面 X;"y" → 平面 Y),
          圖面鉛直 v = 標高 Z(1FL=0,地下為負)。直接用真實標高當 v,
          故標高數字與圖面位置一致;origin 只做整體平移擺位。

draw_section(剖面圖):沿一個方向剖切,畫——
    柱列(從基礎貫通到屋頂,直接展示上下對齊)、各層樓板 + 屋頂板、
    女兒牆、地盤線 GL + 基礎(柱腳基腳)、左側樓層標高符號(▽)、
    右側層高尺寸鏈、剖面標題。地下層一併畫出(在 GL 以下)。

draw_elevation(立面圖):看某一外牆立面(僅地上層,地下室在地面下不畫)——
    建築外輪廓、各層樓板線、該面外牆的門窗開口(投影到立面對應高度)、
    屋頂女兒牆、地盤線、左側標高。

⚠️ 待確認假設見模組結尾 PENDING(樓板/女兒牆/基礎尺寸、窗台高、剖面為
   「示意結構剖面」非逐牆精確剖切等)。
"""
from __future__ import annotations

from ezdxf.enums import TextEntityAlignment

from src.drafting.dim_chains import building_extent, wall_side

Point = tuple[float, float]

# ── 尺寸常數(mm)——待確認,皆為住宅常見經驗值 ──────────────────────────
SLAB_THICKNESS = 150      # 樓板厚
PARAPET_HEIGHT = 1000     # 屋頂女兒牆高
PARAPET_THICKNESS = 150   # 女兒牆厚
FOUNDATION_DEPTH = 1500   # 柱由最低樓板面再往下延伸(至基腳)的深度
FOOTING_WIDTH = 1600      # 獨立基腳寬
FOOTING_HEIGHT = 400      # 基腳高
WINDOW_SILL = 900         # 立面:窗台距該層樓板面高
WINDOW_HEIGHT = 1500      # 立面:窗高
DOOR_HEIGHT = 2100        # 立面:門高(由該層樓板面起算)
GROUND_EXTEND = 1500      # 地盤線超出建築兩側的長度
MARK_OFFSET = 2000        # 樓層標高符號離建築左緣的距離
DIM_OFFSET = 1500         # 層高尺寸鏈離建築右緣的距離
TEXT_H = 300              # 標高/標題文字高


# ---------------------------------------------------------------------------
# 從 BuildingSpec 抽出剖面/立面需要的資料(鴨子型別,不 import design 層)
# ---------------------------------------------------------------------------
def _axis_grid(spec, axis: str) -> tuple[float, float, list[float]]:
    """回傳沿 axis 的 (建築起緣, 建築迄緣, 軸線位置清單=柱列)。"""
    bx0, by0, bx1, by1 = building_extent(spec)
    if axis == "x":
        lo, hi, spac, o = bx0, bx1, spec.x_spacings, bx0
    else:
        lo, hi, spac, o = by0, by1, spec.y_spacings, by0
    axes = [o]
    for s in spac:
        axes.append(axes[-1] + s)
    return lo, hi, axes


def _levels(building) -> list:
    """由下而上排序的樓層(FloorLevel)。"""
    return sorted(building.floors, key=lambda f: f.elevation)


def _ground_elevation(building) -> float:
    """地盤線標高 GL = 1FL(level==1)的標高;無 1F 時取最低地上層。"""
    ups = [f for f in building.floors if f.level >= 1]
    return min(ups, key=lambda f: f.level).elevation if ups else 0.0


def _elev_text(e: float) -> str:
    """標高數字:0→±0.00,正→+X.XX,負→-X.XX(單位 m)。"""
    if abs(e) < 1.0:
        return "±0.00"
    return f"{'+' if e > 0 else '-'}{abs(e)/1000:.2f}"


# ---------------------------------------------------------------------------
# 繪圖小工具
# ---------------------------------------------------------------------------
def _rect(msp, u0, v0, u1, v1, layer, origin=(0.0, 0.0)) -> None:
    ou, ov = origin
    msp.add_lwpolyline(
        [(u0 + ou, v0 + ov), (u1 + ou, v0 + ov),
         (u1 + ou, v1 + ov), (u0 + ou, v1 + ov)],
        close=True, dxfattribs={"layer": layer})


def _line(msp, u0, v0, u1, v1, layer, origin=(0.0, 0.0)) -> None:
    ou, ov = origin
    msp.add_line((u0 + ou, v0 + ov), (u1 + ou, v1 + ov),
                 dxfattribs={"layer": layer})


def _text(msp, s, u, v, layer, *, height=TEXT_H,
          align=TextEntityAlignment.MIDDLE_RIGHT, origin=(0.0, 0.0)) -> None:
    ou, ov = origin
    msp.add_text(s, height=height, dxfattribs={"layer": layer, "style": "STRUCT"}
                 ).set_placement((u + ou, v + ov), align=align)


def _level_mark(msp, u, e, label, layer_text, origin=(0.0, 0.0)) -> None:
    """樓層標高符號:▽(頂點觸及標高線)+ 引線 + 「標籤 標高」文字(左側)。"""
    ou, ov = origin
    t = 180.0
    # 下三角(頂點在標高線上)。
    msp.add_lwpolyline(
        [(u + ou, e + ov), (u - t + ou, e + t * 1.4 + ov),
         (u + t + ou, e + t * 1.4 + ov)],
        close=True, dxfattribs={"layer": layer_text})
    _text(msp, f"{label}  {_elev_text(e)}", u - t - 120, e + t * 1.4,
          layer_text, height=TEXT_H, align=TextEntityAlignment.MIDDLE_RIGHT,
          origin=origin)


def _stair_flight(msp, stair, axis: str, e0: float, e1: float,
                  layer: str, origin: Point) -> None:
    """一段樓梯的剖面示意:階梯折線(踏步)+ 底下平行的梯板斜線。

    stair 鴨子型別吃 Stair(單跑,steps)或 UStair(折返,steps_per_flight×2);
    u 範圍 = 樓梯符號在剖切方向上的投影(origin + width/length 依方向)。
    """
    ox, oy = stair.origin
    along_x = stair.direction in ("east", "west")   # 行進方向沿 X?
    if axis == "x":
        u0 = ox
        u1 = ox + (stair.length if along_x else stair.width)
    else:
        u0 = oy
        u1 = oy + (stair.width if along_x else stair.length)

    n = getattr(stair, "steps", None) or getattr(stair, "steps_per_flight", 8) * 2
    du, dv = (u1 - u0) / n, (e1 - e0) / n
    pts = [(u0, e0)]
    for i in range(n):                    # 一階 = 先上(級高)再前進(級深)
        pts.append((u0 + i * du, e0 + (i + 1) * dv))
        pts.append((u0 + (i + 1) * du, e0 + (i + 1) * dv))
    ou, ov = origin
    msp.add_lwpolyline([(u + ou, v + ov) for u, v in pts],
                       dxfattribs={"layer": layer})
    # 梯板底斜線(平行於爬升方向,往下移一個樓板厚)。
    _line(msp, u0, e0 - SLAB_THICKNESS, u1, e1 - SLAB_THICKNESS, layer, origin)


# ---------------------------------------------------------------------------
# 剖面圖
# ---------------------------------------------------------------------------
def draw_section(msp, building, layers: dict[str, str], *,
                 axis: str = "x", origin: Point = (0.0, 0.0),
                 title: str = "剖面圖") -> None:
    """畫一張結構剖面圖(含地下層)。axis 決定剖切/展開方向(見模組說明)。"""
    if axis not in ("x", "y"):
        raise ValueError(f"axis 需為 'x' 或 'y',收到 {axis!r}")

    lv = _levels(building)
    ref = lv[-1].spec                         # 以最高層(標準層)取柱列/範圍
    lo, hi, axes = _axis_grid(ref, axis)
    col = ref.column_size

    gl = _ground_elevation(building)
    roof = lv[-1].elevation + building.floor_height
    found_bottom = lv[0].elevation - FOUNDATION_DEPTH

    col_layer = layers["COL"]
    wall_layer = layers["WALL"]
    other = layers["OTHER"]
    text_layer = layers["TEXT"]

    # (1) 柱列:每條軸線一根,從基腳頂貫通到屋頂板底(展示上下對齊/連續)。
    for ug in axes:
        _rect(msp, ug - col / 2, found_bottom + FOOTING_HEIGHT, ug + col / 2, roof,
              col_layer, origin)
        # 基腳(獨立基礎示意)。
        _rect(msp, ug - FOOTING_WIDTH / 2, found_bottom,
              ug + FOOTING_WIDTH / 2, found_bottom + FOOTING_HEIGHT, col_layer, origin)

    # (2) 各層樓板 + 屋頂板(樓板面 = 標高,板身在其下)。
    for f in lv:
        _rect(msp, lo, f.elevation - SLAB_THICKNESS, hi, f.elevation, wall_layer, origin)
    _rect(msp, lo, roof - SLAB_THICKNESS, hi, roof, wall_layer, origin)

    # (2.5) 樓梯梯段(示意):相鄰兩層之間,沿剖切方向在樓梯間範圍畫階梯折線
    #       + 平行的梯板底斜線。與整張「示意剖面」同一精度等級——表達梯段
    #       上下貫通,不是精確剖切(方向垂直於剖切面的梯段也以側投影示意)。
    rail_layer = layers["HANDRAIL"]
    for lower, upper in zip(lv, lv[1:]):
        for st in getattr(lower.spec, "stairs", []):
            _stair_flight(msp, st, axis, lower.elevation, upper.elevation,
                          rail_layer, origin)

    # (3) 屋頂女兒牆(兩端)。
    _rect(msp, lo, roof, lo + PARAPET_THICKNESS, roof + PARAPET_HEIGHT, wall_layer, origin)
    _rect(msp, hi - PARAPET_THICKNESS, roof, hi, roof + PARAPET_HEIGHT, wall_layer, origin)

    # (4) 地盤線 GL(粗實線,超出建築兩側)。
    _line(msp, lo - GROUND_EXTEND, gl, hi + GROUND_EXTEND, gl, other, origin)
    _text(msp, "GL", hi + GROUND_EXTEND, gl + 120, text_layer,
          align=TextEntityAlignment.BOTTOM_RIGHT, origin=origin)

    # (5) 左側樓層標高符號 + (6) 右側層高尺寸鏈。
    mark_u = lo - MARK_OFFSET
    for f in lv:
        _level_mark(msp, mark_u, f.elevation, f.label + "L", text_layer, origin)
    _level_mark(msp, mark_u, roof, "RFL", text_layer, origin)

    elevs = [f.elevation for f in lv] + [roof]
    dim_u = hi + DIM_OFFSET
    ou, ov = origin
    for a, b in zip(elevs, elevs[1:]):
        dim = msp.add_linear_dim(
            base=(dim_u + ou, (a + b) / 2 + ov),
            p1=(hi + ou, a + ov), p2=(hi + ou, b + ov),
            angle=90, dimstyle="STRUCT", dxfattribs={"layer": layers["DIM"]})
        dim.render()
    # 全高(最外一道)。
    dim = msp.add_linear_dim(
        base=(dim_u + 1400 + ou, (elevs[0] + elevs[-1]) / 2 + ov),
        p1=(hi + ou, elevs[0] + ov), p2=(hi + ou, elevs[-1] + ov),
        angle=90, dimstyle="STRUCT", dxfattribs={"layer": layers["DIM"]})
    dim.render()

    # (7) 標題。
    _text(msp, title, (lo + hi) / 2, roof + PARAPET_HEIGHT + 1200, text_layer,
          height=TEXT_H * 2, align=TextEntityAlignment.MIDDLE_CENTER, origin=origin)


# ---------------------------------------------------------------------------
# 立面圖
# ---------------------------------------------------------------------------
def draw_elevation(msp, building, layers: dict[str, str], *,
                   side: str = "south", origin: Point = (0.0, 0.0),
                   title: str = "立面圖") -> None:
    """畫某一外牆的立面(僅地上層)。side = south/north/east/west。"""
    if side not in ("south", "north", "east", "west"):
        raise ValueError(f"side 需為 south/north/east/west,收到 {side!r}")
    axis = "x" if side in ("south", "north") else "y"

    lv = [f for f in _levels(building) if f.level >= 1]     # 地上層才有立面
    if not lv:
        raise ValueError("沒有地上樓層,無法畫立面")
    ref = lv[-1].spec
    lo, hi, _ = _axis_grid(ref, axis)

    gl = _ground_elevation(building)
    roof = lv[-1].elevation + building.floor_height

    wall_layer = layers["WALL"]
    dw_layer = layers["DW"]
    other = layers["OTHER"]
    text_layer = layers["TEXT"]

    # (1) 外輪廓 + 女兒牆頂。
    _rect(msp, lo, gl, hi, roof, wall_layer, origin)
    _rect(msp, lo, roof, hi, roof + PARAPET_HEIGHT, wall_layer, origin)

    # (2) 各層樓板線(細)。
    for f in lv:
        if abs(f.elevation - gl) < 1.0:
            continue
        _line(msp, lo, f.elevation, hi, f.elevation, wall_layer, origin)

    # (3) 該面外牆的門窗,投影到立面對應高度。
    for f in lv:
        extent = building_extent(f.spec)
        for w in f.spec.walls:
            if wall_side(w, extent) != side:
                continue
            for op in w.openings:
                px, py = w.point_at(op.position)
                u = px if axis == "x" else py
                if op.kind == "window":
                    v0, v1 = f.elevation + WINDOW_SILL, f.elevation + WINDOW_SILL + WINDOW_HEIGHT
                else:  # door
                    v0, v1 = f.elevation, f.elevation + DOOR_HEIGHT
                _rect(msp, u - op.width / 2, v0, u + op.width / 2, v1, dw_layer, origin)

    # (4) 地盤線 + 標高。
    _line(msp, lo - GROUND_EXTEND, gl, hi + GROUND_EXTEND, gl, other, origin)
    mark_u = lo - MARK_OFFSET
    for f in lv:
        _level_mark(msp, mark_u, f.elevation, f.label + "L", text_layer, origin)
    _level_mark(msp, mark_u, roof, "RFL", text_layer, origin)

    # (5) 標題。
    _text(msp, title, (lo + hi) / 2, roof + PARAPET_HEIGHT + 1200, text_layer,
          height=TEXT_H * 2, align=TextEntityAlignment.MIDDLE_CENTER, origin=origin)


# =============================================================================
# PENDING(待確認假設)
# =============================================================================
# 1. 這是「示意結構剖面」:柱列沿剖切方向的每條軸線各畫一根(把該軸線上一
#    整排柱投影成一根),樓板畫成滿跨的水平帶——著重表達樓層數/層高/柱上下
#    貫通/地下室,不是逐道牆精確剖切(真實剖面需指定剖切線、區分剖到與看到)。
# 2. 尺寸常數(樓板 150、女兒牆 1000、基礎 1500/基腳 1600×400、窗台 900、
#    窗高 1500、門高 2100)皆住宅常見經驗值,未依實際結構計算。
# 3. 地盤線 GL 取 1FL 標高;未做土壤剖面線、回填、地梁、筏基等。
# 4. 立面只畫指定一面外牆的門窗開口(矩形),未畫窗框分割、落水管、雨遮、
#    外牆材質分格線;地下層不畫(在地面下)。
# 5. 樓梯已入剖面(E4):相鄰兩層間畫階梯折線+梯板底斜線(HANDRAIL 層),
#    以樓梯符號在剖切方向的投影當範圍——是「示意」:行進方向垂直於剖切面
#    的梯段也用側投影畫(真實剖面該畫剖到的平台/梯段虛實線);電梯井未剖出。
# =============================================================================
