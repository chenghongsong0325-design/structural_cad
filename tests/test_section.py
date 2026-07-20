"""剖面圖 / 立面圖(section)單元測試(D3,多樓層)。

驗:draw_section/draw_elevation 能對 BuildingSpec 出圖不報錯、畫出正確
圖層的實體、樓層數對應樓板數、柱列從基礎貫通到屋頂、立面只畫地上層、
輔助函式(標高文字/軸線/地盤標高)正確。
"""
import pytest

from src.drafting.section import (
    FOOTING_HEIGHT,
    FOUNDATION_DEPTH,
    SLAB_THICKNESS,
    _axis_grid,
    _elev_text,
    _ground_elevation,
    draw_elevation,
    draw_section,
)
from src.design.building_generator import BuildingBrief, generate_building
from src.design.layout_generator import CorridorBrief, HouseBrief
from src.standards.loader import apply_standard, load_standard, new_document

HOUSE = dict(site_width=19000, site_depth=13000, bedrooms=3)


def _doc_layers():
    doc = new_document()
    return doc, apply_standard(doc, load_standard())


def _corridor(basements=1, floors=3):
    return generate_building(BuildingBrief(
        typical=CorridorBrief(units_per_row=6), floors=floors, basements=basements))


def _house(basements=1, floors=3):
    return generate_building(BuildingBrief(
        typical=HouseBrief(**HOUSE), floors=floors, basements=basements,
        differentiated=True))


# ── 輔助函式 ──────────────────────────────────────────────────────────────
def test_elev_text_formats():
    assert _elev_text(0) == "±0.00"
    assert _elev_text(3200) == "+3.20"
    assert _elev_text(-3200) == "-3.20"


def test_axis_grid_returns_column_lines():
    b = _corridor(basements=0, floors=2)
    spec = b.floors[-1].spec
    lo, hi, axes = _axis_grid(spec, "x")
    assert lo == spec.grid_origin[0]
    assert abs(hi - (lo + sum(spec.x_spacings))) < 1e-6
    assert len(axes) == len(spec.x_spacings) + 1     # 軸線數 = 跨數+1
    assert axes[0] == lo and abs(axes[-1] - hi) < 1e-6


def test_ground_elevation_is_first_floor():
    b = _corridor(basements=1, floors=3)
    assert _ground_elevation(b) == 0.0               # 1FL = ±0.00


# ── 剖面圖 ────────────────────────────────────────────────────────────────
def test_section_draws_without_error():
    doc, layers = _doc_layers()
    draw_section(doc.modelspace(), _corridor(), layers, axis="x")
    assert len(list(doc.modelspace())) > 0


def test_section_slab_count_matches_floors_plus_roof():
    """樓板數 = 樓層數 + 屋頂板(滿跨水平帶,寬=建築寬)。"""
    b = _house(basements=1, floors=3)   # B1F,1F,2F,3F = 4 層
    doc, layers = _doc_layers()
    draw_section(doc.modelspace(), b, layers, axis="x")
    wall_layer = layers["WALL"]
    lo, hi, _ = _axis_grid(b.floors[-1].spec, "x")
    width = hi - lo
    slabs = 0
    for e in doc.modelspace().query("LWPOLYLINE"):
        if e.dxf.layer != wall_layer:
            continue
        pts = [(p[0], p[1]) for p in e.get_points()]
        w = max(p[0] for p in pts) - min(p[0] for p in pts)
        h = max(p[1] for p in pts) - min(p[1] for p in pts)
        if abs(w - width) < 1.0 and abs(h - SLAB_THICKNESS) < 1.0:
            slabs += 1
    assert slabs == len(b.floors) + 1


