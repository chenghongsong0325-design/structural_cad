"use strict";
(() => {
  const el = id => document.getElementById(id);
  let report = null, job = null, revision = 0, pendingReview = null;
  const labels = {accepted:"接受初稿", revision_needed:"需要修改", not_applicable:"不適用"};
  async function post(url, data) {
    const response = await fetch(url, {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({...data, code:el("code").value})});
    if (!response.ok) {
      const error = await response.json();
      throw new Error(typeof error.detail === "string" ? error.detail : "請檢查欄位或稍後重試");
    }
    return response;
  }
  function download(blob, name) {
    const url = URL.createObjectURL(blob), link = document.createElement("a");
    link.href = url; link.download = name; link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  function reviews(rows) {
    el("review-history").replaceChildren();
    for (const row of rows) {
      const p = document.createElement("p");
      p.textContent = `${row.created_at} · ${row.reviewer} · ${labels[row.decision] || row.decision}：${row.reason}`;
      el("review-history").appendChild(p);
    }
  }
  window.renderDesignAudit = data => {
    revision += 1; const current = revision;
    report = data.design_audit || null; job = data.job_id || null; pendingReview = null;
    el("audit-panel").classList.toggle("hidden", !report && !job);
    el("audit-status").textContent = "";
    el("review-reason").value = ""; reviews([]);
    el("human-review-form").classList.toggle("hidden", !job);
    el("audit-html").disabled = !job;
    el("audit-summary").textContent = job ? `方案 ${job}：報告與回饋綁定此方案。` : "本次未交付圖面：可下載失敗診斷 JSON，不能對前次圖面誤記本次回饋。";
    el("audit-preview").textContent = report ? JSON.stringify(report, null, 2) : "舊方案的部分資料未記錄。";
    if (job) post(`/api/jobs/${job}/audit`, {format:"json"}).then(r => r.json()).then(data => {
      if (current !== revision) return;
      report = data; reviews(data.human_reviews || []);
      el("audit-preview").textContent = JSON.stringify(data, null, 2);
    }).catch(error => {if (current === revision) el("audit-status").textContent = error.message;});
  };
  for (const format of ["html", "json"]) el(`audit-${format}`).addEventListener("click", async () => {
    const current = revision, target = job;
    try {
      let blob;
      if (target) blob = await (await post(`/api/jobs/${target}/audit`, {format})).blob();
      else if (report && format === "json") blob = new Blob([JSON.stringify(report, null, 2)], {type:"application/json"});
      else return;
      download(blob, `${target || "未交付候選"}-設計報告.${format}`);
      if (current === revision) el("audit-status").textContent = "已下載報告。";
    } catch(error) { if (current === revision) el("audit-status").textContent = error.message; }
  });
  el("review-save").addEventListener("click", async () => {
    if (!job) return;
    const current = revision, target = job;
    const value = {decision:el("review-decision").value, reviewer:el("reviewer").value.trim(), reason:el("review-reason").value.trim()};
    if (!value.reviewer || !value.reason) {el("audit-status").textContent = "請填寫核對者與具體原因。"; return;}
    if (!pendingReview || pendingReview.payload !== JSON.stringify(value)) pendingReview = {payload:JSON.stringify(value), id:crypto.randomUUID()};
    el("review-save").disabled = true;
    try {
      await post(`/api/jobs/${target}/reviews`, {...value, request_id:pendingReview.id});
      const data = await (await post(`/api/jobs/${target}/audit`, {format:"json"})).json();
      if (current === revision) {
        reviews(data.human_reviews || []); report = data;
        el("audit-preview").textContent = JSON.stringify(data, null, 2);
        el("audit-status").textContent = "人工回饋已保存；未自動加入 RAG。請下載報告備份。";
      }
    } catch(error) {if(current === revision) el("audit-status").textContent = error.message;}
    finally {el("review-save").disabled = false;}
  });
  el("rag-check").addEventListener("click", async () => {
    el("rag-check").disabled = true; el("rag-check-status").textContent = "正在檢查模型與索引…";
    try {
      const response = await fetch("/api/rag/status"); const state = await response.json();
      el("rag-check-status").textContent = state.status === "ready" ? `已就緒：${state.documents} 份文件、${state.chunks} 段、${state.dimensions} 維。${state.embedding_signature}` : `尚未就緒：${state.reason || state.status}`;
    } catch(error) {el("rag-check-status").textContent = error.message;}
    finally {el("rag-check").disabled = false;}
  });
  el("rag-inspect").addEventListener("click", async () => {
    const query = el("rag-inspect-query").value.trim();
    if (!query) {el("rag-inspect-result").textContent = "請輸入檢索文字。";return;}
    el("rag-inspect").disabled = true; el("rag-inspect-result").textContent = "正在檢索…";
    try {
      const response = await post("/api/rag/search", {query, stage:el("rag-inspect-stage").value, top_k:3});
      const data = await response.json();
      el("rag-inspect-result").textContent = "selected＝檢索選中；below_threshold＝低於門檻；same_section＝同節去重；top_k_limit＝超過筆數；context_budget＝提示長度限制。\n此獨立查詢沒有送給生成模型。\n" + JSON.stringify(data, null, 2);
    } catch(error) {el("rag-inspect-result").textContent = error.message;}
    finally {el("rag-inspect").disabled = false;}
  });
})();
