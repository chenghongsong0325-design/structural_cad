"""Layout Engine 公開 API 契約測試(v0.7 Phase 5-8)。

這份測試是給**文件**用的護欄:docs/LAYOUT_ENGINE.md 上寫的每一個進入點、
每一條規則、每一個評分面向,都在這裡被釘住。任何人改名或刪掉,文件失準之前
測試會先紅。

不重複各層的行為測試(那些在各自的 test_*.py),只驗:
  * 進入點存在且可呼叫
  * Report 型別遵守序列化契約
  * 唯讀層真的唯讀
  * 依賴方向沒有循環
"""
import importlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.design.layout_generator import HouseBrief, generate_floor_plan
from src.design.report import JsonReport


def _spec():
    return generate_floor_plan(
        HouseBrief(site_width=16000, site_depth=14000, bedrooms=3))


# 文件上宣告的公開進入點:模組 → 必須存在的名稱
PUBLIC_API = {
    "src.design.report": ["JsonReport"],
    "src.design.connectivity": [
        "analyze_connectivity", "build_graphs", "reachable_from",
        "ConnectivityGraphs", "ConnectivityReport", "DoorNode",
        "room_polys", "wall_cover", "door_points", "shared_edge"],
    "src.design.layout_validation": [
        "validate_layout", "LayoutValidator", "LayoutReport", "LayoutIssue"],
    "src.design.corridor": [
        "analyze_corridors", "CorridorReport", "CorridorInfo",
        "MIN_CORRIDOR_WIDTH", "MIN_OPENING_WIDTH"],
    "src.design.scoring": [
        "score_layout", "LayoutScore", "ScoreItem", "ScoreWeights",
        "PUBLIC_KINDS", "SEMI_PRIVATE_KINDS"],
    "src.design.constraints": [
        "check_constraints", "ConstraintReport", "ConstraintViolation",
        "ConstraintRule", "ConstraintContext", "RULES"],
    "src.design.optimizer": ["optimize_step", "OptimizeStep", "candidates"],
    "src.design.optimization_benchmark": [
        "run", "run_floor", "measure", "Metrics",
        "FloorOptimization", "OptimizationReport"],
    "src.design.collision": [
        "resolve_collisions", "find_collisions", "collision_problems",
        "column_contacts", "CollisionEngine", "ResolveReport", "Obstacle"],
}


def test_public_api_exists():
    """★ 文件宣告的每個進入點都必須真的存在。"""
    for mod_name, names in PUBLIC_API.items():
        mod = importlib.import_module(mod_name)
        for name in names:
            assert hasattr(mod, name), f"{mod_name}.{name} 不存在(文件失準)"


def test_documented_constraint_rules_are_registered():
    """★ 文件列出的五條規則都在登錄表裡。"""
    from src.design.constraints import RULES
    documented = {
        "bedroom_not_facing_kitchen", "bathroom_not_facing_dining",
        "entrance_not_facing_toilet", "bedroom_avoids_public_adjacency",
        "kitchen_near_dining"}
    assert {r.rule_id for r in RULES} == documented


def test_documented_score_metrics_and_weights():
    """★ 文件列出的七個面向與預設權重都要對得上。"""
    from src.design.scoring import ScoreWeights, score_layout
    documented = {"connectivity": 2.0, "circulation": 1.5, "privacy": 1.0,
                  "lighting": 1.5, "utilization": 1.0, "furniture": 1.0,
                  "collision": 2.0}
    assert ScoreWeights().as_map() == documented
    score = score_layout(_spec())
    assert [i.name for i in score.items] == list(documented)


def test_score_total_matches_documented_formula():
    """★ 文件寫的公式:total = Σ(subscore × weight) / Σ(weight)。"""
    from src.design.scoring import score_layout
    sc = score_layout(_spec())
    wsum = sum(i.weight for i in sc.items)
    assert abs(sc.total - sum(i.score * i.weight for i in sc.items) / wsum) < 1e-9


