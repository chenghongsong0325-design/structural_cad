"""柱斷面概算(column_design)的單元測試。

驗證重點:
  1. 負擔面積:角柱最小、中間柱最大(標準的 1:2:4 關係);柱位微調過的
     同一排要被併成同一條軸線,不能算成兩排。
  2. 軸力與斷面:樓層越多柱越粗;守 30cm 下限與 5cm 級距。
  3. 套用:只縮不放;柱位微調量跟著縮回去(貼牆那面保持齊平)。
  4. 端到端:真實樓棟的柱由下往上遞減,且仍通過柱位對齊檢核。
"""
from __future__ import annotations

import json

import pytest

from src.design.column_design import (
    DEAD_LOAD,
    LIVE_LOAD,
    MIN_SIDE,
    SIDE_STEP,
    ColumnDesignReport,
    _push_exterior_out,
    _shrink_tuck,
    apply_column_design,
    axial_load,
    column_seating,
    column_visibility,
    design_building_columns,
    empirical_start_side,
    grid_regularity,
    required_side,
    tributary_areas,
)


# ---------------------------------------------------------------------------
# 1) 負擔面積
# ---------------------------------------------------------------------------
def test_tributary_corner_edge_interior_ratio() -> None:
    """3×3 等距柱網:角柱 : 邊柱 : 中柱 = 1 : 2 : 4(結構學的標準結果)。"""
    step = 6000.0
    centers = [(i * step, j * step) for i in range(3) for j in range(3)]
    trib = dict(zip(centers, tributary_areas(centers)))

    corner = trib[(0.0, 0.0)]
    edge = trib[(step, 0.0)]
    interior = trib[(step, step)]

    assert corner == pytest.approx(9.0)          # 3m × 3m
    assert edge == pytest.approx(corner * 2)
    assert interior == pytest.approx(corner * 4)


def test_tributary_total_equals_footprint() -> None:
    """所有柱的負擔面積加起來 = 柱網涵蓋的樓地板面積(不重不漏)。"""
    xs, ys = [0.0, 5000.0, 11000.0], [0.0, 4000.0, 9000.0]
    centers = [(x, y) for x in xs for y in ys]
    assert sum(tributary_areas(centers)) == pytest.approx(11.0 * 9.0)


def test_tucked_row_counts_as_one_axis() -> None:
    """柱位微調過的同一排(y 差 190mm)要併成一條軸線,不能算成兩排。

    不併的話那排會被當成兩條相距 190mm 的軸線,負擔面積算出來會嚴重偏小。
    """
    centers = [(0.0, 0.0), (6000.0, 0.0),
               (0.0, 6190.0), (6000.0, 6000.0),      # 這排一根被推了 190
               (0.0, 12000.0), (6000.0, 12000.0)]
    trib = tributary_areas(centers)
    assert sum(trib) == pytest.approx(6.0 * 12.0)
    assert len(set(round(t, 6) for t in trib)) == 2   # 只有角柱/邊柱兩種


def test_single_column_has_no_span() -> None:
    assert tributary_areas([(0.0, 0.0)]) == [0.0]


def test_no_columns_returns_empty() -> None:
    assert tributary_areas([]) == []


# ---------------------------------------------------------------------------
# 2) 軸力與斷面
# ---------------------------------------------------------------------------
def test_axial_load_is_area_times_floors_times_unit_weight() -> None:
    assert axial_load(20.0, 3) == pytest.approx(20.0 * 3 * (DEAD_LOAD + LIVE_LOAD))


def test_axial_load_rejects_negative_floors() -> None:
    with pytest.raises(ValueError):
        axial_load(20.0, -1)


def test_required_side_grows_with_load() -> None:
    small = required_side(axial_load(20.0, 2))
    big = required_side(axial_load(20.0, 12))
    assert big > small


def test_required_side_respects_code_minimum_and_step() -> None:
    """再小的載重也不得低於 30cm(規範下限);尺寸一律 5cm 級距。"""
    s = required_side(0.1)
    assert s == MIN_SIDE
    for load in (5, 50, 120, 400, 900):
        assert required_side(load) % SIDE_STEP == 0


