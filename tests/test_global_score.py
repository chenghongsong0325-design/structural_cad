"""Global Layout Scoring 測試(v0.7 Phase 6-7)。

整棟住宅層級的總評分:12 個子分數 → overall_score → grade(A+~D);
LayoutBenchmark 可排序多份 Layout、算平均、出 JSON/CSV。重點:

  * 子分數齊全且在 0~100;overall = 加權平均;grade 依門檻。
  * 唯讀:評分不改 spec;子分數會隨佈局變動(碰撞變差→分數降)。
  * Benchmark 能排序、平均、JSON、CSV。

⚠️ Layout Scoring 唯讀、不接進生成流程,不影響 DXF/PNG/Benchmark 生成。
"""
import copy
import csv
import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shapely.geometry import Polygon

from src.design.layout.global_score import (
    DEFAULT_LAYOUT_WEIGHTS,
    SCORE_ITEMS,
    LayoutBenchmark,
    LayoutScore,
    LayoutScoreEngine,
    grade_of,
)
from src.design.layout_generator import (
    HouseBrief,
    generate_floor_plan,
    generate_house_upper,
)
from src.drafting.fixtures import FixturePlacement, fixture_footprint

GRADES = {"A+", "A", "B", "C", "D"}


def _spec(bedrooms=3):
    return generate_floor_plan(
        HouseBrief(site_width=20000, site_depth=14000, bedrooms=bedrooms))


def _upper():
    return generate_house_upper(
        HouseBrief(site_width=26000, site_depth=16000, bedrooms=3, seed=1))


def _fake_score(name, overall, sub_val=80.0):
    return LayoutScore(name=name, overall_score=overall,
                       grade=grade_of(overall),
                       sub_scores={k: sub_val for k in SCORE_ITEMS})


# ── 引擎:結構與範圍 ──────────────────────────────────────────────────────
def test_score_returns_layoutscore_in_range():
    sc = LayoutScoreEngine().score(_spec(), name="h")
    assert isinstance(sc, LayoutScore)
    assert 0.0 <= sc.overall_score <= 100.0
    assert sc.grade in GRADES and sc.name == "h"


def test_all_twelve_sub_scores_present_and_in_range():
    """★ 12 個子分數齊全、都在 0~100。"""
    sc = LayoutScoreEngine().score(_spec())
    assert set(sc.sub_scores) == set(SCORE_ITEMS)
    assert len(SCORE_ITEMS) == 12
    for k, v in sc.sub_scores.items():
        assert 0.0 <= v <= 100.0, f"{k}={v}"


def test_score_items_match_default_weight_keys():
    assert set(SCORE_ITEMS) == set(DEFAULT_LAYOUT_WEIGHTS)


def test_grade_thresholds():
    """★ grade 門檻:A+≥90 / A≥80 / B≥70 / C≥60 / D。"""
    assert grade_of(95) == "A+"
    assert grade_of(90) == "A+"
    assert grade_of(85) == "A"
    assert grade_of(75) == "B"
    assert grade_of(65) == "C"
    assert grade_of(59.9) == "D"


def test_overall_is_weighted_average_of_sub_scores():
    """★ overall = Σ(子分數 × 權重) / Σ(權重)。"""
    eng = LayoutScoreEngine()
    sc = eng.score(_spec())
    w = DEFAULT_LAYOUT_WEIGHTS
    wsum = sum(w[k] for k in SCORE_ITEMS)
    expect = sum(sc.sub_scores[k] * w[k] for k in SCORE_ITEMS) / wsum
    assert abs(sc.overall_score - expect) < 1e-6


def test_custom_weights_change_overall():
    """★ 換權重會改變 overall(否則權重是裝飾)。"""
    spec = _spec()
    base = LayoutScoreEngine().score(spec)
    tilt = LayoutScoreEngine({**DEFAULT_LAYOUT_WEIGHTS,
                              "symmetry": 100.0}).score(spec)
    assert abs(base.overall_score - tilt.overall_score) > 1e-6


def test_scoring_is_deterministic():
    spec = _spec()
    a = LayoutScoreEngine().score(spec)
    b = LayoutScoreEngine().score(spec)
    assert a.overall_score == b.overall_score and a.sub_scores == b.sub_scores


def test_scoring_does_not_mutate_spec():
    """★ 唯讀:評分不得改動 spec。"""
    spec = _spec()
    before = [(getattr(f, "name", "counter"), getattr(f, "insert", None))
              for f in spec.fixtures]
    rooms_before = [r.points for r in spec.rooms]
    LayoutScoreEngine().score(spec)
    after = [(getattr(f, "name", "counter"), getattr(f, "insert", None))
             for f in spec.fixtures]
    assert before == after and rooms_before == [r.points for r in spec.rooms]


# ── 子分數會回應佈局變化 ──────────────────────────────────────────────────
def test_collision_sub_score_drops_on_overlap():
    """★ 注入一件與既有家具重疊的家具 → collision 子分數下降。"""
    spec = _spec()
    base = LayoutScoreEngine().sub_scores(spec)["collision"]
    spec2 = copy.deepcopy(spec)
    dup = next(f for f in spec2.fixtures if isinstance(f, FixturePlacement))
    spec2.fixtures.append(FixturePlacement(dup.name, dup.insert, dup.rotation))
    after = LayoutScoreEngine().sub_scores(spec2)["collision"]
    assert after < base


