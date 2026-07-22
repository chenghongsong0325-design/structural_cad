"""Connectivity Graph 測試(v0.7 Phase 5-2)。

兩類:
  * **零誤報**——真實生成的圖必須全數連通(入口走得到每一間房)。
  * **抓得到**——注入 Dead Room / Disconnected Area / Orphan Door 都要報出來。
另有「唯讀」保證:analyze_connectivity() 不得改動 spec。
"""
import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shapely.geometry import Point as SPoint
from shapely.geometry import Polygon

from src.design.connectivity import (
    LINK_DOOR,
    LINK_OPEN,
    ConnectivityGraphs,
    analyze_connectivity,
    build_graphs,
    reachable_from,
)
from src.design.layout_generator import (
    HouseBrief,
    generate_floor_plan,
    generate_house_public,
    generate_house_upper,
)

_SPECS = [
    lambda: generate_floor_plan(
        HouseBrief(site_width=16000, site_depth=14000, bedrooms=3)),
    lambda: generate_floor_plan(
        HouseBrief(site_width=20000, site_depth=14000, bedrooms=3)),
    lambda: generate_house_upper(
        HouseBrief(site_width=26000, site_depth=16000, bedrooms=3, seed=1)),
    lambda: generate_house_public(
        HouseBrief(site_width=22000, site_depth=14000, bedrooms=3, seed=1)),
]


def _seal_room(spec, idx: int) -> int:
    """把落在房間 idx 邊界上的**所有可通行洞口**(kind=="door")改成窗,
    並移除對應的 DoorPlacement → 該房與外界的通路全斷。回封掉幾個。

    ⚠️ 連「沒有門扇的開放連通口」也要封(它們同樣是 kind=="door" 的洞口),
    否則開放式餐廚那種連接會漏封。"""
    ring = Polygon(spec.rooms[idx].points).exterior
    sealed = 0
    for w in spec.walls:
        for op in w.openings:
            if op.kind != "door":
                continue
            if ring.distance(SPoint(w.point_at(op.position))) < 1.0:
                op.kind = "window"
                sealed += 1
    spec.doors = [d for d in spec.doors
                  if spec.walls[d.wall_index].openings[d.opening_index].kind
                  == "door"]
    return sealed


# ── 圖結構 ────────────────────────────────────────────────────────────────
def test_build_graphs_returns_four_graphs():
    """四張圖都建得出來,且節點數與 spec.rooms 一致。"""
    for make in _SPECS:
        spec = make()
        g = build_graphs(spec)
        assert isinstance(g, ConnectivityGraphs)
        n = len(spec.rooms)
        assert len(g.names) == n and len(g.kinds) == n
        assert set(g.adjacency) == set(range(n))       # Adjacency Graph
        assert set(g.room_graph) == set(range(n))      # Room Graph
        assert len(g.doors) == len(spec.doors)         # Door Graph
        assert g.spaces and set(g.space_graph) == set(range(len(g.spaces)))


def test_room_graph_is_subgraph_of_adjacency():
    """可通行 ⊆ 實體相鄰:走得到的兩間房一定有共用邊界。"""
    for make in _SPECS:
        g = build_graphs(make())
        for i, nbrs in g.room_graph.items():
            for j in nbrs:
                assert j in g.adjacency[i]


def test_graphs_are_symmetric():
    """無向圖:邊必須雙向一致。"""
    for make in _SPECS:
        g = build_graphs(make())
        for i, nbrs in g.room_graph.items():
            for j, link in nbrs.items():
                assert g.room_graph[j][i] == link
        for i, nbrs in g.adjacency.items():
            for j in nbrs:
                assert i in g.adjacency[j]


def test_links_are_door_or_open():
    """邊只有兩種:有門(door)或無牆開口(open)。"""
    kinds = set()
    for make in _SPECS:
        g = build_graphs(make())
        for nbrs in g.room_graph.values():
            kinds |= set(nbrs.values())
    assert kinds <= {LINK_DOOR, LINK_OPEN}
    assert LINK_DOOR in kinds and LINK_OPEN in kinds   # 兩種都真的出現過


def test_spaces_partition_every_room():
    """Space 是房間的一個分割:每間房恰好屬於一個 Space。"""
    for make in _SPECS:
        spec = make()
        g = build_graphs(spec)
        flat = [i for members in g.spaces for i in members]
        assert sorted(flat) == list(range(len(spec.rooms)))
        for i in range(len(spec.rooms)):
            assert g.space_of(i) is not None


def test_open_linked_rooms_share_a_space():
    """開放連通(無門扇)的兩間房必須被併進同一個 Space。"""
    for make in _SPECS:
        g = build_graphs(make())
        for i, nbrs in g.room_graph.items():
            for j, link in nbrs.items():
                if link == LINK_OPEN:
                    assert g.space_of(i) == g.space_of(j)


