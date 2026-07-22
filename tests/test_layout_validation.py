"""Layout Validation Layer 測試(v0.7 Phase 5-1)。

兩類:
  * **零誤報**——真實生成的圖必須全數 PASS(驗證器若對合格圖亂報就沒用)。
  * **抓得到**——注入每一種缺陷,對應的檢查必須報出來。
另有一條「唯讀」保證:validate() 不得改動 spec 的任何欄位。
"""
import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.design.layout_generator import (
    HouseBrief,
    generate_floor_plan,
    generate_house_public,
    generate_house_upper,
)
from src.design.layout_validation import (
    LayoutIssue,
    LayoutReport,
    LayoutValidator,
    validate_layout,
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


def _door_pt(spec, dp):
    w = spec.walls[dp.wall_index]
    return w.point_at(w.openings[dp.opening_index].position)


# ── 零誤報 ────────────────────────────────────────────────────────────────
def test_generated_layouts_pass_clean():
    """核心保證:實際生成的圖必須零 error、零 warn。"""
    for make in _SPECS:
        report = validate_layout(make())
        assert report.ok, report.summary()
        assert report.issues == []


def test_report_carries_counts_and_summary():
    """LayoutReport 帶得出房間/門/走道數量,summary 可讀。"""
    spec = _SPECS[0]()
    report = validate_layout(spec)
    assert isinstance(report, LayoutReport)
    assert report.rooms == len(spec.rooms)
    assert report.doors == len(spec.doors)
    assert report.corridors == sum(1 for r in spec.rooms if r.kind == "corridor")
    assert "PASS" in report.summary()


def test_validator_does_not_mutate_spec():
    """★ 需求:只做 Validation,不得修改 Layout。"""
    for make in _SPECS:
        spec = make()
        before = copy.deepcopy((
            [r.points for r in spec.rooms],
            [(w.start, w.end, [(o.position, o.width, o.kind) for o in w.openings])
             for w in spec.walls],
            [(d.wall_index, d.opening_index) for d in spec.doors],
            len(spec.fixtures),
        ))
        validate_layout(spec)
        after = (
            [r.points for r in spec.rooms],
            [(w.start, w.end, [(o.position, o.width, o.kind) for o in w.openings])
             for w in spec.walls],
            [(d.wall_index, d.opening_index) for d in spec.doors],
            len(spec.fixtures),
        )
        assert before == after


# ── 1. Room Polygon 是否封閉 ──────────────────────────────────────────────
def test_polygon_check_catches_degenerate_room():
    """頂點不足以構成面 → 報 polygon error(而不是讓驗證器爆掉)。"""
    spec = _SPECS[0]()
    spec.rooms[0].points = [(0.0, 0.0), (1000.0, 0.0)]
    report = validate_layout(spec)
    assert not report.ok
    assert any(i.check == "polygon" and "頂點" in i.message for i in report.errors)


def test_polygon_check_catches_self_intersection():
    """自交的「蝴蝶結」多邊形 → 報 polygon error。"""
    spec = _SPECS[0]()
    spec.rooms[0].points = [(0.0, 0.0), (1000.0, 1000.0),
                            (1000.0, 0.0), (0.0, 1000.0)]
    report = validate_layout(spec)
    assert any(i.check == "polygon" for i in report.errors)


# ── 2. Room 是否重疊 ──────────────────────────────────────────────────────
def test_overlap_check_catches_overlapping_rooms():
    """複製一間房疊上去 → 報 overlap error。"""
    spec = _SPECS[0]()
    dup = copy.deepcopy(spec.rooms[1])
    dup.name = dup.name + "(複製)"
    spec.rooms.append(dup)
    report = validate_layout(spec)
    assert not report.ok
    assert any(i.check == "overlap" for i in report.errors)


# ── 3. 是否存在孤立 Room ──────────────────────────────────────────────────
def test_isolated_check_catches_sealed_room():
    """把某間臥室周邊的門洞全改成窗(封死)→ 報 isolated error。"""
    from shapely.geometry import Point as SPoint
    from shapely.geometry import Polygon

    spec = _SPECS[0]()
    target = next(r for r in spec.rooms if r.kind == "bedroom")
    ring = Polygon(target.points).exterior
    sealed = False
    for w in spec.walls:                            # 落在該房邊界上的門洞 → 窗
        for op in w.openings:
            if op.kind != "door":
                continue
            if ring.distance(SPoint(w.point_at(op.position))) < 1.0:
                op.kind = "window"
                sealed = True
    assert sealed                                   # 前提:真的有門被封掉
    spec.doors = [d for d in spec.doors
                  if ring.distance(SPoint(_door_pt(spec, d))) >= 1.0]

    report = validate_layout(spec)
    assert not report.ok
    assert any(i.check == "isolated" and target.name in i.message
               for i in report.errors)


def test_isolated_check_ignores_patio():
    """天井是室外空井,不該被當成「走不到的房間」誤報。"""
    spec = generate_house_upper(
        HouseBrief(site_width=20000, site_depth=20000, bedrooms=3, seed=1))
    assert any(r.kind == "patio" for r in spec.rooms)   # 前提:這張圖有天井
    report = validate_layout(spec)
    assert report.ok, report.summary()


# ── 4. Door 是否可連通 ────────────────────────────────────────────────────
def test_door_check_catches_door_off_every_room():
    """把帶門的牆整道移到天邊 → 該門不在任何房間邊界上,報 door error。"""
    spec = _SPECS[0]()
    dp = spec.doors[0]
    w = spec.walls[dp.wall_index]
    w.start, w.end = (-99000.0, -99000.0), (-95000.0, -99000.0)
    report = validate_layout(spec)
    assert any(i.check == "door" for i in report.errors)


# ── 5. Corridor 是否中斷 ──────────────────────────────────────────────────
def test_corridor_check_catches_split_corridor():
    """再加一段離很遠的走道 → 走道分兩塊,報 corridor error。"""
    spec = next((s for s in (m() for m in _SPECS)
                 if any(r.kind == "corridor" for r in s.rooms)), None)
    assert spec is not None                         # 前提:找得到有走道的圖
    corr = next(r for r in spec.rooms if r.kind == "corridor")
    far = copy.deepcopy(corr)
    far.name = corr.name + "(遠處)"
    far.points = [(x + 500000.0, y) for x, y in corr.points]
    spec.rooms.append(far)
    report = validate_layout(spec)
    assert any(i.check == "corridor" for i in report.errors)


def test_single_corridor_is_not_reported_as_broken():
    """只有一段走道不可能「中斷」,不得誤報。"""
    for make in _SPECS:
        spec = make()
        if sum(1 for r in spec.rooms if r.kind == "corridor") <= 1:
            assert not [i for i in validate_layout(spec).issues
                        if i.check == "corridor"]


# ── LayoutReport 行為 ─────────────────────────────────────────────────────
def test_report_separates_errors_and_warnings():
    report = LayoutReport(issues=[
        LayoutIssue("polygon", "error", "壞掉"),
        LayoutIssue("isolated", "warn", "可疑"),
    ], rooms=3, doors=2)
    assert len(report.errors) == 1 and len(report.warnings) == 1
    assert report.ok is False
    assert "FAIL" in report.summary()


def test_validator_class_exposes_individual_checks():
    """五項檢查可單獨呼叫(給日後報表/除錯用)。"""
    v = LayoutValidator(_SPECS[0]())
    assert v.check_polygons() == []
    assert v.check_overlap() == []
    assert v.check_isolated() == []
    assert v.check_doors() == []
    assert v.check_corridor() == []
