"""Layout Scoring Engine 測試(v0.7 Phase 5-4)。

驗三件事:
  * **公式正確**——總分確實是加權平均,權重真的有作用。
  * **有鑑別度**——每個面向在對應缺陷出現時要掉分(不是恆為 100)。
  * **唯讀**——score_layout() 不得改動 spec。
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
from src.design.scoring import (
    PUBLIC_KINDS,
    SEMI_PRIVATE_KINDS,
    LayoutScore,
    ScoreItem,
    ScoreWeights,
    score_layout,
)
from src.drafting.fixtures import FixturePlacement

METRICS = ("connectivity", "circulation", "privacy", "lighting",
           "utilization", "furniture", "collision")

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


# ── 結構與公式 ────────────────────────────────────────────────────────────
def test_score_has_all_seven_metrics():
    for make in _SPECS:
        sc = score_layout(make())
        assert isinstance(sc, LayoutScore)
        assert tuple(i.name for i in sc.items) == METRICS
        assert all(isinstance(i, ScoreItem) for i in sc.items)


def test_scores_are_within_range():
    for make in _SPECS:
        sc = score_layout(make())
        assert 0.0 <= sc.total <= 100.0
        for i in sc.items:
            assert 0.0 <= i.score <= 100.0, f"{i.name} 超出範圍"
            assert i.detail                      # 每項都要有可讀說明


def test_total_is_weighted_average():
    """★ 公式:總分 = Σ(子分數 × 權重) / Σ(權重)。"""
    sc = score_layout(_spec())
    wsum = sum(i.weight for i in sc.items)
    expect = sum(i.score * i.weight for i in sc.items) / wsum
    assert abs(sc.total - expect) < 1e-9


def test_weights_are_configurable():
    """只留一個面向的權重 → 總分等於該面向的分數。"""
    spec = _spec()
    for name in METRICS:
        w = ScoreWeights(**{m: (1.0 if m == name else 0.0) for m in METRICS})
        sc = score_layout(spec, w)
        assert abs(sc.total - sc.get(name).score) < 1e-9


def test_changing_weights_changes_total():
    spec = _spec()
    base = score_layout(spec, ScoreWeights())
    tilted = score_layout(spec, ScoreWeights(privacy=100.0))
    assert abs(base.total - tilted.total) > 1e-6


def test_grade_thresholds():
    assert LayoutScore(total=95).grade == "A"
    assert LayoutScore(total=85).grade == "B"
    assert LayoutScore(total=75).grade == "C"
    assert LayoutScore(total=65).grade == "D"
    assert LayoutScore(total=10).grade == "F"


def test_score_does_not_mutate_spec():
    """★ 需求:不得修改 Layout,不得 Auto Optimize。"""
    for make in _SPECS:
        spec = make()
        before = copy.deepcopy((
            [r.points for r in spec.rooms],
            [(w.start, w.end, [(o.position, o.width, o.kind) for o in w.openings])
             for w in spec.walls],
            [(getattr(f, "name", "counter"), getattr(f, "insert", None))
             for f in spec.fixtures],
        ))
        score_layout(spec)
        after = (
            [r.points for r in spec.rooms],
            [(w.start, w.end, [(o.position, o.width, o.kind) for o in w.openings])
             for w in spec.walls],
            [(getattr(f, "name", "counter"), getattr(f, "insert", None))
             for f in spec.fixtures],
        )
        assert before == after


# ── 各面向的鑑別度 ────────────────────────────────────────────────────────
def test_generated_layouts_score_well():
    """實際生成的圖:硬指標(連通/採光/家具/碰撞)應該滿分。"""
    for make in _SPECS:
        sc = score_layout(make())
        assert sc.get("connectivity").score == 100.0
        assert sc.get("lighting").score == 100.0
        assert sc.get("furniture").score == 100.0
        assert sc.get("collision").score == 100.0


def test_connectivity_drops_when_room_unreachable():
    spec = _spec()
    idx = next(i for i, r in enumerate(spec.rooms) if r.kind == "bedroom")
    spec.rooms[idx].points = [(x + 500000.0, y)
                              for x, y in spec.rooms[idx].points]
    assert score_layout(spec).get("connectivity").score < 100.0


def test_collision_drops_when_furniture_overlaps():
    spec = _spec()
    fx = next(f for f in spec.fixtures if isinstance(f, FixturePlacement))
    spec.fixtures.append(FixturePlacement(fx.name, fx.insert, fx.rotation))
    assert score_layout(spec).get("collision").score < 100.0


def test_furniture_drops_when_fixtures_removed():
    spec = _spec()
    spec.fixtures.clear()
    assert score_layout(spec).get("furniture").score == 0.0


def test_lighting_drops_when_windows_removed():
    spec = _spec()
    for w in spec.walls:
        for op in w.openings:
            if op.kind == "window":
                op.kind = "door"                 # 拆掉所有窗
    assert score_layout(spec).get("lighting").score == 0.0


def test_utilization_reflects_corridor_overhead():
    """有走道的圖可用面積 < 100%;沒有走道的圖 = 100%。"""
    spec = _spec()
    item = score_layout(spec).get("utilization")
    if any(r.kind == "corridor" for r in spec.rooms):
        assert item.score < 100.0
    else:
        assert item.score == 100.0


def test_circulation_drops_with_bottleneck():
    spec = _spec()
    op = next(o for w in spec.walls for o in w.openings if o.kind == "door")
    op.width = 600.0                             # 過窄的通行洞口
    assert score_layout(spec).get("circulation").score < 100.0


# ── Privacy 的語意(這一項最容易做錯)───────────────────────────────────
def test_family_room_is_not_treated_as_public():
    """★ 臥室開向家庭廳是透天臥室層的正常設計,不該算侵犯隱私。

    實測 100 層中 bedroom→family 出現 75 次;若把 family 當 public,
    正確的格局會被大量誤扣。"""
    assert "family" in SEMI_PRIVATE_KINDS
    assert "family" not in PUBLIC_KINDS


def test_bedroom_next_to_family_room_is_not_penalised():
    """★ 只與家庭廳相鄰的臥室,不得被算成隱私外露。

    這是本項最容易做錯的地方:實測 100 層有 75 次 bedroom→family,
    若把 family 當 public,正確的透天臥室層會被大量誤扣。"""
    from src.design.connectivity import build_graphs
    spec = generate_house_upper(
        HouseBrief(site_width=26000, site_depth=16000, bedrooms=3, seed=1))
    g = build_graphs(spec)
    target = next(
        (i for i in range(len(g.kinds))
         if g.kinds[i] == "bedroom"
         and any(g.kinds[j] == "family" for j in g.room_graph[i])
         and not any(g.kinds[j] in PUBLIC_KINDS for j in g.room_graph[i])),
        None)
    assert target is not None                    # 前提:真的有這種臥室
    detail = score_layout(spec).get("privacy").detail
    assert g.names[target] not in detail         # 沒被列進外露清單


def test_privacy_discriminates_between_layouts():
    """隱私分要有鑑別度:不同案例拿到不同分數,且抓得到真正的外露。"""
    scores = [score_layout(m()).get("privacy").score for m in _SPECS]
    assert min(scores) < 100.0                   # 真的有案例被抓到
    assert min(scores) < max(scores)             # 不是所有案例都同分
    details = [score_layout(m()).get("privacy").detail for m in _SPECS]
    assert any("外露" in d for d in details)


# ── 序列化(遵守 Report 慣例)─────────────────────────────────────────────
def test_score_reports_follow_json_convention():
    import json
    sc = score_layout(_spec())
    for obj in (sc, sc.items[0], ScoreWeights()):
        d = obj.to_dict()
        assert isinstance(d, dict)
        assert json.loads(obj.to_json()) == d
    d = sc.to_dict()
    assert d["grade"] == sc.grade
    assert len(d["items"]) == len(METRICS)
    assert "\\u" not in sc.to_json()             # 中文保持可讀
