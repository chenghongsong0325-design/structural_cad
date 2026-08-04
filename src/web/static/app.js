/* 前端邏輯:送需求 → 收回每張圖的 SVG → 頁籤切換 + 縮放平移 + 下載
   E4:關鍵數字(建蔽/容積/造價)、多輪修改、歷史方案、PDF 圖冊。 */
"use strict";

const $ = (id) => document.getElementById(id);

let sheets = [];        // 目前顯示的圖紙 [{label, kind, svg, dxf}]
let current = -1;       // 目前顯示第幾張
let view = { x: 0, y: 0, k: 1 };   // 平移/縮放狀態(切頁籤時重設)
let lastText = "";      // 上次送出的需求(「重新設計」沿用同一句)
let lastBriefData = null;  // 上次解析出的需求 dict(多輪修改的底)
let lastSeed = null;       // 上次方案的 seed(修改時沿用 → 格局不重骰)
let lastJobId = null;      // 上次方案的 job_id(家具評分用)

// 12 個子分數的中文標籤(家具評分卡用)
const SUB_LABELS = {
  furniture: "家具齊全", collision: "碰撞", walkway: "走道",
  circulation: "房內動線",
  human_clearance: "人體活動", constraint: "擺放偏好",
  pair_constraint: "家具關聯", room_semantic: "房間機能",
  space_efficiency: "空間效率", furniture_density: "家具密度",
  symmetry: "對稱", natural_lighting: "採光", window_usage: "窗戶可用",
};

// ── 開機自檢:要不要通行碼、伺服器有沒有設 API key ─────────────────
fetch("/api/config").then((r) => r.json()).then((cfg) => {
  if (cfg.needs_code) $("code").classList.remove("hidden");
  if (!cfg.has_api_key) {
    showError("伺服器沒設定 GEMINI_API_KEY,生成功能暫時無法使用。");
  }
});

// ── 範例句:點了直接填進輸入框 ─────────────────────────────────────
// chip 上顯示的是短標籤(排起來才整齊),真正要送出的整句放 data-text;
// 設計建議的 chip 是 JS 生的、沒有 data-text,就退回讀顯示文字。
$("examples").addEventListener("click", (e) => {
  if (!e.target.classList.contains("chip")) return;
  $("text").value = e.target.dataset.text
    || e.target.textContent.split("(")[0].split("(")[0].trim();
  $("text").focus();
});

// ── 生成 / 重新設計 / 修改 ─────────────────────────────────────────
$("generate").addEventListener("click", () => generate(null));
$("text").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) generate(null);
});
// 重新設計:同一句需求、伺服器隨機換一個 seed → 換一個方案
$("redesign").addEventListener("click", () => generate(lastText));
// 多輪修改:指令 + 上一輪需求 dict(base)+ 上一輪 seed(格局不重骰)
$("modify").addEventListener("click", modify);
$("modify-text").addEventListener("keydown", (e) => {
  if (e.key === "Enter") modify();
});

// ── 家具配置評分(Phase 6-7;只評分,不搬家具)──────────────────────
$("score-btn").addEventListener("click", scoreLayout);

async function scoreLayout() {
  if (!lastJobId) { showError("請先生成一個方案,再做家具評分"); return; }
  const btn = $("score-btn");
  btn.disabled = true;
  $("status").textContent = "評估整棟家具配置中…(約 3~10 秒)";
  hideError();
  try {
    const resp = await fetch("/api/score", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ job_id: lastJobId, code: $("code").value }),
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || `伺服器錯誤 (${resp.status})`);
    renderLayoutScore(data);
    $("layout-score").scrollIntoView({ behavior: "smooth", block: "nearest" });
  } catch (err) {
    showError(err.message);
  } finally {
    btn.disabled = false;
    $("status").textContent = "";
  }
}

