"""標準單元重複組合(unit)的單元測試。

驗證重點:
  1. 座標變換:平移、mirror_x、mirror_y、鏡射後家具旋轉角。
  2. place_unit:數量/索引偏移正確、洞口距離不變、奇數次鏡射翻門向、
     流理台交換起訖點(檯面側不跑掉)。
  3. 雙走廊示範:8 戶展開、面積閉合(單元×8+走廊=建築)、端到端出圖。
"""
from __future__ import annotations

import pytest

from src.drafting.apartment_plan import FloorPlanSpec, draw_floor_plan
from src.drafting.fixtures import Counter, FixturePlacement
from src.drafting.room import Room
from src.drafting.unit import (
    UnitSpec,
    _t_point,
    _t_rotation,
    demo_corridor_spec,
    one_room_unit,
    place_unit,
)
from src.drafting.wall import Opening, Wall
from src.standards.loader import apply_standard, load_standard, new_document


def _empty_spec() -> FloorPlanSpec:
    return FloorPlanSpec(
        site_boundary=[(0, 0), (30000, 0), (30000, 20000), (0, 20000)],
        setback=2000, x_spacings=[4000], y_spacings=[6000],
        grid_origin=(2000, 2000),
    )


# ---------------------------------------------------------------------------
# 1) 座標變換
# ---------------------------------------------------------------------------
def test_t_point_translate_and_mirrors() -> None:
    u = one_room_unit()   # 4000×6000
    assert _t_point(u, (100, 200), False, False, (1000, 2000)) == (1100, 2200)
    assert _t_point(u, (0, 0), True, False, (1000, 2000)) == (3000, 2000)
    assert _t_point(u, (0, 0), False, True, (1000, 2000)) == (1000, 4000)


@pytest.mark.parametrize("mx, my, r, expect", [
    (False, False, 90, 90),
    (True, False, 90, 270),    # mirror_x:r → -r
    (False, True, 90, 90),     # mirror_y:r → 180-r
    (False, True, 180, 0),     # 床頭朝北 → 鏡射後朝南
    (True, True, 90, 270 - 360 + 360),  # 雙鏡射 = 旋轉180:90→270
])
def test_t_rotation(mx, my, r, expect) -> None:
    assert _t_rotation(mx, my, r) == expect % 360


# ---------------------------------------------------------------------------
# 2) place_unit
# ---------------------------------------------------------------------------
def test_place_unit_counts_and_offsets() -> None:
    spec = _empty_spec()
    spec.walls.append(Wall((0, 0), (100, 0)))     # 先塞一道牆,驗索引偏移
    unit = one_room_unit()
    place_unit(spec, unit, origin=(2000, 9800))

    assert len(spec.walls) == 1 + 5
    assert len(spec.doors) == 2
    assert len(spec.windows) == 1
    assert len(spec.rooms) == 2
    assert len(spec.fixtures) == 4
    # 索引偏移:入口門原指單元牆 0 → 現在指 spec.walls[1]。
    assert spec.doors[0].wall_index == 1
    wall = spec.walls[spec.doors[0].wall_index]
    assert wall.start == (2000, 9800)             # 南牆平移正確
    assert wall.openings[0].position == 2000      # 洞口距離不變


def test_place_unit_mirror_y_geometry_and_swing() -> None:
    spec = _empty_spec()
    unit = one_room_unit()
    place_unit(spec, unit, origin=(2000, 2000), mirror_y=True)

    # 南牆(局部 y=0)鏡射後在單元頂 y=8000;北牆(y=6000)落到 y=2000。
    south = spec.walls[0]
    north = spec.walls[1]
    assert south.start[1] == south.end[1] == 8000
    assert north.start[1] == north.end[1] == 2000
    # 奇數次鏡射:門向翻轉(out→in),hinge 不變。
    assert spec.doors[0].door.swing == "in"
    assert spec.doors[0].door.hinge == "left"
    # 家具:床原 rot 180(床頭朝北)→ 0(床頭朝南,靠世界南牆)。
    bed = next(f for f in spec.fixtures if f.name == "bed_double")
    assert bed.rotation == 0
    assert bed.insert == (2000 + 2900, 2000 + 75)   # y: 6000-5925=75


def test_place_unit_mirror_x_room_flips() -> None:
    spec = _empty_spec()
    unit = one_room_unit()
    place_unit(spec, unit, origin=(0, 0), mirror_x=True)
    # 浴廁原在西南角(x 0..1800)→ 鏡射後在東南角(x 2200..4000)。
    bath = next(r for r in spec.rooms if r.name == "浴廁")
    xs = [p[0] for p in bath.points]
    assert (min(xs), max(xs)) == (2200, 4000)
    assert bath.area_m2 == pytest.approx(3.6)       # 面積不變


def test_place_unit_counter_swaps_on_mirror() -> None:
    """流理台在奇數次鏡射時交換起訖點,檯面仍伸向房內。"""
    unit = UnitSpec(name="t", width=4000, depth=6000,
                    fixtures=[Counter(start=(3940, 500), end=(3940, 2500))])
    spec = _empty_spec()
    place_unit(spec, unit, origin=(0, 0), mirror_x=True)

    c = spec.fixtures[0]
    # 鏡射後貼西側 x=60;起訖交換 → 方向改為 -Y,左手側 = +X(往房內)。
    assert (c.start, c.end) == ((60, 2500), (60, 500))


def test_same_unit_reusable() -> None:
    """同一 UnitSpec 展開兩次,兩份牆是獨立物件(改一份不影響另一份)。"""
    spec = _empty_spec()
    unit = one_room_unit()
    place_unit(spec, unit, origin=(0, 0))
    place_unit(spec, unit, origin=(4000, 0))
    spec.walls[0].openings.append(Opening(3000, 500, "window"))
    assert len(spec.walls[5].openings) == 1         # 第二份不受影響
    assert len(unit.walls[0].openings) == 1         # 原型也不受影響


# ---------------------------------------------------------------------------
# 3) 雙走廊示範
# ---------------------------------------------------------------------------
def test_demo_corridor_counts() -> None:
    spec = demo_corridor_spec()
    assert len([r for r in spec.rooms if r.name == "1房型"]) == 8
    assert len([r for r in spec.rooms if r.name == "走廊"]) == 1
    assert len(spec.walls) == 2 + 8 * 5
    assert len(spec.doors) == 16
    assert len(spec.windows) == 8
    assert len(spec.fixtures) == 32


def test_demo_corridor_area_closure() -> None:
    """單元×8 + 走廊 = 建築範圍(牆中心線面積閉合)。"""
    spec = demo_corridor_spec()
    total = sum(r.area_m2 for r in spec.rooms)
    building = 16.0 * 13.8       # 16m × (6+1.8+6)m
    assert total == pytest.approx(building)


def test_demo_corridor_draws_end_to_end() -> None:
    std = load_standard()
    doc = new_document()
    layers = apply_standard(doc, std)
    draw_floor_plan(doc.modelspace(), demo_corridor_spec(), layers)
    msp = doc.modelspace()
    assert len(list(msp.query("INSERT"))) > 40      # 門窗+家具+標題欄+北箭頭
    assert len(list(msp.query("DIMENSION"))) > 20   # 尺寸鏈