"""Constraint Score 併入 FurniturePlacementOptimizer 的測試(v0.7 Phase 6-4-1)。

把 Phase 6-4 的 evaluate_constraint 接成 optimizer 的第六個**軟分數**
(constraint)。重點:

  1. Constraint 好 → 總分提高。
  2. Constraint 差 → 總分降低。
  3. Collision invalid → constraint 不得覆蓋(硬閘門永遠先擋)。
  4. constraint 權重=0 → 完全不影響總分。
  5. constraint 權重獨大 → 完全主導排名。
  6. 既有行為不變(best 仍一定通過 collision)。

⚠️ constraint 是軟分數:違反只扣分,不淘汰候選;合法與否仍由 collision 決定。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shapely.geometry import Polygon

from src.design.collision.furniture_engine import FurnitureCollisionEngine
from src.design.collision.placement_optimizer import (
    FurniturePlacementOptimizer,
    PlacementWeights,
)
from src.design.layout_generator import HouseBrief, generate_floor_plan
from src.drafting.fixtures import FixturePlacement

ALL_KEYS = ("wall_distance", "window_distance", "walkway",
            "symmetry", "room_usability", "constraint", "pair_constraint")


def _spec():
    return generate_floor_plan(
        HouseBrief(site_width=20000, site_depth=14000, bedrooms=3))


def _room(spec, kind):
    return next(r for r in spec.rooms if r.kind == kind)


def _rect(room):
    xs = [p[0] for p in room.points]
    ys = [p[1] for p in room.points]
    return min(xs), min(ys), max(xs), max(ys)


def _weighted(scores, w):
    """把一組 score 依權重加權平均——與 place() 內的算法同式。"""
    wmap = w.as_map()
    return sum(scores[k] * wmap[k] for k in wmap) / sum(wmap.values())


# ── 1 & 2:constraint 好→總分升,差→總分降 ───────────────────────────────
def test_good_constraint_raises_total_bad_lowers_it():
    """★ 其他分數相同、只有 constraint 不同時:好的總分 > 差的總分。"""
    other = {k: 50.0 for k in ALL_KEYS}
    good = {**other, "constraint": 100.0}
    bad = {**other, "constraint": 0.0}
    w = PlacementWeights()                       # constraint 預設權重 0.20
    assert _weighted(good, w) > _weighted(other, w) > _weighted(bad, w)


# ── 3:collision 硬閘門不可被 constraint 覆蓋 ────────────────────────────
def test_collision_invalid_is_never_overridden_by_constraint():
    """★ 撞牆/出界的候選,constraint 再高也不評分、不入選。"""
    spec = _spec()
    opt = FurniturePlacementOptimizer(spec)
    room = _room(spec, "bedroom")
    room_poly = Polygon(room.points)
    # 擺到房間外:collision 直接判 invalid
    outside = FixturePlacement("wardrobe", (10_000_000, 10_000_000), 0.0)
    cand = opt._score(outside, room, room_poly, None)
    assert cand.valid is False
    assert cand.scores == {}                     # 沒進到軟評分,更沒有 constraint
    assert cand.reject_reason                     # 有淘汰原因(collision)


def test_place_best_is_collision_valid_even_with_dominant_constraint():
    """★ constraint 權重灌到爆,best 仍必須通過 collision(硬閘門優先)。"""
    spec = _spec()
    opt = FurniturePlacementOptimizer(spec)
    eng = FurnitureCollisionEngine(spec)
    heavy = PlacementWeights(wall_distance=0.0, window_distance=0.0,
                             walkway=0.0, symmetry=0.0, room_usability=0.0,
                             constraint=1000.0)
    for kind, name in (("bedroom", "wardrobe"), ("living", "sofa3")):
        res = opt.place(name, _room(spec, kind), weights=heavy)
        assert res.found
        assert eng.check(res.best.placement()).valid


def test_all_invalid_room_stays_unfound_regardless_of_constraint():
    """塞不下的房間(車放浴室),constraint 權重再大也 found False。"""
    spec = generate_floor_plan(
        HouseBrief(site_width=16000, site_depth=14000, bedrooms=3))
    opt = FurniturePlacementOptimizer(spec)
    res = opt.place("car", _room(spec, "bathroom"),
                    weights=PlacementWeights(constraint=1000.0))
    assert res.valid_candidates == 0 and res.found is False


# ── 4:權重=0 → constraint 不影響總分 ────────────────────────────────────
def test_zero_weight_makes_constraint_irrelevant():
    """★ constraint 權重=0:constraint 分數怎麼變,總分都不變。"""
    w0 = PlacementWeights(constraint=0.0)
    other = {k: 40.0 for k in ALL_KEYS}
    a = {**other, "constraint": 0.0}
    b = {**other, "constraint": 100.0}
    assert abs(_weighted(a, w0) - _weighted(b, w0)) < 1e-9


def test_zero_weight_place_matches_pre_constraint_choice():
    """constraint 權重=0 時,選出的擺位與『沒有 constraint 那一項』完全一致。"""
    spec = _spec()
    opt = FurniturePlacementOptimizer(spec)
    room = _room(spec, "bedroom")
    # 只留其餘五項(constraint=0),best 的總分只由五項決定
    w = PlacementWeights(constraint=0.0)
    res = opt.place("wardrobe", room, weights=w)
    room_poly = Polygon(room.points)
    best_five = -1.0
    wmap = {k: v for k, v in w.as_map().items() if k != "constraint"}
    wsum = sum(wmap.values())
    for placement in opt.candidates("wardrobe", room):
        c = opt._score(placement, room, room_poly, None)
        if c.valid:
            best_five = max(best_five,
                            sum(c.scores[k] * wmap[k] for k in wmap) / wsum)
    assert abs(res.best.total - best_five) < 1e-6


# ── 5:權重獨大 → constraint 完全主導排名 ───────────────────────────────
def test_constraint_weight_dominates_ranking():
    """★ 只有 constraint 有權重時,best 的 constraint 分數 = 合法候選中的最大值。"""
    spec = _spec()
    opt = FurniturePlacementOptimizer(spec)
    room = _room(spec, "bedroom")
    room_poly = Polygon(room.points)
    only = PlacementWeights(wall_distance=0.0, window_distance=0.0,
                            walkway=0.0, symmetry=0.0, room_usability=0.0,
                            constraint=1.0, pair_constraint=0.0)
    best_constraint = -1.0
    for placement in opt.candidates("wardrobe", room):
        c = opt._score(placement, room, room_poly, None)
        if c.valid:
            best_constraint = max(best_constraint, c.scores["constraint"])
    res = opt.place("wardrobe", room, weights=only)
    # 權重全壓在 constraint:total == constraint 分數 == 最大 constraint
    assert abs(res.best.total - best_constraint) < 1e-6
    assert abs(res.best.scores["constraint"] - best_constraint) < 1e-6


# ── API / Report ──────────────────────────────────────────────────────────
def test_result_exposes_constraint_score_and_report_lists_it():
    """★ PlacementResult 有 constraint_score;報告/序列化都含 constraint。"""
    spec = _spec()
    res = FurniturePlacementOptimizer(spec).place(
        "wardrobe", _room(spec, "bedroom"))
    assert res.found
    # 結果層有 constraint_score,且 = best 的 constraint 分數
    assert abs(res.constraint_score - res.best.scores["constraint"]) < 1e-6
    assert 0.0 <= res.constraint_score <= 100.0
    d = res.to_dict()
    assert "constraint_score" in d
    assert "constraint" in d["best"]["scores"]
    assert "constraint" in res.summary()          # debug 輸出看得到
    # 六項軟指標都在 0~100
    for k in ALL_KEYS:
        assert 0.0 <= res.best.scores[k] <= 100.0


def test_weights_dataclass_carries_constraint_default_020():
    """★ constraint 權重不寫死:預設 0.20,且序列化含此欄。"""
    w = PlacementWeights()
    assert w.constraint == 0.20
    assert w.as_map()["constraint"] == 0.20
    assert w.to_dict()["constraint"] == 0.20
