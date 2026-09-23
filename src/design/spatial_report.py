"""Read-only geometry evidence for the web inspector. No LLM explanations.

Reuses connectivity.build_graphs and room_circulation, keeping their tolerances
and limitations visible. Edges describe topology, not pedestrian trajectories.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from shapely.geometry import Point, Polygon

from src.design.connectivity import build_graphs, MIN_SHARE, MIN_PASSAGE, ON_BOUNDARY_TOL
from src.design.layout.plan_check import building_env, _on_envelope, VOID_KINDS
from src.design.layout.room_circulation import analyze_room, _is_cabinet, PASSAGE_WIDTH
from src.design.report import JsonReport


SUGGESTIONS = {
    'room_no_door': '檢查房間與公共空間的共用牆，安排可用門洞或開放通道。',
    'floor_split': '檢查不同連通區之間的隔牆與通道；相鄰不代表可以通行。',
    'circulation_blocked': '調整擋路家具、門口或通道寬；保留房間必要用途。',
    'through_bedroom': '改由公共走道或起居空間進入，避免穿越其他人的臥室。',
    'daylight_area': '核對已計入的外窗／天井開口，再調整房間面積或開口；不可在共壁任意加窗。',
    'room_no_daylight': '檢查是否能接外牆或天井；內部房間不能只靠改名稱取得採光。',
    'bath_no_window': '確認浴廁通風方式；目前程式沒有替你驗證機械通風設備。',
    'opening_on_column': '沿可用牆段移動門窗，保持柱位及最小門口淨距。',
    'furniture_in_wall': '檢查房間淨尺寸與家具外框，重新擺放或換合適尺寸。',
    'door_swing_blocked': '檢查門扇迴轉範圍及附近牆、柱與家具。',
    'room_oversize': '重新分配用途或面積；這是設計警告，不等於結構不安全。',
}


def _paths(graph, starts):
    paths = {i: [i] for i in starts}
    queue = deque(sorted(starts))
    while queue:
        node = queue.popleft()
        for nxt in sorted(graph[node]):
            if nxt not in paths:
                paths[nxt] = paths[node] + [nxt]
                queue.append(nxt)
    return paths


@dataclass
class SpatialReport(JsonReport):
    floors: list = field(default_factory=list)
    conflicts: list = field(default_factory=list)
    allocation: dict | None = None
    status: str = 'ready'

    def to_dict(self):
        return {'version': 1, 'status': self.status, 'coordinate_unit': 'mm',
                'area_basis': '房間多邊形沿牆中心線計算，不是淨使用面積',
                'scope': '逐層拓撲分析；連線不是實際行走軌跡，也不證明跨樓層逃生可行。'
                         '房內家具動線另以既有通行寬模型檢查。',
                'rules': {'shared_boundary_min_mm': MIN_SHARE,
                          'open_passage_min_mm': MIN_PASSAGE,
                          'room_circulation_width_mm': PASSAGE_WIDTH},
                'floors': self.floors, 'conflicts': self.conflicts,
                'bedroom_allocation': self.allocation}

    def summary(self):
        return f'{len(self.floors)} 層空間分析，{len(self.conflicts)} 項待核對紀錄'


def _floor_report(fl, fi):
    spec = fl.spec
    graph = build_graphs(spec)
    env = building_env(spec)
    prefix = f'f{fi}'
    ids = {i: f'{prefix}:r{i}' for i in range(len(spec.rooms))}
    # 1F starts at actual boundary door openings, not the legacy living-room
    # heuristic. Upper floors start at rooms containing the stair projection.
    starts = set()
    polys = [Polygon(r.points) for r in spec.rooms]
    openings = []
    for wi, wall in enumerate(spec.walls):
        for oi, op in enumerate(wall.openings):
            point = Point(wall.point_at(op.position))
            touched = [i for i, poly in enumerate(polys)
                       if poly.exterior.distance(point) < ON_BOUNDARY_TOL]
            exterior = _on_envelope(point.x, point.y, env)
            openings.append({'id': f'{prefix}:w{wi}:o{oi}', 'kind': op.kind,
                             'point': [point.x, point.y], 'width_mm': op.width,
                             'exterior': exterior, 'room_ids': [ids[i] for i in touched]})
            if fl.level == 1 and op.kind == 'door' and exterior:
                starts.update(touched)
    if fl.level != 1:
        from src.design.layout.plan_check import _stair_footprint
        footprints = [_stair_footprint(stair) for stair in spec.stairs]
        starts.update(i for i, room in enumerate(spec.rooms)
                      if room.kind in {'stair', 'stair_hall'}
                      and any(polys[i].intersection(p).area > 0 for p in footprints))
    paths = _paths(graph.room_graph, starts)
    from src.design.collision.geometry import fixture_obstacles
    furniture = fixture_obstacles(spec)
    nodes = []
    for i, (room, poly) in enumerate(zip(spec.rooms, polys)):
        representative = poly.representative_point()
        skipped = room.kind in VOID_KINDS | {'garage', 'parking', 'stair', 'balcony'} or _is_cabinet(room)
        circulation = None if skipped else analyze_room(spec, room).to_dict()
        # Use the collision engine's full-footprint centroids, including counters.
        fixtures = [ob.tag for ob in furniture if poly.covers(ob.poly.centroid)]
        nodes.append({'id': ids[i], 'index': i, 'name': room.name, 'kind': room.kind,
                      'polygon': [list(p) for p in room.points],
                      'center': [representative.x, representative.y],
                      'area_m2': round(poly.area / 1e6, 3), 'bounds_mm': list(poly.bounds),
                      'access_required': room.kind not in VOID_KINDS,
                      'reachable': i in paths if starts and room.kind not in VOID_KINDS else None,
                      'route': [ids[j] for j in paths.get(i, [])],
                      'neighbours': [ids[j] for j in sorted(graph.room_graph[i])],
                      'fixtures': fixtures, 'circulation': circulation,
                      'conflict_ids': []})
    return {'id': prefix, 'label': fl.label, 'level': fl.level, 'nodes': nodes,
            'adjacency': [[ids[i], ids[j]] for i, linked in graph.adjacency.items() for j in sorted(linked) if i < j],
            'edges': [{'a': ids[i], 'b': ids[j], 'kind': kind}
                      for i, linked in graph.room_graph.items() for j, kind in sorted(linked.items()) if i < j],
            'openings': openings, 'anchors': [ids[i] for i in sorted(starts)],
            'anchor_basis': '實際外牆門洞' if fl.level == 1 else '樓梯投影所在的樓梯間',
            'anchor_status': 'identified' if starts else 'unverified',
            'allocation': getattr(spec, '_bedroom_allocation', None)}


def build_spatial_report(building, validation=None, requirement_check=None):
    result = SpatialReport()
    result.floors = [_floor_report(fl, i) for i, fl in enumerate(building.floors)]
    result.allocation = next((f['allocation'] for f in result.floors if f['allocation']), None)
    all_nodes = {n['id']: n for f in result.floors for n in f['nodes']}

    def add(item):
        item['id'] = f'issue{len(result.conflicts)}'
        result.conflicts.append(item)
        for node_id in item.get('room_ids', []):
            all_nodes[node_id]['conflict_ids'].append(item['id'])

    for source in ('plan_check', 'code_check'):
        for issue in (validation or {}).get(source, {}).get('issues', []):
            floors = [f for f in result.floors if f['label'] == issue.get('floor')]
            nodes = [n for f in floors for n in f['nodes'] if n['name'] == issue.get('room')]
            # Duplicate labels are candidates, never silently the first room.
            location = 'room' if len(nodes) == 1 else 'ambiguous' if nodes else 'floor' if floors else 'building'
            add({**issue, 'source': source, 'room_ids': [n['id'] for n in nodes],
                 'floor_ids': [f['id'] for f in floors], 'location': location,
                 'suggestion': SUGGESTIONS.get(issue['code'], '依量測結果檢查相關房間、構件或條件，再重新生成並驗證。')})
    for row in (requirement_check or {}).get('items', []):
        if row['status'] == 'met':
            continue
        node_ids = [n['id'] for ref in row.get('evidence', []) for f in result.floors
                    if f['label'] == ref.get('floor') for n in f['nodes'] if n['index'] == ref.get('room_index')]
        add({'source': 'requirements', 'code': row['field'],
             'severity': 'error' if row['priority'] == 'required' else 'warning',
             'detail': row['label'] + '：' + row['detail'], 'floor': '',
             'location': 'rooms' if node_ids else 'building', 'room_ids': node_ids,
             'floor_ids': [], 'expected': row['expected'], 'actual': row['actual'],
             'evidence': row.get('source', ''),
             'suggestion': '必要條件保留；調整配置或明確修改需求後重試。' if row['priority'] == 'required'
                           else '此項為偏好，保留取捨紀錄供人工決定。'})
    return result
