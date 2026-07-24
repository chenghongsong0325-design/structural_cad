"""Auto Layout Engine — 搜尋式(v0.7 Phase 7.0)。

把 Phase 1~6 串成完整的自動配置流程:**產生多個候選 Layout → 用 Phase 6 的
Global Layout Score 評分 → 挑最高分**。第一版只做 Random Search。

    User Input → 產生候選(換 seed / 換擺法…)→ 評分(Phase 6)→ 記錄
              → 重複 N 次 → 選最高分 → 交 DXF/JSON/報表輸出

⚠️ **不新增任何評分規則、不改 Phase 1~6**:評分一律呼叫既有 Global Layout
Score(score_report),搜尋只負責「產生候選 + 記錄 + 挑最好」。候選由外部注入的
generator 產生(每次都是全新 spec),搜尋不改任何既有 spec。

⚠️ **搜尋策略與評分器解耦**:SearchStrategy 是抽象介面,Random Search 是第一個
實作;之後要換 Beam Search / Simulated Annealing / Genetic Algorithm,只要新增一個
SearchStrategy 子類別、不動評分器也不動引擎。

⚠️ 命名:本模組是「**搜尋式**自動配置」(LayoutSearchEngine),與 Phase 6-9 的
`auto_layout_engine.AutoLayoutEngine`(單次逐房最佳化包裝)分開,避免混淆。

⚠️ 已知限制(據實記錄):目前的 Room Generator 對同一需求換 seed 會產生等價品質
的 Layout(實測各 seed 都 93.5 分),故在現行產生器上 Random Search 的「最佳」與
任一候選幾乎同分——搜尋框架是對的,但要真正拉開分數差,需要一個「會改變家具擺法
/分數」的候選 generator(可注入,見 candidate 參數)。

典型用法::

    from src.design.layout.layout_search import LayoutSearchEngine
    from src.design.layout_generator import HouseBrief

    eng = LayoutSearchEngine.from_brief(
        HouseBrief(site_width=20000, site_depth=14000, bedrooms=3))
    res = eng.search(search_count=100)
    res.best_score, res.best_grade, res.best_layout   # best_layout = spec,可畫 DXF
"""
from __future__ import annotations

import copy
import random
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, replace

from src.design.layout.global_score import grade_of, score_report
from src.design.report import JsonReport

# 隨機 seed 的取樣範圍(與 web 端 E2 變體一致)。
SEED_SPACE = 1_000_000


# ── 候選(一次評分結果的精簡紀錄)──────────────────────────────────────────
@dataclass
class Candidate(JsonReport):
    """一個被評過分的候選:用什麼 seed 產生、得幾分。"""

    seed: int
    score: float
    grade: str = ""

    def __post_init__(self):
        if not self.grade:
            self.grade = grade_of(self.score)

    def to_dict(self) -> dict:
        return {"seed": self.seed, "score": round(self.score, 1),
                "grade": self.grade}


# ── 搜尋策略(可插拔;Random 是第一個實作)────────────────────────────────
class SearchStrategy(ABC):
    """決定「下一個候選要用什麼 seed」的策略。**與評分器解耦**。

    Random Search 忽略歷史、每次隨機;未來 Beam/SA/GA 可看 history(已評候選及
    其分數)來提議下一個候選——只要覆寫 propose(),引擎與評分器都不必動。
    """

    name = "base"

    @abstractmethod
    def propose(self, iteration: int, history: list[Candidate]) -> int:
        """回傳第 iteration 個候選要用的 seed(可參考已評 history)。"""

    def stop(self, iteration: int, history: list[Candidate]) -> bool:
        """提前停止條件(預設不停,由 search_count 控制)。"""
        return False


class RandomSearchStrategy(SearchStrategy):
    """Random Search:每次抽一個隨機 seed(忽略歷史)。

    以 rng_seed 播種,故「同 rng_seed + 同 search_count」的搜尋結果可重現。"""

    name = "random"

    def __init__(self, rng_seed: int = 0, seed_space: int = SEED_SPACE):
        self._rng = random.Random(rng_seed)
        self.seed_space = seed_space

    def propose(self, iteration: int, history: list[Candidate]) -> int:
        return self._rng.randrange(self.seed_space)