def test_empirical_start_increases_with_floors_carried() -> None:
    sides = [empirical_start_side(n) for n in range(1, 13)]
    assert all(a <= b for a, b in zip(sides, sides[1:])), sides
    assert empirical_start_side(1) == MIN_SIDE
    assert empirical_start_side(6) > empirical_start_side(2)


# ---------------------------------------------------------------------------
# 3) 柱位微調的縮回
# ---------------------------------------------------------------------------
def test_shrink_tuck_keeps_seated_face_flush() -> None:
    """柱變細之後,原本貼著牆面的那一面要還在原位。

    500 的柱被往北推 190(= 500/2 − 120/2)讓南面貼齊牆南面;縮成 350 之後
    推移量應變成 115(= 350/2 − 120/2),南面仍在同一個 y。
    """
    axis = 10000.0
    tucked = [(0.0, axis + 190.0)]
    (_, ny), = _shrink_tuck(tucked, [axis], old=500.0, new=350.0)
    assert ny == pytest.approx(axis + 115.0)
    # 南面 = 柱心 − 半個柱寬,縮前縮後同一位置。
    assert (axis + 190.0) - 250.0 == pytest.approx(ny - 175.0)


def test_shrink_tuck_leaves_untucked_columns_alone() -> None:
    axis = 10000.0
    assert _shrink_tuck([(0.0, axis)], [axis], 500.0, 300.0) == [(0.0, axis)]


def test_shrink_tuck_handles_negative_offset() -> None:
    """往南推的柱(負推移量)也要對稱地縮回去。"""
    axis = 10000.0
    (_, ny), = _shrink_tuck([(0.0, axis - 190.0)], [axis], 500.0, 350.0)
    assert ny == pytest.approx(axis - 115.0)


# ---------------------------------------------------------------------------
# 4) 端到端
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def house_building():
    from src.design.building_generator import BuildingBrief, generate_building
    from src.design.layout_generator import HouseBrief
    return generate_building(BuildingBrief(
        typical=HouseBrief(site_width=19000, site_depth=13000, bedrooms=3),
        floors=3, basements=1, differentiated=True))


def test_lower_floors_get_fatter_columns(house_building) -> None:
    sizes = [f.spec.column_size for f in house_building.floors]
    assert all(a >= b for a, b in zip(sizes, sizes[1:])), sizes
    assert sizes[0] > sizes[-1], f"四層樓應有粗細變化,實得 {sizes}"


def test_applied_never_exceeds_default(house_building) -> None:
    """只縮不放 —— 放大才可能新撞到門窗家具,所以這條是安全性的根本。"""
    assert all(f.spec.column_size <= 500.0 for f in house_building.floors)


def test_alignment_still_passes_with_stepped_columns(house_building) -> None:
    from src.design.building_generator import check_column_alignment
    assert check_column_alignment(house_building) == []


def test_report_has_serialization_and_governing_reason(house_building) -> None:
    rep = design_building_columns(house_building)
    assert isinstance(rep, ColumnDesignReport)
    assert rep.floors and rep.summary()
    d = rep.to_dict()
    assert set(d["sizes"]) == {f.label for f in house_building.floors}
    assert json.loads(rep.to_json())["note"]
    # 每層都要講清楚斷面是「重力」還是「經驗值」決定的,不能讓人以為全是算的。
    assert all(f["governed_by"] in ("重力", "經驗值") for f in d["floors"])