# ── Report 契約 ───────────────────────────────────────────────────────────
def _all_reports(spec):
    from src.design.collision import CollisionEngine
    from src.design.connectivity import analyze_connectivity, build_graphs
    from src.design.constraints import check_constraints
    from src.design.corridor import analyze_corridors
    from src.design.layout_validation import validate_layout
    from src.design.optimizer import optimize_step
    from src.design.scoring import score_layout
    import copy
    return [
        ("LayoutReport", validate_layout(spec)),
        ("ConnectivityReport", analyze_connectivity(spec)),
        ("ConnectivityGraphs", build_graphs(spec)),
        ("CorridorReport", analyze_corridors(spec)),
        ("LayoutScore", score_layout(spec)),
        ("ConstraintReport", check_constraints(spec)),
        ("ResolveReport", CollisionEngine(spec).resolve()),
        ("OptimizeStep", optimize_step(copy.deepcopy(spec))),
    ]


def test_every_report_follows_serialisation_contract():
    """★ 每個 Report 都是 JsonReport,且真的 json round-trip 得回來。"""
    for name, rep in _all_reports(_spec()):
        assert isinstance(rep, JsonReport), f"{name} 未繼承 JsonReport"
        d = rep.to_dict()
        assert isinstance(d, dict), f"{name}.to_dict() 不是 dict"
        assert json.loads(rep.to_json()) == d, f"{name} round-trip 不一致"
        assert "\\u" not in rep.to_json(), f"{name} 中文被轉義了"


# ── 唯讀保證 ──────────────────────────────────────────────────────────────
def test_read_only_layers_never_mutate_spec():
    """★ 分析層(①~⑤)一律唯讀——這是整個 v0.7 Regression = 0 的基礎。"""
    import copy
    from src.design.connectivity import analyze_connectivity
    from src.design.constraints import check_constraints
    from src.design.corridor import analyze_corridors
    from src.design.layout_validation import validate_layout
    from src.design.scoring import score_layout

    def snap(s):
        return copy.deepcopy((
            [(r.points, r.kind, r.name) for r in s.rooms],
            [(w.start, w.end,
              [(o.position, o.width, o.kind) for o in w.openings])
             for w in s.walls],
            [(d.wall_index, d.opening_index) for d in s.doors],
            [(getattr(f, "name", "counter"), getattr(f, "insert", None))
             for f in s.fixtures],
        ))

    for fn in (validate_layout, analyze_connectivity, analyze_corridors,
               score_layout, check_constraints):
        spec = _spec()
        before = snap(spec)
        fn(spec)
        assert snap(spec) == before, f"{fn.__name__} 動到了 spec"


def test_optimizer_writes_layout_but_never_fixtures():
    """★ 可寫範圍的界線:optimizer 只動 rooms/walls/doors,不碰家具。"""
    from src.design.optimizer import optimize_step
    spec = _spec()
    before = [(getattr(f, "name", "counter"), getattr(f, "insert", None),
               getattr(f, "start", None)) for f in spec.fixtures]
    optimize_step(spec)
    after = [(getattr(f, "name", "counter"), getattr(f, "insert", None),
              getattr(f, "start", None)) for f in spec.fixtures]
    assert before == after


# ── 架構 ──────────────────────────────────────────────────────────────────
def test_dependency_direction_has_no_cycle():
    """★ 文件宣告的單向 DAG:底層不得反過來 import 上層。"""
    import ast
    root = Path(__file__).resolve().parents[1] / "src" / "design"
    # 模組 → 不允許出現在它 import 清單裡的上層模組
    forbidden = {
        "report": {"connectivity", "layout_validation", "corridor", "scoring",
                   "constraints", "optimizer", "optimization_benchmark"},
        "connectivity": {"layout_validation", "corridor", "scoring",
                         "constraints", "optimizer", "optimization_benchmark"},
        "layout_validation": {"scoring", "constraints", "optimizer",
                              "optimization_benchmark"},
        "corridor": {"scoring", "constraints", "optimizer",
                     "optimization_benchmark"},
        "scoring": {"optimizer", "optimization_benchmark"},
        "constraints": {"optimizer", "optimization_benchmark"},
        "optimizer": {"optimization_benchmark"},
    }
    for mod, banned in forbidden.items():
        tree = ast.parse((root / f"{mod}.py").read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[-1])
            elif isinstance(node, ast.Import):
                for a in node.names:
                    imported.add(a.name.split(".")[-1])
        clash = imported & banned
        assert not clash, f"{mod} 反向依賴上層模組:{clash}"