// 家具評分卡:總分 + 等第 + 12 項子分數 + 各層 + 各房機能檢查。
function renderLayoutScore(data) {
  const box = $("layout-score");
  const grade = data.grade || "—";
  const chips = Object.entries(SUB_LABELS).map(([k, label]) => {
    const v = (data.sub_scores && data.sub_scores[k] != null)
      ? data.sub_scores[k] : 0;
    return `<span class="sub" title="${label}"><b>${Math.round(v)}</b>${label}</span>`;
  }).join("");
  const floors = (data.floors || []).length > 1
    ? `<div class="floors">` + data.floors.map((f) =>
        `<span class="floor-chip">${f.label} <b>${f.overall}</b> [${f.grade}]</span>`
      ).join("") + `</div>`
    : "";
  const rooms = (data.rooms || []).map((r) => {
    const miss = (r.missing && r.missing.length)
      ? ` · 缺 ${r.missing.join("、")}` : "";
    return `<span class="room-chip">${r.room}` +
      `<em>家具 ${r.furniture} 件 · 機能 ${Math.round(r.semantic)}${miss}</em>` +
      `</span>`;
  }).join("");
  box.innerHTML =
    `<div class="score-head">` +
      `<span class="grade grade-${grade.replace("+", "plus")}">${grade}</span>` +
      `<div class="score-num"><b>${data.overall_score}</b><span>整棟家具配置分數</span></div>` +
    `</div>` +
    floors +
    `<div class="subs">${chips}</div>` +
    (rooms ? `<div class="rooms">${rooms}</div>` : "") +
    `<span class="hint">評分為啟發式輔助,非法規檢討;只評估產生器擺好的家具,不搬動、不改牆與房間。</span>`;
  box.classList.remove("hidden");
}

// seed=null → 首次生成用輸入框的字、由伺服器隨機抽方案;
// 重新設計時把上次的字傳進來(reuseText)。
async function generate(reuseText) {
  const text = (reuseText !== null ? reuseText : $("text").value).trim();
  if (!text) { showError("請先輸入需求描述"); return; }
  const btn = reuseText !== null ? $("redesign") : $("generate");
  await requestPlan({ text, code: $("code").value }, btn, text);
}

async function modify() {
  const instruction = $("modify-text").value.trim();
  if (!instruction) { showError("請先輸入修改指令"); return; }
  if (!lastBriefData) { showError("還沒有方案可修改,請先生成"); return; }
  const ok = await requestPlan({
    text: instruction,
    code: $("code").value,
    base: lastBriefData,
    seed: lastSeed,
  }, $("modify"), lastText + "(" + instruction + ")");
  if (ok) $("modify-text").value = "";
}

// ── 生成進度燈 ─────────────────────────────────────────────────────
// 出圖要 10~60 秒。只寫一行「設計中…」的話,使用者會以為當掉了;四段亮燈
// 是**估時**的動畫(伺服器沒有回報進度),所以最後一段停著等,不會自己跑完。
const PROGRESS_AT = [0, 3000, 9000, 16000];   // 各段大約什麼時候亮
let progressTimers = [];

function startProgress() {
  stopProgress();
  const lis = [...$("progress").children];
  $("progress").classList.remove("hidden");
  PROGRESS_AT.forEach((ms, i) => {
    progressTimers.push(setTimeout(() => {
      lis.forEach((li, j) => {
        li.classList.toggle("done", j < i);
        li.classList.toggle("now", j === i);
      });
    }, ms));
  });
}

function stopProgress() {
  progressTimers.forEach(clearTimeout);
  progressTimers = [];
  const box = $("progress");
  box.classList.add("hidden");
  [...box.children].forEach((li) => li.classList.remove("done", "now"));
}

