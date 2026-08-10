"""Room Circulation 測試(v0.7 Phase 7.2a)。

驗證「房間內部走得通」的檢查,以及它接進 Global Score 的 circulation 子分數。重點:

  * 真實產生器的合格房都走得通(尺寸合格房 circulation ok)。
  * 窄餐廳修正回歸守門:S11 的餐桌偏移後,餐廳不再被切成兩塊(門走得通)。
  * 家具把路擋死的佈局會被抓出來(blocked 非空),且 circulation 子分數 < 100。
  * skip_kinds 會濾掉車位/陽台/天井等非居住空間。

⚠️ 唯讀:分析器不改 spec。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
from shapely.geometry import Polygon

from src.design.benchmark import CASES
from src.design.layout.auto_furnish import furnish_spec
from src.design.layout.bsp_layout import bsp_to_spec, random_theta
from src.design.layout.global_score import SCORE_ITEMS, score_report
from src.design.layout.room_circulation import (
    CirculationReport,
    RoomCirculation,
    analyze_room_circulation,
    circulation_ok,
)
from src.design.layout_generator import generate_floor_plan


def _case(cid):
    c = next(x for x in CASES if x.cid == cid)
    _, brief = c.build()
    return generate_floor_plan(brief)


def _min_side(room):
    x0, y0, x1, y1 = Polygon(room.points).bounds
    return min(x1 - x0, y1 - y0)


# ── 報告結構 ────────────────────────────────────────────────────────────────
def test_report_shape():
    rep = analyze_room_circulation(_case("S04"))
    assert isinstance(rep, CirculationReport)
    assert rep.rooms and all(isinstance(r, RoomCirculation) for r in rep.rooms)
    d = rep.to_dict()
    assert {"ok", "count", "blocked", "rooms"} <= set(d)
    assert {"name", "kind", "ok", "components", "isolated"} <= set(rep.rooms[0].to_dict())


def test_circulation_ok_matches_report():
    spec = _case("S04")
    assert circulation_ok(spec) == analyze_room_circulation(spec).ok


# ── 合格佈局走得通 ──────────────────────────────────────────────────────────
def test_good_layout_adequate_rooms_all_ok():
    """★ 經典戶型:所有尺寸合格的房間內部都走得通。"""
    spec = _case("S04")
    rep = analyze_room_circulation(spec)
    by_name = {r.name: r for r in rep.rooms}
    bad = []
    for room in spec.rooms:
        r = by_name.get(room.name)
        if r is not None and _min_side(room) >= 1500 and not r.ok:
            bad.append((room.name, r.reason))
    assert bad == [], bad


def test_narrow_dining_not_blocked():
    """★ 窄餐廳修正回歸守門:餐桌偏移後,餐廳動線不再被切斷。

    ⚠️ 原本只釘 S11 一案。2026-08-10 起外牆要替柱留位置(STRUCT_MARGIN),建築
       每邊縮 27.5cm,S11(18×14 三房)的餐廳因此併進客廳、不再是獨立房間 ——
       **釘單一案子的守門會這樣悄悄失效**(next() 直接 StopIteration)。改成掃過
       所有仍有餐廳的案子,最窄的那間(S09,短邊 3.2m)也要走得通,並要求至少
       找得到一間,免得哪天全被併掉還一路綠燈。
    """
    seen = 0
    for cid in ("S07", "S08", "S09"):
        rep = analyze_room_circulation(_case(cid))
        for room in rep.rooms:
            if room.kind != "dining":
                continue
            seen += 1
            assert room.ok, f"{cid} 餐廳動線不通:{room.reason}"
    assert seen, "沒有任何案子有獨立餐廳 —— 這條守門形同虛設,要換案子"


# ── 擋路會被抓出來 ──────────────────────────────────────────────────────────
def test_blocked_layout_is_detected_and_scored():
    """★ 家具把路擋死的佈局:blocked 非空,且 circulation 子分數 < 100。"""
    rng = np.random.default_rng(0)
    th, fl = random_theta(rng)
    spec = furnish_spec(bsp_to_spec(th, fl, 20000.0, 14000.0, 3))
    rep = analyze_room_circulation(spec)
    assert rep.blocked                       # 至少一房動線被擋
    assert not rep.ok
    gs = score_report(spec)
    assert gs["sub_scores"]["circulation"] < 100.0


def test_good_layout_scores_circulation_full():
    """合格佈局的 circulation 子分數 = 100。"""
    gs = score_report(_case("S04"))
    assert gs["sub_scores"]["circulation"] == 100.0


# ── 子分數已接進 Global Score ────────────────────────────────────────────────
def test_circulation_is_a_global_sub_score():
    assert "circulation" in SCORE_ITEMS
    assert "circulation" in score_report(_case("S04"))["sub_scores"]


# ── skip_kinds ──────────────────────────────────────────────────────────────
def test_skip_kinds_filters_rooms():
    spec = _case("S04")
    full = analyze_room_circulation(spec)
    less = analyze_room_circulation(
        spec, skip_kinds=("bedroom", "patio", "parking", "garage", "stair",
                          "balcony"))
    assert len(less.rooms) < len(full.rooms)     # 臥室被濾掉
    assert all(r.kind != "bedroom" for r in less.rooms)
