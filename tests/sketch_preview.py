"""Browser verification using an existing result; no LLM or new generation.

python tests/sketch_preview.py --result output/web/<job>/result.json
Open http://127.0.0.1:8016/ for assertions or /preview for the real frontend.
"""
import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8016)
    args = parser.parse_args()
    result = json.loads(args.result.read_text(encoding="utf-8"))
    root = Path(__file__).resolve().parents[1]
    static = root / "src/web/static"

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            route = urlsplit(self.path).path
            mime = "application/json; charset=utf-8"
            if route == "/api/config":
                body = json.dumps({"needs_code": False, "has_api_key": True, "demo": True})
            elif route == "/api/history":
                body = json.dumps([{"job_id": "preview", "text": "既有方案・素描動畫預覽", "summary": result.get("summary", "")}], ensure_ascii=False)
            elif route == "/api/jobs/preview/result":
                body = json.dumps(result, ensure_ascii=False)
            elif route == "/fixture.svg":
                body, mime = result["sheets"][0]["svg"], "image/svg+xml"
            elif route == "/":
                body, mime = (root / "tests/sketch_browser.html").read_text(encoding="utf-8"), "text/html; charset=utf-8"
            elif route in {"/preview", "/index.html", "/style.css", "/app.js", "/spatial.js", "/sketch.js"}:
                name = "index.html" if route == "/preview" else route[1:]
                body = (static / name).read_text(encoding="utf-8")
                if name == "index.html":
                    body = body.replace("住宅配置初稿 · 需求與圖面逐項核對", "動畫預覽 · 請點最近的既有方案；產圖請用原本網站")
                    for control in ("generate", "modify", "redesign", "score-btn", "research-btn", "import-btn"):
                        body = body.replace(f'id="{control}"', f'id="{control}" disabled')
                mime = {".html": "text/html", ".css": "text/css", ".js": "text/javascript"}[Path(name).suffix] + "; charset=utf-8"
            else:
                self.send_error(404)
                return
            payload = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    print(f"Sketch tests: http://127.0.0.1:{args.port}/", flush=True)
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
