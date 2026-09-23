"""Search -> read real public sources -> cited local RAG, without an LLM.

Search snippets never become evidence. Network research runs only after an
explicit web-research request, never automatically inside design generation.
"""
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
from urllib.parse import urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

from .importers import Evidence, ImportReport, _LOCK, _markdown, update_index
from .public_web import WebFailure, WebPage, USER_AGENT, normalize_url, public_get
from .storage import staging_directory

MAX_TEXT = 12_000
MAX_CANDIDATES = 8
BLOCKED_DOMAINS = {"facebook.com", "instagram.com", "pinterest.com", "youtube.com", "tiktok.com",
                   "x.com", "twitter.com", "accounts.google.com"}


def web_enabled() -> bool:
    return os.environ.get("RAG_WEB_ENABLED", "1").lower() not in {"0", "off", "false"}


def search_sources(query: str, limit: int = MAX_CANDIDATES) -> list[dict]:
    try:
        from ddgs import DDGS
    except ImportError as exc:
        raise WebFailure("請先安裝 requirements-web.txt") from exc
    try:
        rows = DDGS(timeout=8, verify=True).text(query, region="tw-tzh", safesearch="moderate",
                                               max_results=limit, backend="bing,brave,duckduckgo")
    except Exception as exc:
        raise WebFailure("搜尋服務暫時無法使用，請稍後重試或改用較短的主題") from exc
    candidates, seen = [], set()
    for row in rows:
        try:
            url = normalize_url(row.get("href", ""))
        except WebFailure:
            continue
        host = urlsplit(url).hostname
        if url in seen or any(host == domain or host.endswith("." + domain) for domain in BLOCKED_DOMAINS):
            continue
        seen.add(url)
        candidates.append({"title": str(row.get("title", host))[:240], "url": url})
    # Prefer government, academic and established architectural publications;
    # this ranking is not a claim that an individual source has been verified.
    def rank(candidate):
        host = urlsplit(candidate["url"]).hostname
        return 0 if host.endswith((".gov.tw", ".edu.tw", ".gov", ".edu")) else (
            1 if host in {"www.archdaily.com", "www.archdaily.cn", "www.archdaily.tw"} else 2)
    return sorted(candidates, key=rank)[:limit]


def allow_crawl(url: str, cache: dict) -> None:
    parsed = urlsplit(url)
    origin = urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
    if origin not in cache:
        response = public_get(origin + "/robots.txt", max_bytes=512_000)
        if response.status in {404, 410}:
            cache[origin] = None
        elif response.status == 200:
            rules = RobotFileParser()
            rules.parse(response.body.decode("utf-8", errors="replace").splitlines())
            cache[origin] = rules
        else:
            raise WebFailure("無法確認網站擷取規則，已跳過")
    if cache[origin] is not None and not cache[origin].can_fetch(USER_AGENT, url):
        raise WebFailure("網站規則不允許自動擷取，已跳過")


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def html_evidence(page: WebPage) -> tuple[str, list[Evidence], dict]:
    from lxml import html
    try:
        root = html.fromstring(page.body, parser=html.HTMLParser(no_network=True))
    except (ValueError, TypeError) as exc:
        raise WebFailure("網頁內容無法解析") from exc
    robots = " ".join(root.xpath('//meta[translate(@name,"ROBTS","robts")="robots"]/@content'))
    directives = page.headers.get("x-robots-tag", "") + " " + robots
    if re.search(r"\b(noindex|noarchive|noai)\b", directives, re.I):
        raise WebFailure("來源要求不索引或不供 AI 使用，已跳過")
    titles = root.xpath('//meta[@property="og:title"]/@content') or root.xpath('//title/text()')
    title = _clean(titles[0])[:240] if titles else urlsplit(page.url).hostname
    if any(token in title.lower() for token in ("just a moment", "access denied", "captcha", "sign in", "登入")):
        raise WebFailure("來源需要登入或人工驗證，已跳過")
    metadata = {"published": next(iter(root.xpath('//meta[@property="article:published_time"]/@content')), ""),
                "author": next(iter(root.xpath('//meta[@name="author"]/@content')), ""),
                "license": next(iter(root.xpath('//a[@rel="license"]/@href')), "未明示；原作者保留權利")}
    for node in root.xpath('//script|//style|//noscript|//nav|//header|//footer|//form|//aside|//svg'):
        node.drop_tree()
    roots = root.xpath('//article') or root.xpath('//main') or root.xpath('//body') or [root]
    container = max(roots, key=lambda node: len(node.text_content()))
    blocks, seen, length = [], set(), 0
    for node in container.xpath('.//h1|.//h2|.//h3|.//p|.//li|.//td'):
        text = _clean(node.text_content())
        if len(text) < 10 or text in seen:
            continue
        seen.add(text)
        blocks.append(text[:MAX_TEXT - length])
        length += len(blocks[-1]) + 1
        if length >= MAX_TEXT:
            break
    text = "\n".join(blocks).strip()
    if len(text) < 120:
        raise WebFailure("未取得足夠正文，可能是動態網頁或登入頁，未將搜尋摘要當成文件")
    return title, [Evidence("網頁正文", "web_text", text, {"url": page.url})], metadata


