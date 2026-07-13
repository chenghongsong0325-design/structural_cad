"""房間(Room)的單元測試。

驗證重點:
  1. 面積:鞋帶公式(順/逆時針皆可、L 形也對)、m² 與坪的換算。
  2. 形心:矩形形心在正中央;標註文字放形心。
  3. from_walls:牆亂序、反向都能串成迴路;串不起來/不封閉要報錯。
  4. draw_room_label:兩行文字(名稱+面積)、掛對圖層、含樓層前綴。
"""
from __future__ import annotations

import pytest

from src.drafting.room import (
    M2_PER_PING,
    Room,
    draw_room_label,
    room_label_lines,
)
from src.drafting.wall import Wall
from src.standards.loader import apply_standard, load_standard, new_document

# 6m × 8m 矩形(mm),面積 48 m²。
RECT = [(0, 0), (6000, 0), (6000, 8000), (0, 8000)]


# ---------------------------------------------------------------------------
# 1) 面積與換算
# ---------------------------------------------------------------------------
def test_rectangle_area_m2() -> None:
    assert Room("客廳", RECT).area_m2 == pytest.approx(48.0)


def test_area_independent_of_winding_order() -> None:
    """角點順時針給也要算出一樣的面積(鞋帶公式取絕對值)。"""
    cw = list(reversed(RECT))
    assert Room("客廳", cw).area_m2 == pytest.approx(48.0)


def test_l_shape_area() -> None:
    """L 形:6×8 矩形缺右上角 2×3 → 48 - 6 = 42 m²。"""
    l_shape = [(0, 0), (6000, 0), (6000, 5000), (4000, 5000), (4000, 8000), (0, 8000)]
    assert Room("L形房", l_shape).area_m2 == pytest.approx(42.0)


def test_area_ping_conversion() -> None:
    room = Room("客廳", RECT)
    assert room.area_ping == pytest.approx(48.0 / M2_PER_PING)
    assert room.area_ping == pytest.approx(14.52, abs=0.01)


def test_too_few_points_raises() -> None:
    with pytest.raises(ValueError):
        Room("壞房間", [(0, 0), (1000, 0)])


# ---------------------------------------------------------------------------
# 2) 形心
# ---------------------------------------------------------------------------
def test_rectangle_centroid_is_center() -> None:
    assert Room("客廳", RECT).centroid == pytest.approx((3000, 4000))


def test_centroid_independent_of_winding_order() -> None:
    assert Room("客廳", list(reversed(RECT))).centroid == pytest.approx((3000, 4000))


# ---------------------------------------------------------------------------
# 3) from_walls
# ---------------------------------------------------------------------------
def _square_walls() -> list[Wall]:
    """4 道牆圍成 4m × 4m 正方形(中心線)。"""
    return [
        Wall(start=(0, 0), end=(4000, 0)),
        Wall(start=(4000, 0), end=(4000, 4000)),
        Wall(start=(4000, 4000), end=(0, 4000)),
        Wall(start=(0, 4000), end=(0, 0)),
    ]


def test_from_walls_square() -> None:
    room = Room.from_walls("臥室", _square_walls())
    assert room.area_m2 == pytest.approx(16.0)
    assert room.centroid == pytest.approx((2000, 2000))


def test_from_walls_unordered_and_reversed() -> None:
    """牆亂序、方向顛倒,也要能串成迴路。"""
    w = _square_walls()
    shuffled = [w[2], Wall(start=w[0].end, end=w[0].start), w[3], w[1]]
    room = Room.from_walls("臥室", shuffled)
    assert room.area_m2 == pytest.approx(16.0)


def test_from_walls_not_closed_raises() -> None:
    """只有三道牆、缺一邊 → 不封閉,要報錯。"""
    w = _square_walls()[:3]
    with pytest.raises(ValueError):
        Room.from_walls("缺牆", w)


def test_from_walls_disconnected_raises() -> None:
    """有一道牆接不上其他牆 → 報錯。"""
    w = _square_walls()
    w[1] = Wall(start=(9000, 9000), end=(9000, 12000))  # 換成一道孤兒牆
    with pytest.raises(ValueError):
        Room.from_walls("斷牆", w)


# ---------------------------------------------------------------------------
# 4) 標註
# ---------------------------------------------------------------------------
def test_room_label_lines() -> None:
    name, area = room_label_lines(Room("客廳", RECT))
    assert name == "客廳"
    assert area == "48.0㎡"


@pytest.fixture()
def doc_and_layers():
    standard = load_standard()
    doc = new_document()
    layers = apply_standard(doc, standard)
    return doc, layers


def test_a_text_layer_exists_from_standard(doc_and_layers) -> None:
    """default.yaml 應已補上 A-TEXT 圖層。"""
    _, layers = doc_and_layers
    assert "A-TEXT" in layers


def test_draw_room_label_two_texts_on_layer(doc_and_layers) -> None:
    doc, layers = doc_and_layers
    msp = doc.modelspace()

    room = Room("客廳", RECT)
    draw_room_label(msp, room, layers["A-TEXT"])

    texts = list(msp.query("TEXT"))
    assert len(texts) == 2
    contents = {t.dxf.text for t in texts}
    assert contents == {"客廳", "48.0㎡"}
    for t in texts:
        assert t.dxf.layer == layers["A-TEXT"]
        # 兩行文字都對齊在形心的鉛直線上(x = 3000)。
        _, point, _ = t.get_placement()
        assert point.x == pytest.approx(3000)


def test_draw_room_label_with_prefix() -> None:
    standard = load_standard()
    doc = new_document()
    layers = apply_standard(doc, standard, prefix="2F建築底圖")
    msp = doc.modelspace()

    draw_room_label(msp, Room("廁所", [(0, 0), (2000, 0), (2000, 1500), (0, 1500)]), layers["A-TEXT"])

    # A-TEXT 經別名對應到規範圖層 TEXT。
    for t in msp.query("TEXT"):
        assert t.dxf.layer == "2F建築底圖$0$TEXT"
