"""Connectivity Graph(v0.7 Phase 5-2)—— 把格局的「走得到走不到」變成圖。

⚠️ **唯讀**:只建圖與分析,不改 spec、不 Auto Fix、不碰 Generator。

四張圖(同一份 spec 的四種視角):

    Adjacency Graph  房間**實體相鄰**(共用邊界夠長),不管走不走得過去。
    Room Graph       房間**可通行**(有門,或無牆的開口夠寬);Adjacency 的子圖。
    Space Graph      把「開放連通」的房間併成一個 Space(客廳+餐廚+玄關 = 一個
                     開放空間),Space 之間用**門**相連 → 看得出「幾個獨立區域」。
    Door Graph       每扇門服務哪些房間(2 房=內門、1 房=對外門、0 房=孤兒門)。

連通判準(與 Layout Validation 同一套,單一來源):
  * 牆會擋人,但 **kind=="door" 的洞口要扣掉** —— 開放式餐廚、客廳↔家庭廳的
    連通口是「有洞口、無 DoorPlacement」,只看 spec.doors 會誤判成不連通。
  * 窗不能走,不扣。
  * 邊的種類:共用邊界上有門點 → "door";否則無牆缺口 ≥ MIN_PASSAGE → "open"。

典型用法::

    from src.design.connectivity import analyze_connectivity

    report = analyze_connectivity(spec)
    if not report.ok:
        print(report.summary())
"""
from __future__ import annotations

from dataclasses import dataclass, field

from shapely.geometry import LineString
from shapely.geometry import Point as SPoint
from shapely.geometry import Polygon
from shapely.ops import substring, unary_union

# 兩房要算「相鄰」,共用邊界至少這麼長(mm);更短的只是角碰角。
MIN_SHARE = 100.0
# 沒有牆遮蔽的共用邊界要有這麼寬才算「走得過去」(mm)。
MIN_PASSAGE = 700.0
# 牆中心線緩衝(mm):房間邊界與牆中心線共線,用細緩衝判斷該段有沒有牆。
WALL_EPS = 2.0
# 判斷點是否落在某條邊界上的距離門檻(mm)。
ON_BOUNDARY_TOL = 1.0
# 不要求「必須走得到」的房型:天井/挑空是室外空井,不是動線終點。
UNREACHABLE_EXEMPT = {"patio"}

LINK_DOOR = "door"
LINK_OPEN = "open"


# ---------------------------------------------------------------------------
# 幾何原語(唯讀;Layout Validation 也共用這一份)
# ---------------------------------------------------------------------------
def room_polys(spec) -> list[Polygon]:
    """房間多邊形;頂點不足以構成面時回**空多邊形**(不丟例外——退化的房間
    由上層報成問題,不該讓分析器爆掉)。"""
    out = []
    for r in spec.rooms:
        try:
            out.append(Polygon(r.points))
        except (ValueError, TypeError):
            out.append(Polygon())
    return out


def wall_cover(spec):
    """牆對「通行」的遮蔽範圍(細緩衝聯集):牆中心線扣掉 kind=="door" 的洞口。

    門洞不論有沒有掛門扇都是通道;窗不能走,不扣。"""
    segs = []
    for w in spec.walls:
        if w.start == w.end:
            continue
        line = LineString([w.start, w.end])
        length = line.length
        gaps = sorted(
            (max(0.0, op.position - op.width / 2),
             min(length, op.position + op.width / 2))
            for op in w.openings if op.kind == "door")
        pos = 0.0
        for a, b in gaps:                            # 逐段留下「有牆」的部分
            if a > pos:
                segs.append(substring(line, pos, a))
            pos = max(pos, b)
        if pos < length:
            segs.append(substring(line, pos, length))
    buf = [s.buffer(WALL_EPS) for s in segs if s.length > 0]
    return unary_union(buf) if buf else None


def door_points(spec) -> list:
    """每扇門在牆中心線上的中心點(世界座標)。"""
    pts = []
    for dp in spec.doors:
        w = spec.walls[dp.wall_index]
        op = w.openings[dp.opening_index]
        pts.append(SPoint(w.point_at(op.position)))
    return pts


def shared_edge(a: Polygon, b: Polygon):
    """兩房共用的邊界(線);沒有共用邊或太短回 None。"""
    if a.is_empty or b.is_empty or not a.intersects(b):
        return None
    edge = a.exterior.intersection(b.exterior)
    if edge.is_empty or edge.length < MIN_SHARE:
        return None
    return edge


