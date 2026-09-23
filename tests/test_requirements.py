from types import SimpleNamespace as NS
import pytest
from fastapi import HTTPException

from src.design.requirements import prepare_data, check_requirements, option_locks
from src.design.validation import validate_building, validation_status


def building(bedrooms=3, garage=False):
    def room(kind, index):
        return NS(kind=kind, name=f"{kind}{index}", points=[(0, 0), (3000, 0), (3000, 3000), (0, 3000)])
    rooms = [room("bedroom", i) for i in range(bedrooms)]
    if garage:
        rooms.append(room("garage", 1))
    spec = NS(rooms=rooms, fixtures=[NS(name="car")] if garage else [],
              x_spacings=[6000], y_spacings=[12000])
    return NS(floors=[NS(level=1, label="1F", spec=spec)], floor_height=3200)


def contract(**fields):
    return prepare_data(fields, "")['_requirements']


def test_required_and_preferred_are_bound_to_original_clause():
    data = prepare_data({"garage": True, "bedrooms": 3, "patio": True,
                         "requirement_details": [{"field": "garage", "priority": "preferred", "quote": "可取消車庫"}]},
                        "三房，一定要車庫，最好有天井")
    by = {r['field']: r for r in data['_requirements']}
    assert by['garage']['priority'] == 'required'
    assert by['garage']['source'] == '一定要車庫'
    assert by['patio']['priority'] == 'preferred'
    assert option_locks(data['_requirements']) == {'bedrooms': 3, 'garage': True}


def test_modification_cannot_change_unmentioned_requirements():
    base = prepare_data({'bedrooms': 3, 'garage': True, 'floors_above': 3}, '三房，車庫，三層')
    next_data = prepare_data({'bedrooms': 2, 'garage': False, 'floors_above': 1}, '改二房', base)
    assert next_data['bedrooms'] == 2
    assert next_data['garage'] is True and next_data['floors_above'] == 3
    assert option_locks(next_data['_requirements'])['garage'] is True


def test_neighbouring_preference_does_not_demote_bedrooms():
    data = prepare_data({'bedrooms': 3, 'patio': True}, '三房最好有天井')
    by = {r['field']: r for r in data['_requirements']}
    assert by['bedrooms']['priority'] == 'required'
    assert by['patio']['priority'] == 'preferred'
    bounded = prepare_data({'bedrooms': 3, 'floors_above': 2}, '三房至少兩層')
    by = {r['field']: r for r in bounded['_requirements']}
    assert by['bedrooms']['operator'] == 'eq' and by['floors_above']['operator'] == 'min'


def test_literal_count_catches_parser_mismatch_and_records_evidence():
    data = prepare_data({'bedrooms': 3, 'floors_above': 2}, '四房，三層')
    assert data['bedrooms'] == 4 and data['floors_above'] == 3
    assert len(data['_parse_corrections']) == 2
    base = prepare_data({'bedrooms': 3, 'floors_above': 3}, '三房，三層')
    relative = prepare_data({'bedrooms': 4}, '增加一房', base)
    assert relative['bedrooms'] == 4  # relative counts are not a literal target
    location = prepare_data({'garage': True, 'floors_above': None}, '一樓要車庫')
    assert location['floors_above'] is None  # floor location is not storey count


def test_explicit_cancel_remains_a_prohibition():
    data = prepare_data({'garage': None, 'basements': None}, '不要車庫，取消地下室')
    assert data['garage'] is False and data['basements'] == 0
    result = check_requirements(building(garage=True), data['_requirements'])
    assert not result.ok
    base = prepare_data({'garage': True, 'car_spaces': 1}, '需要車庫與一個車位')
    cancelled = prepare_data({'garage': False, 'car_spaces': 0}, '不要車庫', base)
    assert cancelled['car_spaces'] == 0 and cancelled['garage'] is False


@pytest.mark.parametrize('phrase,priority', [('車庫不可取消', 'required'),
    ('不要取消車庫', 'required'), ('車庫可取消', 'preferred'), ('車庫可以取消', 'preferred')])
def test_permission_to_relax_is_not_an_instruction_to_remove(phrase, priority):
    data = prepare_data({'garage': True}, phrase)
    row = next(r for r in data['_requirements'] if r['field'] == 'garage')
    assert data['garage'] is True and row['priority'] == priority


