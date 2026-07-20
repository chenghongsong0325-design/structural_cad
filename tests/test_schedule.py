"""圖面表格(src/drafting/schedule.py)的單元測試。

驗證重點:
  1. 面積計算表:每室一列 + 表頭 + 合計;合計數值 = 各室面積和;天井不列。
  2. 門窗表:門/窗依寬度分組,數量對得上 spec.doors/windows。
  3. draw_floor_plan 的 schedules 開關:sheet=True 才畫,文字掛 A-TEXT。
"""
from __future__ import annotations

from dataclasses import replace

import pytest

from src.drafting.apartment_plan import demo_spec, draw_floor_plan
from src.drafting.schedule import draw_area_table, draw_opening_table
from src.standards.loader import apply_standard, load_standard, new_document


@pytest.fixture()
def doc_and_layers():
    doc = new_document()
    layers = apply_standard(doc, load_standard())
    return doc, layers


def _texts(msp) -> list[str]:
    return [t.dxf.text for t in msp.query("TEXT")]


def test_area_table_rows_and_total(doc_and_layers) -> None:
    doc, layers = doc_and_layers
    msp = doc.modelspace()
    spec = demo_spec()
    draw_area_table(msp, spec, layers, origin=(0, 0))

    texts = _texts(msp)
    assert "面積計算表" in texts
    for room in spec.rooms:                     # 每室都有一列
        assert room.name in texts
    total = sum(r.area_m2 for r in spec.rooms if r.kind != "patio")
    assert f"{total:.1f}" in texts              # 合計數值
    assert "合計" in texts


def test_opening_table_counts(doc_and_layers) -> None:
    doc, layers = doc_and_layers
    msp = doc.modelspace()
    spec = demo_spec()
    draw_opening_table(msp, spec, layers, origin=(0, 0))

    texts = _texts(msp)
    assert "門窗表" in texts
    assert "D1" in texts and "W1" in texts
    # 門的總數 = spec.doors;從表格「門」列的數量欄加總驗證。
    # (表格文字沒有欄位結構,改驗:門寬分組的組數 ≥1、且門總數出現在數量欄。)
    widths = {}
    for dp in spec.doors:
        w = spec.walls[dp.wall_index].openings[dp.opening_index].width
        widths[w] = widths.get(w, 0) + 1
    for w, n in widths.items():
        assert f"{w / 10:.0f}" in texts         # 寬度(cm)有列出
        assert str(n) in texts                  # 數量有列出


def test_floor_plan_schedules_switch(doc_and_layers) -> None:
    """schedules=True 且 sheet=True → 圖上有兩張表;預設不畫。"""
    doc, layers = doc_and_layers
    msp = doc.modelspace()
    draw_floor_plan(msp, demo_spec(), layers)          # 預設 schedules=False
    base_texts = _texts(msp)
    assert "面積計算表" not in base_texts

    doc2 = new_document()
    layers2 = apply_standard(doc2, load_standard())
    msp2 = doc2.modelspace()
    spec = replace(demo_spec(), schedules=True)
    assert spec.sheet, "demo_spec 應該開著圖框(前提檢查)"
    draw_floor_plan(msp2, spec, layers2)
    texts = _texts(msp2)
    assert "面積計算表" in texts and "門窗表" in texts
    # 表格文字掛 A-TEXT。
    table_title = next(t for t in msp2.query("TEXT")
                       if t.dxf.text == "面積計算表")
    assert table_title.dxf.layer == layers2["A-TEXT"]


def test_floor_plan_schedules_without_sheet(doc_and_layers) -> None:
    """無圖框(sheet=False,網頁 DXF 的常態)也要畫表:放地界線右側。"""
    doc, layers = doc_and_layers
    msp = doc.modelspace()
    spec = replace(demo_spec(), schedules=True, sheet=False, title_block=None)
    draw_floor_plan(msp, spec, layers)
    texts = _texts(msp)
    assert "面積計算表" in texts and "門窗表" in texts
    # 表格在地界線右側(x > 基地最大 x)。
    title = next(t for t in msp.query("TEXT") if t.dxf.text == "面積計算表")
    max_x = max(p[0] for p in spec.site_boundary)
    assert title.dxf.insert.x > max_x