// 共用請求流程:送出 → 成功就渲染結果。回傳是否成功。
async function requestPlan(body, btn, textForRedesign) {
  btn.disabled = true;
  $("status").textContent = "設計中…(約 10~60 秒)";
  startProgress();
  hideError();
  try {
    const resp = await fetch("/api/generate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || `伺服器錯誤 (${resp.status})`);
    lastText = textForRedesign;
    applyResult(data);
    return true;
  } catch (err) {
    showError(err.message);
    return false;
  } finally {
    btn.disabled = false;
    $("status").textContent = "";
    stopProgress();
  }
}

// 把一包生成結果(新生成或歷史載入)渲染到畫面。
function applyResult(data) {
  const keepLabel = current >= 0 && sheets[current] ? sheets[current].label : "1F";
  sheets = data.sheets;
  lastJobId = data.job_id || null;
  $("layout-score").classList.add("hidden");   // 新方案 → 舊評分卡收起
  lastBriefData = data.brief_data || null;
  lastSeed = (data.seed === undefined) ? null : data.seed;
  // 示範模式(DEMO_MODE):LLM 的回覆是錄好的回放,要明講,不能讓人以為當場打了 Gemini。
  $("summary").textContent = (data.demo ? "【示範模式・離線回放】" : "") + data.summary;
  $("design-note").textContent = data.design_note
    ? "本案設計:" + data.design_note : "";
  renderAiPanel(data);
  renderMetrics(data.metrics || null);
  renderSuggestions(data.suggestions || []);
  $("zip").href = data.zip;
  if (data.pdf) { $("pdf").href = data.pdf; $("pdf").classList.remove("hidden"); }
  else { $("pdf").classList.add("hidden"); }
  buildTabs();
  const keep = sheets.findIndex((s) => s.label === keepLabel);   // 沿用當前樓層
  showSheet(keep >= 0 ? keep : 0);
  $("result").classList.remove("hidden");
  document.body.classList.add("has-result");   // 收掉舞台的空狀態線稿
  $("about").open = false;      // 有方案了 → 能力說明收起來,側欄留給結果
  $("history").classList.add("hidden");
  loadRecent();                 // 剛存的這一筆要出現在「最近」
}

function showError(msg) {
  $("error").textContent = msg;
  $("error").classList.remove("hidden");
}
function hideError() { $("error").classList.add("hidden"); }

// ── 關鍵數字:建蔽率/容積率/樓地板/粗估造價(老闆看的那排數字)──────
function renderMetrics(m) {
  const box = $("metrics");
  box.innerHTML = "";
  if (!m) { box.classList.add("hidden"); return; }
  const items = [
    ["建蔽率", m.coverage_pct + "%"],
    ["容積率", m.far_pct + "%"],
    ["基地", m.site_area_m2 + " m²"],
    ["地上樓地板", m.floors_area_m2 + " m²"],
  ];
  if (m.basement_m2 > 0) items.push(["地下", m.basement_m2 + " m²"]);
  items.push(["總坪數", m.total_ping + " 坪"]);
  // 造價獨立一整列(wide):這是評審第一個問的數字,不該跟其他量體擠在半格裡
  items.push(["粗估造價", "約 " + m.est_cost_wan.toLocaleString() + " 萬", "wide"]);
  items.forEach(([k, v, cls]) => {
    const el = document.createElement("span");
    el.className = "metric" + (cls ? " " + cls : "");
    el.innerHTML = `<b>${v}</b><span>${k}</span>`;
    box.appendChild(el);
  });
  const note = document.createElement("span");
  note.className = "hint";
  note.textContent = "量體粗估,非法規檢討";
  box.appendChild(note);
  box.classList.remove("hidden");
}

// ── AI 設計師收斂軌跡:每次迭代的分數/問題數 + 收斂後仍待改的問題 ──────────
function renderAiPanel(data) {
  const box = $("ai-panel");
  if (!data) { box.classList.add("hidden"); box.innerHTML = ""; return; }
  if (!data.ai_design) {                    // 規則引擎:只顯示圖面檢查結果
    const html = renderPlanCheck(data.plan_check) + renderCodeCheck(data.code_check);
    box.innerHTML = html;
    box.classList.toggle("hidden", !html);
    return;
  }
  const traj = (data.ai_trajectory || []).map((h) =>
    `<span class="ai-step">${h.iter}｜分 ${Math.round(h.mean_score)} · 問題 ${h.n_problems}</span>`
  ).join(`<span class="ai-arrow">→</span>`);
  const probs = (data.ai_problems || []).length
    ? `<div class="ai-problems"><b>收斂後仍待改(${data.ai_problems.length}):</b>` +
      data.ai_problems.map((p) => `<div>· ${p}</div>`).join("") + `</div>`
    : `<div class="ai-problems ok"><b>收斂後無明顯問題</b></div>`;
  box.innerHTML =
    `<div class="ai-head">AI 設計師:設計 → 落實 → 挑毛病 → 重設計,擇優</div>` +
    `<div class="ai-traj">${traj}</div>` + renderPlanCheck(data.plan_check) +
    renderCodeCheck(data.code_check) + probs;
  box.classList.remove("hidden");
}

// ── 圖面正確性檢查:每房有門/室內連通/有大門/家具不穿牆/動線通 ──────────
function renderPlanCheck(c) {
  if (!c) return "";
  const items = (c.issues || []).filter((i) => i.severity === "error");
  if (c.ok) {
    return `<div class="ai-problems ok"><b>圖面檢查通過</b>　每間房都有門、室內走得通、` +
           `有臨路大門、家具不穿牆、動線暢通` +
           (c.n_warnings ? `(另有 ${c.n_warnings} 項設計建議)` : "") + `</div>`;
  }
  return `<div class="ai-problems"><b>圖面檢查未過(${items.length}):</b>` +
         items.map((i) => `<div>· ${i.floor}${i.room ? "/" + i.room : ""}:${i.detail}</div>`)
              .join("") + `</div>`;
}

// ── 法規檢查(建築技術規則):樓梯尺寸/居室採光…──────────────────────────
function renderCodeCheck(c) {
  if (!c) return "";
  const items = (c.issues || []).filter((i) => i.severity === "violation");
  if (c.ok) {
    return `<div class="ai-problems ok"><b>法規檢查通過</b>　樓梯級高級深/梯段寬/平臺深` +
           `(施工編§33)、居室採光開口 ≥1/8(§40)` +
           (c.n_warnings ? `(另有 ${c.n_warnings} 項慣例建議)` : "") + `</div>`;
  }
  return `<div class="ai-problems"><b>法規檢查未過(${items.length}):</b>` +
         items.map((i) => `<div>· [${i.article}] ${i.floor}${i.room ? "/" + i.room : ""}:${i.detail}</div>`)
              .join("") + `</div>`;
}

// ── 歷史方案:列出最近生成、點了重新載入 ───────────────────────────
$("history-btn").addEventListener("click", async () => {
  const box = $("history");
  if (!box.classList.contains("hidden")) { box.classList.add("hidden"); return; }
  box.innerHTML = "載入中…";
  box.classList.remove("hidden");
  try {
    const items = await (await fetch("/api/history")).json();
    box.innerHTML = "";
    if (!items.length) { box.textContent = "還沒有生成紀錄。"; return; }
    items.forEach((m) => {
      const b = document.createElement("button");
      b.className = "history-item";
      const when = m.created ? new Date(m.created).toLocaleString() : "";
      b.innerHTML = `<span class="h-text">${m.text}</span>` +
                    `<span class="h-meta">${when} · ${m.summary || ""}</span>`;
      b.addEventListener("click", () => loadJob(m));
      box.appendChild(b);
    });
  } catch (err) {
    box.textContent = "歷史載入失敗:" + err.message;
  }
});

// ── 最近方案:開頁就抓最新 3 筆列在側欄(沒紀錄就整塊不出現)──────────
// 跟「歷史方案」按鈕是同一份資料,差別在這裡是隨手可點的捷徑,那裡是完整清單。
async function loadRecent() {
  try {
    const items = await (await fetch("/api/history")).json();
    const box = $("recent");
    box.innerHTML = "";
    if (!items.length) { $("recent-block").classList.add("hidden"); return; }
    items.slice(0, 3).forEach((m) => {
      const b = document.createElement("button");
      b.className = "recent-item";
      b.innerHTML = `<span class="h-text">${m.text}</span>` +
                    `<span class="h-meta">${m.summary || ""}</span>`;
      b.addEventListener("click", () => loadJob(m));
      box.appendChild(b);
    });
    $("recent-block").classList.remove("hidden");
  } catch (err) {
    $("recent-block").classList.add("hidden");   // 抓不到就當沒這塊,不吵使用者
  }
}
loadRecent();

async function loadJob(meta) {
  $("status").textContent = "載入歷史方案…";
  try {
    const resp = await fetch(`/api/jobs/${meta.job_id}/result`);
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || "載入失敗");
    lastText = meta.text || lastText;
    $("text").value = meta.text || $("text").value;
    applyResult(data);
  } catch (err) {
    showError(err.message);
  } finally {
    $("status").textContent = "";
  }
}