def test_door_graph_classifies_doors():
    """門分三類:內門(2房)、對外門(1房)、孤兒門(0房)。生成的圖不該有孤兒門。"""
    for make in _SPECS:
        g = build_graphs(make())
        for d in g.doors:
            assert d.is_interior or d.is_exterior or d.is_orphan
            assert not d.is_orphan
        assert any(d.is_interior for d in g.doors)


# ── 零誤報 ────────────────────────────────────────────────────────────────
def test_entrance_reaches_every_room():
    """★ 核心:入口走得到所有房間(天井豁免)。"""
    for make in _SPECS:
        spec = make()
        report = analyze_connectivity(spec)
        assert report.entrance is not None
        assert report.ok, report.summary()
        assert report.dead_rooms == [] and report.unreachable == []
        assert report.disconnected_areas == []
        assert "PASS" in report.summary()


def test_patio_is_exempt_from_reachability():
    """天井是室外空井,不該被當成走不到的房間。"""
    spec = generate_house_upper(
        HouseBrief(site_width=20000, site_depth=20000, bedrooms=3, seed=1))
    assert any(r.kind == "patio" for r in spec.rooms)
    assert analyze_connectivity(spec).ok


def test_analyze_does_not_mutate_spec():
    """★ 需求:只建 Graph,不得修改 Layout。"""
    for make in _SPECS:
        spec = make()
        before = copy.deepcopy((
            [r.points for r in spec.rooms],
            [(w.start, w.end, [(o.position, o.width, o.kind) for o in w.openings])
             for w in spec.walls],
            [(d.wall_index, d.opening_index) for d in spec.doors],
        ))
        analyze_connectivity(spec)
        after = (
            [r.points for r in spec.rooms],
            [(w.start, w.end, [(o.position, o.width, o.kind) for o in w.openings])
             for w in spec.walls],
            [(d.wall_index, d.opening_index) for d in spec.doors],
        )
        assert before == after


# ── 抓得到缺陷 ────────────────────────────────────────────────────────────
def test_dead_room_detected():
    """把某間臥室四周通路全封死 → Dead Room(完全沒有出入口)。"""
    spec = _SPECS[0]()
    idx = next(i for i, r in enumerate(spec.rooms) if r.kind == "bedroom")
    name = spec.rooms[idx].name
    assert _seal_room(spec, idx) > 0

    report = analyze_connectivity(spec)
    assert not report.ok
    assert name in report.dead_rooms
    assert build_graphs(spec).room_graph[idx] == {}     # 圖上真的沒有邊


def test_disconnected_area_and_unreachable_detected():
    """把「彼此相鄰的一組房間」整組搬到遠處 → 它們仍互通,但與入口脫節,
    應報 unreachable(有路但接不到)+ disconnected area(版面裂成多區)。"""
    from src.design.connectivity import shared_edge

    spec = _SPECS[0]()
    g = build_graphs(spec)
    polys = [Polygon(r.points) for r in spec.rooms]
    # 挑共用邊界最長、且都不是入口的一組相鄰房(確保搬走後彼此仍走得通)
    pairs = [(i, j) for i in g.adjacency for j in g.adjacency[i]
             if i < j and g.entry not in (i, j)]
    assert pairs
    i, j = max(pairs, key=lambda p: shared_edge(polys[p[0]], polys[p[1]]).length)
    for k in (i, j):
        spec.rooms[k].points = [(x + 500000.0, y) for x, y in spec.rooms[k].points]

    report = analyze_connectivity(spec)
    assert not report.ok
    assert report.unreachable                       # 有路但接不到入口
    assert report.disconnected_areas                # 版面裂成多區
    assert report.unreachable_spaces


def test_orphan_door_detected():
    """把帶門的整道牆移到天邊 → 該門不服務任何房間,報 orphan door。"""
    spec = _SPECS[0]()
    dp = spec.doors[0]
    w = spec.walls[dp.wall_index]
    w.start, w.end = (-99000.0, -99000.0), (-95000.0, -99000.0)
    report = analyze_connectivity(spec)
    assert report.orphan_doors
    assert not report.ok


def test_reachable_from_none_is_empty():
    """沒有入口時 reachable_from 回空集合(不炸)。"""
    g = build_graphs(_SPECS[0]())
    assert reachable_from(g, None) == set()


def test_reachable_from_entry_covers_all_non_patio():
    """從入口 BFS 應涵蓋所有非天井房間。"""
    for make in _SPECS:
        spec = make()
        g = build_graphs(spec)
        seen = reachable_from(g, g.entry)
        for i, r in enumerate(spec.rooms):
            if r.kind != "patio":
                assert i in seen, f"{r.name} 走不到"