# ---------------------------------------------------------------------------
# 5) 柱座落品質(柱藏在牆裡)
# ---------------------------------------------------------------------------
def test_seating_classifies_junction_edge_orphan() -> None:
    """三種座落各造一根柱驗證分類:牆交會 / 只有一道牆 / 完全沒牆。"""
    from types import SimpleNamespace

    from src.drafting.wall import Wall

    spec = SimpleNamespace(
        column_size=500.0,
        x_spacings=[6000.0], y_spacings=[6000.0], grid_origin=(0.0, 0.0),
        column_centers=[(0.0, 0.0), (6000.0, 0.0), (3000.0, 3000.0)],
        walls=[Wall(start=(0.0, -3000.0), end=(0.0, 9000.0)),   # 縱牆穿過 (0,0)
               Wall(start=(-3000.0, 0.0), end=(9000.0, 0.0))],  # 橫牆穿過 (0,0)、(6000,0)
    )
    rep = column_seating(spec)
    assert (rep.junction, rep.edge, rep.orphan) == (1, 1, 1)
    assert rep.orphan_points == [(3000.0, 3000.0)]
    assert rep.total == 3
    assert rep.seated_pct == pytest.approx(200 / 3)


def test_seating_counts_tucked_column_as_seated() -> None:
    """柱位微調把柱推離牆中心線 190,但柱身仍蓋著牆 —— 要算「坐在牆上」。

    這條是判準的關鍵:如果改用「柱心到牆中心線的距離」去判,這種**最正確**
    的柱反而會被誤判成孤柱。
    """
    from types import SimpleNamespace

    from src.drafting.wall import Wall

    spec = SimpleNamespace(
        column_size=500.0,
        x_spacings=[], y_spacings=[], grid_origin=(0.0, 0.0),
        column_centers=[(0.0, 190.0)],                       # 被往北推了 190
        walls=[Wall(start=(-3000.0, 0.0), end=(3000.0, 0.0))],
    )
    assert column_seating(spec).orphan == 0


@pytest.mark.parametrize("width, depth, beds", [
    (19000, 13000, 3), (24000, 16000, 4), (21000, 12000, 3),
])
def test_no_orphan_columns_in_two_band_house(width, depth, beds) -> None:
    """孤柱 = 柱單獨杵在房間裡,是柱網品質的紅線 —— 任何尺寸都不該出現。

    這條守的是 _plan_x_grid 的「軸線吸附主要隔牆 / 反過來從隔牆挑軸線」兩條
    路徑。日後有人動柱網演算法,孤柱一冒出來這裡就會亮。
    """
    from src.design.building_generator import BuildingBrief, generate_building
    from src.design.layout_generator import HouseBrief

    b = generate_building(BuildingBrief(
        typical=HouseBrief(site_width=width, site_depth=depth, bedrooms=beds),
        floors=3, differentiated=True))
    for fl in b.floors:
        rep = column_seating(fl.spec)
        assert rep.orphan == 0, f"{fl.label} 有孤柱 {rep.orphan_points}"
        assert rep.seated_pct == 100.0


def test_seating_report_serialization() -> None:
    from types import SimpleNamespace
    spec = SimpleNamespace(column_size=500.0, x_spacings=[], y_spacings=[],
                           grid_origin=(0.0, 0.0), column_centers=[(0.0, 0.0)],
                           walls=[])
    rep = column_seating(spec)
    assert rep.summary()
    assert json.loads(rep.to_json())["orphan"] == 1


# ---------------------------------------------------------------------------
# 6) 室內看不看得到柱(使用者 2026-08-07 定調的設計原則)
# ---------------------------------------------------------------------------
def _box_spec(column_centers, *, size=500.0, thickness=200.0,
              site=None, setback=0.0, room_kind="living"):
    """10×10m 的方盒子:四面外牆 + 一個房間,給柱位測試用。"""
    from types import SimpleNamespace

    from src.drafting.room import Room
    from src.drafting.wall import Wall

    c = [(0.0, 0.0), (10000.0, 0.0), (10000.0, 10000.0), (0.0, 10000.0)]
    walls = [Wall(start=c[i], end=c[(i + 1) % 4], thickness=thickness)
             for i in range(4)]
    return SimpleNamespace(
        column_size=size, column_centers=column_centers, walls=walls,
        rooms=[Room(name="客廳", kind=room_kind, points=c)],
        x_spacings=[10000.0], y_spacings=[10000.0], grid_origin=(0.0, 0.0),
        site_boundary=site or c, setback=setback)


