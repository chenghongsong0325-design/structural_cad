"""Multi-room Optimization 測試(v0.7 Phase 6-8)。

整棟依序最佳化:依功能優先序逐房逐件重擺家具,全部做完再打整棟總分。重點:

  * 依優先序(bedroom→bathroom→kitchen→dining→living→study→laundry→balcony)。
  * 唯讀 w.r.t. 輸入:在深拷貝上作業,原 spec 不變。
  * 只在合法位置挑 → 最佳化後仍 collision-valid(硬閘門優先)。
  * 輸出 overall_score / room_scores / summary,可餵進 LayoutBenchmark。

⚠️ 不接進生成流程,不影響 DXF/PNG/Benchmark 生成。
"""
import copy
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.design.collision.furniture_engine import FurnitureCollisionEngine
from src.design.collision.placement_optimizer import PlacementWeights
from src.design.layout.global_score import (
    DEFAULT_LAYOUT_WEIGHTS,
    LayoutBenchmark,
    LayoutScore,
    LayoutScoreEngine,
)
from src.design.layout.multi_room_optimizer import (
    ROOM_ORDER,
    MultiRoomOptimizer,
    MultiRoomResult,
    RoomScore,
)
from src.design.semantic.room_semantic import canonical_room, get_room_rule
from src.design.layout_generator import (
    HouseBrief,
    generate_floor_plan,
    generate_house_upper,
)
from src.drafting.fixtures import Counter, FixturePlacement

GRADES = {"A+", "A", "B", "C", "D"}


def _spec(bedrooms=3):
    return generate_floor_plan(
        HouseBrief(site_width=20000, site_depth=14000, bedrooms=bedrooms))


def _upper():
    return generate_house_upper(
        HouseBrief(site_width=26000, site_depth=16000, bedrooms=3, seed=1))


def _positions(spec):
    return [(f.name, tuple(f.insert), f.rotation)
            for f in spec.fixtures if isinstance(f, FixturePlacement)]


# 主 spec 只跑一次最佳化,多數測試共用(逐件重建 optimizer 較慢)。
SPEC = _spec(3)
SNAPSHOT = _positions(SPEC)
RES = MultiRoomOptimizer(SPEC).optimize()


# ── 結構與範圍 ────────────────────────────────────────────────────────────
def test_optimize_returns_result_in_range():
    assert isinstance(RES, MultiRoomResult)
    assert 0.0 <= RES.overall_score <= 100.0
    assert RES.grade in GRADES
    assert isinstance(RES.global_score, LayoutScore)


def test_room_scores_present_and_in_range():
    """★ 每房都有成績,分數在範圍、replaced ≤ furniture_count。"""
    assert RES.room_scores
    for rs in RES.room_scores:
        assert isinstance(rs, RoomScore)
        assert 0.0 <= rs.semantic <= 100.0
        assert 0.0 <= rs.avg_placement <= 100.0
        assert 0 <= rs.replaced <= rs.furniture_count


def test_rooms_processed_in_priority_order():
    """★ 房間依功能優先序處理(bedroom 在 living 之前…)。"""
    kinds = []
    for name in RES.processed_rooms:
        room = next(r for r in RES.spec.rooms if r.name == name)
        kinds.append(canonical_room(room.kind))
    prio = [ROOM_ORDER.index(k) if k in ROOM_ORDER else len(ROOM_ORDER)
            for k in kinds]
    assert prio == sorted(prio)


def test_bedroom_processed_before_living():
    """★ 明確驗:臥室排在客廳之前。"""
    order = [canonical_room(next(r.kind for r in RES.spec.rooms if r.name == n))
             for n in RES.processed_rooms]
    if "bedroom" in order and "living" in order:
        assert order.index("bedroom") < order.index("living")


# ── 唯讀 / 拷貝 ───────────────────────────────────────────────────────────
def test_source_spec_is_not_mutated():
    """★ 唯讀:原 spec 的家具位置一個都沒動。"""
    assert _positions(SPEC) == SNAPSHOT


def test_result_spec_is_a_distinct_copy():
    assert RES.spec is not SPEC
    assert RES.spec.fixtures is not SPEC.fixtures


def test_optimization_is_deterministic():
    r2 = MultiRoomOptimizer(_spec(3)).optimize()
    assert abs(r2.overall_score - RES.overall_score) < 1e-6
    assert r2.processed_rooms == RES.processed_rooms


# ── 與 Global Score 一致 ─────────────────────────────────────────────────
def test_overall_matches_global_score_of_optimized_spec():
    """★ overall_score = 對最佳化後 spec 打的整棟總分(Phase 6-7)。"""
    gs = LayoutScoreEngine().score(RES.spec)
    assert abs(RES.overall_score - gs.overall_score) < 1e-6


