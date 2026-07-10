"""門(Door)與窗(Window)的單元測試。

驗證重點:
  1. 圖塊定義:門(單線門扇 + 90°弧)、窗(n 條平行線),且冪等。
  2. 門的四種開啟方向(左/右鉸鏈 × 內/外開):門扇端點、弧半徑、圖層正確對齊洞口。
  3. 窗:平行線數、跨牆厚、對齊洞口、圖層。
  4. 樓層前綴、參數錯誤處理。

門扇線在世界座標(WCS)可靠,故以它驗證幾何;鏡射弧線的 dxf.center 受 OCS 影響
不直接比對,改比對弧半徑。
"""
from __future__ import annotations

import pytest

from src.drafting.door_window import (
    DOOR_BLOCK,
    Door,
    Window,
    create_door_block,
    create_window_block,
    window_block_name,
)
from src.drafting.wall import Opening, Wall
from src.standards.loader import apply_standard, load_standard, new_document


@pytest.fixture()
def doc_and_layers():
    standard = load_standard()
    doc = new_document()
    layers = apply_standard(doc, standard)
    return doc, layers


# 水平牆 + 洞口中心 2000、寬 900 → 門樘在 d0=1550(左)、d1=2450(右)。
def _wall_with_opening(width: float = 900, thickness: float = 240):
    op = Opening(position=2000, width=width)
    wall = Wall(start=(0, 0), end=(4000, 0), thickness=thickness, openings=[op])
    return wall, op


def _round_xy(v):
    return (round(v.x), round(v.y))


def _leaf_and_arc(blockref):
    ents = list(blockref.virtual_entities())
    leaf = next(e for e in ents if e.dxftype() == "LINE")
    arc = next(e for e in ents if e.dxftype() == "ARC")
    return leaf, arc


# ---------------------------------------------------------------------------
# 1) 圖塊定義
# ---------------------------------------------------------------------------
def test_door_block_created_with_line_and_arc(doc_and_layers) -> None:
    doc, _ = doc_and_layers
    create_door_block(doc)
    assert DOOR_BLOCK in doc.blocks
    blk = doc.blocks.get(DOOR_BLOCK)
    types = sorted(e.dxftype() for e in blk)
    assert types == ["ARC", "LINE"]


def test_door_block_idempotent(doc_and_layers) -> None:
    doc, _ = doc_and_layers
    create_door_block(doc)
    create_door_block(doc)  # 第二次不應報錯或重複
    assert sum(1 for _ in doc.blocks) >= 1
    assert DOOR_BLOCK in doc.blocks


def test_window_block_has_n_lines(doc_and_layers) -> None:
    doc, _ = doc_and_layers
    create_window_block(doc, lines=3)
    blk = doc.blocks.get(window_block_name(3))
    lines = [e for e in blk if e.dxftype() == "LINE"]
    assert len(lines) == 3


def test_window_block_too_few_lines_raises(doc_and_layers) -> None:
    doc, _ = doc_and_layers
    with pytest.raises(ValueError):
        create_window_block(doc, lines=1)


def test_door_window_layers_exist_from_standard(doc_and_layers) -> None:
    _, layers = doc_and_layers
    assert "A-DOOR" in layers
    assert "A-GLAZ" in layers


# ---------------------------------------------------------------------------
# 2) 門:四種開啟方向
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "hinge, swing, expect_hinge, expect_tip",
    [
        ("left", "out", (1550, 0), (1550, 900)),
        ("left", "in", (1550, 0), (1550, -900)),
        ("right", "out", (2450, 0), (2450, 900)),
        ("right", "in", (2450, 0), (2450, -900)),
    ],
)
def test_door_orientations(doc_and_layers, hinge, swing, expect_hinge, expect_tip) -> None:
    doc, layers = doc_and_layers
    msp = doc.modelspace()
    wall, op = _wall_with_opening()

    ref = Door(hinge=hinge, swing=swing).place_in_wall(msp, wall, op, layers)

    assert ref.dxf.layer == layers["A-DOOR"]
    leaf, arc = _leaf_and_arc(ref)
    # 門扇線:起點=鉸鏈(門樘)、終點=開啟後的門扇尖端。
    assert _round_xy(leaf.dxf.start) == expect_hinge
    assert _round_xy(leaf.dxf.end) == expect_tip
    # 開啟弧半徑 = 洞口寬。
    assert arc.dxf.radius == pytest.approx(900)