# ── 搜尋結果(Best Layout Manager 的產物)──────────────────────────────────
@dataclass
class SearchResult(JsonReport):
    """一次搜尋的結果。best_layout 是最佳方案的 spec(可直接交 DXF Export)。"""

    best_seed: int = 0
    best_score: float = 0.0
    best_grade: str = "D"
    best_layout: object = None                    # spec(不進 to_dict)
    best_report: dict = field(default_factory=dict)   # 最佳方案的 Phase 6 評分報表
    layout_count: int = 0                         # 成功評分的候選數
    failed_count: int = 0                         # 產生/評分失敗而略過的候選數
    elapsed_time: float = 0.0                     # 秒
    strategy: str = "random"
    top: list = field(default_factory=list)       # list[Candidate](Top-N,分數高→低)

    @property
    def found(self) -> bool:
        return self.best_layout is not None

    def to_dict(self) -> dict:
        return {
            "strategy": self.strategy,
            "best_seed": self.best_seed,
            "best_score": round(self.best_score, 1),
            "best_grade": self.best_grade,
            "layout_count": self.layout_count,
            "failed_count": self.failed_count,
            "elapsed_time": round(self.elapsed_time, 3),
            "top": [c.to_dict() for c in self.top],
            "best_report": self.best_report,
        }

    def summary(self) -> str:
        if not self.found:
            return (f"LayoutSearch[{self.strategy}]:試了 {self.layout_count} 個候選"
                    f"都失敗,找不到方案")
        head = (f"LayoutSearch[{self.strategy}]:最佳 {self.best_score:.1f} 分 "
                f"[{self.best_grade}](seed {self.best_seed})· "
                f"評分 {self.layout_count} 個候選 / 略過 {self.failed_count} · "
                f"{self.elapsed_time:.1f}s")
        lines = [head, "  Top:"]
        for i, c in enumerate(self.top, 1):
            lines.append(f"    {i}. seed {c.seed} → {c.score:.1f} [{c.grade}]")
        return "\n".join(lines)

    def __str__(self) -> str:
        return self.summary()


# ── 引擎(Search Manager)──────────────────────────────────────────────────
def _global_score(spec) -> float:
    """預設評分器:直接用 Phase 6 的 Global Layout Score(不新增任何規則)。"""
    return score_report(spec)["overall_score"]


class LayoutSearchEngine:
    """搜尋式自動配置引擎。**唯讀 w.r.t. 既有資料**:候選由 generator 現產,
    最佳方案存深拷貝。

    candidate:make_spec(seed) -> spec(FloorPlanSpec 或 BuildingSpec)。
    evaluate :spec -> float 分數(預設 = Phase 6 Global Layout Score;要換評分器
              也只換這個函式,不改引擎)。
    strategy :SearchStrategy(預設 RandomSearchStrategy)。
    """

    def __init__(self, candidate, *, evaluate=None, strategy: SearchStrategy | None = None):
        self.candidate = candidate
        self.evaluate = evaluate or _global_score
        self.strategy = strategy or RandomSearchStrategy()

    @classmethod
    def from_brief(cls, brief, *, evaluate=None,
                   strategy: SearchStrategy | None = None) -> "LayoutSearchEngine":
        """由需求(HouseBrief / CorridorBrief / BuildingBrief)建立引擎:
        候選 = 用不同 seed 生成的 Layout(E2 設計變體)。"""
        return cls(seed_candidate(brief), evaluate=evaluate, strategy=strategy)

    def search(self, search_count: int = 100, *, top_n: int = 5) -> SearchResult:
        """跑 search_count 次 Random Search,回傳最佳 Layout。

        每次:strategy 提議 seed → generator 產生候選 → evaluate 評分 → 記錄;
        產生或評分失敗的候選略過(計入 failed_count),不讓單一壞候選中斷搜尋。
        """
        start = time.perf_counter()
        history: list[Candidate] = []
        result = SearchResult(strategy=self.strategy.name)
        best_spec = None
        best_report: dict = {}

        for i in range(search_count):
            if self.strategy.stop(i, history):
                break
            seed = self.strategy.propose(i, history)
            try:
                spec = self.candidate(seed)
                score = float(self.evaluate(spec))
            except Exception:                      # 壞候選略過,不中斷搜尋
                result.failed_count += 1
                continue

            cand = Candidate(seed=seed, score=score)
            history.append(cand)
            if best_spec is None or score > result.best_score:
                result.best_seed = seed
                result.best_score = score
                result.best_grade = cand.grade
                best_spec = copy.deepcopy(spec)    # 最佳方案存深拷貝

        result.layout_count = len(history)
        result.elapsed_time = time.perf_counter() - start
        if best_spec is not None:
            result.best_layout = best_spec
            try:                                   # 最佳方案的完整 Phase 6 報表
                result.best_report = score_report(
                    best_spec, name=f"seed{result.best_seed}")
            except Exception:                      # 自訂評分器/非 spec 候選時容錯
                result.best_report = {}
        result.top = sorted(history, key=lambda c: c.score, reverse=True)[:top_n]
        return result


# ── 候選 generator:用不同 seed 生成 Layout ────────────────────────────────
def _with_seed(brief, seed: int):
    """回一份把 seed 換掉的 brief 副本(BuildingBrief 換到內層 typical)。"""
    if hasattr(brief, "typical"):                  # BuildingBrief
        t = brief.typical
        if hasattr(t, "seed"):
            return replace(brief, typical=replace(t, seed=seed))
        return brief
    if hasattr(brief, "seed"):
        return replace(brief, seed=seed)
    return brief


def seed_candidate(brief):
    """建立 make_spec(seed) -> spec:同一需求、換 seed 生成不同設計變體。

    BuildingBrief → generate_building;HouseBrief/CorridorBrief → generate_floor_plan。
    每次都產生**全新** spec,天然與其他候選隔離(不共享、不互相污染)。"""
    from src.design.building_generator import BuildingBrief, generate_building
    from src.design.layout_generator import generate_floor_plan
    is_building = isinstance(brief, BuildingBrief)

    def make(seed: int):
        b = _with_seed(brief, seed)
        return generate_building(b) if is_building else generate_floor_plan(b)

    return make
