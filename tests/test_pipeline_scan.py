"""全產線隨機掃描(回歸網)—— 「以後的圖都不會有這些問題」的依據。

單元測試守的是**已知案例**;這一支守的是**沒人想到的尺寸組合**:固定亂數種子抽
一批需求(透天/集合住宅、各種寬深房數樓層),整棟送 plan_check + code_check,
硬錯誤一律 0。

判準與產線完全一致(不另立標準):
  * 生得出來 → 兩道關卡都不能有 error/violation。
  * 生不出來 → 只允許 **ValueError 並附白話原因**(容量/尺寸不足),不允許其他例外
    (那是程式沒接住的錯)。

⚠️ 這裡只跑 60 案(約 1 分鐘)。改骨架後請另外跑完整版:
    python scripts/scan_plans.py --n 400 --seed 20260802
"""
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from src.design.building_generator import BuildingBrief, generate_building_auto
from src.design.layout.code_check import check_code_building
from src.design.layout.plan_check import check_building
from src.design.layout_generator import CorridorBrief, HouseBrief

SCAN_SEED = 20260802
SCAN_N = 60


def _cases(n: int, seed: int) -> list:
    """隨機需求(建築尺寸;setback=0 → 尺寸直接對應各骨架的定義域)。"""
    rng = random.Random(seed)
    out = []
    for _ in range(n):
        out.append({
            "w": round(rng.uniform(4000, 28000), -2),
            "d": round(rng.uniform(5000, 18000), -2),
            "bedrooms": rng.randint(1, 4),
            "floors": rng.choice([1, 2, 3]),
            "seed": rng.randrange(10000),
        })
    return out


@pytest.mark.slow
def test_random_scan_has_no_hard_errors():
    """★★ 隨機 60 案:出得了圖的一律零硬錯誤;出不了圖的只能是 ValueError。"""
    bad, raised = [], []
    for c in _cases(SCAN_N, SCAN_SEED):
        brief = BuildingBrief(
            typical=HouseBrief(site_width=c["w"], site_depth=c["d"],
                               bedrooms=c["bedrooms"], setback=0,
                               seed=c["seed"]),
            floors=c["floors"], differentiated=c["floors"] > 1)
        try:
            building = generate_building_auto(brief)
        except ValueError as exc:                 # 尺寸不夠 → 明確拒絕,可接受
            raised.append((c, str(exc)))
            continue
        except Exception as exc:                  # 其他例外 = 程式沒接住
            bad.append((c, f"{type(exc).__name__}: {exc}"))
            continue
        floors = [(f.label, f.spec) for f in building.floors]
        plan = check_building(floors)
        code = check_code_building(floors)
        if not (plan.ok and code.ok):
            bad.append((c, [i.code for i in plan.errors + code.violations]))
    assert not bad, f"{len(bad)} 案生出不合格圖:{bad[:5]}"
    assert len(raised) < SCAN_N * 0.6, "太多案生不出來,定義域可能壞了"


@pytest.mark.parametrize("units", [2, 4, 6])
def test_apartment_floor_is_clean(units):
    """★ 集合住宅標準層(2/4/6 戶/排):兩道關卡全過。"""
    from src.design.layout_generator import generate_floor_plan
    spec = generate_floor_plan(CorridorBrief(units_per_row=units))
    plan = check_building([("3F", spec)])
    code = check_code_building([("3F", spec)])
    assert plan.ok, [(i.code, i.room) for i in plan.errors]
    assert code.ok, [(i.code, i.room) for i in code.violations]


@pytest.mark.parametrize("bw", [12000.0, 16000.0, 20000.0])
@pytest.mark.parametrize("bd", [9000.0, 13000.0])
def test_two_band_house_is_clean(bw, bd):
    """★★ 兩帶式單戶住宅(建築 ≥10m 寬):兩道關卡全過。

    這條產線比關卡早做,長期沒被驗收過——2026-08-02 修好後由這條守住。"""
    from src.design.layout_generator import generate_floor_plan
    spec = generate_floor_plan(HouseBrief(site_width=bw + 4000,
                                          site_depth=bd + 4000, bedrooms=3))
    plan = check_building([("1F", spec)])
    code = check_code_building([("1F", spec)])
    assert plan.ok, [(i.code, i.room) for i in plan.errors]
    assert code.ok, [(i.code, i.room) for i in code.violations]