// ── 設計建議:基地還放得下什麼(升級房數/加車庫/加樓層),點了直接重生成 ──
function renderSuggestions(items) {
  const box = $("suggestions");
  box.innerHTML = "";
  if (!items.length) { box.classList.add("hidden"); return; }
  const hint = document.createElement("span");
  hint.className = "hint";
  hint.textContent = "這塊基地還可以:";
  box.appendChild(hint);
  items.forEach((s) => {
    const b = document.createElement("button");
    b.className = "chip";
    b.textContent = s.label;
    b.title = s.note + "\n→ " + s.text;   // 滑鼠停留看細節與完整需求句
    b.addEventListener("click", () => {
      $("text").value = s.text;           // 換成建議的需求句,直接重新生成
      generate(null);
    });
    box.appendChild(b);
  });
  box.classList.remove("hidden");
}

// ── 頁籤 ───────────────────────────────────────────────────────────
function buildTabs() {
  const bar = $("tabs");
  bar.innerHTML = "";
  sheets.forEach((s, i) => {
    const b = document.createElement("button");
    b.className = "tab";
    b.textContent = s.label;
    b.addEventListener("click", () => showSheet(i));
    bar.appendChild(b);
  });
}

function showSheet(i) {
  current = i;
  [...$("tabs").children].forEach((b, j) =>
    b.classList.toggle("active", j === i));

  const pane = document.createElement("div");
  pane.className = "pane";
  pane.innerHTML = sheets[i].svg;
  const svg = pane.querySelector("svg");
  if (svg) {                       // 拿掉固定 mm 尺寸,改成填滿視窗、自由縮放
    svg.removeAttribute("width");
    svg.removeAttribute("height");
  }
  $("canvas").replaceChildren(pane);
  setDxfLink(sheets[i]);
  resetView();
}

