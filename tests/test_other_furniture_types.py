from types import SimpleNamespace
import pytest
from shapely.geometry import Polygon
from src.design.layout.narrow_house import generate_narrow_building
from src.design.layout.furniture_refine import usage_issues
from src.design.collision.human_clearance import HumanClearanceEvaluator
from src.drafting.fixtures import FixturePlacement, fixture_footprint
from src.design.layout_generator import HouseBrief,generate_floor_plan


@pytest.mark.parametrize('w,d,name',[(4000,13500,'fridge'),(6000,15000,'wardrobe'),(8000,13500,'wardrobe')])
def test_other_widths_keep_equipment_usable(w,d,name):
    found = 0
    for label,s in generate_narrow_building(w,d,floors=2,seed=0):
        for f in s.fixtures:
            if getattr(f,'name','') != name:continue
            room=next(r for r in s.rooms if Polygon(r.points).covers(Polygon(fixture_footprint(f)).centroid))
            assert not HumanClearanceEvaluator().evaluate_human_clearance(f,room,s.fixtures).violations
            found+=1
    assert found


def test_general_house_basin_and_review_are_real_geometry():
    s=generate_floor_plan(HouseBrief(site_width=16000,site_depth=14000,bedrooms=2))
    building=SimpleNamespace(floors=[SimpleNamespace(label='1F',spec=s)])
    assert not usage_issues(building)
    bed=next(f for f in s.fixtures if getattr(f,'name','')=='bed_double')
    # Deliberately put a cabinet in front of a bed; reporting must catch it.
    s.fixtures.append(FixturePlacement('wardrobe',bed.insert,bed.rotation))
    assert usage_issues(building)


@pytest.mark.parametrize('units',[2,4])
def test_repartitioned_studios_keep_outline_and_double_beds(units):
    from src.design.layout_generator import CorridorBrief
    from src.drafting.unit import one_room_unit
    from src.design.layout.plan_check import check_building
    unit=one_room_unit()
    assert (unit.width,unit.depth)==(4000,6000)
    s=generate_floor_plan(CorridorBrief(units_per_row=units))
    assert sum(getattr(f,'name','')=='bed_double' for f in s.fixtures)==units*2
    assert not usage_issues(SimpleNamespace(floors=[SimpleNamespace(label='3F',spec=s)]))
    assert check_building([('3F',s)]).ok


def test_repartitioned_shallow_house_keeps_two_double_beds_and_site():
    from src.design.layout.shallow_house import generate_shallow_building
    from src.design.layout.plan_check import check_building
    floors=generate_shallow_building(8000,9000,floors=2)
    s=floors[1][1]
    assert sum(getattr(f,'name','')=='bed_double' for f in s.fixtures)==2
    xs=[p[0] for p in s.site_boundary];ys=[p[1] for p in s.site_boundary]
    assert max(xs)-min(xs)==12000
    assert max(ys)-min(ys)==13000
    assert not usage_issues(SimpleNamespace(floors=[SimpleNamespace(label=l,spec=sp) for l,sp in floors]))
    assert check_building(floors).ok
