"""清單功能的幾何、數量、型號、真實比例、API 與歷史回復契約。"""
from copy import deepcopy
from dataclasses import asdict
import io
import json
import zipfile

import ezdxf
import pytest
from pydantic import ValidationError

from src.design.building_generator import BuildingBrief, BuildingSpec, FloorLevel, generate_building_auto
from src.design.layout_generator import HouseBrief
from src.design.drawing_profile import DrawingOptions, RoomCheck, apply_profile, build_report, net_rooms
from src.design.validation import validate_building
from src.drafting.apartment_plan import FloorPlanSpec, DoorPlacement, WindowPlacement
from src.drafting.wall import Wall, Opening
from src.drafting.room import Room
from src.drafting.door_window import Door, Window
from src.drafting.chengji import build_profile_sheets, PROFILE_MARKER


def rectangle(name="客廳", kind="living"):
    pts=[(0,0),(4000,0),(4000,5000),(0,5000)]
    return FloorPlanSpec(site_boundary=pts,setback=0,column_centers=[],rooms=[Room(name,pts,kind)],
                         walls=[Wall(a,b,200) for a,b in zip(pts,pts[1:]+pts[:1])])


def building(spec):
    return BuildingSpec([FloorLevel(1,0,spec)],3600)


def test_net_room_subtracts_walls_and_closes_openings():
    spec=rectangle()
    spec.walls[0].openings=[Opening(2000,1200,"door")]
    net=net_rooms(spec)[0]
    assert net.area == pytest.approx(3800*4800)
    assert net.length == pytest.approx(2*(3800+4800))


def test_net_room_subtracts_actual_column_and_preserves_concavity():
    spec=rectangle()
    spec.column_centers=[(0,0)]
    spec.column_size=400
    net=net_rooms(spec)[0]
    assert net.area == pytest.approx(3800*4800-100*100)
    report=build_report(building(spec),DrawingOptions()).to_dict()
    assert not report['floors'][0]['rooms'][0]['rectangular']
    assert len(report['floors'][0]['rooms'][0]['edges_cm'])==6


def test_exact_pdf_quantities_and_no_input_mutation():
    spec=rectangle('浴室','bathroom')
    before=asdict(spec)
    report=build_report(building(spec),DrawingOptions()).to_dict()
    q={r['item']:r['value'] for r in report['floors'][0]['quantities']}
    assert q['浴室牆面粉刷']==pytest.approx(17.2*3.6)
    assert q['室內牆面油漆']==pytest.approx(17.2*.4)
    assert q['浴室壁磚']==pytest.approx(17.2*2.4)
    assert q['浴室地磚']==pytest.approx(18.24)
    assert q['天花板']==pytest.approx(18.24)
    assert asdict(spec)==before


def test_curtain_uses_each_exterior_window_once():
    spec=rectangle()
    spec.walls[0].openings=[Opening(2000,1200,'window')]
    spec.windows=[WindowPlacement(0,0)]
    report=build_report(building(spec),DrawingOptions()).to_dict()
    q=report['floors'][0]['quantities'][-1]
    assert q['value']==pytest.approx((120+30)/(100/3.3))


def test_same_width_different_type_gets_distinct_codes_across_floors():
    spec=rectangle()
    spec.walls[0].openings=[Opening(1000,900,'door'),Opening(3000,900,'door')]
    spec.doors=[DoorPlacement(0,0,Door()),DoorPlacement(0,1,Door(sliding=True))]
    upper=deepcopy(spec)
    b=building(spec);b.floors.append(FloorLevel(2,3600,upper))
    report=build_report(b,DrawingOptions()).to_dict()
    assert len(report['openings'])==2
    assert [o['count'] for o in report['openings']]==[2,2]
    assert [o['code'] for o in report['floors'][0]['openings']]==[o['code'] for o in report['floors'][1]['openings']]


def test_room_constraints_reject_ambiguous_name_and_wrong_size():
    spec=rectangle()
    req=DrawingOptions(rooms=[RoomCheck(floor='1F',name='客廳',width_cm=380,depth_cm=480)])
    build_report(building(spec),req)
    req.rooms[0].width_cm=400
    with pytest.raises(ValueError,match='未符合指定淨尺寸'): build_report(building(spec),req)
    spec.rooms.append(deepcopy(spec.rooms[0]))
    with pytest.raises(ValueError,match='無法唯一辨識'): build_report(building(spec),req)