def entry_index(spec, polys, dpts) -> int | None:
    """入口房:優先玄關,其次客廳,再其次「貼著最多門」的房間。"""
    for want in ("foyer", "living"):
        for i, r in enumerate(spec.rooms):
            if r.kind == want:
                return i
    best, best_n = None, 0
    for i, poly in enumerate(polys):
        if poly.is_empty:
            continue
        n = sum(1 for p in dpts if poly.exterior.distance(p) < ON_BOUNDARY_TOL)
        if n > best_n:
            best, best_n = i, n
    return best


def _components(nodes, graph) -> list[list[int]]:
    """無向圖的連通分量(節點由小到大,分量依最小節點排序)。"""
    seen: set[int] = set()
    out: list[list[int]] = []
    for n in nodes:
        if n in seen:
            continue
        comp, stack = [], [n]
        seen.add(n)
        while stack:
            cur = stack.pop()
            comp.append(cur)
            for nxt in graph[cur]:
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        out.append(sorted(comp))
    return sorted(out, key=lambda c: c[0])


# ---------------------------------------------------------------------------
# 圖結構
# ---------------------------------------------------------------------------
@dataclass
class DoorNode:
    """Door Graph 的一個節點:一扇門 + 它服務的房間。"""

    wall_index: int
    opening_index: int
    point: tuple
    rooms: list[int] = field(default_factory=list)   # 貼到的房間 index

    @property
    def is_interior(self) -> bool:
        return len(self.rooms) >= 2

    @property
    def is_exterior(self) -> bool:
        return len(self.rooms) == 1                  # 對外門(如大門)

    @property
    def is_orphan(self) -> bool:
        return not self.rooms                        # 不貼任何房間


@dataclass
class ConnectivityGraphs:
    """四張圖。房間一律以 spec.rooms 的 index 表示。"""

    names: list[str] = field(default_factory=list)
    kinds: list[str] = field(default_factory=list)
    adjacency: dict = field(default_factory=dict)    # int -> set[int](實體相鄰)
    room_graph: dict = field(default_factory=dict)   # int -> dict[int, link]
    doors: list = field(default_factory=list)        # list[DoorNode]
    spaces: list = field(default_factory=list)       # list[list[int]](開放合併)
    space_graph: dict = field(default_factory=dict)  # int -> set[int](門相連)
    entry: int | None = None

    def neighbours(self, i: int) -> set:
        """房間 i 走得到的鄰居(單步)。"""
        return set(self.room_graph.get(i, {}))

    def space_of(self, i: int) -> int | None:
        for s, members in enumerate(self.spaces):
            if i in members:
                return s
        return None


def build_graphs(spec) -> ConnectivityGraphs:
    """由 spec 建出四張圖(唯讀)。"""
    polys = room_polys(spec)
    cover = wall_cover(spec)
    dpts = door_points(spec)
    n = len(polys)

    adjacency: dict = {i: set() for i in range(n)}
    room_graph: dict = {i: {} for i in range(n)}
    for i in range(n):
        for j in range(i + 1, n):
            edge = shared_edge(polys[i], polys[j])
            if edge is None:
                continue
            adjacency[i].add(j)
            adjacency[j].add(i)
            link = None
            if any(p.distance(edge) < ON_BOUNDARY_TOL for p in dpts):
                link = LINK_DOOR
            else:
                gap = edge.difference(cover) if cover is not None else edge
                if (not gap.is_empty) and gap.length >= MIN_PASSAGE:
                    link = LINK_OPEN
            if link:
                room_graph[i][j] = link
                room_graph[j][i] = link

    # Door Graph:每扇門貼到哪些房間
    doors = []
    for dp, pt in zip(spec.doors, dpts):
        served = [i for i, poly in enumerate(polys)
                  if not poly.is_empty
                  and poly.exterior.distance(pt) < ON_BOUNDARY_TOL]
        doors.append(DoorNode(dp.wall_index, dp.opening_index,
                              (pt.x, pt.y), served))

    # Space Graph:只用 open 邊合併成 Space,Space 之間用 door 邊相連
    open_graph = {i: {j for j, lk in room_graph[i].items() if lk == LINK_OPEN}
                  for i in range(n)}
    spaces = _components(range(n), open_graph)
    of: dict = {}
    for s, members in enumerate(spaces):
        for m in members:
            of[m] = s
    space_graph: dict = {s: set() for s in range(len(spaces))}
    for i in range(n):
        for j, lk in room_graph[i].items():
            if lk == LINK_DOOR and of[i] != of[j]:
                space_graph[of[i]].add(of[j])
                space_graph[of[j]].add(of[i])

    return ConnectivityGraphs(
        names=[r.name for r in spec.rooms],
        kinds=[r.kind for r in spec.rooms],
        adjacency=adjacency, room_graph=room_graph, doors=doors,
        spaces=spaces, space_graph=space_graph,
        entry=entry_index(spec, polys, dpts))


