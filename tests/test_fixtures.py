"""衛浴廚具設備+家具圖塊(fixtures)的單元測試。

驗證重點:
  1. 圖塊建立:8 種都能建、冪等、未知種類報錯、內部實體掛圖層 "0"。
  2. 放置:blockref 掛 OTHER、旋轉正確。
  3. 流理台:檯面矩形方向(左手側)、深度、水槽圓、起訖相同報錯。
  4. 生產線整合:demo 的設備家具全部畫出。
"""
from __future__ import annotations

import pytest

from src.drafting.fixtures import (
    FIXTURE_BUILDERS,
    Counter,
    FixturePlacement,
    create_fixture_block,
    draw_counter,
    place_fixture,
)
from src.standards.loader import apply_standard, load_standard, new_document


@pytest.fixture()
def doc_and_layers():
    standard = load_standard()
    doc = new_document()
    layers = apply_standard(doc, standard)
    return doc, layers


# ---------------------------------------------------------------------------
# 1) 圖塊建立
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name", sorted(FIXTURE_BUILDERS))
def test_create_each_fixture_block(doc_and_layers, name) -> None:
    doc, _ = doc_and_layers
    block_name = create_fixture_block(doc, name)
    assert block_name == f"FX_{name.upper()}"
    blk = doc.blocks.get(block_name)
    entities = list(blk)
    assert len(entities) >= 2
    # 內部實體掛圖層 "0",插入時才會繼承 blockref 的圖層。
    assert all(e.dxf.layer == "0" for e in entities)


def test_create_fixture_block_idempotent(doc_and_layers) -> None:
    doc, _ = doc_and_layers
    create_fixture_block(doc, "toilet")
    create_fixture_block(doc, "toilet")   # 不應報錯或重複
    assert "FX_TOILET" in doc.blocks


def test_unknown_fixture_raises(doc_and_layers) -> None:
    doc, _ = doc_and_layers
    with pytest.raises(ValueError):
        create_fixture_block(doc, "piano")


# ---------------------------------------------------------------------------
# 2) 放置
# ---------------------------------------------------------------------------
def test_place_fixture_layer_and_rotation(doc_and_layers) -> None:
    doc, layers = doc_and_layers
    msp = doc.modelspace()
    ref = place_fixture(msp, FixturePlacement("bed_double", (4200, 11925), 180), layers)
    assert ref.dxf.name == "FX_BED_DOUBLE"
    assert ref.dxf.layer == layers["OTHER"]
    assert ref.dxf.rotation == pytest.approx(180)
    assert tuple(ref.dxf.insert)[:2] == (4200, 11925)


def test_placed_toilet_extends_away_from_wall(doc_and_layers) -> None:
    """rotation=90(貼東牆)→ 馬桶應往 -X 伸出。"""
    doc, layers = doc_and_layers
    msp = doc.modelspace()
    ref = place_fixture(msp, FixturePlacement("toilet", (13925, 3700), 90), layers)
    xs = []
    for e in ref.virtual_entities():
        if e.dxftype() == "LWPOLYLINE":
            xs += [p[0] for p in e.get_points()]
    assert max(xs) <= 13925 + 1e-6      # 全部在牆內面以西
    assert min(xs) < 13925 - 100        # 確實往房內伸


# ---------------------------------------------------------------------------
# 3) 流理台
# ---------------------------------------------------------------------------
def test_counter_left_side_and_depth(doc_and_layers) -> None:
    """沿 +Y 的流理台:檯面往左手側(-X)伸出 depth。"""
    doc, layers = doc_and_layers
    msp = doc.modelspace()
    draw_counter(msp, Counter(start=(13925, 4560), end=(13925, 6940)), layers)

    poly = list(msp.query("LWPOLYLINE"))[0]
    assert poly.dxf.layer == layers["OTHER"]
    xs = [p[0] for p in poly.get_points()]
    assert (min(xs), max(xs)) == (13325, 13925)     # 往 -X 伸 600


def test_counter_sink_circle(doc_and_layers) -> None:
    doc, layers = doc_and_layers
    msp = doc.modelspace()
    draw_counter(msp, Counter(start=(13325, 6940), end=(11060, 6940), sink=True), layers)
    circles = list(msp.query("CIRCLE"))
    assert len(circles) == 1
    assert circles[0].dxf.radius == 180
    # 沿 -X、左手側 = -Y → 水槽在 y 6940-300 = 6640。
    assert circles[0].dxf.center.y == pytest.approx(6640)


def test_counter_zero_length_raises() -> None:
    with pytest.raises(ValueError):
        Counter(start=(0, 0), end=(0, 0))


# ---------------------------------------------------------------------------
# 4) 生產線整合
# ---------------------------------------------------------------------------
def test_floor_plan_draws_fixtures(doc_and_layers) -> None:
    from src.drafting.apartment_plan import demo_spec, draw_floor_plan

    doc, layers = doc_and_layers
    msp = doc.modelspace()
    spec = demo_spec()
    assert len(spec.fixtures) == 13
    draw_floor_plan(msp, spec, layers)

    fx_inserts = [e for e in msp.query("INSERT") if e.dxf.name.startswith("FX_")]
    assert len(fx_inserts) == 11        # 11 件圖塊(另 2 段流理台是多義線)
    assert all(e.dxf.layer == layers["OTHER"] for e in fx_inserts)
