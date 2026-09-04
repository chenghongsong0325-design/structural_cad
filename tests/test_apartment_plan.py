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
    # 門 7(含樓梯間門)+ 窗 7 個 INSERT 在 DW。
    # ⚠️ 判準要寫成它真正的意思,不要釘死一個數字:2026-09-04 起洞口 ≥1m 的
    #    大門畫成**子母門**(一寬一窄兩片),那扇門就多一個 INSERT。從 spec
    #    自己推,以後再加別的門型也不會假性失敗。
    from src.drafting.door_window import PAIR_DOOR_MIN_W
    spec = demo_spec()
    extra = sum(1 for dp in spec.doors
                if not getattr(dp.door, "sliding", False)
                and (dp.door.width
                     or spec.walls[dp.wall_index]
                     .openings[dp.opening_index].width) >= PAIR_DOOR_MIN_W)
    assert extra >= 1, "demo_spec 要有一扇大門,不然這條測試驗不到子母門"
    assert by_layer.get("DW") == len(spec.doors) + len(spec.windows) + extra
    # OTHER:A3 圖框 2 + 標題欄 1 + 電梯符號 3 + 設備家具圖塊 11 +
    #        流理台(2 段多義線 + 1 水槽圓)3 + 北向箭頭 1 = 21;
    #        2026-08-03 起再加圖面標註(對照丙級檢定參考圖):門窗編號圈 14 +
    #        牆厚引線 2 種×2 段 4 = 18。
    #        ⚠️ 以前還有「剖切符號(線 1 + 兩端各 箭幹1+箭羽2)7 條」,使用者
    #        2026-08-19 決定拿掉 → 25 減 7 = 18。要畫回來見 FloorPlanSpec
    #        的 section_mark(功能沒刪,只是預設關)。
    #        2026-09-03 起再加**大門入口 ▲** 1 個實體(使用者給的台灣平面圖符號
    #        對照表:「大門入口 = 門外一個實心三角形」,見 draw_entry_marks)。
    #        ⚠️ 同一天加的**天井 X**(矩形+對角線 2 條)在這份 demo 圖上是 0 ——
    #        它沒有天井。有天井的圖每座天井 +2,不要以為這個數字與它無關。
    assert by_layer.get("OTHER") == 21 + 18 + 1
    # 尺度在 DIM:四邊三層尺寸鏈(細部 20 + 軸距 10 + 總長 4)= 34,
    # 加基地標註 8(下方/左方各:院|建築|院 3 段 + 基地總長 1)= 42 個。
    assert by_layer.get("DIM") == 42
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


# ── 剖切符號:預設不畫,但功能還在(使用者 2026-08-19 決定拿掉)────────────
def _cut_mark_lines(msp, spec) -> int:
    """剖切符號畫在 OTHER 層:剖切線 1 + 兩端各(箭幹 1 + 箭羽 2)= 7 條。

    用「線的條數」比對,不用位置 —— 位置會隨建築尺寸跑,條數不會。"""
    return len(msp.query('LINE[layer=="OTHER"]'))


def test_section_mark_is_off_by_default(doc_and_layers) -> None:
    """★ 使用者 2026-08-19:「幫我拿掉」。預設出的圖上不可以有剖切符號。

    判準用**代號文字**:剖切符號的「A」字高 CUT_TEXT_H(420),跟軸網編號
    (字高 250)分得開,不會誤判。"""
    from src.drafting.annotations import CUT_TEXT_H

    doc, layers = doc_and_layers
    msp = doc.modelspace()
    spec = demo_spec()
    assert spec.section_mark is None, "預設就該是關的"
    draw_floor_plan(msp, spec, layers)

    marks = [e for e in msp.query("TEXT")
             if e.dxf.text.strip() == "A" and abs(e.dxf.height - CUT_TEXT_H) < 1]
    assert marks == [], f"圖上還有 {len(marks)} 個剖切代號"


def test_section_mark_can_still_be_switched_back_on(doc_and_layers) -> None:
    """★ 拿掉的是「預設」,不是功能本身 —— 口試前想加回來要真的加得回來。

    這條同時守住:上面那條測試不是因為 draw_section_mark 壞了才過的。"""
    from dataclasses import replace

    from src.drafting.annotations import CUT_TEXT_H

    doc, layers = doc_and_layers
    msp = doc.modelspace()
    before = _cut_mark_lines(msp, None)
    draw_floor_plan(msp, replace(demo_spec(), section_mark="A"), layers)

    marks = [e for e in msp.query("TEXT")
             if e.dxf.text.strip() == "A" and abs(e.dxf.height - CUT_TEXT_H) < 1]
    assert len(marks) == 2, "剖切線兩端各要一個代號"
    assert _cut_mark_lines(msp, None) - before >= 7, "剖切線 1 + 箭頭 6 條"
