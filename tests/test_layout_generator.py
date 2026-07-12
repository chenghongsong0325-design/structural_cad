"""規則式格局產生器(C1)的單元測試。

核心保證:多組不同需求(單戶矩陣 × 集合住宅)都產出「通過 validate_spec
檢核」的完整設計,並端到端畫成圖——大量出圖、張張合格。
"""
from __future__ import annotations

import pytest

from src.design.layout_generator import (
    CorridorBrief,
    HouseBrief,
    generate_floor_plan,
    validate_spec,
)
from src.drafting.apartment_plan import draw_floor_plan
from src.drafting.fixtures import Counter, FixturePlacement
from src.standards.loader import apply_standard, load_standard, new_document

HOUSE_BRIEFS = [
    HouseBrief(site_width=12000, site_depth=11000, bedrooms=1),
    HouseBrief(site_width=12000, site_depth=11000, bedrooms=2),
    HouseBrief(site_width=14000, site_depth=12000, bedrooms=2),
    HouseBrief(site_width=16000, site_depth=14000, bedrooms=3),
    HouseBrief(site_width=18000, site_depth=13000, bedrooms=3),
    HouseBrief(site_width=20000, site_depth=13000, bedrooms=4),
    HouseBrief(site_width=22000, site_depth=15000, bedrooms=4),
]
CORRIDOR_BRIEFS = [
    CorridorBrief(units_per_row=2),
    CorridorBrief(units_per_row=4),
    CorridorBrief(units_per_row=6, corridor_width=2000),
]
ALL_BRIEFS = HOUSE_BRIEFS + CORRIDOR_BRIEFS


def _brief_id(b) -> str:
    if isinstance(b, HouseBrief):
        return f"house-{b.site_width//1000}x{b.site_depth//1000}-{b.bedrooms}房"
    return f"corridor-{b.units_per_row}戶"


# ---------------------------------------------------------------------------
# 1) 核心保證:每組需求 → 合格設計 → 能出圖
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("brief", ALL_BRIEFS, ids=_brief_id)
def test_generate_passes_validation(brief) -> None:
    spec = generate_floor_plan(brief)      # 內部已跑檢核,失敗會 raise
    assert validate_spec(spec) == []


@pytest.mark.parametrize("brief", [HOUSE_BRIEFS[3], CORRIDOR_BRIEFS[1]], ids=_brief_id)
def test_generate_draws_end_to_end(brief) -> None:
    spec = generate_floor_plan(brief)
    std = load_standard()
    doc = new_document()
    layers = apply_standard(doc, std)
    draw_floor_plan(doc.modelspace(), spec, layers)
    msp = doc.modelspace()
    assert len(list(msp)) > 100
    assert len(list(msp.query("DIMENSION"))) > 10   # 尺寸鏈
    assert len(list(msp.query("INSERT"))) > 10      # 門窗+家具+圖面配件


# ---------------------------------------------------------------------------
# 2) 單戶:設計內容隨需求變化
# ---------------------------------------------------------------------------
def test_bedroom_count_matches_brief() -> None:
    for n in (1, 2, 3, 4):
        spec = generate_floor_plan(HouseBrief(site_width=20000, site_depth=14000, bedrooms=n))
        assert len([r for r in spec.rooms if r.kind == "bedroom"]) == n


def test_master_bedroom_is_biggest() -> None:
    spec = generate_floor_plan(HouseBrief(site_width=16000, site_depth=14000, bedrooms=3))
    beds = {r.name: r.area_m2 for r in spec.rooms if r.kind == "bedroom"}
    assert beds["主臥室"] > beds["臥室A"]


def test_small_site_merges_dining() -> None:
    small = generate_floor_plan(HouseBrief(site_width=12000, site_depth=11000, bedrooms=2))
    assert "客餐廳" in {r.name for r in small.rooms}
    big = generate_floor_plan(HouseBrief(site_width=20000, site_depth=14000, bedrooms=3))
    assert "餐廳" in {r.name for r in big.rooms}


def test_house_fixtures_follow_rooms() -> None:
    """家具數量跟房型走:每臥室 床+衣櫃、浴廁 馬桶+洗手台、廚房 L 型流理台。"""
    spec = generate_floor_plan(HouseBrief(site_width=18000, site_depth=14000, bedrooms=3))
    fx = spec.fixtures
    names = [f.name for f in fx if isinstance(f, FixturePlacement)]
    assert names.count("bed_double") == 1
    assert names.count("bed_single") == 2
    assert names.count("wardrobe") == 3
    assert names.count("toilet") == 1 and names.count("basin") == 1
    assert names.count("sofa3") == 1
    counters = [f for f in fx if isinstance(f, Counter)]
    assert len(counters) == 2                      # L 型 = 兩段
    assert sum(1 for c in counters if c.sink) == 1


def test_house_rooms_have_codes() -> None:
    spec = generate_floor_plan(HouseBrief(site_width=16000, site_depth=14000, bedrooms=3))
    assert all(r.code for r in spec.rooms)
    assert {r.code for r in spec.rooms if r.kind == "bedroom"} == {"X05"}


# ---------------------------------------------------------------------------
# 3) 集合住宅
# ---------------------------------------------------------------------------
def test_corridor_unit_count_scales() -> None:
    for n in (2, 4, 6):
        spec = generate_floor_plan(CorridorBrief(units_per_row=n))
        assert len([r for r in spec.rooms if r.name == "1房型"]) == 2 * n
        assert len(spec.doors) == 2 * (2 * n)      # 每戶 入口+浴廁門


def test_corridor_area_closure() -> None:
    spec = generate_floor_plan(CorridorBrief(units_per_row=4))
    building = 16.0 * 13.8
    assert sum(r.area_m2 for r in spec.rooms) == pytest.approx(building)


# ---------------------------------------------------------------------------
# 4) 不合理需求要報清楚的錯
# ---------------------------------------------------------------------------
def test_too_small_site_raises() -> None:
    with pytest.raises(ValueError):
        generate_floor_plan(HouseBrief(site_width=9000, site_depth=8000, bedrooms=2))


def test_too_many_bedrooms_raises() -> None:
    with pytest.raises(ValueError):
        generate_floor_plan(HouseBrief(site_width=13000, site_depth=12000, bedrooms=4))


def test_invalid_bedroom_count_raises() -> None:
    with pytest.raises(ValueError):
        generate_floor_plan(HouseBrief(site_width=16000, site_depth=14000, bedrooms=5))


def test_invalid_units_per_row_raises() -> None:
    with pytest.raises(ValueError):
        generate_floor_plan(CorridorBrief(units_per_row=1))


def test_unknown_brief_type_raises() -> None:
    with pytest.raises(TypeError):
        generate_floor_plan("三房兩廳")   # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 5) 確定性:同需求 → 同設計
# ---------------------------------------------------------------------------
def test_same_brief_same_design() -> None:
    brief = HouseBrief(site_width=16000, site_depth=14000, bedrooms=3)
    a = generate_floor_plan(brief)
    b = generate_floor_plan(brief)
    assert [r.points for r in a.rooms] == [r.points for r in b.rooms]
    assert [(w.start, w.end, w.thickness) for w in a.walls] == \
           [(w.start, w.end, w.thickness) for w in b.walls]