def test_furniture_sub_score_drops_when_a_room_is_emptied():
    """★ 把某個需要家具的房間清空 → furniture 佈置涵蓋率下降。"""
    spec = _spec()
    base = LayoutScoreEngine().sub_scores(spec)["furniture"]
    spec2 = copy.deepcopy(spec)
    bedroom = next(r for r in spec2.rooms if r.kind == "bedroom")
    poly = Polygon(bedroom.points)
    spec2.fixtures = [
        f for f in spec2.fixtures
        if not (isinstance(f, FixturePlacement)
                and poly.contains(Polygon(fixture_footprint(f)).centroid))]
    after = LayoutScoreEngine().sub_scores(spec2)["furniture"]
    assert after < base or base < 100.0


def test_lighting_and_window_usage_in_range():
    sub = LayoutScoreEngine().sub_scores(_spec())
    assert 0.0 <= sub["natural_lighting"] <= 100.0
    assert 0.0 <= sub["window_usage"] <= 100.0


# ── LayoutScore API / 序列化 ──────────────────────────────────────────────
def test_layoutscore_follows_json_convention():
    sc = LayoutScoreEngine().score(_spec(), name="h")
    assert json.loads(sc.to_json()) == sc.to_dict()
    assert "\\u" not in sc.to_json()
    d = sc.to_dict()
    assert set(d) >= {"name", "overall_score", "grade", "sub_scores"}
    assert set(d["sub_scores"]) == set(SCORE_ITEMS)
    assert sc.grade in sc.summary()


# ── Benchmark:排序 / 平均 ─────────────────────────────────────────────────
def test_benchmark_from_specs_names_entries():
    b = LayoutBenchmark.from_specs({"a": _spec(), "b": _upper()})
    assert {e.name for e in b.entries} == {"a", "b"}


def test_benchmark_from_list_auto_names():
    b = LayoutBenchmark.from_specs([_spec(), _spec(bedrooms=2)])
    assert [e.name for e in b.entries] == ["layout_0", "layout_1"]


def test_benchmark_ranked_is_descending():
    """★ ranked() 依 overall 由高到低。"""
    b = LayoutBenchmark()
    for n, s in (("low", 60.0), ("high", 92.0), ("mid", 78.0)):
        b.add(_fake_score(n, s))
    ranked = b.ranked()
    assert [e.name for e in ranked] == ["high", "mid", "low"]
    assert b.best().name == "high"


def test_benchmark_average_and_sub_averages():
    """★ average() = overall 平均;average_sub_scores 每項平均。"""
    b = LayoutBenchmark()
    b.add(_fake_score("x", 80.0, sub_val=70.0))
    b.add(_fake_score("y", 90.0, sub_val=90.0))
    assert abs(b.average() - 85.0) < 1e-9
    avg = b.average_sub_scores()
    assert set(avg) == set(SCORE_ITEMS)
    assert abs(avg["collision"] - 80.0) < 1e-9


def test_benchmark_sorts_real_layouts():
    b = LayoutBenchmark.from_specs({"house": _spec(), "upper": _upper()})
    ranked = b.ranked()
    assert ranked[0].overall_score >= ranked[-1].overall_score


# ── Benchmark:JSON / CSV ──────────────────────────────────────────────────
def test_benchmark_to_json_round_trips():
    b = LayoutBenchmark.from_specs({"a": _spec(), "b": _upper()})
    d = b.to_dict()
    assert json.loads(b.to_json()) == d
    assert d["count"] == 2 and "average" in d
    assert len(d["ranking"]) == 2
    assert d["ranking"][0]["overall_score"] >= d["ranking"][1]["overall_score"]


def test_benchmark_to_csv_has_header_and_rows():
    """★ CSV:表頭 + 每份一列,欄位含 rank/name/overall/grade + 12 子分數。"""
    b = LayoutBenchmark()
    for n, s in (("a", 88.0), ("b", 72.0)):
        b.add(_fake_score(n, s))
    rows = list(csv.reader(io.StringIO(b.to_csv())))
    assert rows[0] == ["rank", "name", "overall_score", "grade", *SCORE_ITEMS]
    assert len(rows) == 3                            # 表頭 + 2 份
    assert rows[1][0] == "1" and rows[1][1] == "a"   # 依排名,a(88)在前
    assert rows[2][0] == "2" and rows[2][1] == "b"


def test_benchmark_csv_columns_count():
    b = LayoutBenchmark()
    b.add(_fake_score("only", 80.0))
    rows = list(csv.reader(io.StringIO(b.to_csv())))
    assert len(rows[0]) == 4 + len(SCORE_ITEMS)      # rank/name/overall/grade + 12


def test_empty_benchmark_is_graceful():
    """★ 空 benchmark:average 0、best None、CSV 只有表頭、to_dict count 0。"""
    b = LayoutBenchmark()
    assert b.average() == 0.0 and b.best() is None
    rows = list(csv.reader(io.StringIO(b.to_csv())))
    assert len(rows) == 1                            # 只有表頭
    assert b.to_dict()["count"] == 0


def test_benchmark_add_appends():
    b = LayoutBenchmark()
    b.add(_fake_score("a", 80.0))
    b.add(_fake_score("b", 70.0))
    assert len(b.entries) == 2


def test_benchmark_summary_lists_ranking():
    b = LayoutBenchmark()
    b.add(_fake_score("top", 91.0))
    b.add(_fake_score("bot", 61.0))
    s = b.summary()
    assert "top" in s and "bot" in s and "平均" in s
