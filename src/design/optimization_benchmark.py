"""Optimization Benchmark(v0.7 Phase 5-7)—— 量化「最佳化到底有沒有用」。

對每一層跑 **Before → Optimize → After**,把兩端的指標並排,證明(或否證)
Layout Optimizer 的價值。這是**工程巡檢台**,不是產品功能,也**不接進生成
流程** —— 故對既有輸出零影響(Regression = 0)。

量測的指標:
    Layout Score            七面向加權總分 + 等級(Phase 5-4)
    Furniture Success Rate  該配家具的房間配齊的比例
    Collision               殘留碰撞數(Phase 1~4 Collision Engine)
    Walk Distance           最遠 / 平均步行距離(Phase 5-3)
    Area Utilization        扣掉純走道後的可用樓地板比
    Constraint Errors       違反設計常規的條數(Phase 5-5)

輸出(output/optimization/):
    optimization.json           完整量測(OptimizationReport.to_json)
    <case>/<floor>_before.dxf   最佳化前的圖
    <case>/<floor>_after.dxf    最佳化後的圖
    <case>/<floor>_before.png   前後預覽圖
    <case>/<floor>_after.png

⚠️ Optimizer 是 **single step**:本模組以「重複呼叫」達成多步,每一步都
獨立通過安全閘門,不是一次大改。

    python -m src.design.optimization_benchmark --limit 8 --steps 3
"""
from __future__ import annotations

import argparse
import copy
import json
import sys
import time
from dataclasses import dataclass, field, replace
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from src.design.benchmark import CASES, render_png
from src.design.building_generator import generate_building
from src.design.collision.detector import find_collisions
from src.design.collision.geometry import collect_active
from src.design.constraints import check_constraints
from src.design.corridor import analyze_corridors
from src.design.layout_generator import generate_floor_plan
from src.design.optimizer import optimize_step
from src.design.report import JsonReport
from src.design.scoring import ScoreWeights, score_layout
from src.drafting.apartment_plan import draw_floor_plan
from src.web.render import _new_doc

OUT_DIR = _PROJECT_ROOT / "output" / "optimization"
DEFAULT_STEPS = 3


# ---------------------------------------------------------------------------
# 指標
# ---------------------------------------------------------------------------
@dataclass
class Metrics(JsonReport):
    """一張圖在某個時間點(before / after)的量測。"""

    score: float = 0.0
    grade: str = ""
    furniture_rate: float = 0.0
    collisions: int = 0
    longest_walk: float = 0.0
    average_walk: float = 0.0
    utilization: float = 0.0
    constraint_errors: int = 0

    def to_dict(self) -> dict:
        return {
            "score": round(self.score, 2),
            "grade": self.grade,
            "furniture_rate": round(self.furniture_rate, 1),
            "collisions": self.collisions,
            "longest_walk": round(self.longest_walk, 1),
            "average_walk": round(self.average_walk, 1),
            "utilization": round(self.utilization, 2),
            "constraint_errors": self.constraint_errors,
        }


def measure(spec, weights: ScoreWeights | None = None) -> Metrics:
    """量一張圖的所有指標(唯讀)。"""
    sc = score_layout(spec, weights)
    corr = analyze_corridors(spec)
    return Metrics(
        score=sc.total,
        grade=sc.grade,
        furniture_rate=sc.get("furniture").score,
        collisions=len(find_collisions(collect_active(spec))),
        longest_walk=corr.longest_distance,
        average_walk=corr.average_distance,
        utilization=sc.get("utilization").score,
        constraint_errors=len(check_constraints(spec).errors))


# ---------------------------------------------------------------------------
# 單層結果
# ---------------------------------------------------------------------------
@dataclass
class FloorOptimization(JsonReport):
    """一層樓的 Before → Optimize → After。"""

    case: str = ""
    floor: str = ""
    before: Metrics = field(default_factory=Metrics)
    after: Metrics = field(default_factory=Metrics)
    steps: list = field(default_factory=list)        # 每一步的 OptimizeStep dict
    seconds: float = 0.0
    files: dict = field(default_factory=dict)

    @property
    def applied(self) -> int:
        return sum(1 for s in self.steps if s.get("applied"))

    @property
    def score_delta(self) -> float:
        return self.after.score - self.before.score

    @property
    def improved(self) -> bool:
        return self.score_delta > 1e-9 or \
            self.after.constraint_errors < self.before.constraint_errors

    @property
    def regressed(self) -> bool:
        """任何指標變差都算退步——這是本 benchmark 最該盯的欄位。"""
        return (self.after.score < self.before.score - 1e-9
                or self.after.collisions > self.before.collisions
                or self.after.constraint_errors > self.before.constraint_errors)

    def to_dict(self) -> dict:
        return {
            "case": self.case, "floor": self.floor,
            "before": self.before.to_dict(), "after": self.after.to_dict(),
            "applied": self.applied, "improved": self.improved,
            "regressed": self.regressed,
            "score_delta": round(self.score_delta, 3),
            "seconds": round(self.seconds, 2),
            "steps": list(self.steps),
            "files": dict(self.files),
        }


