"""Corridor Analyzer(v0.7 Phase 5-3)—— 動線的量化分析。

⚠️ **唯讀**:只分析,不改 spec、不 Auto Fix、不碰 Generator。

建立在 Phase 5-2 的 Connectivity Graph 之上(不重造連通判定),量五件事:

    Corridor Width     每段走道的寬度(最小外接矩形的短邊)與長度
    Bottleneck         寬度不足的走道段,以及過窄的通行洞口
    Dead End           走道的盡端(在 Room Graph 上度數 <= 1 = 走進去沒有去處)
    Walking Distance   從入口走到每個房間的距離(Dijkstra,經實際通行點轉折)
    Longest Path       走最遠的那間房、距離、以及完整路徑

距離模型:每條邊的權重 = 房A形心 → 通行點 → 房B形心 的折線長度。通行點取
該邊上的門位置(door 邊)或共用邊界的中點(open 邊)——比「形心直線距離」
貼近真實步行,因為人一定要穿過門洞。
"""
from __future__ import annotations

import heapq
import math
from dataclasses import dataclass, field

from src.design.connectivity import (
    LINK_DOOR,
    ON_BOUNDARY_TOL,
    build_graphs,
    door_points,
    room_polys,
    shared_edge,
)
from src.design.report import JsonReport

# 走道淨寬下限(mm)。低於此值視為 Bottleneck。
# 實測 34 案 100 層(19 層有走道):走道寬 min 1200 · median 3500 · max 6500,
# 全部 >= 1200 → 取 900 對現況零誤報,又擋得住真正過窄的走道。
MIN_CORRIDOR_WIDTH = 900.0
# 通行洞口寬度下限(mm)。實測 636 個門洞:min 750 · median 900 · max 2500;
# 750 是浴廁門的合理下限,故用「嚴格小於 750」才算瓶頸 → 現況零誤報。
MIN_OPENING_WIDTH = 750.0


@dataclass
class CorridorInfo(JsonReport):
    """一段走道的量測結果。"""

    name: str
    index: int
    width: float                    # 最小外接矩形短邊(mm)
    length: float                   # 最小外接矩形長邊(mm)
    area_m2: float
    degree: int                     # 在 Room Graph 上的連接數
    serves: list = field(default_factory=list)      # 直接連到的房間名

    @property
    def is_dead_end(self) -> bool:
        """走道盡端:只有一個(或沒有)出入口 → 走進去無處可去。"""
        return self.degree <= 1

    @property
    def is_narrow(self) -> bool:
        return self.width < MIN_CORRIDOR_WIDTH

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "index": self.index,
            "width": round(self.width, 1),
            "length": round(self.length, 1),
            "area_m2": self.area_m2,
            "degree": self.degree,
            "serves": list(self.serves),
            "is_dead_end": self.is_dead_end,
            "is_narrow": self.is_narrow,
        }


