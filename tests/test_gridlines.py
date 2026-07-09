"""軸網系統產生器的單元測試。

驗證重點:
  1. 編號規則(數字 / 英文)正確,包含超過 26 條軸線的英文進位。
  2. 間距列表能正確換算成累加座標。
  3. 畫到 modelspace 後,軸線與編號圈/文字確實掛在正確圖層、數量正確。
"""
from __future__ import annotations

import pytest

from src.drafting.gridlines import (
    alpha_labels,
    build_grid_system,
    draw_grid,
    draw_grid_dimensions,
    numeric_labels,
)
from src.standards.loader import apply_standard, load_standard, new_document


# ---------------------------------------------------------------------------
# 1) 編號規則
# ---------------------------------------------------------------------------
def test_numeric_labels() -> None:
    assert numeric_labels(3) == ["1", "2", "3"]


def test_alpha_labels_basic() -> None:
    assert alpha_labels(3) == ["A", "B", "C"]


def test_alpha_labels_past_z() -> None:
    labels = alpha_labels(28)
    assert labels[25] == "Z"
    assert labels[26] == "AA"
    assert labels[27] == "AB"


# ---------------------------------------------------------------------------
# 2) 間距 → 座標 → GridSystem
# ---------------------------------------------------------------------------
def test_build_grid_system_positions_and_labels() -> None:
    grid = build_grid_system(x_spacings=[6000, 6000], y_spacings=[5000, 5000, 4000])

    assert [a.position for a in grid.x_axes] == [0, 6000, 12000]
    assert [a.label for a in grid.x_axes] == ["1", "2", "3"]

    assert [a.position for a in grid.y_axes] == [0, 5000, 10000, 14000]
    assert [a.label for a in grid.y_axes] == ["A", "B", "C", "D"]


def test_build_grid_system_custom_labels() -> None:
    grid = build_grid_system(x_spacings=[6000], x_labels=["X1", "X2"])
    assert [a.label for a in grid.x_axes] == ["X1", "X2"]


def test_build_grid_system_label_count_mismatch_raises() -> None:
    with pytest.raises(ValueError):
        build_grid_system(x_spacings=[6000, 6000], x_labels=["X1"])


def test_build_grid_system_single_direction() -> None:
    """只給一個方向的間距,另一方向應為單一軸線(位置 0)的空列表,而非出錯。"""
    grid = build_grid_system(x_spacings=[6000, 6000])
    assert len(grid.x_axes) == 3
    assert grid.y_axes == []


# ---------------------------------------------------------------------------
# 3) 畫到 modelspace
# ---------------------------------------------------------------------------
@pytest.fixture()
def doc_and_layers():
    standard = load_standard()
    doc = new_document()
    layers = apply_standard(doc, standard)
    return doc, layers


def test_draw_grid_entity_counts(doc_and_layers) -> None:
    doc, layers = doc_and_layers
    msp = doc.modelspace()

    grid = build_grid_system(x_spacings=[6000, 6000], y_spacings=[5000, 5000])
    draw_grid(msp, grid, layers)

    lines = list(msp.query("LINE"))
    circles = list(msp.query("CIRCLE"))
    texts = list(msp.query("TEXT"))

    # 3 條 X 軸線 + 3 條 Y 軸線 = 6 條線;每條軸線各一個圈+一個文字。
    assert len(lines) == 6
    assert len(circles) == 6
    assert len(texts) == 6


def test_draw_grid_entities_on_correct_layers(doc_and_layers) -> None:
    doc, layers = doc_and_layers
    msp = doc.modelspace()

    grid = build_grid_system(x_spacings=[6000], y_spacings=[5000])
    draw_grid(msp, grid, layers)

    for line in msp.query("LINE"):
        assert line.dxf.layer == layers["AXIS"]
    for circle in msp.query("CIRCLE"):
        assert circle.dxf.layer == layers["AXIS"]
        assert circle.dxf.linetype == "CONTINUOUS"
    for text in msp.query("TEXT"):
        assert text.dxf.layer == layers["S-TEXTB"]


def test_draw_grid_labels_present_in_text(doc_and_layers) -> None:
    doc, layers = doc_and_layers
    msp = doc.modelspace()

    grid = build_grid_system(x_spacings=[6000, 6000], y_spacings=[5000])
    draw_grid(msp, grid, layers)

    texts = {t.dxf.text for t in msp.query("TEXT")}
    assert texts == {"1", "2", "3", "A", "B"}


def test_draw_grid_with_floor_prefix(doc_and_layers) -> None:
    """跟 P1a 的樓層前綴系統一起用也要正確。"""
    standard = load_standard()
    doc = new_document()
    layers = apply_standard(doc, standard, prefix="2F建築底圖")
    msp = doc.modelspace()

    grid = build_grid_system(x_spacings=[6000], y_spacings=[5000])
    draw_grid(msp, grid, layers)

    for line in msp.query("LINE"):
        assert line.dxf.layer == "2F建築底圖$0$AXIS"


# ---------------------------------------------------------------------------
# 4) 軸間尺寸標註
# ---------------------------------------------------------------------------
def test_draw_grid_dimensions_count(doc_and_layers) -> None:
    doc, layers = doc_and_layers
    msp = doc.modelspace()

    # 3 條 X 軸線(2 個間距)+ 2 條 Y 軸線(1 個間距)= 3 個標註實體。
    grid = build_grid_system(x_spacings=[6000, 6000], y_spacings=[5000])
    draw_grid_dimensions(msp, grid, layers)

    dims = list(msp.query("DIMENSION"))
    assert len(dims) == 3


def test_draw_grid_dimensions_on_dim_layer(doc_and_layers) -> None:
    doc, layers = doc_and_layers
    msp = doc.modelspace()

    grid = build_grid_system(x_spacings=[6000, 6000], y_spacings=[5000])
    draw_grid_dimensions(msp, grid, layers)

    for dim in msp.query("DIMENSION"):
        assert dim.dxf.layer == layers["DIM"]


def test_draw_grid_dimensions_measures_correct_spacing(doc_and_layers) -> None:
    """量出來的間距值要等於實際的軸距,而不是隨便一個數字。"""
    doc, layers = doc_and_layers
    msp = doc.modelspace()

    grid = build_grid_system(x_spacings=[6000, 4500])
    draw_grid_dimensions(msp, grid, layers)

    measurements = sorted(dim.get_measurement() for dim in msp.query("DIMENSION"))
    assert measurements == [4500, 6000]


def test_draw_grid_dimensions_skips_single_axis_direction(doc_and_layers) -> None:
    """只有一條軸線的方向沒有「間距」可標,應該直接略過、不出錯。"""
    doc, layers = doc_and_layers
    msp = doc.modelspace()

    grid = build_grid_system(x_spacings=[6000])  # y_spacings=None → 無 Y 軸線
    draw_grid_dimensions(msp, grid, layers)

    dims = list(msp.query("DIMENSION"))
    assert len(dims) == 1
