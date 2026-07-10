"""外圍尺寸鏈 —— 對平面圖四個邊自動生成 2~3 層尺寸標註(掛 DIM 圖層)。

真實建案圖(與丙級檢定圖)的靈魂:圖面四周由內到外的多層尺寸——

    建築外緣 →|細部層|軸距層|總長層|
                1200    2000    2800   (離建築外緣的距離,mm;待確認)

  * 內層(細部):牆段-洞口-牆段…——從每道「外牆」的洞口位置自動算出分割點,
    逐段標註。該邊沒有外牆、或外牆沒開洞(細部=總長,重複)就略過此層。
  * 中層(軸距):軸線間距,每跨一道(取代 gridlines.draw_grid_dimensions
    只標上/右兩邊的做法——這裡四個邊都標)。
  * 外層(總長):建築總長/總寬,一道。

用法(獨立使用或經 FloorPlanSpec.dim_chains=True 由生產線呼叫)::

    from src.drafting.dim_chains import draw_dim_chains
    n = draw_dim_chains(msp, spec, layers)   # 回傳畫了幾道標註

spec 只需要鴨子型別的四個欄位:grid_origin、x_spacings、y_spacings、walls。

⚠️ 待確認假設見模組結尾 PENDING 區塊(層距、標註起訖點的取法)。
"""
from __future__ import annotations

from typing import Optional

from src.drafting.wall import Wall

Point = tuple[float, float]

# 三層離建築外緣的距離(由內到外:細部/軸距/總長)。待確認。
TIER_OFFSETS = (1200.0, 2000.0, 2800.0)

# 判斷「牆貼在建築外緣線上」的容差(mm)。
SIDE_TOL = 1.0

_SIDES = ("south", "north", "west", "east")


# ---------------------------------------------------------------------------
# 建築範圍與外牆偵測(純計算)
# ---------------------------------------------------------------------------
def building_extent(spec) -> tuple[float, float, float, float]:
    """由軸網原點與跨距推回建築範圍 (bx0, by0, bx1, by1)。"""
    bx0, by0 = spec.grid_origin
    return (bx0, by0, bx0 + sum(spec.x_spacings), by0 + sum(spec.y_spacings))


def wall_side(wall: Wall, extent: tuple[float, float, float, float]) -> Optional[str]:
    """判斷一道牆是否貼在建築某一邊的外緣線上(兩端點都在線上才算)。

    回傳 "south"/"north"/"west"/"east" 或 None(內牆/斜牆)。
    """
    bx0, by0, bx1, by1 = extent
    (x1, y1), (x2, y2) = wall.start, wall.end
    if abs(y1 - by0) <= SIDE_TOL and abs(y2 - by0) <= SIDE_TOL:
        return "south"
    if abs(y1 - by1) <= SIDE_TOL and abs(y2 - by1) <= SIDE_TOL:
        return "north"
    if abs(x1 - bx0) <= SIDE_TOL and abs(x2 - bx0) <= SIDE_TOL:
        return "west"
    if abs(x1 - bx1) <= SIDE_TOL and abs(x2 - bx1) <= SIDE_TOL:
        return "east"
    return None


def detail_points(walls: list[Wall], side: str,
                  extent: tuple[float, float, float, float]) -> list[float]:
    """某一邊的細部分割點(沿該邊軸向的世界座標,已排序去重)。

    分割點 = 該邊每道外牆的:兩端點 + 各洞口的左右邊。
    South/North 回傳 x 座標;West/East 回傳 y 座標。
    """
    axis = 0 if side in ("south", "north") else 1   # 沿邊軸:x 或 y
    pts: set[float] = set()
    for w in walls:
        if wall_side(w, extent) != side:
            continue
        start_c = w.start[axis]
        end_c = w.end[axis]
        sign = 1.0 if end_c >= start_c else -1.0    # 牆的行進方向(±)
        pts.add(start_c)
        pts.add(end_c)
        for op in w.openings:
            a, b = op.span                           # 沿牆距離
            a, b = max(0.0, a), min(w.length, b)     # 裁到牆內
            if b > a:
                pts.add(start_c + sign * a)
                pts.add(start_c + sign * b)
    return sorted(pts)


