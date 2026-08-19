"""圖面文字不要黏在一起(`src/drafting/label_space.py`)。

使用者 2026-08-19 截圖抓到三種:

    「UP 18樓梯間」        梯級標示黏著室名
    「12cm 磚牆」壓在      浴室的面積數字上
    「D2」壓在             室名上

⚠️ 判準是**留白**不是「不重疊」:實測那張圖的「UP 18」與「樓梯間」重疊面積是
   0 —— 兩段字只是貼著,讀起來照樣連成一串。所以測試量的是**最短距離**。
"""
import random
import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))   # 借用 test_graph_layout 的關係圖

import pytest
from shapely.geometry import box

from src.design.building_generator import BuildingBrief, generate_building_auto
from src.design.layout.graph_layout import realize_graph_building
from src.design.layout_generator import HouseBrief
from src.drafting.apartment_plan import draw_floor_plan
from src.drafting.label_space import (
    LABEL_GAP,
    LabelSpace,
    is_flight_label,
    is_movable_label,
    text_box,
    text_width,
)
from src.standards.loader import apply_standard, load_standard, new_document
from test_graph_layout import GRAPH


# ── 量字的工具本身 ─────────────────────────────────────────────────────────
def test_chinese_is_wider_than_ascii():
    """全形字比半形寬 —— 用等寬估會低估中文,那正是疊字沒被發現的原因。"""
    assert text_width("樓梯間", 250) > text_width("abc", 250)


def test_box_follows_the_alignment():
    """置中對齊的字,框要往左右各長一半;靠左對齊則整段往右長。"""
    from ezdxf.enums import TextEntityAlignment
    mid = text_box("ABC", 100, 0, 0, TextEntityAlignment.MIDDLE_CENTER)
    left = text_box("ABC", 100, 0, 0, None)
    assert mid[0] < 0 < mid[2]
    assert left[0] == pytest.approx(0)


def test_space_requires_a_gap_not_just_no_overlap():
    """★ 這是這支模組的重點:碰不到 ≠ 讀得開。"""
    space = LabelSpace(gap=100)
    space.occupy((0, 0, 100, 100))
    assert not space.is_clear((110, 0, 210, 100)), "只隔 10 就該算太擠"
    assert space.is_clear((250, 0, 350, 100))


def test_take_returns_the_first_clear_candidate_and_books_it():
    space = LabelSpace(gap=0)
    space.occupy((0, 0, 100, 100))
    got = space.take([((50, 50, 150, 150), "髒"), ((200, 0, 300, 100), "乾淨")])
    assert got[1] == "乾淨"
    assert not space.is_clear((200, 0, 300, 100)), "挑走了就要登記起來"


def test_take_returns_none_when_everything_collides():
    """挑不到要回 None,呼叫端才知道要退回原位(少一個編號比疊字嚴重)。"""
    space = LabelSpace(gap=0)
    space.occupy((0, 0, 100, 100))
    assert space.take([((10, 10, 20, 20), "x")]) is None


# ── 誰可以搬家 ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize("text,movable", [
    ("UP 18", True), ("UP", True), ("DN", True), ("拉門", True),
    ("樓梯間", False), ("14.6㎡", False), ("D1", False), ("B", False),
    ("UP甲", False),
])
def test_only_position_insensitive_labels_may_move(text, movable):
    """★ 室名/面積/軸網編號的**位置本身帶資訊**,不可以為了排版亂搬。

    梯級標示只要落在梯段上、拉門註記只要在門洞旁,就讀得懂 —— 那才可以動。"""
    assert is_movable_label(text) is movable


def test_flight_label_matcher_agrees_with_the_stair_module():
    """★ `is_flight_label` 認的形狀必須跟 `stair.flight_label` 真正產出的一致。

    兩邊是同一份約定寫在兩個檔案裡,分開走鐘的話梯級標示就不會再閃避。"""
    from src.drafting.stair import flight_label
    for label, steps in (("上", 18), ("上", 0), ("下", 16), ("DN", 0)):
        assert is_flight_label(flight_label(label, steps)), flight_label(label, steps)


# ── 整張圖:實際量出來不能有黏在一起的字 ────────────────────────────────────
def _crowded_pairs(spec):
    doc = new_document()
    layers = apply_standard(doc, load_standard())
    draw_floor_plan(doc.modelspace(),
                    replace(spec, sheet=False, title_block=None), layers)
    items = []
    for e in doc.modelspace().query("TEXT"):
        t = str(e.dxf.text)
        if not t.strip():
            continue
        try:
            align = e.get_align_enum()
        except Exception:                            # noqa: BLE001
            align = None
        p = e.dxf.align_point if (e.dxf.halign or e.dxf.valign) else e.dxf.insert
        items.append((box(*text_box(t, float(e.dxf.height),
                                    float(p.x), float(p.y), align)), t))
    bad = []
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            if items[i][0].distance(items[j][0]) < LABEL_GAP:
                bad.append(f"{items[i][1]!r}×{items[j][1]!r}")
    return bad


@pytest.mark.parametrize("w,d,floors", [(19, 13, 3), (7, 12, 3), (15, 12, 2)])
def test_rule_pipeline_has_no_crowded_labels(w, d, floors):
    brief = BuildingBrief(
        typical=HouseBrief(site_width=w * 1000, site_depth=d * 1000,
                           bedrooms=3, setback=0, seed=0),
        floors=floors, differentiated=True)
    for f in generate_building_auto(brief).floors:
        bad = _crowded_pairs(f.spec)
        assert not bad, f"{w}x{d} {f.label} 這些字黏在一起:{bad}"


@pytest.mark.parametrize("w,d", [(7000, 12000), (10000, 11000), (9000, 14000)])
def test_ai_pipeline_has_no_crowded_labels(w, d):
    """★ 使用者截到的那張就是 AI 產線出的(改之前這裡有 26 對疊字)。"""
    for label, spec, _, _ in realize_graph_building(
            GRAPH, w, d, setback=0, rng=random.Random(7)):
        bad = _crowded_pairs(spec)
        assert not bad, f"{w}x{d} {label} 這些字黏在一起:{bad}"