@pytest.mark.parametrize('count,expected', [(2, False), (3, True), (4, False)])
def test_exact_bedroom_count_uses_rooms_not_options(count, expected):
    result = check_requirements(building(count), contract(bedrooms=3))
    assert result.ok is expected
    assert result.to_dict()['items'][0]['actual'] == count
    assert len(result.items[0]['evidence']) == count


def test_at_least_requires_explicit_user_wording():
    requirements = prepare_data({'bedrooms': 3}, '至少三房')['_requirements']
    assert check_requirements(building(4), requirements).ok
    assert not check_requirements(building(2), requirements).ok


@pytest.mark.parametrize('field,phrase,value,operator,priority', [
    ('car_spaces', '至少兩個車位', 2, 'min', 'required'),
    ('car_spaces', '最多兩個車位', 2, 'max', 'required'),
    ('car_spaces', '最好有兩個車位', 2, 'eq', 'preferred'),
    ('bedrooms', '至少三間臥室', 3, 'min', 'required'),
])
def test_quantity_phrase_keeps_its_qualifier(field, phrase, value, operator, priority):
    record = prepare_data({field: value}, phrase)['_requirements'][0]
    assert record['operator'] == operator and record['priority'] == priority


def test_preferences_do_not_block_but_still_report_unmet():
    requirements = prepare_data({'garage': True}, '最好有車庫')['_requirements']
    result = check_requirements(building(), requirements)
    assert result.ok and result.items[0]['status'] == 'unmet'


def test_basement_and_parking_are_not_inferred_from_input():
    result = check_requirements(building(), contract(basements=1, car_spaces=2))
    assert not result.ok
    assert all(i['actual'] == 0 and i['status'] == 'unmet' for i in result.items)


def test_unmeasured_required_condition_is_unverified():
    result = check_requirements(building(), contract(units_per_row=4))
    assert not result.ok and result.items[0]['status'] == 'unverified'


@pytest.mark.parametrize('value', [-1, 1.5, True, float('nan')])
def test_invalid_count_cannot_be_silently_clamped(value):
    with pytest.raises(ValueError):
        prepare_data({'bedrooms': value}, '臥室數')


def good_checks():
    return {'plan_check': {'ok': True, 'n_errors': 0, 'issues': []},
            'code_check': {'ok': True, 'n_violations': 0, 'issues': []}}


@pytest.mark.parametrize('report', [{}, {'plan_check': {'ok': True}},
                                  {'plan_check': {'ok': True, 'n_errors': 0, 'issues': []}}])
def test_missing_checks_never_pass(report):
    from src.web.app import _reject_if_broken
    with pytest.raises(HTTPException) as exc:
        _reject_if_broken(report, NS())
    assert exc.value.status_code == 503
    assert exc.value.detail['validation']['status'] == 'unverified'


def test_inconsistent_validator_report_is_not_a_pass():
    reports = good_checks()
    reports['plan_check']['ok'] = False
    assert validation_status(reports)['status'] == 'unverified'


@pytest.mark.parametrize('broken', ['plan_check', 'code_check'])
def test_check_exception_preserves_other_report_and_stops_output(monkeypatch, broken):
    from src.design.layout import plan_check, code_check
    monkeypatch.setattr(plan_check, 'check_building', lambda *a: NS(to_dict=lambda: good_checks()['plan_check']))
    monkeypatch.setattr(code_check, 'check_code_building', lambda *a: NS(to_dict=lambda: good_checks()['code_check']))
    def fail(*args):
        raise RuntimeError('private path must not be exposed')
    monkeypatch.setattr(plan_check if broken == 'plan_check' else code_check,
                        'check_building' if broken == 'plan_check' else 'check_code_building', fail)
    report = validate_building(building())
    assert report['validation']['status'] == 'unverified'
    assert report['validation']['failures'][0]['check'] == broken
    assert 'private' not in str(report)
    assert ('code_check' if broken == 'plan_check' else 'plan_check') in report


def test_required_garage_is_not_removed_by_normalization():
    from src.design.layout.townhouse_options import build_from_options
    with pytest.raises(ValueError, match='必要條件'):
        build_from_options(4500, 10000, {'garage': True, 'floors': 3}, required={'garage': True})