# ── 合法性:硬閘門優先 ────────────────────────────────────────────────────
def test_optimized_layout_is_collision_valid():
    """★ 最佳化後所有家具仍通過事前碰撞查詢(只在合法位置挑)。"""
    checks = FurnitureCollisionEngine(RES.spec).check_existing()
    assert checks and all(res.valid for _, res in checks)


def test_collision_score_does_not_regress():
    """★ collision 子分數不會變差(挑不到合法位就保留原位)。"""
    before = LayoutScoreEngine().sub_scores(SPEC)["collision"]
    after = LayoutScoreEngine().sub_scores(RES.spec)["collision"]
    assert after >= before - 1e-6


# ── 家具數 / 流理台保留 ───────────────────────────────────────────────────
def test_fixture_count_is_preserved():
    """★ 最佳化只搬動、不新增/刪除家具。"""
    assert len(RES.spec.fixtures) == len(SPEC.fixtures)


def test_counters_are_preserved():
    """★ 流理台(Counter)保留原位、數量不變。"""
    src = [f for f in SPEC.fixtures if isinstance(f, Counter)]
    opt = [f for f in RES.spec.fixtures if isinstance(f, Counter)]
    assert len(src) == len(opt)
    assert [(c.start, c.end) for c in src] == [(c.start, c.end) for c in opt]


def test_every_furnishable_room_is_processed():
    """★ 有語意規則的房間都被最佳化到。"""
    want = {r.name for r in SPEC.rooms if get_room_rule(r.kind) is not None}
    assert set(RES.processed_rooms) == want


# ── 房數規模:單房 / 雙房 / 四房 / 完整住宅 ───────────────────────────────
def test_one_bedroom_house():
    r = MultiRoomOptimizer(_spec(1)).optimize()
    assert r.room_scores and 0.0 <= r.overall_score <= 100.0
    assert all(res.valid for _, res in
               FurnitureCollisionEngine(r.spec).check_existing())


def test_two_bedroom_house():
    r = MultiRoomOptimizer(_spec(2)).optimize()
    assert r.grade in GRADES
    assert all(res.valid for _, res in
               FurnitureCollisionEngine(r.spec).check_existing())


def test_four_bedroom_house():
    r = MultiRoomOptimizer(_spec(4)).optimize()
    bedrooms = [rs for rs in r.room_scores if rs.kind == "bedroom"]
    assert len(bedrooms) >= 3
    assert all(res.valid for _, res in
               FurnitureCollisionEngine(r.spec).check_existing())


def test_full_multi_floor_house():
    r = MultiRoomOptimizer(_upper()).optimize()
    assert r.room_scores and r.grade in GRADES
    assert all(res.valid for _, res in
               FurnitureCollisionEngine(r.spec).check_existing())


# ── 權重 ──────────────────────────────────────────────────────────────────
def test_custom_placement_weights_accepted():
    r = MultiRoomOptimizer(
        _spec(2), weights=PlacementWeights(human_clearance=5.0)).optimize()
    assert 0.0 <= r.overall_score <= 100.0
    assert all(res.valid for _, res in
               FurnitureCollisionEngine(r.spec).check_existing())


def test_custom_layout_weights_change_overall():
    spec = _spec(2)
    base = MultiRoomOptimizer(spec).optimize()
    tilt = MultiRoomOptimizer(
        spec, layout_weights={**DEFAULT_LAYOUT_WEIGHTS,
                              "symmetry": 100.0}).optimize()
    assert base.overall_score != tilt.overall_score


# ── API / Report / Benchmark ──────────────────────────────────────────────
def test_result_follows_json_convention():
    assert json.loads(RES.to_json()) == RES.to_dict()
    assert "\\u" not in RES.to_json()
    d = RES.to_dict()
    assert set(d) >= {"overall_score", "grade", "processed_rooms",
                      "room_scores", "global_score"}
    assert len(d["room_scores"]) == len(RES.room_scores)


def test_summary_lists_grade_and_rooms():
    s = RES.summary()
    assert RES.grade in s
    assert RES.room_scores[0].room in s


def test_results_feed_into_layout_benchmark():
    """★ 多個 MultiRoomResult 的 global_score 可丟進 LayoutBenchmark 排序。"""
    r1 = RES
    r2 = MultiRoomOptimizer(_upper()).optimize()
    bench = LayoutBenchmark()
    bench.add(r1.global_score)
    bench.add(r2.global_score)
    ranked = bench.ranked()
    assert len(ranked) == 2
    assert ranked[0].overall_score >= ranked[1].overall_score
    assert 0.0 <= bench.average() <= 100.0
