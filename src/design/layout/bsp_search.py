"""BSP Layout Search(v0.7 Phase 7.1c)—— 把「BSP 生成 + 家具 + Phase 6 評分」接成

Phase 7 的搜尋:**隨機刀位產生多個結構不同的方案 → 各自配家具 → Phase 6 評分 →
挑最高 + 列 Top-N**。這就是最初要的「像建築師產生多種方案並挑最好」真正跑起來。

    seed → 隨機 θ → bsp_to_spec(7.1a)→ furnish_spec(7.1b)→ Phase 6 評分(搜尋器)

⚠️ 這裡只做**接線**,不新增任何生成/評分/搜尋邏輯:
  * 生成 = bsp_layout.bsp_to_spec(7.1a)
  * 家具 = auto_furnish.furnish_spec(7.1b,位置由 Phase 6 optimizer 決定)
  * 評分 = global_score.score_report(Phase 6)
  * 搜尋 = layout_search.LayoutSearchEngine(Phase 7.0,Random Search,可插拔策略)

⚠️ 決定性:候選 seed → np.random.default_rng(seed) → 固定 θ → 固定方案;搭配
RandomSearchStrategy(rng_seed) 播種,整場搜尋可重現。

⚠️ 成本:配家具是瓶頸(~0.85s/案),search_count 越大越慢;預設 30 約 25 秒。

典型用法::

    from src.design.layout.bsp_search import bsp_search, portfolio
    res = bsp_search(20000, 14000, bedrooms=3, search_count=30)
    res.best_layout            # 最佳方案 spec,可畫 DXF / 交 Phase 6 報表
    for cand, spec in portfolio(res, 20000, 14000, 3):   # Top-N 提案(建築師的多案)
        ...
"""
from __future__ import annotations

import numpy as np

from src.design.layout.auto_furnish import furnish_spec
from src.design.layout.bsp_layout import bsp_to_spec, random_theta
from src.design.layout.layout_search import (
    LayoutSearchEngine,
    RandomSearchStrategy,
    SearchResult,
)


def spec_for_seed(seed: int, site_w_mm: float, site_d_mm: float, bedrooms: int,
                  *, furnish: bool = True, weights=None):
    """由候選 seed 決定性地重建那個方案的 spec(給 Top-N 出圖 / 重看用)。"""
    rng = np.random.default_rng(seed)
    theta, flags = random_theta(rng)
    spec = bsp_to_spec(theta, flags, site_w_mm, site_d_mm, bedrooms)
    if furnish:
        furnish_spec(spec, weights=weights)
    return spec


def bsp_candidate(site_w_mm: float, site_d_mm: float, bedrooms: int,
                  *, furnish: bool = True, weights=None):
    """建立 make(seed) -> 已配家具的 BSP 方案 spec(給 LayoutSearchEngine 當候選源)。"""
    def make(seed: int):
        return spec_for_seed(seed, site_w_mm, site_d_mm, bedrooms,
                             furnish=furnish, weights=weights)
    return make


def bsp_search(site_w_mm: float, site_d_mm: float, bedrooms: int, *,
               search_count: int = 30, top_n: int = 5,
               furnish: bool = True, rng_seed: int = 0,
               weights=None) -> SearchResult:
    """對一個需求跑 BSP Random Search,回最佳方案 + Top-N。

    重用 Phase 7.0 的 LayoutSearchEngine:候選 = 隨機 BSP 方案(配家具),
    評分 = Phase 6 Global Score(預設)。"""
    engine = LayoutSearchEngine(
        bsp_candidate(site_w_mm, site_d_mm, bedrooms,
                      furnish=furnish, weights=weights),
        strategy=RandomSearchStrategy(rng_seed=rng_seed))
    return engine.search(search_count=search_count, top_n=top_n)


def portfolio(result: SearchResult, site_w_mm: float, site_d_mm: float,
              bedrooms: int, *, furnish: bool = True, weights=None) -> list:
    """把搜尋結果的 Top-N 還原成 [(Candidate, spec)]——建築師的多案提案。

    每個提案的 spec 由該候選 seed 決定性重建(與搜尋時評的是同一個方案)。"""
    return [(cand, spec_for_seed(cand.seed, site_w_mm, site_d_mm, bedrooms,
                                 furnish=furnish, weights=weights))
            for cand in result.top]
