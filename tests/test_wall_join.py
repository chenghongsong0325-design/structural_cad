"""牆角接合(wall_join)的單元測試。

驗證重點:
  1. 聯集面積正確(單牆、含洞口、L 接角扣重疊)。
  2. 連通性:相接的牆聯集成「單一多邊形」、分開的牆是「多個多邊形」——
     這正是「接角有沒有接起來」的判準。
  3. draw_walls_joined 把輪廓畫在正確圖層,且交會處不再各自成矩形(用面積/多邊形數驗)。
"""
from __future__ import annotations

import pytest

from src.drafting.wall import Opening, Wall
from src.drafting.wall_join import (
    draw_walls_joined,
    merged_wall_footprint,
)
from src.standards.loader import apply_standard, load_standard, new_document


@pytest.fixture()
def doc_and_layers():
    standard = load_standard()
    doc = new_document()
    layers = apply_standard(doc, standard)
    return doc, layers


# ---------------------------------------------------------------------------
# 1) 聯集面積
# ---------------------------------------------------------------------------
def test_single_wall_footprint_area() -> None:
    wall = Wall(start=(0, 0), end=(4000, 0), thickness=200)
    fp = merged_wall_footprint([wall])
    assert fp.geom_type == "Polygon"
    assert fp.area == pytest.approx(4000 * 200)


def test_wall_with_opening_area_reduced() -> None:
    """洞口在 footprint 上形成缺口,面積 = (牆長 - 洞口寬) × 厚。"""
    wall = Wall(start=(0, 0), end=(4000, 0), thickness=200, openings=[Opening(2000, 900)])
    fp = merged_wall_footprint([wall])
    assert fp.area == pytest.approx((4000 - 900) * 200)


def test_L_junction_area_subtracts_overlap() -> None:
    """兩道垂直牆在角落交會:聯集面積 = 兩塊 - 重疊的角部方塊(只算一次)。"""
    t = 200
    a = Wall(start=(0, 0), end=(4000, 0), thickness=t)      # 橫牆
    b = Wall(start=(4000, 0), end=(4000, 3000), thickness=t)  # 直牆,在 (4000,0) 交會
    fp = merged_wall_footprint([a, b])
    # 角部重疊方塊 = (t/2) × (t/2):橫牆伸到 x=4000、直牆半寬 x[3900,4100],
    # 交會處 x[3900,4000]×y[0,100] = t/2 × t/2。
    expected = a.length * t + b.length * t - (t * t / 4)
    assert fp.geom_type == "Polygon"      # 有接起來 → 單一多邊形
    assert fp.area == pytest.approx(expected)


# ---------------------------------------------------------------------------
# 2) 連通性:接起來 vs 沒接起來
# ---------------------------------------------------------------------------
def test_joined_walls_are_single_polygon() -> None:
    """相接的一圈四道牆 → 聯集成單一多邊形(中間是內孔)。"""
    t = 200
    walls = [
        Wall((0, 0), (4000, 0), thickness=t),
        Wall((4000, 0), (4000, 4000), thickness=t),
        Wall((4000, 4000), (0, 4000), thickness=t),
        Wall((0, 4000), (0, 0), thickness=t),
    ]
    fp = merged_wall_footprint(walls)
    assert fp.geom_type == "Polygon"
    # 封閉一圈牆 → 中間有一個內孔(房間內側)。
    assert len(fp.interiors) == 1


def test_disconnected_walls_are_multipolygon() -> None:
    """兩道離很遠、不相接的牆 → MultiPolygon(兩塊)。"""
    walls = [
        Wall((0, 0), (2000, 0), thickness=200),
        Wall((9000, 9000), (11000, 9000), thickness=200),
    ]
    fp = merged_wall_footprint(walls)
    assert fp.geom_type == "MultiPolygon"
    assert len(fp.geoms) == 2


def test_T_junction_is_single_polygon() -> None:
    """T 形:一道牆中段接上另一道牆 → 聯集成單一多邊形。"""
    main = Wall((0, 0), (6000, 0), thickness=200)
    branch = Wall((3000, 0), (3000, 3000), thickness=200)
    fp = merged_wall_footprint([main, branch])
    assert fp.geom_type == "Polygon"


