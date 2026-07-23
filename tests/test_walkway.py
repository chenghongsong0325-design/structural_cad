"""Walkway Analyzer 測試(v0.7 Phase 6-3)。

重點:
  * 掃描線量的淨寬**不超過**外接矩形短邊(L 形要被修正,不被高估)。
  * 主/次走道分類正確(主走道服務最多房間)。
  * blocked 抓得到(注入家具塞窄走道),擦邊不誤判。
  * 唯讀,且沒有走道的樓層 graceful 回空。
"""
import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shapely.geometry import Polygon

from src.design.layout_generator import (
    HouseBrief,
    generate_floor_plan,
    generate_house_public,
)
from src.design.walkway import (
    MIN_WALKWAY_WIDTH,
    WalkwayReport,
    WalkwaySegment,
    _scan_min_width,
    analyze_walkways,
)
from src.drafting.fixtures import FixturePlacement

# 這些單層案例會配走道(三房以上、基地夠大)。
_CORRIDOR_SPECS = [
    lambda: generate_floor_plan(
        HouseBrief(site_width=16000, site_depth=14000, bedrooms=3)),
    lambda: generate_floor_plan(
        HouseBrief(site_width=20000, site_depth=14000, bedrooms=3)),
    lambda: generate_floor_plan(
        HouseBrief(site_width=22000, site_depth=15000, bedrooms=4)),
]
# 小宅沒有獨立走道。
_NO_CORRIDOR = lambda: generate_floor_plan(  # noqa: E731
    HouseBrief(site_width=12000, site_depth=11000, bedrooms=1))


def _corridor_spec():
    for make in _CORRIDOR_SPECS:
        spec = make()
        if any(r.kind == "corridor" for r in spec.rooms):
            return spec
    raise AssertionError("找不到含走道的案例")


def _corridor_room(spec):
    return next(r for r in spec.rooms if r.kind == "corridor")


def _bbox_short(room):
    xs = [p[0] for p in room.points]
    ys = [p[1] for p in room.points]
    return min(max(xs) - min(xs), max(ys) - min(ys))


# ── 幾何:掃描線 ──────────────────────────────────────────────────────────
def test_scan_width_of_plain_rectangle():
    """矩形走道:掃描寬 = 短邊。"""
    poly = Polygon([(0, 0), (8000, 0), (8000, 1200), (0, 1200)])
    assert abs(_scan_min_width(poly, poly.bounds) - 1200) < 100


def test_scan_width_of_L_shape_is_the_arm_width():
    """L 形走道:掃描寬 = 臂寬(不是外接矩形短邊)。"""
    # 一個 5000×5000 外接、但兩臂都只有 1000 寬的 L
    poly = Polygon([(0, 0), (1000, 0), (1000, 4000), (5000, 4000),
                    (5000, 5000), (0, 5000)])
    bbox_short = min(poly.bounds[2] - poly.bounds[0],
                     poly.bounds[3] - poly.bounds[1])
    w = _scan_min_width(poly, poly.bounds)
    assert abs(w - 1000) < 150            # 量到臂寬 1000
    assert w < bbox_short                 # 而且比外接矩形短邊(5000)小很多


# ── 報告基本盤 ────────────────────────────────────────────────────────────
def test_walkway_report_on_corridor_floor():
    spec = _corridor_spec()
    rep = analyze_walkways(spec)
    assert isinstance(rep, WalkwayReport)
    assert rep.has_walkway
    assert rep.main is not None
    for w in rep.walkways:
        assert w.width > 0 and w.length > 0 and w.serves > 0
        assert w.role in ("main", "secondary")
    assert rep.min_width == min(w.width for w in rep.walkways)


def test_no_corridor_floor_returns_empty():
    """★ 小宅沒有獨立走道 → graceful 回空,ok=True(沒有走道可擋)。"""
    spec = _NO_CORRIDOR()
    if not any(r.kind == "corridor" for r in spec.rooms):
        rep = analyze_walkways(spec)
        assert not rep.has_walkway
        assert rep.walkways == [] and rep.min_width is None
        assert rep.ok
        assert "無獨立走道" in rep.summary()


