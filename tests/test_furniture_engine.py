"""FurnitureCollisionEngine 測試(v0.7 Phase 6-1)。

三個重點:
  * **零誤判**——已合格的圖,每一件既有家具都必須是 valid。
  * **每種 reason 都抓得到**——注入對應缺陷必須回報正確的 reason
    (只測零誤判的話,一個永遠回 valid 的壞引擎也會通過)。
  * **唯讀**——check() 不得改動 spec。
"""
import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shapely.geometry import Polygon

from src.design.collision.furniture_engine import (
    REASON_COLUMN,
    REASON_DOOR,
    REASON_FURNITURE,
    REASON_STAIR,
    REASON_VOID,
    REASON_WALL,
    REASON_WINDOW,
    TALL_FIXTURES,
    CollisionResult,
    FurnitureCollisionEngine,
)
from src.design.collision.geometry import window_obstacles
from src.design.collision.obstacle import WINDOW
from src.design.layout_generator import (
    HouseBrief,
    generate_floor_plan,
    generate_house_public,
    generate_house_upper,
)
from src.drafting.fixtures import FixturePlacement

_SPECS = [
    lambda: generate_floor_plan(
        HouseBrief(site_width=16000, site_depth=14000, bedrooms=3)),
    lambda: generate_floor_plan(
        HouseBrief(site_width=20000, site_depth=14000, bedrooms=3)),
    lambda: generate_house_upper(
        HouseBrief(site_width=26000, site_depth=16000, bedrooms=3, seed=1)),
    lambda: generate_house_public(
        HouseBrief(site_width=22000, site_depth=14000, bedrooms=3, seed=1)),
]


def _spec():
    return _SPECS[0]()


# ── 零誤判 ────────────────────────────────────────────────────────────────
def test_existing_furniture_is_all_valid():
    """★ 核心:已合格圖面的既有家具,每一件都必須通過事前查詢。

    這同時證明「事前查詢」與 v0.6「事後修復」對同一張圖的判斷一致。"""
    for make in _SPECS:
        spec = make()
        for tag, res in FurnitureCollisionEngine(spec).check_existing():
            assert res.valid, f"{tag}: {res}"


def test_check_does_not_mutate_spec():
    """★ 唯讀:查詢不得改動 spec。"""
    for make in _SPECS:
        spec = make()
        before = copy.deepcopy((
            [r.points for r in spec.rooms],
            [(getattr(f, "name", "counter"), getattr(f, "insert", None))
             for f in spec.fixtures],
        ))
        eng = FurnitureCollisionEngine(spec)
        eng.check(FixturePlacement("wardrobe", (1000.0, 1000.0), 0))
        eng.check_existing()
        after = (
            [r.points for r in spec.rooms],
            [(getattr(f, "name", "counter"), getattr(f, "insert", None))
             for f in spec.fixtures],
        )
        assert before == after


# ── CollisionResult 介面 ──────────────────────────────────────────────────
def test_result_shape_and_bool():
    eng = FurnitureCollisionEngine(_spec())
    ok = eng.check(next(f for f in eng.spec.fixtures
                        if isinstance(f, FixturePlacement)),
                   ignore=next(f for f in eng.spec.fixtures
                               if isinstance(f, FixturePlacement)))
    assert isinstance(ok, CollisionResult)
    assert ok.valid is True and ok.reason == "" and ok.overlap_area == 0.0
    assert bool(ok) is True and str(ok) == "OK"


def test_result_follows_json_convention():
    import json
    eng = FurnitureCollisionEngine(_spec())
    res = eng.check(FixturePlacement("wardrobe", (-50000.0, -50000.0), 0))
    d = res.to_dict()
    assert json.loads(res.to_json()) == d
    assert set(d) == {"valid", "reason", "overlap_area",
                      "obstacle_tag", "detail"}
    assert "\\u" not in res.to_json()


def test_can_place_helper():
    eng = FurnitureCollisionEngine(_spec())
    res = eng.can_place("wardrobe", (-50000.0, -50000.0), 0)
    assert isinstance(res, CollisionResult) and not res.valid


# ── 每種 reason 都要抓得到 ────────────────────────────────────────────────
def test_outside_any_room_is_wall_reason():
    """放到建築外 → 不在任何房間內,回 wall。"""
    eng = FurnitureCollisionEngine(_spec())
    res = eng.check(FixturePlacement("bed_double", (-50000.0, -50000.0), 0))
    assert not res.valid and res.reason == REASON_WALL


def test_crossing_room_boundary_is_wall_reason():
    """把家具推到跨越房間邊界 → 回 wall。"""
    spec = _spec()
    eng = FurnitureCollisionEngine(spec)
    sofa = next(f for f in spec.fixtures
                if isinstance(f, FixturePlacement) and f.name == "sofa3")
    moved = FixturePlacement(sofa.name, (sofa.insert[0] - 1500.0,
                                         sofa.insert[1]), sofa.rotation)
    res = eng.check(moved, ignore=sofa)
    assert not res.valid
    assert res.reason in (REASON_WALL, REASON_FURNITURE, REASON_DOOR)


