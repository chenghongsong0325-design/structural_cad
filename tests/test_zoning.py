"""使用分區 / 建蔽率 → 連棟街屋能蓋多大(design/zoning.py)。

這條產線以前的毛病:拿**獨棟**的「四面各退 2m」去算真實透天基地,5×20m 的地
算出「建築 1×16m」然後直接 raise —— 使用者 2026-08-25 給真實尺寸時撞到的。
連棟街屋左右共壁、側邊不退縮,建築進深由**建蔽率**決定。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from src.design.building_generator import BuildingBrief, generate_building_auto
from src.design.layout.code_check import check_code_building
from src.design.layout.plan_check import building_env, check_building
from src.design.layout_generator import HouseBrief
from src.design.metrics import building_metrics
from src.design.zoning import (
    COVERAGE_BY_ZONE,
    coverage_for,
    max_building_depth,
    townhouse_envelope,
)


# ── 建蔽率本身 ──────────────────────────────────────────────────────────────
def test_coverage_comes_from_the_zone():
    assert coverage_for("住宅區") == COVERAGE_BY_ZONE["住宅區"] == 0.60
    assert coverage_for("商業區") == 0.80
    assert coverage_for() == COVERAGE_BY_ZONE["住宅區"]      # 預設住宅區


def test_explicit_coverage_beats_the_zone_table():
    """★ 手上有都市計畫書的實際數字時,那份數字比常見值表可信。"""
    assert coverage_for("住宅區", 0.5) == 0.5
    with pytest.raises(ValueError):
        coverage_for("住宅區", 1.5)
    with pytest.raises(ValueError):
        coverage_for("農業區")                               # 表裡沒有就報錯


def test_envelope_reproduces_the_hand_calculation():
    """★★ 使用者手算的那條推算鏈,程式要一步不差地重現。

        基地 5×20 = 100㎡ → 建蔽 60% → 建築 60㎡ → 面寬 5m → 進深 12m
        剩下 8m → 前院 4 + 後院 4
    """
    lot = townhouse_envelope(5000.0, 20000.0, zone="住宅區")
    assert lot.site_area_m2 == pytest.approx(100.0)
    assert lot.building_w == 5000.0                          # 共壁:面寬用滿
    assert lot.building_d == pytest.approx(12000.0)
    assert lot.building_area_m2 == pytest.approx(60.0)
    assert lot.front_yard == lot.rear_yard == pytest.approx(4000.0)


def test_coverage_only_limits_depth():
    """★ 連棟街屋的面寬整塊用滿 → 建蔽率限制的**只有進深**(面寬約掉了)。"""
    for w in (3500.0, 4500.0, 8000.0):
        assert townhouse_envelope(w, 20000.0).building_d == pytest.approx(12000.0)
    assert max_building_depth(20000.0, zone="商業區") == pytest.approx(16000.0)


def test_report_has_the_serialisation_trio():
    """★ 專案慣例:每個 Report 都要有 summary() / to_dict() / to_json()。"""
    lot = townhouse_envelope(5000.0, 20000.0)
    assert "建蔽" in lot.summary()
    d = lot.to_dict()
    assert d["coverage_limit"] == 0.6 and d["building_d"] == 12000.0
    import json
    assert json.loads(lot.to_json())["zone"] == "住宅區"


# ── 接進產線 ────────────────────────────────────────────────────────────────
def _house(sw_m, sd_m, **kw):
    return BuildingBrief(
        typical=HouseBrief(site_width=sw_m * 1000, site_depth=sd_m * 1000,
                           bedrooms=3, seed=0, **kw),
        floors=3, differentiated=True)


def test_real_townhouse_lot_now_generates():
    """★★ 真實透天基地 5×20m:改之前算出「建築 1×16m」直接 raise。"""
    bld = generate_building_auto(_house(5.0, 20.0))
    floors = [(f.spec.floor_label, f.spec) for f in bld.floors]
    assert check_building(floors).ok
    assert check_code_building(floors).ok


@pytest.mark.parametrize("sw", [4.0, 4.5, 5.0, 6.0, 8.0])
def test_party_wall_sits_on_the_lot_line(sw):
    """★★ 共壁:建築側緣**就是**地界線,不退側院(退了會在兩戶之間留一條縫)。"""
    bld = generate_building_auto(_house(sw, 20.0))
    spec = bld.floors[0].spec
    x0, y0, x1, y1 = building_env(spec)
    xs = [p[0] for p in spec.site_boundary]
    assert x0 == pytest.approx(min(xs)), sw
    assert x1 == pytest.approx(max(xs)), sw


@pytest.mark.parametrize("sw,sd,zone", [(5.0, 20.0, None), (4.5, 20.0, None),
                                        (4.5, 15.0, "商業區"), (6.0, 20.0, None)])
def test_coverage_is_respected_and_yards_are_left(sw, sd, zone):
    """★★ 蓋出來的建蔽率不得超過分區上限,而且剩下的地真的變成前後院。"""
    bld = generate_building_auto(_house(sw, sd, zone=zone))
    spec = bld.floors[0].spec
    limit = coverage_for(zone)
    assert building_metrics(bld)["coverage_pct"] <= limit * 100 + 1e-6
    x0, y0, x1, y1 = building_env(spec)
    ys = [p[1] for p in spec.site_boundary]
    assert y0 - min(ys) > 100.0 and max(ys) - y1 > 100.0      # 前後都留了院子


def test_the_drawing_says_why_it_is_only_this_deep():
    """★ 圖上要看得到建蔽率 —— 只給建築尺寸的話,沒人知道為什麼只蓋這麼深。"""
    spec = generate_building_auto(_house(5.0, 20.0)).floors[0].spec
    assert spec.lot_note and "建蔽率" in spec.lot_note
    assert "前院" in spec.lot_note and "後院" in spec.lot_note


def test_residential_zone_needs_a_deep_lot():
    """★★ 住宅區 60% 是很緊的:基地 4.5×15m 只能蓋 9m 深,低於骨架下限。

    這不是 bug,是**真實的限制** —— 使用者自己指出那張參考圖的建蔽率高達 93%,
    只可能是商業區或既存老屋。同一塊地換成商業區(80%)就生得出來。"""
    with pytest.raises(ValueError):
        generate_building_auto(_house(4.5, 15.0))            # 住宅區 → 9.0m 深
    generate_building_auto(_house(4.5, 15.0, zone="商業區"))  # 商業區 → 12.0m ✅


def test_building_basis_is_untouched():
    """★★ 講「建築物 5×15」的人已經自己扣好了,不能再套一次建蔽率。

    掃描/預覽腳本走的就是這條(尺寸一律當建築物尺寸),套兩次會讓
    「建築 5×15」變成「基地 5×15 → 建築 5×9」直接 raise。"""
    bld = generate_building_auto(
        _house(5.0, 15.0, setback=0, dimension_basis="building"))
    x0, y0, x1, y1 = building_env(bld.floors[0].spec)
    assert y1 - y0 > 13000.0                                 # 沒有被建蔽率砍掉
