"""BSP Layout Search 測試(v0.7 Phase 7.1c)。

把「BSP 生成 + 家具 + Phase 6 評分」接成 Phase 7 Random Search:隨機刀位產生多個
結構不同的方案 → 配家具 → Phase 6 評分 → 挑最高 + Top-N。重點:

  * 搜尋回最佳方案:已配家具、collision-valid、可畫 DXF。
  * best_score = Top-N 最高;Top-N 依分數排序。
  * 決定性:同 rng_seed → 同結果;portfolio 還原的 spec 與搜尋時評的是同一個
    (分數一致)。
  * 只接線、不新增邏輯:生成/家具/評分/搜尋全用既有模組。

⚠️ 配家具較慢(~0.85s/案),測試用小 search_count。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.design.collision.furniture_engine import FurnitureCollisionEngine
from src.design.layout.bsp_search import (
    bsp_candidate,
    bsp_search,
    portfolio,
    spec_for_seed,
)
from src.design.layout.global_score import score_report
from src.design.layout.layout_search import SearchResult
from src.drafting.apartment_plan import draw_floor_plan
from src.drafting.fixtures import FixturePlacement
from src.web.render import _new_doc

SITE_W, SITE_D, BEDS = 18000.0, 12000.0, 2

# 一次搜尋,多數測試共用(配家具較慢)。
RES = bsp_search(SITE_W, SITE_D, BEDS, search_count=8, top_n=3, rng_seed=0)


def _valid(spec):
    checks = FurnitureCollisionEngine(spec).check_existing()
    return bool(checks) and all(r.valid for _, r in checks)


# ── 搜尋結果 ────────────────────────────────────────────────────────────────
def test_search_returns_best_layout():
    assert isinstance(RES, SearchResult) and RES.found
    assert RES.layout_count == 8 and RES.failed_count == 0
    assert RES.best_grade in {"A+", "A", "B", "C", "D"}


def test_best_is_furnished_valid_and_drawable():
    """★ 最佳方案:配了家具、collision-valid、畫得出 DXF。"""
    spec = RES.best_layout
    assert sum(isinstance(f, FixturePlacement) for f in spec.fixtures) > 5
    assert _valid(spec)
    doc, layers = _new_doc()
    draw_floor_plan(doc.modelspace(), spec, layers)
    assert len(list(doc.modelspace())) > 0


def test_best_score_is_top_of_ranking():
    """★ best = Top-N 最高;Top-N 依分數由高到低。"""
    assert RES.best_score == RES.top[0].score
    scores = [c.score for c in RES.top]
    assert scores == sorted(scores, reverse=True)
    assert RES.best_report["overall_score"] == RES.best_score


# ── 決定性 / 提案還原 ───────────────────────────────────────────────────────
def test_reproducible_with_same_rng_seed():
    """★ 同 rng_seed → 同最佳方案(整場搜尋可重現)。"""
    again = bsp_search(SITE_W, SITE_D, BEDS, search_count=8, rng_seed=0)
    assert again.best_seed == RES.best_seed
    assert again.best_score == RES.best_score


def test_spec_for_seed_is_deterministic():
    a = spec_for_seed(RES.best_seed, SITE_W, SITE_D, BEDS)
    b = spec_for_seed(RES.best_seed, SITE_W, SITE_D, BEDS)
    assert [r.points for r in a.rooms] == [r.points for r in b.rooms]
    assert len(a.fixtures) == len(b.fixtures)


def test_portfolio_specs_match_their_candidate_scores():
    """★ Top-N 提案:每個還原的 spec,分數 = 搜尋時記的候選分數(同一個方案)。"""
    props = portfolio(RES, SITE_W, SITE_D, BEDS)
    assert len(props) == len(RES.top)
    for cand, spec in props:
        assert abs(score_report(spec)["overall_score"] - cand.score) < 1e-6
        assert _valid(spec)                          # 每個提案都合法可用


def test_portfolio_offers_distinct_schemes():
    """★ Top-N 是不同方案(seed 不同 → 佈局不同)。"""
    props = portfolio(RES, SITE_W, SITE_D, BEDS)
    seeds = [c.seed for c, _ in props]
    assert len(set(seeds)) == len(seeds)             # seed 互異
    area_sigs = {tuple(sorted(round(__import__("shapely.geometry",
                 fromlist=["Polygon"]).Polygon(r.points).area) for r in s.rooms))
                 for _, s in props}
    assert len(area_sigs) > 1                         # 佈局真的不同


# ── 候選源 / furnish 開關 ───────────────────────────────────────────────────
def test_bsp_candidate_produces_furnished_spec():
    make = bsp_candidate(SITE_W, SITE_D, BEDS)
    spec = make(123)
    assert sum(isinstance(f, FixturePlacement) for f in spec.fixtures) > 0
    assert _valid(spec)


def test_furnish_false_gives_bare_but_scoreable():
    """furnish=False:候選不配家具(furniture 子分數 0),搜尋仍跑得動。"""
    res = bsp_search(SITE_W, SITE_D, BEDS, search_count=4, furnish=False,
                     rng_seed=1)
    assert res.found
    assert res.best_report["sub_scores"]["furniture"] == 0.0
