"""樓梯(Stair)的單元測試。

驗證重點:
  1. 資料模型:方向/踏步數/放不下的檢查;局部→世界座標(四個方向)。
  2. draw_stair:踏步線數量與間距、折斷線後的踏步用 HIDDEN 虛線、
     折斷線(鋸齒多義線)、方向箭頭、「上/下」文字;各掛正確圖層。
  3. 接進 FloorPlanSpec:spec.stairs 能畫、不再報 NotImplementedError。
"""
from __future__ import annotations

import pytest

from src.drafting.stair import Stair, UStair, draw_stair, draw_u_stair
from src.standards.loader import apply_standard, load_standard, new_document


@pytest.fixture()
def doc_and_layers():
    standard = load_standard()
    doc = new_document()
    layers = apply_standard(doc, standard)
    return doc, layers


def _demo_stair(**overrides) -> Stair:
    base = dict(origin=(0, 0), width=1200, length=2700,
                direction="north", steps=10, tread=260)
    base.update(overrides)
    return Stair(**base)


# ---------------------------------------------------------------------------
# 1) 資料模型
# ---------------------------------------------------------------------------
def test_flight_length() -> None:
    assert _demo_stair().flight_length == 2600


def test_flight_too_long_raises() -> None:
    with pytest.raises(ValueError):
        _demo_stair(steps=11)   # 11×260=2860 > 2700


def test_invalid_direction_raises() -> None:
    with pytest.raises(ValueError):
        _demo_stair(direction="up")


def test_too_few_steps_raises() -> None:
    with pytest.raises(ValueError):
        _demo_stair(steps=1)


@pytest.mark.parametrize("direction, expect", [
    ("north", (100, 500)),     # 起步端在南:s 沿 +Y
    ("south", (100, 2200)),    # 起步端在北:s 沿 -Y(length=2700)
    ("east", (500, 100)),      # 起步端在西:s 沿 +X
    ("west", (2200, 100)),     # 起步端在東:s 沿 -X
])
def test_to_world_directions(direction, expect) -> None:
    stair = _demo_stair(direction=direction)
    assert stair.to_world(100, 500) == pytest.approx(expect)


# ---------------------------------------------------------------------------
# 2) 畫圖
# ---------------------------------------------------------------------------
def test_draw_stair_riser_count_and_layers(doc_and_layers) -> None:
    doc, layers = doc_and_layers
    msp = doc.modelspace()
    draw_stair(msp, _demo_stair(), layers)

    lines = [e for e in msp.query("LINE")]
    # 踏步線 10 + 箭桿 1 + 箭頭兩撇 2 + 中央扶手兩線 2 = 15。
    assert len(lines) == 15
    for ln in lines:
        assert ln.dxf.layer == layers["HANDRAIL"]


def test_draw_stair_hidden_beyond_break(doc_and_layers) -> None:
    """折斷線(60% 處 = s 1560)之後的踏步要用 HIDDEN 虛線。"""
    doc, layers = doc_and_layers
    msp = doc.modelspace()
    draw_stair(msp, _demo_stair(), layers)

    # 踏步線 = 水平線(direction north)且長度 = 梯寬 1200。
    risers = [e for e in msp.query("LINE")
              if abs(e.dxf.start.y - e.dxf.end.y) < 1e-6
              and abs(abs(e.dxf.end.x - e.dxf.start.x) - 1200) < 1e-6]
    assert len(risers) == 10
    solid = [r for r in risers if r.dxf.linetype != "HIDDEN"]
    hidden = [r for r in risers if r.dxf.linetype == "HIDDEN"]
    # i=1..6(s=260..1560)實線;i=7..10(s=1820..2600)虛線。
    assert len(solid) == 6
    assert len(hidden) == 4
    assert max(r.dxf.start.y for r in solid) == pytest.approx(1560)
    assert min(r.dxf.start.y for r in hidden) == pytest.approx(1820)