@pytest.mark.parametrize('fields',[{'ceiling_height_cm':400},{'window_sill_cm':200,'window_height_cm':200},
                                  {'exterior_wall_cm':float('nan')},{'bathroom_tile_cm':300}])
def test_invalid_settings_fail_closed(fields):
    with pytest.raises(ValidationError): DrawingOptions(**fields)


@pytest.fixture(scope='module')
def narrow():
    return generate_building_auto(BuildingBrief(HouseBrief(4500,14000,setback=0,dimension_basis='building'),floors=3,differentiated=True))


def test_profile_applies_actual_dimensions_and_revalidates(narrow):
    before=asdict(narrow)
    updated=apply_profile(narrow,DrawingOptions())
    assert asdict(narrow)==before
    assert updated.floor_height==3600
    assert updated.floors[2].elevation==7200
    assert all(f.spec.column_size==400 for f in updated.floors)
    assert validate_building(updated)['validation']['status']=='passed'


def test_saved_dxf_cm_viewport_and_shared_elevation_codes(tmp_path,narrow):
    updated=apply_profile(narrow,DrawingOptions())
    report=build_report(updated,DrawingOptions()).to_dict()
    sheets=build_profile_sheets(updated,report)
    assert {'section','elevation','opening_elevation','schedule','floor'} <= {s.kind for s in sheets}
    path=tmp_path/'floor.dxf';sheets[0].doc.saveas(path)
    doc=ezdxf.readfile(path)
    assert doc.units==ezdxf.units.CM
    assert not doc.audit().has_errors
    assert doc.header['$USERI1']==PROFILE_MARKER
    vp=[v for v in doc.layouts.get('Layout1').query('VIEWPORT') if v.dxf.id>1][0]
    assert vp.dxf.height/vp.dxf.view_height==pytest.approx(.2)
    assert doc.modelspace().query('INSERT')
    assert doc.modelspace().query('HATCH')
    codes={o['code'] for o in report['openings']}
    assert codes=={s.filename.removeprefix('opening_').removesuffix('.dxf') for s in sheets if s.kind=='opening_elevation'}
    section=next(s.doc for s in sheets if s.kind=='section')
    assert not section.modelspace().query('DIMENSION')
    assert any(e.dxf.text=='360' for e in section.modelspace().query('MTEXT'))


def test_api_profile_download_and_history(tmp_path,monkeypatch):
    from tests.test_web_app import _client, _payload
    import src.web.app as web
    monkeypatch.setattr(web,'JOBS_DIR',tmp_path)
    monkeypatch.setenv('RAG_ENABLED','0')
    client=_client(_payload(site_width_m=4.5,site_depth_m=14,dimension_basis='building',floors_above=3,bedrooms=4))
    response=client.post('/api/generate',json={'text':'透天三層，建築物4.5×14米，四房','seed':0,'drawing':{'enabled':True}})
    assert response.status_code==200,response.text[:2000]
    result=response.json()
    assert result['validation']['status']=='passed'
    assert result['drawing_report']['settings']['floor_height_cm']==360
    archive=zipfile.ZipFile(io.BytesIO(client.get(result['zip']).content))
    assert json.loads(archive.read('drawing_report.json'))==result['drawing_report']
    saved=json.loads((tmp_path/result['job_id']/'result.json').read_text(encoding='utf-8'))
    assert saved['brief_data']['_drawing']['enabled']
    assert saved['design_audit']['snapshot']['drawing_report']==result['drawing_report']


def test_pdf_preserves_physical_paper_size_after_reload(tmp_path):
    pdf = pytest.importorskip('pymupdf')
    from src.web.render import docs_to_pdf
    from src.drafting.chengji import elevation_doc
    doc=elevation_doc(dict(code='W1',type='橫拉窗',width_cm=120,height_cm=150,sill_cm=90,direction='',count=1))
    path=tmp_path/'window.dxf';doc.saveas(path)
    stored=ezdxf.readfile(path)
    target=tmp_path/'window.pdf';docs_to_pdf([stored],target)
    with pdf.open(target) as output:
        page=output[0]
        assert page.rect.width*25.4/72==pytest.approx(stored.header['$USERI2'],abs=.01)
        assert page.rect.height*25.4/72==pytest.approx(stored.header['$USERI3'],abs=.01)
        # 門窗框線寬120cm，以1:50輸出應是24mm，不能僅紙張大小正確。
        widths=[d['rect'].width*25.4/72 for d in page.get_drawings()]
        assert any(abs(w-24)<.03 for w in widths)