@dataclass
class CorridorReport(JsonReport):
    """動線分析結果(唯讀產物)。"""

    corridors: list = field(default_factory=list)        # list[CorridorInfo]
    bottlenecks: list = field(default_factory=list)      # 過窄的走道/洞口說明
    dead_ends: list = field(default_factory=list)        # 盡端走道名
    walking_distance: dict = field(default_factory=dict)  # 房名 -> mm
    longest_room: str | None = None
    longest_distance: float = 0.0
    longest_path: list = field(default_factory=list)     # 房名序列
    unreachable: list = field(default_factory=list)      # 走不到的房(無距離)
    entrance: str | None = None

    @property
    def has_corridor(self) -> bool:
        return bool(self.corridors)

    @property
    def min_width(self) -> float | None:
        return min((c.width for c in self.corridors), default=None)

    @property
    def average_distance(self) -> float:
        vals = list(self.walking_distance.values())
        return sum(vals) / len(vals) if vals else 0.0

    @property
    def ok(self) -> bool:
        """沒有盡端走道、沒有瓶頸、沒有走不到的房間。"""
        return not (self.dead_ends or self.bottlenecks or self.unreachable)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "entrance": self.entrance,
            "has_corridor": self.has_corridor,
            "min_width": (round(self.min_width, 1)
                          if self.min_width is not None else None),
            "average_distance": round(self.average_distance, 1),
            "corridors": [c.to_dict() for c in self.corridors],
            "bottlenecks": list(self.bottlenecks),
            "dead_ends": list(self.dead_ends),
            "walking_distance": dict(self.walking_distance),
            "longest_room": self.longest_room,
            "longest_distance": round(self.longest_distance, 1),
            "longest_path": list(self.longest_path),
            "unreachable": list(self.unreachable),
        }

    def summary(self) -> str:
        w = f"{self.min_width:.0f}mm" if self.min_width is not None else "—"
        head = (f"CorridorReport:走道 {len(self.corridors)} 段(最窄 {w})· "
                f"入口 {self.entrance or '(無)'} · "
                f"最遠 {self.longest_room or '—'} "
                f"{self.longest_distance/1000:.1f}m → "
                f"{'PASS' if self.ok else 'FAIL'}")
        lines = [head]
        for c in self.corridors:
            lines.append(f"  走道 {c.name}:寬 {c.width:.0f} × 長 {c.length:.0f}mm"
                         f",連接 {c.degree} 處")
        for b in self.bottlenecks:
            lines.append(f"  Bottleneck:{b}")
        for d in self.dead_ends:
            lines.append(f"  Dead End:{d}")
        if self.unreachable:
            lines.append(f"  走不到:{', '.join(self.unreachable)}")
        if self.longest_path:
            lines.append(f"  Longest Path:{' → '.join(self.longest_path)}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# 幾何
# ---------------------------------------------------------------------------
def _rect_dims(poly) -> tuple[float, float]:
    """最小外接矩形的(短邊, 長邊)。退化圖形回 (0, 0)。

    ⚠️ 直線走道量得準;L 形走道會被外接矩形高估(轉角處),屬已知近似。"""
    if poly.is_empty or poly.area <= 0:
        return (0.0, 0.0)
    rect = poly.minimum_rotated_rectangle
    if not hasattr(rect, "exterior"):
        return (0.0, 0.0)
    pts = list(rect.exterior.coords)[:4]
    if len(pts) < 4:
        return (0.0, 0.0)
    a = math.dist(pts[0], pts[1])
    b = math.dist(pts[1], pts[2])
    return (min(a, b), max(a, b))


def _link_point(polys, dpts, i: int, j: int):
    """兩房之間的實際通行點:該邊上的門位置,否則取共用邊界中點。"""
    edge = shared_edge(polys[i], polys[j])
    if edge is None:
        return None
    for p in dpts:
        if p.distance(edge) < ON_BOUNDARY_TOL:
            return (p.x, p.y)
    mid = edge.interpolate(0.5, normalized=True)
    return (mid.x, mid.y)


def _dijkstra(start: int, nodes, weights) -> tuple[dict, dict]:
    """回 (距離, 前驅);weights[(i,j)] = 邊權重。"""
    dist = {start: 0.0}
    prev: dict = {}
    pq = [(0.0, start)]
    done = set()
    while pq:
        d, u = heapq.heappop(pq)
        if u in done:
            continue
        done.add(u)
        for v in nodes[u]:
            w = weights.get((u, v))
            if w is None:
                continue
            nd = d + w
            if nd < dist.get(v, math.inf):
                dist[v] = nd
                prev[v] = u
                heapq.heappush(pq, (nd, v))
    return dist, prev


# ---------------------------------------------------------------------------
# Analyzer
# ---------------------------------------------------------------------------
def analyze_corridors(spec) -> CorridorReport:
    """分析動線,回一份 CorridorReport。**唯讀**,不改 spec。"""
    g = build_graphs(spec)
    polys = room_polys(spec)
    dpts = door_points(spec)
    names = g.names

    # ── 邊權重:形心 → 通行點 → 形心 ──────────────────────────────────────
    weights: dict = {}
    adj = {i: set(g.room_graph[i]) for i in range(len(names))}
    for i in range(len(names)):
        for j in g.room_graph[i]:
            if (i, j) in weights:
                continue
            lp = _link_point(polys, dpts, i, j)
            if lp is None:
                continue
            ci, cj = polys[i].centroid, polys[j].centroid
            w = math.dist((ci.x, ci.y), lp) + math.dist(lp, (cj.x, cj.y))
            weights[(i, j)] = weights[(j, i)] = w

    # ── Corridor Width / Dead End ────────────────────────────────────────
    corridors = []
    for i, kind in enumerate(g.kinds):
        if kind != "corridor":
            continue
        width, length = _rect_dims(polys[i])
        corridors.append(CorridorInfo(
            name=names[i], index=i, width=width, length=length,
            area_m2=round(polys[i].area / 1e6, 1),
            degree=len(g.room_graph[i]),
            serves=[names[j] for j in g.room_graph[i]]))

    dead_ends = [c.name for c in corridors if c.is_dead_end]

    # ── Bottleneck:過窄的走道 + 過窄的通行洞口 ──────────────────────────
    bottlenecks = [f"走道 {c.name} 淨寬 {c.width:.0f}mm < {MIN_CORRIDOR_WIDTH:.0f}mm"
                   for c in corridors if c.is_narrow]
    for w_i, wall in enumerate(spec.walls):
        for op in wall.openings:
            if op.kind == "door" and op.width < MIN_OPENING_WIDTH:
                bottlenecks.append(
                    f"牆 {w_i} 的通行洞口僅 {op.width:.0f}mm "
                    f"< {MIN_OPENING_WIDTH:.0f}mm")

    # ── Walking Distance / Longest Path ──────────────────────────────────
    dist, prev = ({}, {}) if g.entry is None else _dijkstra(g.entry, adj, weights)
    walking = {names[i]: round(d, 1) for i, d in sorted(dist.items())}
    unreachable = [names[i] for i in range(len(names))
                   if i not in dist and g.kinds[i] != "patio"]

    longest_room = longest_path = None
    longest_distance = 0.0
    if dist:
        far = max(dist, key=lambda k: dist[k])
        longest_room, longest_distance = names[far], round(dist[far], 1)
        path, cur = [], far
        while cur is not None:
            path.append(names[cur])
            cur = prev.get(cur)
        longest_path = list(reversed(path))

    return CorridorReport(
        corridors=corridors, bottlenecks=bottlenecks, dead_ends=dead_ends,
        walking_distance=walking, longest_room=longest_room,
        longest_distance=longest_distance, longest_path=longest_path or [],
        unreachable=unreachable,
        entrance=names[g.entry] if g.entry is not None else None)
