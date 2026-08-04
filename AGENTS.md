# AGENTS.md — 給 AI 助手的專案說明

這份檔案給任何在這個 repo 工作的 AI coding agent（Codex / Claude Code / 其他）看。
先讀完再動手，可以省掉重新摸索的時間，也避免踩到已經解過的坑。

---

## 這個專案在做什麼

**一句中文需求 → 可施工的 DXF 建築平面圖。**

```
「透天三層，基地 19×13 米，三房，地下一層車庫」
        ↓
  平面圖 / 剖面 / 立面 + 圖框標題欄 + 門窗家具 → DXF / PDF
```

Python + [ezdxf](https://ezdxf.mozman.at/)，是一個學生專題。使用者不是資深軟體工程師，
**解釋技術問題時請用生活化比喻，不要堆術語。** 專案的長期目標是「自動設計出對得起
真實審定圖的平面圖」，不只是幾何正確。

---

## 環境與常用指令

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt

python -m pytest -q                    # 全套測試，目前 ~986 passed，必須全綠
python -m src.design.benchmark         # 34 案巡檢 → output/benchmark/report.html
uvicorn src.web.app:app --reload       # 網頁版 http://localhost:8000
```

- 網頁版與 AI 設計師模式需要環境變數 `GEMINI_API_KEY`。
- ⚠️ Gemini 免費額度 **每日 20 次**，AI 模式一次請求吃 2~3 次呼叫（約 7 次/日就用完）。
  除錯時盡量用測試裡的假 client，不要真的一直打 API。
- 產出都在 `output/`（已 gitignore），整個刪掉不影響程式。

---

## 兩條產線（很重要，別搞混）

| | 規則版 | AI 設計師版 |
|---|---|---|
| 入口 | `src/design/layout_generator.py` → `generate_floor_plan` | `src/design/layout/design_loop.py` → `design_building` |
| 怎麼決定格局 | 寫死的建築規則 | Gemini 產「房間關係圖」→ BSP 落實 |
| 適用 | 一般基地、集合住宅 | 只做窄透天：建築寬 5~30m、深 ≥9m |
| 網頁怎麼選 | `src/web/app.py` 的 `_generate_auto` 自動選，AI 掛掉自動退回規則版 | 同左 |

**改東西時先確認你在改哪一條。** 兩條產線共用的只有：`plan_check`（圖面關卡）、
`narrow_house._fix_openings`（收門洞）、`_add_stair_guard_walls`（樓梯導牆）、
Phase 6 家具擺放。

### AI 設計師版的流程

```
自然語言 brief
  → room_graph.propose_room_graph   （Gemini 當設計師，只出房間關係，不出尺寸）
  → sanity_check                    （關係圖本身合不合理）
  → graph_layout.realize_graph_building （關係圖 → 實際切割 + 柱 + 家具 + 動線修復）
  → plan_check                      （硬規則關卡，不合格就換切法重生）
  → design_loop.critique_building   （挑毛病）
  → room_graph.refine_room_graph    （Gemini 重新設計）→ 迴圈
```

收斂迴圈的 fitness = `平均分 − 2 × 問題數`，**只留最高分版本，絕不退步**。

---

## `plan_check.py` — 什麼叫「合格的圖」

`src/design/layout/plan_check.py` 把圖面正確性寫成硬規則。這是整個專案的品質底線，
**動格局相關的程式碼前一定要先讀它**。

**error（擋圖重生，換個切法就能解）：**

1. `room_no_door` 房間沒門
2. `floor_split` 同層室內斷成兩塊
3. `no_entry` 1F 沒大門
4. `entry_upstairs` 樓上外牆開門（門通往空中）
5. `furniture_in_wall` 家具穿牆
6. `door_in_corner` 門卡在房間角落
7. `stair_blocks_door` 門直接開在階梯上（缺起步平台）
8. `stair_side_open` 梯段有一側沒牆（會從旁邊掉下去）
9. `circulation_blocked` 動線走不通

**warning（只回報，要改設計才救得動）：** `room_no_daylight`、`room_oversize`、`room_skinny`

分界原則：**error = 同一份房間關係圖、換個切法就能解決**；warning = 非改設計不可。
新增規則時請照這個原則歸類，錯類會讓產線陷入無限重生。

---

## 已經踩過的坑（別再犯）

- **共壁 vs 獨棟**：透天共壁不能四面開窗，但**中庭骨架是獨棟、四面都能開**。
  誤把共壁規則套到獨棟會讓採光分數掉到 0~50%。用 `party_walls` 開關控制。
- **房間不能用名稱比對**：同一層可能有兩間「臥室」，用名稱找會永遠修錯間。用 id。
- **房間多邊形走的是牆中心線**，擺家具要縮到牆內面（`_inner_room`），否則家具穿牆。
- **家具碰撞尺寸 ≠ 繪圖尺寸**：擺位器用的尺寸較小、畫圖較大，沙發會畫到牆外。
- **`ai_extra` 永遠非空**（裡面帶 engine），要判斷是不是 AI 出的圖請用 `.get("ai_design")`。
- **門角淨距**分級退讓 350/250/150/100，全產線共用，不要各自寫一套。

---

## 設計原則（使用者定調過的，別自作主張改）

- **走道** = 多房間共用的動線才設。房間少的小宅，動線融入客廳，不要硬塞走廊。
- **柱網**：規則等距、跨度 6~9m 最經濟、柱要藏在牆內不卡開口、多層一定上下對齊。
- **Report 慣例**：每個新模組的 Report 類別都要有 `.summary()`（給人看）
  和 `.to_dict()` / `.to_json()`（給程式吃）。

---

## 目前能力邊界

**做得到：** 窄透天（建築寬 5~30m、深 ≥9m）多層、含柱/樓梯/管道間/中庭/家具，
每層動線通、垂直核對齊。實測 15×15=91 分、20×20=92、24×16=92、34×22=90，0 硬錯誤。

**離「合格圖面」還差多少**：`python -m src.design.gap_analysis` 會拿產線畫出來的圖
去對照丙級檢定術科的參考平面圖（台灣的圖；**不用簡體字的圖當基準**），逐項量測後
更新 [docs/gap_analysis.md](docs/gap_analysis.md)。目前 35 項元素涵蓋率 64%。

**圖面標註**（2026-08-03 補，`FloorPlanSpec` 預設開啟，不像 `schedules` 是可選旁註）：
門窗編號 `opening_marks`（與門窗表同一來源 `schedule.opening_codes`）、牆厚引線
`wall_notes`（「15cm RC Wall」）、剖切符號 `section_mark`（A—A）；樓梯改標
`UP 16`／`DN`（`stair.flight_label`，級數由樓梯自己算）。

**做不到 / 尚未做：**
- 寬度 <5m 或深度 <9m（物理下限，骨架排不下）
- 寬度 >30m
- 集合住宅不走 AI 產線（只有規則版）
- **天花板尚未實作** — 這是「對照真實審定圖」還缺的一塊
- **陽台只接規則版透天**（`layout/balcony.py`：二樓以上前後挑出 1.2~2.0m，落地
  橫拉門，不算居室）；AI 關係圖版與兩帶式尚未配陽台。陽台面積也還沒進面積計算表
  （真實圖會列成附屬建物），§41「開口外側有陽台」的採光折減未實作
- 樓梯/電梯井在剖面圖尚未剖出

---

## 工作方式

1. 改動前先跑 `python -m pytest -q` 確認基準是全綠的。
2. 動格局引擎的話，**跑隨機掃描驗證**（過去用 108~208 案掃描抓出系統性錯誤），
   只跑單一案例會漏掉。目標是硬錯誤 0。
3. 每個改動都要附測試。測試不能為了通過而放寬既有斷言。
4. 使用者驗收的方式是「看渲染出來的圖」，所以做完請產出預覽讓他看。
5. commit message 用中文，格式 `type(scope): 說明`，例如
   `fix(layout): 梯段兩側一定有牆 — 補導牆 + 第 9 條硬規則 stair_side_open`。

---

## 延伸文件

| 文件 | 內容 |
|---|---|
| [README.md](README.md) | 快速開始、專案結構、分析工具用法 |
| [docs/LAYOUT_ENGINE.md](docs/LAYOUT_ENGINE.md) | 格局引擎：分析堆疊、各層 API、評分公式 |
| [docs/ARCHITECTURE_V0.7.md](docs/ARCHITECTURE_V0.7.md) | 架構快照：分層、誰可以寫 spec、已知限制 |
| [docs/DEVELOPMENT_GUIDE.md](docs/DEVELOPMENT_GUIDE.md) | 開發原則（九步流程、工程紀律） |
| [ROADMAP.md](ROADMAP.md) | 階段 A~E 的原始路線圖（**只到 E1，之後的進度看 CHANGELOG**） |
| [CHANGELOG.md](CHANGELOG.md) | 版本變更紀錄 |
