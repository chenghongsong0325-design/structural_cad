"""Manage the local RAG model/index without calling Gemini."""
import argparse
import json
import sys
from pathlib import Path

from .rag import (E5Embedder, MODEL, MODEL_SIGNATURE, REVISION,
                  get_retriever, paths, retrieve)


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="本機建築知識庫：建索引／檢索")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("prepare", help="下載公開向量模型並建立索引（只需第一次）")
    sub.add_parser("index", help="重新檢查資料並更新索引（離線）")
    sub.add_parser("prepare-dwg", help="安裝固定版本的本機 DWG 轉檔工具（Windows x64）")
    search = sub.add_parser("search", help="檢查檢索結果，不呼叫 Gemini")
    search.add_argument("query")
    search.add_argument("--stage", choices=["parse", "townhouse", "graph"], default="townhouse")
    ingest = sub.add_parser("import", help="自動擷取 PDF／DWG／DXF／圖片並更新索引（可傳入資料夾）")
    ingest.add_argument("paths", nargs="+")
    web = sub.add_parser("research", help="搜尋公開網頁／文字 PDF 並加入 RAG")
    web.add_argument("query")
    web.add_argument("--limit", type=int, choices=range(1, 6), default=3)
    args = parser.parse_args()
    if args.command == "research":
        from .web_research import research
        result = research(args.query, paths()[1].parent, args.limit)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if result["status"] != "ready":
            raise SystemExit(1)
        return
    if args.command == "prepare-dwg":
        from .prepare_dwg import prepare
        print(json.dumps(prepare(paths()[1].parent), ensure_ascii=False, indent=2))
        return
    if args.command == "import":
        from .importers import EXTENSIONS, import_file, update_index
        _, index, _ = paths()
        files = []
        for value in args.paths:
            path = Path(value)
            files.extend(sorted(p for p in path.rglob("*") if p.is_file() and p.suffix.lower() in EXTENSIONS)
                         if path.is_dir() else [path])
        if not files:
            parser.error("沒有找到支援的檔案")
        reports = [import_file(p, index.parent).to_dict() for p in dict.fromkeys(files)]
        state = update_index() if any(r["status"] in {"imported", "partial", "duplicate"} for r in reports) else {"status": "not_updated"}
        print(json.dumps({"files": reports, "index": state}, ensure_ascii=False, indent=2))
        if any(r["status"] in {"failed", "empty", "partial"} for r in reports) or state["status"] != "ready":
            raise SystemExit(1)
        return
    if args.command == "prepare":
        from sentence_transformers import SentenceTransformer
        _, _, model_dir = paths()
        try:
            E5Embedder(model_dir).encode(["住宅"])
        except (OSError, ValueError):
            model = SentenceTransformer(MODEL, revision=REVISION, device="cpu",
                                        trust_remote_code=False, token=False,
                                        cache_folder=str(model_dir.parent / "downloads"))
            model.max_seq_length = 512
            model.save_pretrained(str(model_dir))
            (model_dir / "rag_model.json").write_text(
                json.dumps({"signature": MODEL_SIGNATURE}), encoding="utf-8")
    if args.command in {"prepare", "index"}:
        print(json.dumps(get_retriever().status(), ensure_ascii=False, indent=2))
    else:
        result = retrieve(args.query, args.stage)
        print(result.to_json())
        if result.status == "unavailable":
            raise SystemExit(1)


if __name__ == "__main__":
    main()