def test_visibility_sees_column_standing_in_a_room() -> None:
    """房間正中央一根柱 → 整根都看得到。"""
    rep = column_visibility(_box_spec([(5000.0, 5000.0)]))
    assert rep.visible_m2 == pytest.approx(0.25, rel=0.02)   # 500×500 = 0.25m²
    assert rep.interior_columns == 1 and rep.exterior_columns == 0


def test_visibility_ignores_column_swallowed_by_a_thick_wall() -> None:
    """牆比柱厚 → 柱整根埋在牆裡,室內一點都看不到。"""
    rep = column_visibility(_box_spec([(0.0, 5000.0)], size=200.0,
                                      thickness=400.0))
    assert rep.visible_m2 == pytest.approx(0.0, abs=1e-6)


def test_push_exterior_makes_inner_face_flush_with_wall() -> None:
    """外牆柱推出去之後,室內那一面要與牆內面齊平(室內完全平整)。"""
    site = [(-3000.0, -3000.0), (13000.0, -3000.0),
            (13000.0, 13000.0), (-3000.0, 13000.0)]
    spec = _box_spec([(0.0, 5000.0)], size=500.0, thickness=200.0,
                     site=site, setback=1000.0)
    (nx, ny), = _push_exterior_out(spec, 500.0)
    assert ny == 5000.0                       # 沿牆方向不動
    assert nx == pytest.approx(-150.0)        # 往西推 (500−200)/2
    assert nx + 250.0 == pytest.approx(100.0)  # 柱東面 == 牆東面(內面)


def test_push_respects_building_line() -> None:
    """建築剛好蓋到建築線上 → 一根都不能推(寧可室內看得到,也不能違建)。"""
    site = [(-100.0, -100.0), (10100.0, -100.0),
            (10100.0, 10100.0), (-100.0, 10100.0)]
    spec = _box_spec([(0.0, 5000.0)], size=500.0, thickness=200.0,
                     site=site, setback=0.0)
    assert _push_exterior_out(spec, 500.0) == [(0.0, 5000.0)]


def test_push_is_partial_when_room_is_tight() -> None:
    """餘裕不足全推時要「有多少推多少」,不是全有全無。"""
    site = [(-300.0, -300.0), (10300.0, -300.0),
            (10300.0, 10300.0), (-300.0, 10300.0)]
    spec = _box_spec([(0.0, 5000.0)], size=500.0, thickness=200.0,
                     site=site, setback=0.0)
    (nx, _), = _push_exterior_out(spec, 500.0)
    assert nx == pytest.approx(-50.0)     # 想推 150,只推得動 50(300−250)


def test_push_is_idempotent() -> None:
    """推出去之後柱心已離開建築外緣 → 再呼叫一次不該被重複推。"""
    site = [(-3000.0, -3000.0), (13000.0, -3000.0),
            (13000.0, 13000.0), (-3000.0, 13000.0)]
    spec = _box_spec([(0.0, 5000.0)], size=500.0, thickness=200.0,
                     site=site, setback=1000.0)
    once = _push_exterior_out(spec, 500.0)
    spec.column_centers = once
    assert _push_exterior_out(spec, 500.0) == once


def test_push_moves_corner_column_both_ways() -> None:
    site = [(-3000.0, -3000.0), (13000.0, -3000.0),
            (13000.0, 13000.0), (-3000.0, 13000.0)]
    spec = _box_spec([(0.0, 0.0)], size=500.0, thickness=200.0,
                     site=site, setback=1000.0)
    (nx, ny), = _push_exterior_out(spec, 500.0)
    assert (nx, ny) == pytest.approx((-150.0, -150.0))


def test_generated_house_hides_most_columns(house_building) -> None:
    """端到端:實際樓棟裡「室內看得到的柱角」要遠小於舊行為(全棟 500)。

    舊行為量到 3.3~4.2 m²(19×13 三層),現在應該落在 1 m² 以下。
    """
    total = sum(column_visibility(f.spec).visible_m2 for f in house_building.floors)
    assert total < 1.5, f"室內看得到的柱角 {total:.3f} m²,比預期多"


