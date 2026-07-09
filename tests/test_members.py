"""結構構件產生器(柱/梁)的單元測試。

驗證重點:
  1. Beam 的長度/中點/單位向量/法向量算得對(含水平、垂直、斜向三種情況)。
  2. 畫到 modelspace 後,柱/梁的圖層、幾何範圍、標籤文字都正確。
"""
from __future__ import annotations

import math

import pytest

from src.drafting.members import (
    Beam,
    Column,
    beam_label_text,
    draw_beam,
    draw_beam_label,
    draw_column,
)
from src.standards.loader import apply_standard, load_standard, new_document


# ---------------------------------------------------------------------------
# 1) Beam 的幾何性質(純計算,不碰 ezdxf)
# ---------------------------------------------------------------------------
def test_beam_length_horizontal() -> None:
    beam = Beam(start=(0, 0), end=(6000, 0), width=240, depth=235)
    assert beam.length == 6000


def test_beam_length_diagonal() -> None:
    beam = Beam(start=(0, 0), end=(3000, 4000), width=240, depth=235)
    assert beam.length == 5000  # 3-4-5 三角形 * 1000


def test_beam_midpoint() -> None:
    beam = Beam(start=(0, 0), end=(6000, 2000), width=240, depth=235)
    assert beam.midpoint == (3000, 1000)


def test_beam_unit_vector_horizontal() -> None:
    beam = Beam(start=(0, 0), end=(6000, 0), width=240, depth=235)
    assert beam.unit_vector == pytest.approx((1, 0))


def test_beam_unit_vector_vertical() -> None:
    beam = Beam(start=(0, 0), end=(0, 6000), width=240, depth=235)
    assert beam.unit_vector == pytest.approx((0, 1))


def test_beam_normal_vector_horizontal() -> None:
    """水平梁的法向量應指向 +Y(上方),跟 hello_cad.py 舊版的標籤位置慣例一致。"""
    beam = Beam(start=(0, 0), end=(6000, 0), width=240, depth=235)
    assert beam.normal_vector == pytest.approx((0, 1))


def test_beam_zero_length_raises() -> None:
    beam = Beam(start=(0, 0), end=(0, 0), width=240, depth=235)
    with pytest.raises(ValueError):
        _ = beam.unit_vector


def test_beam_label_text_default_format() -> None:
    beam = Beam(start=(0, 0), end=(6000, 0), width=240, depth=235)
    assert beam_label_text(beam) == "240×235"


def test_beam_label_text_custom_format() -> None:
    beam = Beam(start=(0, 0), end=(6000, 0), width=240, depth=235)
    assert beam_label_text(beam, fmt="{width}x{depth}mm") == "240x235mm"


# ---------------------------------------------------------------------------
# 2) 畫到 modelspace
# ---------------------------------------------------------------------------
@pytest.fixture()
def doc_and_layers():
    standard = load_standard()
    doc = new_document()
    layers = apply_standard(doc, standard)
    return doc, layers, standard


def test_draw_column_creates_polyline_on_correct_layer(doc_and_layers) -> None:
    doc, layers, _ = doc_and_layers
    msp = doc.modelspace()

    draw_column(msp, Column(center=(0, 0), width=500, depth=500), layers["COLUMN"])

    polylines = list(msp.query("LWPOLYLINE"))
    assert len(polylines) == 1
    poly = polylines[0]
    assert poly.dxf.layer == layers["COLUMN"]
    assert poly.closed is True

    xs = [p[0] for p in poly.get_points()]
    ys = [p[1] for p in poly.get_points()]
    assert min(xs) == -250 and max(xs) == 250
    assert min(ys) == -250 and max(ys) == 250


def test_draw_beam_horizontal_bounding_box(doc_and_layers) -> None:
    doc, layers, _ = doc_and_layers
    msp = doc.modelspace()

    beam = Beam(start=(0, 0), end=(6000, 0), width=240, depth=235)
    draw_beam(msp, beam, layers["S-RCBMB"])

    poly = list(msp.query("LWPOLYLINE"))[0]
    assert poly.dxf.layer == layers["S-RCBMB"]

    xs = [p[0] for p in poly.get_points()]
    ys = [p[1] for p in poly.get_points()]
    assert min(xs) == pytest.approx(0)
    assert max(xs) == pytest.approx(6000)
    assert min(ys) == pytest.approx(-120)
    assert max(ys) == pytest.approx(120)


def test_draw_beam_vertical_bounding_box(doc_and_layers) -> None:
    """垂直梁也要能正確畫出來(舊版 hello_cad.py 手刻邏輯只支援水平梁)。"""
    doc, layers, _ = doc_and_layers
    msp = doc.modelspace()

    beam = Beam(start=(0, 0), end=(0, 6000), width=240, depth=235)
    draw_beam(msp, beam, layers["S-RCBMB"])

    poly = list(msp.query("LWPOLYLINE"))[0]
    xs = [p[0] for p in poly.get_points()]
    ys = [p[1] for p in poly.get_points()]
    assert min(xs) == pytest.approx(-120)
    assert max(xs) == pytest.approx(120)
    assert min(ys) == pytest.approx(0)
    assert max(ys) == pytest.approx(6000)


def test_draw_beam_label_places_text_above_beam(doc_and_layers) -> None:
    doc, layers, standard = doc_and_layers
    msp = doc.modelspace()

    beam = Beam(start=(0, 0), end=(6000, 0), width=240, depth=235)
    label = beam_label_text(beam, standard.beam_section_format)
    draw_beam_label(msp, beam, label, layers["S-TEXTB"])

    texts = list(msp.query("TEXT"))
    assert len(texts) == 1
    text = texts[0]
    assert text.dxf.layer == layers["S-TEXTB"]
    assert text.dxf.text == "240×235"
    # 標籤要在梁的上方(Y 值大於梁頂邊 120),而不是壓在梁上或跑到下面。
    assert text.dxf.insert.y > 120
