"""Local document ingestion. Originals, page/label evidence and OCR scores survive.

Imported annotations are reference material, never recovered room geometry or code
compliance. Staging directories are invisible to retrieval until atomic publication.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from functools import lru_cache
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading

import yaml

from src.design.report import JsonReport
from .storage import staging_directory

EXTENSIONS = {".pdf", ".dwg", ".dxf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp", ".bmp"}
MAX_BYTES = 50 * 1024 * 1024
MAX_PAGES = 100
MAX_PIXELS = 25_000_000
MAX_TEXT = 150_000
IMPORT_VERSION = "1"
_LOCK = threading.RLock()
_OCR_LOCK = threading.RLock()
LOGGER = logging.getLogger(__name__)


class ImportFailure(ValueError):
    """An actionable, safe-to-display import error."""


@dataclass
class Evidence:
    location: str
    method: str
    text: str
    details: dict = field(default_factory=dict)


@dataclass
class ImportReport(JsonReport):
    filename: str
    status: str
    document_id: str = ""
    chunks: int = 0
    evidence: list = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    reason: str = ""

    def to_dict(self):
        return asdict(self)

    def summary(self):
        return f"{self.filename}: {self.status}, {self.chunks} 段參考資料"


def safe_name(name: str) -> str:
    # Both path separators matter, including browser-supplied Windows paths.
    name = re.split(r"[/\\]", name)[-1]
    return re.sub(r"[\x00-\x1f\x7f]", "", name).strip()[:180] or "document"


@lru_cache(maxsize=1)
def _ocr_engine():
    try:
        import rapidocr
        from rapidocr import RapidOCR
    except ImportError as exc:
        raise ImportFailure("OCR 尚未安裝，請執行 pip install -r requirements-import.txt") from exc
    # Explicit paths disable the library's automatic model download fallback.
    models = Path(rapidocr.__file__).parent / "models"
    names = {"Det": "PP-OCRv6_det_small.onnx", "Rec": "PP-OCRv6_rec_small.onnx",
             "Cls": "ch_ppocr_mobile_v2.0_cls_mobile.onnx"}
    if not all((models / name).is_file() for name in names.values()):
        raise ImportFailure("OCR 模型不完整，請重新安裝 requirements-import.txt")
    return RapidOCR(params={**{f"{key}.model_path": str(models / name) for key, name in names.items()},
                            "EngineConfig.onnxruntime.intra_op_num_threads": 2,
                            "EngineConfig.onnxruntime.inter_op_num_threads": 1,
                            "Global.log_level": "warning"})


def ocr_image(image) -> tuple[str, dict]:
    import numpy as np
    with _OCR_LOCK:
        result = _ocr_engine()(np.asarray(image.convert("RGB"))[:, :, ::-1].copy())
    texts = list(result.txts or [])
    scores = [] if result.scores is None else [float(v) for v in result.scores]
    boxes = [] if result.boxes is None else result.boxes.tolist()
    return "\n".join(texts), {"text_boxes": [
        {"text": text, "confidence": scores[i], "box": boxes[i]}
        for i, text in enumerate(texts)
    ], "mean_confidence": sum(scores) / len(scores) if scores else None}


def _image_evidence(image, location: str, warnings: list) -> Evidence:
    from PIL import ImageOps
    if image.width * image.height > MAX_PIXELS:
        raise ImportFailure("圖片超過 2,500 萬像素，請先縮小或分割")
    oriented = ImageOps.exif_transpose(image)
    text, details = ocr_image(oriented)
    details["image_size"] = list(oriented.size)
    confidence = details.get("mean_confidence")
    if confidence is not None and confidence < .8:
        warnings.append(f"{location}：OCR 平均辨識分數偏低，請核對原圖")
    return Evidence(location, "ocr", text, details)


def extract_image(path: Path):
    from PIL import Image, UnidentifiedImageError
    evidence, warnings = [], ["OCR 文字及尺寸數字可能誤讀；未辨識牆線、房間邊界或比例尺。"]
    try:
        with Image.open(path) as image:
            frames = getattr(image, "n_frames", 1)
            if frames > MAX_PAGES:
                raise ImportFailure(f"圖片頁數超過 {MAX_PAGES} 頁")
            for page in range(frames):
                image.seek(page)
                evidence.append(_image_evidence(image, f"圖片第 {page + 1} 頁", warnings))
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise ImportFailure("無法讀取圖片，請確認格式或重新匯出") from exc
    return evidence, warnings


def extract_pdf(path: Path):
    try:
        import pymupdf
    except ImportError as exc:
        raise ImportFailure("PDF 套件尚未安裝，請執行 pip install -r requirements-import.txt") from exc
    from PIL import Image
    evidence, warnings = [], []
    try:
        with pymupdf.open(path) as doc:
            if not doc.is_pdf:
                raise ImportFailure("檔案內容不是 PDF")
            if doc.needs_pass:
                raise ImportFailure("PDF 已加密，請先提供解除密碼的副本")
            if len(doc) > MAX_PAGES:
                raise ImportFailure(f"PDF 超過 {MAX_PAGES} 頁，請分批匯入")
            for i, page in enumerate(doc):
                location = f"第 {i + 1} 頁"
                text = page.get_text("text", sort=True).strip()
                if text:
                    evidence.append(Evidence(location, "pdf_text", text))
                # OCR also covers mixed pages; even a caption can accompany a scanned plan.
                needs_ocr = len(text) < 30 or bool(page.get_image_info())
                if not needs_ocr:
                    continue
                try:
                    area = max(1, page.rect.width * page.rect.height)
                    scale = min(2.5, math.sqrt(MAX_PIXELS / area) * .99)
                    pix = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale),
                                         colorspace=pymupdf.csRGB, alpha=False)
                    item = _image_evidence(Image.frombytes("RGB", (pix.width, pix.height), pix.samples),
                                           location, warnings)
                    # Remove exact repeated text lines already obtained losslessly from PDF.
                    existing = {line.strip() for line in text.splitlines()}
                    item.text = "\n".join(line for line in item.text.splitlines() if line.strip() not in existing)
                    evidence.append(item)
                except Exception as exc:
                    reason = str(exc) if isinstance(exc, ImportFailure) else type(exc).__name__
                    warnings.append(f"{location} OCR 未完成：{reason}")
    except ImportFailure:
        raise
    except Exception as exc:
        raise ImportFailure("無法讀取 PDF，請確認檔案完整或重新匯出") from exc
    if any(e.method == "ocr" for e in evidence):
        warnings.append("OCR 文字及尺寸數字可能誤讀；未還原圖面幾何。")
    return evidence, warnings


def autocad_console() -> Path | None:
    explicit = os.environ.get("RAG_ACCORECONSOLE")
    if explicit:
        path = Path(explicit)
        return path if path.is_file() else None
    base = Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Autodesk"
    return next((p for p in sorted(base.glob("AutoCAD */accoreconsole.exe"), reverse=True)
                 if p.is_file()), None)


def dwg2dxf_executable() -> Path | None:
    explicit = os.environ.get("RAG_DWG2DXF")
    if explicit:
        return Path(explicit) if Path(explicit).is_file() else None
    from .rag import paths
    folder = paths()[1].parent / "tools" / "libredwg-0.14"
    return next(folder.rglob("dwg2dxf.exe"), None) if folder.is_dir() else None


def convert_dwg(source: Path, directory: Path) -> Path:
    converter = dwg2dxf_executable()
    if converter is not None and not os.environ.get("RAG_ACCORECONSOLE"):
        shutil.copyfile(source, directory / "input.dwg")
        target = directory / "converted.dxf"
        try:
            with (directory / "converter.log").open("wb") as log:
                result = subprocess.run([str(converter), "--as", "r2018", "-o", "converted.dxf", "input.dwg"],
                                        cwd=directory, stdin=subprocess.DEVNULL, stdout=log,
                                        stderr=subprocess.STDOUT, timeout=120,
                                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except subprocess.TimeoutExpired as exc:
            raise ImportFailure("DWG 轉換超過 120 秒，請分割圖檔或先另存 DXF") from exc
        except OSError as exc:
            raise ImportFailure("無法啟動 DWG 轉檔工具，請執行 python -m src.knowledge prepare-dwg") from exc
        if result.returncode != 0 or not target.is_file() or not 0 < target.stat().st_size <= MAX_BYTES:
            raise ImportFailure("DWG 轉檔失敗或超過 50 MB，請以 CAD 另存 DXF 後匯入")
        (directory / "converter.json").write_text(json.dumps({"engine": "LibreDWG", "version": "0.14"}), encoding="utf-8")
        return target
    console = autocad_console()
    if console is None:
        raise ImportFailure("請執行 python -m src.knowledge prepare-dwg 安裝轉檔工具，或設定可用的 AutoCAD Core Console（RAG_ACCORECONSOLE）")
    # Fixed filenames avoid interpreting a submitted filename as script/code.
    drawing = directory / "input.dwg"
    shutil.copyfile(source, drawing)
    target = directory / "converted.dxf"
    script = directory / "convert.scr"
    script.write_text('(setvar "FILEDIA" 0)\n(setvar "CMDDIA" 0)\n'
                      '(command "_.DXFOUT" "converted.dxf" "_Version" "2018" "16")\n'
                      '(command "_.QUIT" "_No")\n', encoding="ascii")
    try:
        with (directory / "converter.log").open("wb") as log:
            result = subprocess.run([str(console), "/i", str(drawing), "/s", str(script),
                                     "/l", "en-US"], cwd=directory, stdin=subprocess.DEVNULL,
                                    stdout=log, stderr=subprocess.STDOUT, timeout=120,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired as exc:
        raise ImportFailure("DWG 轉換超過 120 秒；請確認 AutoCAD 授權可用，或先另存 DXF") from exc
    except OSError as exc:
        raise ImportFailure("無法啟動 AutoCAD Core Console，請確認安裝路徑與授權") from exc
    if result.returncode != 0 or not target.is_file() or target.stat().st_size == 0:
        raise ImportFailure("AutoCAD 未成功轉出 DXF；請先開啟 AutoCAD 確認授權，或另存 DXF 再匯入")
    if target.stat().st_size > MAX_BYTES:
        raise ImportFailure("轉換後 DXF 超過 50 MB，請分割圖檔")
    (directory / "converter.json").write_text(json.dumps({"engine": "AutoCAD Core Console"}), encoding="utf-8")
    return target


def extract_dxf(path: Path):
    import ezdxf
    from ezdxf import units
    try:
        doc = ezdxf.readfile(path)
    except (OSError, ezdxf.DXFError, UnicodeError) as exc:
        raise ImportFailure("無法讀取 DXF，請用 CAD 重新另存檔案") from exc
    evidence, warnings, visited = [], [], 0
    unit_code = int(doc.header.get("$INSUNITS", 0))
    unit = units.unit_name(unit_code)

    def walk(entities, layout, ancestry=(), inherited_layer="0"):
        nonlocal visited
        for entity in entities:
            visited += 1
            if visited > 100_000:
                raise ImportFailure("CAD 圖元超過 100,000 個，請分割圖檔")
            kind = entity.dxftype()
            layer = entity.dxf.get("layer", "0")
            if layer == "0":
                layer = inherited_layer
            location = f"配置 {layout} · 圖層 {layer}"
            details = {"layout": layout, "layer": layer, "entity_type": kind,
                       "handle": entity.dxf.get("handle", ""), "units": unit,
                       "block_path": list(ancestry)}
            text = ""
            if kind in {"TEXT", "MTEXT", "ATTRIB"}:
                text = entity.plain_text()
            elif kind == "ATTDEF" and entity.is_const:
                text = entity.plain_text()
            elif kind == "DIMENSION":
                # Measurement is stored in drawing units, not a recovered room size.
                label = entity.dxf.get("text", "")
                if label.strip() or label == "":
                    try:
                        measurement = entity.get_measurement()
                        measure_unit = "degrees" if entity.dimtype in {2, 5} else unit
                        space = "圖塊定義座標，未套用插入比例" if ancestry else "圖面座標"
                        details.update(dimension_type=entity.dimtype, measurement_unit=measure_unit,
                                       measurement_space=space)
                        text = f"尺寸標註：{label or '<>'}；原始量測值 {measurement} {measure_unit}（{space}；未套用標註樣式倍率）"
                    except (ValueError, TypeError, AttributeError):
                        text = f"尺寸標註：{label}" if label else ""
            elif kind == "INSERT":
                walk(entity.attribs, layout, ancestry, layer)
                name = entity.dxf.name
                if name in ancestry or len(ancestry) >= 12:
                    warnings.append(f"{location}：略過循環或過深的圖塊 {name}")
                else:
                    block = doc.blocks.get(name)
                    if block is not None and not block.block.is_xref:
                        walk(block, layout, (*ancestry, name), layer)
                    else:
                        warnings.append(f"{location}：外部參照或遺失圖塊 {name} 未匯入")
            if text.strip():
                evidence.append(Evidence(location, "cad_annotation", text.strip(), details))

    for layout in doc.layouts:
        walk(layout, layout.name)
    warnings.append("CAD 匯入擷取文字、屬性與尺寸標註；未將線段推導為房間或可施工模型。")
    return evidence, list(dict.fromkeys(warnings))


def _markdown(report: ImportReport, evidence: list[Evidence], *, source: str | None = None) -> str:
    meta = {"id": report.document_id, "title": report.filename,
            "source": source or f"本機匯入：{report.filename}",
            "updated": datetime.now(timezone.utc).date().isoformat(),
            "scopes": ["townhouse", "graph"],
            "case": {"category": "reference", "review_status": "unreviewed"}}
    body, groups = [], {}
    for item in evidence:
        if not item.text.strip():
            continue
        groups.setdefault((item.location, item.method), []).append(item.text)
    for (location, method), texts in groups.items():
        # Indent extracted content so its Markdown cannot invent page headings.
        body.append(f"## {location.replace(chr(10), ' ')} · {method}\n" +
                    "\n".join("  " + line for text in texts for line in text.splitlines()))
    return "---\n" + yaml.safe_dump(meta, allow_unicode=True, sort_keys=False) + "---\n" + "\n\n".join(body) + "\n"


def import_file(source: Path, data_dir: Path, *, filename: str | None = None) -> ImportReport:
    """Publish extraction atomically; identical contents + type are idempotent.

    Indexing is a separate, explicit result: extracted != searchable if the E5 model
    is unavailable. No source paths supplied by a web client are opened here.
    """
    from .rag import read_corpus
    name = safe_name(filename or source.name)
    report = ImportReport(name, "failed")
    extension = Path(name).suffix.lower()
    try:
        if extension not in EXTENSIONS:
            raise ImportFailure("支援 PDF、DWG、DXF、PNG、JPG、TIFF、WebP、BMP")
        if not source.is_file() or not 0 < source.stat().st_size <= MAX_BYTES:
            raise ImportFailure("檔案必須非空，且不超過 50 MB")
        with source.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        report.document_id = "import-" + hashlib.sha256((IMPORT_VERSION + extension + digest).encode()).hexdigest()[:24]
        with _LOCK:
            imports = data_dir / "imports"
            imports.mkdir(parents=True, exist_ok=True)
            target = imports / report.document_id
            if (target / "manifest.json").is_file():
                stored = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
                report = ImportReport(**stored["report"])
                report.status = "duplicate"
                return report
            with staging_directory(data_dir, "ingest") as stage:
                original = stage / f"source{extension}"
                shutil.copyfile(source, original)
                if extension == ".pdf":
                    evidence, warnings = extract_pdf(original)
                elif extension in {".dwg", ".dxf"}:
                    cad_path = convert_dwg(original, stage) if extension == ".dwg" else original
                    evidence, warnings = extract_dxf(cad_path)
                    if extension == ".dwg":
                        converter = json.loads((stage / "converter.json").read_text(encoding="utf-8"))
                        warnings.append(f"DWG 經 {converter['engine']} 轉為 DXF；特殊 CAD 物件可能未完整轉換，請核對原圖。")
                        log = (stage / "converter.log").read_bytes()
                        if converter["engine"] == "LibreDWG" and b"ERROR:" in log:
                            warnings.append("DWG 部分物件轉換未完成，請核對原圖；細節保留於 converter.log。")
                else:
                    evidence, warnings = extract_image(original)
                report.warnings = warnings
                if sum(len(e.text) for e in evidence) > MAX_TEXT:
                    raise ImportFailure("擷取文字超過 150,000 字，請分批匯入")
                if not any(e.text.strip() for e in evidence):
                    report.status, report.reason = "empty", "沒有取得可索引文字；請提供含文字的圖面或較清晰的掃描"
                    return report
                report.evidence = [asdict(e) for e in evidence if e.text.strip()]
                (stage / "document.md").write_text(_markdown(report, evidence), encoding="utf-8")
                report.chunks = len(read_corpus(stage))
                existing = sum(len(read_corpus(p)) for p in imports.iterdir() if p.is_dir())
                if existing + report.chunks > 4000:
                    raise ImportFailure("匯入資料超過 4,000 段，請先整理已有資料")
                report.status = "partial" if any("未完成" in w for w in warnings) else "imported"
                (stage / "manifest.json").write_text(json.dumps(
                    {"version": IMPORT_VERSION, "sha256": digest, "report": report.to_dict()},
                    ensure_ascii=False, indent=2), encoding="utf-8")
                # Move only the directory created here; source is never moved/deleted.
                if not stage.resolve().is_relative_to(data_dir.resolve()) or not target.resolve().is_relative_to(data_dir.resolve()):
                    raise ImportFailure("匯入儲存路徑超出資料目錄")
                stage.rename(target)
        return report
    except Exception as exc:
        report.status = "failed"
        LOGGER.warning("Document import failed (%s)", type(exc).__name__, exc_info=not isinstance(exc, ImportFailure))
        report.reason = str(exc) if isinstance(exc, ImportFailure) else f"匯入失敗（{type(exc).__name__}），原始檔案未變更"
        return report


def update_index() -> dict:
    from .rag import enabled, get_retriever
    if not enabled():
        return {"status": "disabled", "reason": "資料已儲存；RAG_ENABLED=0，尚未啟用檢索"}
    try:
        return get_retriever().status()
    except Exception:
        return {"status": "unavailable", "reason": "資料已儲存，但索引尚未就緒；請執行 python -m src.knowledge index"}
