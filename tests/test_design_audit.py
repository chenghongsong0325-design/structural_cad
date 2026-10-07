import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from src.web import app as web
from src.web.design_audit import build_report, digest, append_review, read_reviews, render_html


@pytest.fixture
def saved_job(tmp_path, monkeypatch):
    monkeypatch.setattr(web, 'JOBS_DIR', tmp_path)
    monkeypatch.setenv('ACCESS_CODE', 'private-code-never-exported')
    directory = tmp_path / ('a' * 12)
    directory.mkdir()
    saved = {'job_id': directory.name, 'brief_data': {'bedrooms': 3},
             'spatial_report': {'floors': [{'nodes': [{'polygon': [[1, 2], [3, 4]]}]}]},
             'requirement_check': {'items': [{'label': '臥室', 'expected': 3, 'actual': 3, 'status': 'met'}]}}
    saved['design_audit'] = build_report(saved, text='三房<script>alert(1)</script>')
    (directory / 'result.json').write_text(json.dumps(saved), encoding='utf-8')
    return TestClient(web.create_app(lambda: object())), directory, saved


def test_export_uses_saved_geometry_and_never_exports_access_code(saved_job):
    client, directory, saved = saved_job
    url = f'/api/jobs/{directory.name}/audit'
    assert client.post(url, json={}).status_code == 403
    response = client.post(url, json={'code': 'private-code-never-exported'})
    data = response.json()
    assert data['snapshot']['spatial_report'] == saved['spatial_report']
    assert data['snapshot_sha256'] == digest(data['snapshot'])
    assert 'private-code-never-exported' not in response.text
    assert data['human_reviews'] == []
    response = client.post(url, json={'code': 'private-code-never-exported', 'format': 'html'})
    assert response.status_code == 200 and 'attachment' in response.headers['content-disposition']
    assert '<script>' not in response.text and '&lt;script&gt;' in response.text


def test_review_idempotency_conflict_and_exported_history(saved_job):
    client, directory, saved = saved_job
    payload = {'code': 'private-code-never-exported', 'decision': 'revision_needed', 'reviewer': '老師代號',
               'reason': '入口仍需人工核對', 'request_id': uuid4().hex}
    url = f'/api/jobs/{directory.name}/reviews'
    assert client.post(url, json={**payload, 'code': ''}).status_code == 403
    first = client.post(url, json=payload)
    assert first.status_code == 200
    assert client.post(url, json=payload).json() == first.json()
    assert client.post(url, json={**payload, 'reason': '改寫原紀錄'}).status_code == 422
    assert client.post(url, json={**payload, 'reviewer': '  ', 'request_id': uuid4().hex}).status_code == 422
    rows = read_reviews(directory)
    assert len(rows) == 1 and rows[0]['snapshot_sha256'] == saved['design_audit']['snapshot_sha256']
    result = client.post(f'/api/jobs/{directory.name}/audit', json={'code': payload['code']}).json()
    assert result['human_reviews'] == rows
    assert json.loads((directory / 'result.json').read_text()) == saved
    assert not (directory / 'document.md').exists()  # never promotes feedback into RAG


def test_concurrent_reviews_do_not_lose_entries(saved_job):
    _, directory, saved = saved_job
    def save(i):
        return append_review(directory, report=saved['design_audit'], decision='accepted',
                             reviewer='核對者', reason=f'核對第{i}筆', request_id=uuid4().hex)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(save, range(12)))
    assert len(read_reviews(directory)) == 12


def test_parent_chain_preserves_original_input_and_records_actual_changes():
    parent = {'job_id': 'a' * 12, 'brief_data': {'bedrooms': 3}, 'requirement_check': {'ok': True}}
    parent['design_audit'] = build_report(parent, text='三房')
    child = build_report({'brief_data': {'bedrooms': 4}}, text='改四房', base=parent['brief_data'], parent=parent)
    assert child['snapshot']['original_input'] == '三房'
    assert child['snapshot']['changes'] == [{'field': 'bedrooms', 'before': 3, 'after': 4}]
    assert child['snapshot']['previous_requirement_check'] == {'ok': True}
    assert child['snapshot']['base_source'] == 'saved_parent'


def test_failed_candidate_gets_diagnostic_without_creating_success_job(tmp_path, monkeypatch):
    monkeypatch.setattr(web, 'JOBS_DIR', tmp_path)
    monkeypatch.delenv('ACCESS_CODE', raising=False)
    monkeypatch.setenv('RAG_ENABLED', '0')
    monkeypatch.setattr(web, 'parse_brief_data', lambda *a, **k: {'brief_type': 'house', 'dimension_basis': 'building',
         'site_width_m': 4.5, 'site_depth_m': 14, 'floors_above': 3, 'bedrooms': 3})
    def reject(*a, **k):
        raise HTTPException(422, {'message': '未满足必要需求', 'requirement_check': {'ok': False, 'items': []}})
    monkeypatch.setattr(web, '_generate_auto', reject)
    response = TestClient(web.create_app(lambda: object())).post('/api/generate', json={'text': '三房'})
    report = response.json()['detail']['design_audit']
    assert response.status_code == 422 and report['snapshot']['outcome'] == 'not_delivered'
    assert report['snapshot']['requirement_check']['ok'] is False
    assert list(tmp_path.iterdir()) == []


def test_parent_job_is_authoritative_before_modification(saved_job, monkeypatch):
    client, directory, saved = saved_job
    def parse(text, base, **kwargs):
        assert base == saved['brief_data']
        raise ValueError('stop after validating saved parent')
    monkeypatch.setattr(web, 'parse_modification_data', parse)
    response = client.post('/api/generate', json={'text': '改四房', 'code': 'private-code-never-exported',
        'parent_job_id': directory.name, 'base': {'bedrooms': 999}})
    assert response.status_code == 422 and 'saved parent' in response.text


def test_legacy_report_marks_missing_data_instead_of_inventing_reasons(saved_job):
    client, directory, saved = saved_job
    saved.pop('design_audit')
    (directory / 'result.json').write_text(json.dumps(saved), encoding='utf-8')
    response = client.post(f'/api/jobs/{directory.name}/audit', json={'code': 'private-code-never-exported'})
    report = response.json()
    assert report['snapshot']['outcome'] == 'legacy_record_incomplete'
    assert report['snapshot']['input_text'] is None and report['snapshot']['ai_trajectory'] is None
