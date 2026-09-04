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
import pytest
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


# ── 家具不得嵌進牆體(v0.7:房間多邊形走牆中心線,擺位前要縮到牆內面)──────────
def test_furniture_never_embedded_in_walls():
    """★ 家具不會穿牆:所有家具與牆體的重疊面積趨近 0。

    房間多邊形記的是牆**中心線**,家具貼齊房間邊界就會陷進半個牆厚(≈75~100mm),
    畫出來就是「家具穿牆」。_inner_room 先把可擺範圍縮到牆內面,消除這個誤差。
    """
    from shapely.geometry import LineString

    from src.design.collision.geometry import fixture_obstacles

    spec = furnish_spec(bsp_to_spec(THETA, FLAGS, SITE_W, SITE_D, BEDS))
    bodies = [LineString([w.start, w.end]).buffer(w.thickness / 2.0,
                                                 cap_style=2, join_style=2)
              for w in spec.walls]
    for o in fixture_obstacles(spec):
        overlap = sum(o.poly.intersection(b).area for b in bodies)
        assert overlap < 1000.0, f"{o.tag} 嵌進牆體 {overlap/1e6:.3f}㎡"


# ── 家具配得完整:四條產線都要(使用者 2026-09-04「全部尺寸都用這些規則排」)──
def _shallow(bw, bd, floors=3):
    from src.design.layout.shallow_house import generate_shallow_building
    return generate_shallow_building(bw, bd, floors=floors)


def _room_fixture_names(spec, room):
    """這間房裡有哪些家具(判準與 auto_furnish 同一把尺:流理台看檯面形心)。"""
    from shapely.geometry import Point, Polygon

    from src.drafting.fixtures import counter_footprint
    poly = Polygon(room.points).buffer(1.0)
    out = set()
    for f in spec.fixtures:
        if hasattr(f, "insert"):
            pt, nm = Point(*f.insert), getattr(f, "name", "")
        else:
            pt, nm = Polygon(counter_footprint(f)).centroid, "counter"
        if poly.contains(pt):
            out.add(nm)
    return out


@pytest.mark.parametrize("bw,bd", [(5000, 5000), (6000, 7000), (9000, 9000)])
def test_shallow_bedrooms_all_have_a_bed(bw, bd):
    """★★ 淺基地的臥室都要有床。

    ⚠️ 這條在 2026-09-04 之前是**紅的**(實測 4 間空臥室),而且完全看不出來 ——
    擺位器擺不下不報錯、動線修復器移走家具也不報錯。兩個根因:
    ①`_declutter_for_circulation` 挑受害者只看**大小**,床(2㎡)永遠比衣櫃
      (0.9㎡)先被丟,而 `collision/priority.py` 早就寫著床是「必要」;
    ②補回來的床補在**擋路的位置**,下一輪修復器又把它移掉(補了等於沒補)。
    """
    from src.design.semantic.room_semantic import canonical_room
    empty = []
    for lb, spec in _shallow(bw, bd):
        for r in spec.rooms:
            if canonical_room(r.kind) != "bedroom":
                continue
            if not ({"bed_single", "bed_double"} & _room_fixture_names(spec, r)):
                empty.append(f"{lb} {r.name}")
    assert not empty, f"沒有床的臥室:{empty}"


@pytest.mark.parametrize("bw,bd", [(5000, 5000), (6000, 7000), (8000, 7000)])
def test_shallow_living_rooms_all_have_a_sofa(bw, bd):
    """★★ 客廳都要有沙發(三人或二人)。

    書上〈空間最適尺寸〉Space 1 的客廳深度就是
    「電視櫃 + 走道 + 茶几 + 走道 + **沙發**」算出來的 —— 沙發是那條算式的主角。
    改之前淺基地 5 個尺寸的 1F 客廳**全部**只有電視櫃與茶几。
    """
    from src.design.semantic.room_semantic import canonical_room
    empty = []
    for lb, spec in _shallow(bw, bd):
        for r in spec.rooms:
            if canonical_room(r.kind) != "living":
                continue
            if not ({"sofa3", "sofa2"} & _room_fixture_names(spec, r)):
                empty.append(f"{lb} {r.name}")
    assert not empty, f"沒有沙發的客廳:{empty}"


def test_sofa2_follows_the_book_seat_length_rule():
    """★ 二人沙發 1500×850:書上「座面總長 = 60cm × 人數」+ 兩側扶手 150。"""
    from src.drafting.fixtures import FIXTURE_SIZES
    w, d = FIXTURE_SIZES["sofa2"]
    assert (w, d) == (1500, 850)
    assert w == 600 * 2 + 150 * 2                  # 座面 120cm + 扶手
    assert FIXTURE_SIZES["sofa3"][0] > w           # 退讓用:一定比三人座小


def test_declutter_drops_decoration_before_necessities():
    """★★ 動線修復器要**先丟裝飾**,不是先丟最大件。

    `collision/priority.py` 的模組說明本來就寫著「高優先的(床/馬桶/沙發)永遠
    保留,只犧牲裝飾」—— 但 `_declutter_for_circulation` 以前只挑最大件,
    於是床(1000×2000)永遠排在茶几/床頭櫃前面被丟掉。
    """
    from src.design.collision.priority import priority_of
    # 判準寫成它真正的意思:床/馬桶/沙發/流理台是必要,茶几/床頭櫃是裝飾。
    for must in ("bed_single", "bed_double", "toilet", "sofa3", "sofa2",
                 "counter"):
        for deco in ("coffee_table", "nightstand", "armchair", "tv_cabinet"):
            assert priority_of(must) > priority_of(deco), f"{must} vs {deco}"
