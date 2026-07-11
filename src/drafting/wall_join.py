"""牆角接合 —— 把多道牆的 footprint 聯集後,只畫合併外輪廓。

問題:wall.py 的 draw_wall 把每道牆各自畫成封閉矩形。兩道牆交會(L/T/X 形)時,
兩個矩形只是重疊,交會處會看到互相穿越的線與多餘封口,不是乾淨的接角。

解法:把每道牆(已扣除洞口)的每一段實牆轉成一個多邊形,對「全部牆段」做多邊形聯集
(shapely unary_union),聯集後重疊處自動合而為一;只畫合併後的外輪廓(外環 + 內孔),
交會處就乾淨了。這個做法對任意角度、L/T/X 甚至更複雜的交會都通用。

與 draw_wall 的分工:
  * draw_wall(單道牆):畫單一牆或不在意接角時用,簡單直接。
  * draw_walls_joined(一組牆):要乾淨接角時用,傳入整組牆一起聯集。
  兩者都吃同一個 Wall 資料模型;洞口一樣會在 footprint 上形成缺口。

典型用法::

    from src.drafting.wall_join import draw_walls_joined
    draw_walls_joined(msp, walls, layers["A-WALL"])
"""
from __future__ import annotations

from shapely.geometry import GeometryCollection, Polygon
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

from src.drafting.wall import Wall, solid_segments


def _segment_polygons(wall: Wall) -> list[Polygon]:
    """把一道牆的每一段實牆(扣掉洞口後)轉成一個矩形多邊形。"""
    nx, ny = wall.normal_vector
    half_t = wall.thickness / 2
    polys: list[Polygon] = []
    for a, b in solid_segments(wall.length, wall.openings):
        (ax, ay) = wall.point_at(a)
        (bx, by) = wall.point_at(b)
        polys.append(
            Polygon([
                (ax + nx * half_t, ay + ny * half_t),
                (bx + nx * half_t, by + ny * half_t),
                (bx - nx * half_t, by - ny * half_t),
                (ax - nx * half_t, ay - ny * half_t),
            ])
        )
    return polys


Ring = "list[tuple[float, float]]"  # 一圈多邊形角點(給 subtract 用)


def merged_wall_footprint(
    walls: list[Wall],
    subtract: list[Ring] | None = None,
) -> BaseGeometry:
    """回傳所有牆(扣除洞口)聯集後的 footprint。

    可能是 Polygon(全部連在一起)或 MultiPolygon(有分開的牆群);
    沒有任何實牆時回傳空的 GeometryCollection。

    subtract: 一串「要從牆裡挖掉」的多邊形(各以角點串列表示)。用途是柱內不畫牆線——
              把柱斷面(members.column_corners)傳進來,牆線就會停在柱面、不進柱內。
    """
    polys: list[Polygon] = []
    for w in walls:
        polys.extend(_segment_polygons(w))
    if not polys:
        return GeometryCollection()
    footprint = unary_union(polys)

    if subtract:
        obstacles = unary_union([Polygon(ring) for ring in subtract])
        footprint = footprint.difference(obstacles)
    return footprint


def _iter_polygons(geom: BaseGeometry):
    """把聯集結果攤平成一串 Polygon(處理 Polygon / MultiPolygon / 空)。"""
    if geom.is_empty:
        return
    if geom.geom_type == "Polygon":
        yield geom
    elif geom.geom_type == "MultiPolygon":
        yield from geom.geoms


def draw_wall_hatch(
    msp,
    walls: list[Wall],
    layer: str,
    *,
    subtract: list[Ring] | None = None,
    pattern: str = "ANSI31",
    scale: float = 30.0,
) -> int:
    """對一組牆的合併 footprint 加剖面線填充(HATCH),回傳建立的 HATCH 數。

    每個合併多邊形建一個 HATCH:外環 + 各內孔都加進邊界路徑(內孔自動不填)。
    pattern:RC 牆常用 ANSI31(斜線)、磚牆可用 ANSI37(交叉線)等,由呼叫端
    依材質分組各呼叫一次。subtract 同 merged_wall_footprint(柱內不填)。
    """
    merged = merged_wall_footprint(walls, subtract=subtract)
    n = 0
    for poly in _iter_polygons(merged):
        hatch = msp.add_hatch(dxfattribs={"layer": layer})
        hatch.set_pattern_fill(pattern, scale=scale)
        hatch.paths.add_polyline_path(list(poly.exterior.coords)[:-1], is_closed=True)
        for ring in poly.interiors:
            hatch.paths.add_polyline_path(list(ring.coords)[:-1], is_closed=True)
        n += 1
    return n


def draw_walls_joined(
    msp,
    walls: list[Wall],
    layer: str,
    subtract: list[Ring] | None = None,
) -> BaseGeometry:
    """把一組牆聯集後,只畫合併外輪廓(外環 + 內孔),自動清乾淨 L/T/X 交會。

    每個合併多邊形畫一條封閉外環多義線;若多邊形有內孔(例如封閉房間的內側牆面),
    每個內孔也各畫一條封閉多義線。回傳合併後的 shapely 幾何(方便呼叫端檢查/重用)。

    subtract: 要從牆裡挖掉的多邊形(如柱斷面);見 merged_wall_footprint。
    """
    merged = merged_wall_footprint(walls, subtract=subtract)
    for poly in _iter_polygons(merged):
        # shapely 的環首尾點重複,畫成封閉多義線時去掉最後一點。
        msp.add_lwpolyline(list(poly.exterior.coords)[:-1], close=True, dxfattribs={"layer": layer})
        for ring in poly.interiors:
            msp.add_lwpolyline(list(ring.coords)[:-1], close=True, dxfattribs={"layer": layer})
    return merged
