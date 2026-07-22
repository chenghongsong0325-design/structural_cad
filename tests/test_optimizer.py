"""Layout Optimizer 測試(v0.7 Phase 5-6)。

重點在**安全**與**單步**:
  * 一次呼叫最多套用一個改動(不得一次大量修改)。
  * 改完仍然是合法圖(validate_spec 全過),碰撞不增加,家具完全不動。
  * 沒有可行候選時,spec 一個位元組都不能變。
  * 給一張被弄差的圖,要真的能改回來(證明它不是永遠不作為)。
"""
import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.design.collision.detector import find_collisions
from src.design.collision.geometry import collect_active
from src.design.connectivity import analyze_connectivity
from src.design.layout_generator import (
    HouseBrief,
    generate_floor_plan,
    generate_house_upper,
    validate_spec,
)
from src.design.optimizer import (
    OptimizeStep,
    _evaluate,
    _is_safe,
    _shift_boundary,
    candidates,
    optimize_step,
)
from src.design.scoring import ScoreWeights, score_layout


def _spec():
    return generate_floor_plan(
        HouseBrief(site_width=16000, site_depth=14000, bedrooms=3))


_SPECS = [
    _spec,
    lambda: generate_floor_plan(
        HouseBrief(site_width=20000, site_depth=14000, bedrooms=3)),
    lambda: generate_house_upper(
        HouseBrief(site_width=26000, site_depth=16000, bedrooms=3, seed=1)),
]


def _snapshot(spec):
    return copy.deepcopy((
        [r.points for r in spec.rooms],
        [(w.start, w.end, [(o.position, o.width, o.kind) for o in w.openings])
         for w in spec.walls],
        [(getattr(f, "name", "counter"), getattr(f, "insert", None),
          getattr(f, "start", None)) for f in spec.fixtures],
    ))


def _door_positions(spec):
    return [tuple(o.position for o in w.openings) for w in spec.walls]


# ── 候選 ──────────────────────────────────────────────────────────────────
def test_candidates_cover_the_three_allowed_operations():
    """只有三種被允許的微調,而且三種都真的產得出候選。"""
    kinds = {c.strategy for c in candidates(_spec())}
    assert kinds == {"door_position", "room_position", "room_rotation"}


def test_rotation_candidates_exist_but_are_always_rejected():
    """★ 旋轉在「軸對齊且完全鋪滿」的平面上必然破壞鋪滿 → 一定被安全閘門擋下。

    保留這類候選是為了日後自由平面;這條測試把「它現在永遠不會生效」明確
    寫死,避免日後有人誤以為旋轉有在作用。"""
    spec = _spec()
    rots = [c for c in candidates(spec) if c.strategy == "room_rotation"]
    assert rots
    base = _evaluate(spec, ScoreWeights())
    for cand in rots:
        trial = copy.deepcopy(spec)
        cand.apply(trial)
        assert not _is_safe(trial, base)


# ── 單步保證 ──────────────────────────────────────────────────────────────
def test_single_step_changes_only_one_kind_of_thing():
    """★ 一次只做一件事:移門就不動房間,推分界就不動門。"""
    for make in _SPECS:
        spec = make()
        rooms_before = copy.deepcopy([r.points for r in spec.rooms])
        doors_before = _door_positions(spec)
        step = optimize_step(spec)
        if not step.applied:
            continue
        rooms_after = [r.points for r in spec.rooms]
        doors_after = _door_positions(spec)
        if step.strategy == "door_position":
            assert rooms_after == rooms_before        # 房間不動
            assert doors_after != doors_before
        elif step.strategy == "room_position":
            assert doors_after == doors_before        # 門位置不動
            assert rooms_after != rooms_before


def test_no_improvement_leaves_spec_byte_identical():
    """★ 找不到可行候選時,spec 必須完全不動。"""
    spec = _spec()
    for _ in range(6):                                # 先把能改的都改完
        if not optimize_step(spec).applied:
            break
    before = _snapshot(spec)
    step = optimize_step(spec)
    if not step.applied:
        assert _snapshot(spec) == before


# ── 安全 ──────────────────────────────────────────────────────────────────
def test_optimized_layout_is_still_valid():
    """★ 改完仍須通過 validate_spec 與連通檢查。"""
    for make in _SPECS:
        spec = make()
        step = optimize_step(spec)
        assert validate_spec(spec) == [], step.summary()
        assert analyze_connectivity(spec).ok


def test_optimizer_never_touches_fixtures():
    """★ 需求:不得影響 Collision Engine —— 家具一件都不能動。"""
    for make in _SPECS:
        spec = make()
        before = [(getattr(f, "name", "counter"), getattr(f, "insert", None),
                   getattr(f, "start", None)) for f in spec.fixtures]
        optimize_step(spec)
        after = [(getattr(f, "name", "counter"), getattr(f, "insert", None),
                  getattr(f, "start", None)) for f in spec.fixtures]
        assert before == after


def test_collisions_never_increase():
    for make in _SPECS:
        spec = make()
        before = len(find_collisions(collect_active(spec)))
        optimize_step(spec)
        assert len(find_collisions(collect_active(spec))) <= before


def test_score_never_decreases():
    """接受條件是「更好」,所以分數不可能被改差。"""
    for make in _SPECS:
        spec = make()
        before = score_layout(spec).total
        step = optimize_step(spec)
        after = score_layout(spec).total
        assert after >= before - 1e-9
        if step.applied:
            assert step.after_errors <= step.before_errors


# ── 真的會最佳化(不是永遠不作為)────────────────────────────────────────
def test_degraded_layout_is_improved():
    """★ 把一條分界推歪讓分數變差,optimizer 要能找回來。"""
    spec = _spec()
    good = score_layout(spec).total

    worse = None
    for room in spec.rooms:                           # 找一條推歪後仍合法的分界
        if room.kind != "corridor":
            continue
        xs = sorted({p[0] for p in room.points})
        for c in xs:
            trial = copy.deepcopy(spec)
            _shift_boundary(trial, 0, c, 100.0)
            if validate_spec(trial):
                continue
            s = score_layout(trial).total
            if s < good - 1e-9:
                worse = trial
                break
        if worse:
            break
    assert worse is not None, "構造不出『被弄差』的圖"
    degraded = score_layout(worse).total
    assert degraded < good

    step = optimize_step(worse)
    assert step.applied, step.summary()
    assert score_layout(worse).total > degraded       # 真的變好了
    assert validate_spec(worse) == []                 # 而且仍然合法


# ── 報表 ──────────────────────────────────────────────────────────────────
def test_step_report_follows_json_convention():
    import json
    step = optimize_step(_spec())
    assert isinstance(step, OptimizeStep)
    d = step.to_dict()
    assert json.loads(step.to_json()) == d
    assert set(d) >= {"applied", "strategy", "target", "detail",
                      "before_score", "after_score", "improvement",
                      "candidates", "accepted"}
    assert "\\u" not in step.to_json()
    assert isinstance(step.summary(), str) and step.summary()


def test_report_counts_candidates():
    spec = _spec()
    step = optimize_step(spec)
    assert step.candidates == len(candidates(spec)) or step.candidates > 0
    assert step.accepted >= (1 if step.applied else 0)