def test_scan_width_never_exceeds_bbox_short_side():
    """★ 掃描淨寬不得高估:恆 ≤ 外接矩形短邊。"""
    for make in _CORRIDOR_SPECS:
        spec = make()
        rep = analyze_walkways(spec)
        for w in rep.walkways:
            room = next(r for r in spec.rooms if r.name == w.name)
            assert w.raw_width <= _bbox_short(room) + 1.0


# ── 主/次分類 ─────────────────────────────────────────────────────────────
def test_main_walkway_serves_the_most_rooms():
    """★ 主走道服務的房間數 ≥ 任何次走道。"""
    for make in _CORRIDOR_SPECS:
        spec = make()
        rep = analyze_walkways(spec)
        if not rep.has_walkway:
            continue
        main = rep.main
        assert main is not None
        for sec in rep.secondary:
            assert main.serves >= sec.serves


# ── blocked 偵測 ──────────────────────────────────────────────────────────
def test_clean_corridors_are_not_blocked():
    """★ 零誤判:實際生成的走道都不該被判 blocked。"""
    for make in _CORRIDOR_SPECS:
        rep = analyze_walkways(make())
        assert rep.blocked == [], rep.summary()
        assert rep.ok


def test_furniture_narrowing_a_corridor_is_blocked():
    """★ 注入一件塞進走道的家具,把淨寬壓到門檻以下 → blocked。"""
    spec = _corridor_spec()
    room = _corridor_room(spec)
    poly = Polygon(room.points)
    c = poly.centroid
    # 衣櫃 900×600,擺在走道中央:走道寬 1200,被吃掉 600 → 剩 600 < 750
    spec.fixtures.append(FixturePlacement("wardrobe", (c.x, c.y), 0))
    rep = analyze_walkways(spec)
    assert rep.blocked, rep.summary()
    blk = rep.blocked[0]
    assert blk.width < MIN_WALKWAY_WIDTH
    assert "障礙" in blk.block_reason
    assert not rep.ok


def test_scan_width_reflects_obstacle_clearance():
    """★ 淨寬 = 扣掉障礙後剩下的寬:小障礙留夠寬(不 block),大障礙塞窄(block)。

    用合成幾何確定性地驗機制,不依賴家具擺放的原點慣例。"""
    from shapely.geometry import box
    corridor = box(0, 0, 8000, 1200)                # 寬 1200 的走道
    # 靠一側長牆、只吃掉 400 深 → 留 800 > 750
    small = box(3000, 0, 3900, 400)
    free_small = corridor.difference(small)
    assert _scan_min_width(free_small, corridor.bounds) >= MIN_WALKWAY_WIDTH
    # 吃掉 600 深 → 留 600 < 750
    big = box(3000, 0, 3900, 600)
    free_big = corridor.difference(big)
    assert _scan_min_width(free_big, corridor.bounds) < MIN_WALKWAY_WIDTH


# ── 唯讀 + 序列化 ─────────────────────────────────────────────────────────
def test_analyze_does_not_mutate_spec():
    for make in _CORRIDOR_SPECS:
        spec = make()
        before = copy.deepcopy((
            [r.points for r in spec.rooms],
            [(getattr(f, "name", "counter"), getattr(f, "insert", None))
             for f in spec.fixtures],
        ))
        analyze_walkways(spec)
        after = (
            [r.points for r in spec.rooms],
            [(getattr(f, "name", "counter"), getattr(f, "insert", None))
             for f in spec.fixtures],
        )
        assert before == after


def test_report_follows_json_convention():
    import json
    rep = analyze_walkways(_corridor_spec())
    d = rep.to_dict()
    assert json.loads(rep.to_json()) == d
    assert set(d) >= {"ok", "has_walkway", "min_width", "count", "walkways"}
    row = d["walkways"][0]
    assert set(row) >= {"name", "role", "width", "raw_width", "length",
                        "serves", "blocked", "block_reason"}
    assert "\\u" not in rep.to_json()


def test_segment_is_json_report():
    seg = WalkwaySegment("走道", "main", 1200.0, 1200.0, 9000.0, 5)
    import json
    assert json.loads(seg.to_json())["role"] == "main"
