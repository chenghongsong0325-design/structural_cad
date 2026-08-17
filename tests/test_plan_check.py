"""圖面正確性檢查器 + 產線關卡測試。

這組測試的目的不是「檢查某一張圖」,而是保證**產線不會輸出壞圖**:

  * 檢查器本身抓得到人為破壞(拆掉門 → 抓到沒門/斷開;家具塞進牆 → 抓到穿牆)。
  * error / warning 分界正確(落實端救得動的才是 error)。
  * Report 可序列化(to_dict/to_json),照專案慣例。
  * **隨機掃描**:多種尺寸 × 多份隨機關係圖 × 多個種子,硬錯誤必須是 0。
"""
import random
import pytest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import json

from src.design.layout.graph_layout import realize_graph_building
from src.design.layout.narrow_house import generate_narrow_building
from src.design.layout.plan_check import (
    PlanCheckReport,
    check_building,
    check_floor,
)

SB = 2000.0

GRAPH = {
    "rooms": [
        {"id": "entry", "kind": "corridor", "floor": 1, "wants_daylight": False},
        {"id": "living", "kind": "living", "floor": 1, "wants_daylight": True},
        {"id": "kitchen", "kind": "kitchen", "floor": 1, "wants_daylight": True},
        {"id": "stair1", "kind": "stair", "floor": 1, "wants_daylight": False},
        {"id": "corr2", "kind": "corridor", "floor": 2, "wants_daylight": False},
        {"id": "bedA", "kind": "bedroom", "floor": 2, "wants_daylight": True},
        {"id": "bath2", "kind": "bathroom", "floor": 2, "wants_daylight": False},
        {"id": "stair2", "kind": "stair", "floor": 2, "wants_daylight": False},
    ],
    "adjacencies": [
        {"a": "entry", "b": "living", "connection": "open"},
        {"a": "living", "b": "kitchen", "connection": "open"},
        {"a": "stair2", "b": "corr2", "connection": "open"},
        {"a": "corr2", "b": "bedA", "connection": "door"},
        {"a": "corr2", "b": "bath2", "connection": "door"},
    ],
    "entry": "entry",
}


def _build(bw=7000.0, bd=12000.0, seed=7):
    env = (SB, SB, SB + bw, SB + bd)
    return realize_graph_building(GRAPH, bw, bd, rng=random.Random(seed)), env


# ── 檢查器抓不抓得到問題 ─────────────────────────────────────────────────────
def test_clean_plan_passes():
    floors, env = _build()
    rep = check_building(floors, env)
    assert rep.ok, rep.summary()


def test_detects_removed_doors():
    """★ 人為拆掉所有門 → 檢查器必須抓到「沒門」與「室內斷開」。"""
    floors, env = _build()
    _lb, spec = floors[0][0], floors[0][1]
    for w in spec.walls:                        # 把門洞全部拿掉
        w.openings = [op for op in w.openings if op.kind != "door"]
    spec.doors = []
    codes = {i.code for i in check_floor(spec, env, 1, "1F")}
    assert "room_no_door" in codes
    assert "no_entry" in codes


def test_detects_furniture_in_wall():
    """★ 把一件家具挪到牆中心線上 → 檢查器必須抓到穿牆。"""
    from src.drafting.fixtures import FixturePlacement

    floors, env = _build()
    spec = floors[0][1]
    fx = next(f for f in spec.fixtures if isinstance(f, FixturePlacement))
    assert not any(i.code == "furniture_in_wall"
                   for i in check_floor(spec, env, 1, "1F"))   # 原本是乾淨的
    wall = spec.walls[0]                        # 家具插入點移到牆中心線 → 必嵌牆
    fx.insert = wall.point_at(wall.length / 2)
    codes = [i.code for i in check_floor(spec, env, 1, "1F")]
    assert "furniture_in_wall" in codes


def test_detects_open_stair_side():
    """★ 人為拆掉梯段旁的導牆 → 檢查器必須抓到「梯段一側懸空」。

    規則版窄透天的梯段旁邊留了通道,兩者之間那道導牆若不見了,人走在階梯上就會從
    旁邊掉下去——這是看圖不明顯、但真實圖面絕不允許的錯誤。"""
    from src.design.layout.narrow_house import generate_narrow_building

    floors = generate_narrow_building(7000.0, 12000.0, floors=2)
    env = (SB, SB, SB + 7000.0, SB + 12000.0)
    spec = floors[0][1]
    assert not any(i.code == "stair_side_open"
                   for i in check_floor(spec, env, 1, "1F"))    # 原本是乾淨的
    spec.walls = [w for w in spec.walls if not getattr(w, "stair_guard", False)]
    codes = [i.code for i in check_floor(spec, env, 1, "1F")]
    assert "stair_side_open" in codes


