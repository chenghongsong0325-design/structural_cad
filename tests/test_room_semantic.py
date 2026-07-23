"""Room Semantic Engine 測試(v0.7 Phase 6-6)。

房間功能語意:這個房間**該有什麼、不該有什麼**。臥室要床、廚房要流理台+冰箱、
浴室不該有床。RoomSemanticEvaluator 給 0~100 分,列出 missing / extra / violations。
重點:

  * required 缺 → 扣;forbidden 出現 → 重扣;數量太少/太多 → 扣;preferred 不強制。
  * 未涵蓋的房間 → 跳過(100)。
  * 併進 optimizer 後是**軟分數**:room_semantic 權重再大也不能讓非法位置變合法。

⚠️ Room Semantic 只影響分數,不影響合法性(collision 永遠是 hard gate)。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shapely.geometry import Polygon

from src.design.collision.furniture_engine import FurnitureCollisionEngine
from src.design.collision.placement_optimizer import (
    FurniturePlacementOptimizer,
    PlacementWeights,
)
from src.design.layout_generator import HouseBrief, generate_floor_plan
from src.design.semantic.room_semantic import (
    ROOM_SEMANTIC_RULES,
    RoomSemanticEvaluator,
    RoomSemanticResult,
    RoomSemanticRule,
    canonical_room,
    get_room_rule,
    placement_type,
)
from src.drafting.fixtures import Counter, FixturePlacement

ALL_KEYS = ("wall_distance", "window_distance", "walkway", "symmetry",
            "room_usability", "constraint", "pair_constraint", "human_clearance",
            "room_semantic")

LISTED_ROOMS = ("living", "dining", "bedroom", "kitchen", "bathroom",
                "laundry", "study", "entrance", "balcony")

EV = RoomSemanticEvaluator()


class _Room:
    def __init__(self, kind, name="測試房"):
        self.kind = kind
        self.name = name
        self.points = [(0, 0), (4000, 0), (4000, 4000), (0, 4000)]


def _fx(name):
    return FixturePlacement(name, (0, 0), 0.0)


def _ev(kind, names):
    return EV.evaluate_room_semantics(_Room(kind), [_fx(n) for n in names])


def _spec():
    return generate_floor_plan(
        HouseBrief(site_width=20000, site_depth=14000, bedrooms=3))


def _room(spec, kind):
    return next(r for r in spec.rooms if r.kind == kind)


def _weighted(scores, w):
    wmap = w.as_map()
    return sum(scores[k] * wmap[k] for k in wmap) / sum(wmap.values())


# ── Bedroom ────────────────────────────────────────────────────────────────
def test_bedroom_with_bed_is_full_score():
    """★ 臥室有床 → 必要滿足,滿分。"""
    res = _ev("bedroom", ["bed_double"])
    assert res.score == 100.0 and res.missing == [] and res.violations == []


def test_bedroom_with_preferred_still_full():
    """臥室有床 + 衣櫃 + 書桌(preferred)→ 仍滿分。"""
    res = _ev("bedroom", ["bed_double", "wardrobe", "desk"])
    assert res.score == 100.0 and res.extra == []


def test_bedroom_missing_bed():
    """★ 臥室沒有床 → missing 有 bed、扣分、violations 記缺。"""
    res = _ev("bedroom", ["wardrobe"])
    assert "bed" in res.missing and res.score < 100.0
    assert any("bed" in v for v in res.violations)


def test_bedroom_with_forbidden_toilet():
    """★ 臥室出現馬桶(forbidden)→ 扣分、violations 記不該出現。"""
    res = _ev("bedroom", ["bed_double", "toilet"])
    assert res.score < 100.0
    assert any("toilet" in v for v in res.violations)


# ── Kitchen ────────────────────────────────────────────────────────────────
def test_kitchen_with_counter_and_fridge_full():
    """★ 廚房有流理台 + 冰箱 → 滿分。"""
    res = EV.evaluate_room_semantics(
        _Room("kitchen"),
        [Counter(start=(0, 0), end=(2000, 0)), _fx("fridge")])
    assert res.score == 100.0 and res.missing == []


def test_kitchen_missing_fridge():
    """★ 廚房只有流理台、缺冰箱 → missing 有 fridge。"""
    res = EV.evaluate_room_semantics(
        _Room("kitchen"), [Counter(start=(0, 0), end=(2000, 0))])
    assert "fridge" in res.missing and res.score < 100.0


# ── Living ─────────────────────────────────────────────────────────────────
def test_living_with_sofa_full():
    """★ 客廳有沙發 → 滿分。"""
    assert _ev("living", ["sofa3"]).score == 100.0


def test_living_with_sofa_and_tv_full():
    res = _ev("living", ["sofa3", "tv_cabinet"])
    assert res.score == 100.0 and res.missing == []


def test_living_empty_missing_sofa_and_too_few():
    """★ 空客廳 → 缺沙發 + 家具太少,雙重扣分。"""
    res = _ev("living", [])
    assert "sofa" in res.missing
    assert any("太少" in v for v in res.violations)
    assert res.score <= 50.0


# ── Bathroom ───────────────────────────────────────────────────────────────
def test_bathroom_with_toilet_full():
    """★ 浴室有馬桶 → 滿分。"""
    assert _ev("bathroom", ["toilet"]).score == 100.0


def test_bathroom_with_basin_and_bathtub_full():
    res = _ev("bathroom", ["toilet", "basin", "bathtub"])
    assert res.score == 100.0


def test_bathroom_with_forbidden_bed():
    """★ 浴室出現床(forbidden)→ 重扣。"""
    res = _ev("bathroom", ["toilet", "bed_single"])
    assert res.score < 100.0 and any("bed" in v for v in res.violations)


# ── Study ──────────────────────────────────────────────────────────────────
def test_study_with_desk_full():
    """★ 書房有書桌 → 滿分。"""
    assert _ev("study", ["desk"]).score == 100.0


def test_study_missing_desk():
    res = _ev("study", ["bookshelf"])
    assert "desk" in res.missing and res.score < 100.0


def test_study_with_extra_furniture():
    """★ 書房出現冰箱(既非必要也非偏好也非禁止)→ 進 extra、小扣分。"""
    res = _ev("study", ["desk", "fridge"])
    assert "fridge" in res.extra and res.score < 100.0


# ── Laundry ────────────────────────────────────────────────────────────────
def test_laundry_with_washer_full():
    """★ 洗衣間有洗衣機 → 滿分(washer 為宣告式類別)。"""
    assert _ev("laundry", ["washer"]).score == 100.0


def test_laundry_missing_washer():
    res = _ev("laundry", [])
    assert "washer" in res.missing


# ── Balcony / Entrance ─────────────────────────────────────────────────────
def test_balcony_with_preferred_washer_full():
    """★ 陽台放洗衣機(preferred、無必要家具)→ 滿分。"""
    assert _ev("balcony", ["washer"]).score == 100.0


def test_balcony_with_forbidden_bed():
    res = _ev("balcony", ["bed_double"])
    assert res.score < 100.0 and any("bed" in v for v in res.violations)


def test_entrance_with_shoe_cabinet_full():
    assert _ev("entrance", ["shoe_cabinet"]).score == 100.0


def test_entrance_empty_is_ok():
    """玄關沒有必要家具、min_count 0 → 空著也滿分。"""
    assert _ev("entrance", []).score == 100.0


# ── 數量 ────────────────────────────────────────────────────────────────────
def test_too_many_furniture_penalised():
    """★ 家具過多(超過 max_count)→ 扣分、violations 記太多。"""
    names = ["bed_double"] + ["nightstand"] * 9         # 10 件 > 臥室上限 8
    res = _ev("bedroom", names)
    assert res.score < 100.0 and any("太多" in v for v in res.violations)


def test_too_few_furniture_penalised():
    """★ 家具太少(低於 min_count)→ 扣分。"""
    res = _ev("kitchen", [])                             # 0 < 1
    assert any("太少" in v for v in res.violations)


# ── 未涵蓋房間 / 別名 / 型別 ─────────────────────────────────────────────
def test_unknown_room_kind_is_skipped():
    """★ 未涵蓋的房間(如天井)→ 跳過,滿分、無規則。"""
    res = _ev("patio", ["bed_double"])
    assert res.score == 100.0 and res.violations == []


def test_canonical_room_aliases():
    """★ 房間別名歸一:family→living、single→bedroom、foyer→entrance。"""
    assert canonical_room("family") == "living"
    assert canonical_room("single") == "bedroom"
    assert canonical_room("foyer") == "entrance"
    assert get_room_rule("family").room_kind == "living"


def test_placement_type_maps_names():
    """★ 圖塊名→canonical 類別:sofa3→sofa、餐桌→dining_table、床→bed、流理台→counter。"""
    assert placement_type(_fx("sofa3")) == "sofa"
    assert placement_type(_fx("table4")) == "dining_table"
    assert placement_type(_fx("bed_double")) == "bed"
    assert placement_type(Counter(start=(0, 0), end=(1000, 0))) == "counter"


# ── 全部 Rule / API / Report ──────────────────────────────────────────────
def test_all_listed_rooms_have_rules():
    """★ 題目列的 9 種房間都有規則。"""
    for k in LISTED_ROOMS:
        assert k in ROOM_SEMANTIC_RULES


def test_all_rules_are_well_formed():
    """★ 每條規則:required/preferred/forbidden 互斥、min≤max。"""
    for k, r in ROOM_SEMANTIC_RULES.items():
        assert r.min_count <= r.max_count, k
        assert not (r.required & r.forbidden)
        assert not (r.required & r.preferred)
        assert not (r.preferred & r.forbidden)


def test_result_and_rule_follow_json_convention():
    """★ RoomSemanticResult / RoomSemanticRule 遵循 to_dict/to_json 契約。"""
    res = _ev("bedroom", ["bed_double", "toilet"])
    for obj in (res, ROOM_SEMANTIC_RULES["bedroom"]):
        assert json.loads(obj.to_json()) == obj.to_dict()
        assert "\\u" not in obj.to_json()
    d = res.to_dict()
    assert set(d) >= {"room", "room_kind", "score", "missing", "extra",
                      "violations", "summary"}
    assert isinstance(res, RoomSemanticResult)
    assert res.room_kind in res.summary()


# ── Optimizer 整合 ────────────────────────────────────────────────────────
def test_optimizer_room_semantic_weight_default_is_015():
    """★ room_semantic 權重不寫死:預設 0.15,序列化含此欄。"""
    w = PlacementWeights()
    assert w.room_semantic == 0.15
    assert w.as_map()["room_semantic"] == 0.15
    assert w.to_dict()["room_semantic"] == 0.15


def test_optimizer_result_exposes_room_semantic_score():
    """★ PlacementResult 有 room_semantic_score;report/summary/scores 都含它。"""
    spec = _spec()
    res = FurniturePlacementOptimizer(spec).place("wardrobe", _room(spec, "bedroom"))
    assert res.found
    assert abs(res.room_semantic_score
               - res.best.scores["room_semantic"]) < 1e-6
    assert 0.0 <= res.room_semantic_score <= 100.0
    d = res.to_dict()
    assert "room_semantic_score" in d
    assert "room_semantic" in d["best"]["scores"]
    assert "room_semantic" in res.summary()


def test_room_semantic_is_soft_not_a_hard_gate():
    """★ room_semantic 權重灌爆,best 仍必須通過 collision。"""
    spec = _spec()
    opt = FurniturePlacementOptimizer(spec)
    eng = FurnitureCollisionEngine(spec)
    heavy = PlacementWeights(wall_distance=0.0, window_distance=0.0,
                             walkway=0.0, symmetry=0.0, room_usability=0.0,
                             constraint=0.0, pair_constraint=0.0,
                             human_clearance=0.0, room_semantic=1000.0)
    for kind, name in (("bedroom", "wardrobe"), ("living", "sofa3")):
        res = opt.place(name, _room(spec, kind), weights=heavy)
        assert res.found
        assert eng.check(res.best.placement()).valid


def test_room_semantic_zero_weight_makes_it_irrelevant():
    """★ room_semantic 權重=0:分數怎麼變,總分都不變。"""
    w0 = PlacementWeights(room_semantic=0.0)
    other = {k: 40.0 for k in ALL_KEYS}
    a = {**other, "room_semantic": 0.0}
    b = {**other, "room_semantic": 100.0}
    assert abs(_weighted(a, w0) - _weighted(b, w0)) < 1e-9


def test_room_semantic_weight_dominates_ranking():
    """★ 只有 room_semantic 有權重時,best 的分數 = 合法候選中的最大值。"""
    spec = _spec()
    opt = FurniturePlacementOptimizer(spec)
    room = _room(spec, "bedroom")
    room_poly = Polygon(room.points)
    only = PlacementWeights(wall_distance=0.0, window_distance=0.0,
                            walkway=0.0, symmetry=0.0, room_usability=0.0,
                            constraint=0.0, pair_constraint=0.0,
                            human_clearance=0.0, room_semantic=1.0)
    best_sem = -1.0
    for placement in opt.candidates("wardrobe", room):
        c = opt._score(placement, room, room_poly, None)
        if c.valid:
            best_sem = max(best_sem, c.scores["room_semantic"])
    res = opt.place("wardrobe", room, weights=only)
    assert res.found
    assert abs(res.best.total - best_sem) < 1e-6
    assert abs(res.best.scores["room_semantic"] - best_sem) < 1e-6
