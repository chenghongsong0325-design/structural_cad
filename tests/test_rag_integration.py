"""Verify the actual Gemini call boundaries and API trace persistence, offline."""
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from src.knowledge import rag
from src.design import nl_parser
from src.design.layout import room_graph, townhouse_options
from src.web import app as web


@pytest.fixture
def references(monkeypatch):
    queries = []
    monkeypatch.setenv("RAG_ENABLED", "1")
    def search(query, stage="townhouse", top_k=3, *, filters=None):
        queries.append((query, stage))
        return rag.RetrievalReport(stage, "ready", [{
            "id": "evidence-1", "text": "門前應保留可以站立的空間。",
            "title": "動線", "section": "門口", "source": "knowledge/test.md",
            "path": "test.md", "line": 8,
        }])
    monkeypatch.setattr(rag, "get_retriever", lambda: SimpleNamespace(search=search))
    return queries


class Client:
    def __init__(self, payload=None):
        self.payload = payload or {"rationale": "測試"}
        self.calls = []
        self.models = self

    def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(text=json.dumps(self.payload))


@pytest.mark.parametrize("operation,stage,schema", [
    ("parse", "parse", nl_parser.BRIEF_SCHEMA),
    ("modify", "parse", nl_parser.BRIEF_SCHEMA),
    ("options", "townhouse", townhouse_options.TOWNHOUSE_OPTIONS_SCHEMA),
    ("refine_options", "townhouse", townhouse_options.TOWNHOUSE_OPTIONS_SCHEMA),
    ("graph", "graph", room_graph.ROOM_GRAPH_SCHEMA),
    ("refine_graph", "graph", room_graph.ROOM_GRAPH_SCHEMA),
])
def test_all_model_boundaries_receive_evidence(references, operation, stage, schema):
    client = Client()
    with rag.retrieval_session():
        if operation == "parse":
            nl_parser.parse_brief_data("透天三房", client=client)
        elif operation == "modify":
            nl_parser.parse_modification_data("改為四房", {"bedrooms": 3}, client=client)
        elif operation == "options":
            townhouse_options.propose_options("透天三房", width=5000, depth=15000, client=client)
        elif operation == "refine_options":
            townhouse_options.refine_options({}, ["門口不通"], width=5000, depth=15000, client=client)
        elif operation == "graph":
            room_graph.propose_room_graph("透天三房", client=client)
        else:
            room_graph.refine_room_graph({"long": "x" * 5000}, ["門口不通"], client=client)
        sent = client.calls[0]
        assert "evidence-1" in sent["contents"]
        assert "門前應保留" in sent["contents"]
        assert rag.RAG_POLICY in sent["config"]["system_instruction"]
        assert sent["config"]["response_schema"] == schema
        assert rag.trace_report()["events"][0]["stage"] == stage
    if operation.startswith("refine"):
        assert references[0][0] == "門口不通"  # long prior graph cannot bury the actual problem


def test_api_search_auth_validation_and_disabled_status(references, monkeypatch):
    monkeypatch.setenv("ACCESS_CODE", "test-code")
    client = TestClient(web.create_app(client_factory=Client))
    assert client.post("/api/rag/search", json={"query": "門"}).status_code == 403
    assert references == []
    valid = {"query": "門", "code": "test-code"}
    assert client.post("/api/rag/search", json={**valid, "top_k": 99}).status_code == 422
    assert client.post("/api/rag/search", json={**valid, "stage": "bad"}).status_code == 422
    result = client.post("/api/rag/search", json=valid)
    assert result.status_code == 200 and result.json()["sources"][0]["id"] == "evidence-1"
    monkeypatch.setenv("RAG_ENABLED", "0")
    assert client.get("/api/rag/status").json()["status"] == "disabled"


def test_generation_persists_actual_trace_and_does_not_leak_across_jobs(references, monkeypatch, tmp_path):
    # Exercise real parser + option prompt calls, isolate expensive drawing for this contract test.
    monkeypatch.delenv("ACCESS_CODE", raising=False)
    monkeypatch.setattr(web, "JOBS_DIR", tmp_path)
    monkeypatch.setattr(web, "building_brief_from_data", lambda *a, **k: SimpleNamespace(typical=None))
    def generate(text, brief, client, **kwargs):
        townhouse_options.propose_options(text, width=5000, depth=15000, client=client)
        return object(), {}
    monkeypatch.setattr(web, "_generate_auto", generate)
    monkeypatch.setattr(web, "build_sheets", lambda _: [])
    monkeypatch.setattr(web, "building_metrics", lambda _: {})
    monkeypatch.setattr(web, "house_design_note", lambda _: "test")
    monkeypatch.setattr(web, "_summary", lambda *a: "test")
    monkeypatch.setattr(web, "_suggestions", lambda *a: [])
    client = TestClient(web.create_app(client_factory=Client))
    for _ in range(2):
        response = client.post("/api/generate", json={"text": "透天三房"})
        assert response.status_code == 200, response.text
        data = response.json()
        assert [e["stage"] for e in data["rag"]["events"]] == ["parse", "townhouse"]
        saved = json.loads((tmp_path / data["job_id"] / "result.json").read_text(encoding="utf-8"))
        assert saved["rag"] == data["rag"]
    assert rag.trace_report()["events"] == []