# ---------------------------------------------------------------------------
# 2b) 柱內不畫牆(subtract)
# ---------------------------------------------------------------------------
def test_subtract_column_reduces_area() -> None:
    """把一根壓在牆上的柱從 footprint 減掉,面積應少掉重疊部分。"""
    wall = Wall(start=(0, 0), end=(4000, 0), thickness=200)   # y[-100,100]
    # 柱 400×400 於 (2000,0):與牆重疊 400(x)×200(y,整個牆厚)= 80000。
    col = [(1800, -200), (2200, -200), (2200, 200), (1800, 200)]
    full = merged_wall_footprint([wall]).area
    cut = merged_wall_footprint([wall], subtract=[col]).area
    assert full - cut == pytest.approx(400 * 200)


def test_subtract_column_at_corner_splits_or_notches() -> None:
    """柱壓在牆上時,牆 footprint 在柱範圍內被挖掉(該處不再有牆)。"""
    from shapely.geometry import Point as SPoint

    wall = Wall(start=(0, 0), end=(4000, 0), thickness=200)
    col = [(1800, -200), (2200, -200), (2200, 200), (1800, 200)]
    fp = merged_wall_footprint([wall], subtract=[col])
    # 柱中心點(2000,0)原本在牆內,挖掉後不應再被 footprint 覆蓋。
    assert not fp.covers(SPoint(2000, 0))
    # 柱範圍外的牆(例如 x=500)仍在。
    assert fp.covers(SPoint(500, 0))


# ---------------------------------------------------------------------------
# 3) 空輸入
# ---------------------------------------------------------------------------
def test_empty_walls_returns_empty() -> None:
    assert merged_wall_footprint([]).is_empty


def test_all_opening_wall_returns_empty() -> None:
    """整道牆都是洞口 → 沒有實牆 → 空。"""
    wall = Wall((0, 0), (1000, 0), thickness=200, openings=[Opening(500, 2000)])
    assert merged_wall_footprint([wall]).is_empty


# ---------------------------------------------------------------------------
# 4) 畫圖
# ---------------------------------------------------------------------------
def test_draw_walls_joined_on_layer(doc_and_layers) -> None:
    doc, layers = doc_and_layers
    msp = doc.modelspace()

    walls = [
        Wall((0, 0), (4000, 0), thickness=200),
        Wall((4000, 0), (4000, 3000), thickness=200),
    ]
    draw_walls_joined(msp, walls, layers["A-WALL"])

    polys = list(msp.query("LWPOLYLINE"))
    # L 接角聯集成單一多邊形、無內孔 → 只畫一條封閉外環。
    assert len(polys) == 1
    assert polys[0].dxf.layer == layers["A-WALL"]
    assert polys[0].closed is True


def test_closed_ring_draws_outer_and_inner(doc_and_layers) -> None:
    """封閉一圈牆 → 畫外環 + 一個內孔 = 2 條多義線。"""
    doc, layers = doc_and_layers
    msp = doc.modelspace()
    t = 200
    walls = [
        Wall((0, 0), (4000, 0), thickness=t),
        Wall((4000, 0), (4000, 4000), thickness=t),
        Wall((4000, 4000), (0, 4000), thickness=t),
        Wall((0, 4000), (0, 0), thickness=t),
    ]
    draw_walls_joined(msp, walls, layers["A-WALL"])
    assert len(list(msp.query("LWPOLYLINE"))) == 2


def test_draw_walls_joined_with_prefix() -> None:
    standard = load_standard()
    doc = new_document()
    layers = apply_standard(doc, standard, prefix="2F建築底圖")
    msp = doc.modelspace()

    walls = [Wall((0, 0), (4000, 0), thickness=200), Wall((4000, 0), (4000, 3000), thickness=200)]
    draw_walls_joined(msp, walls, layers["A-WALL"])
    # A-WALL 經別名對應到規範圖層 WALL。
    for poly in msp.query("LWPOLYLINE"):
        assert poly.dxf.layer == "2F建築底圖$0$WALL"
