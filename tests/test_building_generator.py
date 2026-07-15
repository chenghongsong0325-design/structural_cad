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
from src.design.layout_generator import (
    CorridorBrief,
    HouseBrief,
    generate_corridor_basement,
    generate_floor_plan,
)

# 透天層別分化用的基地(3 房要塞進臥室帶,基地要夠寬)。
HOUSE = dict(site_width=19000, site_depth=13000, bedrooms=3)


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


# ── D2:地下室(basements)───────────────────────────────────────────────
def test_basement_levels_labels_elevations():
    b = generate_building(BuildingBrief(
        typical=CorridorBrief(units_per_row=6), floors=2, basements=2))
    assert [f.label for f in b.floors] == ["B2F", "B1F", "1F", "2F"]
    assert [f.elevation for f in b.floors] == [-6400, -3200, 0, 3200]
    assert b.levels == [-2, -1, 1, 2]
    assert [f.spec.floor_label for f in b.floors] == ["B2F", "B1F", "1F", "2F"]


def test_corridor_basement_rooms_and_no_windows():
    spec = generate_corridor_basement(CorridorBrief(units_per_row=6))
    names = [r.name for r in spec.rooms]
    for expected in ("機車停車場", "車道坡道", "機房", "蓄水池"):
        assert expected in names
    assert sum(1 for r in spec.rooms if r.kind == "stair") == 2   # 逃生核直落
    assert len(spec.stairs) == 2 and len(spec.elevators) == 1
    # 地面下無對外窗。
    assert not any(op.kind == "window" for w in spec.walls for op in w.openings)
    # 車道口存在(無門扇的洞)。
    assert any(op.kind == "door" and op.width == 2700
               for w in spec.walls for op in w.openings)


def test_corridor_basement_alignment():
    """D2 重點:B1F 格局不同,柱位仍與標準層上下對齊(同骨架軸網)。"""
    b = generate_building(BuildingBrief(
        typical=CorridorBrief(units_per_row=6), floors=3, basements=1))
    assert check_column_alignment(b) == []
    assert b.floors[0].spec.x_spacings == b.floors[1].spec.x_spacings


# ── D2:透天層別分化(differentiated)────────────────────────────────────
def test_house_differentiated_floor_programs():
    b = generate_building(BuildingBrief(
        typical=HouseBrief(**HOUSE), floors=3, basements=1, differentiated=True))
    by_label = {f.label: f.spec for f in b.floors}
    n1 = [r.name for r in by_label["1F"].rooms]
    for expected in ("客廳", "玄關", "廚房", "餐廳"):
        assert expected in n1
    assert not any(r.kind == "bedroom" for r in by_label["1F"].rooms)  # 臥室全上樓
    beds = [r for r in by_label["2F"].rooms if r.kind == "bedroom"]
    assert len(beds) == 3
    assert not any(r.kind == "kitchen" for r in by_label["2F"].rooms)
    nb = [r.name for r in by_label["B1F"].rooms]
    assert "車庫" in nb and "機房" in nb
    assert check_column_alignment(b) == []                # 四層柱全對齊


def test_house_stairwell_stacked():
    """樓梯間每層同位、同一座梯(上下貫通);頂層樓梯標「下」。"""
    b = generate_building(BuildingBrief(
        typical=HouseBrief(**HOUSE), floors=3, basements=1, differentiated=True))
    zones = [next(r for r in f.spec.rooms if r.kind == "stair").points
             for f in b.floors]
    assert all(z == zones[0] for z in zones)
    origins = [f.spec.stairs[0].origin for f in b.floors]
    assert all(o == origins[0] for o in origins)
    assert b.floors[-1].spec.stairs[0].label == "下"      # 3F 只能往下
    assert b.floors[0].spec.stairs[0].label == "上"       # B1F 往上


def test_house_wet_stack_aligned():
    """濕區管道上下對齊:1F 衛浴、2F 衛浴、B1F 機房同一開間。"""
    b = generate_building(BuildingBrief(
        typical=HouseBrief(**HOUSE), floors=2, basements=1, differentiated=True))
    by_label = {f.label: f.spec for f in b.floors}

    def x_range(spec, name):
        r = next(r for r in spec.rooms if r.name == name)
        xs = [p[0] for p in r.points]
        return (min(xs), max(xs))

    assert (x_range(by_label["1F"], "衛浴")
            == x_range(by_label["2F"], "衛浴")
            == x_range(by_label["B1F"], "機房"))


@pytest.mark.parametrize("site", [
    HOUSE,                                                  # D2 示範基地
    dict(site_width=30000, site_depth=12000, bedrooms=2),   # 寬基地(E1 反饋)
])
def test_house_columns_hidden_in_wall_junctions(site):
    """使用者反饋 2026-07-14(兩次):柱要站在兩道豎牆的交會處,不能凸在
    房間牆段中間 → 每條中間軸線都必須有一道豎牆坐在上面(三種樓層都查)。
    寬基地案例:2 房攤在 26m 寬會生出沒牆可吸附的軸線,靠建築寬度收斂修掉。"""
    from src.design.layout_generator import (
        generate_house_basement, generate_house_public, generate_house_upper)
    brief = HouseBrief(**site)
    for spec in (generate_house_public(brief), generate_house_upper(brief),
                 generate_house_basement(brief)):
        ox = spec.grid_origin[0]
        axes = [ox]
        for s in spec.x_spacings:
            axes.append(axes[-1] + s)
        wall_xs = {w.start[0] for w in spec.walls if w.start[0] == w.end[0]}
        for a in axes[1:-1]:                     # 中間軸線(兩端在外牆上)
            assert any(abs(a - wx) < 1 for wx in wall_xs), \
                f"{spec.floor_label} 軸線 x={a} 上沒有豎牆(柱會凸在房間裡)"


