"""Auto Layout Engine 測試(v0.7 Phase 6-9)。

Phase 6 總入口:吃 FloorPlanSpec 或 BuildingSpec,跑完整家具智慧鏈,輸出最佳
Layout + 分數。重點:

  * generate 單一配置跑一次;optimize 試多種權重挑最好。
  * 唯讀 w.r.t. 輸入:原 spec/building 不變;最佳化後仍 collision-valid、可畫 DXF。
  * export_json / export_report / benchmark / top_n。

⚠️ 不接進生成流程,不影響 DXF/PNG/Benchmark 生成。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from src.design.building_generator import (
    BuildingBrief,
    BuildingSpec,
    generate_building,
)
from src.design.collision.furniture_engine import FurnitureCollisionEngine
from src.design.layout.auto_layout_engine import (
    WEIGHT_PROFILES,
    AutoLayoutEngine,
    AutoLayoutResult,
)
from src.design.layout.global_score import SCORE_ITEMS, LayoutBenchmark, LayoutScore
from src.design.layout.multi_room_optimizer import RoomScore
from src.design.layout_generator import (
    HouseBrief,
    generate_floor_plan,
    generate_house_upper,
)
from src.drafting.apartment_plan import draw_floor_plan
from src.drafting.fixtures import FixturePlacement
from src.web.render import _new_doc

GRADES = {"A+", "A", "B", "C", "D"}


def _floor(bedrooms=3):
    return generate_floor_plan(
        HouseBrief(site_width=20000, site_depth=14000, bedrooms=bedrooms))


def _townhouse(floors=2):
    return generate_building(BuildingBrief(
        typical=HouseBrief(site_width=19000, site_depth=13000,
                           bedrooms=3, seed=5),
        floors=floors))


def _positions(floor_spec):
    return [(f.name, tuple(f.insert), f.rotation)
            for f in floor_spec.fixtures if isinstance(f, FixturePlacement)]


def _draw_ok(floor_spec) -> int:
    doc, layers = _new_doc()
    draw_floor_plan(doc.modelspace(), floor_spec, layers)
    return len(list(doc.modelspace()))


def _all_valid(floor_spec) -> bool:
    checks = FurnitureCollisionEngine(floor_spec).check_existing()
    return bool(checks) and all(r.valid for _, r in checks)


# 主案例只跑一次,多數測試共用(逐件重擺較慢)。
FLOOR = _floor(3)
FLOOR_SNAP = _positions(FLOOR)
RES = AutoLayoutEngine().generate(FLOOR, name="house3")

TOWN = _townhouse(2)
TOWN_SNAP = [(fl.label, _positions(fl.spec)) for fl in TOWN.floors]
BRES = AutoLayoutEngine().generate(TOWN, name="town")


# ── 單層 generate:結構 ───────────────────────────────────────────────────
def test_generate_returns_result_in_range():
    assert isinstance(RES, AutoLayoutResult)
    assert 0.0 <= RES.overall_score <= 100.0 and RES.grade in GRADES
    assert RES.name == "house3"


def test_room_layouts_map_rooms_to_furniture():
    """★ room_layouts:房名 → 家具清單(每件有 name/insert/rotation)。"""
    assert RES.room_layouts
    for room, items in RES.room_layouts.items():
        assert isinstance(room, str) and isinstance(items, list)
    all_items = [it for items in RES.room_layouts.values() for it in items]
    assert any("insert" in it for it in all_items)


def test_placement_results_are_room_scores():
    assert RES.placement_results
    assert all(isinstance(r, RoomScore) for r in RES.placement_results)


def test_floor_scores_single_entry():
    assert len(RES.floor_scores) == 1
    fs = RES.floor_scores[0]
    assert {"label", "overall", "grade"} <= set(fs)


def test_global_scores_carry_twelve_sub_scores():
    assert RES.global_scores and isinstance(RES.global_scores[0], LayoutScore)
    assert set(RES.global_scores[0].sub_scores) == set(SCORE_ITEMS)


def test_layout_score_present_for_benchmarking():
    assert isinstance(RES.layout_score, LayoutScore)
    assert abs(RES.layout_score.overall_score - RES.overall_score) < 1.0


def test_generate_is_deterministic():
    r2 = AutoLayoutEngine().generate(_floor(3))
    assert abs(r2.overall_score - RES.overall_score) < 1e-6


# ── 唯讀 / DXF / 合法性 ───────────────────────────────────────────────────
def test_source_spec_not_mutated():
    """★ 唯讀:原 floor spec 家具位置不變。"""
    assert _positions(FLOOR) == FLOOR_SNAP


def test_optimized_spec_is_collision_valid():
    """★ 最佳化後家具全通過碰撞查詢。"""
    assert _all_valid(RES.spec)


def test_optimized_spec_draws_to_dxf():
    """★ DXF 沒被破壞:最佳化後的 spec 仍能正常畫出實體。"""
    assert _draw_ok(RES.spec) > 0


# ── optimize:多權重搜尋 ─────────────────────────────────────────────────
def test_optimize_is_at_least_as_good_as_default():
    """★ optimize 試多配置挑最好 → 不差於預設(balanced 是其中一個配置)。"""
    spec = _floor(2)
    base = AutoLayoutEngine().generate(spec)
    best = AutoLayoutEngine().optimize(spec)
    assert best.overall_score >= base.overall_score - 1e-6


def test_optimize_result_is_valid_and_drawable():
    best = AutoLayoutEngine().optimize(_floor(2))
    assert best.grade in GRADES
    assert _all_valid(best.spec) and _draw_ok(best.spec) > 0


def test_weight_profiles_include_balanced():
    assert "balanced" in WEIGHT_PROFILES
    assert all(hasattr(w, "as_map") for w in WEIGHT_PROFILES.values())


# ── 房數規模:小 / 四房 / 大 ─────────────────────────────────────────────
def test_small_house_one_bedroom():
    r = AutoLayoutEngine().generate(_floor(1))
    assert r.room_layouts and r.grade in GRADES and _all_valid(r.spec)


def test_two_bedroom_house():
    r = AutoLayoutEngine().generate(_floor(2))
    assert 0.0 <= r.overall_score <= 100.0 and _all_valid(r.spec)


def test_four_bedroom_house():
    r = AutoLayoutEngine().generate(_floor(4))
    bedrooms = [rs for rs in r.placement_results if rs.kind == "bedroom"]
    assert len(bedrooms) >= 3 and _all_valid(r.spec)


def test_large_multi_room_house():
    r = AutoLayoutEngine().generate(generate_house_upper(
        HouseBrief(site_width=26000, site_depth=16000, bedrooms=3, seed=1)))
    assert r.room_layouts and _all_valid(r.spec) and _draw_ok(r.spec) > 0


# ── 透天(多樓層 BuildingSpec)────────────────────────────────────────────
def test_townhouse_has_per_floor_scores():
    """★ 透天:每層一個 floor_score,overall 為整棟聚合。"""
    assert len(BRES.floor_scores) == len(TOWN.floors)
    assert 0.0 <= BRES.overall_score <= 100.0 and BRES.grade in GRADES


def test_townhouse_room_layouts_are_floor_prefixed():
    """★ 透天房名以樓層前綴,避免跨層同名相撞。"""
    assert BRES.room_layouts
    assert all("/" in k for k in BRES.room_layouts)
    labels = {k.split("/")[0] for k in BRES.room_layouts}
    assert labels <= {fl.label for fl in TOWN.floors}


def test_townhouse_placement_results_aggregate_floors():
    assert len(BRES.placement_results) >= len(BRES.floor_scores)


def test_townhouse_source_not_mutated():
    """★ 唯讀:原 building 各層家具位置不變。"""
    now = [(fl.label, _positions(fl.spec)) for fl in TOWN.floors]
    assert now == TOWN_SNAP


def test_townhouse_result_spec_is_building_and_drawable():
    """★ 結果 spec 是 BuildingSpec,層數不變,每層都能畫 DXF。"""
    assert isinstance(BRES.spec, BuildingSpec)
    assert len(BRES.spec.floors) == len(TOWN.floors)
    for fl in BRES.spec.floors:
        assert _draw_ok(fl.spec) > 0
        assert _all_valid(fl.spec)


# ── 匯出 ──────────────────────────────────────────────────────────────────
def test_export_json_round_trips():
    eng = AutoLayoutEngine()
    eng.generate(_floor(2), name="x")
    text = eng.export_json()
    d = json.loads(text)
    assert d == eng._last.to_dict()
    assert "overall_score" in d and "room_layouts" in d
    assert "\\u" not in text


def test_export_report_is_readable_text():
    eng = AutoLayoutEngine()
    r = eng.generate(_floor(2), name="rep")
    rep = eng.export_report()
    assert isinstance(rep, str) and r.grade in rep


def test_export_before_generate_raises():
    """★ 還沒 generate 就 export → 明確報錯。"""
    eng = AutoLayoutEngine()
    with pytest.raises(RuntimeError):
        eng.export_json()
    with pytest.raises(RuntimeError):
        eng.export_report()


def test_result_to_dict_is_json_native():
    d = RES.to_dict()
    text = json.dumps(d)                             # 純原生型別才 dumps 得出來
    assert isinstance(text, str)
    assert set(d) >= {"name", "overall_score", "grade", "room_layouts",
                      "placement_results", "global_scores"}


def test_result_follows_json_convention():
    assert json.loads(RES.to_json()) == RES.to_dict()
    assert RES.grade in RES.summary()


# ── Benchmark / Top N ─────────────────────────────────────────────────────
def test_benchmark_from_named_specs():
    """★ benchmark 多份平面 → LayoutBenchmark,含各自命名。"""
    eng = AutoLayoutEngine()
    bench = eng.benchmark({"a": _floor(2), "b": _floor(1)})
    assert isinstance(bench, LayoutBenchmark)
    assert {e.name for e in bench.entries} == {"a", "b"}


def test_benchmark_ranked_and_average():
    eng = AutoLayoutEngine()
    bench = eng.benchmark({"a": _floor(2), "b": _floor(3)})
    ranked = bench.ranked()
    assert ranked[0].overall_score >= ranked[-1].overall_score
    assert 0.0 <= bench.average() <= 100.0


def test_benchmark_outputs_csv_and_json():
    eng = AutoLayoutEngine()
    bench = eng.benchmark([_floor(1), _floor(2)])
    assert bench.to_csv().splitlines()[0].startswith("rank,name,overall_score")
    assert json.loads(bench.to_json())["count"] == 2


def test_top_n_returns_sorted_prefix():
    """★ top_n 回排名前 n(overall 由高到低)。"""
    eng = AutoLayoutEngine()
    top = eng.top_n({"a": _floor(1), "b": _floor(2), "c": _floor(3)}, 2)
    assert len(top) == 2
    assert top[0].overall_score >= top[1].overall_score


def test_top_n_more_than_available_returns_all():
    eng = AutoLayoutEngine()
    top = eng.top_n([_floor(1)], 5)
    assert len(top) == 1


# ── 輸入型別 ──────────────────────────────────────────────────────────────
def test_generate_rejects_bad_input():
    """★ 非 FloorPlanSpec/BuildingSpec → 明確報 TypeError。"""
    with pytest.raises(TypeError):
        AutoLayoutEngine().generate(object())
