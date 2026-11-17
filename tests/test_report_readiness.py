"""明天展示流程的實際回歸：外部服務失敗時退援，以及 PDF 可讀性。"""
import json
from types import SimpleNamespace
import pytest
import httpx
import ezdxf
from src.design import nl_parser as parser


def fake_client(outcomes, calls):
    def generate_content(**kwargs):
        calls.append(kwargs)
        response = outcomes.pop(0)
        if isinstance(response, Exception):
            raise response
        return SimpleNamespace(text=json.dumps(response))
    return SimpleNamespace(models=SimpleNamespace(generate_content=generate_content))


@pytest.fixture
def model_setup(monkeypatch):
    monkeypatch.setattr(parser, 'MODEL', 'primary')
    monkeypatch.setattr(parser, 'FALLBACK_MODEL', 'fallback')
    monkeypatch.setattr(parser.time, 'sleep', lambda _: None)


@pytest.mark.parametrize('error', [RuntimeError('503 UNAVAILABLE high demand'), httpx.ReadTimeout(''), TimeoutError()])
def test_busy_or_timeout_switches_model_with_bounded_requests(model_setup, error):
    calls=[]
    result=parser._call_llm('透天三層', fake_client([error,error,{'brief_type':'house'}], calls))
    assert result=={'brief_type':'house'}
    assert [c['model'] for c in calls]==['primary','primary','fallback']
    assert all(c['config']['http_options']['timeout'] <= 20000 for c in calls)
    assert all(c['config']['http_options']['retry_options']['attempts']==1 for c in calls)


def test_missing_model_uses_fallback_without_retrying_missing_model(model_setup):
    calls=[]
    parser._call_llm('需求',fake_client([RuntimeError('404 NOT_FOUND models/primary is not found'),{}],calls))
    assert [c['model'] for c in calls]==['primary','fallback']


@pytest.mark.parametrize('error',[RuntimeError('401 UNAUTHENTICATED API key not valid'),RuntimeError('403 PERMISSION_DENIED'),ValueError('invalid response schema')])
def test_invalid_credentials_or_schema_fail_immediately(model_setup,error):
    calls=[]
    with pytest.raises(type(error)):
        parser._call_llm('需求',fake_client([error],calls))
    assert len(calls)==1


def test_exhausted_models_stop_after_finite_retries(model_setup):
    calls=[]
    with pytest.raises(RuntimeError,match='503'):
        parser._call_llm('需求',fake_client([RuntimeError('503 UNAVAILABLE')]*4,calls))
    assert [c['model'] for c in calls]==['primary','primary','fallback','fallback']


def test_sdk_does_not_add_hidden_long_retries(monkeypatch):
    from google import genai
    from src.design.api_keys import _default_factory
    seen={}
    monkeypatch.setattr(genai,'Client',lambda **kw:seen.update(kw))
    _default_factory('not-a-real-key')
    options=seen['http_options']
    assert options.timeout==30000
    assert options.retry_options.attempts==1


def test_pdf_uses_a3_and_legible_strokes_for_different_aspect_ratios(tmp_path):
    pdf=pytest.importorskip('pymupdf')
    from src.web.render import docs_to_pdf
    docs=[]
    for x,y in [(4000,14000),(14000,4000)]:
        doc=ezdxf.new('R2010',setup=True)
        doc.modelspace().add_line((0,0),(x,y),dxfattribs={'lineweight':50,'color':1})
        doc.modelspace().add_text('PDF readable',dxfattribs={'height':200})
        docs.append(doc)
    path=tmp_path/'readable.pdf';docs_to_pdf(docs,path)
    with pdf.open(path) as book:
        assert len(book)==2
        for page in book:
            assert page.rect.width==pytest.approx(420/25.4*72,abs=.1)
            assert page.rect.height==pytest.approx(297/25.4*72,abs=.1)
            strokes=[p['width'] for p in page.get_drawings() if p['type'] in {'s','fs'}]
            assert strokes and max(strokes)==pytest.approx(.5*72/25.4,abs=.01)
    assert all(doc.modelspace().query('LINE')[0].dxf.color==1 for doc in docs)


