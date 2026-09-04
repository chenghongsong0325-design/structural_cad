"""樓梯(UStair 折返梯)的單元測試。

⚠️ **全專案只有折返梯一種梯型**(使用者 2026-09-04:「樓梯只要做折返梯就好,
其他樓梯幫我移除」)。以前這支還測一個單跑直梯 `Stair` —— 連同它的畫圖函式
`draw_stair`、中央扶手都拿掉了,對應的測試也一併刪除(不是註解掉)。
`test_the_straight_stair_is_gone` 釘住這件事,免得日後誰又把它撿回來。

驗證重點:
  1. 資料模型:方向/踏步數/平台放不下的檢查;局部→世界座標(四個方向)。
  2. draw_u_stair:兩梯段的踏步線、折斷線前後的實線/虛線、梯井線、平台邊、
     方向箭頭與「UP N」文字;各掛正確圖層。
  3. 接進 FloorPlanSpec:spec.stairs 能畫、不再報 NotImplementedError。
"""
from __future__ import annotations

import pytest

from src.drafting.stair import UStair, draw_u_stair
from src.standards.loader import apply_standard, load_standard, new_document


@pytest.fixture()
def doc_and_layers():
    standard = load_standard()
    doc = new_document()
    layers = apply_standard(doc, standard)
    return doc, layers


# ---------------------------------------------------------------------------
# 0) 梯型只剩一種
# ---------------------------------------------------------------------------
def test_the_straight_stair_is_gone() -> None:
    """★ 使用者 2026-09-04:「樓梯只要做折返梯就好,其他樓梯幫我移除」。

    釘的是**沒有第二種梯型可用**,不是「產線剛好沒用到」—— 只要 `Stair` 還在,
    下一個為了擠出走道而卡住的人就會再把它接回去(那正是它 2026-08-27 誕生的
    原因)。要新增梯型請另開類別,不要復活這個。"""
    import src.drafting.stair as mod

    assert not hasattr(mod, "Stair")
    assert not hasattr(mod, "draw_stair")


# ---------------------------------------------------------------------------
# 2b) 折返梯(UStair)
# ---------------------------------------------------------------------------
def _demo_ustair(**overrides) -> UStair:
    # 總寬 2500 = 梯段 1200×2 + 梯井 100;9 級×260 = 2340,平台 = 3200-2340 = 860。
    base = dict(origin=(0, 0), width=2500, length=3200,
                direction="north", steps_per_flight=9, tread=260, well_gap=100)
    base.update(overrides)
    return UStair(**base)


def test_invalid_direction_raises() -> None:
    with pytest.raises(ValueError):
        _demo_ustair(direction="up")


def test_too_few_steps_raises() -> None:
    with pytest.raises(ValueError):
        _demo_ustair(steps_per_flight=1)


@pytest.mark.parametrize("direction, expect", [
    ("north", (100, 500)),     # 起步端在南:s 沿 +Y
    ("south", (100, 2700)),    # 起步端在北:s 沿 -Y(length=3200)
    ("east", (500, 100)),      # 起步端在西:s 沿 +X
    ("west", (2700, 100)),     # 起步端在東:s 沿 -X
])
def test_to_world_directions(direction, expect) -> None:
    """局部座標(t 橫向、s 沿行進方向)→ 世界座標,四個方向都要對。

    ⚠️ 這條原本釘在單跑直梯上,`_to_world` 是兩種梯型共用的 —— 直梯拿掉之後
    改釘在折返梯上,少了它這支函式就沒有人守。"""
    assert _demo_ustair(direction=direction).to_world(100, 500) == pytest.approx(expect)


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
    draw_u_stair(msp, _demo_ustair(label="上"), layers)
    texts = list(msp.query("TEXT"))
    assert len(texts) == 1
    # 上行要標**級數**(參考圖寫「UP 16」);級數由樓梯自己算,不靠呼叫端寫進 label。
    assert texts[0].dxf.text == "UP 18"


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
