"""多樓層骨架(building_generator)單元測試。

驗:generate_building 產生正確層數/標示/標高、各層共用同套軸網、各層是
獨立物件、check_column_alignment 對正常樓棟通過且能抓出人為錯位。
"""
import copy

import pytest

from src.design.building_generator import (
    BuildingBrief,
    BuildingSpec,
    FloorLevel,
    check_column_alignment,
    generate_building,
)
from src.design.layout_generator import CorridorBrief, HouseBrief, generate_floor_plan


# ── 基本產出 ──────────────────────────────────────────────────────────────
def test_floor_count_and_labels():
    b = generate_building(BuildingBrief(
        typical=CorridorBrief(units_per_row=4), floors=5))
    assert len(b.floors) == 5
    assert [f.label for f in b.floors] == ["1F", "2F", "3F", "4F", "5F"]
    assert b.levels == [1, 2, 3, 4, 5]


def test_elevations_use_floor_height():
    b = generate_building(BuildingBrief(
        typical=CorridorBrief(units_per_row=4), floors=4, floor_height=3000))
    assert [f.elevation for f in b.floors] == [0, 3000, 6000, 9000]
    assert b.total_height == 4 * 3000


def test_start_level_offsets_labels():
    b = generate_building(BuildingBrief(
        typical=HouseBrief(site_width=14000, site_depth=12000, bedrooms=2),
        floors=2, start_level=3))
    assert [f.label for f in b.floors] == ["3F", "4F"]
    # 標高仍由 0 起算(第一個產出的樓層樓板 = 基準)。
    assert [f.elevation for f in b.floors] == [0, 3200]


def test_floor_label_written_into_spec():
    b = generate_building(BuildingBrief(
        typical=CorridorBrief(units_per_row=4), floors=3))
    assert [f.spec.floor_label for f in b.floors] == ["1F", "2F", "3F"]


# ── 共用軸網(柱上下對齊的根本)────────────────────────────────────────────
def test_all_floors_share_same_grid():
    b = generate_building(BuildingBrief(
        typical=CorridorBrief(units_per_row=6), floors=4))
    ref = b.floors[0].spec
    for fl in b.floors[1:]:
        assert fl.spec.x_spacings == ref.x_spacings
        assert fl.spec.y_spacings == ref.y_spacings
        assert fl.spec.grid_origin == ref.grid_origin
        assert fl.spec.column_size == ref.column_size


def test_floors_are_independent_objects():
    """深拷貝:改一層不影響其他層(日後 1F 變化層才安全)。"""
    b = generate_building(BuildingBrief(
        typical=CorridorBrief(units_per_row=4), floors=3))
    b.floors[0].spec.rooms.pop()
    assert len(b.floors[0].spec.rooms) != len(b.floors[1].spec.rooms)
    # 牆物件也不是同一個 reference。
    assert b.floors[0].spec.walls[0] is not b.floors[1].spec.walls[0]


# ── 柱網對齊檢核 ──────────────────────────────────────────────────────────
def test_alignment_passes_for_generated_building():
    b = generate_building(BuildingBrief(
        typical=CorridorBrief(units_per_row=6), floors=5))
    assert check_column_alignment(b) == []


def test_alignment_catches_shifted_floor():
    """人為把某層軸網原點平移 → 應被檢核抓到上下不對齊。"""
    b = generate_building(BuildingBrief(
        typical=CorridorBrief(units_per_row=4), floors=3))
    ox, oy = b.floors[1].spec.grid_origin
    b.floors[1].spec.grid_origin = (ox + 800, oy)   # 2F 整體東移 0.8m
    problems = check_column_alignment(b)
    assert problems
    assert any("2F" in p for p in problems)


def test_single_floor_building_ok():
    b = generate_building(BuildingBrief(
        typical=HouseBrief(site_width=14000, site_depth=12000, bedrooms=2),
        floors=1))
    assert len(b.floors) == 1
    assert check_column_alignment(b) == []      # 無相鄰層可比 → 無問題


def test_zero_floors_rejected():
    with pytest.raises(ValueError):
        generate_building(BuildingBrief(
            typical=CorridorBrief(units_per_row=4), floors=0))


# ── 檢核不依賴產生器(可直接餵手組 BuildingSpec)────────────────────────────
def test_check_alignment_on_handmade_spec():
    base = generate_floor_plan(CorridorBrief(units_per_row=4))
    b = BuildingSpec(floors=[
        FloorLevel(1, 0, copy.deepcopy(base)),
        FloorLevel(2, 3200, copy.deepcopy(base)),
    ])
    assert check_column_alignment(b) == []
