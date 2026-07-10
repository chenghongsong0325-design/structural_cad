"""建築牆產生器的單元測試。

驗證重點:
  1. Wall 幾何(長度/單位向量/法向量/point_at)在水平、垂直、斜向都算得對。
  2. solid_segments:洞口把牆切成正確的實牆分段(含洞口在中間/端點/超界/蓋滿)。
  3. draw_wall:雙線牆的分段數、厚度、圖層正確;洞口處確實斷開。
"""
from __future__ import annotations

import math

import pytest

from src.drafting.wall import (
    EXTERIOR_WALL_THICKNESS,
    INTERIOR_WALL_THICKNESS,
    Opening,
    Wall,
    draw_wall,
    solid_segments,
)
from src.standards.loader import apply_standard, load_standard, new_document


# ---------------------------------------------------------------------------
# 1) Wall 幾何(純計算)
# ---------------------------------------------------------------------------
def test_wall_length_horizontal() -> None:
    assert Wall(start=(0, 0), end=(4000, 0)).length == 4000


def test_wall_length_diagonal() -> None:
    assert Wall(start=(0, 0), end=(3000, 4000)).length == 5000


def test_wall_normal_vector_horizontal() -> None:
    assert Wall(start=(0, 0), end=(4000, 0)).normal_vector == pytest.approx((0, 1))


def test_wall_point_at() -> None:
    wall = Wall(start=(0, 0), end=(4000, 0))
    assert wall.point_at(1000) == pytest.approx((1000, 0))


def test_wall_zero_length_raises() -> None:
    with pytest.raises(ValueError):
        _ = Wall(start=(0, 0), end=(0, 0)).unit_vector


def test_default_thicknesses_differ() -> None:
    """外牆預設應比內牆厚(預設值,待確認)。"""
    assert EXTERIOR_WALL_THICKNESS > INTERIOR_WALL_THICKNESS


# ---------------------------------------------------------------------------
# 2) solid_segments:洞口切割
# ---------------------------------------------------------------------------
def test_no_openings_is_one_segment() -> None:
    assert solid_segments(4000, []) == [(0.0, 4000.0)]


def test_one_middle_opening_splits_into_two() -> None:
    # 洞口中心 2000、寬 900 → 洞口範圍 [1550, 2450]。
    assert solid_segments(4000, [Opening(2000, 900)]) == [(0.0, 1550.0), (2450.0, 4000.0)]


def test_opening_at_start_leaves_one_segment() -> None:
    # 洞口壓在起點(中心 0、寬 900 → [-450, 450],裁到 [0, 450])。
    assert solid_segments(4000, [Opening(0, 900)]) == [(450.0, 4000.0)]


def test_opening_covering_whole_wall_leaves_nothing() -> None:
    assert solid_segments(2000, [Opening(1000, 4000)]) == []


def test_two_openings_split_into_three() -> None:
    segs = solid_segments(6000, [Opening(1500, 1000), Opening(4500, 1000)])
    assert segs == [(0.0, 1000.0), (2000.0, 4000.0), (5000.0, 6000.0)]


def test_overlapping_openings_merge() -> None:
    # 兩個重疊洞口應合併成一個缺口。
    segs = solid_segments(6000, [Opening(2000, 1000), Opening(2500, 1000)])
    assert segs == [(0.0, 1500.0), (3000.0, 6000.0)]


# ---------------------------------------------------------------------------
# 3) draw_wall:畫到 modelspace
# ---------------------------------------------------------------------------
@pytest.fixture()
def doc_and_layers():
    standard = load_standard()
    doc = new_document()
    layers = apply_standard(doc, standard)
    return doc, layers


def test_a_wall_layer_exists_from_standard(doc_and_layers) -> None:
    """default.yaml 應已補上 A-WALL 圖層,且被 apply_standard 建立。"""
    _, layers = doc_and_layers
    assert "A-WALL" in layers


def test_draw_solid_wall_is_one_closed_rectangle(doc_and_layers) -> None:
    doc, layers = doc_and_layers
    msp = doc.modelspace()

    wall = Wall(start=(0, 0), end=(4000, 0), thickness=240)
    draw_wall(msp, wall, layers["A-WALL"])

    polys = list(msp.query("LWPOLYLINE"))
    assert len(polys) == 1
    poly = polys[0]
    assert poly.dxf.layer == layers["A-WALL"]
    assert poly.closed is True
    # 雙線牆:垂直牆向(Y)的範圍應等於牆厚(±120)。
    ys = [p[1] for p in poly.get_points()]
    assert min(ys) == pytest.approx(-120)
    assert max(ys) == pytest.approx(120)


def test_draw_wall_with_door_breaks_into_two(doc_and_layers) -> None:
    doc, layers = doc_and_layers
    msp = doc.modelspace()

    wall = Wall(start=(0, 0), end=(4000, 0), thickness=240, openings=[Opening(2000, 900)])
    draw_wall(msp, wall, layers["A-WALL"])

    polys = list(msp.query("LWPOLYLINE"))
    # 一個門洞 → 兩段實牆 → 兩個封閉矩形。
    assert len(polys) == 2

    # 洞口 [1550, 2450] 之間不應有任何牆體:檢查每段矩形的 X 範圍都不落在缺口內。
    for poly in polys:
        xs = [p[0] for p in poly.get_points()]
        seg_min, seg_max = min(xs), max(xs)
        # 這段牆要嘛整段在洞口左邊、要嘛整段在右邊。
        assert seg_max <= 1550 + 1e-6 or seg_min >= 2450 - 1e-6


def test_draw_vertical_wall_thickness_in_x(doc_and_layers) -> None:
    """垂直牆也要正確:牆厚應反映在 X 方向。"""
    doc, layers = doc_and_layers
    msp = doc.modelspace()

    wall = Wall(start=(0, 0), end=(0, 4000), thickness=200)
    draw_wall(msp, wall, layers["A-WALL"])

    poly = list(msp.query("LWPOLYLINE"))[0]
    xs = [p[0] for p in poly.get_points()]
    assert min(xs) == pytest.approx(-100)
    assert max(xs) == pytest.approx(100)


def test_draw_wall_with_prefix(doc_and_layers) -> None:
    standard = load_standard()
    doc = new_document()
    layers = apply_standard(doc, standard, prefix="2F建築底圖")
    msp = doc.modelspace()

    wall = Wall(start=(0, 0), end=(4000, 0))
    draw_wall(msp, wall, layers["A-WALL"])

    for poly in msp.query("LWPOLYLINE"):
        assert poly.dxf.layer == "2F建築底圖$0$A-WALL"