def test_error_vs_warning_split():
    """★ 只有「換切法就能解」的問題算 error;設計面問題是 warning。"""
    floors, env = _build()
    rep = check_building(floors, env)
    hard = {"room_no_door", "floor_split", "no_entry", "entry_upstairs",
            "furniture_in_wall", "circulation_blocked", "door_in_corner",
            "stair_blocks_door", "stair_side_open"}
    for i in rep.issues:
        if i.severity == "error":
            assert i.code in hard, f"{i.code} 不該是硬錯誤"
        else:
            assert i.code not in hard


def test_report_serialisable():
    """★ Report 要能 to_dict/to_json(專案慣例)。"""
    floors, env = _build()
    rep = check_building(floors, env)
    d = rep.to_dict()
    assert set(d) == {"ok", "n_errors", "n_warnings", "issues"}
    assert json.loads(rep.to_json())["ok"] == rep.ok
    assert isinstance(PlanCheckReport().summary(), str)


# ── 產線關卡:掃描各種尺寸/關係圖,硬錯誤必須是 0 ──────────────────────────
def _random_graph(rng, n_floors):
    """隨機但合理的房間關係圖(模擬 LLM 的各種輸出)。"""
    rooms = [{"id": "entry", "kind": "corridor", "floor": 1,
              "wants_daylight": False},
             {"id": "living", "kind": "living", "floor": 1,
              "wants_daylight": True},
             {"id": "stair1", "kind": "stair", "floor": 1,
              "wants_daylight": False}]
    adj = [{"a": "entry", "b": "living", "connection": "open"}]
    if rng.random() < 0.8:
        rooms.append({"id": "kitchen", "kind": "kitchen", "floor": 1,
                      "wants_daylight": True})
        adj.append({"a": "living", "b": "kitchen", "connection": "open"})
    if rng.random() < 0.6:
        rooms.append({"id": "t1", "kind": "toilet", "floor": 1,
                      "wants_daylight": False})
        adj.append({"a": "entry", "b": "t1", "connection": "door"})
    for fl in range(2, n_floors + 1):
        rooms += [{"id": f"c{fl}", "kind": "corridor", "floor": fl,
                   "wants_daylight": False},
                  {"id": f"s{fl}", "kind": "stair", "floor": fl,
                   "wants_daylight": False},
                  {"id": f"ba{fl}", "kind": "bathroom", "floor": fl,
                   "wants_daylight": False}]
        adj += [{"a": f"s{fl}", "b": f"c{fl}", "connection": "open"},
                {"a": f"c{fl}", "b": f"ba{fl}", "connection": "door"}]
        for i in range(rng.randint(1, 2)):
            rooms.append({"id": f"b{fl}_{i}", "kind": "bedroom", "floor": fl,
                          "wants_daylight": True})
            adj.append({"a": f"c{fl}", "b": f"b{fl}_{i}", "connection": "door"})
    return {"rooms": rooms, "adjacencies": adj, "entry": "entry"}


@pytest.mark.slow
def test_sweep_ai_pipeline_never_emits_broken_plan():
    """★★ 隨機掃描:各種建築尺寸 × 隨機關係圖 × 隨機種子,硬錯誤一律 0。

    這是「以後的圖都不會有問題」的真正依據——不是挑幾個尺寸看起來對。"""
    rng = random.Random(20260728)
    bad = []
    for bw in (5000.0, 7000.0, 9000.0, 11000.0, 13000.0, 16000.0, 20000.0):
        for bd in (9000.0, 12000.0, 18000.0):
            g = _random_graph(rng, rng.choice([2, 3]))
            env = (SB, SB, SB + bw, SB + bd)
            floors = realize_graph_building(g, bw, bd,
                                            rng=random.Random(rng.randrange(10 ** 6)))
            rep = check_building(floors, env)
            if not rep.ok:
                bad.append((bw, bd, [i.code for i in rep.errors]))
    assert not bad, f"這些尺寸生出不合格圖:{bad}"


