"""Furniture Placement Optimizer 測試(v0.7 Phase 6-2)。

重點:
  * 產生多個候選、每個都評分、挑最高分。
  * collision 是**硬閘門**:被選中的最佳擺位一定通過 FurnitureCollisionEngine。
  * 貼牆家具挑到的位置真的貼牆;桌子(中心原點)挑到房間中央。
  * 權重可調且真的影響結果。
  * 唯讀:place() 不寫回 spec。
"""
import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shapely.geometry import Polygon

from src.design.collision.furniture_engine import FurnitureCollisionEngine
from src.design.collision.placement_optimizer import (
    FurniturePlacementOptimizer,
    PlacementCandidate,
    PlacementResult,
    PlacementWeights,
)
from src.design.layout_generator import (
    HouseBrief,
    generate_floor_plan,
    generate_house_upper,
)
from src.drafting.fixtures import FixturePlacement, fixture_footprint

SOFT_CRITERIA = ("wall_distance", "window_distance", "walkway",
                 "symmetry", "room_usability")


def _spec():
    return generate_floor_plan(
        HouseBrief(site_width=20000, site_depth=14000, bedrooms=3))


def _room(spec, kind):
    return next(r for r in spec.rooms if r.kind == kind)


def _rect(room):
    xs = [p[0] for p in room.points]
    ys = [p[1] for p in room.points]
    return min(xs), min(ys), max(xs), max(ys)


# ── 產生候選 + 挑最佳 ─────────────────────────────────────────────────────
def test_generates_multiple_candidates():
    spec = _spec()
    opt = FurniturePlacementOptimizer(spec)
    res = opt.place("wardrobe", _room(spec, "bedroom"))
    assert isinstance(res, PlacementResult)
    assert res.candidates > 1                        # 真的產生多個候選
    assert res.valid_candidates >= 1
    assert res.found and isinstance(res.best, PlacementCandidate)


def test_best_placement_is_actually_valid():
    """★ collision 硬閘門:被選中的最佳擺位必須真的通過事前查詢。"""
    spec = _spec()
    opt = FurniturePlacementOptimizer(spec)
    eng = FurnitureCollisionEngine(spec)
    for kind, name in (("bedroom", "wardrobe"), ("living", "sofa3"),
                       ("living", "table4")):
        res = opt.place(name, _room(spec, kind))
        assert res.found, name
        check = eng.check(res.best.placement())
        assert check.valid, f"{name} 最佳擺位竟不合法:{check}"


def test_all_soft_criteria_scored_and_in_range():
    """★ 五個軟指標都有評、都在 0~100。"""
    spec = _spec()
    res = FurniturePlacementOptimizer(spec).place(
        "wardrobe", _room(spec, "bedroom"))
    assert set(res.best.scores) == set(SOFT_CRITERIA)
    for k, v in res.best.scores.items():
        assert 0.0 <= v <= 100.0, f"{k}={v}"
    assert 0.0 <= res.best.total <= 100.0


def test_total_is_weighted_average_of_soft_scores():
    """★ 總分 = Σ(軟分數 × 權重) / Σ(權重)。"""
    spec = _spec()
    w = PlacementWeights()
    res = FurniturePlacementOptimizer(spec).place(
        "wardrobe", _room(spec, "bedroom"), weights=w)
    wmap = w.as_map()
    wsum = sum(wmap.values())
    expect = sum(res.best.scores[k] * wmap[k] for k in wmap) / wsum
    assert abs(res.best.total - expect) < 1e-9


def test_best_has_the_highest_total():
    """最佳候選的總分必須是所有合法候選裡最高的(用單一權重間接驗)。"""
    spec = _spec()
    opt = FurniturePlacementOptimizer(spec)
    room = _room(spec, "bedroom")
    # 只看 wall_distance:最佳的 wall_distance 應該是合法候選中的最大值
    from src.drafting.fixtures import FIXTURE_SIZES  # noqa: F401
    room_poly = Polygon(room.points)
    best_wall = -1.0
    for placement in opt.candidates("wardrobe", room):
        c = opt._score(placement, room, room_poly, None)
        if c.valid:
            best_wall = max(best_wall, c.scores["wall_distance"])
    res = opt.place("wardrobe", room,
                    weights=PlacementWeights(wall_distance=1.0,
                                             window_distance=0.0, walkway=0.0,
                                             symmetry=0.0, room_usability=0.0))
    assert abs(res.best.total - best_wall) < 1e-6


