"""AI 選配版(src/design/layout/townhouse_options.py)的單元測試。

使用者 2026-08-28 指著 AI 產線的圖說:「AI 做的圖不是正確的,不會分成這麼多的
間格,AI 只要從我做的格局稍加修改變更就好。」這支測試釘的就是那件事:

  1. LLM 的答案一律要被**夾**成合法的一組(它會給 10 層樓、3.5m 配車庫)。
  2. 夾不住的組合要**一級一級退**,不是把錯誤丟給使用者。
  3. 蓋出來的是**我們的透天骨架**(樓梯間/浴廁/餐廚),不是關係圖版那種
     「兩間客廳 + 0.5㎡ 管道間」。

全程不碰網路:client 注入假物件(同 test_room_graph 的做法)。
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.design.layout import townhouse_options as topts
from src.design.layout.code_check import check_code_building
from src.design.layout.plan_check import check_building

_GOOD = {"core_style": "mid", "floors": 3, "bedrooms": 3, "garage": False,
         "patio": False, "mirror": False, "open_kitchen": True,
         "entry_frac": 0.22, "rationale": "測試用"}


# ---------------------------------------------------------------------------
# 假 Gemini client
# ---------------------------------------------------------------------------
@dataclass
class _FakeResponse:
    text: str


class _FakeModels:
    def __init__(self, payload):
        self.payload = payload
        self.calls = 0

    def generate_content(self, **kwargs):
        self.calls += 1
        return _FakeResponse(json.dumps(self.payload, ensure_ascii=False))


class _FakeClient:
    def __init__(self, payload=None):
        self.models = _FakeModels(payload if payload is not None else _GOOD)


# ---------------------------------------------------------------------------
# ① 夾值:LLM 給什麼都要變成一組合法的選項
# ---------------------------------------------------------------------------
def test_nonsense_from_the_llm_is_clamped_into_something_legal():
    """★★ schema 擋得住型別,擋不住「物理上不可能」。

    10 層樓、0 間臥室、不存在的核、0.9 的大門位置、淺基地配車庫 —— 這些
    LLM 都給得出來,夾不住就會一路帶到 `generate_narrow_building` 那邊 raise。"""
    got = topts.normalize_options(
        {"core_style": "沒有這款", "floors": 99, "bedrooms": 0, "garage": True,
         "patio": True, "mirror": "yes", "open_kitchen": False,
         "entry_frac": 0.93, "rationale": ""},
        width=4000.0, depth=12500.0)
    assert got["core_style"] in topts.CORE_STYLES
    assert 1 <= got["floors"] <= 4 and 1 <= got["bedrooms"] <= 4
    assert got["entry_frac"] in topts.ENTRY_FRACS
    assert got["garage"] is False          # 12.5m 進深放不下一個車位長的前段
    # 天井只有方案 B 的核放得下 —— 核名亂給時會被夾成預設的那一款,所以這裡
    # 跟著夾出來的核問(預設 2026-08-28 起是方案 B,見 normalize_options)。
    assert got["patio"] is (got["core_style"] == "ref")


def test_patio_only_survives_on_the_reference_core():
    """★★ 天井只有方案 B 排得下:另外兩款的面寬已經被三段用完。"""
    for style, want in (("ref", True), ("mid", False), ("default", False)):
        got = topts.normalize_options({**_GOOD, "core_style": style,
                                       "patio": True},
                                      width=4500.0, depth=14450.0)
        assert got["patio"] is want, style


def test_one_floor_never_gets_a_garage():
    """★★ 一層樓配車庫 = 連客廳都沒有(客廳是往上挪到 2F 的)。"""
    got = topts.normalize_options({**_GOOD, "floors": 1, "garage": True},
                                  width=4500.0, depth=14450.0)
    assert got["garage"] is False


# ---------------------------------------------------------------------------
# ② 退讓階梯:選了放不下的組合要靜靜地退,不是 raise
# ---------------------------------------------------------------------------
def test_impossible_combo_retreats_instead_of_raising():
    """★★ 加分項不得讓原本生得出來的案子生不出來(AGENTS.md 那條鐵則)。

