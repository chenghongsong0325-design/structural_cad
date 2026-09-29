"""No external requests: public-address guards, extraction, citations and API."""
import json
from pathlib import Path
import socket
import time

import numpy as np
import pytest
from fastapi.testclient import TestClient

from src.knowledge import public_web as net, rag, web_research as web

REAL_REFERENCE_CATALOG = web.REFERENCE_CATALOG


@pytest.fixture(autouse=True)
def isolated_catalog(monkeypatch):
    # Existing unit scenarios exercise search alone; catalog tests opt in below.
    monkeypatch.setattr(web, "REFERENCE_CATALOG", ())


def test_irrelevant_provider_results_continue_fallback(monkeypatch):
    calls = fake_search(monkeypatch, {
        "brave": [{"href": "https://who.int/covid", "title": "Coronavirus disease COVID-19"}],
        "duckduckgo": [{"href": "https://drugs.com/baclofen", "title": "Baclofen uses"}],
        "yahoo": [{"href": "https://design.example/garage", "title": "案例", "body": "住宅車庫動線"}],
    })
    attempts = []
    result = web.search_sources("住宅 車庫 玄關 動線", attempts=attempts)
    assert calls == list(web.SEARCH_BACKENDS)
    assert [a["status"] for a in attempts] == ["unrelated", "unrelated", "ok"]
    assert result == [{"url": "https://design.example/garage", "title": "案例"}]


def test_catalog_is_topic_limited(monkeypatch):
    monkeypatch.setattr(web, "REFERENCE_CATALOG", REAL_REFERENCE_CATALOG)
    assert len(web.reference_sources("住宅 車庫 玄關 動線")) == 2
    assert web.reference_sources("透天住宅 採光 平面圖")
    assert web.reference_sources("巧克力蛋糕") == []
    assert web.reference_sources("COVID Baclofen") == []


@pytest.mark.parametrize("failure", ["unavailable", "unrelated", "unreadable"])
def test_catalog_fetches_real_body_and_records_discovery(store, monkeypatch, failure):
    monkeypatch.setattr(web, "REFERENCE_CATALOG", REAL_REFERENCE_CATALOG)
    def search(query, **kwargs):
        if failure == "unavailable":
            raise net.WebFailure("搜尋連線失敗")
        if failure == "unreadable":
            return [{"url": "https://design.example/blocked", "title": "住宅"}]
        return []
    monkeypatch.setattr(web, "search_sources", search)
    reads = []
    def read(candidate, cache):
        reads.append(candidate["url"])
        if candidate["url"].endswith("blocked"):
            raise net.WebFailure("網站不允許")
        title, evidence, metadata = web.html_evidence(html_page())
        return title, candidate["url"], evidence, metadata
    monkeypatch.setattr(web, "read_source", read)
    monkeypatch.setattr(web, "update_index", lambda: {"status": "unavailable"})
    result = web.research("住宅 車庫 玄關 動線", store[0], limit=1)
    assert result["status"] == "stored" and result["catalog_used"]
    assert "非即時搜尋" in result["provider"]
    assert result["search_attempts"][-1] == {"provider": "reference_catalog", "status": "catalog"}
    assert reads[-1] == REAL_REFERENCE_CATALOG[0]["url"]
    manifest = json.loads(next((store[0] / "imports").glob("*/manifest.json")).read_text(encoding="utf-8"))
    assert manifest["origin"]["discovery"] == "reference_catalog"
    assert manifest["origin"]["url"] == reads[-1]
    assert "行人動線" in manifest["report"]["evidence"][0]["text"]


@pytest.mark.parametrize("blocked", [False, True])
def test_catalog_cannot_import_unrelated_or_opted_out_body(tmp_path, monkeypatch, blocked):
    monkeypatch.setattr(web, "REFERENCE_CATALOG", REAL_REFERENCE_CATALOG)
    monkeypatch.setattr(web, "search_sources", lambda *a, **k: [])
    monkeypatch.setattr(web, "allow_crawl", lambda *a: None)
    page = html_page('<meta name="robots" content="noindex">' if blocked else "",
                     body="Coronavirus disease treatment and Baclofen drug information. " * 20)
    monkeypatch.setattr(web, "public_get", lambda *a, **k: page)
    monkeypatch.setattr(web, "update_index", lambda: pytest.fail("must not index rejected sources"))
    result = web.research("住宅 採光", tmp_path)
    assert result["status"] == "no_match" and len(result["items"]) == 2
    assert all("不索引" in i["reason"] if blocked else "關聯不足" in i["reason"] for i in result["items"])
    assert not list(tmp_path.iterdir())