# ---------------------------------------------------------------------------
# ConnectivityReport
# ---------------------------------------------------------------------------
@dataclass
class ConnectivityReport:
    """連通性分析結果(唯讀產物)。"""

    graphs: ConnectivityGraphs
    entrance: str | None = None
    reachable: list = field(default_factory=list)       # 走得到的房名
    dead_rooms: list = field(default_factory=list)      # 完全沒有出入口
    unreachable: list = field(default_factory=list)     # 走不到(但有鄰接)
    unreachable_spaces: list = field(default_factory=list)   # list[list[str]]
    disconnected_areas: list = field(default_factory=list)   # list[list[str]]
    orphan_doors: list = field(default_factory=list)    # 不貼任何房間的門

    @property
    def ok(self) -> bool:
        """入口存在,且所有非豁免房間都走得到、走道區域不分裂。"""
        return not (self.dead_rooms or self.unreachable
                    or self.disconnected_areas or self.orphan_doors
                    or self.entrance is None)

    def summary(self) -> str:
        g = self.graphs
        head = (f"ConnectivityReport:房間 {len(g.names)} · 門 {len(g.doors)} · "
                f"Space {len(g.spaces)} · 入口 {self.entrance or '(無)'} → "
                f"{'PASS' if self.ok else 'FAIL'}")
        lines = [head]
        if self.dead_rooms:
            lines.append(f"  Dead Room:{', '.join(self.dead_rooms)}")
        if self.unreachable:
            lines.append(f"  Unreachable:{', '.join(self.unreachable)}")
        if self.unreachable_spaces:
            for sp in self.unreachable_spaces:
                lines.append(f"  Unreachable Space:{', '.join(sp)}")
        if self.disconnected_areas:
            for ar in self.disconnected_areas:
                lines.append(f"  Disconnected Area:{', '.join(ar)}")
        if self.orphan_doors:
            lines.append(f"  Orphan Door:{', '.join(self.orphan_doors)}")
        return "\n".join(lines)


def reachable_from(graphs: ConnectivityGraphs, start: int | None) -> set:
    """從 start 房間在 Room Graph 上走得到的所有房間 index。"""
    if start is None:
        return set()
    seen, stack = {start}, [start]
    while stack:
        for nxt in graphs.room_graph[stack.pop()]:
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    return seen


def analyze_connectivity(spec) -> ConnectivityReport:
    """建圖 + 分析,回一份 ConnectivityReport。**唯讀**,不改 spec。"""
    g = build_graphs(spec)
    n = len(g.names)
    seen = reachable_from(g, g.entry)

    def exempt(i: int) -> bool:
        return g.kinds[i] in UNREACHABLE_EXEMPT

    dead, unreach = [], []
    for i in range(n):
        if i in seen or exempt(i):
            continue
        if not g.room_graph[i]:
            dead.append(g.names[i])                  # 完全沒有出入口
        else:
            unreach.append(g.names[i])               # 有路但接不到入口

    entry_space = g.space_of(g.entry) if g.entry is not None else None
    space_seen = set()
    if entry_space is not None:
        space_seen, stack = {entry_space}, [entry_space]
        while stack:
            for nxt in g.space_graph[stack.pop()]:
                if nxt not in space_seen:
                    space_seen.add(nxt)
                    stack.append(nxt)
    unreachable_spaces = [
        [g.names[i] for i in members]
        for s, members in enumerate(g.spaces)
        if s not in space_seen and not all(exempt(i) for i in members)]

    areas = _components(range(n), {i: set(g.room_graph[i]) for i in range(n)})
    disconnected = [[g.names[i] for i in comp] for comp in areas
                    if g.entry not in comp and not all(exempt(i) for i in comp)]

    return ConnectivityReport(
        graphs=g,
        entrance=g.names[g.entry] if g.entry is not None else None,
        reachable=[g.names[i] for i in sorted(seen)],
        dead_rooms=dead, unreachable=unreach,
        unreachable_spaces=unreachable_spaces,
        disconnected_areas=disconnected,
        orphan_doors=[f"牆 {d.wall_index}" for d in g.doors if d.is_orphan])
