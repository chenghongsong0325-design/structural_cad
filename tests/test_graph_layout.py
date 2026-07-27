"""graph_layout(混合式 AI 建築師第 2 步:LLM 關係圖 → 真圖)測試。

用**固定關係圖**(不呼叫 Gemini)驗證落實引擎:
  * 三層都生得出、畫得出 DXF、評得動分。
  * 垂直核(樓梯+管道間+天井)與結構柱**上下對齊**(符合柱網原則)。
  * 每層動線走得通(furnish 後動線修復器把擋路家具移掉)。
  * 管道間(機電豎管)非走入 → 不開門。
  * BSP 分割鋪滿、不重疊。

⚠️ 不測 propose_room_graph 的網路呼叫(那在 test_room_graph 用假 client 測)。
"""
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from shapely.geometry import Polygon

from src.design.layout.graph_layout import (
    partition_envelope,
    realize_graph_building,
)
from src.design.layout.room_circulation import _room_openings, analyze_room_circulation
from src.drafting.apartment_plan import FloorPlanSpec, draw_floor_plan
from src.web.render import _new_doc

W, D = 7000.0, 12000.0

# 一張典型窄面寬透天的關係圖(模仿 Gemini 產出:1F 公共、2F/3F 臥室)。
GRAPH = {
    "rooms": [
        {"id": "entry", "kind": "corridor", "floor": 1, "wants_daylight": False},
        {"id": "living", "kind": "living", "floor": 1, "wants_daylight": True},
        {"id": "dining", "kind": "dining", "floor": 1, "wants_daylight": True},
        {"id": "kitchen", "kind": "kitchen", "floor": 1, "wants_daylight": False},
        {"id": "toilet1", "kind": "toilet", "floor": 1, "wants_daylight": False},
        {"id": "stair1", "kind": "stair", "floor": 1, "wants_daylight": False},
        {"id": "patio1", "kind": "patio", "floor": 1, "wants_daylight": True},
        {"id": "corr2", "kind": "corridor", "floor": 2, "wants_daylight": False},
        {"id": "bedA", "kind": "bedroom", "floor": 2, "wants_daylight": True},
        {"id": "bedB", "kind": "bedroom", "floor": 2, "wants_daylight": True},
        {"id": "bath2", "kind": "bathroom", "floor": 2, "wants_daylight": False},
        {"id": "stair2", "kind": "stair", "floor": 2, "wants_daylight": False},
        {"id": "corr3", "kind": "corridor", "floor": 3, "wants_daylight": False},
        {"id": "master", "kind": "master_bedroom", "floor": 3,
         "wants_daylight": True},
        {"id": "mbath", "kind": "bathroom", "floor": 3, "wants_daylight": False},
        {"id": "study", "kind": "study", "floor": 3, "wants_daylight": True},
        {"id": "stair3", "kind": "stair", "floor": 3, "wants_daylight": False},
    ],
    "adjacencies": [
        {"a": "entry", "b": "living", "connection": "open"},
        {"a": "living", "b": "dining", "connection": "open"},
        {"a": "dining", "b": "kitchen", "connection": "open"},
        {"a": "entry", "b": "toilet1", "connection": "door"},
        {"a": "entry", "b": "stair1", "connection": "near"},
        {"a": "stair2", "b": "corr2", "connection": "open"},
        {"a": "corr2", "b": "bedA", "connection": "door"},
        {"a": "corr2", "b": "bedB", "connection": "door"},
        {"a": "corr2", "b": "bath2", "connection": "door"},
        {"a": "stair3", "b": "corr3", "connection": "open"},
        {"a": "corr3", "b": "master", "connection": "door"},
        {"a": "master", "b": "mbath", "connection": "door"},
        {"a": "corr3", "b": "study", "connection": "door"},
    ],
    "entry": "entry",
}


@pytest.fixture(scope="module")
def floors():
    """整棟只落實一次(含搜尋+家具),各測試共用。"""
    return realize_graph_building(GRAPH, W, D, rng=random.Random(1))


def test_builds_three_floors(floors):
    assert [lb for lb, _, _, _ in floors] == ["1F", "2F", "3F"]
    for _, spec, _, _ in floors:
        assert isinstance(spec, FloorPlanSpec)
        assert spec.rooms and spec.walls and spec.doors


def test_core_and_patio_aligned(floors):
    """★ 樓梯與天井每層同位(上下對齊 → 通樓垂直核)。"""
    stairs = {s.origin for _, sp, _, _ in floors for s in sp.stairs}
    patios = {next((tuple(map(tuple, r.points))
                    for r in sp.rooms if r.kind == "patio"), None)
              for _, sp, _, _ in floors}
    assert len(stairs) == 1
    assert len(patios) == 1 and None not in patios


def test_structural_columns_aligned(floors):
    """★ 結構柱網每層相同(規則等距、上下對齊);進深至少兩跨(≤~6m 經濟跨度)。"""
    grids = {(sp.grid_origin, tuple(sp.x_spacings), tuple(sp.y_spacings))
             for _, sp, _, _ in floors}
    assert len(grids) == 1
    _, spec, _, _ = floors[0]
    assert spec.column_centers is None          # None → 自動放在軸網交點
    assert len(spec.y_spacings) >= 2
    assert max(spec.y_spacings) <= 9000.0        # 經濟跨度上限


def test_every_floor_circulation_ok(floors):
    """★ furnish 後動線修復器把擋路家具移掉 → 每層都走得通。"""
    for lb, spec, _, _ in floors:
        rep = analyze_room_circulation(spec)
        assert rep.ok, (lb, [(r.name, r.reason) for r in rep.blocked])


def test_pipe_shaft_present_and_no_door(floors):
    """★ 每層有管道間(機電豎管),且不開走入門。"""
    for _, spec, _, _ in floors:
        shafts = [r for r in spec.rooms if r.kind == "pipe_shaft"]
        assert len(shafts) == 1
        assert len(_room_openings(spec, Polygon(shafts[0].points))) == 0


def test_draws_to_dxf(floors):
    for _, spec, _, _ in floors:
        doc, layers = _new_doc()
        draw_floor_plan(doc.modelspace(), spec, layers)
        assert len(list(doc.modelspace())) > 0


def test_partition_tiles_envelope():
    env = (0.0, 0.0, W, D)
    cells = partition_envelope(env, 6, random.Random(3))
    assert len(cells) == 6
    total = sum((x1 - x0) * (y1 - y0) for x0, y0, x1, y1 in cells)
    assert abs(total - W * D) < 1.0             # 鋪滿、不重疊