def test_draw_stair_riser_spacing(doc_and_layers) -> None:
    doc, layers = doc_and_layers
    msp = doc.modelspace()
    draw_stair(msp, _demo_stair(), layers)

    ys = sorted({round(e.dxf.start.y) for e in msp.query("LINE")
                 if abs(e.dxf.start.y - e.dxf.end.y) < 1e-6
                 and abs(abs(e.dxf.end.x - e.dxf.start.x) - 1200) < 1e-6})
    assert ys == [260 * i for i in range(1, 11)]


def test_draw_stair_break_line_polyline(doc_and_layers) -> None:
    doc, layers = doc_and_layers
    msp = doc.modelspace()
    draw_stair(msp, _demo_stair(), layers)

    polys = list(msp.query("LWPOLYLINE"))
    # 折斷線(6 點)+ 中央扶手立柱 4 個小方塊(各 4 點)。
    break_lines = [p for p in polys if len(p) == 6]
    posts = [p for p in polys if len(p) == 4]
    assert len(break_lines) == 1                  # 折斷線
    assert break_lines[0].dxf.layer == layers["HANDRAIL"]
    assert len(posts) == 4                        # 兩扶手線 × 上下端 = 4 立柱
    for p in posts:
        assert p.dxf.layer == layers["HANDRAIL"]


def test_draw_stair_center_handrail(doc_and_layers) -> None:
    """中央扶手:兩條縱向平行線(對稱於中心 t=600),長度 = 第一階到梯段頂。"""
    doc, layers = doc_and_layers
    msp = doc.modelspace()
    draw_stair(msp, _demo_stair(), layers)   # width=1200 → 中心 x=600;flight=2600

    # 扶手線 = 縱向(北向:x 固定、y 變動),x 在中心 600 ± 60。
    rails = [e for e in msp.query("LINE")
             if abs(e.dxf.start.x - e.dxf.end.x) < 1e-6
             and abs(e.dxf.start.x - 600) == pytest.approx(60)]
    assert len(rails) == 2
    for r in rails:
        assert r.dxf.layer == layers["HANDRAIL"]
        lo, hi = sorted((r.dxf.start.y, r.dxf.end.y))
        assert lo == pytest.approx(260)          # 第一階(tread)
        assert hi == pytest.approx(2600)         # 梯段頂(flight_length)


def test_draw_stair_label_text(doc_and_layers) -> None:
    doc, layers = doc_and_layers
    msp = doc.modelspace()
    draw_stair(msp, _demo_stair(label="下"), layers)

    texts = list(msp.query("TEXT"))
    assert len(texts) == 1
    assert texts[0].dxf.text == "下"
    assert texts[0].dxf.layer == layers["A-TEXT"]


def test_draw_stair_east_direction_risers_vertical(doc_and_layers) -> None:
    """東向樓梯:踏步線應為垂直線(垂直於行進方向)。"""
    doc, layers = doc_and_layers
    msp = doc.modelspace()
    draw_stair(msp, _demo_stair(direction="east"), layers)

    risers = [e for e in msp.query("LINE")
              if abs(e.dxf.start.x - e.dxf.end.x) < 1e-6
              and abs(abs(e.dxf.end.y - e.dxf.start.y) - 1200) < 1e-6]
    assert len(risers) == 10


# ---------------------------------------------------------------------------
# 2b) 折返梯(UStair)
# ---------------------------------------------------------------------------
def _demo_ustair(**overrides) -> UStair:
    # 總寬 2500 = 梯段 1200×2 + 梯井 100;9 級×260 = 2340,平台 = 3200-2340 = 860。
    base = dict(origin=(0, 0), width=2500, length=3200,
                direction="north", steps_per_flight=9, tread=260, well_gap=100)
    base.update(overrides)
    return UStair(**base)


def test_ustair_derived_dimensions() -> None:
    u = _demo_ustair()
    assert u.flight_width == 1200
    assert u.flight_run == 2340
    assert u.landing_depth == pytest.approx(860)


def test_ustair_landing_too_small_raises() -> None:
    with pytest.raises(ValueError):
        _demo_ustair(length=2600)   # 平台只剩 260 < 600


def test_ustair_flight_too_narrow_raises() -> None:
    with pytest.raises(ValueError):
        _demo_ustair(width=1200)    # 梯段寬 (1200-100)/2 = 550 < 600