def test_required_values_are_passed_to_every_attempt(monkeypatch):
    from src.design.layout import townhouse_options as options, plan_check
    calls = []
    def generate(*a, **kw):
        calls.append(kw)
        if kw['core_style'] != 'default':
            raise ValueError('try another core')
        return [('1F', NS())]
    monkeypatch.setattr(options, 'generate_narrow_building', generate)
    monkeypatch.setattr(plan_check, 'check_building', lambda *a: NS(ok=True))
    _, used = options.build_from_options(7000, 17000, {'floors': 4, 'bedrooms': 4, 'garage': False},
                                        required={'floors': 3, 'bedrooms': 2, 'garage': True})
    assert used['garage'] is True
    assert all(c['garage'] and c['floors'] == 3 and c['bedrooms'] == 2 for c in calls)


def test_feasible_candidate_wins_over_higher_fitness(monkeypatch):
    from src.design.layout import townhouse_options as options, design_loop, global_score, plan_check
    monkeypatch.setattr(options, 'propose_options', lambda *a, **k: {'core_style': 'ref'})
    monkeypatch.setattr(options, 'refine_options', lambda *a, **k: {'core_style': 'mid'})
    monkeypatch.setattr(options, 'build_from_options', lambda w, d, opts, **k: ([('1F', opts)], opts))
    monkeypatch.setattr(global_score, 'score_report', lambda sp: {'overall_score': 99 if sp['core_style'] == 'ref' else 60})
    monkeypatch.setattr(plan_check, 'building_env', lambda sp: (0, 0, 7000, 15000))
    monkeypatch.setattr(design_loop, 'critique_building', lambda *a: ['continue'])
    monkeypatch.setattr(options, 'assess_candidate', lambda fl, r: {
        'feasible': fl[0][1]['core_style'] == 'mid', 'requirement_check': {}, 'problems': []})
    best, history = options.design_townhouse('三房', 7000, 15000, verbose=False, requirements=contract(bedrooms=3))
    assert best['options']['core_style'] == 'mid'
    assert history[0]['fitness'] > history[1]['fitness']


def test_api_does_not_render_or_write_job_when_requirements_fail(monkeypatch, tmp_path):
    import src.web.app as web
    from fastapi.testclient import TestClient
    monkeypatch.delenv('ACCESS_CODE', raising=False)
    monkeypatch.setattr(web, 'parse_brief_data', lambda *a, **k: {'brief_type': 'house', 'site_width_m': 10,
                        'site_depth_m': 16, 'bedrooms': 3, 'dimension_basis': 'building'})
    monkeypatch.setattr(web, '_ai_applicable', lambda brief: False)
    monkeypatch.setattr(web, 'generate_building_auto', lambda brief: building(2))
    monkeypatch.setattr('src.design.validation.validate_building', lambda *a, **k: good_checks())
    monkeypatch.setattr(web, 'JOBS_DIR', tmp_path / 'jobs')
    monkeypatch.setattr(web, 'build_sheets', lambda *a: pytest.fail('unmet requirement must stop before rendering'))
    response = TestClient(web.create_app(lambda: object())).post('/api/generate', json={'text': '三房'})
    assert response.status_code == 422
    report = response.json()['detail']['requirement_check']
    assert not report['ok'] and next(i for i in report['items'] if i['field'] == 'bedrooms')['actual'] == 2
    assert not (tmp_path / 'jobs').exists()


def test_refinement_receives_original_required_condition():
    import json
    from src.design.layout.townhouse_options import refine_options
    calls = []
    def generate(**kw):
        calls.append(kw)
        return NS(text=json.dumps({}))
    refine_options({'garage': False}, ['門口不通'], width=7000, depth=15000,
                   client=NS(models=NS(generate_content=generate)), requirements=contract(garage=True))
    assert 'required' in calls[0]['contents'] and '"expected": true' in calls[0]['contents']


def test_cached_score_is_returned_without_reconstructing_another_plan(monkeypatch, tmp_path):
    import json
    from fastapi.testclient import TestClient
    import src.web.app as web
    job = 'aabbccddeeff'
    folder = tmp_path / job
    folder.mkdir()
    score = {'job_id': job, 'overall_score': 73.2, 'grade': 'B'}
    (folder / 'result.json').write_text(json.dumps({'layout_score': score, 'ai_design': True}), encoding='utf-8')
    monkeypatch.setattr(web, 'JOBS_DIR', tmp_path)
    monkeypatch.setattr(web, 'generate_building_auto', lambda *a: pytest.fail('must use the actual saved score'))
    monkeypatch.delenv('ACCESS_CODE', raising=False)
    result = TestClient(web.create_app()).post('/api/score', json={'job_id': job})
    assert result.status_code == 200 and result.json() == score
