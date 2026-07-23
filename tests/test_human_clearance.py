"""Human Clearance 測試(v0.7 Phase 6-5)。

人體活動空間:家具沒撞到,不代表用得了(衣櫃門前 300mm 打不開、書桌角落椅子
拉不出來)。本模組替每種家具劃活動區,看它是否落在房間外(缺空間)或被別的
家具佔住(活動被擋)。重點:

  * 活動區完整 → 滿分;缺空間 / 被佔 → 扣分;不需要活動區的家具 → 跳過(100)。
  * 併進 optimizer 後是**軟分數**:human 權重再大也不能讓非法位置變合法。

⚠️ Human Clearance 只影響分數,不影響合法性(collision 永遠是 hard gate)。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shapely.geometry import Polygon

from src.design.collision.furniture_engine import FurnitureCollisionEngine
from src.design.collision.human_clearance import (
    HUMAN_CLEARANCE_RULES,
    HumanClearanceEvaluator,
    HumanClearanceResult,
    HumanClearanceRule,
    clearance_regions,
    human_type,
)
from src.design.collision.placement_optimizer import (
    FurniturePlacementOptimizer,
    PlacementWeights,
)
from src.design.layout_generator import HouseBrief, generate_floor_plan
from src.drafting.fixtures import Counter, FixturePlacement

ALL_KEYS = ("wall_distance", "window_distance", "walkway", "symmetry",
            "room_usability", "constraint", "pair_constraint", "human_clearance")

LISTED_TYPES = ("bed", "wardrobe", "desk", "chair", "sofa", "dining_table",
                "toilet", "shower", "sink", "washer", "fridge",
                "kitchen_counter")


class _Room:
    def __init__(self, x0, y0, x1, y1, kind="bedroom"):
        self.points = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
        self.kind = kind
        self.name = "測試房"


EV = HumanClearanceEvaluator()


def _spec():
    return generate_floor_plan(
        HouseBrief(site_width=20000, site_depth=14000, bedrooms=3))


def _room(spec, kind):
    return next(r for r in spec.rooms if r.kind == kind)


def _weighted(scores, w):
    wmap = w.as_map()
    return sum(scores[k] * wmap[k] for k in wmap) / sum(wmap.values())


# ── Wardrobe 開門 ──────────────────────────────────────────────────────────
def test_wardrobe_door_clear_scores_full():
    """★ 衣櫃門前寬敞(活動區全落在房內)→ 滿分,無 blocked。"""
    room = _Room(0, 0, 5000, 5000)
    res = EV.evaluate_human_clearance(
        FixturePlacement("wardrobe", (2500, 0), 0.0), room, [])
    assert res.score == 100.0 and res.blocked_regions == []


def test_wardrobe_door_blocked_by_near_wall():
    """★ 衣櫃門前很淺(活動區戳出對牆)→ 扣分,front 進 blocked/violations。"""
    room = _Room(0, 0, 5000, 1000)               # 對牆只離 1000,前淨空要 900
    res = EV.evaluate_human_clearance(
        FixturePlacement("wardrobe", (2500, 0), 0.0), room, [])
    assert res.score < 60.0
    assert "front" in res.blocked_regions and res.violations


def test_wardrobe_door_blocked_by_other_furniture():
    """★ 衣櫃門前被別的家具佔住 → 扣分,front 進 blocked。"""
    room = _Room(0, 0, 5000, 5000)
    blocker = FixturePlacement("bookshelf", (2500, 900), 0.0)
    res = EV.evaluate_human_clearance(
        FixturePlacement("wardrobe", (2500, 0), 0.0), room, [blocker])
    assert res.score < 100.0 and "front" in res.blocked_regions


# ── Desk 拉椅 / Sofa 前活動 ────────────────────────────────────────────────
def test_desk_chair_pullout_clear_vs_blocked():
    """★ 書桌前拉椅空間:寬敞滿分、貼對牆扣分。"""
    clear = EV.evaluate_human_clearance(
        FixturePlacement("desk", (2500, 0), 0.0), _Room(0, 0, 5000, 5000), [])
    blocked = EV.evaluate_human_clearance(
        FixturePlacement("desk", (2500, 0), 0.0), _Room(0, 0, 5000, 900), [])
    assert clear.score == 100.0 and blocked.score < clear.score


def test_sofa_front_activity_clear_vs_blocked():
    """★ 沙發前活動空間:寬敞滿分、對牆太近扣分。"""
    clear = EV.evaluate_human_clearance(
        FixturePlacement("sofa3", (2500, 0), 0.0), _Room(0, 0, 6000, 6000), [])
    blocked = EV.evaluate_human_clearance(
        FixturePlacement("sofa3", (2500, 0), 0.0), _Room(0, 0, 6000, 1200), [])
    assert clear.score == 100.0 and blocked.score < 70.0


# ── Dining chair(四面）──────────────────────────────────────────────────
def test_dining_table_all_sides_clear():
    """★ 餐桌置房間中央 → 四面坐人空間都在 → 滿分。"""
    res = EV.evaluate_human_clearance(
        FixturePlacement("table4", (2500, 2500), 0.0),
        _Room(0, 0, 5000, 5000), [])
    assert res.score == 100.0 and res.blocked_regions == []


def test_dining_table_against_wall_blocks_one_side():
    """★ 餐桌貼牆 → 背側坐人空間戳出牆外 → back 進 blocked。"""
    res = EV.evaluate_human_clearance(
        FixturePlacement("table4", (2500, 900), 0.0),
        _Room(0, 0, 5000, 5000), [])
    assert res.score < 100.0 and "back" in res.blocked_regions


# ── Bed 側邊 / 兩側 ────────────────────────────────────────────────────────
def test_bed_side_clearance_one_side_blocked_in_corner():
    """★ 床推到牆角 → 一側上下床空間沒了 → 該側進 blocked。"""
    # 床寬 1600,插入 x=800 → 床身 x 0..1600 貼西牆,左側活動區戳出 x<0
    res = EV.evaluate_human_clearance(
        FixturePlacement("bed_double", (800, 0), 0.0),
        _Room(0, 0, 5000, 5000), [])
    assert "left" in res.blocked_regions and res.score < 100.0


def test_bed_both_sides_blocked_in_narrow_alcove():
    """★ 床塞進和床同寬的凹室 → 兩側都沒空間 → left/right 都 blocked、分數很低。"""
    res = EV.evaluate_human_clearance(
        FixturePlacement("bed_double", (800, 0), 0.0),
        _Room(0, 0, 1600, 5000), [])         # 房寬 = 床寬,兩側 0
    assert "left" in res.blocked_regions and "right" in res.blocked_regions
    assert res.score < 50.0


# ── Toilet / Sink / Kitchen ───────────────────────────────────────────────
def test_toilet_front_clearance():
    clear = EV.evaluate_human_clearance(
        FixturePlacement("toilet", (2500, 0), 0.0), _Room(0, 0, 5000, 5000), [])
    blocked = EV.evaluate_human_clearance(
        FixturePlacement("toilet", (2500, 0), 0.0), _Room(0, 0, 5000, 900), [])
    assert clear.score == 100.0 and blocked.score < clear.score


def test_sink_front_clearance():
    """basin → sink:面盆前站立空間。"""
    clear = EV.evaluate_human_clearance(
        FixturePlacement("basin", (2500, 0), 0.0), _Room(0, 0, 5000, 5000), [])
    blocked = EV.evaluate_human_clearance(
        FixturePlacement("basin", (2500, 0), 0.0), _Room(0, 0, 5000, 700), [])
    assert clear.score == 100.0 and blocked.score < clear.score


def test_kitchen_counter_aisle_clear_vs_blocked():
    """★ 流理台工作走道:走道在房內滿分、戳出牆外扣分。"""
    clear = EV.evaluate_human_clearance(
        Counter(start=(1000, 2000), end=(3000, 2000), depth=600),
        _Room(0, 0, 5000, 5000, kind="kitchen"), [])
    blocked = EV.evaluate_human_clearance(
        Counter(start=(1000, 300), end=(3000, 300), depth=600),
        _Room(0, 0, 5000, 5000, kind="kitchen"), [])
    assert clear.score == 100.0 and blocked.score < clear.score


# ── Washer(規則存在,無圖塊幾何）─────────────────────────────────────────
def test_washer_rule_defined():
    """★ 洗衣機規則存在且要求前方開門空間(目前無對應圖塊,屬宣告式規則)。"""
    r = HUMAN_CLEARANCE_RULES["washer"]
    assert r.front_clearance >= 500.0 and r.needs_clearance


# ── 不需要活動區 → 跳過 ────────────────────────────────────────────────────
def test_furniture_without_rule_is_skipped():
    """★ 沒有活動區規則的家具(床頭櫃/電視櫃)→ 直接 100、無 reason。"""
    for name in ("nightstand", "tv_cabinet", "shoe_cabinet"):
        res = EV.evaluate_human_clearance(
            FixturePlacement(name, (2500, 0), 0.0), _Room(0, 0, 5000, 5000), [])
        assert res.score == 100.0 and res.reasons == []


# ── 全部 Rule / 型別 ──────────────────────────────────────────────────────
def test_all_listed_types_have_rules():
    """★ 題目列的 12 種類別都有規則。"""
    for t in LISTED_TYPES:
        assert t in HUMAN_CLEARANCE_RULES


def test_all_rules_are_well_formed():
    """★ 每條規則:淨空非負、至少一側 > 0(needs_clearance)。"""
    for t, r in HUMAN_CLEARANCE_RULES.items():
        assert r.front_clearance >= 0 and r.side_clearance >= 0 \
            and r.back_clearance >= 0
        assert r.needs_clearance, t


def test_human_type_maps_fixture_names():
    """★ 圖塊名→canonical 類別:床→bed、basin→sink、浴缸→shower、餐桌→dining_table、
    流理台→kitchen_counter。"""
    assert human_type(FixturePlacement("bed_double", (0, 0), 0.0)) == "bed"
    assert human_type(FixturePlacement("basin", (0, 0), 0.0)) == "sink"
    assert human_type(FixturePlacement("bathtub", (0, 0), 0.0)) == "shower"
    assert human_type(FixturePlacement("table4", (0, 0), 0.0)) == "dining_table"
    assert human_type(Counter(start=(0, 0), end=(1000, 0))) == "kitchen_counter"


# ── 分數線性 / API ────────────────────────────────────────────────────────
def test_partial_block_gives_partial_score():
    """★ 活動區一半戳出牆 → 分數約 50(缺空間比例線性扣分)。"""
    # 衣櫃前淨空 900,對牆離 1050 → front zone(600..1500)有一半(1050..1500)在外
    res = EV.evaluate_human_clearance(
        FixturePlacement("wardrobe", (2500, 0), 0.0), _Room(0, 0, 5000, 1050), [])
    assert abs(res.score - 50.0) < 3.0


def test_result_and_rule_follow_json_convention():
    """★ HumanClearanceResult / HumanClearanceRule 遵循 to_dict/to_json 契約。"""
    res = EV.evaluate_human_clearance(
        FixturePlacement("wardrobe", (2500, 0), 0.0), _Room(0, 0, 5000, 1000), [])
    for obj in (res, HUMAN_CLEARANCE_RULES["wardrobe"]):
        assert json.loads(obj.to_json()) == obj.to_dict()
        assert "\\u" not in obj.to_json()
    d = res.to_dict()
    assert set(d) >= {"score", "violations", "blocked_regions", "reasons"}
    assert isinstance(res, HumanClearanceResult)


def test_clearance_regions_shape():
    """★ 活動區幾何:衣櫃只有 front;餐桌 front/back/left/right 四塊。"""
    ward = clearance_regions(FixturePlacement("wardrobe", (2500, 0), 0.0),
                             HUMAN_CLEARANCE_RULES["wardrobe"])
    assert [n for n, _ in ward] == ["front"]
    tbl = clearance_regions(FixturePlacement("table4", (2500, 2500), 0.0),
                            HUMAN_CLEARANCE_RULES["dining_table"])
    assert {n for n, _ in tbl} == {"front", "back", "left", "right"}


# ── Optimizer 整合 ────────────────────────────────────────────────────────
def test_optimizer_human_weight_default_is_020():
    """★ human_clearance 權重不寫死:預設 0.20,序列化含此欄。"""
    w = PlacementWeights()
    assert w.human_clearance == 0.20
    assert w.as_map()["human_clearance"] == 0.20
    assert w.to_dict()["human_clearance"] == 0.20


def test_optimizer_result_exposes_human_clearance_score():
    """★ PlacementResult 有 human_clearance_score;report/summary/scores 都含它。"""
    spec = _spec()
    res = FurniturePlacementOptimizer(spec).place("wardrobe", _room(spec, "bedroom"))
    assert res.found
    assert abs(res.human_clearance_score
               - res.best.scores["human_clearance"]) < 1e-6
    assert 0.0 <= res.human_clearance_score <= 100.0
    d = res.to_dict()
    assert "human_clearance_score" in d
    assert "human_clearance" in d["best"]["scores"]
    assert "human_clearance" in res.summary()


def test_human_is_soft_not_a_hard_gate():
    """★ human 權重灌爆,best 仍必須通過 collision(human 不能讓非法變合法)。"""
    spec = _spec()
    opt = FurniturePlacementOptimizer(spec)
    eng = FurnitureCollisionEngine(spec)
    heavy = PlacementWeights(wall_distance=0.0, window_distance=0.0,
                             walkway=0.0, symmetry=0.0, room_usability=0.0,
                             constraint=0.0, pair_constraint=0.0,
                             human_clearance=1000.0)
    for kind, name in (("bedroom", "wardrobe"), ("living", "sofa3")):
        res = opt.place(name, _room(spec, kind), weights=heavy)
        assert res.found
        assert eng.check(res.best.placement()).valid


def test_human_zero_weight_makes_it_irrelevant():
    """★ human 權重=0:human 分數怎麼變,總分都不變。"""
    w0 = PlacementWeights(human_clearance=0.0)
    other = {k: 40.0 for k in ALL_KEYS}
    a = {**other, "human_clearance": 0.0}
    b = {**other, "human_clearance": 100.0}
    assert abs(_weighted(a, w0) - _weighted(b, w0)) < 1e-9


def test_human_weight_dominates_ranking():
    """★ 只有 human 有權重時,best 的 human 分數 = 合法候選中的最大值。"""
    spec = _spec()
    opt = FurniturePlacementOptimizer(spec)
    room = _room(spec, "bedroom")
    room_poly = Polygon(room.points)
    only = PlacementWeights(wall_distance=0.0, window_distance=0.0,
                            walkway=0.0, symmetry=0.0, room_usability=0.0,
                            constraint=0.0, pair_constraint=0.0,
                            human_clearance=1.0)
    best_human = -1.0
    for placement in opt.candidates("wardrobe", room):
        c = opt._score(placement, room, room_poly, None)
        if c.valid:
            best_human = max(best_human, c.scores["human_clearance"])
    res = opt.place("wardrobe", room, weights=only)
    assert res.found
    assert abs(res.best.total - best_human) < 1e-6
    assert abs(res.best.scores["human_clearance"] - best_human) < 1e-6