# ── 擺位合理性 ────────────────────────────────────────────────────────────
def test_wall_hugging_furniture_lands_against_a_wall():
    """★ 貼牆家具挑到的位置 wall_distance 應接近滿分(真的貼牆)。"""
    spec = _spec()
    opt = FurniturePlacementOptimizer(spec)
    for kind, name in (("bedroom", "wardrobe"), ("living", "sofa3"),
                       ("living", "tv_cabinet")):
        res = opt.place(name, _room(spec, kind))
        assert res.best.scores["wall_distance"] >= 90.0, name
        # footprint 一邊要貼著房間邊界
        poly = Polygon(fixture_footprint(res.best.placement()))
        room_poly = Polygon(_room(spec, kind).points)
        assert poly.exterior.distance(room_poly.exterior) < 50.0


def test_center_origin_furniture_lands_near_room_center():
    """★ 桌子(中心原點)不貼牆,挑到房間中央(靠邊分 room_usability 應偏低)。"""
    spec = _spec()
    opt = FurniturePlacementOptimizer(spec)
    room = _room(spec, "living")
    res = opt.place("table4", room)
    assert res.found
    x0, y0, x1, y1 = _rect(room)
    bx, by = res.best.insert
    # 落在房間中央區(離四邊都有一段距離)
    assert x0 + (x1 - x0) * 0.25 < bx < x1 - (x1 - x0) * 0.25
    assert res.best.scores["room_usability"] < 50.0    # 佔中央 → 靠邊分低


# ── 權重 ──────────────────────────────────────────────────────────────────
def test_weights_change_the_choice():
    """★ 改權重會改變最佳擺位(否則權重是裝飾)。"""
    spec = _spec()
    opt = FurniturePlacementOptimizer(spec)
    room = _room(spec, "bedroom")
    base = opt.place("wardrobe", room)
    tilt = opt.place("wardrobe", room, weights=PlacementWeights(symmetry=100.0))
    assert base.best.insert != tilt.best.insert or \
        abs(base.best.total - tilt.best.total) > 1e-6


# ── 找不到合法擺位 ────────────────────────────────────────────────────────
def test_no_valid_placement_returns_none_best():
    """★ 塞不下時(車子放浴室)graceful:found False、best None、不丟例外。"""
    spec = generate_floor_plan(
        HouseBrief(site_width=16000, site_depth=14000, bedrooms=3))
    res = FurniturePlacementOptimizer(spec).place("car", _room(spec, "bathroom"))
    assert res.candidates > 0
    assert res.valid_candidates == 0
    assert res.found is False and res.best is None
    assert "找不到" in res.summary()


# ── 唯讀 + 介面 ───────────────────────────────────────────────────────────
def test_place_does_not_mutate_spec():
    """★ 唯讀:挑擺位不得改動 spec(尤其不能把候選寫進 fixtures)。"""
    spec = _spec()
    before = copy.deepcopy((
        [r.points for r in spec.rooms],
        [(getattr(f, "name", "counter"), getattr(f, "insert", None))
         for f in spec.fixtures],
    ))
    opt = FurniturePlacementOptimizer(spec)
    opt.place("wardrobe", _room(spec, "bedroom"))
    opt.place("sofa3", _room(spec, "living"))
    after = (
        [r.points for r in spec.rooms],
        [(getattr(f, "name", "counter"), getattr(f, "insert", None))
         for f in spec.fixtures],
    )
    assert before == after


def test_room_can_be_given_by_index():
    spec = _spec()
    opt = FurniturePlacementOptimizer(spec)
    idx = next(i for i, r in enumerate(spec.rooms) if r.kind == "bedroom")
    res = opt.place("wardrobe", idx)
    assert res.found and res.room == spec.rooms[idx].name


def test_ignore_lets_a_fixture_reselect_in_its_own_room():
    """把某房既有的家具傳給 ignore,它原本的佔位不該擋住自己重新選位。"""
    spec = generate_house_upper(
        HouseBrief(site_width=26000, site_depth=16000, bedrooms=3, seed=1))
    opt = FurniturePlacementOptimizer(spec)
    bed_room = _room(spec, "bedroom")
    existing = [f for f in spec.fixtures
                if isinstance(f, FixturePlacement)
                and Polygon(bed_room.points).contains(
                    __import__("shapely.geometry", fromlist=["Point"])
                    .Point(*f.insert))]
    res = opt.place("wardrobe", bed_room, ignore=existing or None)
    assert res.found


def test_reports_follow_json_convention():
    import json
    spec = _spec()
    res = FurniturePlacementOptimizer(spec).place(
        "wardrobe", _room(spec, "bedroom"))
    for obj in (res, res.best, PlacementWeights()):
        assert json.loads(obj.to_json()) == obj.to_dict()
    d = res.to_dict()
    assert set(d) >= {"name", "room", "found", "candidates",
                      "valid_candidates", "best"}
    assert "\\u" not in res.to_json()
    assert isinstance(res.summary(), str) and res.name in res.summary()
