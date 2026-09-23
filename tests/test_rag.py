"""RAG persistence, filtering and failure isolation; no network/model download."""
from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3
from pathlib import Path

import numpy as np
import pytest

from src.knowledge import rag


class FakeEmbedder:
    signature = "test-embedding-v1"

    def __init__(self):
        self.calls = []

    def encode(self, texts, *, query=False):
        self.calls.append((query, list(texts)))
        values = np.zeros((len(texts), rag.DIMENSIONS), dtype=np.float32)
        for i, text in enumerate(texts):
            topic = (0 if any(w in text for w in ("車庫", "停車")) else
                     1 if any(w in text for w in ("採光", "天井")) else 2)
            values[i, topic] = 1
        return values


def document(directory, name="parking", body="## 配置\n車庫位於一樓。",
             scopes="townhouse, graph"):
    directory.mkdir(exist_ok=True)
    path = directory / f"{name}.md"
    path.write_text(f'---\nid: {name}\ntitle: 設計說明\nsource: source/{name}\n'
                    f'updated: "2026-09-18"\nscopes: [{scopes}]\n---\n{body}\n',
                    encoding="utf-8")
    return path


@pytest.fixture
def corpus(tmp_path):
    directory = tmp_path / "knowledge"
    document(directory)
    document(directory, "light", "## 配置\n採光與天井。")
    document(directory, "terms", "## 術語\n車庫與地下層不同。", "parse")
    return directory


@pytest.fixture
def engine(corpus, tmp_path, monkeypatch):
    result = rag.Retriever(corpus, tmp_path / "index.sqlite3", FakeEmbedder())
    monkeypatch.setenv("RAG_ENABLED", "1")
    monkeypatch.setattr(rag, "get_retriever", lambda: result)
    return result


def test_stage_filter_and_ranked_sources(engine):
    found = engine.search("希望可以停車", "townhouse")
    assert [s["document_id"] for s in found.sources] == ["parking"]
    assert found.sources[0]["source"] == "source/parking"
    assert found.sources[0]["score"] == pytest.approx(1)
    assert engine.search("停車", "parse").sources[0]["document_id"] == "terms"
    assert engine.search("天井", "townhouse").sources[0]["document_id"] == "light"
    assert engine.search("量子力學", "townhouse").status == "no_match"
    assert engine.search(" ").status == "no_match"


def test_chunk_boundaries_unique_ids_and_line_numbers(tmp_path):
    path = document(tmp_path, body="## 重複\n\n車庫\n## 重複\n車庫\n## 長段\n" + "長" * 800)
    chunks = rag.read_corpus(tmp_path)
    assert len(chunks) == 5
    assert len({c.id for c in chunks}) == 5
    assert all(len(c.text) <= rag.CHUNK_SIZE for c in chunks)
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[chunks[0].line - 1] == "車庫"
    assert lines[chunks[1].line - 1] == "車庫"


def test_only_curated_markdown_is_read(corpus):
    (corpus / "README.md").write_text("instructions", encoding="utf-8")
    (corpus / "api_keys.json").write_text('{"secret":"not evidence"}', encoding="utf-8")
    assert len(rag.read_corpus(corpus)) == 3


@pytest.mark.parametrize("change", ["missing", "duplicate", "bad_scope"])
def test_invalid_metadata_is_rejected(corpus, change):
    target = corpus / "bad.md"
    raw = (corpus / "parking.md").read_text(encoding="utf-8")
    if change == "missing":
        raw = "# 沒有 metadata"
    elif change == "bad_scope":
        raw = raw.replace("id: parking", "id: bad").replace("[townhouse, graph]", "[{}]")
    target.write_text(raw, encoding="utf-8")
    with pytest.raises(ValueError):
        rag.read_corpus(corpus)


def test_saved_index_reuse_update_and_delete(engine, corpus):
    original = engine.status()["fingerprint"]
    second = rag.Retriever(corpus, engine.index_path, FakeEmbedder())
    assert second.status()["fingerprint"] == original
    assert second.embedder.calls == []  # no document encoding on an unchanged index
    document(corpus, "parking", "## 配置\n車庫更新內容。")
    updated = second.status()["fingerprint"]
    assert updated != original
    assert len(second.embedder.calls) == 1
    (corpus / "parking.md").unlink()
    assert second.status()["fingerprint"] != updated
    assert second.search("停車", "townhouse").sources == []


def test_corrupt_vector_rebuilds(engine, corpus):
    engine.status()
    with sqlite3.connect(engine.index_path) as db:
        db.execute("UPDATE chunks SET vector = ?", (b"bad",))
    second = rag.Retriever(corpus, engine.index_path, FakeEmbedder())
    assert second.search("停車").status == "ready"
    assert second.embedder.calls[0][0] is False


@pytest.mark.parametrize("value", [np.zeros((1, 384)), np.ones((1, 12)),
                                   np.full((1, 384), np.nan)])
def test_invalid_vectors_cannot_enter_index(value):
    with pytest.raises(ValueError):
        rag._vectors(value, 1)


def test_missing_model_does_not_report_ready_from_saved_index(engine, corpus, tmp_path):
    engine.status()
    embedder = rag.E5Embedder(tmp_path / "missing")
    embedder.signature = engine.embedder.signature
    second = rag.Retriever(corpus, engine.index_path, embedder)
    with pytest.raises(FileNotFoundError):
        second.status()


def test_disabled_and_failed_retrieval_keep_original_prompt(engine, monkeypatch):
    monkeypatch.setenv("RAG_ENABLED", "0")
    assert rag.augment_prompt("要停車", "townhouse") == "要停車"
    assert engine.embedder.calls == []
    monkeypatch.setenv("RAG_ENABLED", "1")
    def fail():
        raise OSError("private local path or secret")
    monkeypatch.setattr(rag, "get_retriever", fail)
    with rag.retrieval_session():
        assert rag.augment_prompt("要停車", "townhouse") == "要停車"
        trace = rag.trace_report()
        assert trace["status"] == "unavailable"
        assert "private" not in json.dumps(trace)


def test_trace_has_only_appended_sources_and_resets(engine, monkeypatch):
    monkeypatch.setattr(rag, "CONTEXT_LIMIT", 10)
    with rag.retrieval_session():
        assert rag.augment_prompt("停車", "townhouse") == "停車"
        assert rag.trace_report()["events"][0]["sources"] == []
    monkeypatch.setattr(rag, "CONTEXT_LIMIT", 3600)
    with pytest.raises(RuntimeError), rag.retrieval_session():
        prompt = rag.augment_prompt("停車", "townhouse")
        source = rag.trace_report()["events"][0]["sources"][0]
        assert source["id"] in prompt and source["text"] in prompt
        raise RuntimeError("abort")
    assert rag.trace_report()["events"] == []


def test_concurrent_requests_have_separate_traces(engine):
    def run(query):
        with rag.retrieval_session():
            rag.augment_prompt(query, "townhouse")
            return rag.trace_report()["events"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = list(pool.map(run, ["停車", "採光"]))
    assert len(first) == len(second) == 1
    assert first[0]["sources"][0]["document_id"] == "parking"
    assert second[0]["sources"][0]["document_id"] == "light"


def test_empty_corpus_and_report_serialization(tmp_path):
    retriever = rag.Retriever(tmp_path, tmp_path / "index.sqlite3", FakeEmbedder())
    assert retriever.status()["status"] == "empty"
    report = retriever.search("停車")
    assert report.status == "no_match" and report.summary()
    assert json.loads(report.to_json())["sources"] == []
