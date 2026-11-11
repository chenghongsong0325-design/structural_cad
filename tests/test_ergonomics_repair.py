from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from src.design.ergonomics import analyze_ergonomics
from src.design.ergonomics_repair import repair_building, _gaps, _companions
from src.drafting.apartment_plan import FloorPlanSpec
from src.drafting.fixtures import FixturePlacement, Counter
from src.drafting.room import Room


def sample():
    room = Room("測試房",[(0,0),(5000,0),(5000,5000),(0,5000)],kind="study")
    spec = FloorPlanSpec(site_boundary=room.points,setback=0,rooms=[room],column_centers=[],
                         fixtures=[FixturePlacement("wardrobe",(2500,0),0),
                                   FixturePlacement("desk",(2500,900),0)])
    return SimpleNamespace(floors=[SimpleNamespace(label="1F",spec=spec)])


def test_moves_the_blocker_and_remeasures_actual_clearance():
    b = sample()
    spec = b.floors[0].spec
    original = deepcopy(spec)
    before = analyze_ergonomics(b).to_dict()
    result = repair_building(b)
    after = analyze_ergonomics(b).to_dict()
    assert result.status == "improved"
    assert result.before == before["warnings"] > result.after == after["warnings"]
    assert result.changes and result.attempts <= result.candidate_limit
    measurements = [m for c in result.changes for m in c["measurements"]]
    assert measurements and all(m["after_mm"] > m["before_mm"] for m in measurements)
    assert spec.fixtures[1].insert != original.fixtures[1].insert
    assert spec.site_boundary == original.site_boundary and spec.rooms == original.rooms
    assert [f.name for f in spec.fixtures] == [f.name for f in original.fixtures]
    old,new = _gaps(before["items"]),_gaps(after["items"])
    assert all(new[k] <= old[k]+1 for k in old)
    assert json.loads(result.to_json()) == result.to_dict()


def test_no_candidates_is_honest_and_keeps_the_whole_plan():
    b = sample()
    original = deepcopy(b.floors[0].spec)
    result = repair_building(b,max_attempts=0)
    assert result.status == "unchanged" and result.after == result.before > 0
    assert result.remaining and result.limit_reached and not result.changes
    assert b.floors[0].spec == original
    assert "有限候選" in result.remaining[0]["reason"]


def test_check_failure_rolls_back_and_is_never_reported_as_improvement(monkeypatch):
    import src.design.ergonomics_repair as repair
    b = sample()
    original = deepcopy(b.floors[0].spec)
    def broken(*a):
        raise RuntimeError("not safe to accept")
    monkeypatch.setattr(repair,"_faults",broken)
    result = repair_building(b)
    assert result.status == "unverified" and result.error_type == "RuntimeError"
    assert not result.changes and b.floors[0].spec == original


def test_rejected_hard_faults_do_not_get_committed(monkeypatch):
    import src.design.ergonomics_repair as repair
    b = sample()
    original = deepcopy(b.floors[0].spec)
    monkeypatch.setattr(repair,"_faults",lambda spec,label:
                        set() if spec.fixtures == original.fixtures
                        else {("door_swing_blocked","new fault")})
    result = repair_building(b,max_attempts=24)
    assert not result.changes and b.floors[0].spec == original


def test_bedside_tables_belong_only_to_their_own_bed():
    a = FixturePlacement("bed_double",(1500,0),0)
    b = FixturePlacement("bed_double",(5000,0),0)
    left = FixturePlacement("nightstand",(425,0),0)
    right = FixturePlacement("nightstand",(6075,0),0)
    assert _companions(a,[a,b,left,right]) == [left]
    assert _companions(b,[a,b,left,right]) == [right]


def test_counter_and_double_bed_dimensions_are_retained():
    b = sample()
    b.floors[0].spec.fixtures += [Counter((0,4500),(3000,4500),depth=600),
                                 FixturePlacement("bed_double",(4000,0),0)]
    fixtures = b.floors[0].spec.fixtures
    counter = deepcopy(fixtures[2])
    repair_building(b,max_attempts=24)
    assert fixtures[2] == counter
    assert sum(f.name == "bed_double" for f in fixtures if isinstance(f,FixturePlacement)) == 1


def test_repair_has_finite_work_and_is_deterministic():
    a,b = sample(),sample()
    ra,rb = repair_building(a,max_attempts=12),repair_building(b,max_attempts=12)
    assert ra.attempts <= 12 and ra.to_dict() == rb.to_dict()
    assert a.floors[0].spec == b.floors[0].spec


def test_late_failure_restores_already_accepted_moves(monkeypatch):
    import src.design.ergonomics_repair as repair
    b = sample()
    original = deepcopy(b.floors[0].spec)
    analyze = repair.analyze_ergonomics
    def fail_at_final(building):
        # Single-floor local analyses use a temporary wrapper, final uses b.
        if building is b and b.floors[0].spec.fixtures != original.fixtures:
            raise RuntimeError("final measurement failed")
        return analyze(building)
    monkeypatch.setattr(repair,"analyze_ergonomics",fail_at_final)
    result = repair.repair_building(b)
    assert result.status == "unverified" and not result.changes
    assert b.floors[0].spec == original


def test_same_named_rooms_use_original_indices():
    b = sample()
    spec = b.floors[0].spec
    spec.rooms.insert(0,Room("測試房",[(-5000,0),(0,0),(0,5000),(-5000,5000)],kind="study"))
    result = repair_building(b)
    assert result.changes and all(c["room_index"] == 1 for c in result.changes)
