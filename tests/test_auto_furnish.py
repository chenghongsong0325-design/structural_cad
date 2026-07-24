"""Auto Furnish 測試(v0.7 Phase 7.1b)。

給「只有房間與牆」的 BSP 平面自動配家具,讓 Phase 6 評得出東西。重點:

  * 依房間用途配家具:臥室有床、客廳有沙發、廚房有流理台+冰箱、浴室有馬桶。
  * 位置由 Phase 6 的 FurniturePlacementOptimizer 決定 → **擺完必定 collision-valid**。
  * 配完家具後 furniture / room_semantic 子分數由 0/低 變成有意義,總分上升。
  * 配完仍畫得出 DXF;任意隨機 θ 都不崩。

⚠️ 擺不下的家具直接略過(不硬塞、不產生非法佈局)。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
from shapely.geometry import Polygon

from src.design.collision.furniture_engine import FurnitureCollisionEngine
from src.design.layout.auto_furnish import FURNITURE_PROGRAM, furnish_spec
from src.design.layout.bsp_layout import bsp_to_spec, random_theta
from src.design.layout.global_score import score_report
from src.drafting.apartment_plan import draw_floor_plan
from src.drafting.fixtures import Counter, FixturePlacement
from src.web.render import _new_doc

THETA = np.array([0.62, 0.50, 0.33, 0.66, 0.0, 0.72, 0.55, 0.42, 0.20, 0.30])
FLAGS = np.array([1.0, 1.0])
SITE_W, SITE_D, BEDS = 20000.0, 14000.0, 3


def _bare():
    """只有房間/牆/門/窗、沒有家具的 BSP 平面(7.1a 產物)。"""
    return bsp_to_spec(THETA, FLAGS, SITE_W, SITE_D, BEDS)


def _furnished():
    return furnish_spec(_bare())


def _names(spec):
    return {f.name for f in spec.fixtures if isinstance(f, FixturePlacement)}


def _in_room(spec, kind):
    """某 kind 房間裡的家具名稱集合。"""
    out = set()
    for room in [r for r in spec.rooms if r.kind == kind]:
        poly = Polygon(room.points)
        for f in spec.fixtures:
            if isinstance(f, FixturePlacement):
                from src.drafting.fixtures import fixture_footprint
                if poly.contains(Polygon(fixture_footprint(f)).centroid):
                    out.add(f.name)
    return out


# ── 有配到家具 ──────────────────────────────────────────────────────────────
def test_bare_spec_has_no_furniture():
    assert _bare().fixtures == []


def test_furnish_adds_furniture():
    spec = _furnished()
    assert len(spec.fixtures) > 5


def test_furnish_returns_same_spec_object():
    spec = _bare()
    assert furnish_spec(spec) is spec


# ── 各房該有的東西 ──────────────────────────────────────────────────────────
def test_bedrooms_get_a_bed():
    """★ 臥室要有床(雙人放不下會退成單人)。"""
    beds = _in_room(_furnished(), "bedroom")
    assert beds & {"bed_double", "bed_single"}


def test_living_gets_sofa():
    assert "sofa3" in _in_room(_furnished(), "living")


def test_bathroom_gets_toilet():
    assert "toilet" in _in_room(_furnished(), "bathroom")


def test_kitchen_gets_counter_and_fridge():
    """★ 廚房要有流理台(Counter)與冰箱。"""
    spec = _furnished()
    assert any(isinstance(f, Counter) for f in spec.fixtures)
    assert "fridge" in _in_room(spec, "kitchen")


def test_corridor_gets_no_furniture():
    """走道不配家具(它是動線)。"""
    assert _in_room(_furnished(), "corridor") == set()


def test_program_covers_main_room_kinds():
    assert {"bedroom", "living", "kitchen", "bathroom"} <= set(FURNITURE_PROGRAM)


# ── 合法性:擺完不撞 ────────────────────────────────────────────────────────
def test_all_placed_furniture_is_collision_valid():
    """★ 位置由 Phase 6 optimizer 挑 → 擺完每件都通過碰撞查詢。"""
    spec = _furnished()
    checks = FurnitureCollisionEngine(spec).check_existing()
    assert checks and all(res.valid for _, res in checks)


def test_furnished_spec_still_draws():
    doc, layers = _new_doc()
    draw_floor_plan(doc.modelspace(), _furnished(), layers)
    assert len(list(doc.modelspace())) > 0


# ── 分數:配完家具才評得出東西 ──────────────────────────────────────────────
def test_furnishing_raises_score_and_fills_sub_scores():
    """★ 配家具後 furniture / room_semantic 由 0/低 變高,總分上升。"""
    bare = _bare()
    before = score_report(bare)
    after = score_report(furnish_spec(bare))
    assert before["sub_scores"]["furniture"] == 0.0
    assert after["sub_scores"]["furniture"] > before["sub_scores"]["furniture"]
    assert after["sub_scores"]["room_semantic"] > before["sub_scores"]["room_semantic"]
    assert after["overall_score"] > before["overall_score"]


def test_furnished_scores_vary_across_theta():
    """★ 不同刀位配完家具後分數會拉開(這是 7.1c 搜尋有意義的前提)。"""
    rng = np.random.default_rng(0)
    scores = []
    for _ in range(6):
        th, fl = random_theta(rng)
        spec = furnish_spec(bsp_to_spec(th, fl, SITE_W, SITE_D, BEDS))
        scores.append(score_report(spec)["overall_score"])
    assert max(scores) - min(scores) > 1.0            # 真的有高低差


# ── 穩健性 ──────────────────────────────────────────────────────────────────
def test_random_theta_furnishes_without_error():
    rng = np.random.default_rng(3)
    for _ in range(5):
        th, fl = random_theta(rng)
        spec = furnish_spec(bsp_to_spec(th, fl, SITE_W, SITE_D, BEDS))
        assert 0.0 <= score_report(spec)["overall_score"] <= 100.0