最窄的面寬 + 車庫(前段要一整個車位長 5.5m)在 12.5m 進深下排不下 —— 那時
    要退掉車庫把圖生出來,而不是把 ValueError 丟給使用者。

    ⚠️ 原本釘的是「3.6m 面寬配方案 B 排不下」。面寬下限升到 4.0m(2026-09-04
       只做折返梯)之後,定義域裡 ref 每個尺寸都排得下,那個組合已經不會發生 ——
       改用**還會發生**的那一種(車庫),否則這條測試等於什麼都沒驗。"""
    floors, used = topts.build_from_options(
        4000.0, 12500.0, {**_GOOD, "core_style": "ref", "garage": True})
    assert floors and check_building(floors).ok
    assert used["core_style"] in topts.CORE_STYLES


# ---------------------------------------------------------------------------
# ③ 蓋出來的是**我們的骨架**,不是關係圖版那種一堆小格子
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("bw,bd", [(4500.0, 14450.0), (7000.0, 15000.0)])
def test_ai_builds_our_townhouse_skeleton_not_its_own(bw, bd):
    """★★ 使用者 2026-08-28:「不會分成這麼多的間格。」

    關係圖版在 7m 面寬會切出**兩間**「客廳」外加一間 0.5㎡ 的「管道間」——
    那不是真實街屋。選配版蓋的是骨架本身:樓梯間 + 浴廁 + 餐廚,房名不重複,
    而且窄透天**不放管道間**(那是 AI 關係圖版的核才有的東西)。"""
    best, _hist = topts.design_townhouse("透天三層三房", bw, bd,
                                         client=_FakeClient(), verbose=False)
    floors = best["floors"]
    assert check_building(floors).ok, [str(i)
                                       for i in check_building(floors).errors]
    assert check_code_building(floors).ok
    for label, spec in floors:
        names = [r.name for r in spec.rooms]
        kinds = {r.kind for r in spec.rooms}
        assert len(names) == len(set(names)), (label, names)   # 沒有同名房
        assert "pipe_shaft" not in kinds, (label, names)       # 窄透天不放管道間
        assert "stair_hall" in kinds and "bathroom" in kinds, (label, names)


def test_the_loop_keeps_the_best_and_never_regresses():
    """★★ 收斂迴圈的規矩與關係圖版一致:留 fitness 最高的那版。"""
    best, history = topts.design_townhouse("透天三層三房", 4500.0, 14450.0,
                                           iterations=2, client=_FakeClient(),
                                           verbose=False)
    assert history
    assert best["fitness"] == max(h["fitness"] for h in history)


def test_design_townhouse_refuses_sizes_the_skeleton_cannot_take():
    """★★ 骨架收不下的尺寸要明講(呼叫端才知道要退回關係圖版)。"""
    with pytest.raises(ValueError):
        topts.design_townhouse("透天", 12000.0, 15000.0, client=_FakeClient(),
                               verbose=False)


# ---------------------------------------------------------------------------
# ④ 網站真的走這條(使用者回報的就是網站下載的 DXF)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("bw,bd", [(4500.0, 14450.0), (7000.0, 15000.0)])
def test_web_ai_mode_uses_the_option_pipeline_for_townhouses(bw, bd):
    """★★ 使用者是從**網站**下載 DXF 的,所以要釘住網站那條路徑真的換掉了。

    ⚠️ 4~5m 是最常見的街屋面寬,而關係圖版的下限是 5m —— 舊的定義域檢查會在
    這裡就把 4.5m 踢掉,永遠走不到選配版。定義域要是**兩條的聯集**。"""
    from src.design.building_generator import BuildingBrief
    from src.design.layout_generator import HouseBrief
    from src.web.app import _ai_applicable, _ai_generate

    brief = BuildingBrief(
        typical=HouseBrief(site_width=bw, site_depth=bd, bedrooms=3,
                           setback=0, dimension_basis="building"),
        floors=3, differentiated=True)
    assert _ai_applicable(brief)
    _building, extra = _ai_generate("透天三層三房", brief, _FakeClient())
    assert extra["ai_core_style"] in topts.CORE_STYLES     # 不是 "graph"
    assert extra["plan_check"]["ok"] and extra["code_check"]["ok"]
