"use strict";
(() => {
  const panel = document.getElementById("ergonomics-panel");
  function add(tag, text, parent) {
    const el = document.createElement(tag);
    el.textContent = text;
    parent.appendChild(el);
    return el;
  }
  window.renderErgonomics = report => {
    panel.replaceChildren();
    panel.classList.remove("hidden");
    add("h2", "人因工學・家具操作空間", panel).className = "block-title";
    if (!report || report.status === "unverified") {
      add("p", "本方案的人因操作空間尚未完成檢查；舊方案請重新生成。", panel);
      return;
    }
    add("p", `已檢查 ${report.checked} 件家具／廚具，${report.warnings} 件需要核對。`, panel);
    add("p", report.scope, panel).className = "import-note";
    const repair = report.repair;
    if (repair) {
      if (repair.status === "unverified") {
        add("p", "家具自動修正未完成驗證，已保留原配置；請核對下方尺寸。", panel);
      } else {
        add("p", `自動修正 ${repair.changes.length} 次，待核對家具 ${repair.before} → ${repair.after} 件。${repair.limit_reached ? "已達本次搜尋上限。" : ""}`, panel);
      }
      const history = add("details", "", panel);
      add("summary", "查看家具修正紀錄與未解項目", history);
      add("p", repair.scope, history).className = "import-note";
      for (const change of repair.changes || []) {
        const pose = p => `(${p.insert.map(v => (v/10).toFixed(1)).join(", ")} cm)，方向 ${p.rotation}°`;
        add("p", `${change.floor} ${change.room}・${change.furniture}：${pose(change.before)} → ${pose(change.after)}；連同配套共移動 ${change.moved_count} 件。`, history);
        for (const m of change.measurements || []) {
          add("p", `${m.furniture} ${m.label}：淨深 ${(m.before_mm/10).toFixed(1)} → ${(m.after_mm/10).toFixed(1)} cm／建議 ${(m.target_mm/10).toFixed(1)} cm。`, history);
        }
      }
      for (const item of repair.remaining || []) {
        add("p", `${item.floor} ${item.room}・${item.furniture}：${item.reason}`, history);
      }
    }
    const details = add("details", "", panel);
    add("summary", "查看各側尺寸與來源", details);
    add("p", report.method, details).className = "import-note";
    // Put shortfalls first without mutating the saved response.
    const items = [...(report.items || [])].sort((a,b) =>
      Number(b.status === "needs_review") - Number(a.status === "needs_review"));
    for (const item of items) {
      const row = add("details", "", details);
      add("summary", `${item.floor} ${item.room}・${item.furniture}：${item.status === "needs_review" ? "待調整" : "達本次目標"}`, row);
      for (const z of (item.regions || [])) {
        add("p", `${z.label}：淨深 ${(z.available_mm/10).toFixed(1)} cm／建議 ${(z.target_mm/10).toFixed(1)} cm${z.status === "needs_review" ? "（不足）" : "（達標，上限為建議值）"}`, row);
      }
      add("p", item.basis?.reference || "專題設計目標", row).className = "import-note";
      const source = item.basis?.source_url;
      if (source) {
        try {
          const url = new URL(source);
          if (url.protocol !== "https:") continue;
          const a = add("a", "查看原始指引", row);
          a.href = url.href; a.target = "_blank"; a.rel = "noopener noreferrer";
        } catch (_) { /* Invalid source URLs remain non-interactive. */ }
      }
    }
    add("p", `未檢查：${(report.not_checked || []).join("、")}。另有 ${(report.unchecked_furniture || []).length} 件家具尚無活動尺寸規則。`, details).className = "import-note";
  };
})();