def pdf_evidence(page: WebPage, fallback_title: str) -> tuple[str, list[Evidence], dict]:
    try:
        import pymupdf
        with pymupdf.open(stream=page.body, filetype="pdf") as doc:
            if doc.needs_pass:
                raise WebFailure("網路 PDF 已加密，已跳過")
            if len(doc) > 30:
                raise WebFailure("網路 PDF 超過 30 頁，請下載後分批本機匯入")
            evidence, length = [], 0
            for i, item in enumerate(doc):
                text = item.get_text("text", sort=True).strip()
                if not text:
                    continue
                clipped = text[:max(0, MAX_TEXT - length)]
                if clipped:
                    evidence.append(Evidence(f"第 {i + 1} 頁", "web_pdf_text", clipped, {"url": page.url}))
                    length += len(clipped)
                if length >= MAX_TEXT:
                    break
            if length < 120:
                raise WebFailure("網路 PDF 沒有足夠文字；掃描版請下載後使用本機 OCR 匯入")
            title = str(doc.metadata.get("title") or fallback_title)[:240]
            return title, evidence, {"author": doc.metadata.get("author", ""), "license": "原作者保留權利"}
    except WebFailure:
        raise
    except Exception as exc:
        raise WebFailure("PDF 無法讀取或尚未安裝 PDF 套件") from exc


def read_source(candidate: dict, robots_cache: dict) -> tuple[str, str, list[Evidence], dict]:
    url = normalize_url(candidate["url"])
    allow_crawl(url, robots_cache)
    page = public_get(url, before_redirect=lambda target: allow_crawl(target, robots_cache))
    # Redirect targets have independent crawl policies as well as independent DNS checks.
    if page.url != url:
        allow_crawl(page.url, robots_cache)
    if page.status != 200:
        raise WebFailure(f"來源回覆 HTTP {page.status}，已跳過")
    if re.search(r"\b(noindex|noarchive|noai)\b", page.headers.get("x-robots-tag", ""), re.I):
        raise WebFailure("來源要求不索引，已跳過")
    content_type = page.headers.get("content-type", "").lower()
    if page.body.startswith(b"%PDF-"):
        title, evidence, metadata = pdf_evidence(page, candidate["title"])
    elif "text/html" in content_type or "application/xhtml+xml" in content_type:
        title, evidence, metadata = html_evidence(page)
    else:
        raise WebFailure("網路搜尋目前讀取公開網頁與文字 PDF；其他格式請使用檔案匯入")
    return title, page.url, evidence, metadata


def relevant(query: str, title: str, evidence: list[Evidence]) -> bool:
    query = query.lower()
    terms = re.findall(r"[a-z0-9]{3,}", query)
    for group in re.findall(r"[\u3400-\u9fff]+", query):
        terms.extend(group[i:i + 2] for i in range(len(group) - 1))
    terms = set(terms)
    text = (title + "\n" + "\n".join(item.text for item in evidence)).lower()
    return bool(terms) and sum(term in text for term in terms) / len(terms) >= .2


