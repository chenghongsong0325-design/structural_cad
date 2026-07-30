"""graph_layout(混合式 AI 建築師第 2 步:LLM 關係圖 → 真圖)測試。

用**固定關係圖**(不呼叫 Gemini)驗證落實引擎:
  * 三層都生得出、畫得出 DXF、評得動分。
  * 垂直核(樓梯+管道間)與結構柱**上下對齊**(符合柱網原則);不設天井。
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
from shapely.geometry import Point, Polygon

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


def test_core_and_shaft_aligned(floors):
    """★ 樓梯與管道間每層同位(上下對齊 → 通樓垂直核)。

    ⚠️ 住宅不設天井(使用者 2026-07-29 定調):LLM 關係圖裡就算提了 patio,
       落實時也會被丟掉,圖上不會有天井。"""
    stairs = {s.origin for _, sp, _, _ in floors for s in sp.stairs}
    shafts = {next((tuple(map(tuple, r.points))
                    for r in sp.rooms if r.kind == "pipe_shaft"), None)
              for _, sp, _, _ in floors}
    assert len(stairs) == 1
    assert len(shafts) == 1 and None not in shafts
    assert not [r for _, sp, _, _ in floors for r in sp.rooms
                if r.kind == "patio"]


def test_pipe_shaft_is_realistic_size(floors):
    """★ 管道間是真實尺寸(寬 40~80cm、深 40~60cm),不是一整條核那麼大。

    使用者 2026-07-29 定調;原本是「橫跨整個核 2.0~2.6m 寬 × 0.7m 深」的一條帶。"""
    from shapely.geometry import Polygon
    for _lb, sp, _a, _b in floors:
        shafts = [r for r in sp.rooms if r.kind == "pipe_shaft"]
        assert shafts
        for r in shafts:
            x0, y0, x1, y1 = Polygon(r.points).bounds
            assert 400 <= x1 - x0 <= 800, (x1 - x0)
            assert 400 <= y1 - y0 <= 600, (y1 - y0)


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


def _floating_columns(spec, env):
    """柱網交點中「落在某房內部(離邊界 >半柱寬)」的數目 = 孤立在房間中央的內柱。"""
    from shapely.geometry import Point
    from src.design.layout.graph_layout import COLUMN_SIZE, COLUMN_SPAN, _n_bays_x
    wd, dp = env[2] - env[0], env[3] - env[1]
    nx, ny = _n_bays_x(wd), max(1, round(dp / COLUMN_SPAN))
    xs = [env[0] + i * (wd / nx) for i in range(nx + 1)]
    ys = [env[1] + j * (dp / ny) for j in range(ny + 1)]
    polys = [Polygon(r.points) for r in spec.rooms]
    half = COLUMN_SIZE / 2
    return sum(
        1 for x in xs for y in ys
        if any(p.contains(Point(x, y)) and p.boundary.distance(Point(x, y)) > half
               for p in polys))


def test_courtyard_skeleton_for_wide_and_square_sites():
    """★ Phase 3:寬扁/方形基地自動換**中庭骨架**——核與中庭置中、房間繞一圈,
    視為獨棟 → 四面採光(不套透天的東西共壁規則),圖面檢查零硬錯誤。"""
    from src.design.layout.global_score import score_report
    from src.design.layout.graph_layout import _choose_core
    from src.design.layout.plan_check import check_building

    for bw, bd in [(15000.0, 15000.0), (20000.0, 20000.0), (24000.0, 16000.0)]:
        env = (2000.0, 2000.0, 2000.0 + bw, 2000.0 + bd)
        assert _choose_core(env)[5], f"{bw:.0f}x{bd:.0f} 應該選中庭骨架"
        floors = realize_graph_building(GRAPH, bw, bd, rng=random.Random(7))
        rep = check_building(floors, env)
        assert rep.ok, rep.summary()
        for _lb, spec, _s, _t in floors:          # 獨棟:四面開窗 → 不該是暗棟
            assert score_report(spec)["sub_scores"]["natural_lighting"] > 0
        grids = {(sp.grid_origin, tuple(sp.x_spacings), tuple(sp.y_spacings))
                 for _l, sp, _s, _t in floors}
        assert len(grids) == 1                    # 柱網每層相同(上下對齊)


def test_narrow_still_uses_column_core():
    """★ 窄長基地維持原本的西側核骨架(Phase 3 不影響既有透天)。"""
    from src.design.layout.graph_layout import _choose_core
    for bw, bd in [(7000.0, 12000.0), (11000.0, 12000.0), (13000.0, 14000.0)]:
        env = (2000.0, 2000.0, 2000.0 + bw, 2000.0 + bd)
        assert _choose_core(env)[5] is None       # None = 西側核(非中庭)


def test_habitable_rooms_get_windows():
    """★ 有採光面的居室一定有窗:貼前後外牆(或天井)的房間不能是暗房。

    以前配窗挑「最長的外牆邊」,深長客廳最長邊是東側共壁 → 窗開了又被共壁規則刪掉,
    結果整層採光分 0。修正後由 _ensure_room_windows 補回前後外牆/天井側的窗。"""
    from src.design.layout.global_score import score_report
    from src.design.layout.narrow_house import WINDOW_KINDS

    for bw, bd in [(7000.0, 12000.0), (11000.0, 12000.0), (13000.0, 14000.0)]:
        env = (2000.0, 2000.0, 2000.0 + bw, 2000.0 + bd)
        for lb, spec, _, _ in realize_graph_building(GRAPH, bw, bd,
                                                     rng=random.Random(7)):
            for r in spec.rooms:
                if r.kind not in WINDOW_KINDS:
                    continue
                poly = Polygon(r.points)
                x0, y0, x1, y1 = poly.bounds
                if not (abs(y0 - env[1]) < 60 or abs(y1 - env[3]) < 60):
                    continue                     # 內間(無前後外牆)不強制,由 critique 回報
                has = any(op.kind == "window" and poly.exterior.distance(
                    Point(*w.point_at(op.position))) < 60
                    for w in spec.walls for op in w.openings)
                assert has, f"{bw:.0f}x{bd:.0f} {lb}/{r.name} 貼外牆卻沒窗"
        # 整棟採光子分數不得再是 0
        for _lb, spec, _, _ in realize_graph_building(GRAPH, bw, bd,
                                                      rng=random.Random(7)):
            assert score_report(spec)["sub_scores"]["natural_lighting"] > 0


def test_floor_is_one_connected_whole_all_sizes():
    """★ 從大門進來走得到每一間房:整層室內連通,不會被柱線牆切成兩半、
    也不會有「只能從室外自己開門進去」的孤島。"""
    from src.design.layout.narrow_house import _room_components

    for bw, bd in [(5000.0, 10000.0), (7000.0, 12000.0), (9000.0, 12000.0),
                   (11000.0, 12000.0), (13000.0, 14000.0)]:
        for lb, spec, _, _ in realize_graph_building(GRAPH, bw, bd,
                                                     rng=random.Random(7)):
            comps = _room_components(spec)
            assert len(comps) == 1, (
                f"{bw:.0f}x{bd:.0f} {lb} 室內斷成 {len(comps)} 塊:"
                f"{[[spec.rooms[i].name for i in c] for c in comps]}")


def test_only_one_entrance_on_ground_floor():
    """★ 1F 恰有一扇對外大門(不會為了補門在外牆多開一道);樓上外牆完全不開門。"""
    for bw, bd in [(7000.0, 12000.0), (11000.0, 12000.0)]:
        env = (2000.0, 2000.0, 2000.0 + bw, 2000.0 + bd)
        for lb, spec, _, _ in realize_graph_building(GRAPH, bw, bd,
                                                     rng=random.Random(7)):
            ext = 0
            for w in spec.walls:
                for op in w.openings:
                    if op.kind != "door":
                        continue
                    cx, cy = w.point_at(op.position)
                    if (abs(cy - env[1]) < 60 or abs(cy - env[3]) < 60
                            or abs(cx - env[0]) < 60 or abs(cx - env[2]) < 60):
                        ext += 1
            assert ext == (1 if lb == "1F" else 0), f"{lb} 對外門 {ext} 扇"


def test_every_room_has_a_door_all_sizes():
    """★ 不管尺寸:每間可進入的房間都有門/通道(機電豎管、天井除外),1F 有臨路大門,
    樓上外牆不開門(不會有通往空中的門)。"""
    from src.design.layout.narrow_house import NO_DOOR_KINDS
    from src.design.layout.room_circulation import _room_openings

    for bw, bd in [(5000.0, 10000.0), (7000.0, 12000.0), (11000.0, 12000.0),
                   (13000.0, 14000.0)]:
        env = (2000.0, 2000.0, 2000.0 + bw, 2000.0 + bd)
        floors = realize_graph_building(GRAPH, bw, bd, rng=random.Random(7))
        for lb, spec, _, _ in floors:
            for r in spec.rooms:
                if r.kind in NO_DOOR_KINDS:
                    continue
                assert _room_openings(spec, Polygon(r.points)), \
                    f"{bw:.0f}x{bd:.0f} {lb}/{r.name}({r.kind}) 沒有門"
            # 樓上外牆不得開門(通往空中)
            if lb != "1F":
                for w in spec.walls:
                    for op in w.openings:
                        if op.kind != "door":
                            continue
                        cx, cy = w.point_at(op.position)
                        on_edge = (abs(cy - env[1]) < 60 or abs(cy - env[3]) < 60
                                   or abs(cx - env[0]) < 60 or abs(cx - env[2]) < 60)
                        assert not on_edge, f"{lb} 外牆開了通往室外的門"
        # 1F 一定有臨路大門(南向外牆)
        _, f1, _, _ = floors[0]
        assert any(op.kind == "door" and abs(w.point_at(op.position)[1] - env[1]) < 60
                   for w in f1.walls for op in w.openings), "1F 沒有臨路大門"


def test_wide_building_multibay_columns_hidden():
    """★ Phase 2:面寬 >9m → 多跨柱(每跨 ≤9m),內柱藏預切的柱線牆、不孤立房中央,
    每層動線仍全通。"""
    bw, bd = 13000.0, 12000.0
    env = (2000.0, 2000.0, 2000.0 + bw, 2000.0 + bd)
    floors = realize_graph_building(GRAPH, bw, bd, rng=random.Random(7))
    _, spec, _, _ = floors[0]
    assert len(spec.x_spacings) >= 2                 # 分多跨
    assert max(spec.x_spacings) <= 9000.0            # 每跨 ≤9m 經濟上限
    floating = sum(_floating_columns(sp, env) for _, sp, _, _ in floors)
    assert floating <= 1, f"孤立內柱過多:{floating}"   # 藏牆內(容 1 根動線退讓)
    for lb, sp, _, _ in floors:
        assert analyze_room_circulation(sp).ok, lb
