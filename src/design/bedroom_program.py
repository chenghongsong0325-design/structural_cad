"""Deterministic bedroom allocation inside an existing townhouse skeleton.

Keeps public-floor functions and an aligned vertical core. Chooses upper-floor room
regions, then applies their functions BEFORE walls, openings and furnishings
are built. This is bounded space programming, not a general geometric solver.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from shapely.geometry import Polygon

from src.design.report import JsonReport
from src.design.room_program import ROOM_PROGRAM

BEDROOMS = {'bedroom', 'master_bedroom'}
ELIGIBLE = BEDROOMS | {'study'}


class BedroomCapacityError(ValueError):
    def __init__(self, requested, capacities, detail=None):
        self.conflict = {
            'code': 'bedroom_capacity', 'field': 'bedrooms',
            'requested': requested, 'available': sum(capacities.values()),
            'floor_capacities': [{'floor': k, 'capacity': v} for k, v in capacities.items()],
            'scope': '目前骨架候選可用的上層房間區塊，並非所有建築解法的數學上限',
            'suggestion': '保留原需求；可增加樓層、調整尺寸或改用其他骨架後重試。',
        }
        super().__init__(detail or f'要求 {requested} 間臥室，目前骨架候選只有 '
                         f'{sum(capacities.values())} 個可配置區塊；必要房數沒有被改少。')


def eligible_room(kind, points):
    if kind not in ELIGIBLE:
        return False
    poly = Polygon(points)
    rule = ROOM_PROGRAM['bedroom']
    if not poly.is_valid or poly.is_empty:
        return False
    x0, y0, x1, y1 = poly.bounds
    # Preliminary envelope/area filter only; the final furnishings and all
    # existing geometry checks still run on the actual reconstructed rooms.
    return (poly.area / 1e6 >= rule.min_area
            and min(x1-x0, y1-y0) >= min(rule.min_width, rule.min_depth))


@dataclass
class BedroomAllocation(JsonReport):
    requested: int
    floors: list = field(default_factory=list)

    def to_dict(self):
        return {'requested': self.requested, 'floors': self.floors,
                'method': '保留一樓公共功能與跨層對齊的垂直核；優先保留主臥，再輪流分配到上層可用區塊。'
                          '剩餘臥室區塊改作彈性起居空間，重新配置門窗與家具。',
                'capacity_basis': '分配前候選的臥室／書房初篩數量；後續幾何退讓可能改變區塊，最終房數另行實測核對。',
                'scope': '既有骨架的房間用途分配；通過初篩不代表家具、動線或尺寸檢查已通過。'}

    def summary(self):
        return f'指定 {self.requested} 房：' + '、'.join(f"{f['floor']} {f['assigned']} 房" for f in self.floors)


def allocate_bedrooms(floors, requested):
    if type(requested) is not int or requested < 0:
        raise ValueError('指定臥室數須為非負整數')
    capacity, quotas, master = {}, {}, None
    for label, spec in floors:
        level = int(label.removesuffix('F'))
        capacity[label] = sum(eligible_room(r.kind, r.points) for r in spec.rooms) if level > 1 else 0
        quotas[label] = 0
        if level > 1 and any(r.kind == 'master_bedroom' and eligible_room(r.kind, r.points) for r in spec.rooms):
            master = master or label
    if requested > sum(capacity.values()):
        raise BedroomCapacityError(requested, capacity)
    remaining = requested
    if remaining and master:
        quotas[master] = 1
        remaining -= 1
    while remaining:
        # Balance floor counts, prefer lower floors on ties. Deterministic.
        available = [label for label in capacity if quotas[label] < capacity[label]]
        label = min(available, key=lambda k: (quotas[k], int(k.removesuffix('F'))))
        quotas[label] += 1
        remaining -= 1
    return BedroomAllocation(requested, [
        {'floor': label, 'capacity': capacity[label], 'assigned': quotas[label]}
        for label in capacity])


def apply_floor_program(rooms, quota, level):
    if quota is None:
        return rooms
    candidates = [i for i, (kind, _, points) in enumerate(rooms)
                  if level > 1 and eligible_room(kind, points)]
    if quota > len(candidates):
        raise BedroomCapacityError(quota, {f'{level}F': len(candidates)},
                                   f'{level}F 需要 {quota} 房，但本次幾何退讓後只剩 {len(candidates)} 個可用區塊。')
    candidates.sort(key=lambda i: ({'master_bedroom': 0, 'bedroom': 1, 'study': 2}[rooms[i][0]], i))
    selected = set(candidates[:quota])
    out = []
    for i, (kind, name, points) in enumerate(rooms):
        if i in selected and kind == 'study':
            kind, name = 'bedroom', f'臥室{level}F-{i+1}'
        elif kind in BEDROOMS and i not in selected:
            kind, name = 'living', f'彈性起居{level}F-{i+1}'
        out.append((kind, name, points))
    return out


def requested_bedrooms(requirements):
    """Exact required counts opt into allocation; minimums may retain more rooms."""
    row = next((r for r in requirements or [] if r['field'] == 'bedrooms'
                and r['priority'] == 'required'), None)
    return row['expected'] if row and row.get('operator', 'eq') == 'eq' else None
