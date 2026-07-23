"""Furniture Pair Constraint 測試(v0.7 Phase 6-4-2)。

家具**之間**的關聯偏好(沙發面向電視、床頭櫃貼床、書桌靠窗…)。重點:

  * 每種 relation(NEAR/FAR/FACE/ALIGN/CENTER/LEFT_OF/RIGHT_OF)都能評、都在 0~100。
  * 沒有 target 家具 → 不扣分(score 100)。
  * 多條規則 → 加權平均;多個同類目標 → 取最滿足的。
  * 併進 optimizer 後是**軟分數**:pair 權重再大也不能讓非法位置變合法
    (collision 永遠是 hard gate)。

⚠️ Pair Constraint 只影響分數,不影響合法性。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from shapely.geometry import Polygon

from src.design.collision.furniture_engine import FurnitureCollisionEngine
from src.design.collision.furniture_pair_constraint import (
    ALIGN,
    CENTER,
    DEFAULT_PAIR_RULES,
    FACE,
    FAR,
    LEFT_OF,
    NEAR,
    RELATIONS,
    RIGHT_OF,
    VIOLATION_THRESHOLD,
    FurniturePairEvaluator,
    FurniturePairRule,
    PairConstraintResult,
    PairTarget,
    type_of,
)
from src.design.collision.placement_optimizer import (
    FurniturePlacementOptimizer,
    PlacementWeights,
)
from src.design.layout_generator import HouseBrief, generate_floor_plan
from src.drafting.fixtures import FixturePlacement

ALL_KEYS = ("wall_distance", "window_distance", "walkway", "symmetry",
            "room_usability", "constraint", "pair_constraint", "human_clearance",
            "room_semantic")


class _Room:
    """輕量假房間:一個矩形,足夠驗關聯幾何。"""

    def __init__(self, x0, y0, x1, y1, kind="living"):
        self.points = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
        self.kind = kind
        self.name = "測試房"


ROOM = _Room(0, 0, 6000, 6000)


def _ev(*rules):
    return FurniturePairEvaluator(list(rules))


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
    wmap = w.as_map()
    return sum(scores[k] * wmap[k] for k in wmap) / sum(wmap.values())


# ── 沒有 target → 不扣分 ────────────────────────────────────────────────────
def test_no_target_furniture_no_penalty():
    """★ 房裡沒有電視時,沙發的 sofa→tv 規則跳過,不扣分。"""
    res = FurniturePairEvaluator().evaluate_pair_constraints(
        FixturePlacement("sofa3", (3000, 0), 0.0), ROOM, [])
    assert res.score == 100.0
    assert res.matched_rules == [] and res.violations == []


# ── NEAR / FAR ─────────────────────────────────────────────────────────────
def test_near_scores_high_when_close():
    rule = FurniturePairRule("sofa", "tv", NEAR, 1.0, 2000.0, 4000.0)
    res = _ev(rule).evaluate_pair_constraints(
        FixturePlacement("sofa3", (0, 0), 0.0), ROOM, [PairTarget("tv", (0, 1000))])
    assert res.score >= 90.0 and res.matched_rules


def test_near_scores_low_when_far():
    """★ 距離過遠 → NEAR 扣到低分。"""
    rule = FurniturePairRule("sofa", "tv", NEAR, 1.0, 2000.0, 4000.0)
    res = _ev(rule).evaluate_pair_constraints(
        FixturePlacement("sofa3", (0, 0), 0.0), ROOM, [PairTarget("tv", (0, 20000))])
    assert res.score <= 20.0 and res.violations


def test_far_is_inverse_of_near():
    """FAR:近時低、遠時高(NEAR 的反向)。"""
    rule = FurniturePairRule("fridge", "toilet", FAR, 1.0, 2000.0, 5000.0)
    ev = _ev(rule)
    close = ev.evaluate_pair_constraints(
        FixturePlacement("fridge", (0, 0), 0.0), ROOM, [PairTarget("toilet", (0, 500))])
    far = ev.evaluate_pair_constraints(
        FixturePlacement("fridge", (0, 0), 0.0), ROOM, [PairTarget("toilet", (0, 9000))])
    assert far.score > close.score and far.score >= 90.0


# ── FACE ───────────────────────────────────────────────────────────────────
def test_face_high_when_pointing_at_target_low_when_away():
    """★ FACE:正面朝目標 → 高;背對 → 低。"""
    rule = FurniturePairRule("sofa", "tv", FACE, 1.0, 3000.0, 5000.0)
    ev = _ev(rule)
    toward = ev.evaluate_pair_constraints(          # rot0 面向 +Y(北)
        FixturePlacement("sofa3", (0, 0), 0.0), ROOM, [PairTarget("tv", (0, 5000))])
    away = ev.evaluate_pair_constraints(
        FixturePlacement("sofa3", (0, 0), 0.0), ROOM, [PairTarget("tv", (0, -5000))])
    assert toward.score >= 90.0 and away.score <= 10.0


# ── ALIGN ──────────────────────────────────────────────────────────────────
def test_align_high_when_centre_lines_match():
    """★ ALIGN:中心線對齊 → 高;偏移 → 低。"""
    rule = FurniturePairRule("tv", "sofa", ALIGN, 1.0, 500.0, 1500.0)
    ev = _ev(rule)
    aligned = ev.evaluate_pair_constraints(
        FixturePlacement("tv_cabinet", (0, 0), 0.0), ROOM, [PairTarget("sofa", (0, 3000))])
    offset = ev.evaluate_pair_constraints(
        FixturePlacement("tv_cabinet", (0, 0), 0.0), ROOM, [PairTarget("sofa", (450, 3000))])
    assert aligned.score >= 95.0 and offset.score <= 30.0


# ── LEFT_OF / RIGHT_OF ─────────────────────────────────────────────────────
def test_right_of_scores_by_relative_position():
    """★ RIGHT_OF:source 在目標右側(較大 x)→ 高,左側 → 低。"""
    rule = FurniturePairRule("nightstand", "bed", RIGHT_OF, 1.0, 1000.0, 2000.0)
    ev = _ev(rule)
    right = ev.evaluate_pair_constraints(
        FixturePlacement("nightstand", (1500, 0), 0.0), ROOM, [PairTarget("bed", (0, 0))])
    left = ev.evaluate_pair_constraints(
        FixturePlacement("nightstand", (-1500, 0), 0.0), ROOM, [PairTarget("bed", (0, 0))])
    assert right.score >= 90.0 and left.score <= 10.0


def test_left_of_scores_by_relative_position():
    """★ LEFT_OF:source 在目標左側 → 高,右側 → 低(RIGHT_OF 的鏡像)。"""
    rule = FurniturePairRule("nightstand", "bed", LEFT_OF, 1.0, 1000.0, 2000.0)
    ev = _ev(rule)
    left = ev.evaluate_pair_constraints(
        FixturePlacement("nightstand", (-1500, 0), 0.0), ROOM, [PairTarget("bed", (0, 0))])
    right = ev.evaluate_pair_constraints(
        FixturePlacement("nightstand", (1500, 0), 0.0), ROOM, [PairTarget("bed", (0, 0))])
    assert left.score >= 90.0 and right.score <= 10.0


# ── CENTER ─────────────────────────────────────────────────────────────────
def test_center_high_near_room_centre_low_at_corner():
    """★ CENTER:靠房間中央 → 高,牆角 → 低(不需要 target)。"""
    rule = FurniturePairRule("dining_table", "", CENTER, 1.0, 500.0, 1500.0)
    ev = _ev(rule)
    room = _Room(0, 0, 4000, 4000)
    middle = ev.evaluate_pair_constraints(
        FixturePlacement("table4", (2000, 2000), 0.0), room, [])
    corner = ev.evaluate_pair_constraints(
        FixturePlacement("table4", (300, 300), 0.0), room, [])
    assert middle.score >= 95.0 and corner.score <= 30.0
    assert "dining_table-> CENTER" in middle.matched_rules[0]


# ── 加權平均 / 多目標 ──────────────────────────────────────────────────────
def test_overall_is_weighted_average_of_rules():
    """★ 多條規則 → 依 weight 加權平均。"""
    good = FurniturePairRule("sofa", "tv", NEAR, 1.0, 3000.0, 5000.0)     # →100
    bad = FurniturePairRule("sofa", "lamp", NEAR, 3.0, 100.0, 300.0)      # →0
    res = _ev(good, bad).evaluate_pair_constraints(
        FixturePlacement("sofa3", (0, 0), 0.0), ROOM,
        [PairTarget("tv", (0, 1000)), PairTarget("lamp", (0, 20000))])
    # (100*1 + 0*3) / 4 = 25
    assert abs(res.score - 25.0) < 1.0
    assert len(res.matched_rules) == 2


def test_multiple_targets_takes_the_best():
    """★ 多個同類目標 → 取最滿足規則的那個(床挑最近的床頭櫃)。"""
    rule = FurniturePairRule("bed", "nightstand", NEAR, 1.0, 400.0, 1000.0)
    res = _ev(rule).evaluate_pair_constraints(
        FixturePlacement("bed_double", (0, 0), 0.0), ROOM,
        [PairTarget("nightstand", (0, 30000)), PairTarget("nightstand", (0, 1100))])
    assert res.score >= 80.0                          # 由較近那個決定


# ── 預設規則 / 全 relation ────────────────────────────────────────────────
def test_all_default_rules_are_well_formed():
    """★ 每條預設規則:relation 合法、weight>0、ideal≤max、type 非空。"""
    assert DEFAULT_PAIR_RULES
    for r in DEFAULT_PAIR_RULES:
        assert r.relation in RELATIONS
        assert r.weight > 0
        assert 0 < r.ideal_distance <= r.max_distance
        assert r.source_type and (r.target_type or r.relation == CENTER)


def test_every_relation_is_scorable_and_in_range():
    """★ 七種 relation 都能評、都落在 0~100。"""
    room = _Room(0, 0, 5000, 5000)
    for rel in RELATIONS:
        tgt = "" if rel == CENTER else "tv"
        rule = FurniturePairRule("sofa", tgt, rel, 1.0, 1000.0, 3000.0)
        res = _ev(rule).evaluate_pair_constraints(
            FixturePlacement("sofa3", (2000, 1000), 0.0), room,
            [PairTarget("tv", (2000, 3500))])
        assert 0.0 <= res.score <= 100.0, rel


# ── 預設規則的端到端例子 ──────────────────────────────────────────────────
def test_sofa_near_and_facing_tv_default_rules():
    """★ 沙發面向且靠近電視(預設規則 FACE+NEAR)→ 高分。"""
    res = FurniturePairEvaluator().evaluate_pair_constraints(
        FixturePlacement("sofa3", (0, 0), 0.0), ROOM, [PairTarget("tv", (0, 3000))])
    assert res.score >= 85.0
    assert any("sofa->tv" in m for m in res.matched_rules)


def test_desk_near_window_default_rule():
    """★ 書桌靠近窗(預設 desk→window NEAR)→ 近高遠低。"""
    ev = FurniturePairEvaluator()
    near = ev.evaluate_pair_constraints(
        FixturePlacement("desk", (0, 0), 0.0), ROOM, [PairTarget("window", (0, 700))])
    far = ev.evaluate_pair_constraints(
        FixturePlacement("desk", (0, 0), 0.0), ROOM, [PairTarget("window", (0, 12000))])
    assert near.score >= 90.0 and far.score <= near.score


def test_washer_near_balcony_default_rule():
    """★ 洗衣機靠近陽台(預設 washer→balcony NEAR),source 用 PairTarget 代入。"""
    ev = FurniturePairEvaluator()
    near = ev.evaluate_pair_constraints(
        PairTarget("washer", (0, 0)), ROOM, [PairTarget("balcony", (0, 1000))])
    far = ev.evaluate_pair_constraints(
        PairTarget("washer", (0, 0)), ROOM, [PairTarget("balcony", (0, 15000))])
    assert near.score >= 90.0 and far.score <= 20.0


def test_bed_near_nightstand_default_rule():
    """★ 床靠近床頭櫃(預設 bed→nightstand NEAR)→ 近高遠低。"""
    ev = FurniturePairEvaluator()
    near = ev.evaluate_pair_constraints(
        FixturePlacement("bed_double", (0, 0), 0.0), ROOM,
        [PairTarget("nightstand", (900, 200))])
    far = ev.evaluate_pair_constraints(
        FixturePlacement("bed_double", (0, 0), 0.0), ROOM,
        [PairTarget("nightstand", (0, 20000))])
    assert near.score > far.score


# ── violations / API / 型別 ───────────────────────────────────────────────
def test_violations_list_records_low_scoring_rules():
    """★ 分數低於門檻的規則進 violations,高分的不進。"""
    rule = FurniturePairRule("sofa", "tv", NEAR, 1.0, 500.0, 1000.0)
    ev = _ev(rule)
    bad = ev.evaluate_pair_constraints(
        FixturePlacement("sofa3", (0, 0), 0.0), ROOM, [PairTarget("tv", (0, 30000))])
    good = ev.evaluate_pair_constraints(
        FixturePlacement("sofa3", (0, 0), 0.0), ROOM, [PairTarget("tv", (0, 300))])
    assert bad.violations and bad.score < VIOLATION_THRESHOLD
    assert not good.violations


def test_rule_rejects_unknown_relation():
    """★ 未知 relation 明確報錯,不默默接受。"""
    with pytest.raises(ValueError):
        FurniturePairRule("a", "b", "TELEPORT")


def test_type_of_maps_fixture_names_to_canonical_types():
    """★ 圖塊名 → canonical 類別:sofa3→sofa、tv_cabinet→tv、床→bed、餐桌→dining_table。"""
    assert type_of(FixturePlacement("sofa3", (0, 0), 0.0)) == "sofa"
    assert type_of(FixturePlacement("tv_cabinet", (0, 0), 0.0)) == "tv"
    assert type_of(FixturePlacement("bed_double", (0, 0), 0.0)) == "bed"
    assert type_of(FixturePlacement("table4", (0, 0), 0.0)) == "dining_table"
    assert type_of(PairTarget("balcony", (0, 0))) == "balcony"


def test_result_and_rule_follow_json_convention():
    """★ PairConstraintResult / FurniturePairRule 遵循 to_dict/to_json 契約。"""
    rule = FurniturePairRule("sofa", "tv", NEAR, 1.0, 2000.0, 4000.0)
    res = _ev(rule).evaluate_pair_constraints(
        FixturePlacement("sofa3", (0, 0), 0.0), ROOM, [PairTarget("tv", (0, 1000))])
    for obj in (res, rule):
        assert json.loads(obj.to_json()) == obj.to_dict()
        assert "\\u" not in obj.to_json()
    d = res.to_dict()
    assert set(d) >= {"score", "reasons", "violations", "matched_rules"}
    assert isinstance(res, PairConstraintResult)


# ── Optimizer 整合 ────────────────────────────────────────────────────────
def test_optimizer_pair_weight_default_is_015():
    """★ pair_constraint 權重不寫死:預設 0.15,序列化含此欄。"""
    w = PlacementWeights()
    assert w.pair_constraint == 0.15
    assert w.as_map()["pair_constraint"] == 0.15
    assert w.to_dict()["pair_constraint"] == 0.15


def test_optimizer_result_exposes_pair_constraint_score():
    """★ PlacementResult 有 pair_constraint_score;report/summary/scores 都含它。"""
    spec = _spec()
    res = FurniturePlacementOptimizer(spec).place("sofa3", _room(spec, "living"))
    assert res.found
    assert abs(res.pair_constraint_score
               - res.best.scores["pair_constraint"]) < 1e-6
    assert 0.0 <= res.pair_constraint_score <= 100.0
    d = res.to_dict()
    assert "pair_constraint_score" in d
    assert "pair_constraint" in d["best"]["scores"]
    assert "pair_constraint" in res.summary()


def test_pair_is_soft_not_a_hard_gate():
    """★ pair 權重灌爆,best 仍必須通過 collision(pair 不能讓非法變合法)。"""
    spec = _spec()
    opt = FurniturePlacementOptimizer(spec)
    eng = FurnitureCollisionEngine(spec)
    heavy = PlacementWeights(wall_distance=0.0, window_distance=0.0,
                             walkway=0.0, symmetry=0.0, room_usability=0.0,
                             constraint=0.0, pair_constraint=1000.0,
                             human_clearance=0.0, room_semantic=0.0)
    for kind, name in (("living", "sofa3"), ("bedroom", "wardrobe")):
        res = opt.place(name, _room(spec, kind), weights=heavy)
        assert res.found
        assert eng.check(res.best.placement()).valid


def test_pair_zero_weight_makes_it_irrelevant():
    """★ pair 權重=0:pair 分數怎麼變,總分都不變。"""
    w0 = PlacementWeights(pair_constraint=0.0)
    other = {k: 40.0 for k in ALL_KEYS}
    a = {**other, "pair_constraint": 0.0}
    b = {**other, "pair_constraint": 100.0}
    assert abs(_weighted(a, w0) - _weighted(b, w0)) < 1e-9


def test_pair_weight_dominates_ranking():
    """★ 只有 pair 有權重時,best 的 pair 分數 = 合法候選中的最大值。

    在客廳南牆放一台電視當關聯目標,讓沙發候選的 sofa→tv 分數真的有高有低。"""
    spec = _spec()
    living = _room(spec, "living")
    x0, y0, x1, y1 = _rect(living)
    spec.fixtures.append(
        FixturePlacement("tv_cabinet", ((x0 + x1) / 2, y0), 0.0))
    opt = FurniturePlacementOptimizer(spec)
    room_poly = Polygon(living.points)
    only = PlacementWeights(wall_distance=0.0, window_distance=0.0,
                            walkway=0.0, symmetry=0.0, room_usability=0.0,
                            constraint=0.0, pair_constraint=1.0,
                            human_clearance=0.0, room_semantic=0.0)
    best_pair = -1.0
    for placement in opt.candidates("sofa3", living):
        c = opt._score(placement, living, room_poly, None)
        if c.valid:
            best_pair = max(best_pair, c.scores["pair_constraint"])
    res = opt.place("sofa3", living, weights=only)
    assert res.found
    assert abs(res.best.total - best_pair) < 1e-6
    assert abs(res.best.scores["pair_constraint"] - best_pair) < 1e-6