@pytest.mark.slow
def test_sweep_rule_pipeline_never_emits_broken_plan():
    """★★ 規則版窄透天(不同寬/深/層數)同樣不得有硬錯誤。"""
    bad = []
    for bw in (5000.0, 6000.0, 7000.0):
        for bd in (11000.0, 14000.0):
            for n in (1, 3):
                floors = generate_narrow_building(bw, bd, floors=n)
                # 外框讓 spec 自推:深基地會封頂建築、留前後院(見 building_env)
                rep = check_building([(lb, sp) for lb, sp in floors])
                if not rep.ok:
                    bad.append((bw, bd, n, [i.code for i in rep.errors]))
    assert not bad, f"規則版生出不合格圖:{bad}"


# ── 「房間過大」要用對的尺量 ────────────────────────────────────────────────
def test_oversize_band_uses_master_bedroom_band_for_the_master():
    """★★ 主臥的 kind 也是 "bedroom"(規則版兩帶式這樣存),不能拿次臥的上限去量。

    ⚠️ 實測 19×13m 三層:2F/3F 的主臥 27㎡ 被次臥的上限(18)判成過大,
    但主臥的合理上限是 24 —— 量錯尺,不是設計問題。"""
    from types import SimpleNamespace

    from src.design.layout.graph_layout import AREA_BAND
    from src.design.layout.plan_check import oversize_band

    master = SimpleNamespace(kind="bedroom", name="主臥室")
    plain = SimpleNamespace(kind="bedroom", name="臥室2")
    assert oversize_band(master, AREA_BAND) == AREA_BAND["master_bedroom"]
    assert oversize_band(plain, AREA_BAND) == AREA_BAND["bedroom"]


def test_oversize_band_adds_up_for_merged_rooms():
    """★★ 「餐廚」= 餐廳+廚房、「客餐廳」= 客廳+餐廳,上限要相加。

    開放式格局是我們自己選的設計,拿單獨一間餐廳的上限去量它一定超標。"""
    from types import SimpleNamespace

    from src.design.layout.graph_layout import AREA_BAND
    from src.design.layout.plan_check import oversize_band

    band = oversize_band(SimpleNamespace(kind="dining", name="餐廚"), AREA_BAND)
    assert band == (AREA_BAND["dining"][0] + AREA_BAND["kitchen"][0],
                    AREA_BAND["dining"][1] + AREA_BAND["kitchen"][1])
    band2 = oversize_band(SimpleNamespace(kind="living", name="客餐廳"), AREA_BAND)
    assert band2 == (AREA_BAND["living"][0] + AREA_BAND["dining"][0],
                     AREA_BAND["living"][1] + AREA_BAND["dining"][1])


def test_oversize_still_catches_a_genuinely_huge_room():
    """★ 尺換對了,真正過大的房間還是要抓得到(不能變成放水)。"""
    from types import SimpleNamespace

    from src.design.layout.graph_layout import AREA_BAND
    from src.design.layout.plan_check import OVERSIZE_RATIO, oversize_band

    band = oversize_band(SimpleNamespace(kind="study", name="書房"), AREA_BAND)
    assert 32.9 > band[1] * OVERSIZE_RATIO          # 改版前 1F 的書房
    band_m = oversize_band(SimpleNamespace(kind="bedroom", name="主臥室"), AREA_BAND)
    assert 60.0 > band_m[1] * OVERSIZE_RATIO        # 60㎡ 的主臥仍然過大


def test_two_band_house_has_no_false_oversize_warnings():
    """★★ 端到端:19×13m 三層不再冒出「量錯尺」的過大警告。

    改版前 10 個,其中 3 個是尺不對(餐廚 ×1、主臥 ×2)、1 個是真的
    (書房 32.9㎡,已由北帶多切一刀解掉)。"""
    from src.design.building_generator import BuildingBrief, generate_building_auto
    from src.design.layout_generator import HouseBrief

    b = generate_building_auto(BuildingBrief(
        typical=HouseBrief(site_width=19000, site_depth=13000, bedrooms=3,
                           setback=0, seed=0),
        floors=3, differentiated=True))
    rep = check_building([(f.label, f.spec) for f in b.floors])
    over = [i for i in rep.issues if i.code == "room_oversize"]
    assert not [i for i in over if "餐廚" in i.room or "主臥" in i.room or
                "書房" in i.room], [(i.floor, i.room, i.detail) for i in over]
