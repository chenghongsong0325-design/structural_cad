import copy
import json
from dataclasses import asdict
from types import SimpleNamespace as NS

import pytest
from shapely.geometry import Polygon, Point

from src.design.bedroom_program import BEDROOMS, BedroomCapacityError, allocate_bedrooms, apply_floor_program
from src.design.building_generator import _narrow_to_building, FLOOR_HEIGHT, BuildingBrief, generate_building_auto
from src.design.layout.narrow_house import generate_narrow_building
from src.design.layout_generator import HouseBrief
from src.design.requirements import check_requirements, prepare_data
from src.design.spatial_report import build_spatial_report
from src.design.validation import validate_building


@pytest.fixture(scope='module')
def three_bedroom_building():
    return _narrow_to_building(generate_narrow_building(4500, 14000, bedroom_target=3, seed=7), FLOOR_HEIGHT)


def test_three_bedrooms_are_real_rooms_with_new_furnishings(three_bedroom_building):
    from src.design.collision.geometry import fixture_obstacles
    building = three_bedroom_building
    beds = [(f, r) for f in building.floors for r in f.spec.rooms if r.kind in BEDROOMS]
    assert len(beds) == 3
    assert validate_building(building)['validation']['status'] == 'passed'
    for fl in building.floors:
        furniture = fixture_obstacles(fl.spec)
        for room in fl.spec.rooms:
            bed_count = sum(ob.tag.startswith('bed_') and Polygon(room.points).covers(ob.poly.centroid)
                            for ob in furniture)
            if room.kind in BEDROOMS:
                assert bed_count >= 1, room.name
            if room.name.startswith('彈性起居'):
                assert room.kind == 'living' and bed_count == 0
    assert any(r.kind == 'living' for r in building.floors[0].spec.rooms)
    assert all(f.spec.stairs for f in building.floors)


@pytest.mark.parametrize('count', [1, 2, 4])
def test_requested_total_is_respected_across_upper_floors(count):
    floors = generate_narrow_building(4500, 14000, bedroom_target=count, furnish=False, seed=7)
    building = _narrow_to_building(floors, FLOOR_HEIGHT)
    assert check_requirements(building, prepare_data({'bedrooms': count}, '')['_requirements']).ok
    assert sum(r.kind == 'master_bedroom' for _,sp in floors for r in sp.rooms) == 1
    assert all(sp._bedroom_allocation['requested'] == count for _,sp in floors)


def test_upper_floor_allocation_preserves_public_floor_and_reports_capacity():
    def spec(kinds):
        return NS(rooms=[NS(kind=k, points=[(0,0),(3000,0),(3000,4000),(0,4000)]) for k in kinds])
    floors = [('1F',spec(['living','dining'])),('2F',spec(['master_bedroom','bedroom'])),
              ('3F',spec(['bedroom','study']))]
    plan = allocate_bedrooms(floors, 3)
    assert [f['assigned'] for f in plan.floors] == [0,2,1]
    assert json.loads(plan.to_json())['requested'] == 3
    with pytest.raises(BedroomCapacityError) as error:
        allocate_bedrooms(floors, 5)
    assert error.value.conflict['available'] == 4
    assert error.value.conflict['requested'] == 5


def test_eligible_study_can_become_a_bedroom_before_furnishing():
    points = [(0,0),(3000,0),(3000,4000),(0,4000)]
    rooms = [('study','書房',points),('living','客廳',points)]
    result = apply_floor_program(rooms, 1, 2)
    assert result[0][0] == 'bedroom' and result[1][0] == 'living'
    assert rooms[0][0] == 'study'  # Does not mutate the input program.


def test_rule_entry_point_uses_total_target():
    brief = BuildingBrief(HouseBrief(site_width=4500, site_depth=14000,
                         dimension_basis='building', setback=0, bedrooms=3),
                         floors=3, differentiated=True, bedroom_target=3)
    building = generate_building_auto(brief)
    assert sum(r.kind in BEDROOMS for f in building.floors for r in f.spec.rooms) == 3


def test_spatial_evidence_is_serializable_and_read_only(three_bedroom_building):
    building = three_bedroom_building
    before = asdict(building)
    validation = validate_building(building)
    report = build_spatial_report(building, validation)
    data = json.loads(report.to_json())
    assert data['status'] == 'ready' and len(data['floors']) == 3
    assert data['bedroom_allocation']['requested'] == 3
    ids = [n['id'] for f in data['floors'] for n in f['nodes']]
    assert len(ids) == len(set(ids))
    assert all(f['anchors'] for f in data['floors'])
    assert all(set((e['a'], e['b'])) <= set(ids) for f in data['floors'] for e in f['edges'])
    assert asdict(building) == before