def test_draw_u_stair_line_counts(doc_and_layers) -> None:
    doc, layers = doc_and_layers
    msp = doc.modelspace()
    draw_u_stair(msp, _demo_ustair(), layers)

    lines = list(msp.query("LINE"))
    # 起步梯段 9 + 折返梯段 9 + 梯井 2 + 平台邊 1 + 箭桿 1 + 箭頭 2 = 24。
    assert len(lines) == 24
    for ln in lines:
        assert ln.dxf.layer == layers["HANDRAIL"]


def test_draw_u_stair_return_flight_all_hidden(doc_and_layers) -> None:
    """折返梯段(左側,剖切面以上)的踏步應全部為 HIDDEN 虛線。"""
    doc, layers = doc_and_layers
    msp = doc.modelspace()
    draw_u_stair(msp, _demo_ustair(), layers)

    # 折返梯段踏步 = 水平線、跨 t 0..1200(x 0..1200)。
    return_risers = [e for e in msp.query("LINE")
                     if abs(e.dxf.start.y - e.dxf.end.y) < 1e-6
                     and min(e.dxf.start.x, e.dxf.end.x) == pytest.approx(0)
                     and abs(abs(e.dxf.end.x - e.dxf.start.x) - 1200) < 1e-6]
    assert len(return_risers) == 9
    assert all(r.dxf.linetype == "HIDDEN" for r in return_risers)


def test_draw_u_stair_entry_flight_break_split(doc_and_layers) -> None:
    """起步梯段(右側):折斷線(60% = s1404)前實線 5 條、後虛線 4 條。"""
    doc, layers = doc_and_layers
    msp = doc.modelspace()
    draw_u_stair(msp, _demo_ustair(), layers)

    entry_risers = [e for e in msp.query("LINE")
                    if abs(e.dxf.start.y - e.dxf.end.y) < 1e-6
                    and min(e.dxf.start.x, e.dxf.end.x) == pytest.approx(1300)]
    assert len(entry_risers) == 9
    solid = [r for r in entry_risers if r.dxf.linetype != "HIDDEN"]
    hidden = [r for r in entry_risers if r.dxf.linetype == "HIDDEN"]
    assert len(solid) == 5     # s = 260..1300
    assert len(hidden) == 4    # s = 1560..2340


def test_draw_u_stair_well_lines(doc_and_layers) -> None:
    """梯井線:t=1200 與 t=1300 兩條縱線,長度 = 梯段水平長 2340。"""
    doc, layers = doc_and_layers
    msp = doc.modelspace()
    draw_u_stair(msp, _demo_ustair(), layers)

    wells = [e for e in msp.query("LINE")
             if abs(e.dxf.start.x - e.dxf.end.x) < 1e-6
             and e.dxf.start.x in (1200.0, 1300.0)
             and abs(abs(e.dxf.end.y - e.dxf.start.y) - 2340) < 1e-6]
    assert len(wells) == 2


def test_draw_u_stair_label(doc_and_layers) -> None:
    doc, layers = doc_and_layers
    msp = doc.modelspace()
    draw_u_stair(msp, _demo_ustair(label="上18"), layers)
    texts = list(msp.query("TEXT"))
    assert len(texts) == 1
    assert texts[0].dxf.text == "上18"


# ---------------------------------------------------------------------------
# 3) FloorPlanSpec 整合
# ---------------------------------------------------------------------------
def test_floor_plan_spec_draws_stairs(doc_and_layers) -> None:
    from src.drafting.apartment_plan import demo_spec, draw_floor_plan

    doc, layers = doc_and_layers
    msp = doc.modelspace()
    spec = demo_spec()
    assert len(spec.stairs) == 1                 # demo 戶型有一座樓梯
    draw_floor_plan(msp, spec, layers)           # 不應報 NotImplementedError

    handrail = [e for e in msp if e.dxf.layer == layers["HANDRAIL"]]
    assert len(handrail) >= 13                   # 9 踏步 + 3 箭頭 + 折斷線
