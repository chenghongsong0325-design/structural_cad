# structural_cad — 自動住宅平面圖生成

用 Python + [ezdxf](https://ezdxf.mozman.at/) 把**一句中文需求**變成**可施工的
DXF 平面圖**:規則式格局生成 → 家具碰撞修復 → 檢核 → 出圖,並附一整套
**格局分析與品質量化**工具。

```
「透天三層,基地 19×13 米,三房,地下一層車庫」
        ↓
  平面圖 / 剖面 / 立面 + 圖框標題欄 + 門窗家具 → DXF / PDF
```

**目前版本:v0.7**(617 tests · Benchmark 34/34 · 20 pass / 14 warn / 0 fail)

---

## 快速開始

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1          # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
```

### 產生一張圖

```python
from src.design.layout_generator import HouseBrief, generate_floor_plan
from src.drafting.apartment_plan import draw_floor_plan
from src.web.render import _new_doc

spec = generate_floor_plan(HouseBrief(site_width=16000, site_depth=14000,
                                      bedrooms=3))
doc, layers = _new_doc()
draw_floor_plan(doc.modelspace(), spec, layers)
doc.saveas("output/house.dxf")
```

### 網頁版(輸入一句話就出圖)

```powershell
$env:GEMINI_API_KEY = "你的金鑰"     # https://aistudio.google.com 申請
uvicorn src.web.app:app --reload
# 瀏覽器開 http://localhost:8000
```

每層樓一個頁籤(含剖面/立面),可縮放平移、下載 DXF。
產出存在 `output/web/`,整個刪掉也不影響程式。

---

## 專案結構

```
src/
├── design/                     格局引擎
│   ├── room_program.py         面積程式(min/preferred/max + 形狀約束)
│   ├── layout_generator.py     ★ 規則式生成 + validate_spec 守門
│   ├── building_generator.py   多樓層堆疊、柱位對齊
│   ├── collision/              v0.6 家具碰撞引擎(偵測 → 主動修復)
│   ├── connectivity.py         v0.7 連通圖(四張圖,分析層的單一來源)
│   ├── layout_validation.py    v0.7 格局健檢
│   ├── corridor.py             v0.7 動線分析
│   ├── scoring.py              v0.7 七面向評分
│   ├── constraints.py          v0.7 設計常規規則
│   ├── optimizer.py            v0.7 單步微調(唯一會改格局的分析層)
│   ├── report.py               v0.7 Report 序列化基底
│   ├── benchmark.py            34 案巡檢台
│   └── optimization_benchmark.py  Before → Optimize → After
├── drafting/                   製圖引擎(牆/門窗/樓梯/尺寸/圖框/家具 → DXF)
└── web/                        FastAPI 網頁版
docs/                           架構、路線圖、開發原則、各版 release notes
tests/                          617 個測試
output/                         產出(已 gitignore)
```

---

## 格局分析工具

生成之後,可以問「這張圖好不好」:

```python
from src.design.layout_validation import validate_layout   # 格局健檢
from src.design.connectivity import analyze_connectivity   # 連通性
from src.design.corridor import analyze_corridors          # 動線
from src.design.scoring import score_layout                # 七面向評分
from src.design.constraints import check_constraints       # 設計常規

print(score_layout(spec).summary())
# LayoutScore:97.0 / 100(等級 A)
#   connectivity   100.0 ×2.0  9/9 間走得到
#   circulation    100.0 ×1.5  最遠 11.6m / 對角 15.6m (繞路比 0.74)
#   privacy         80.0 ×1.0  4/5 間不直開公共空間(外露:浴廁)
#   ...
```

每個 Report 都有 `.summary()`(給人看)與 `.to_dict()` / `.to_json()`(給程式吃)。

完整說明:**[docs/LAYOUT_ENGINE.md](docs/LAYOUT_ENGINE.md)**

---

## 巡檢與品質

```bash
python -m pytest -q                                    # 617 個測試
python -m src.design.benchmark                         # 34 案巡檢 → report.html
python -m src.design.optimization_benchmark --limit 8  # Before → After 比較
```

Benchmark 會輸出 `output/benchmark/report.html`(自包含,含縮圖),
逐案列出房間比例、動線、家具、DXF 檢核結果。

---

## 部署(Render.com 免費方案)

1. repo 推上 GitHub。
2. [render.com](https://render.com) → **New +** → **Blueprint** → 選這個 repo
   (自動讀 `render.yaml` + `Dockerfile`)。
3. 在 **Environment** 填:
   - `GEMINI_API_KEY` — Gemini 金鑰(**不要**寫進程式碼或 git)
   - `ACCESS_CODE` — 自訂通行碼,防止陌生人消耗 API 額度

> 免費方案 15 分鐘沒人用會休眠,下次打開要等約 30 秒喚醒,屬正常現象。

---

## 文件

| 文件 | 內容 |
|---|---|
| [docs/LAYOUT_ENGINE.md](docs/LAYOUT_ENGINE.md) | **格局引擎**:分析堆疊、各層 API、評分公式、規則清單 |
| [docs/ARCHITECTURE_V0.7.md](docs/ARCHITECTURE_V0.7.md) | 架構快照:分層、誰可以寫 spec、品質基準、已知限制 |
| [docs/DEVELOPMENT_GUIDE.md](docs/DEVELOPMENT_GUIDE.md) | 開發原則(九步流程、工程紀律) |
| [docs/ROADMAP.md](docs/ROADMAP.md) | 路線圖 |
| [CHANGELOG.md](CHANGELOG.md) | 版本變更紀錄 |

---

## DXF 開啟提示

- 中文顯示成問號/方框 → 把文字圖層的字型改成支援中文的字型(標楷體等)。
- 虛線看起來像實線 → 命令列輸入 `LTSCALE` 調整全域線型比例。