def test_overlapping_existing_furniture_is_furniture_reason():
    """疊在既有家具上(且不 ignore 它)→ 回 furniture。"""
    spec = _spec()
    eng = FurnitureCollisionEngine(spec)
    bed = next(f for f in spec.fixtures
               if isinstance(f, FixturePlacement) and f.name.startswith("bed"))
    res = eng.check(FixturePlacement("bed_double", bed.insert, bed.rotation))
    assert not res.valid and res.reason == REASON_FURNITURE
    assert res.overlap_area > 100 and res.obstacle_tag


def test_ignore_lets_a_fixture_pass_its_own_position():
    """ignore 自己時,原位就不該被自己擋住。"""
    spec = _spec()
    eng = FurnitureCollisionEngine(spec)
    bed = next(f for f in spec.fixtures
               if isinstance(f, FixturePlacement) and f.name.startswith("bed"))
    assert eng.check(bed, ignore=bed).valid


def test_door_swing_is_detected():
    """擺在門迴轉方塊正中央 → 回 door_swing。"""
    spec = _spec()
    eng = FurnitureCollisionEngine(spec)
    for d in eng.doors:
        c = d.poly.centroid
        res = eng.check(FixturePlacement("coffee_table", (c.x, c.y), 0))
        if res.reason == REASON_DOOR:
            assert not res.valid and res.overlap_area > 100
            return
    raise AssertionError("找不到會觸發 door_swing 的位置")


def test_stair_is_detected():
    """擺在梯段上 → 回 stair。"""
    spec = generate_house_upper(
        HouseBrief(site_width=26000, site_depth=16000, bedrooms=3, seed=1))
    eng = FurnitureCollisionEngine(spec)
    assert eng.stairs
    c = eng.stairs[0].poly.centroid
    res = eng.check(FixturePlacement("coffee_table", (c.x, c.y), 0))
    assert not res.valid and res.reason in (REASON_STAIR, REASON_WALL)


def test_void_is_detected():
    """擺進天井 → 回 void(天井不在任何房間內,故 wall 也可接受)。"""
    spec = generate_house_upper(
        HouseBrief(site_width=20000, site_depth=20000, bedrooms=3, seed=1))
    eng = FurnitureCollisionEngine(spec)
    assert eng.voids
    c = eng.voids[0].poly.centroid
    res = eng.check(FixturePlacement("coffee_table", (c.x, c.y), 0))
    assert not res.valid and res.reason in (REASON_VOID, REASON_WALL)


def test_column_is_detected():
    """在房間內插一根獨立柱,家具擺柱心 → 回 column。"""
    spec = _spec()
    room = next(r for r in spec.rooms if r.kind == "living")
    c = Polygon(room.points).centroid
    spec.column_centers = [(c.x, c.y)]
    spec.fixtures.clear()
    eng = FurnitureCollisionEngine(spec)
    res = eng.check(FixturePlacement("coffee_table", (c.x, c.y), 0))
    assert not res.valid and res.reason == REASON_COLUMN


# ── 窗前淨空(這一項最容易做錯)────────────────────────────────────────
def test_window_obstacles_are_built():
    for make in _SPECS:
        wins = window_obstacles(make())
        assert wins and all(w.kind == WINDOW and not w.movable for w in wins)


def test_window_check_only_applies_to_tall_furniture():
    """★ 床/沙發靠窗是正確擺法,不得被擋;只有高家具受窗前淨空約束。"""
    assert "wardrobe" in TALL_FIXTURES
    assert "bed_double" not in TALL_FIXTURES
    assert "sofa3" not in TALL_FIXTURES
    assert "counter" not in TALL_FIXTURES        # 水槽對窗是標準做法

    spec = _spec()
    eng = FurnitureCollisionEngine(spec)
    assert eng.windows
    w = eng.windows[0]
    c = w.poly.centroid
    low = eng.check(FixturePlacement("bed_double", (c.x, c.y), 0))
    assert low.reason != REASON_WINDOW           # 矮家具不受此規則


def test_tall_furniture_blocking_a_window_is_detected():
    """高家具正對窗心 → 回 window_clearance。"""
    spec = _spec()
    spec.fixtures.clear()                        # 隔離,避免先撞到別的家具
    eng = FurnitureCollisionEngine(spec)
    for w in eng.windows:
        c = w.poly.centroid
        res = eng.check(FixturePlacement("wardrobe", (c.x, c.y), 0))
        if res.reason == REASON_WINDOW:
            assert not res.valid and res.overlap_area > 0
            return
    raise AssertionError("找不到會觸發 window_clearance 的窗")


def test_grazing_a_window_is_not_blocking():
    """★ 擦到窗前淨空一點點不算擋窗——實測 28 件衣櫃只擦到 9~42mm 寬,
    用絕對門檻會全部誤判。"""
    for make in _SPECS:
        spec = make()
        for tag, res in FurnitureCollisionEngine(spec).check_existing():
            assert res.reason != REASON_WINDOW, f"{tag} 被誤判成擋窗:{res}"


def test_window_check_can_be_disabled():
    spec = _spec()
    spec.fixtures.clear()
    eng_on = FurnitureCollisionEngine(spec)
    eng_off = FurnitureCollisionEngine(spec, window_check=False)
    for w in eng_on.windows:
        c = w.poly.centroid
        fx = FixturePlacement("wardrobe", (c.x, c.y), 0)
        if eng_on.check(fx).reason == REASON_WINDOW:
            assert eng_off.check(fx).reason != REASON_WINDOW
            return
    raise AssertionError("找不到會觸發 window_clearance 的窗")
