import pytest
from shapely.geometry import Polygon
from src.design.layout.narrow_house import generate_narrow_building
from src.design.layout.furniture_refine import _facing
from src.design.layout.plan_check import check_building
from src.design.collision.human_clearance import HumanClearanceEvaluator, clearance_regions, get_rule
from src.design.collision.furniture_pair_constraint import FurniturePairEvaluator
from src.drafting.fixtures import FixturePlacement, Counter, fixture_footprint, counter_footprint
from src.drafting.room import Room


@pytest.mark.parametrize('w,d', [(4500,14000),(5000,14000),(6000,15000),(7000,12000),(8000,13500)])
def test_living_group_faces_each_other(w,d):
    floors = generate_narrow_building(w,d,floors=2,seed=0)
    s = floors[0][1]
    sofa = next(f for f in s.fixtures if getattr(f,'name','') in ('sofa3','sofa2'))
    tv = next(f for f in s.fixtures if getattr(f,'name','') == 'tv_cabinet')
    assert min(_facing(sofa,tv),_facing(tv,sofa)) > .85
    assert check_building(floors).ok


def test_small_bath_and_dining_operation_space():
    for label,s in generate_narrow_building(4500,14000,floors=2,seed=0):
        bath=next(r for r in s.rooms if r.kind=='bathroom')
        for f in s.fixtures:
            if getattr(f,'name','') in ('toilet','basin_small'):
                assert HumanClearanceEvaluator().evaluate_human_clearance(f,bath,s.fixtures).score >= 90
        if label == '1F':
            counter=next(f for f in s.fixtures if isinstance(f,Counter))
            table=next(f for f in s.fixtures if getattr(f,'name','')=='table4')
            assert Polygon(counter_footprint(counter)).distance(Polygon(fixture_footprint(table))) >= 900


def test_counter_operation_zone_starts_at_front_edge():
    c=Counter((0,0),(2000,0),depth=600)
    zone=clearance_regions(c,get_rule('kitchen_counter'))[0][1]
    assert zone.bounds == (0,600,2000,1600)


def test_bedside_score_uses_head_not_bed_centroid():
    room=Room('bedroom',[(0,0),(5000,0),(5000,5000),(0,5000)],kind='bedroom')
    bed=FixturePlacement('bed_double',(2500,0),0)
    stand=FixturePlacement('nightstand',(3575,0),0)
    ev=FurniturePairEvaluator()
    assert ev.evaluate_pair_constraints(bed,room,[stand]).score == 100
    stand.insert=(3575,2000)
    assert ev.evaluate_pair_constraints(bed,room,[stand]).score < 50