def test_visibility_report_serialization(house_building) -> None:
    rep = column_visibility(house_building.floors[0].spec)
    assert rep.summary()
    d = json.loads(rep.to_json())
    assert d["visible_m2"] >= 0 and isinstance(d["by_room_kind"], dict)


def test_apply_is_idempotent(house_building) -> None:
    """再套一次不該再縮 —— 否則每次重畫柱都會愈來愈細。"""
    before = [f.spec.column_size for f in house_building.floors]
    apply_column_design(house_building)
    assert [f.spec.column_size for f in house_building.floors] == before


# ---------------------------------------------------------------------------
# 5) 柱是實心的 —— 家具不能壓在柱上(使用者 2026-08-10 指出:沙發被柱卡住)
# ---------------------------------------------------------------------------
def _counter_spec(counter, column_centers, *, size=350.0):
    """一段流理台 + 指定柱位的方盒子(牆厚 150,柱兩側各凸 100mm)。"""
    spec = _box_spec(column_centers, size=size, thickness=150.0)
    spec.fixtures = [counter]
    return spec


def test_column_footprints_are_solid_boxes() -> None:
    from src.design.column_design import column_footprints

    polys = column_footprints(_box_spec([(0.0, 5000.0), (10000.0, 5000.0)],
                                        size=350.0))
    assert len(polys) == 2
    assert all(p.area == pytest.approx(350.0 ** 2) for p in polys)


def test_column_footprints_empty_without_a_grid() -> None:
    """窄透天/淺透天沒有柱網 —— 解不出來要回空清單,不能炸掉整條產線。"""
    from types import SimpleNamespace

    from src.design.column_design import column_footprints

    assert column_footprints(SimpleNamespace(column_size=350.0)) == []


def test_counter_is_trimmed_when_a_column_bites_its_end() -> None:
    """柱角咬到檯面**端部** → 截短讓開(不是整排往室內推)。"""
    from src.design.layout.fixture_fix import trim_counters_at_columns
    from src.drafting.fixtures import Counter, counter_footprint
    from src.design.column_design import column_footprints
    from shapely.geometry import Polygon

    # 北牆(y=10000)下方的檯面,西端剛好碰到 x=2000 那根柱。
    counter = Counter(start=(1900.0, 9925.0), end=(8000.0, 9925.0), depth=600.0)
    spec = _counter_spec(counter, [(2000.0, 10000.0)])

    assert trim_counters_at_columns(spec) == 1
    assert counter.start[0] == pytest.approx(2175.0)      # 縮到柱的東緣
    assert counter.end == (8000.0, 9925.0)                # 另一端不動
    foot = Polygon(counter_footprint(counter))
    assert all(foot.intersection(c).area < 1000.0
               for c in column_footprints(spec))


def test_counter_with_a_column_mid_run_is_left_alone() -> None:
    """柱落在檯面中段 → 截了會斷成兩截,不動它(交給整排外推收尾)。"""
    from src.design.layout.fixture_fix import trim_counters_at_columns
    from src.drafting.fixtures import Counter

    counter = Counter(start=(1000.0, 9925.0), end=(9000.0, 9925.0), depth=600.0)
    spec = _counter_spec(counter, [(5000.0, 10000.0)])
    before = (counter.start, counter.end)

    assert trim_counters_at_columns(spec) == 0
    assert (counter.start, counter.end) == before


def test_counter_is_not_trimmed_below_minimum_length() -> None:
    """截短到不成一段廚具就不截 —— 寧可外推,也不要留一截 30cm 的檯面。"""
    from src.design.layout.fixture_fix import MIN_COUNTER_LEN, trim_counters_at_columns
    from src.drafting.fixtures import Counter

    counter = Counter(start=(1900.0, 9925.0), end=(3000.0, 9925.0), depth=600.0)
    assert counter.length < MIN_COUNTER_LEN + 200.0
    spec = _counter_spec(counter, [(2000.0, 10000.0)])
    before = (counter.start, counter.end)

    assert trim_counters_at_columns(spec) == 0
    assert (counter.start, counter.end) == before