def grid_points(spec, side: str) -> list[float]:
    """某一邊的軸線位置(South/North → 各 X 軸;West/East → 各 Y 軸)。"""
    bx0, by0 = spec.grid_origin
    if side in ("south", "north"):
        pts, c = [bx0], bx0
        for s in spec.x_spacings:
            c += s
            pts.append(c)
    else:
        pts, c = [by0], by0
        for s in spec.y_spacings:
            c += s
            pts.append(c)
    return pts


# ---------------------------------------------------------------------------
# 畫標註
# ---------------------------------------------------------------------------
def _add_chain(msp, side: str, coords: list[float], offset: float,
               extent: tuple[float, float, float, float],
               layer: str, dimstyle: str) -> int:
    """沿某一邊、離外緣 offset 處,把 coords 相鄰兩點逐段標註。回傳道數。"""
    bx0, by0, bx1, by1 = extent
    n = 0
    for a, b in zip(coords, coords[1:]):
        if b - a <= SIDE_TOL:                        # 重合點不標
            continue
        if side == "south":
            p1, p2 = (a, by0), (b, by0)
            base, angle = ((a + b) / 2, by0 - offset), 0
        elif side == "north":
            p1, p2 = (a, by1), (b, by1)
            base, angle = ((a + b) / 2, by1 + offset), 0
        elif side == "west":
            p1, p2 = (bx0, a), (bx0, b)
            base, angle = (bx0 - offset, (a + b) / 2), 90
        else:  # east
            p1, p2 = (bx1, a), (bx1, b)
            base, angle = (bx1 + offset, (a + b) / 2), 90
        dim = msp.add_linear_dim(
            base=base, p1=p1, p2=p2, angle=angle,
            dimstyle=dimstyle, dxfattribs={"layer": layer},
        )
        dim.render()
        n += 1
    return n


def draw_dim_chains(msp, spec, layers: dict[str, str], *,
                    tiers: tuple[float, float, float] = TIER_OFFSETS,
                    dimstyle: str = "STRUCT") -> int:
    """對 spec 的四個邊畫「細部/軸距/總長」尺寸鏈,回傳總共畫了幾道標註。

    細部層:該邊分割點少於 3 個(= 沒外牆、或外牆無洞口)時略過,避免與
    總長層重複。三層離建築外緣由近到遠:tiers = (細部, 軸距, 總長)。
    """
    layer = layers["DIM"]
    extent = building_extent(spec)
    bx0, by0, bx1, by1 = extent
    detail_off, grid_off, overall_off = tiers
    total = 0

    for side in _SIDES:
        # (1) 細部層:牆段-洞口-牆段。
        pts = detail_points(spec.walls, side, extent)
        if len(pts) >= 3:
            total += _add_chain(msp, side, pts, detail_off, extent, layer, dimstyle)

        # (2) 軸距層。
        total += _add_chain(msp, side, grid_points(spec, side), grid_off,
                            extent, layer, dimstyle)

        # (3) 總長層。
        span = [bx0, bx1] if side in ("south", "north") else [by0, by1]
        total += _add_chain(msp, side, span, overall_off, extent, layer, dimstyle)

    return total


# =============================================================================
# PENDING(待確認假設彙整)
# =============================================================================
# 1. 層距:三層離建築外緣 1200/2000/2800(層間 800)。真實圖常依比例尺調
#    (1:100 出圖紙上約 8mm 一層);且軸網編號圈(draw_grid 的 bubble)要退到
#    最外層之外——生產線在開尺寸鏈時把 bubble_offset 加大到 2600。待確認。
# 2. 標註起訖點的取法:細部層量到「牆中心線端點與洞口邊」(洞口位置本來就
#    定義在牆中心線上);總長/軸距量軸線位置。實務有的量到牆外皮——若要
#    改成外皮,分割點各外推半個牆厚即可。待確認。
# 3. 細部層只涵蓋「貼在建築外緣線上的外牆」;斜牆、退縮立面的牆不會被
#    偵測(wall_side 回 None),屆時需擴充。
# 4. 四個邊都標三層;有的圖只標兩個邊(下+右)。要省略哪幾邊由呼叫端
#    自行不用此函式、或之後加參數。待確認。
# 5. 內牆(如樓梯間牆)上的門洞不進外圍尺寸鏈——室內細部尺寸是另一種
#    標註(引線/內部尺寸),之後視需要另做。
# =============================================================================
