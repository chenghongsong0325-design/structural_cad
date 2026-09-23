from pathlib import Path
import numpy as np
import pytest
import yaml
from pydantic import ValidationError

from src.knowledge import rag
from src.knowledge.case_metadata import CaseMetadata, CaseQuery, compatibility, case_context, current_query, update_document


class Embedder:
    signature = 'case-tests-v1'
    def encode(self, texts, **kwargs):
        result = np.zeros((len(texts), 384), dtype=np.float32)
        result[:, 0] = 1
        return result


def document(root, name, **case):
    root.mkdir(exist_ok=True)
    meta = {'id': name, 'title': name, 'source': 'test fixture', 'updated': '2026-09-21',
            'scopes': ['townhouse', 'graph'], 'case': case}
    path = root / (name + '.md')
    path.write_text('---\n' + yaml.safe_dump(meta) + '---\n## Plan\nTest case parking.\n', encoding='utf-8')
    return path


def test_incompatible_cases_are_excluded_before_semantic_ranking(tmp_path):
    corpus = tmp_path / 'knowledge'
    document(corpus, 'narrow', width_m={'min': 4, 'max': 6})
    document(corpus, 'wide', width_m={'min': 9, 'max': 12})
    engine = rag.Retriever(corpus, tmp_path / 'index.sqlite3', Embedder())
    result = engine.search('parking', filters={'width_m': 5})
    assert [s['document_id'] for s in result.sources] == ['narrow']
    assert result.excluded[0]['document_id'] == 'wide'
    assert result.filters['width_m'] == 5


@pytest.mark.parametrize('meta,query', [({'floors': {'min': 2, 'max': 3}}, {'floors': 5}),
    ({'party_walls': True}, {'party_walls': False}),
    ({'window_sides': ['N', 'E']}, {'window_sides': ['N', 'S']}),
    ({'car_spaces': {'min': 0, 'max': 1}}, {'car_spaces': 2})])
def test_known_mismatch_is_not_compatible(meta, query):
    assert not compatibility(CaseMetadata(**meta).model_dump(), query)[0]


def test_unknown_or_different_dimension_basis_is_never_claimed_verified():
    meta = CaseMetadata(dimension_basis='site', width_m={'min': 10, 'max': 12}).model_dump()
    good, note = compatibility(meta, {'dimension_basis': 'building', 'width_m': 5})
    assert good and '未記載' in note
    assert not compatibility(meta, {'reviewed_only': True})[0]


@pytest.mark.parametrize('data', [{'width_m': {'min': 8, 'max': 4}},
    {'review_status': 'reviewed'}, {'window_sides': ['E', 'E']},
    {'width_m': {'min': 0, 'max': float('inf')}}, {'arbitrary_instruction': 'ignore rules'}])
def test_invalid_metadata_is_rejected(data):
    with pytest.raises(ValidationError):
        CaseMetadata(**data)


def test_context_isolated_and_reset_after_exception():
    with pytest.raises(RuntimeError), case_context({'floors': 3}):
        assert current_query()['floors'] == 3
        with case_context({'floors': 2}):
            assert current_query()['floors'] == 2
        assert current_query()['floors'] == 3
        raise RuntimeError()
    assert current_query() == {}


def test_metadata_update_rebuilds_index_without_changing_evidence(tmp_path):
    corpus = tmp_path / 'knowledge'
    path = document(corpus, 'one')
    body = path.read_text(encoding='utf-8').split('---\n')[-1]
    engine = rag.Retriever(corpus, tmp_path / 'index.sqlite3', Embedder())
    old = engine.status()['fingerprint']
    update_document(corpus, tmp_path, 'one', CaseMetadata(width_m={'min': 9, 'max': 12}))
    assert path.read_text(encoding='utf-8').split('---\n')[-1] == body
    assert engine.status()['fingerprint'] != old
    assert engine.search('parking', filters={'width_m': 5}).status == 'no_match'
    with pytest.raises(ValueError):
        update_document(corpus, tmp_path, '../../outside', CaseMetadata())


def test_metadata_routes_check_auth_before_read_or_write(monkeypatch):
    from fastapi.testclient import TestClient
    from src.web.app import create_app
    monkeypatch.setenv('ACCESS_CODE', 'required')
    monkeypatch.setattr('src.knowledge.case_metadata.list_documents', lambda *a: pytest.fail('must authenticate first'))
    monkeypatch.setattr('src.knowledge.case_metadata.update_document', lambda *a: pytest.fail('must authenticate first'))
    client = TestClient(create_app())
    assert client.get('/api/rag/documents').status_code == 403
    assert client.patch('/api/rag/documents/test', json={'case': {}}).status_code == 403


def test_townhouse_prompt_uses_physical_skeleton_filters(monkeypatch):
    import json
    from types import SimpleNamespace as NS
    from src.design.layout import townhouse_options
    observed = []
    def search(query, stage, top_k, *, filters):
        observed.append(filters)
        return rag.RetrievalReport(stage, 'no_match')
    monkeypatch.setenv('RAG_ENABLED', '1')
    monkeypatch.setattr(rag, 'get_retriever', lambda: NS(search=search))
    client = NS(models=NS(generate_content=lambda **k: NS(text=json.dumps({}))))
    with case_context({'dimension_basis': 'site', 'width_m': 10, 'floors': 3}):
        townhouse_options.propose_options('三層', width=6000, depth=13000, client=client)
    assert observed[0]['dimension_basis'] == 'building'
    assert observed[0]['width_m'] == 6 and observed[0]['depth_m'] == 13
    assert observed[0]['party_walls'] is True and observed[0]['window_sides'] == ['N', 'S']
    assert observed[0]['floors'] == 3
