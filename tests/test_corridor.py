"""Corridor Analyzer 測試(v0.7 Phase 5-3)。

兩類:
  * **零誤報**——真實生成的圖不該被報成瓶頸/盡端/走不到。
  * **抓得到**——注入窄走道、盡端走道、窄門洞都要報出來。
另有「唯讀」保證:analyze_corridors() 不得改動 spec。
"""
import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shapely.geometry import Point as SPoint
from shapely.geometry import Polygon

from src.design.corridor import (
    MIN_CORRIDOR_WIDTH,
    MIN_OPENING_WIDTH,
    CorridorReport,
    _rect_dims,
    analyze_corridors,
)
from src.design.layout_generator import (
    HouseBrief,
    generate_floor_plan,
    generate_house_public,
    generate_house_upper,
)

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


def _corridor_spec():
    """挑一份**有走道**的圖(單層三房會配走道)。"""
    for make in _SPECS:
        spec = make()
        if any(r.kind == "corridor" for r in spec.rooms):
            return spec
    raise AssertionError("找不到含走道的案例")


# ── 零誤報 ────────────────────────────────────────────────────────────────
def test_generated_layouts_have_no_bottleneck_or_dead_end():
    """★ 核心:實際生成的圖不該有瓶頸、盡端走道或走不到的房間。"""
    for make in _SPECS:
        report = analyze_corridors(make())
        assert isinstance(report, CorridorReport)
        assert report.bottlenecks == [], report.summary()
        assert report.dead_ends == [], report.summary()
        assert report.unreachable == [], report.summary()
        assert report.ok


def test_corridor_width_and_length_are_measured():
    """走道量得出寬與長,且寬 <= 長、寬達到下限。"""
    report = analyze_corridors(_corridor_spec())
    assert report.has_corridor
    for c in report.corridors:
        assert c.width > 0 and c.length > 0
        assert c.width <= c.length
        assert c.width >= MIN_CORRIDOR_WIDTH
        assert not c.is_narrow and not c.is_dead_end
    assert report.min_width == min(c.width for c in report.corridors)


def test_corridor_serves_multiple_rooms():
    """走道存在的理由就是分流:連接數應 >= 2(否則就是盡端)。"""
    report = analyze_corridors(_corridor_spec())
    for c in report.corridors:
        assert c.degree >= 2
        assert len(c.serves) == c.degree


def test_walking_distance_and_longest_path():
    """步行距離:入口為 0、其他為正;最長路徑從入口出發並止於最遠房。"""
    for make in _SPECS:
        spec = make()
        report = analyze_corridors(spec)
        assert report.entrance is not None
        assert report.walking_distance[report.entrance] == 0.0
        assert report.longest_room is not None
        assert report.longest_distance > 0
        assert report.longest_distance == max(report.walking_distance.values())
        assert report.longest_path[0] == report.entrance
        assert report.longest_path[-1] == report.longest_room
        assert report.average_distance > 0


def test_longest_path_is_a_real_route():
    """最長路徑的每一步都必須是 Room Graph 上真的走得通的邊。"""
    from src.design.connectivity import build_graphs
    for make in _SPECS:
        spec = make()
        g = build_graphs(spec)
        idx = {n: i for i, n in enumerate(g.names)}
        path = analyze_corridors(spec).longest_path
        for a, b in zip(path, path[1:]):
            assert idx[b] in g.room_graph[idx[a]], f"{a}→{b} 走不通"


def test_no_corridor_case_is_handled():
    """沒有走道的圖不該爆掉,也不該被當成有問題。"""
    spec = next((s for s in (m() for m in _SPECS)
                 if not any(r.kind == "corridor" for r in s.rooms)), None)
    if spec is not None:
        report = analyze_corridors(spec)
        assert report.has_corridor is False
        assert report.min_width is None
        assert report.corridors == [] and report.dead_ends == []


