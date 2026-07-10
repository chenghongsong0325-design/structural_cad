"""外圍尺寸鏈(dim_chains)的單元測試。

驗證重點:
  1. 外牆偵測(wall_side)與細部分割點(detail_points):含牆方向顛倒的邊。
  2. 標註數值 get_measurement 要等於實際尺寸——細部逐段、軸距、總長;
     且每邊細部總和 = 總長(尺寸鏈閉合)。
  3. 三層位置自動錯開(細部 1200 / 軸距 2000 / 總長 2800)。
  4. 沒洞口的邊略過細部層(避免與總長重複)。
  5. FloorPlanSpec.dim_chains=True 的生產線整合。
"""
from __future__ import annotations

import pytest

from src.drafting.apartment_plan import FloorPlanSpec, demo_spec, draw_floor_plan
from src.drafting.dim_chains import (
    building_extent,
    detail_points,
    draw_dim_chains,
    grid_points,
    wall_side,
)
from src.drafting.wall import Opening, Wall
from src.standards.loader import apply_standard, load_standard, new_document


@pytest.fixture()
def doc_and_layers():
    standard = load_standard()
    doc = new_document()
    layers = apply_standard(doc, standard)
    return doc, layers


def _measurements(msp) -> list[float]:
    return sorted(round(d.get_measurement(), 3) for d in msp.query("DIMENSION"))


# ---------------------------------------------------------------------------
# 1) 外牆偵測與分割點
# ---------------------------------------------------------------------------
def test_wall_side_detection() -> None:
    spec = demo_spec()
    extent = building_extent(spec)
    assert extent == (2000, 2000, 14000, 12000)
    sides = [wall_side(w, extent) for w in spec.walls]
    # 前四道是外牆(南/東/北/西),其餘是內牆(None)。
    assert sides[:4] == ["south", "east", "north", "west"]
    assert all(s is None for s in sides[4:])


def test_detail_points_south() -> None:
    """南牆:大門(2500..3500)+ 餐廳窗(6400..7600)→ 6 個分割點。"""
    spec = demo_spec()
    pts = detail_points(spec.walls, "south", building_extent(spec))
    assert pts == [2000, 4500, 5500, 8400, 9600, 14000]


def test_detail_points_reversed_wall() -> None:
    """北牆(起點在東、往西畫)的洞口也要正確映射成世界座標。"""
    spec = demo_spec()
    pts = detail_points(spec.walls, "north", building_extent(spec))
    # 北牆從 (14000,12000)→(2000,12000):窗@5250w1500 → x 8000..9500;
    # 窗@1750w1500 → x 11500..13000。
    assert pts == [2000, 8000, 9500, 11500, 13000, 14000]


def test_grid_points() -> None:
    spec = demo_spec()
    assert grid_points(spec, "south") == [2000, 6000, 10000, 14000]
    assert grid_points(spec, "west") == [2000, 7000, 12000]


# ---------------------------------------------------------------------------
# 2) 標註數值 = 實際尺寸
# ---------------------------------------------------------------------------
def test_total_dimension_count(doc_and_layers) -> None:
    doc, layers = doc_and_layers
    msp = doc.modelspace()
    n = draw_dim_chains(msp, demo_spec(), layers)
    # 細部 5×4邊 + 軸距 (3+3+2+2) + 總長 4 = 34。
    assert n == 34
    assert len(list(msp.query("DIMENSION"))) == 34


def test_measurements_match_reality(doc_and_layers) -> None:
    doc, layers = doc_and_layers
    msp = doc.modelspace()
    draw_dim_chains(msp, demo_spec(), layers)
    values = _measurements(msp)

    # 總長:12000(南北)×2、10000(東西)×2。
    assert values.count(12000) == 2
    assert values.count(10000) == 2
    # 軸距:4000×6(南北各3跨)、5000×4(東西各2跨)。
    assert values.count(4000) == 6
    assert values.count(5000) == 4
    # 南邊細部:2500/1000/2900/1200/4400(牆段-門-牆段-窗-牆段)。
    for v in (2500, 1000, 2900, 1200, 4400):
        assert v in values


def test_detail_chain_sums_to_overall(doc_and_layers) -> None:
    """每邊細部逐段總和 = 該邊總長(尺寸鏈閉合)。"""
    spec = demo_spec()
    extent = building_extent(spec)
    for side, overall in (("south", 12000), ("north", 12000),
                          ("west", 10000), ("east", 10000)):
        pts = detail_points(spec.walls, side, extent)
        segs = [b - a for a, b in zip(pts, pts[1:])]
        assert sum(segs) == pytest.approx(overall)


# ---------------------------------------------------------------------------
# 3) 三層錯開
# ---------------------------------------------------------------------------
def test_three_tiers_staggered_south(doc_and_layers) -> None:
    doc, layers = doc_and_layers
    msp = doc.modelspace()
    draw_dim_chains(msp, demo_spec(), layers)

    # 南邊的標註:尺寸線定義點 y < 建築南緣 2000;應恰為三個高度。
    ys = {round(d.dxf.defpoint.y) for d in msp.query("DIMENSION")
          if d.dxf.defpoint.y < 2000}
    assert ys == {2000 - 1200, 2000 - 2000, 2000 - 2800}


# ---------------------------------------------------------------------------
# 4) 沒洞口的邊 → 細部層略過
# ---------------------------------------------------------------------------
def test_side_without_openings_skips_detail(doc_and_layers) -> None:
    doc, layers = doc_and_layers
    msp = doc.modelspace()
    spec = FloorPlanSpec(
        site_boundary=[(0, 0), (12000, 0), (12000, 10000), (0, 10000)],
        setback=2000,
        x_spacings=[4000, 4000], y_spacings=[6000],
        grid_origin=(2000, 2000),
        walls=[Wall((2000, 2000), (10000, 2000),
                    openings=[Opening(4000, 1000, "door")])],  # 只有南牆有洞
    )
    n = draw_dim_chains(msp, spec, layers)
    # 細部只有南邊(3 段);軸距 2+2+1+1;總長 4 → 3+6+4 = 13。
    assert n == 13


# ---------------------------------------------------------------------------
# 5) 生產線整合
# ---------------------------------------------------------------------------
def test_floor_plan_dim_chains_integration(doc_and_layers) -> None:
    doc, layers = doc_and_layers
    msp = doc.modelspace()
    draw_floor_plan(msp, demo_spec(), layers)   # demo 已開 dim_chains=True

    dims = [e for e in msp.query("DIMENSION")]
    assert len(dims) == 34
    for d in dims:
        assert d.dxf.layer == layers["DIM"]
