"""與參考圖對照(Phase 12)的測試 —— 重點是**這份報表不能說謊**。

對照報表最容易犯的錯不是漏列項目,而是「明明沒做卻報成做到了」。第一版就中了:
門窗編號的探針去全圖找 `D1`/`W1`,結果撈到**門窗表**裡的編號,回報「圖上有 4 個」
——圖上其實一個都沒有。所以這組測試主要在守:

  1. 探針只認**畫在地界線範圍內**的字(表格在地界線右側,不算圖面上的標註)。
  2. 已知缺口就要報缺(補起來的那天,測試會提醒回來更新報表)。
  3. Report 可序列化(專案慣例)、涵蓋率算得對。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from src.design.gap_analysis import (
    HAVE,
    MISSING,
    PARTIAL,
    GapItem,
    GapReport,
    analyze_gap,
    to_markdown,
)


@pytest.fixture(scope="module")
def report():
    return analyze_gap()


def _item(rep, code):
    return next(i for i in rep.items if i.code == code)


# ── 報表不能說謊 ────────────────────────────────────────────────────────────
def test_opening_tags_not_fooled_by_the_schedule_table(report):
    """★★ 門窗編號:表格裡有 D1/W1,但圖上沒標 → 必須報 partial,不能報 have。

    這正是第一版的錯誤。受測的圖**開著門窗表**,探針要能分清楚
    「畫在地界線範圍內的標註」與「表格裡的欄位」。"""
    it = _item(report, "opening_tags")
    assert it.status == PARTIAL, it.ours
    assert "圖上一個都沒標" in it.ours


@pytest.mark.parametrize("code", [
    "wall_thickness_note",      # 牆厚只在資料裡,圖上沒寫
    "spot_level",               # 沒有地坪標高
    "balcony_area",             # 陽台面積沒進面積計算表
    "drainage",                 # 沒有基地排水
    "ceiling",                  # 沒有天花板圖
])
def test_known_gaps_are_reported_missing(report, code):
    """★ 已知還沒做的,報表就要說沒做(做完了這條會紅,提醒回來更新)。"""
    assert _item(report, code).status == MISSING


@pytest.mark.parametrize("code", [
    "sheet_frame", "dim_chains", "site_line", "building_line", "walls",
    "doors", "windows", "stairs", "balcony", "room_name", "furniture",
    "area_table", "opening_table",
])
def test_things_we_really_do_have(report, code):
    """★ 這些是真的畫得出來的(探針在圖裡找得到實體/文字才算)。"""
    assert _item(report, code).status == HAVE, _item(report, code).ours


def test_every_item_is_classified(report):
    assert report.items
    for i in report.items:
        assert i.status in (HAVE, PARTIAL, MISSING)
        assert i.name and i.seen_at and i.ours
        if i.status != HAVE:
            assert i.note, f"{i.code} 不是 have 就要寫清楚差在哪"


def test_codes_are_unique(report):
    codes = [i.code for i in report.items]
    assert len(codes) == len(set(codes))


# ── 報表本身 ────────────────────────────────────────────────────────────────
def test_coverage_counts_partial_as_half():
    rep = GapReport([
        GapItem("a", "甲", "x", HAVE, "有"),
        GapItem("b", "乙", "x", PARTIAL, "半套", "差一半"),
        GapItem("c", "丙", "x", MISSING, "沒有", "要補"),
        GapItem("d", "丁", "x", MISSING, "沒有", "要補"),
    ])
    assert rep.coverage == pytest.approx((1 + 0.5) / 4)


def test_report_serialisable(report):
    """★ Report 要能 to_dict/to_json(專案慣例)。"""
    d = report.to_dict()
    assert set(d) == {"reference", "n_items", "n_have", "n_partial",
                      "n_missing", "coverage", "items"}
    assert d["n_have"] + d["n_partial"] + d["n_missing"] == d["n_items"]
    assert json.loads(report.to_json())["n_items"] == d["n_items"]
    assert isinstance(GapReport().summary(), str)


def test_markdown_lists_every_item(report):
    md = to_markdown(report)
    assert md.startswith("# 與參考圖對照")
    for i in report.items:
        assert i.name in md


def test_reference_is_the_taiwanese_drawing(report):
    """★ 基準是台灣的檢定圖(使用者定調:不要拿簡體字的圖當基準)。"""
    assert "丙級" in report.reference and "技能檢定" in report.reference