// DXF 下載:優先用回應裡內嵌的檔案(dxf_b64)。
// 為什麼:伺服器(Render 免費方案)的硬碟是暫時的,重新部署或閒置休眠就清空,
// /api/jobs/... 那條連結會 404;內嵌的這份在瀏覽器裡,關掉分頁前都還在。
let dxfObjectUrl = null;
function setDxfLink(sheet) {
  const a = $("dxf");
  if (dxfObjectUrl) { URL.revokeObjectURL(dxfObjectUrl); dxfObjectUrl = null; }
  if (sheet.dxf_b64) {
    const bin = atob(sheet.dxf_b64);
    const buf = new Uint8Array(bin.length);
    for (let k = 0; k < bin.length; k++) buf[k] = bin.charCodeAt(k);
    dxfObjectUrl = URL.createObjectURL(new Blob([buf],
      { type: "application/dxf" }));
    a.href = dxfObjectUrl;
    a.download = (sheet.label || "plan") + ".dxf";
  } else {                          // 沒內嵌(檔案太大)→ 退回伺服器連結
    a.href = sheet.dxf;
    a.removeAttribute("download");
  }
}

// ── 縮放/平移(滾輪縮放、拖曳平移、雙擊還原)───────────────────────
function resetView() { view = { x: 0, y: 0, k: 1 }; applyView(); }
function applyView() {
  const pane = $("canvas").querySelector(".pane");
  if (pane) pane.style.transform =
    `translate(${view.x}px, ${view.y}px) scale(${view.k})`;
}

// 工具列按鈕:以畫面正中央為圓心縮放(滾輪是以游標為圓心,兩者不衝突)
function zoomBy(factor) {
  const rect = $("canvas").getBoundingClientRect();
  const mx = rect.width / 2, my = rect.height / 2;
  const k = Math.min(40, Math.max(0.2, view.k * factor));
  view.x = mx - (mx - view.x) * (k / view.k);
  view.y = my - (my - view.y) * (k / view.k);
  view.k = k;
  applyView();
}
$("zoom-in").addEventListener("click", () => zoomBy(1.3));
$("zoom-out").addEventListener("click", () => zoomBy(1 / 1.3));
$("zoom-reset").addEventListener("click", resetView);

$("canvas").addEventListener("wheel", (e) => {
  e.preventDefault();
  const rect = $("canvas").getBoundingClientRect();
  const mx = e.clientX - rect.left, my = e.clientY - rect.top;
  const factor = e.deltaY < 0 ? 1.2 : 1 / 1.2;
  const k = Math.min(40, Math.max(0.2, view.k * factor));
  // 以滑鼠位置為中心縮放:游標指到哪,放大就往哪裡鑽
  view.x = mx - (mx - view.x) * (k / view.k);
  view.y = my - (my - view.y) * (k / view.k);
  view.k = k;
  applyView();
}, { passive: false });

let drag = null;
$("canvas").addEventListener("pointerdown", (e) => {
  drag = { x: e.clientX - view.x, y: e.clientY - view.y };
  $("canvas").setPointerCapture(e.pointerId);
});
$("canvas").addEventListener("pointermove", (e) => {
  if (!drag) return;
  view.x = e.clientX - drag.x;
  view.y = e.clientY - drag.y;
  applyView();
});
$("canvas").addEventListener("pointerup", () => { drag = null; });
$("canvas").addEventListener("dblclick", resetView);