def test_no_furniture_sits_on_a_column(house_building) -> None:
    """**回歸關卡**:實際樓棟裡不該有任何一件家具壓在柱上。

    使用者 2026-08-10 指出「柱會卡到沙發」——當時實測 18×13 三層有 10 件
    家具壓在柱上(1F 兩張沙發各 0.035㎡、流理台 0.021㎡),原因是擺位器與
    圖面關卡都只認牆、不認柱。
    """
    from src.design.column_design import column_footprints
    from src.design.layout.fixture_fix import _footprint

    bad = []
    for fl in house_building.floors:
        cols = column_footprints(fl.spec)
        for fx in getattr(fl.spec, "fixtures", None) or []:
            area = sum(_footprint(fx).intersection(c).area for c in cols)
            if area > 1000.0:
                bad.append((fl.spec.floor_label,
                            getattr(fx, "name", type(fx).__name__),
                            round(area / 1e6, 3)))
    assert not bad, f"這些家具壓在柱上:{bad}"


def test_plan_check_flags_furniture_on_a_column() -> None:
    """關卡要抓得到「家具壓在柱上」,而且與穿牆分開報(成因與解法不同)。"""
    from src.design.layout.plan_check import check_floor
    from src.drafting.fixtures import FixturePlacement

    spec = _box_spec([(5000.0, 5000.0)], size=500.0, thickness=150.0)
    spec.floor_label = "1F"
    spec.fixtures = [FixturePlacement(name="sofa3", insert=(5000.0, 5000.0),
                                      rotation=0)]
    hit = [i for i in check_floor(spec) if i.code == "furniture_in_column"]
    assert hit
    # ⚠️ 必須是 warning:挪不開時我們刻意留著壓柱(見 clear_fixtures_off_columns),
    #    列成 error 會讓產線為了修不掉的東西無限重生。
    assert hit[0].severity == "warning"
    codes = {i.code for i in check_floor(spec)}
    assert "furniture_in_wall" not in codes         # 與穿牆分開報,不混為一談


def test_clearing_a_column_never_blocks_a_door() -> None:
    """★ 優先順序:穿牆 > 擋門 > 壓柱。閃柱會擋到門時,寧可留著壓柱不動。

    18.4×14.5m/seed4242 的鞋櫃就是這個情境 —— 第一版把柱併進 `_wall_union`
    (壓柱=穿牆同級),鞋櫃為了閃柱退進門的開啟弧,整份設計被檢核否決。
    """
    from src.design.layout_generator import HouseBrief, generate_floor_plan

    generate_floor_plan(HouseBrief(site_width=18400.0, site_depth=14500.0,
                                   bedrooms=4, setback=0, seed=4242))


# ---------------------------------------------------------------------------
# 6) 替柱留位置 → 外牆柱躲到室外(使用者 2026-08-10:「室內看不到柱」)
# ---------------------------------------------------------------------------
def test_exterior_columns_invisible_when_site_can_spare_the_margin() -> None:
    """★ 基地留得起 STRUCT_MARGIN 時,外牆柱一根都不該從室內看到。

    對照丙級術科參考圖(107 年版壹層/貳層平面圖):房間是乾淨的長方形,柱凸出
    的部分全在室外。改動前 19×13 三層有 12 根外牆柱露臉、合計 0.854㎡。
    """
    from src.design.building_generator import BuildingBrief, generate_building
    from src.design.layout_generator import HouseBrief

    building = generate_building(BuildingBrief(
        typical=HouseBrief(site_width=19000, site_depth=13000, bedrooms=3),
        floors=3, differentiated=True))
    for fl in building.floors:
        rep = column_visibility(fl.spec)
        assert rep.exterior_columns == 0, f"{fl.label} 還有 {rep.exterior_columns} 根外牆柱露臉"


