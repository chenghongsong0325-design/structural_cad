"""Furniture Constraint 測試(v0.7 Phase 6-4)。

重點:
  * 每種家具都有一筆偏好,三個欄位齊全、值合理(靠牆家具靠牆、自由站立不靠牆)。
  * facing_of / wall_against 幾何正確,且互為反向(靠南牆→朝北)。
  * clearance_zone 真的落在家具正前方、深度=minimum_clearance。
  * evaluate_constraint 三項各自可獨立判對錯,且對真實生成的圖唯讀。
  * Report 遵循 to_dict/to_json 序列化契約。
"""
import copy
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shapely.geometry import Polygon

from src.design.collision.furniture_constraint import (
    ANY_FACING,
    ANY_WALL,
    CARDINALS,
    EAST,
    FREESTANDING,
    FURNITURE_CONSTRAINTS,
    NORTH,
    SOUTH,
    WEST,
    ConstraintResult,
    FurnitureConstraint,
    clearance_zone,
    evaluate_constraint,
    facing_of,
    get_constraint,
    wall_against,
)
from src.design.collision.furniture_engine import FurnitureCollisionEngine
from src.design.layout_generator import HouseBrief, generate_floor_plan
from src.drafting.fixtures import (
    FIXTURE_SIZES,
    FixturePlacement,
    fixture_footprint,
)
from src.drafting.fixtures import _CENTER_ORIGIN as CENTER_ORIGIN


def _spec():
    return generate_floor_plan(
        HouseBrief(site_width=20000, site_depth=14000, bedrooms=3))


def _room(spec, kind):
    return next(r for r in spec.rooms if r.kind == kind)


class _Room:
    """輕量假房間:一個 (x0,y0)-(x1,y1) 的矩形,足夠驗幾何。"""

    def __init__(self, x0, y0, x1, y1):
        self.points = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
        self.name = "測試房"
        self.kind = "bedroom"


# ── 偏好資料本身 ──────────────────────────────────────────────────────────
def test_every_fixture_has_a_constraint():
    """★ 每種畫得出來的家具都要有一筆偏好(不能漏)。"""
    for name in FIXTURE_SIZES:
        c = get_constraint(name)
        assert isinstance(c, FurnitureConstraint) and c.name == name


def test_constraint_has_all_three_fields_with_sane_values():
    """★ 三個欄位齊全:牆是方位集合、淨空非負、朝向是方位集合。"""
    for name, c in FURNITURE_CONSTRAINTS.items():
        assert c.preferred_wall <= ANY_WALL, name
        assert c.preferred_orientation <= ANY_FACING and c.preferred_orientation
        assert c.minimum_clearance >= 0.0, name


def test_wall_furniture_hugs_wall_freestanding_does_not():
    """★ 靠牆家具 preferred_wall 非空;中心原點家具(桌/茶几/汽車)不靠牆。"""
    for name, c in FURNITURE_CONSTRAINTS.items():
        if name in CENTER_ORIGIN:
            assert c.freestanding, f"{name} 應為自由站立"
            assert c.preferred_wall == FREESTANDING
        else:
            assert not c.freestanding, f"{name} 應靠牆"
            assert c.preferred_wall


def test_get_constraint_unknown_returns_permissive_default():
    """未登錄家具回寬鬆預設(任意牆、無淨空要求),不丟例外。"""
    c = get_constraint("piano")
    assert c.name == "piano" and c.preferred_wall == ANY_WALL
    assert c.minimum_clearance == 0.0


# ── 幾何:朝向 / 靠牆 ─────────────────────────────────────────────────────
def test_facing_of_matches_rotation_convention():
    """★ 0°→北 90°→西 180°→南 270°→東(與 fixtures.py 一致)。"""
    assert facing_of(0.0) == NORTH
    assert facing_of(90.0) == WEST
    assert facing_of(180.0) == SOUTH
    assert facing_of(270.0) == EAST


