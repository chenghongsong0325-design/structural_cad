"""戶型組裝模組(apartment_plan)的單元測試。

驗證重點:
  1. 幾何輔助:建築線退縮、軸網平移到 grid_origin、柱自動放軸網交點。
  2. draw_floor_plan 生產線:各元素掛在正確的規範圖層(BORDER/ARCH/COL/
     WALL/DW/TEXT/DIM/OTHER)。
  3. 尚未實作的欄位(樓梯/電梯/陽台/設備)填了要明確報 NotImplementedError。
  4. 示範戶型 demo_spec 能端到端跑完,房間面積合理。
"""
from __future__ import annotations

import pytest

from src.drafting.apartment_plan import (
    FloorPlanSpec,
    build_grid,
    building_line,
    demo_spec,
    draw_floor_plan,
    resolve_columns,
)
from src.standards.loader import apply_standard, load_standard, new_document


@pytest.fixture()
def doc_and_layers():
    standard = load_standard()
    doc = new_document()
    layers = apply_standard(doc, standard)
    return doc, layers


def _minimal_spec(**overrides) -> FloorPlanSpec:
    """一份最小可畫的 spec(只有基地與軸網),測試各別功能用。"""
    base = dict(
        site_boundary=[(0, 0), (16000, 0), (16000, 14000), (0, 14000)],
        setback=2000,
        x_spacings=[4000, 4000],
        y_spacings=[5000],
        grid_origin=(2000, 2000),
    )
    base.update(overrides)
    return FloorPlanSpec(**base)


# ---------------------------------------------------------------------------
# 1) 幾何輔助
# ---------------------------------------------------------------------------
def test_building_line_is_setback_rectangle() -> None:
    spec = _minimal_spec()
    pts = building_line(spec)
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    # 16m×14m 基地退縮 2m → 建築線 2000..14000 × 2000..12000。
    assert (min(xs), max(xs)) == (2000, 14000)
    assert (min(ys), max(ys)) == (2000, 12000)


def test_building_line_too_big_setback_raises() -> None:
    spec = _minimal_spec(setback=99999)
    with pytest.raises(ValueError):
        building_line(spec)


def test_build_grid_shifted_to_origin() -> None:
    grid = build_grid(_minimal_spec())
    assert [a.position for a in grid.x_axes] == [2000, 6000, 10000]
    assert [a.position for a in grid.y_axes] == [2000, 7000]


def test_resolve_columns_default_all_intersections() -> None:
    spec = _minimal_spec()
    cols = resolve_columns(spec, build_grid(spec))
    # 3 條 X 軸 × 2 條 Y 軸 = 6 根柱。
    assert len(cols) == 6
    centers = {c.center for c in cols}
    assert (2000, 2000) in centers
    assert (10000, 7000) in centers
    assert all(c.width == spec.column_size for c in cols)


def test_resolve_columns_explicit_centers() -> None:
    spec = _minimal_spec(column_centers=[(2000, 2000), (10000, 7000)])
    cols = resolve_columns(spec, build_grid(spec))
    assert len(cols) == 2


# ---------------------------------------------------------------------------
# 2) 生產線:圖層歸屬
# ---------------------------------------------------------------------------
def test_draw_floor_plan_layers(doc_and_layers) -> None:
    doc, layers = doc_and_layers
    msp = doc.modelspace()

    draw_floor_plan(msp, demo_spec(), layers)

    by_layer: dict[str, int] = {}
    for e in msp:
        by_layer[e.dxf.layer] = by_layer.get(e.dxf.layer, 0) + 1

    # 地界線與建築線各一條封閉多義線。
    assert by_layer.get("BORDER") == 1
    assert by_layer.get("ARCH") == 1
    # 柱 12 根(4×3 軸網交點)。
    assert by_layer.get("COL") == 12
    # 牆(聯集後的輪廓)至少一條。
    assert by_layer.get("WALL", 0) >= 1
    # 門 7(含樓梯間門)+ 窗 7 = 14 個 INSERT 在 DW。
    assert by_layer.get("DW") == 14
    # OTHER:A3 圖框 2 + 標題欄 1 + 電梯符號 3 + 設備家具圖塊 11 +
    #        流理台(2 段多義線 + 1 水槽圓)3 = 20。
    assert by_layer.get("OTHER") == 20
    # 尺度在 DIM:四邊三層尺寸鏈(細部 20 + 軸距 10 + 總長 4)= 34 個。
    assert by_layer.get("DIM") == 34
    # 文字(軸網編號 7 + 房間名稱/面積 7×2 = 21)在 TEXT 之上(軸網圈在 AXIS)。
    assert by_layer.get("TEXT", 0) >= 21


def test_draw_floor_plan_door_window_are_inserts(doc_and_layers) -> None:
    doc, layers = doc_and_layers
    msp = doc.modelspace()
    draw_floor_plan(msp, demo_spec(), layers)

    inserts = [e for e in msp.query("INSERT") if e.dxf.layer == "DW"]
    names = {i.dxf.name for i in inserts}
    assert "DOOR" in names
    assert any(n.startswith("WINDOW_") for n in names)


# ---------------------------------------------------------------------------
# 4) 示範戶型
# ---------------------------------------------------------------------------
def test_demo_spec_room_areas_reasonable() -> None:
    spec = demo_spec()
    by_name = {r.name: r for r in spec.rooms}
    # 客廳 4.6×5(東側整條讓給樓梯間+電梯的垂直動線核)。
    assert by_name["客廳"].area_m2 == pytest.approx(23.0)
    assert by_name["樓梯間"].area_m2 == pytest.approx(3.92)
    assert by_name["電梯"].area_m2 == pytest.approx(3.08)
    assert by_name["主臥室"].area_m2 == pytest.approx(25.0)
    assert by_name["浴廁"].area_m2 == pytest.approx(7.5)
    # 房間總面積 = 建築範圍 12m×10m = 120 m²(以牆中心線計)。
    assert sum(r.area_m2 for r in spec.rooms) == pytest.approx(120.0)


def test_demo_spec_door_window_references_valid() -> None:
    """門窗指到的牆/洞口索引必須存在,且洞口種類相符(門→door、窗→window)。"""
    spec = demo_spec()
    for dp in spec.doors:
        op = spec.walls[dp.wall_index].openings[dp.opening_index]
        assert op.kind == "door"
    for wp in spec.windows:
        op = spec.walls[wp.wall_index].openings[wp.opening_index]
        assert op.kind == "window"


def test_demo_runs_end_to_end(doc_and_layers) -> None:
    """整條生產線跑完不出錯,且產出實體數量非空。"""
    doc, layers = doc_and_layers
    msp = doc.modelspace()
    draw_floor_plan(msp, demo_spec(), layers)
    assert len(list(msp)) > 50
