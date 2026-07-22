"""Optimization Benchmark 測試(v0.7 Phase 5-7)。

重點:
  * 量測欄位齊全且語意正確(before / after 並排)。
  * **不得退步**——benchmark 最該盯的就是 regressed 欄位。
  * 呼叫端的 spec 不可被動到(harness 在 deepcopy 上最佳化)。
  * 檔案輸出(DXF/PNG/JSON)可開、可讀。

測試刻意只跑 1~2 層、1~2 步,因為每個單步要試算幾十個候選,很慢。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.design.benchmark import CASES
from src.design.layout_generator import (
    HouseBrief,
    generate_floor_plan,
    validate_spec,
)
from src.design.optimization_benchmark import (
    FloorOptimization,
    Metrics,
    OptimizationReport,
    measure,
    run,
    run_floor,
)


def _spec():
    return generate_floor_plan(
        HouseBrief(site_width=20000, site_depth=14000, bedrooms=3))


# ── 指標 ──────────────────────────────────────────────────────────────────
def test_measure_reports_every_required_metric():
    """七項指標都要量得到,且落在合理範圍。"""
    m = measure(_spec())
    assert isinstance(m, Metrics)
    assert 0 <= m.score <= 100 and m.grade in ("A", "B", "C", "D", "F")
    assert 0 <= m.furniture_rate <= 100
    assert 0 <= m.utilization <= 100
    assert m.collisions == 0                        # 合格圖不該有碰撞
    assert m.longest_walk > 0 and m.average_walk > 0
    assert m.constraint_errors >= 0


def test_measure_does_not_mutate_spec():
    spec = _spec()
    before = [r.points for r in spec.rooms]
    measure(spec)
    assert [r.points for r in spec.rooms] == before


# ── 單層 Before → Optimize → After ────────────────────────────────────────
def test_run_floor_records_before_and_after():
    spec = _spec()
    res = run_floor("T01", "1F", spec, steps=1, out_dir=None, render=False)
    assert isinstance(res, FloorOptimization)
    assert res.case == "T01" and res.floor == "1F"
    assert isinstance(res.before, Metrics) and isinstance(res.after, Metrics)
    assert len(res.steps) >= 1                      # 至少嘗試過一步
    assert res.seconds > 0


def test_run_floor_never_regresses():
    """★ 最重要的一條:最佳化後任何指標都不得變差。"""
    spec = _spec()
    res = run_floor("T01", "1F", spec, steps=2, out_dir=None, render=False)
    assert not res.regressed, res.to_dict()
    assert res.after.score >= res.before.score - 1e-9
    assert res.after.collisions <= res.before.collisions
    assert res.after.constraint_errors <= res.before.constraint_errors


def test_run_floor_does_not_mutate_callers_spec():
    """★ harness 在 deepcopy 上最佳化,呼叫端的 spec 必須原封不動。"""
    spec = _spec()
    before = [r.points for r in spec.rooms]
    doors = [tuple(o.position for o in w.openings) for w in spec.walls]
    run_floor("T01", "1F", spec, steps=2, out_dir=None, render=False)
    assert [r.points for r in spec.rooms] == before
    assert [tuple(o.position for o in w.openings) for w in spec.walls] == doors


def test_optimized_result_is_still_a_valid_layout():
    """最佳化後的圖仍須合法(after 指標間接保證),原圖也未被破壞。"""
    spec = _spec()
    res = run_floor("T01", "1F", spec, steps=2, out_dir=None, render=False)
    assert res.after.collisions == 0
    assert validate_spec(spec) == []


# ── 整份報告 ──────────────────────────────────────────────────────────────
def test_run_produces_report_without_files():
    """out_dir=None 時不寫檔,只回報告(測試用)。"""
    rep = run(limit=1, steps=1, out_dir=None, render=False,
              cases=[c for c in CASES if c.cid == "S02"], verbose=False)
    assert isinstance(rep, OptimizationReport)
    assert len(rep.floors) == 1
    assert rep.regressed == 0
    assert rep.total_applied >= 0
    assert rep.seconds > 0


def test_report_aggregates_are_consistent():
    rep = run(limit=1, steps=1, out_dir=None, render=False,
              cases=[c for c in CASES if c.cid == "S04"], verbose=False)
    f = rep.floors[0]
    assert rep.improved == (1 if f.improved else 0)
    assert rep.regressed == (1 if f.regressed else 0)
    assert rep.total_applied == f.applied
    assert abs(rep.mean_delta - f.score_delta) < 1e-9


def test_report_follows_json_convention():
    rep = run(limit=1, steps=1, out_dir=None, render=False,
              cases=[c for c in CASES if c.cid == "S02"], verbose=False)
    d = rep.to_dict()
    assert json.loads(rep.to_json()) == d
    assert set(d) >= {"floors_tested", "steps_limit", "improved", "regressed",
                      "total_steps_applied", "mean_score_delta", "results"}
    row = d["results"][0]
    assert set(row) >= {"case", "floor", "before", "after", "applied",
                        "improved", "regressed", "score_delta", "steps"}
    assert set(row["before"]) >= {"score", "grade", "furniture_rate",
                                  "collisions", "longest_walk",
                                  "average_walk", "utilization",
                                  "constraint_errors"}
    assert "\\u" not in rep.to_json()
    assert "OptimizationReport" in rep.summary()


def test_run_writes_dxf_png_and_json(tmp_path):
    """★ 檔案輸出:前後各一份 DXF/PNG,加一份 JSON,且 DXF 讀得回來。"""
    import ezdxf
    rep = run(limit=1, steps=1, out_dir=tmp_path, render=True,
              cases=[c for c in CASES if c.cid == "S02"], verbose=False)
    assert (tmp_path / "optimization.json").is_file()
    saved = json.loads((tmp_path / "optimization.json").read_text(
        encoding="utf-8"))
    assert saved["floors_tested"] == 1

    f = rep.floors[0]
    assert set(f.files) == {"before_dxf", "before_png",
                            "after_dxf", "after_png"}
    for rel in f.files.values():
        path = tmp_path / rel
        assert path.is_file() and path.stat().st_size > 0
        if path.suffix == ".dxf":
            doc = ezdxf.readfile(path)               # 真的讀得回來
            assert len(list(doc.modelspace())) > 0