def test_wall_against_finds_the_backing_wall():
    """★ 貼牆邊原點落在哪面牆就回哪面;南牆↔朝北互為反向。"""
    room = _Room(0, 0, 4000, 4000)
    assert wall_against(FixturePlacement("wardrobe", (2000, 0), 0.0), room) == SOUTH
    assert wall_against(FixturePlacement("wardrobe", (2000, 4000), 180.0), room) == NORTH
    assert wall_against(FixturePlacement("wardrobe", (4000, 2000), 90.0), room) == EAST
    assert wall_against(FixturePlacement("wardrobe", (0, 2000), 270.0), room) == WEST
    # 靠南牆(0°)→ 正面朝北:靠牆與朝向互為反向
    p = FixturePlacement("wardrobe", (2000, 0), 0.0)
    assert wall_against(p, room) == SOUTH and facing_of(p.rotation) == NORTH


def test_wall_against_none_for_freestanding_and_far():
    room = _Room(0, 0, 4000, 4000)
    assert wall_against(FixturePlacement("table4", (2000, 2000), 0.0), room) is None
    # 貼牆家具擺在房間正中央(離每面牆都 >tol)→ 不算靠任何牆
    assert wall_against(FixturePlacement("wardrobe", (2000, 2000), 0.0), room) is None


# ── 幾何:淨空區 ─────────────────────────────────────────────────────────
def test_clearance_zone_is_in_front_at_correct_depth():
    """★ 靠牆家具的淨空區在正前方(footprint 外緣起),深度=minimum_clearance。"""
    p = FixturePlacement("wardrobe", (2000, 0), 0.0)      # 靠南牆,朝北(+Y)
    c = get_constraint("wardrobe")
    zone = clearance_zone(p, c)
    fp = Polygon(fixture_footprint(p))
    w, d = FIXTURE_SIZES["wardrobe"]
    # 淨空區在家具之外(不重疊 footprint),且緊貼其前緣
    assert zone.intersection(fp).area < 1.0
    assert zone.distance(fp) < 1e-6
    ys = [pt[1] for pt in zone.exterior.coords]
    assert abs(min(ys) - d) < 1e-6                        # 從前緣 d 起
    assert abs((max(ys) - min(ys)) - c.minimum_clearance) < 1e-6


def test_clearance_zone_empty_when_no_clearance_required():
    p = FixturePlacement("nightstand", (200, 0), 0.0)
    z0 = clearance_zone(p, FurnitureConstraint("nightstand", ANY_WALL, 0.0))
    assert z0.is_empty


# ── evaluate:三項可獨立判對錯 ────────────────────────────────────────────
def test_evaluate_passes_for_a_proper_wall_placement():
    """靠牆、朝室內、前方留足淨空 → 三項全過。"""
    room = _Room(0, 0, 5000, 5000)
    res = evaluate_constraint(FixturePlacement("wardrobe", (2500, 0), 0.0), room)
    assert res.wall_ok and res.orientation_ok and res.clearance_ok
    assert res.satisfied and bool(res) is True


def test_evaluate_flags_freestanding_shoved_to_wall():
    """★ 自由站立家具(餐桌)靠牆時,四面淨空環頂出房間 → clearance_ok False;
    擺中央則三項全過。中心原點家具沒有「背貼牆」的邊,故偏好用淨空環表達。"""
    room = _Room(0, 0, 6000, 6000)
    against = evaluate_constraint(FixturePlacement("table4", (60, 3000), 0.0), room)
    assert against.wall_ok is True                        # 中心原點無靠牆邊
    assert against.clearance_ok is False and against.satisfied is False
    center = evaluate_constraint(FixturePlacement("table4", (3000, 3000), 0.0), room)
    assert center.satisfied is True


def test_evaluate_flags_insufficient_clearance():
    """★ 淨空區超出房間(緊貼對牆放,前方沒空間)→ clearance_ok False。"""
    # 房間只比衣櫃深一點:靠南牆放,前方淨空(600)會頂出北牆
    w, d = FIXTURE_SIZES["wardrobe"]
    room = _Room(0, 0, 3000, d + 200)                     # 前方只剩 200 < 600
    res = evaluate_constraint(FixturePlacement("wardrobe", (1500, 0), 0.0), room)
    assert res.clearance_ok is False and res.satisfied is False