@dataclass
class OptimizationReport(JsonReport):
    """整份 Optimization Benchmark 的結果。"""

    floors: list = field(default_factory=list)       # list[FloorOptimization]
    steps_limit: int = DEFAULT_STEPS
    seconds: float = 0.0

    @property
    def improved(self) -> int:
        return sum(1 for f in self.floors if f.improved)

    @property
    def regressed(self) -> int:
        return sum(1 for f in self.floors if f.regressed)

    @property
    def total_applied(self) -> int:
        return sum(f.applied for f in self.floors)

    @property
    def mean_delta(self) -> float:
        return (sum(f.score_delta for f in self.floors) / len(self.floors)
                if self.floors else 0.0)

    def to_dict(self) -> dict:
        return {
            "floors_tested": len(self.floors),
            "steps_limit": self.steps_limit,
            "improved": self.improved,
            "regressed": self.regressed,
            "total_steps_applied": self.total_applied,
            "mean_score_delta": round(self.mean_delta, 3),
            "seconds": round(self.seconds, 1),
            "results": [f.to_dict() for f in self.floors],
        }

    def summary(self) -> str:
        head = (f"OptimizationReport:{len(self.floors)} 層 · "
                f"每層最多 {self.steps_limit} 步 → "
                f"改善 {self.improved} · 退步 {self.regressed} · "
                f"套用 {self.total_applied} 步 · "
                f"平均分數 {self.mean_delta:+.3f}")
        lines = [head,
                 f"{'case':6} {'floor':6} {'score':>16} {'coll':>6} "
                 f"{'err':>5} {'walk(m)':>9} {'util':>7} {'steps':>6}"]
        for f in self.floors:
            lines.append(
                f"{f.case:6} {f.floor:6} "
                f"{f.before.score:6.2f}→{f.after.score:6.2f}  "
                f"{f.before.collisions:2}→{f.after.collisions:<2} "
                f"{f.before.constraint_errors:2}→{f.after.constraint_errors:<2} "
                f"{f.before.longest_walk/1000:4.1f}→{f.after.longest_walk/1000:<4.1f} "
                f"{f.before.utilization:5.1f}→{f.after.utilization:<5.1f} "
                f"{f.applied:6}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# 出圖
# ---------------------------------------------------------------------------
def _doc(spec):
    """預覽用 ezdxf 文件(去圖框/標題欄,和 benchmark 的預覽版一致)。"""
    doc, layers = _new_doc()
    draw_floor_plan(doc.modelspace(),
                    replace(spec, sheet=False, title_block=None), layers)
    return doc


def _emit(spec, path_stem: Path, render: bool) -> dict:
    """輸出 DXF(+ 可選 PNG),回檔名對照。"""
    out = {}
    dxf = path_stem.with_suffix(".dxf")
    doc = _doc(spec)
    doc.saveas(dxf)
    out["dxf"] = dxf.name
    if render:
        png = path_stem.with_suffix(".png")
        render_png(doc, png)
        out["png"] = png.name
    return out


# ---------------------------------------------------------------------------
# 執行
# ---------------------------------------------------------------------------
def _floor_specs(case):
    kind, brief = case.build()
    if kind == "single":
        return [("1F", generate_floor_plan(brief))]
    return [(fl.label, fl.spec) for fl in generate_building(brief).floors]


def run_floor(case_id: str, label: str, spec, steps: int,
              out_dir: Path, render: bool,
              weights: ScoreWeights | None = None) -> FloorOptimization:
    """單層:量 before → 重複單步最佳化 → 量 after → 出圖。"""
    t0 = time.time()
    res = FloorOptimization(case=case_id, floor=label, before=measure(spec, weights))

    work = copy.deepcopy(spec)                       # 不動呼叫端的 spec
    for _ in range(steps):
        step = optimize_step(work, weights)          # ⚠️ 一次只走一步
        res.steps.append(step.to_dict())
        if not step.applied:
            break                                    # 沒有可行候選就停

    res.after = measure(work, weights)
    if out_dir is not None:
        case_dir = out_dir / case_id
        case_dir.mkdir(parents=True, exist_ok=True)
        files = {}
        for tag, s in (("before", spec), ("after", work)):
            for k, v in _emit(s, case_dir / f"{label}_{tag}", render).items():
                files[f"{tag}_{k}"] = f"{case_id}/{v}"
        res.files = files
    res.seconds = time.time() - t0
    return res


def run(limit: int | None = None, steps: int = DEFAULT_STEPS,
        out_dir: Path | None = OUT_DIR, render: bool = True,
        cases=None, verbose: bool = True) -> OptimizationReport:
    """跑 Optimization Benchmark。out_dir=None 表示不輸出檔案(測試用)。"""
    picked = list(cases if cases is not None else CASES)
    if limit:
        picked = picked[:limit]
    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)

    report = OptimizationReport(steps_limit=steps)
    t0 = time.time()
    for n, case in enumerate(picked, 1):
        try:
            floors = _floor_specs(case)
        except Exception as exc:                     # noqa: BLE001
            if verbose:
                print(f"[{n}/{len(picked)}] {case.cid} 生成失敗:{exc}")
            continue
        for label, spec in floors:
            res = run_floor(case.cid, label, spec, steps, out_dir, render)
            report.floors.append(res)
            if verbose:
                print(f"[{n}/{len(picked)}] {case.cid} {label} … "
                      f"{res.before.score:.2f}→{res.after.score:.2f} "
                      f"({res.applied} 步, {res.seconds:.1f}s)")
    report.seconds = time.time() - t0

    if out_dir is not None:
        (out_dir / "optimization.json").write_text(
            report.to_json(), encoding="utf-8")
    return report


def main() -> None:
    ap = argparse.ArgumentParser(description="Optimization Benchmark")
    ap.add_argument("--limit", type=int, default=8, help="只跑前 N 個案例")
    ap.add_argument("--steps", type=int, default=DEFAULT_STEPS,
                    help="每層最多幾個單步(預設 3)")
    ap.add_argument("--no-render", action="store_true", help="不算 PNG(較快)")
    args = ap.parse_args()

    report = run(limit=args.limit, steps=args.steps, render=not args.no_render)
    print()
    print(report.summary())
    print(f"\n報告:{OUT_DIR / 'optimization.json'}")


if __name__ == "__main__":
    main()
