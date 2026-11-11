"""Source targets, actual clear dimensions, and report/placement integration."""
from types import SimpleNamespace
from copy import deepcopy
import json

import pytest
from shapely.affinity import rotate
from shapely.geometry import Polygon, box

from src.design.ergonomics import analyze_ergonomics, measure_region, planning_rules
from src.design.collision.human_clearance import (
    HumanClearanceEvaluator, clearance_regions, get_rule, human_type,
)
from src.drafting.apartment_plan import FloorPlanSpec
from src.drafting.fixtures import Counter, FixturePlacement
from src.drafting.room import Room
from src.drafting.wall import Wall


def building(fixtures, walls=(), columns=()):
    room = Room("同名房間", [(0, 0), (5000, 0), (5000, 5000), (0, 5000)], kind="kitchen")
    spec = FloorPlanSpec(site_boundary=room.points, setback=0, rooms=[room],
                         fixtures=list(fixtures), walls=list(walls), column_centers=list(columns))
    return SimpleNamespace(floors=[SimpleNamespace(label="1F", spec=spec)])


def test_nkba_targets_and_centerline_conversion():
    rules = planning_rules()
    assert rules["kitchen_counter"].front_clearance == 1067
    assert rules["sink"].front_clearance == 762
    assert rules["toilet"].front_clearance == 762
    assert rules["toilet"].side_clearance + 380/2 == 457
    # Planning targets must not silently rewrite the legacy minimum profile.
    assert get_rule("kitchen_counter").front_clearance == 1000


@pytest.mark.parametrize("name,kind", [("basin_small", "sink"), ("closet_rail", "wardrobe"),
                                      ("table2", "wall_dining_table")])
def test_small_fixtures_have_real_activity_rules(name, kind):
    f = FixturePlacement(name, (2500, 0), 0)
    assert human_type(f) == kind
    assert clearance_regions(f, get_rule(kind))
    narrow = Room("測試", [(0,0), (5000,0), (5000,500), (0,500)], kind="dining")
    if name != "table2":
        assert HumanClearanceEvaluator().evaluate_human_clearance(f, narrow, []).score < 100


def test_dining_measurement_begins_at_table_edge_not_seated_chair_edge():
    f = FixturePlacement("table4", (2500, 2500), 0)
    zones = dict(clearance_regions(f, get_rule("dining_table")))
    assert zones["front"].bounds[1] == 2500 + 850/2
    wall_table = FixturePlacement("table2", (2500, 0), 0)
    zones = dict(clearance_regions(wall_table, get_rule("wall_dining_table")))
    assert set(zones) == {"left", "right"}
    assert zones["right"].bounds[0] == 3100  # actual tabletop is 1200 wide


@pytest.mark.parametrize("angle", [0,90,180,270])
def test_thin_obstruction_reports_real_shortfall_even_with_small_area(angle):
    zone = Polygon([(0,0),(1000,0),(1000,900),(0,900)])
    free = box(-5000,-5000,5000,5000).difference(box(490,300,510,350))
    result = measure_region(rotate(zone,angle,origin=(0,0)), "front", 900,
                            rotate(free,angle,origin=(0,0)))
    assert result["status"] == "needs_review"
    assert result["available_mm"] == pytest.approx(300)
    assert result["blocked_percent"] < 1


def test_final_geometry_counts_wall_faces_columns_and_counters_read_only():
    counter = Counter((1000,0),(3000,0),depth=600)
    wall = Wall((0,1600),(5000,1600),thickness=200)
    b = building([counter], [wall])
    before = deepcopy(b.floors[0].spec)
    report = analyze_ergonomics(b)
    row = report.to_dict()["items"][0]
    assert row["regions"][0]["available_mm"] == 900  # front face 600 -> wall face 1500
    assert row["regions"][0]["target_mm"] == 1067
    assert row["basis"]["source_url"].startswith("https://media.nkba.org/")
    assert b.floors[0].spec == before
    assert json.loads(report.to_json()) == report.to_dict()
    assert "1 件待調整" in report.summary()
    b = building([counter], columns=[(2000,1200)])
    assert analyze_ergonomics(b).items[0]["regions"][0]["available_mm"] == 350


def test_empty_and_unmodeled_objects_are_not_reported_as_verified():
    assert analyze_ergonomics(building([])).to_dict()["status"] == "unverified"
    data = analyze_ergonomics(building([FixturePlacement("tv_cabinet", (2500,0),0)])).to_dict()
    assert data["status"] == "unverified" and len(data["unchecked_furniture"]) == 1


def test_report_retains_source_room_indices_and_duplicate_names():
    b = building([FixturePlacement("basin", (2500,0),0)])
    spec = b.floors[0].spec
    spec.rooms.insert(0, Room("陽台", [(-1000,0),(0,0),(0,5000),(-1000,5000)],kind="balcony"))
    assert analyze_ergonomics(b).items[0]["room_id"] == "f0-room1"


def test_saved_audit_and_html_include_ergonomics_evidence():
    from src.web.design_audit import build_report, render_html
    data = analyze_ergonomics(building([Counter((1000,0),(3000,0),depth=600)])).to_dict()
    report = build_report({"ergonomics": data})
    assert report["snapshot"]["ergonomics"] == data
    assert "人因工學" in render_html(report, [])


def test_refinement_leaves_the_verified_kitchen_work_aisle():
    from src.design.layout.furniture_refine import refine_room
    counter = Counter((1000,0),(4000,0),depth=600)
    table = FixturePlacement("table4", (2500,1900),0)
    b = building([counter,table])
    before = analyze_ergonomics(b).items[0]["regions"][0]["available_mm"]
    spec = b.floors[0].spec
    refine_room(spec, spec.rooms[0])
    after = analyze_ergonomics(b).items[0]["regions"][0]["available_mm"]
    assert before < 1067
    assert after == 1067
    assert spec.fixtures == [counter, table]  # retain both pieces and room dimensions


def test_overlapping_obstacles_do_not_count_the_same_floor_area_twice():
    f = FixturePlacement("wardrobe", (2500,0),0)
    blocker = FixturePlacement("basin_small", (2500,900),0)
    room = building([f]).floors[0].spec.rooms[0]
    evaluator = HumanClearanceEvaluator()
    once = evaluator.evaluate_human_clearance(f,room,[blocker])
    twice = evaluator.evaluate_human_clearance(f,room,[blocker,blocker])
    assert once.score == twice.score < 100
