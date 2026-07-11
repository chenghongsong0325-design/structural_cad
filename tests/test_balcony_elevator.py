"""陽台(Balcony)與電梯(Elevator)的單元測試。

驗證重點:
  1. Balcony:三邊矮牆(貼建築邊不畫)、厚度 100、四種 attach 方向;
     欄杆折線掛 HANDRAIL、「陽台」文字掛 TEXT。
  2. Elevator:四面 RC20 牆、門洞在指定面且置中;轎廂符號(內縮矩形+對角
     打叉)掛 OTHER;井太小報錯。
  3. 生產線整合:牆自動併入聯集、demo 端到端;fixtures 仍擋 NotImplementedError。
"""
from __future__ import annotations

import pytest

from src.drafting.balcony_elevator import (
    Balcony,
    Elevator,
    balcony_walls,
    draw_balcony_railing,
    draw_elevator_symbol,
    elevator_walls,
)
from src.standards.loader import apply_standard, load_standard, new_document


@pytest.fixture()
def doc_and_layers():
    standard = load_standard()
    doc = new_document()
    layers = apply_standard(doc, standard)
    return doc, layers


# ---------------------------------------------------------------------------
# 1) 陽台
# ---------------------------------------------------------------------------
def test_balcony_walls_three_sides_no_attach_edge() -> None:
    bal = Balcony(origin=(0, 0), width=2400, depth=1200, attach="north")
    walls = balcony_walls(bal)
    assert len(walls) == 3
    assert all(w.thickness == 100 for w in walls)
    # 貼北 → 不該有任何牆躺在 y=1200(北邊)上。
    for w in walls:
        assert not (w.start[1] == 1200 and w.end[1] == 1200)


@pytest.mark.parametrize("attach, missing_edge", [
    ("north", ((2400, 1200), (0, 1200))),
    ("south", ((0, 0), (2400, 0))),
    ("east", ((2400, 0), (2400, 1200))),
    ("west", ((0, 1200), (0, 0))),
])
def test_balcony_attach_sides(attach, missing_edge) -> None:
    bal = Balcony(origin=(0, 0), width=2400, depth=1200, attach=attach)
    edges = {(w.start, w.end) for w in balcony_walls(bal)}
    assert missing_edge not in edges
    assert len(edges) == 3


def test_balcony_invalid_attach_raises() -> None:
    with pytest.raises(ValueError):
        Balcony(origin=(0, 0), width=2400, depth=1200, attach="up")


def test_draw_balcony_railing(doc_and_layers) -> None:
    doc, layers = doc_and_layers
    msp = doc.modelspace()
    bal = Balcony(origin=(0, 0), width=2400, depth=1200, attach="north")
    draw_balcony_railing(msp, bal, layers)

    polys = list(msp.query("LWPOLYLINE"))
    assert len(polys) == 1
    assert polys[0].dxf.layer == layers["HANDRAIL"]
    assert len(polys[0]) == 4          # 三邊折線 = 4 個點
    texts = list(msp.query("TEXT"))
    assert texts[0].dxf.text == "陽台"
    assert texts[0].dxf.layer == layers["A-TEXT"]


# ---------------------------------------------------------------------------
# 2) 電梯
# ---------------------------------------------------------------------------
def test_elevator_walls_rc20_with_centered_door() -> None:
    elev = Elevator(origin=(0, 0), width=1400, depth=2200, door_side="east")
    walls = elevator_walls(elev)
    assert len(walls) == 4
    assert all(w.thickness == 200 for w in walls)

    with_door = [w for w in walls if w.openings]
    assert len(with_door) == 1
    door_wall = with_door[0]
    # 東面牆:(1400,0)→(1400,2200);門洞置中 = 位置 1100、寬 900。
    assert door_wall.start == (1400, 0) and door_wall.end == (1400, 2200)
    assert door_wall.openings[0].position == 1100
    assert door_wall.openings[0].width == 900


def test_elevator_invalid_door_side_raises() -> None:
    with pytest.raises(ValueError):
        Elevator(origin=(0, 0), width=1400, depth=2200, door_side="middle")


def test_draw_elevator_symbol(doc_and_layers) -> None:
    doc, layers = doc_and_layers
    msp = doc.modelspace()
    elev = Elevator(origin=(0, 0), width=1400, depth=2200)
    draw_elevator_symbol(msp, elev, layers)

    polys = list(msp.query("LWPOLYLINE"))
    lines = list(msp.query("LINE"))
    assert len(polys) == 1 and polys[0].closed
    assert len(lines) == 2             # 兩條對角線
    for e in polys + lines:
        assert e.dxf.layer == layers["OTHER"]
    # 轎廂矩形 = 內縮 200(半壁厚 100 + 淨距 100):(200,200)~(1200,2000)。
    xs = [p[0] for p in polys[0].get_points()]
    ys = [p[1] for p in polys[0].get_points()]
    assert (min(xs), max(xs)) == (200, 1200)
    assert (min(ys), max(ys)) == (200, 2000)


def test_elevator_too_small_symbol_raises(doc_and_layers) -> None:
    doc, layers = doc_and_layers
    elev = Elevator(origin=(0, 0), width=380, depth=2200)   # 內縮後寬 ≤ 0
    with pytest.raises(ValueError):
        draw_elevator_symbol(doc.modelspace(), elev, layers)


# ---------------------------------------------------------------------------
# 3) 生產線整合
# ---------------------------------------------------------------------------
def test_floor_plan_draws_elevator_and_balcony(doc_and_layers) -> None:
    from src.drafting.apartment_plan import demo_spec, draw_floor_plan

    doc, layers = doc_and_layers
    msp = doc.modelspace()
    spec = demo_spec()
    assert len(spec.elevators) == 1 and len(spec.balconies) == 1
    draw_floor_plan(msp, spec, layers)   # 不應報 NotImplementedError

    # 轎廂符號在 OTHER(對角線 2 條);欄杆折線在 HANDRAIL。
    other_lines = [e for e in msp.query("LINE") if e.dxf.layer == layers["OTHER"]]
    assert len(other_lines) == 2
    rail_polys = [e for e in msp.query("LWPOLYLINE")
                  if e.dxf.layer == layers["HANDRAIL"]]
    assert len(rail_polys) >= 2          # 樓梯折斷線 + 陽台欄杆