def test_same_room_names_are_not_silently_mapped_to_first_room(three_bedroom_building):
    building = copy.deepcopy(three_bedroom_building)
    floor = building.floors[1]
    indexes = [i for i,r in enumerate(floor.spec.rooms) if r.kind in BEDROOMS]
    for i in indexes:
        floor.spec.rooms[i].name = '同名臥室'
    validation = {'plan_check': {'issues': [{'severity':'warning','code':'room_oversize',
                                           'floor':floor.label,'room':'同名臥室','detail':'測試量測'}]}}
    report = build_spatial_report(building, validation)
    conflict = report.conflicts[0]
    assert conflict['location'] == 'ambiguous' and len(conflict['room_ids']) == 2
    assert len(set(conflict['room_ids'])) == 2


def test_window_is_not_a_walkable_connection(three_bedroom_building):
    building = copy.deepcopy(three_bedroom_building)
    floor = building.floors[1]
    room_index = next(i for i,r in enumerate(floor.spec.rooms) if r.kind == 'master_bedroom')
    ring = Polygon(floor.spec.rooms[room_index].points).exterior
    for w in floor.spec.walls:
        for op in w.openings:
            if op.kind == 'door' and ring.distance(Point(w.point_at(op.position))) < 1:
                op.kind = 'window'
    floor.spec.doors = [d for d in floor.spec.doors if floor.spec.walls[d.wall_index].openings[d.opening_index].kind == 'door']
    report = build_spatial_report(building)
    node = report.floors[1]['nodes'][room_index]
    assert node['reachable'] is False and not node['route']
    assert any(node['id'] in pair for pair in report.floors[1]['adjacency'])
    assert not any(node['id'] in (e['a'],e['b']) for e in report.floors[1]['edges'])


def test_missing_entry_does_not_use_living_room_as_a_fake_entrance(three_bedroom_building):
    building = copy.deepcopy(three_bedroom_building)
    from src.design.layout.plan_check import building_env, _on_envelope
    spec = building.floors[0].spec
    for w in spec.walls:
        for op in w.openings:
            if op.kind == 'door' and _on_envelope(*w.point_at(op.position), building_env(spec)):
                op.kind = 'window'
    spec.doors = [d for d in spec.doors if spec.walls[d.wall_index].openings[d.opening_index].kind == 'door']
    floor = build_spatial_report(building).floors[0]
    assert floor['anchor_status'] == 'unverified'
    assert all(n['reachable'] is None for n in floor['nodes'])


def test_failure_keeps_spatial_conflicts_and_stops_downloads(monkeypatch, tmp_path, three_bedroom_building):
    from fastapi.testclient import TestClient
    import src.web.app as web
    monkeypatch.setattr(web, 'parse_brief_data', lambda *a, **k: {'brief_type':'house','bedrooms':4,
        'site_width_m':4.5,'site_depth_m':14,'floors_above':3,'dimension_basis':'building'})
    monkeypatch.setattr(web, '_ai_applicable', lambda b: False)
    monkeypatch.setattr(web, 'generate_building_auto', lambda b: three_bedroom_building)
    monkeypatch.setattr(web, 'JOBS_DIR', tmp_path/'jobs')
    monkeypatch.setattr(web, 'build_sheets', lambda b: pytest.fail('Failed candidate must not be rendered'))
    monkeypatch.delenv('ACCESS_CODE', raising=False)
    response = TestClient(web.create_app(lambda: object())).post('/api/generate', json={'text':'四房'})
    assert response.status_code == 422
    data = response.json()['detail']
    assert data['spatial_report']['status'] == 'ready'
    assert any(c['code']=='bedrooms' and c['actual']==3 and len(c['room_ids'])==3 for c in data['conflicts'])
    assert not (tmp_path/'jobs').exists()


def test_capacity_error_is_structured_not_an_unexplained_500(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    import src.web.app as web
    monkeypatch.setattr(web, 'parse_brief_data', lambda *a, **k: {'brief_type':'house','bedrooms':3,
        'site_width_m':4.5,'site_depth_m':14,'floors_above':1,'dimension_basis':'building'})
    monkeypatch.setattr(web, '_ai_applicable', lambda b: False)
    def fail(brief):
        raise BedroomCapacityError(3, {'1F':0})
    monkeypatch.setattr(web, 'generate_building_auto', fail)
    monkeypatch.setattr(web, 'JOBS_DIR', tmp_path/'jobs')
    monkeypatch.delenv('ACCESS_CODE', raising=False)
    response = TestClient(web.create_app(lambda: object())).post('/api/generate', json={'text':'一層三房'})
    assert response.status_code == 422
    assert response.json()['detail']['conflicts'][0]['code'] == 'bedroom_capacity'