def test_struct_margin_never_costs_a_generatable_plan() -> None:
    """★ 鐵則:留柱位不得讓原本生得出來的案子生不出來。

    12×11m/退縮 2m = 建築 8×7m,剛好卡在骨架下限 —— 留滿 275mm 會直接生不出圖。
    退讓階梯必須一路退到 0,行為與改動前一致。
    """
    from src.design.layout_generator import HouseBrief, generate_floor_plan

    generate_floor_plan(HouseBrief(site_width=12000, site_depth=11000,
                                   bedrooms=2))


def test_walls_and_columns_stay_inside_the_building_line() -> None:
    """柱與牆都不得越過建築線(改動前牆外皮就已經超出 65mm,柱超出 165mm)。"""
    from shapely.geometry import Polygon

    from src.design.building_generator import (
        BuildingBrief, _column_centers, generate_building)
    from src.design.layout_generator import HouseBrief

    building = generate_building(BuildingBrief(
        typical=HouseBrief(site_width=19000, site_depth=13000, bedrooms=3),
        floors=3, differentiated=True))
    for fl in building.floors:
        spec = fl.spec
        line = Polygon(spec.site_boundary).buffer(-spec.setback)
        lx0, ly0, lx1, ly1 = line.bounds
        half = float(spec.column_size) / 2.0
        for cx, cy in _column_centers(spec):
            assert cx - half >= lx0 - 1 and cx + half <= lx1 + 1, (fl.label, cx)
            assert cy - half >= ly0 - 1 and cy + half <= ly1 + 1, (fl.label, cy)


# ---------------------------------------------------------------------------
# 7) 閃柱的還原:只還原自己挪過的
# ---------------------------------------------------------------------------
def _sofa_spec(insert, *, columns=()):
    from src.drafting.fixtures import FixturePlacement

    spec = _box_spec(list(columns), size=350.0, thickness=150.0)
    spec.doors = []                     # settle 要查門的迴轉範圍
    spec.fixtures = [FixturePlacement(name="sofa3", insert=insert, rotation=0)]
    return spec, spec.fixtures[0]


def test_settle_returns_a_dodged_fixture_towards_the_wall() -> None:
    """柱後來自己躲到室外了 → 當初為了閃它而讓開的家具要沿原路走回去。"""
    from src.design.layout.fixture_fix import DODGE_MARK, settle_fixtures_to_wall

    spec, fx = _sofa_spec((5000.0, 200.0))
    setattr(fx, DODGE_MARK, (0.0, 100.0, fx.insert))    # 剛才往室內讓了 100mm
    assert settle_fixtures_to_wall(spec) == 1
    assert fx.insert[1] == pytest.approx(100.0)


def test_settle_leaves_untouched_fixtures_alone() -> None:
    """★ 沒有閃柱記號的家具一律不動 —— 那是別的模組的決定。

    第一版是「所有家具都盡量往牆邊推」,把 `_declutter_for_circulation` 特地
    挪開讓出通道的家具又推回去 → 淺透天掃描冒出 4 案 circulation_blocked。
    """
    from src.design.layout.fixture_fix import settle_fixtures_to_wall

    spec, fx = _sofa_spec((5000.0, 3000.0))
    before = fx.insert
    assert settle_fixtures_to_wall(spec) == 0
    assert fx.insert == before


def test_settle_skips_a_fixture_that_moved_since_the_dodge() -> None:
    """記錄的位置對不上 = 之後被別人動過 → 不還原(不能覆蓋別人的決定)。"""
    from src.design.layout.fixture_fix import DODGE_MARK, settle_fixtures_to_wall

    spec, fx = _sofa_spec((5000.0, 200.0))
    setattr(fx, DODGE_MARK, (0.0, 100.0, (1234.0, 5678.0)))
    before = fx.insert
    assert settle_fixtures_to_wall(spec) == 0
    assert fx.insert == before