def test_house_width_capped_on_wide_site():
    """建築寬度隨房數收斂:30×12m 基地做 2 房,建築不該攤滿 26m 可建寬,
    要封頂(房間全到上限的寬度)且置中,臥室寬不得爆表。"""
    from src.design.layout_generator import (
        MAX_BEDROOM_WIDTH, generate_house_upper)
    spec = generate_house_upper(
        HouseBrief(site_width=30000, site_depth=12000, bedrooms=2))
    ox = spec.grid_origin[0]
    width = sum(spec.x_spacings)
    assert width < 15000                        # 收斂了,不是 26000
    assert ox > 2000 + 1000                     # 退縮線再往內縮(置中留側院)
    assert abs((ox - 2000) - (26000 - width - (ox - 2000))) < 1   # 兩側院等寬
    beds = [r for r in spec.rooms if r.kind == "bedroom"]
    assert beds and all(
        (max(p[0] for p in r.points) - min(p[0] for p in r.points))
        <= MAX_BEDROOM_WIDTH + 1 for r in beds)


def test_house_divider_columns_tucked_off_south_band():
    """使用者反饋 2026-07-15(附 AutoCAD 截圖):分界牆上的 T 型柱不能凸進
    南側大客廳/起居室——柱南面要貼齊分界牆南皮。三種樓層都查,且各層該排
    柱心一致(上下對齊)。"""
    from src.design.layout_generator import (
        INT, generate_house_basement, generate_house_public,
        generate_house_upper)
    brief = HouseBrief(site_width=30000, site_depth=12000, bedrooms=2)
    centers_by_floor = []
    for spec in (generate_house_public(brief), generate_house_upper(brief),
                 generate_house_basement(brief)):
        assert spec.column_centers is not None
        by0 = spec.grid_origin[1]
        yd = by0 + spec.y_spacings[0]               # 分界牆 y(南帶進深)
        half = spec.column_size / 2
        divider = sorted(c for c in spec.column_centers
                         if abs(c[1] - half + INT / 2 - yd) < 1)  # 南面≈yd 的柱
        assert len(divider) == len(spec.x_spacings) + 1, \
            f"{spec.floor_label} 分界牆那排柱數不對"
        for cx, cy in divider:
            assert cy - half >= yd - INT / 2 - 1, \
                f"{spec.floor_label} 柱南面 {cy-half} 凸過分界牆南皮 {yd-INT/2}"
        centers_by_floor.append(sorted(spec.column_centers))
    assert centers_by_floor[0] == centers_by_floor[1] == centers_by_floor[2], \
        "各層柱心不一致 → 上下對不齊"


def test_house_floors_furnished():
    """使用者反饋 2026-07-14:房間不能空的——臥室要有床/衣櫃,公共層要有
    沙發/餐桌/流理台/衛浴設備(碰撞與門迴轉由 validate_spec 把關)。"""
    from src.design.layout_generator import (
        Counter, FixturePlacement, generate_house_public, generate_house_upper)
    brief = HouseBrief(**HOUSE)
    up = generate_house_upper(brief)
    names_up = [fx.name for fx in up.fixtures if isinstance(fx, FixturePlacement)]
    assert names_up.count("bed_double") == 1          # 主臥雙人床
    assert names_up.count("bed_single") == brief.bedrooms - 1
    assert names_up.count("wardrobe") == brief.bedrooms
    assert "toilet" in names_up and "basin" in names_up
    pub = generate_house_public(brief)
    names_pub = [fx.name for fx in pub.fixtures if isinstance(fx, FixturePlacement)]
    assert "sofa3" in names_pub and "table4" in names_pub
    assert any(isinstance(fx, Counter) for fx in pub.fixtures)   # 廚房流理台


def test_differentiated_requires_house():
    with pytest.raises(ValueError):
        generate_building(BuildingBrief(
            typical=CorridorBrief(units_per_row=4), floors=3, differentiated=True))


def test_house_basement_requires_differentiated():
    with pytest.raises(ValueError):
        generate_building(BuildingBrief(
            typical=HouseBrief(**HOUSE), floors=2, basements=1))


def test_negative_basements_rejected():
    with pytest.raises(ValueError):
        generate_building(BuildingBrief(
            typical=CorridorBrief(units_per_row=4), floors=2, basements=-1))


# ── 檢核不依賴產生器(可直接餵手組 BuildingSpec)────────────────────────────
def test_check_alignment_on_handmade_spec():
    base = generate_floor_plan(CorridorBrief(units_per_row=4))
    b = BuildingSpec(floors=[
        FloorLevel(1, 0, copy.deepcopy(base)),
        FloorLevel(2, 3200, copy.deepcopy(base)),
    ])
    assert check_column_alignment(b) == []
