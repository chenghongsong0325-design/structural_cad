"""Evidence-only design reports and human reviews; never writes RAG knowledge."""
from datetime import datetime, timezone
import hashlib
import html
import json
from pathlib import Path
import threading
import uuid
from functools import lru_cache

LOCK = threading.RLock()
EVIDENCE_FIELDS = ("job_id", "seed", "summary", "demo", "brief_data", "engine", "ai_design",
                   "engine_fallback", "ai_trajectory", "ai_fitness", "ai_problems", "requirement_check",
                   "validation", "plan_check", "code_check", "spatial_report", "conflicts", "rag", "layout_score", "geometry_snapshot")


@lru_cache(maxsize=1)
def implementation_fingerprint():
    root = Path(__file__).resolve().parents[1]
    sha = hashlib.sha256()
    for path in sorted(root.rglob("*.py")):
        sha.update(path.relative_to(root).as_posix().encode())
        sha.update(path.read_bytes())
    return sha.hexdigest()


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def changes(before, after):
    before, after = before or {}, after or {}
    return [{"field": key, "before": before.get(key), "after": after.get(key)}
            for key in sorted(before.keys() | after.keys()) if before.get(key) != after.get(key)]


def build_report(result, *, text=None, base=None, parent=None, outcome="generated", revision="unknown"):
    snapshot = {key: result.get(key) for key in EVIDENCE_FIELDS}
    snapshot.update(input_text=text, parent_job_id=parent.get("job_id") if parent else None,
                    original_input=(parent.get("design_audit", {}).get("snapshot", {}).get("original_input")
                                    or parent.get("design_audit", {}).get("snapshot", {}).get("input_text")) if parent else text,
                    previous_brief=base, changes=changes(base, result.get("brief_data")) if base is not None else [],
                    previous_requirement_check=parent.get("requirement_check") if parent else None,
                    base_source="saved_parent" if parent else "client_supplied_unverified" if base else "none",
                    outcome=outcome, code_revision=revision, implementation_sha256=implementation_fingerprint())
    return {"version": "design-audit-v1", "created_at": datetime.now(timezone.utc).isoformat(),
            "snapshot_sha256": digest(snapshot), "snapshot": snapshot,
            "scope": "保存當次程式證據；缺值表示未記錄。評分與引用不證明完整設計合格；引用送入提示也不代表模型採用。"}


def read_reviews(job_dir: Path):
    path = job_dir / "human_reviews.json"
    with LOCK:
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else []


def append_review(job_dir: Path, *, report, decision, reviewer, reason, request_id):
    """Atomic append with retry idempotency; review never promotes a case into RAG."""
    with LOCK:
        rows = read_reviews(job_dir)
        existing = next((r for r in rows if r["request_id"] == request_id), None)
        if existing:
            if (existing["decision"], existing["reviewer"], existing["reason"]) != (decision, reviewer, reason):
                raise ValueError("同一筆送出識別碼不能改寫回饋，請重新送出")
            return existing
        if len(rows) >= 200:
            raise ValueError("本案已達 200 筆人工紀錄上限，請先匯出保存")
        entry = {"id": uuid.uuid4().hex, "request_id": request_id, "decision": decision,
                 "reviewer": reviewer, "reason": reason, "created_at": datetime.now(timezone.utc).isoformat(),
                 "snapshot_sha256": report["snapshot_sha256"], "scope": "僅表示人工對初稿的意見，未自動核實為知識庫案例"}
        temporary = job_dir / (".review-" + uuid.uuid4().hex + ".tmp")
        try:
            temporary.write_text(json.dumps(rows + [entry], ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(job_dir / "human_reviews.json")
        finally:
            temporary.unlink(missing_ok=True)
        return entry


def render_html(report, reviews):
    esc = lambda value: html.escape(str(value if value is not None else "未記錄"), quote=True)
    snapshot = report["snapshot"]
    parts = ["<!doctype html><html lang='zh-Hant'><meta charset='utf-8'><title>住宅初稿設計證據報告</title>",
             "<style>body{font:16px/1.7 system-ui;margin:40px auto;max-width:1000px;padding:0 24px;color:#222}table{border-collapse:collapse;width:100%}td,th{border:1px solid #ccc;padding:8px;text-align:left}pre{white-space:pre-wrap;overflow-wrap:anywhere}small{overflow-wrap:anywhere}@media print{details{display:block}pre{font-size:10px}}</style>",
             "<h1>住宅配置初稿・設計證據報告</h1>",
             f"<p>方案：{esc(snapshot.get('job_id'))}｜狀態：{esc(snapshot['outcome'])}｜{esc(report['created_at'])}</p>",
             f"<p>{esc(report['scope'])}</p><small>快照 SHA-256：{esc(report['snapshot_sha256'])}</small>",
             f"<h2>原始與本次需求</h2><p>原始：{esc(snapshot.get('original_input'))}</p><p>本次：{esc(snapshot.get('input_text'))}</p>",
             "<h2>必要與偏好核對</h2><table><tr><th>條件</th><th>優先</th><th>要求</th><th>實際</th><th>結果／依據</th></tr>"]
    for row in (snapshot.get("requirement_check") or {}).get("items", []):
        parts.append("<tr>" + "".join(f"<td>{esc(value)}</td>" for value in
                     [row.get("label"), row.get("priority"), row.get("expected"), row.get("actual"),
                      str(row.get("status")) + "／" + str(row.get("detail", ""))]) + "</tr>")
    parts.append("</table>")
    parts.append("<h2>實際房間與面積</h2><table><tr><th>樓層</th><th>房間</th><th>用途</th><th>牆中心線面積 m²</th><th>起點可達</th></tr>")
    for floor in (snapshot.get("spatial_report") or {}).get("floors", []):
        for node in floor.get("nodes", []):
            parts.append("<tr>" + "".join(f"<td>{esc(v)}</td>" for v in [floor.get("label"), node.get("name"),
                          node.get("kind"), node.get("area_m2"), node.get("reachable")]) + "</tr>")
    parts.append("</table><p>面積與可達性的假設見詳細紀錄；完整生成幾何保存在 JSON 的 geometry_snapshot。</p>")
    for label, keys in [("輸入解析與修改差異", ["brief_data", "previous_brief", "changes", "previous_requirement_check", "base_source", "parent_job_id"]),
                        ("配置路徑與候選紀錄", ["engine", "ai_design", "engine_fallback", "seed", "demo", "code_revision", "implementation_sha256", "ai_trajectory"]),
                        ("幾何、動線與檢查", ["spatial_report", "validation", "plan_check", "code_check", "conflicts"]),
                        ("RAG 實際送入提示的紀錄", ["rag"]), ("輔助評分", ["layout_score"])]:
        parts.append(f"<h2>{label}</h2><details><summary>展開詳細證據</summary><pre>{esc(json.dumps({k: snapshot.get(k) for k in keys}, ensure_ascii=False, indent=2))}</pre></details>")
    parts.append(f"<h2>人工回饋（不等於法規核准）</h2><pre>{esc(json.dumps(reviews, ensure_ascii=False, indent=2))}</pre></html>")
    return "".join(parts)