# ---------------------------------------------------------------------------
# 5) 柱網規則性(跨度等不等距、在不在經濟區間)
# ---------------------------------------------------------------------------
#
# 為什麼要有這一節(使用者 2026-08-19 指出「柱子的位子不合邏輯」):
# `column_seating` 只回答「柱有沒有坐在牆上」,實測一直是 100% —— 但柱網本身
# 可以同時是荒謬的(中央核骨架把核的左右牆當柱線,核寬固定 3.4m,兩側跨度卻
# 跟著建築寬長大,30m 寬時變成 [13.3, 3.4, 13.3])。兩件事要分開量,才講得出
# 「柱網規則性我們有量測」,而不是憑感覺。
class _GridSpec:
    """只帶柱網間距的假 spec —— grid_regularity 只讀這兩個欄位(單位 mm)。"""

    def __init__(self, xs=(), ys=()):
        self.x_spacings = list(xs)
        self.y_spacings = list(ys)


def test_grid_equal_spans_是合格的() -> None:
    r = grid_regularity(_GridSpec([6330.0] * 3, [5500.0] * 2))
    assert r.spans_x == [6.33, 6.33, 6.33]
    assert r.ratio == pytest.approx(1.0)
    assert r.ok


def test_grid_短跨躲不掉時不算缺點() -> None:
    """11m 深切 2 跨 = 5.5m(短),但切 1 跨變 11m —— 超過 9m 上限。

    ⚠️ 這條釘的是我第一版判錯的地方:把這種「建築尺寸逼出來的短跨」報成不合格
    是誣賴,設計師沒有更好的做法可選。它要進 forced_short、不進 outside_economic。
    """
    r = grid_regularity(_GridSpec([], [5500.0, 5500.0]))
    assert r.outside_economic == []
    assert r.forced_short == [5.5, 5.5]
    assert r.ok


def test_grid_等分之後不短的短跨要算缺點() -> None:
    """[7.8, 3.4, 7.8] 的 3.4m 躲得掉 —— 同樣 3 跨等分就是 6.33m,完全落在區間內。

    ⚠️ 這條釘的是我第二版判錯的地方:當時問的是「能不能少切一跨」(19/2=9.5>9
    → 以為躲不掉),但該問的是「**等分**之後還短不短」。問錯問題就會放過真正的
    不規則柱網。
    """
    r = grid_regularity(_GridSpec([7800.0, 3400.0, 7800.0], []))
    assert 3.4 in r.outside_economic
    assert r.forced_short == []
    assert r.ratio == pytest.approx(7.8 / 3.4, rel=1e-3)
    assert not r.ok


def test_grid_過長跨永遠是缺點() -> None:
    """30m 寬的舊柱網 [13.3, 3.4, 13.3]:13m 的樑做不出來,長短跨都要報。"""
    r = grid_regularity(_GridSpec([13300.0, 3400.0, 13300.0], []))
    assert 13.3 in r.outside_economic and 3.4 in r.outside_economic
    assert not r.ok


def test_grid_兩個方向不能混在一起比() -> None:
    """長方形房子本來就是「面寬 3 跨、進深 2 跨」,兩向跨度不同不是不規則。

    X 各 7m、Y 各 5.5m:混著比會得到 7/5.5 = 1.27 倍(誤判),各自比都是 1.00。
    """
    r = grid_regularity(_GridSpec([7000.0] * 3, [5500.0] * 2))
    assert r.ratio == pytest.approx(1.0)
    assert max(r.spans) / min(r.spans) > 1.15      # 混著比就會誤判成不規則


def test_grid_單跨沒有柱網也算合格() -> None:
    """窄透天只有一跨(甚至沒填 spacings)——沒有柱網就沒有規則性問題。"""
    r = grid_regularity(_GridSpec([], []))
    assert r.ratio == pytest.approx(1.0)
    assert r.ok and r.spans == []


def test_grid_report_可序列化() -> None:
    """照專案慣例:每個 Report 都要有 summary() / to_dict() / to_json()。"""
    r = grid_regularity(_GridSpec([7800.0, 3400.0, 7800.0], [5500.0, 5500.0]))
    d = r.to_dict()
    assert set(d) == {"spans_x", "spans_y", "ratio", "outside_economic",
                      "forced_short", "ok"}
    assert d["spans_x"] == [7.8, 3.4, 7.8]
    assert json.loads(r.to_json()) == d
    assert "柱網" in r.summary() and "不合格" in r.summary()
