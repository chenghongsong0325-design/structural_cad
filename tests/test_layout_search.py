"""Auto Layout Engine — 搜尋式(v0.7 Phase 7.0)測試。

Random Search:產生多個候選 Layout → 用 Phase 6 Global Score 評分 → 挑最高分。
重點:

  * 引擎跑得動、回傳最佳方案(best_layout 是可畫 DXF 的 spec)。
  * best_score 一定是所有候選裡最高的(Best Layout Manager 正確)。
  * 評分一律走 Phase 6 Global Score,不新增評分規則。
  * 搜尋策略與評分器解耦:換 SearchStrategy 不動引擎;失敗候選略過不中斷。
  * 唯讀:搜尋不改既有 spec;輸出欄位齊全(best_layout/best_score/layout_count/
    elapsed_time/summary)。

⚠️ 已知:現行產生器對同需求換 seed 會等價同分,故真實搜尋各候選同分——用自訂
評分器製造分數差,驗證「挑最高」的邏輯。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.design.building_generator import BuildingBrief, generate_building
from src.design.layout.global_score import grade_of, score_report
from src.design.layout.layout_search import (
    Candidate,
    LayoutSearchEngine,
    RandomSearchStrategy,
    SearchResult,
    SearchStrategy,
    seed_candidate,
)
from src.design.layout_generator import HouseBrief, generate_floor_plan
from src.drafting.apartment_plan import draw_floor_plan
from src.web.render import _new_doc

GRADES = {"A+", "A", "B", "C", "D"}


def _brief(bedrooms=3):
    return HouseBrief(site_width=20000, site_depth=14000, bedrooms=bedrooms)


# ── 真實產生器:引擎跑得動、輸出齊全 ────────────────────────────────────
def test_search_runs_and_returns_best_layout():
    """★ 搜尋回傳最佳方案,分數/等第在範圍,best_layout 是 spec。"""
    res = LayoutSearchEngine.from_brief(_brief()).search(search_count=6)
    assert isinstance(res, SearchResult) and res.found
    assert 0.0 <= res.best_score <= 100.0 and res.best_grade in GRADES
    assert res.layout_count == 6 and res.failed_count == 0
    assert res.elapsed_time >= 0.0
    assert hasattr(res.best_layout, "rooms")            # FloorPlanSpec


def test_best_layout_is_dxf_exportable():
    """★ 最佳方案可直接交 DXF Export(畫得出實體)。"""
    res = LayoutSearchEngine.from_brief(_brief()).search(search_count=4)
    doc, layers = _new_doc()
    draw_floor_plan(doc.modelspace(), res.best_layout, layers)
    assert len(list(doc.modelspace())) > 0


def test_best_report_uses_phase6_global_score():
    """★ 最佳方案的報表 = Phase 6 Global Score(12 子分數 + grade,不另立規則)。"""
    res = LayoutSearchEngine.from_brief(_brief()).search(search_count=3)
    assert set(res.best_report) >= {"overall_score", "grade", "sub_scores",
                                    "rooms"}
    assert len(res.best_report["sub_scores"]) == 12
    assert abs(res.best_report["overall_score"] - res.best_score) < 1e-6


def test_evaluate_defaults_to_global_score():
    """引擎預設評分器就是 Phase 6 Global Score(對同一 spec 分數一致)。"""
    spec = generate_floor_plan(_brief())
    eng = LayoutSearchEngine(candidate=lambda s: spec)
    assert abs(eng.evaluate(spec) - score_report(spec)["overall_score"]) < 1e-6


# ── Best Layout Manager:一定挑最高分 ───────────────────────────────────
def test_best_is_the_maximum_scoring_candidate():
    """★ best_score = 所有評過候選裡的最大值(用自訂評分器製造分數差)。"""
    # 候選 = seed 本身(int);評分 = seed%97(製造高低差,可驗證挑最高)
    eng = LayoutSearchEngine(candidate=lambda s: s,
                             evaluate=lambda spec: spec % 97,
                             strategy=RandomSearchStrategy(rng_seed=3))
    res = eng.search(search_count=200, top_n=5)
    assert res.layout_count == 200
    assert res.best_score == max(c.score for c in res.top)
    assert res.best_score == res.best_seed % 97
    # top 依分數由高到低、且不超過 top_n
    scores = [c.score for c in res.top]
    assert scores == sorted(scores, reverse=True) and len(res.top) <= 5


def test_top_n_interface_present():
    """★ Top-N 介面:top 是 Candidate 清單,長度 = min(top_n, 候選數)。"""
    eng = LayoutSearchEngine(candidate=lambda s: s,
                             evaluate=lambda spec: float(spec),
                             strategy=RandomSearchStrategy(rng_seed=0))
    res = eng.search(search_count=10, top_n=3)
    assert len(res.top) == 3 and all(isinstance(c, Candidate) for c in res.top)


# ── 決定性 / 可重現 ─────────────────────────────────────────────────────
def test_random_search_is_reproducible_with_same_rng_seed():
    """★ 同 rng_seed + 同次數 → 同結果(RandomSearchStrategy 播種)。"""
    def run():
        return LayoutSearchEngine(
            candidate=lambda s: s, evaluate=lambda spec: spec % 1000,
            strategy=RandomSearchStrategy(rng_seed=42)).search(search_count=30)
    a, b = run(), run()
    assert a.best_seed == b.best_seed and a.best_score == b.best_score


def test_different_rng_seed_explores_different_seeds():
    a = LayoutSearchEngine(candidate=lambda s: s, evaluate=lambda spec: spec,
                           strategy=RandomSearchStrategy(rng_seed=1)
                           ).search(search_count=20)
    b = LayoutSearchEngine(candidate=lambda s: s, evaluate=lambda spec: spec,
                           strategy=RandomSearchStrategy(rng_seed=2)
                           ).search(search_count=20)
    assert [c.seed for c in a.top] != [c.seed for c in b.top]


# ── 失敗候選略過、不中斷 ─────────────────────────────────────────────────
def test_failed_candidates_are_skipped_not_fatal():
    """★ 產生/評分丟例外的候選略過(計入 failed_count),搜尋照常挑最高。"""
    def evaluate(spec):
        if spec % 2 == 0:
            raise ValueError("壞候選")
        return spec % 50
    eng = LayoutSearchEngine(candidate=lambda s: s, evaluate=evaluate,
                             strategy=RandomSearchStrategy(rng_seed=7))
    res = eng.search(search_count=100)
    assert res.found
    assert res.layout_count + res.failed_count == 100
    assert res.failed_count > 0                          # 真的有被略過的
    assert res.best_seed % 2 == 1                         # best 一定是合法(奇數)


def test_all_candidates_fail_returns_not_found():
    eng = LayoutSearchEngine(
        candidate=lambda s: s,
        evaluate=lambda spec: (_ for _ in ()).throw(ValueError("always")))
    res = eng.search(search_count=10)
    assert not res.found and res.best_layout is None
    assert res.failed_count == 10 and res.layout_count == 0
    assert "找不到" in res.summary()


# ── 策略解耦:可換自訂 SearchStrategy ───────────────────────────────────
def test_custom_strategy_plugs_in_without_touching_engine():
    """★ 換一個「照順序枚舉 seed」的策略,引擎與評分器都不必改。"""
    class Enumerate(SearchStrategy):
        name = "enumerate"

        def propose(self, iteration, history):
            return iteration                             # 0,1,2,…

    eng = LayoutSearchEngine(candidate=lambda s: s, evaluate=lambda spec: spec,
                             strategy=Enumerate())
    res = eng.search(search_count=10)
    assert res.strategy == "enumerate"
    assert res.best_seed == 9 and res.best_score == 9     # 枚舉 0~9,最大=9


def test_strategy_stop_condition_halts_early():
    """★ 策略可提前停止(未來 SA/GA 的早停介面)。"""
    class StopAt5(SearchStrategy):
        name = "stop5"

        def propose(self, iteration, history):
            return iteration

        def stop(self, iteration, history):
            return iteration >= 5

    res = LayoutSearchEngine(candidate=lambda s: s, evaluate=lambda spec: spec,
                             strategy=StopAt5()).search(search_count=100)
    assert res.layout_count == 5                          # 第 5 次就停


# ── 唯讀 / 建棟 / 序列化 ─────────────────────────────────────────────────
def test_search_on_building_brief():
    """★ 吃 BuildingBrief:候選 = 整棟,best_layout 是 BuildingSpec。"""
    b = BuildingBrief(typical=_brief(), floors=2)
    res = LayoutSearchEngine.from_brief(b).search(search_count=3)
    assert res.found and hasattr(res.best_layout, "floors")
    assert res.best_grade in GRADES


def test_seed_candidate_produces_fresh_specs():
    """★ 候選 generator 每次產生全新 spec(彼此獨立,不共享)。"""
    make = seed_candidate(_brief())
    s1, s2 = make(1), make(2)
    assert s1 is not s2 and s1.fixtures is not s2.fixtures


def test_result_follows_json_convention():
    """★ SearchResult 遵循 to_dict/to_json;best_layout(spec)不進 JSON。"""
    res = LayoutSearchEngine.from_brief(_brief()).search(search_count=3, top_n=2)
    d = res.to_dict()
    json.dumps(d)                                        # 純原生 → dumps 得出來
    assert "best_layout" not in d                        # spec 不序列化
    assert set(d) >= {"strategy", "best_seed", "best_score", "best_grade",
                      "layout_count", "elapsed_time", "top", "best_report"}
    assert res.best_grade in res.summary()


def test_candidate_grade_derived_from_score():
    c = Candidate(seed=1, score=85.0)
    assert c.grade == grade_of(85.0) == "A"
    assert c.to_dict() == {"seed": 1, "score": 85.0, "grade": "A"}
