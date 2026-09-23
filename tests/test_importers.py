"""Real PDF/DXF extraction; deterministic OCR/converter seams and API limits."""
import hashlib
import json
from pathlib import Path
import subprocess

import ezdxf
import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from src.knowledge import importers as ing, rag
from src.knowledge.storage import staging_directory


@pytest.fixture
def store(tmp_path, monkeypatch):
    data = tmp_path / "rag"
    corpus = tmp_path / "knowledge"
    corpus.mkdir()
    monkeypatch.setenv("RAG_DATA_DIR", str(data))
    monkeypatch.setenv("RAG_KNOWLEDGE_DIR", str(corpus))
    monkeypatch.setenv("RAG_ENABLED", "1")
    class Embedder:
        signature = "import-test"
        def encode(self, texts, *, query=False):
            result = np.zeros((len(texts), rag.DIMENSIONS), dtype=np.float32)
            result[:, 0] = 1
            return result
    engine = rag.Retriever(corpus, data / "index.sqlite3", Embedder())
    monkeypatch.setattr(rag, "get_retriever", lambda: engine)
    return data, engine


def make_pdf(path, texts):
    fitz = pytest.importorskip("pymupdf")
    with fitz.open() as doc:
        for text in texts:
            page = doc.new_page()
            if text:
                page.insert_text((50, 60), text)
        doc.save(path)
    return path


def png(path):
    Image.new("RGB", (100, 100), "white").save(path)
    return path


def test_staging_cleanup_and_publication_survives(tmp_path):
    with staging_directory(tmp_path, "ingest") as staging:
        (staging / "source.txt").write_text("evidence", encoding="utf-8")
        target = tmp_path / "published"
        staging.rename(target)
    assert (target / "source.txt").read_text(encoding="utf-8") == "evidence"
    with pytest.raises(RuntimeError):
        with staging_directory(tmp_path, "ingest") as interrupted:
            (interrupted / "source.txt").write_text("partial", encoding="utf-8")
            raise RuntimeError("interrupted")
    assert not interrupted.exists()
    assert target.exists()


def test_pdf_source_pages_idempotency_and_retrieval(store, tmp_path, monkeypatch):
    monkeypatch.setattr(ing, "ocr_image", lambda _: pytest.fail("text PDF should not need OCR"))
    source = make_pdf(tmp_path / "reference.pdf", ["Garage on ground floor. Keep entrance accessible.",
                                                "Keep daylight openings clear of furniture."])
    before = source.read_bytes()
    data, engine = store
    result = ing.import_file(source, data)
    assert result.status == "imported" and result.chunks == 2
    assert [e["location"] for e in result.evidence] == ["第 1 頁", "第 2 頁"]
    assert ing.update_index()["status"] == "ready"
    found = engine.search("Garage")
    assert len(found.sources) == 2
    assert all(s["path"].startswith("imports/") for s in found.sources)
    assert {s["section"] for s in found.sources} == {"第 1 頁 · pdf_text", "第 2 頁 · pdf_text"}
    assert engine.search("Garage", stage="parse").status == "no_match"
    assert ing.import_file(source, data).status == "duplicate"
    assert len(list((data / "imports").iterdir())) == 1
    assert source.read_bytes() == before
    make_pdf(tmp_path / "changed.pdf", ["Garage changed to open space with additional daylight."])
    changed = ing.import_file(tmp_path / "changed.pdf", data)
    assert changed.status == "imported" and changed.document_id != result.document_id