def test_shallow_public_example_delivers_requested_two_bedrooms(monkeypatch,tmp_path):
    from fastapi.testclient import TestClient
    import src.web.app as web
    monkeypatch.setattr(web,'JOBS_DIR',tmp_path/'jobs')
    payload=dict(brief_type='house',site_width_m=8.5,site_depth_m=9.5,
                 dimension_basis='building',floors_above=2,bedrooms=2,basements=0,car_spaces=0)
    class Models:
        def generate_content(self,**kwargs):
            return SimpleNamespace(text=json.dumps(payload))
    client=TestClient(web.create_app(client_factory=lambda:SimpleNamespace(models=Models())))
    text='透天兩層,建築物8.5×9.5米,兩房'
    assert text in client.get('/').text
    response=client.post('/api/generate',json={'text':text,'seed':0})
    assert response.status_code==200,response.text
    data=response.json()
    assert data['requirement_check']['ok']
    assert data['plan_check']['ok'] and data['code_check']['ok']
    assert len(data['sheets'])==4
    assert {'1F','2F','剖面','立面'}=={s['label'] for s in data['sheets']}


def test_timeout_message_gives_retry_guidance_without_raw_error():
    from src.web.app import _quota_error
    error=_quota_error(httpx.ReadTimeout('internal transport detail'))
    assert error.status_code==504
    assert '稍後' in error.detail
    assert 'internal transport' not in error.detail


def test_summary_keeps_fractional_site_dimensions_and_labels_axis_span():
    from src.web.app import _summary
    from src.design.layout_generator import HouseBrief
    brief=SimpleNamespace(typical=HouseBrief(site_width=8500,site_depth=18000,bedrooms=1))
    spec=SimpleNamespace(rooms=[],x_spacings=[4000],y_spacings=[13500],grid_origin=(2250,2250),balconies=[])
    building=SimpleNamespace(floors=[SimpleNamespace(level=1,spec=spec)])
    text=_summary(brief,building)
    assert '基地 8.5×18 米' in text
    assert '建築軸線' in text


def test_pdf_download_rebuilds_legacy_cache_and_keeps_completed_cache(monkeypatch,tmp_path):
    from fastapi.testclient import TestClient
    import src.web.app as web
    monkeypatch.setattr(web,'JOBS_DIR',tmp_path)
    job=tmp_path/'0123456789ab';job.mkdir()
    (job/'plans.pdf').write_bytes(b'%PDF-legacy-unreadable')
    doc=ezdxf.new();doc.modelspace().add_line((0,0),(4000,14000));doc.saveas(job/'1F.dxf')
    (job/'meta.json').write_text(json.dumps({'files':['1F.dxf']}),encoding='utf-8')
    calls=[]
    def render(docs,path):
        calls.append(len(docs));path.write_bytes(b'%PDF-new-readable')
    monkeypatch.setattr(web,'docs_to_pdf',render)
    client=TestClient(web.create_app())
    for _ in range(2):
        response=client.get('/api/jobs/0123456789ab/pdf')
        assert response.status_code==200 and response.content==b'%PDF-new-readable'
    assert calls==[1]
    assert (job/'plans.pdf').read_bytes()==b'%PDF-legacy-unreadable'


def test_failed_pdf_render_is_not_reused_as_completed_download(monkeypatch,tmp_path):
    from fastapi.testclient import TestClient
    import src.web.app as web
    monkeypatch.setattr(web,'JOBS_DIR',tmp_path)
    job=tmp_path/'0123456789ab';job.mkdir()
    doc=ezdxf.new();doc.modelspace().add_line((0,0),(1,1));doc.saveas(job/'1F.dxf')
    (job/'meta.json').write_text(json.dumps({'files':['1F.dxf']}),encoding='utf-8')
    def broken(docs,path):
        path.write_bytes(b'%PDF-incomplete')
        raise RuntimeError('render failed')
    monkeypatch.setattr(web,'docs_to_pdf',broken)
    client=TestClient(web.create_app(),raise_server_exceptions=False)
    assert client.get('/api/jobs/0123456789ab/pdf').status_code==500
    monkeypatch.setattr(web,'docs_to_pdf',lambda docs,path:path.write_bytes(b'%PDF-completed'))
    response=client.get('/api/jobs/0123456789ab/pdf')
    assert response.status_code==200 and response.content==b'%PDF-completed'
    assert not list(job.glob('*.tmp'))
