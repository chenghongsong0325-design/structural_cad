"""關鍵數字(src/design/metrics.py)的單元測試。

驗證重點:
  1. 基地/建築面積、建蔽率、容積率算得對(用已知尺寸的透天棟驗算)。
  2. 地下室:面積另計、不進容積率;造價含地下加成。
  3. 天井:從建築面積/樓地板扣除。
"""
from __future__ import annotations

import pytest

from src.design.building_generator import BuildingBrief, generate_building
from src.design.layout_generator import HouseBrief
from src.design.metrics import M2_PER_PING, building_metrics


def _house(floors=3, basements=1, **kw) -> BuildingBrief:
    base = dict(site_width=19000, site_depth=13000, bedrooms=3, seed=5)
    base.update(kw)
    return BuildingBrief(typical=HouseBrief(**base), floors=floors,
                         basements=basements, differentiated=True)


def test_metrics_house_values() -> None:
    b = generate_building(_house())
    m = building_metrics(b)

    assert m["site_area_m2"] == pytest.approx(19 * 13, abs=0.1)   # 247
    # 建築面積 = 1F 外殼(軸網範圍);建蔽率 = 建築/基地。
    spec = next(f.spec for f in b.floors if f.level == 1)
    shell = sum(spec.x_spacings) * sum(spec.y_spacings) / 1e6
    assert m["footprint_m2"] == pytest.approx(shell, abs=0.1)
    assert m["coverage_pct"] == round(shell / 247 * 100)
    # 三層樓地板 = 3×外殼;容積率不含地下。
    assert m["floors_area_m2"] == pytest.approx(3 * shell, abs=0.3)
    assert m["far_pct"] == round(3 * shell / 247 * 100)
    assert m["basement_m2"] == pytest.approx(shell, abs=0.1)
    # 總坪數 = (地上+地下)/坪;造價 >0。
    assert m["total_ping"] == pytest.approx(4 * shell / M2_PER_PING, abs=0.3)
    assert m["est_cost_wan"] > 0


def test_metrics_basement_costs_more_per_area() -> None:
    """同面積下,含地下室的棟造價要比純地上高(地下 ×1.6 加成)。"""
    with_b = building_metrics(generate_building(_house(floors=2, basements=1)))
    no_b = building_metrics(generate_building(_house(floors=3, basements=0)))
    # 兩棟總面積相同(外殼一樣、都是三層樓板),含地下的要更貴。
    assert with_b["total_ping"] == pytest.approx(no_b["total_ping"], abs=0.3)
    assert with_b["est_cost_wan"] > no_b["est_cost_wan"]


def test_metrics_patio_excluded() -> None:
    """深基地有天井的棟:建築面積要扣天井(建蔽率跟著降)。"""
    b = generate_building(_house(site_width=19000, site_depth=19000,
                                 floors=2, basements=0))
    spec = next(f.spec for f in b.floors if f.level == 1)
    patio = [r for r in spec.rooms if r.kind == "patio"]
    assert patio, "19×19 深基地應該有天井(前提檢查)"
    m = building_metrics(b)
    shell = sum(spec.x_spacings) * sum(spec.y_spacings) / 1e6
    assert m["footprint_m2"] < shell - 1     # 確實扣掉了(天井 > 1 m²)