def test_section_columns_span_foundation_to_roof():
    """柱從基腳頂貫通到屋頂:應存在覆蓋整個高度範圍的細長柱矩形。"""
    b = _house(basements=1, floors=3)
    doc, layers = _doc_layers()
    draw_section(doc.modelspace(), b, layers, axis="x")
    col_layer = layers["COL"]
    lv = sorted(b.floors, key=lambda f: f.elevation)
    roof = lv[-1].elevation + b.floor_height
    col_top_of_footing = lv[0].elevation - FOUNDATION_DEPTH + FOOTING_HEIGHT
    full = []
    for e in doc.modelspace().query("LWPOLYLINE"):
        if e.dxf.layer != col_layer:
            continue
        ys = [p[1] for p in e.get_points()]
        if abs(min(ys) - col_top_of_footing) < 1.0 and abs(max(ys) - roof) < 1.0:
            full.append(e)
    # 至少每條軸線一根貫通柱。
    _, _, axes = _axis_grid(lv[-1].spec, "x")
    assert len(full) >= len(axes)


def test_section_includes_basement_below_ground():
    """剖面含地下層:應有實體落在地盤線(GL=0)以下。"""
    b = _corridor(basements=1, floors=2)
    doc, layers = _doc_layers()
    draw_section(doc.modelspace(), b, layers, axis="x")
    miny = min(min(p[1] for p in e.get_points())
               for e in doc.modelspace().query("LWPOLYLINE"))
    assert miny < 0


def test_section_has_height_dims():
    b = _corridor(basements=0, floors=3)
    doc, layers = _doc_layers()
    draw_section(doc.modelspace(), b, layers, axis="x")
    # 3 層 → 樓板間 3 道(1-2/2-3/3-屋頂)+ 全高 1 道 = 至少 4 道。
    assert len(doc.modelspace().query("DIMENSION")) >= 4


def test_section_axis_y_ok():
    doc, layers = _doc_layers()
    draw_section(doc.modelspace(), _house(), layers, axis="y")
    assert len(list(doc.modelspace())) > 0


def test_section_bad_axis_rejected():
    doc, layers = _doc_layers()
    with pytest.raises(ValueError):
        draw_section(doc.modelspace(), _corridor(), layers, axis="z")


# ── 立面圖 ────────────────────────────────────────────────────────────────
def test_elevation_draws_without_error():
    doc, layers = _doc_layers()
    draw_elevation(doc.modelspace(), _corridor(), layers, side="south")
    assert len(list(doc.modelspace())) > 0


def test_elevation_excludes_basement():
    """立面在地面下不畫:最低實體不應低於 GL。"""
    b = _house(basements=1, floors=3)
    doc, layers = _doc_layers()
    draw_elevation(doc.modelspace(), b, layers, side="south")
    miny = min(min(p[1] for p in e.get_points())
               for e in doc.modelspace().query("LWPOLYLINE"))
    assert miny >= -1.0                              # 不含地下層


def test_elevation_draws_openings_on_dw_layer():
    """南面外牆的門窗會投影成立面開口(DW 圖層矩形)。"""
    b = _house(basements=1, floors=3)
    doc, layers = _doc_layers()
    draw_elevation(doc.modelspace(), b, layers, side="south")
    dw = layers["DW"]
    n = sum(1 for e in doc.modelspace().query("LWPOLYLINE") if e.dxf.layer == dw)
    assert n > 0


def test_elevation_bad_side_rejected():
    doc, layers = _doc_layers()
    with pytest.raises(ValueError):
        draw_elevation(doc.modelspace(), _corridor(), layers, side="up")


def test_section_draws_stair_flights():
    """剖面有樓梯(E4):相鄰兩層間各一段階梯折線(HANDRAIL 層)。

    透天 B1F~3F 四層 → 3 段梯(B1F→1F、1F→2F、2F→3F);每段是多點折線
    (踏步鋸齒 > 4 點),另有一條梯板底斜線(LINE)。
    """
    b = _house(basements=1, floors=3)
    doc, layers = _doc_layers()
    draw_section(doc.modelspace(), b, layers, axis="x")
    rail = layers["HANDRAIL"]
    flights = [e for e in doc.modelspace().query("LWPOLYLINE")
               if e.dxf.layer == rail and len(e) > 4]
    soffits = [e for e in doc.modelspace().query("LINE")
               if e.dxf.layer == rail]
    assert len(flights) == 3
    assert len(soffits) == 3