def test_door_width_override(doc_and_layers) -> None:
    """Door.width 指定時覆寫洞口寬(門扇長度 = 指定寬)。"""
    doc, layers = doc_and_layers
    msp = doc.modelspace()
    wall, op = _wall_with_opening(width=900)

    ref = Door(hinge="left", swing="out", width=800).place_in_wall(msp, wall, op, layers)
    _, arc = _leaf_and_arc(ref)
    assert arc.dxf.radius == pytest.approx(800)


def test_door_invalid_hinge_raises(doc_and_layers) -> None:
    doc, layers = doc_and_layers
    wall, op = _wall_with_opening()
    with pytest.raises(ValueError):
        Door(hinge="middle").place_in_wall(doc.modelspace(), wall, op, layers)


def test_door_invalid_swing_raises(doc_and_layers) -> None:
    doc, layers = doc_and_layers
    wall, op = _wall_with_opening()
    with pytest.raises(ValueError):
        Door(swing="sideways").place_in_wall(doc.modelspace(), wall, op, layers)


def test_door_on_vertical_wall(doc_and_layers) -> None:
    """垂直牆:門也要正確對齊(用向量運算,不假設水平)。"""
    doc, layers = doc_and_layers
    msp = doc.modelspace()
    op = Opening(position=2000, width=900)
    wall = Wall(start=(0, 0), end=(0, 4000), thickness=240, openings=[op])

    ref = Door(hinge="left", swing="out").place_in_wall(msp, wall, op, layers)
    leaf, arc = _leaf_and_arc(ref)
    # 牆沿 +Y,左鉸鏈在 d0=1550 → 鉸鏈點 (0,1550)。swing out(+n)= -X 側。
    assert _round_xy(leaf.dxf.start) == (0, 1550)
    assert arc.dxf.radius == pytest.approx(900)


# ---------------------------------------------------------------------------
# 3) 窗
# ---------------------------------------------------------------------------
def test_window_places_three_lines_across_opening(doc_and_layers) -> None:
    doc, layers = doc_and_layers
    msp = doc.modelspace()
    op = Opening(position=2000, width=1500)
    wall = Wall(start=(0, 0), end=(4000, 0), thickness=240, openings=[op])

    ref = Window(lines=3).place_in_wall(msp, wall, op, layers)
    assert ref.dxf.layer == layers["A-GLAZ"]

    lines = [e for e in ref.virtual_entities() if e.dxftype() == "LINE"]
    assert len(lines) == 3
    # 每條線都橫跨洞口 x 1250..2750。
    for ln in lines:
        xs = sorted([round(ln.dxf.start.x), round(ln.dxf.end.x)])
        assert xs == [1250, 2750]
    # 三條線落在牆兩面與中線 y = -120, 0, +120。
    ys = sorted(round(ln.dxf.start.y) for ln in lines)
    assert ys == [-120, 0, 120]


def test_window_double_line(doc_and_layers) -> None:
    doc, layers = doc_and_layers
    msp = doc.modelspace()
    op = Opening(position=2000, width=1500)
    wall = Wall(start=(0, 0), end=(4000, 0), thickness=240, openings=[op])

    ref = Window(lines=2).place_in_wall(msp, wall, op, layers)
    lines = [e for e in ref.virtual_entities() if e.dxftype() == "LINE"]
    assert len(lines) == 2


# ---------------------------------------------------------------------------
# 4) 樓層前綴
# ---------------------------------------------------------------------------
def test_door_window_with_prefix() -> None:
    standard = load_standard()
    doc = new_document()
    layers = apply_standard(doc, standard, prefix="2F建築底圖")
    msp = doc.modelspace()

    op_d = Opening(position=2000, width=900)
    op_w = Opening(position=3000, width=1200)
    wall = Wall(start=(0, 0), end=(5000, 0), thickness=240, openings=[op_d, op_w])

    door = Door().place_in_wall(msp, wall, op_d, layers)
    window = Window().place_in_wall(msp, wall, op_w, layers)
    # A-DOOR / A-GLAZ 都經別名對應到規範圖層 DW(開口門窗)。
    assert door.dxf.layer == "2F建築底圖$0$DW"
    assert window.dxf.layer == "2F建築底圖$0$DW"