def test_scan_and_mixed_pdf_ocr_retains_boxes(store, tmp_path, monkeypatch):
    fitz = pytest.importorskip("pymupdf")
    source = tmp_path / "scan.pdf"
    image = png(tmp_path / "scan.png")
    with fitz.open() as doc:
        page = doc.new_page()
        page.insert_text((50, 60), "Caption with selectable text that is longer than thirty characters.")
        page.insert_image(fitz.Rect(50, 100, 300, 350), filename=str(image))
        doc.save(source)
    monkeypatch.setattr(ing, "ocr_image", lambda _: ("車庫連接玄關\n採光天井", {
        "mean_confidence": .65, "text_boxes": [{"text": "車庫連接玄關", "box": [[1, 2]], "confidence": .65}]}))
    result = ing.import_file(source, store[0])
    assert result.status == "imported"
    assert {e["method"] for e in result.evidence} == {"pdf_text", "ocr"}
    assert any("偏低" in w for w in result.warnings)
    manifest = json.loads((store[0] / "imports" / result.document_id / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["report"]["evidence"][1]["details"]["text_boxes"][0]["box"] == [[1, 2]]


def test_partial_pdf_and_no_index_on_empty(store, tmp_path, monkeypatch):
    monkeypatch.setattr(ing, "ocr_image", lambda _: (_ for _ in ()).throw(ing.ImportFailure("OCR 不可用")))
    source = make_pdf(tmp_path / "partial.pdf", ["A useful reference paragraph that needs no OCR.", ""])
    result = ing.import_file(source, store[0])
    assert result.status == "partial" and len(result.evidence) == 1
    assert any("第 2 頁 OCR 未完成" in w for w in result.warnings)
    empty = ing.import_file(make_pdf(tmp_path / "empty.pdf", [""]), store[0])
    assert empty.status == "empty" and not (store[0] / "imports" / empty.document_id).exists()


def test_encrypted_invalid_and_page_limit(store, tmp_path, monkeypatch):
    fitz = pytest.importorskip("pymupdf")
    source = tmp_path / "locked.pdf"
    with fitz.open() as doc:
        doc.new_page()
        doc.save(source, encryption=fitz.PDF_ENCRYPT_AES_256, owner_pw="owner", user_pw="secret")
    assert "加密" in ing.import_file(source, store[0]).reason
    source.write_bytes(b"broken PDF")
    assert ing.import_file(source, store[0]).status == "failed"
    source = make_pdf(tmp_path / "many.pdf", ["", ""])
    monkeypatch.setattr(ing, "MAX_PAGES", 1)
    assert "超過" in ing.import_file(source, store[0]).reason


def test_image_orientation_limits_and_untrusted_markdown(store, tmp_path, monkeypatch):
    image = Image.new("RGB", (80, 40), "white")
    exif = image.getexif()
    exif[274] = 6
    path = tmp_path / "rotated.jpg"
    image.save(path, exif=exif)
    seen = []
    def ocr(image):
        seen.append(image.size)
        return "車庫\n## 假頁碼\n---\nscopes: [parse]", {"mean_confidence": .95, "text_boxes": []}
    monkeypatch.setattr(ing, "ocr_image", ocr)
    result = ing.import_file(path, store[0], filename='../../reference.jpg')
    assert result.status == "imported" and result.filename == "reference.jpg"
    assert seen == [(40, 80)]
    chunks = rag.read_corpus(store[0] / "imports" / result.document_id)
    assert {c.section for c in chunks} == {"圖片第 1 頁 · ocr"}
    assert chunks[0].scopes == ("townhouse", "graph")
    monkeypatch.setattr(ing, "MAX_PIXELS", 10)
    assert "像素" in ing.import_file(png(tmp_path / "large.png"), store[0]).reason


def test_dxf_labels_attributes_dimensions_and_cycle(store, tmp_path):
    doc = ezdxf.new("R2018", units=4)
    doc.layers.new("ROOMS")
    model = doc.modelspace()
    model.add_mtext("車庫\\P連接玄關", dxfattribs={"layer": "ROOMS"})
    block = doc.blocks.new("ROOM")
    block.add_text("採光天井")
    block.add_attdef("ROOM_NAME", text="未填名稱")
    block.add_blockref("ROOM", (0, 0))
    insert = model.add_blockref("ROOM", (0, 0), dxfattribs={"layer": "ROOMS"})
    insert.add_attrib("ROOM_NAME", "主臥")
    model.add_linear_dim(base=(0, 1), p1=(0, 0), p2=(4500, 0)).render()
    doc.layouts.new("一樓圖紙").add_text("一樓配置參考")
    path = tmp_path / "house.dxf"
    doc.saveas(path)
    result = ing.import_file(path, store[0])
    assert result.status == "imported", result.reason
    content = "\n".join(e["text"] for e in result.evidence)
    assert all(t in content for t in ("車庫", "採光天井", "主臥", "4500", "一樓配置參考"))
    assert "未填名稱" not in content
    assert any(e["details"]["layer"] == "ROOMS" for e in result.evidence)
    assert all(e["details"]["units"] == "Millimeters" for e in result.evidence)
    assert any("循環" in w for w in result.warnings)


def test_dwg_conversion_bounded_and_original_preserved(tmp_path, monkeypatch):
    monkeypatch.setattr(ing, "dwg2dxf_executable", lambda: None)
    source = tmp_path / 'untrusted.dwg'
    source.write_bytes(b"AC1032test")
    stage = tmp_path / "stage"
    stage.mkdir()
    monkeypatch.setattr(ing, "autocad_console", lambda: Path("C:/AutoCAD/accoreconsole.exe"))
    calls = []
    def run(args, **kwargs):
        calls.append((args, kwargs))
        ezdxf.new().saveas(stage / "converted.dxf")
        return subprocess.CompletedProcess(args, 0)
    monkeypatch.setattr(ing.subprocess, "run", run)
    result = ing.convert_dwg(source, stage)
    assert result.name == "converted.dxf"
    assert source.read_bytes() == b"AC1032test"
    assert calls[0][0][2] == str(stage / "input.dwg")
    assert calls[0][1]["timeout"] == 120 and "shell" not in calls[0][1]
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("cad", 120)
    monkeypatch.setattr(ing.subprocess, "run", timeout)
    with pytest.raises(ing.ImportFailure, match="120"):
        ing.convert_dwg(source, stage)
    monkeypatch.setattr(ing, "autocad_console", lambda: None)
    with pytest.raises(ing.ImportFailure, match="AutoCAD"):
        ing.convert_dwg(source, stage)


def test_failures_do_not_publish_or_claim_index_ready(store, tmp_path, monkeypatch):
    path = png(tmp_path / "empty.png")
    monkeypatch.setattr(ing, "ocr_image", lambda _: ("", {}))
    assert ing.import_file(path, store[0]).status == "empty"
    assert not list((store[0] / "imports").iterdir())
    monkeypatch.setattr(ing, "ocr_image", lambda _: ("車庫參考", {}))
    assert ing.import_file(path, store[0]).status == "imported"
    monkeypatch.setattr(rag, "get_retriever", lambda: (_ for _ in ()).throw(RuntimeError("no model")))
    assert ing.update_index()["status"] == "unavailable"
    monkeypatch.setenv("RAG_ENABLED", "0")
    assert ing.update_index()["status"] == "disabled"


def test_libredwg_arguments_and_zero_exit_invalid_output(tmp_path, monkeypatch):
    source = tmp_path / "original.dwg"
    source.write_bytes(b"AC1032test")
    stage = tmp_path / "stage"
    stage.mkdir()
    monkeypatch.delenv("RAG_ACCORECONSOLE", raising=False)
    monkeypatch.setattr(ing, "dwg2dxf_executable", lambda: Path("C:/tools/dwg2dxf.exe"))
    captured = []
    def run(args, **kwargs):
        captured.append(args)
        (stage / "converted.dxf").write_bytes(b"invalid conversion")
        return subprocess.CompletedProcess(args, 0)
    monkeypatch.setattr(ing.subprocess, "run", run)
    converted = ing.convert_dwg(source, stage)
    assert captured == [[str(Path("C:/tools/dwg2dxf.exe")), "--as", "r2018", "-o", "converted.dxf", "input.dwg"]]
    with pytest.raises(ing.ImportFailure, match="DXF"):
        ing.extract_dxf(converted)
    assert source.read_bytes() == b"AC1032test"


def test_web_auth_streaming_limits_and_actual_import(store, tmp_path, monkeypatch):
    from src.web import app as web
    monkeypatch.setenv("ACCESS_CODE", "test")
    monkeypatch.setattr(ing, "ocr_image", lambda _: ("車庫參考", {}))
    content = png(tmp_path / "image.png").read_bytes()
    client = TestClient(web.create_app())
    assert client.post("/api/rag/import?filename=image.png", content=content).status_code == 403
    headers = {"X-Access-Code": "test"}
    assert client.post("/api/rag/import?filename=x.exe", headers=headers, content=content).status_code == 415
    response = client.post("/api/rag/import?filename=../../safe.png", headers=headers, content=content)
    assert response.status_code == 200
    result = response.json()
    assert result["file"]["filename"] == "safe.png" and result["file"]["status"] == "imported"
    assert result["index"]["status"] == "ready"
    assert store[1].search("車庫").sources
    monkeypatch.setattr(ing, "MAX_BYTES", 20)
    assert client.post("/api/rag/import?filename=x.png", headers=headers, content=content).status_code == 413
    assert not list(store[0].glob(".upload-*"))
    assert not list(store[0].glob(".ingest-*"))