def test_analyze_does_not_mutate_spec():
    """★ 需求:只分析,不得修改 Layout。"""
    for make in _SPECS:
        spec = make()
        before = copy.deepcopy((
            [r.points for r in spec.rooms],
            [(w.start, w.end, [(o.position, o.width, o.kind) for o in w.openings])
             for w in spec.walls],
            [(d.wall_index, d.opening_index) for d in spec.doors],
        ))
        analyze_corridors(spec)
        after = (
            [r.points for r in spec.rooms],
            [(w.start, w.end, [(o.position, o.width, o.kind) for o in w.openings])
             for w in spec.walls],
            [(d.wall_index, d.opening_index) for d in spec.doors],
        )
        assert before == after


# ── 抓得到缺陷 ────────────────────────────────────────────────────────────
def test_narrow_corridor_reported_as_bottleneck():
    """把走道壓到 600mm 寬 → 報 Bottleneck。"""
    spec = _corridor_spec()
    idx = next(i for i, r in enumerate(spec.rooms) if r.kind == "corridor")
    xs = [p[0] for p in spec.rooms[idx].points]
    ys = [p[1] for p in spec.rooms[idx].points]
    x0, y0, y1 = min(xs), min(ys), max(ys)
    spec.rooms[idx].points = [(x0, y0), (x0 + 600.0, y0),
                              (x0 + 600.0, y1), (x0, y1)]
    report = analyze_corridors(spec)
    assert report.bottlenecks
    assert any("走道" in b and "600" in b for b in report.bottlenecks)
    assert not report.ok


def test_narrow_opening_reported_as_bottleneck():
    """把某個門洞縮到 600mm → 報 Bottleneck。"""
    spec = _corridor_spec()
    op = next(o for w in spec.walls for o in w.openings if o.kind == "door")
    op.width = 600.0
    report = analyze_corridors(spec)
    assert any("洞口" in b for b in report.bottlenecks)
    assert not report.ok


def test_dead_end_corridor_reported():
    """把走道周邊的通路全封死 → 連接數降到 <=1,報 Dead End。"""
    spec = _corridor_spec()
    idx = next(i for i, r in enumerate(spec.rooms) if r.kind == "corridor")
    ring = Polygon(spec.rooms[idx].points).exterior
    for w in spec.walls:                        # 走道邊界上的可通行洞口 → 窗
        for op in w.openings:
            if op.kind == "door" and \
                    ring.distance(SPoint(w.point_at(op.position))) < 1.0:
                op.kind = "window"
    spec.doors = [d for d in spec.doors
                  if spec.walls[d.wall_index].openings[d.opening_index].kind
                  == "door"]
    report = analyze_corridors(spec)
    assert report.dead_ends
    assert not report.ok


def test_unreachable_room_has_no_walking_distance():
    """走不到的房間不該有步行距離,並列進 unreachable。"""
    spec = _corridor_spec()
    idx = next(i for i, r in enumerate(spec.rooms) if r.kind == "bedroom")
    name = spec.rooms[idx].name
    spec.rooms[idx].points = [(x + 500000.0, y)
                              for x, y in spec.rooms[idx].points]
    report = analyze_corridors(spec)
    assert name in report.unreachable
    assert name not in report.walking_distance


# ── 幾何輔助 ──────────────────────────────────────────────────────────────
def test_rect_dims_on_plain_rectangle():
    """矩形的(短邊, 長邊)要量得準。"""
    poly = Polygon([(0, 0), (4000, 0), (4000, 1200), (0, 1200)])
    short, long_ = _rect_dims(poly)
    assert abs(short - 1200) < 1e-6 and abs(long_ - 4000) < 1e-6


def test_rect_dims_on_degenerate_is_zero():
    """退化圖形回 (0, 0),不丟例外。"""
    assert _rect_dims(Polygon()) == (0.0, 0.0)


def test_thresholds_are_configurable_constants():
    """門檻是常數、單一來源(依實測值設定)。"""
    assert MIN_CORRIDOR_WIDTH == 900.0
    assert MIN_OPENING_WIDTH == 750.0