def test_direct_url_does_not_use_catalog_after_fetch_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "REFERENCE_CATALOG", REAL_REFERENCE_CATALOG)
    reads = []
    def read(candidate, cache):
        reads.append(candidate["url"])
        raise net.WebFailure("來源拒絕讀取")
    monkeypatch.setattr(web, "read_source", read)
    result = web.research("住宅", tmp_path, source_url="https://design.example/blocked")
    assert reads == ["https://design.example/blocked"]
    assert "catalog_used" not in result and not list(tmp_path.iterdir())


def test_missing_search_dependency_explains_server_deployment(monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, "ddgs", None)
    with pytest.raises(net.WebFailure, match="網站伺服器.*更新部署") as error:
        web.search_sources("住宅 採光")
    assert "不需要在自己的電腦安裝" in str(error.value)


def fake_search(monkeypatch, responses):
    import sys
    from types import SimpleNamespace
    calls = []
    class Search:
        def __init__(self, **kwargs):
            assert kwargs == {"timeout": 8, "verify": True}
        def text(self, query, **kwargs):
            backend = kwargs["backend"]
            assert "," not in backend and backend != "bing"
            calls.append(backend)
            response = responses[backend]
            if isinstance(response, Exception):
                raise response
            return response
    monkeypatch.setitem(sys.modules, "ddgs", SimpleNamespace(DDGS=Search))
    return calls


def test_independent_fallback_retains_success_after_provider_failure(monkeypatch):
    calls = fake_search(monkeypatch, {
        "brave": RuntimeError("HTTP 429 private-query secret-token"),
        "duckduckgo": [{"href": "https://design.example/garage", "title": "住宅車庫"}],
    })
    attempts = []
    result = web.search_sources("住宅 車庫 玄關 動線", attempts=attempts)
    assert calls == ["brave", "duckduckgo"]
    assert result == [{"url": "https://design.example/garage", "title": "住宅車庫"}]
    assert attempts == [{"provider": "brave", "status": "limited"}, {"provider": "duckduckgo", "status": "ok"}]
    assert "secret" not in str(attempts)


def test_unusable_results_try_next_provider_and_normalize_rank(monkeypatch):
    calls = fake_search(monkeypatch, {
        "brave": [None, {"href": "file:///secret"}, {"href": "https://facebook.com/post"}],
        "duckduckgo": [],
        "yahoo": [{"href": "https://design.example/house#x", "title": "住宅"},
                  {"href": "https://design.example/house#y", "title": "重複"},
                  {"href": "https://housing.gov.tw/design", "title": "住宅設計"}],
    })
    result = web.search_sources("住宅")
    assert calls == list(web.SEARCH_BACKENDS)
    assert [r["title"] for r in result] == ["住宅設計", "住宅"]


def test_all_provider_failures_have_safe_diagnostics_no_index_writes(tmp_path, monkeypatch, caplog):
    calls = fake_search(monkeypatch, {
        "brave": RuntimeError("403 secret-query"),
        "duckduckgo": TimeoutError("timed out secret-token"),
        "yahoo": RuntimeError("TLS private-proxy-password"),
    })
    result = web.research("住宅", tmp_path / "rag")
    assert calls == list(web.SEARCH_BACKENDS) and result["status"] == "unavailable"
    assert [a["status"] for a in result["search_attempts"]] == ["limited", "timeout", "connection_error"]
    assert result["index"]["status"] == "not_updated" and not list(tmp_path.iterdir())
    assert "較短" not in result["reason"]
    assert all(value not in str(result) + caplog.text for value in ("secret-query", "secret-token", "private-proxy-password"))


