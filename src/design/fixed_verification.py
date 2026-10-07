"""Deterministic regression scenarios, including deliberate broken layouts.

These verify detectors and supported examples, not human acceptance or AI benefit.
No model or network is called. Expectations are fixed independently of results.
"""
import copy
from dataclasses import asdict
from datetime import datetime, timezone
from src.design.building_generator import _narrow_to_building, FLOOR_HEIGHT
from src.design.layout.narrow_house import generate_narrow_building
from src.design.requirements import prepare_data, check_requirements
from src.design.validation import validate_building, validation_status


def run_fixed_cases():
    rows = []
    def record(case_id, title, expected, actual, passed, evidence, scope):
        rows.append({'id': case_id, 'title': title, 'expected': expected, 'actual': actual,
                     'passed': bool(passed), 'evidence': evidence, 'scope': scope})

    base = None
    for count, garage, case_id in [(3, False, 'narrow-three'), (4, False, 'narrow-four'), (3, True, 'garage-three')]:
        data = prepare_data({'bedrooms': count, 'floors_above': 3, 'site_width_m': 4.5, 'site_depth_m': 14,
                             'dimension_basis': 'building', **({'car_spaces': 1} if garage else {})}, '')
        building = _narrow_to_building(generate_narrow_building(4500, 14000, floors=3,
                          bedroom_target=count, garage=garage, seed=7), FLOOR_HEIGHT)
        checks = validate_building(building)
        needs = check_requirements(building, data['_requirements']).to_dict()
        ok = checks['validation']['status'] == 'passed' and needs['ok']
        record(case_id, f'4.5×14米三層{count}房' + ('一樓車庫' if garage else ''), '需求與現有檢查通過',
               checks['validation']['status'], ok, {'requirements': needs, **checks}, '規則配置；固定尺寸與 seed=7，未呼叫 LLM')
        if base is None:
            base = building

    from src.design.bedroom_program import BedroomCapacityError
    try:
        generate_narrow_building(4500, 14000, floors=1, bedroom_target=3, seed=7)
        capacity = None
    except BedroomCapacityError as exc:
        capacity = exc.conflict
    record('capacity-conflict', '單層三房超出目前上層分配容量', '回報 bedroom_capacity',
           (capacity or {}).get('code'), (capacity or {}).get("code") == "bedroom_capacity", capacity, '只代表此骨架容量，不證明所有方法都無解')

    wrong = check_requirements(base, prepare_data({'bedrooms': 4}, '')['_requirements']).to_dict()
    record('exact-count-mismatch', '三房圖面核對四房要求', '必要房數未滿足', wrong['ok'],
           not wrong['ok'], wrong, '以實際房間計數，不能以輸入欄位代替')

    from src.design.layout.plan_check import check_floor, building_env
    no_doors = copy.deepcopy(base.floors[0].spec)
    for wall in no_doors.walls:
        wall.openings = [op for op in wall.openings if op.kind != 'door']
    no_doors.doors = []
    issues = [asdict(i) for i in check_floor(no_doors, building_env(no_doors), 1, '1F')]
    codes = {i['code'] for i in issues}
    record('removed-doors', '故意移除一樓全部門', 'room_no_door 與 no_entry', sorted(codes),
           {'room_no_door', 'no_entry'} <= codes, issues, '測試既有 plan_check 偵測器；不是成功圖面')

    from src.drafting.fixtures import FixturePlacement
    furniture = copy.deepcopy(base.floors[0].spec)
    item = next(f for f in furniture.fixtures if isinstance(f, FixturePlacement))
    wall = furniture.walls[0]
    item.insert = wall.point_at(wall.length / 2)
    issues = [asdict(i) for i in check_floor(furniture, building_env(furniture), 1, '1F')]
    record('furniture-in-wall', '故意將家具插入牆體', 'furniture_in_wall', [i['code'] for i in issues],
           any(i['code'] == 'furniture_in_wall' for i in issues), issues, '測試碰撞偵測；未重新生成修復')

    from src.design.layout_validation import LayoutValidator
    overlap = copy.deepcopy(base.floors[1].spec)
    overlap.rooms.append(copy.deepcopy(overlap.rooms[0]))
    issues = [i.to_dict() for i in LayoutValidator(overlap).check_overlap()]
    record('room-overlap', '故意複製房間造成重疊', 'overlap error', issues,
           any(i['check'] == 'overlap' and i['severity'] == 'error' for i in issues), issues,
           '測試分析層 LayoutValidator；此結果不代表已加入最終出圖關卡')

    incomplete = validate_building(base)
    incomplete.pop('code_check', None)
    state = validation_status(incomplete)
    record('missing-check', '故意缺少尺寸檢查報告', 'unverified', state['status'],
           state['status'] == 'unverified', state, '不能把缺報告當成檢查通過')

    from src.web.app import _reject_if_broken
    from fastapi import HTTPException
    extras = validate_building(base)
    extras['requirement_check'] = wrong
    try:
        _reject_if_broken(extras, None)
        blocked = False
    except HTTPException as exc:
        blocked = exc.status_code == 422
    record('delivery-gate', '必要房數不符時不得交付', 'HTTP 422', 422 if blocked else '未阻擋',
           blocked, {'requirement_check': wrong}, '驗證網頁交付關卡；不用錯誤圖產生下載檔')

    return {'version': 'fixed-cases-v1', 'created_at': datetime.now(timezone.utc).isoformat(),
            'passed': sum(r['passed'] for r in rows), 'total': len(rows), 'cases': rows,
            'scope': '固定規則配置與偵測器回歸案例；不代表任意基地皆可行、真實 LLM 正確率或使用者效益。'}