def test_evaluate_clearance_blocked_by_other_furniture_with_engine():
    """★ 給了 engine:正前方淨空被別的家具佔住 → clearance_ok False。"""
    spec = _spec()
    bedroom = _room(spec, "bedroom")
    x0, y0, x1, y1 = (min(p[0] for p in bedroom.points),
                      min(p[1] for p in bedroom.points),
                      max(p[0] for p in bedroom.points),
                      max(p[1] for p in bedroom.points))
    # 一件靠南牆的衣櫃,另一件正好擋在它前方淨空裡
    ward = FixturePlacement("wardrobe", ((x0 + x1) / 2, y0), 0.0)
    w, d = FIXTURE_SIZES["wardrobe"]
    blocker = FixturePlacement("bookshelf", ((x0 + x1) / 2, y0 + d + 100), 0.0)
    spec.fixtures.append(blocker)
    eng = FurnitureCollisionEngine(spec)
    res = evaluate_constraint(ward, bedroom, engine=eng)
    assert res.clearance_ok is False

    # 沒有擋路者時同一件衣櫃前方淨空是通的
    spec2 = _spec()
    room2 = _room(spec2, "bedroom")
    eng2 = FurnitureCollisionEngine(spec2)
    ok = evaluate_constraint(
        FixturePlacement("wardrobe",
                         ((x0 + x1) / 2, min(p[1] for p in room2.points)), 0.0),
        room2, engine=eng2)
    assert ok.clearance_ok is True


def test_evaluate_ignores_the_placement_itself_in_clearance():
    """淨空檢查要略過家具自己(自己不會擋自己的前方)。"""
    spec = _spec()
    bedroom = _room(spec, "bedroom")
    x0 = min(p[0] for p in bedroom.points)
    x1 = max(p[0] for p in bedroom.points)
    y0 = min(p[1] for p in bedroom.points)
    ward = FixturePlacement("wardrobe", ((x0 + x1) / 2, y0), 0.0)
    spec.fixtures.append(ward)                            # 這件就是 spec 裡的它
    eng = FurnitureCollisionEngine(spec)
    res = evaluate_constraint(ward, bedroom, engine=eng)
    assert res.clearance_ok is True                       # 沒把自己算成擋路


# ── 對真實生成的圖:唯讀 ─────────────────────────────────────────────────
def test_evaluate_does_not_mutate_spec():
    """★ 唯讀:evaluate 不得改動 spec。"""
    spec = _spec()
    before = copy.deepcopy(
        [(getattr(f, "name", "counter"), getattr(f, "insert", None))
         for f in spec.fixtures])
    eng = FurnitureCollisionEngine(spec)
    for fx in [f for f in spec.fixtures if isinstance(f, FixturePlacement)][:5]:
        room = next((r for r in spec.rooms
                     if Polygon(r.points).contains(
                         Polygon(fixture_footprint(fx)).centroid)), None)
        if room is not None:
            evaluate_constraint(fx, room, engine=eng)
    after = [(getattr(f, "name", "counter"), getattr(f, "insert", None))
             for f in spec.fixtures]
    assert before == after


# ── 序列化契約 ────────────────────────────────────────────────────────────
def test_reports_follow_json_convention():
    room = _Room(0, 0, 5000, 5000)
    res = evaluate_constraint(FixturePlacement("wardrobe", (2500, 0), 0.0), room)
    for obj in (res, get_constraint("wardrobe")):
        assert json.loads(obj.to_json()) == obj.to_dict()
        assert "\\u" not in obj.to_json()
    d = res.to_dict()
    assert set(d) >= {"name", "satisfied", "wall_ok", "orientation_ok",
                      "clearance_ok", "wall", "facing"}
    cd = get_constraint("wardrobe").to_dict()
    assert isinstance(cd["preferred_wall"], list)         # set 已轉 sorted list
    assert isinstance(cd["preferred_orientation"], list)
