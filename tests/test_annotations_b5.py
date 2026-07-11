"""B5 三項功能的單元測試:房型帶框標籤、牆體剖面線、樓層標示/北向箭頭。"""
from __future__ import annotations

import pytest

from src.drafting.annotations import (
    NORTH_ARROW_BLOCK,
    create_north_arrow_block,
    draw_floor_label,
    place_north_arrow,
)
from src.drafting.room import Room, draw_room_tag
from src.drafting.wall import Wall
from src.drafting.wall_join import draw_wall_hatch
from src.standards.loader import apply_standard, load_standard, new_document

RECT = [(0, 0), (6000, 0), (6000, 8000), (0, 8000)]


@pytest.fixture()
def doc_and_layers():
    standard = load_standard()
    doc = new_document()
    layers = apply_standard(doc, standard)
    return doc, layers


# ---------------------------------------------------------------------------
# 1) 房型帶框標籤
# ---------------------------------------------------------------------------
def test_room_tag_frame_and_text(doc_and_layers) -> None:
    doc, layers = doc_and_layers
    msp = doc.modelspace()
    room = Room("臥室", RECT, kind="bedroom", code="X05")
    draw_room_tag(msp, room, layers["A-TEXT"])

    polys = list(msp.query("LWPOLYLINE"))
    texts = list(msp.query("TEXT"))
    assert len(polys) == 1 and polys[0].closed
    assert len(texts) == 1
    assert texts[0].dxf.text == "X05 臥室"
    assert polys[0].dxf.layer == layers["A-TEXT"]   # 別名 → TEXT
    assert texts[0].dxf.layer == layers["A-TEXT"]

    # 預設位置:形心上方 900;框要把文字點包住。
    _, tp, _ = texts[0].get_placement()
    assert (tp.x, tp.y) == pytest.approx((3000, 4900))
    xs = [p[0] for p in polys[0].get_points()]
    ys = [p[1] for p in polys[0].get_points()]
    assert min(xs) < 3000 < max(xs)
    assert min(ys) < 4900 < max(ys)


def test_room_without_code_draws_nothing(doc_and_layers) -> None:
    doc, layers = doc_and_layers
    msp = doc.modelspace()
    draw_room_tag(msp, Room("臥室", RECT), layers["A-TEXT"])   # code=""
    assert len(list(msp)) == 0


# ---------------------------------------------------------------------------
# 2) 牆體剖面線
# ---------------------------------------------------------------------------
def test_wall_hatch_pattern_and_layer(doc_and_layers) -> None:
    doc, layers = doc_and_layers
    msp = doc.modelspace()
    n = draw_wall_hatch(msp, [Wall((0, 0), (4000, 0), thickness=150)],
                        layers["A-WALL"], pattern="ANSI31", scale=30)
    assert n == 1
    hatch = list(msp.query("HATCH"))[0]
    assert hatch.dxf.pattern_name == "ANSI31"
    assert hatch.dxf.layer == layers["A-WALL"]      # 別名 → WALL
    assert len(hatch.paths) == 1                     # 單牆:只有外環


def test_wall_hatch_ring_has_hole_path(doc_and_layers) -> None:
    """封閉一圈牆 → HATCH 有外環 + 內孔兩條邊界路徑(內孔不填)。"""
    doc, layers = doc_and_layers
    msp = doc.modelspace()
    t = 150
    walls = [Wall((0, 0), (4000, 0), t), Wall((4000, 0), (4000, 4000), t),
             Wall((4000, 4000), (0, 4000), t), Wall((0, 4000), (0, 0), t)]
    n = draw_wall_hatch(msp, walls, layers["A-WALL"])
    assert n == 1
    assert len(list(msp.query("HATCH"))[0].paths) == 2


def test_wall_hatch_subtract_columns_splits(doc_and_layers) -> None:
    """柱把牆切斷 → footprint 變多塊 → 多個 HATCH。"""
    doc, layers = doc_and_layers
    msp = doc.modelspace()
    col = [(1800, -250), (2300, -250), (2300, 250), (1800, 250)]
    n = draw_wall_hatch(msp, [Wall((0, 0), (4000, 0), thickness=150)],
                        layers["A-WALL"], subtract=[col])
    assert n == 2


# ---------------------------------------------------------------------------
# 3) 樓層標示 + 北向箭頭
# ---------------------------------------------------------------------------
def test_floor_label(doc_and_layers) -> None:
    doc, layers = doc_and_layers
    msp = doc.modelspace()
    draw_floor_label(msp, "3F", (1000, 2000), layers)
    t = list(msp.query("TEXT"))[0]
    assert t.dxf.text == "3F"
    assert t.dxf.layer == layers["TEXT"]
    assert t.dxf.height == 1500


def test_north_arrow_block_and_insert(doc_and_layers) -> None:
    doc, layers = doc_and_layers
    msp = doc.modelspace()
    ref = place_north_arrow(msp, (5000, 5000), layers, rotation=15)

    assert ref.dxf.name == NORTH_ARROW_BLOCK
    assert ref.dxf.layer == layers["OTHER"]
    assert ref.dxf.rotation == pytest.approx(15)
    blk = doc.blocks.get(NORTH_ARROW_BLOCK)
    types = sorted(e.dxftype() for e in blk)
    assert types == ["CIRCLE", "LWPOLYLINE", "TEXT"]   # 圓 + 三角 + N


def test_north_arrow_block_idempotent(doc_and_layers) -> None:
    doc, _ = doc_and_layers
    create_north_arrow_block(doc)
    create_north_arrow_block(doc)
    assert NORTH_ARROW_BLOCK in doc.blocks


# ---------------------------------------------------------------------------
# 4) 生產線整合
# ---------------------------------------------------------------------------
def test_floor_plan_b5_integration(doc_and_layers) -> None:
    """demo 預設:房型標籤/樓層標示/北向箭頭都有;牆體填充預設關閉(貼近
    1:100 施工/銷售平面圖:空心雙線)。"""
    from src.drafting.apartment_plan import demo_spec, draw_floor_plan

    doc, layers = doc_and_layers
    msp = doc.modelspace()
    draw_floor_plan(msp, demo_spec(), layers)

    assert len(list(msp.query("HATCH"))) == 0   # 預設不填牆
    texts = {t.dxf.text for t in msp.query("TEXT")}
    assert "2F" in texts                       # 樓層標示
    assert "X05 主臥室" in texts               # 房型帶框標籤
    inserts = {i.dxf.name for i in msp.query("INSERT")}
    assert NORTH_ARROW_BLOCK in inserts        # 北向箭頭


def test_floor_plan_wall_hatch_opt_in(doc_and_layers) -> None:
    """wall_hatch=True 時牆體填 ANSI31(RC)/ANSI37(磚)剖面線。"""
    from src.drafting.apartment_plan import demo_spec, draw_floor_plan

    doc, layers = doc_and_layers
    msp = doc.modelspace()
    spec = demo_spec()
    spec.wall_hatch = True
    draw_floor_plan(msp, spec, layers)

    patterns = {h.dxf.pattern_name for h in msp.query("HATCH")}
    assert patterns == {"ANSI31", "ANSI37"}