def publish_source(title: str, url: str, evidence: list[Evidence], metadata: dict,
                   query: str, data_dir: Path) -> ImportReport:
    from .rag import read_corpus
    identity = json.dumps({"url": url, "evidence": [asdict(e) for e in evidence]},
                          sort_keys=True, ensure_ascii=False)
    digest = hashlib.sha256(identity.encode()).hexdigest()
    report = ImportReport(title, "imported", document_id="web-" + digest[:24],
                          evidence=[asdict(e) for e in evidence],
                          warnings=["網路參考尚未經人工核對；案例、尺寸及法規適用性仍須確認。"])
    with _LOCK:
        imports = data_dir / "imports"
        imports.mkdir(parents=True, exist_ok=True)
        target = imports / report.document_id
        if (target / "manifest.json").is_file():
            stored = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
            report = ImportReport(**stored["report"])
            report.status = "duplicate"
            return report
        with staging_directory(data_dir, "web-import") as stage:
            (stage / "document.md").write_text(_markdown(report, evidence, source=url), encoding="utf-8")
            report.chunks = len(read_corpus(stage))
            existing = sum(len(read_corpus(folder)) for folder in imports.iterdir()
                           if folder.is_dir() and (folder / "manifest.json").is_file())
            if existing + report.chunks > 4000:
                raise WebFailure("匯入資料已達 4,000 段上限，請先整理知識庫")
            provenance = {**metadata, "url": url, "query": query,
                          "retrieved_at": datetime.now(timezone.utc).isoformat(),
                          "extraction": "文字擷取，未用 LLM 生成內容", "max_characters": MAX_TEXT}
            (stage / "manifest.json").write_text(json.dumps(
                {"version": "web-v1", "sha256": digest, "origin": provenance, "report": report.to_dict()},
                ensure_ascii=False, indent=2), encoding="utf-8")
            if not target.resolve().is_relative_to(data_dir.resolve()):
                raise WebFailure("儲存路徑超出知識庫")
            stage.rename(target)
    return report


def research(query: str, data_dir: Path, limit: int = 3) -> dict:
    query = query.strip()
    if not 2 <= len(query) <= 160 or not 1 <= limit <= 5:
        raise ValueError("請輸入 2～160 字的搜尋主題，匯入上限為 1～5 筆")
    result = {"query": query, "status": "no_match", "provider": "網路搜尋（Bing／Brave／DuckDuckGo）",
              "items": [], "index": {"status": "not_updated"}, "reason": ""}
    if not web_enabled():
        result.update(status="disabled", reason="網路搜尋已關閉")
        return result
    try:
        candidates = search_sources(query)
    except WebFailure as exc:
        result.update(status="unavailable", reason=str(exc))
        return result
    robots_cache, hosts, count = {}, set(), 0
    for candidate in candidates:
        host = urlsplit(candidate["url"]).hostname
        if host in hosts:
            continue
        item = {**candidate, "status": "skipped", "reason": ""}
        try:
            title, url, evidence, metadata = read_source(candidate, robots_cache)
            if not relevant(query, title, evidence):
                raise WebFailure("取得的正文與主題關聯不足，已跳過")
            report = publish_source(title, url, evidence, metadata, query, data_dir)
            item.update(title=title, url=url, status=report.status, report=report.to_dict())
            count += 1
            hosts.add(host)
        except Exception as exc:
            item["reason"] = str(exc) if isinstance(exc, WebFailure) else "來源處理失敗，已保留既有知識庫"
        result["items"].append(item)
        if count >= limit:
            break
    if count:
        result["index"] = update_index()
        result["status"] = "ready" if result["index"]["status"] == "ready" else "stored"
        result["reason"] = f"已取得 {count} 份來源" + ("，並更新知識庫" if result["status"] == "ready" else "，但索引尚未就緒")
    else:
        result["reason"] = "未取得可匯入的公開正文，請換較短的主題或改用檔案匯入"
    return result