def test_all_empty_is_no_match_instead_of_connection_failure(tmp_path, monkeypatch):
    fake_search(monkeypatch, {"brave": [], "duckduckgo": RuntimeError("No results found."), "yahoo": []})
    result = web.research("住宅", tmp_path / "rag")
    assert result["status"] == "no_match"
    assert all(a["status"] == "empty" for a in result["search_attempts"])


@pytest.mark.parametrize("url", ["file:///secret", "ftp://example.com/x", "http://localhost/x",
                                 "http://machine.local/x", "https://user:pass@example.com", "https://example.com:8080",
                                 "https://example.com/\nsecret"])
def test_reject_nonpublic_url_forms(url):
    with pytest.raises(net.WebFailure):
        net.normalize_url(url)


@pytest.mark.parametrize("address", ["127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "192.168.1.2"])
def test_private_and_mixed_dns_rejected(monkeypatch, address):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [
        (0, 0, 0, "", ("8.8.8.8", 443)), (0, 0, 0, "", (address, 443))])
    with pytest.raises(net.WebFailure, match="內網"):
        net.public_addresses("untrusted.example", 443)


def test_fetch_pins_public_ip_preserves_tls_and_bounds_response(monkeypatch):
    import urllib3
    seen = []
    class Response:
        status = 200
        headers = {"Content-Type": "text/html"}
        def stream(self, *a, **k): yield b"x" * 20
        def close(self): pass
    class Pool:
        def __init__(self, address, **kwargs): seen.append((address, kwargs))
        def urlopen(self, method, target, **kwargs):
            assert kwargs["headers"]["Host"] == "example.com"
            assert kwargs["redirect"] is False
            return Response()
        def close(self): pass
    monkeypatch.setattr(net, "public_addresses", lambda *a: ["93.184.216.34"])
    monkeypatch.setattr(urllib3, "HTTPSConnectionPool", Pool)
    result = net.public_get("https://example.com/a#fragment")
    assert result.body == b"x" * 20 and result.url == "https://example.com/a"
    assert seen[0][0] == "93.184.216.34"
    assert seen[0][1]["server_hostname"] == seen[0][1]["assert_hostname"] == "example.com"
    assert seen[0][1]["cert_reqs"] == "CERT_REQUIRED"
    with pytest.raises(net.WebFailure, match="大小"):
        net.public_get("https://example.com", max_bytes=10)


def test_redirect_policy_checked_before_next_request(monkeypatch):
    calls = []
    def request(url, *args):
        calls.append(url)
        return net.WebPage(url, 302, {"location": "https://other.example/private"}, b"")
    monkeypatch.setattr(net, "_request", request)
    def deny(_): raise net.WebFailure("robots denied")
    with pytest.raises(net.WebFailure, match="robots"):
        net.public_get("https://example.com", before_redirect=deny)
    assert calls == ["https://example.com/"]


def html_page(extra="", body=None):
    body = body or "住宅車庫與玄關需要分開行人動線，採光與通風需要考量窗戶位置。" * 12
    text = f'<html><head><meta charset="utf-8"><title>住宅採光案例</title>{extra}</head><body><nav>不應入庫的導覽</nav><article><p>{body}</p><script>惡意腳本</script></article></body></html>'
    return net.WebPage("https://design.example/house", 200, {"content-type": "text/html"}, text.encode())


def test_html_real_body_metadata_and_robot_optout():
    title, evidence, metadata = web.html_evidence(html_page('<meta name="author" content="建築作者">'))
    assert title == "住宅採光案例" and metadata["author"] == "建築作者"
    assert "行人動線" in evidence[0].text and "導覽" not in evidence[0].text
    assert "腳本" not in evidence[0].text
    assert web.relevant("住宅 採光", title, evidence)
    assert not web.relevant("巧克力蛋糕", title, evidence)
    with pytest.raises(net.WebFailure, match="不索引"):
        web.html_evidence(html_page('<meta name="ROBOTS" content="noindex, nofollow">'))
    with pytest.raises(net.WebFailure, match="正文"):
        web.html_evidence(html_page(body="搜尋摘要很短"))


@pytest.mark.parametrize("encoding,header,meta", [
    ("utf-8", "text/html; charset=utf-8", ""),
    ("utf-8", "text/html", '<meta charset="utf-8">'),
    ("utf-8", "text/html", ""),
    ("big5", "text/html; charset=big5", ""),
    ("big5", "text/html", '<meta http-equiv="Content-Type" content="text/html; charset=big5">'),
])
def test_html_chinese_encoding_is_preserved(encoding, header, meta):
    text = f'<html><head>{meta}<title>住宅車庫</title></head><body><p>{"住宅車庫玄關動線設計。" * 20}</p></body></html>'
    page = net.WebPage("https://design.example/house", 200, {"content-type": header}, text.encode(encoding))
    title, evidence, _ = web.html_evidence(page)
    assert title == "住宅車庫" and "玄關動線" in evidence[0].text
    assert web.relevant("住宅 車庫 玄關 動線", title, evidence)


def test_robots_disallow_and_unknown_status_fail_closed(monkeypatch):
    monkeypatch.setattr(web, "public_get", lambda url, **k: net.WebPage(url, 200, {}, b"User-agent: *\nDisallow: /private"))
    with pytest.raises(net.WebFailure, match="不允許"):
        web.allow_crawl("https://example.com/private", {})
    web.allow_crawl("https://example.com/public", {})
    monkeypatch.setattr(web, "public_get", lambda url, **k: net.WebPage(url, 503, {}, b""))
    with pytest.raises(net.WebFailure, match="無法確認"):
        web.allow_crawl("https://example.com/public", {})


def test_pdf_page_citations():
    fitz = pytest.importorskip("pymupdf")
    with fitz.open() as doc:
        for text in ("Garage and entrance circulation. " * 6, "Daylight and windows. " * 8):
            doc.new_page().insert_textbox(fitz.Rect(20, 20, 560, 800), text)
        page = net.WebPage("https://example.com/house.pdf", 200, {}, doc.tobytes())
    title, evidence, metadata = web.pdf_evidence(page, "House")
    assert title == "House" and [e.location for e in evidence] == ["第 1 頁", "第 2 頁"]
    assert all(e.details["url"] == page.url for e in evidence)


@pytest.fixture
def store(tmp_path, monkeypatch):
    directory = tmp_path / "rag"
    corpus = tmp_path / "knowledge"
    corpus.mkdir()
    monkeypatch.setenv("RAG_ENABLED", "1")
    monkeypatch.setenv("RAG_WEB_ENABLED", "1")
    monkeypatch.setenv("RAG_DATA_DIR", str(directory))
    class Embedder:
        signature = "web-test"
        def encode(self, texts, **kwargs):
            matrix = np.zeros((len(texts), rag.DIMENSIONS), dtype=np.float32)
            matrix[:, 0] = 1
            return matrix
    engine = rag.Retriever(corpus, directory / "index.sqlite3", Embedder())
    monkeypatch.setattr(rag, "get_retriever", lambda: engine)
    return directory, engine


def test_search_fetch_publish_dedup_and_partial_failure(store, monkeypatch):
    candidates = [{"url": "https://a.example/blocked", "title": "搜尋摘要不入庫"},
                  {"url": "https://b.example/house", "title": "住宅採光"},
                  {"url": "https://b.example/another", "title": "同站重複"}]
    monkeypatch.setattr(web, "search_sources", lambda _, **kwargs: candidates)
    def read(item, cache):
        if item["url"].startswith("https://a."): raise net.WebFailure("網站不允許")
        title, evidence, metadata = web.html_evidence(html_page())
        return title, item["url"], evidence, metadata
    monkeypatch.setattr(web, "read_source", read)
    result = web.research("住宅採光", store[0])
    assert result["status"] == "ready" and len(result["items"]) == 2
    assert [i["status"] for i in result["items"]] == ["skipped", "imported"]
    found = store[1].search("採光", "townhouse")
    assert found.sources[0]["source"] == "https://b.example/house"
    assert "搜尋摘要不入庫" not in str(found.sources)
    assert store[1].search("住宅", "parse").status == "no_match"
    again = web.research("住宅採光", store[0])
    assert again["items"][1]["status"] == "duplicate"
    folders = list((store[0] / "imports").iterdir())
    assert len(folders) == 1
    manifest = json.loads((folders[0] / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["origin"]["retrieved_at"] and manifest["origin"]["url"] == "https://b.example/house"
    monkeypatch.setattr(web, "update_index", lambda: {"status": "unavailable"})
    assert web.research("住宅採光", store[0])["status"] == "stored"


def test_disabled_empty_search_and_api_auth(store, monkeypatch):
    from src.web.app import create_app
    calls = []
    monkeypatch.setattr(web, "search_sources", lambda query, **kwargs: calls.append(query) or [])
    monkeypatch.setenv("ACCESS_CODE", "test")
    client = TestClient(create_app())
    assert client.post("/api/rag/research", json={"query": "住宅採光"}).status_code == 403
    assert calls == []
    assert client.post("/api/rag/research", json={"query": "住宅", "code": "test", "limit": 6}).status_code == 422
    response = client.post("/api/rag/research", json={"query": "住宅採光", "code": "test"})
    assert response.json()["status"] == "no_match" and calls == ["住宅採光"]
    monkeypatch.setenv("RAG_WEB_ENABLED", "0")
    assert web.research("住宅採光", store[0])["status"] == "disabled"
    assert calls == ["住宅採光"]


def test_direct_source_api_auth_and_real_evidence(store, monkeypatch):
    from src.web.app import create_app
    monkeypatch.setenv("ACCESS_CODE", "test")
    monkeypatch.setattr(web, "search_sources", lambda *a, **k: pytest.fail("direct URL must not call search"))
    reads = []
    def read(candidate, cache):
        reads.append(candidate["url"])
        title, evidence, metadata = web.html_evidence(html_page())
        return title, candidate["url"], evidence, metadata
    monkeypatch.setattr(web, "read_source", read)
    client = TestClient(create_app())
    body = {"query": "住宅車庫", "source_url": "https://design.example/house"}
    assert client.post("/api/rag/research", json=body).status_code == 403
    assert reads == []
    response = client.post("/api/rag/research", json={**body, "code": "test"})
    assert response.status_code == 200
    result = response.json()
    assert result["status"] == "ready" and result["search_attempts"] == [], result
    assert "未使用搜尋引擎" in result["provider"]
    assert reads == [body["source_url"]]
    assert result["items"][0]["report"]["evidence"][0]["method"] == "web_text"
    assert client.post("/api/rag/research", json={**body, "source_url": "x" * 2049, "code": "test"}).status_code == 422


@pytest.mark.parametrize("url", ["https://127.0.0.1/", "http://localhost/", "https://user:pass@example.com/", "file:///secret"])
def test_direct_source_keeps_public_url_guards(tmp_path, monkeypatch, url):
    monkeypatch.setattr(web, "search_sources", lambda *a, **k: pytest.fail("must not search"))
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(0, 0, 0, "", ("127.0.0.1", 443))])
    result = web.research("住宅", tmp_path / "rag", source_url=url)
    assert result["status"] in {"no_match", "unavailable"}
    assert result["index"]["status"] == "not_updated" and not list(tmp_path.iterdir())


def test_direct_source_respects_opt_out_relevance_and_feature_toggle(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "search_sources", lambda *a, **k: pytest.fail("must not search"))
    monkeypatch.setattr(web, "allow_crawl", lambda *a: None)
    monkeypatch.setattr(web, "public_get", lambda *a, **k: html_page('<meta name="robots" content="noindex">'))
    result = web.research("住宅", tmp_path, source_url="https://design.example/house")
    assert "不索引" in result["items"][0]["reason"]
    monkeypatch.setattr(web, "public_get", lambda *a, **k: html_page())
    result = web.research("巧克力蛋糕", tmp_path, source_url="https://design.example/house")
    assert "關聯不足" in result["items"][0]["reason"]
    monkeypatch.setenv("RAG_WEB_ENABLED", "0")
    monkeypatch.setattr(web, "read_source", lambda *a, **k: pytest.fail("disabled"))
    assert web.research("住宅", tmp_path, source_url="https://design.example/house")["status"] == "disabled"
    assert not list(tmp_path.iterdir())
