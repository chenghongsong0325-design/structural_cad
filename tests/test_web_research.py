"""No external requests: public-address guards, extraction, citations and API."""
import json
from pathlib import Path
import socket
import time

import numpy as np
import pytest
from fastapi.testclient import TestClient

from src.knowledge import public_web as net, rag, web_research as web


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
    monkeypatch.setattr(web, "search_sources", lambda _: candidates)
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
    monkeypatch.setattr(web, "search_sources", lambda query: calls.append(query) or [])
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
