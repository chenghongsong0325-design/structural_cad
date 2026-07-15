/* 前端邏輯:送需求 → 收回每張圖的 SVG → 頁籤切換 + 縮放平移 + 下載 */
"use strict";

const $ = (id) => document.getElementById(id);

let sheets = [];        // 這次生成的所有圖紙 [{label, kind, svg, dxf}]
let current = -1;       // 目前顯示第幾張
let view = { x: 0, y: 0, k: 1 };   // 平移/縮放狀態(切頁籤時重設)
let lastText = "";      // 上次送出的需求(「重新設計」沿用同一句)

// ── 開機自檢:要不要通行碼、伺服器有沒有設 API key ─────────────────
fetch("/api/config").then((r) => r.json()).then((cfg) => {
  if (cfg.needs_code) $("code").classList.remove("hidden");
  if (!cfg.has_api_key) {
    showError("伺服器沒設定 GEMINI_API_KEY,生成功能暫時無法使用。");
  }
});

// ── 範例句:點了直接填進輸入框 ─────────────────────────────────────
$("examples").addEventListener("click", (e) => {
  if (e.target.classList.contains("chip")) {
    $("text").value = e.target.textContent;
    $("text").focus();
  }
});

// ── 生成 ───────────────────────────────────────────────────────────
$("generate").addEventListener("click", () => generate(null));
$("text").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) generate(null);
});
// 重新設計:同一句需求、伺服器隨機換一個 seed → 換一個方案
$("redesign").addEventListener("click", () => generate(lastText));

// seed=null → 首次生成用輸入框的字、由伺服器隨機抽方案;
// 重新設計時把上次的字傳進來(reuseText)。
async function generate(reuseText) {
  const text = (reuseText !== null ? reuseText : $("text").value).trim();
  if (!text) { showError("請先輸入需求描述"); return; }

  const btn = reuseText !== null ? $("redesign") : $("generate");
  btn.disabled = true;
  $("status").textContent = "解析需求、設計格局、出圖中…(約 10~30 秒)";
  hideError();

  const keepLabel = current >= 0 ? sheets[current].label : "1F";

  try {
    const resp = await fetch("/api/generate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text, code: $("code").value }),
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || `伺服器錯誤 (${resp.status})`);

    lastText = text;
    sheets = data.sheets;
    $("summary").textContent = data.summary;
    $("design-note").textContent = data.design_note
      ? "本案設計:" + data.design_note : "";
    $("zip").href = data.zip;
    buildTabs();
    const keep = sheets.findIndex((s) => s.label === keepLabel);   // 沿用當前樓層
    showSheet(keep >= 0 ? keep : 0);
    $("result").classList.remove("hidden");
  } catch (err) {
    showError(err.message);
  } finally {
    btn.disabled = false;
    $("status").textContent = "";
  }
}

function showError(msg) {
  $("error").textContent = msg;
  $("error").classList.remove("hidden");
}
function hideError() { $("error").classList.add("hidden"); }

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
  $("dxf").href = sheets[i].dxf;
  resetView();
}

// ── 縮放/平移(滾輪縮放、拖曳平移、雙擊還原)───────────────────────
function resetView() { view = { x: 0, y: 0, k: 1 }; applyView(); }
function applyView() {
  const pane = $("canvas").querySelector(".pane");
  if (pane) pane.style.transform =
    `translate(${view.x}px, ${view.y}px) scale(${view.k})`;
}

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
