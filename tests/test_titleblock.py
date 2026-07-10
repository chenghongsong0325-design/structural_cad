"""標題欄 / 圖框圖塊的單元測試。

驗證重點:
  1. 圖塊定義有正確建立,且六個欄位都有對應的屬性定義(ATTDEF)。
  2. 插入後產生的 blockref 掛在正確圖層,且六個屬性都填入正確的值。
  3. 樓層前綴也能正確套用(框線/文字掛到帶前綴的圖層)。
  4. 重複建立同名圖塊不會出錯(冪等)。
"""
from __future__ import annotations

import pytest

from src.drafting.titleblock import (
    A3_HEIGHT,
    A3_WIDTH,
    BLOCK_NAME,
    COMP_BLOCK_NAME,
    CompetitionTitleData,
    TitleBlockData,
    competition_title_size,
    create_title_block_definition,
    draw_sheet_border,
    insert_competition_title_block,
    insert_title_block,
)
from src.standards.loader import apply_standard, load_standard, new_document


@pytest.fixture()
def doc_and_layers():
    standard = load_standard()
    doc = new_document()
    layers = apply_standard(doc, standard)
    return doc, layers


@pytest.fixture()
def sample_data() -> TitleBlockData:
    return TitleBlockData(
        drawing_name="二層結構平面圖",
        drawing_number="S-02",
        scale="1:100",
        date="2026-07-09",
        drawn_by="成弘",
        checked_by="—",
    )


# ---------------------------------------------------------------------------
# 1) 圖塊定義
# ---------------------------------------------------------------------------
def test_create_definition_registers_block(doc_and_layers) -> None:
    doc, layers = doc_and_layers
    create_title_block_definition(doc, layers)
    assert BLOCK_NAME in doc.blocks


def test_definition_has_all_six_attdefs(doc_and_layers) -> None:
    doc, layers = doc_and_layers
    blk = create_title_block_definition(doc, layers)

    tags = {e.dxf.tag for e in blk if e.dxftype() == "ATTDEF"}
    assert tags == {"DWG_NAME", "DWG_NO", "SCALE", "DATE", "DRAWN", "CHECKED"}


def test_create_definition_is_idempotent(doc_and_layers) -> None:
    """重複呼叫不應該重建、也不應報錯(第二次直接回傳既有圖塊)。"""
    doc, layers = doc_and_layers
    blk1 = create_title_block_definition(doc, layers)
    blk2 = create_title_block_definition(doc, layers)
    assert blk1 is blk2


# ---------------------------------------------------------------------------
# 2) 插入 + 填值
# ---------------------------------------------------------------------------
def test_insert_creates_blockref_on_thin_layer(doc_and_layers, sample_data) -> None:
    doc, layers = doc_and_layers
    msp = doc.modelspace()

    insert_title_block(msp, sample_data, layers, insert=(0, 0))

    inserts = list(msp.query("INSERT"))
    assert len(inserts) == 1
    assert inserts[0].dxf.name == BLOCK_NAME
    assert inserts[0].dxf.layer == layers["S-THIN"]


def test_insert_fills_all_attribute_values(doc_and_layers, sample_data) -> None:
    doc, layers = doc_and_layers
    msp = doc.modelspace()

    ref = insert_title_block(msp, sample_data, layers, insert=(0, 0))

    values = {att.dxf.tag: att.dxf.text for att in ref.attribs}
    assert values == {
        "DWG_NAME": "二層結構平面圖",
        "DWG_NO": "S-02",
        "SCALE": "1:100",
        "DATE": "2026-07-09",
        "DRAWN": "成弘",
        "CHECKED": "—",
    }


def test_insert_auto_creates_definition_if_missing(doc_and_layers, sample_data) -> None:
    """沒有先手動建定義,insert 也要能自己補建。"""
    doc, layers = doc_and_layers
    msp = doc.modelspace()

    assert BLOCK_NAME not in doc.blocks
    insert_title_block(msp, sample_data, layers, insert=(0, 0))
    assert BLOCK_NAME in doc.blocks


# ---------------------------------------------------------------------------
# 3) 樓層前綴
# ---------------------------------------------------------------------------
def test_insert_with_floor_prefix(sample_data) -> None:
    standard = load_standard()
    doc = new_document()
    layers = apply_standard(doc, standard, prefix="2F建築底圖")
    msp = doc.modelspace()

    ref = insert_title_block(msp, sample_data, layers, insert=(0, 0))
    # S-THIN→OTHER、S-TEXTB→TEXT(經別名對應到規範圖層)。
    assert ref.dxf.layer == "2F建築底圖$0$OTHER"
    for att in ref.attribs:
        assert att.dxf.layer == "2F建築底圖$0$TEXT"


# ---------------------------------------------------------------------------
# 競賽格式:圖紙外框 + 競賽標題欄
# ---------------------------------------------------------------------------
def test_draw_sheet_border_two_rectangles(doc_and_layers) -> None:
    doc, layers = doc_and_layers
    msp = doc.modelspace()

    outer, inner = draw_sheet_border(msp, layers["OTHER"])
    polys = list(msp.query("LWPOLYLINE"))
    assert len(polys) == 2                       # 外框 + 內框
    for p in polys:
        assert p.dxf.layer == layers["OTHER"]
    # A3 橫式 1:100 → 42000×29700。
    assert outer[2] == (A3_WIDTH, A3_HEIGHT)
    # 內框在外框之內。
    assert inner[0][0] > outer[0][0] and inner[2][0] < outer[2][0]


def test_competition_title_size() -> None:
    w, h = competition_title_size()
    assert (w, h) == (18000, 4000)


def test_competition_block_has_all_attdefs(doc_and_layers) -> None:
    doc, layers = doc_and_layers
    from src.drafting.titleblock import create_competition_title_block
    blk = create_competition_title_block(doc, layers)
    tags = {e.dxf.tag for e in blk if e.dxftype() == "ATTDEF"}
    assert tags == {
        "DWG_NAME", "SCALE", "EXAM_TIME", "EXAM_DATE", "APPROVAL",
        "EXAM_NO", "QUESTION_NO", "EXAMINEE", "CATEGORY",
    }


def test_insert_competition_fills_values(doc_and_layers) -> None:
    doc, layers = doc_and_layers
    msp = doc.modelspace()

    data = CompetitionTitleData(
        drawing_name="標準層平面圖", scale="1:100", exam_time="繪圖2小時30分",
        exam_date="115年8月", approval_unit="技能檢定中心",
        exam_number="A1", question_number="21101-107-0302", examinee="成弘",
    )
    ref = insert_competition_title_block(msp, data, layers, insert=(0, 0))

    assert ref.dxf.name == COMP_BLOCK_NAME
    assert ref.dxf.layer == layers["OTHER"]
    values = {a.dxf.tag: a.dxf.text for a in ref.attribs}
    assert values["DWG_NAME"] == "標準層平面圖"
    assert values["QUESTION_NO"] == "21101-107-0302"
    assert values["EXAMINEE"] == "成弘"
    assert values["CATEGORY"]  # 類別橫幅有預設值


def test_insert_competition_with_prefix() -> None:
    standard = load_standard()
    doc = new_document()
    layers = apply_standard(doc, standard, prefix="2F建築底圖")
    msp = doc.modelspace()

    ref = insert_competition_title_block(msp, CompetitionTitleData(drawing_name="X"), layers)
    assert ref.dxf.layer == "2F建築底圖$0$OTHER"
    for att in ref.attribs:
        assert att.dxf.layer == "2F建築底圖$0$TEXT"
