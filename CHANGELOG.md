# Changelog

本檔記錄各版本的變更。格式參考 [Keep a Changelog](https://keepachangelog.com/),
版本語意採 `主.次-階段`。日期為 ISO 8601(YYYY-MM-DD)。

---

## [v0.5-beta] — 2026-07-21

Dynamic Layout Engine:房間自適應 + 客廳溢位 + 巡檢台。

### Added(新增)
- **Dynamic Room Area**:`src/design/room_program.py` — 面積程式(min/preferred/
  max + min_width/min_depth/aspect_max + priority),加權水位法分配,餘量留院。
- **Living Overflow**:客廳超過 `aspect_max` 自動切出溢位空間,不硬拉長客廳。
- **Program Selector**:`select_overflow_program()` 依樓層/房數/已有房間/尺寸
  決定溢位當書房/家庭廳/多功能室/儲藏室(不固定切成同一種)。
- **Aspect Constraint**:`min_width` / `min_depth` / `aspect_max` 真正參與生成
  (不再只是 Benchmark 評分)。新增房型 `kind="family"`(家庭廳,X15)。
- **Benchmark**:`src/design/benchmark.py` — 34 案巡檢,五面向量化,輸出
  JSON/DXF/PNG/report.html。
- 測試:`tests/test_room_program.py`、`tests/test_benchmark.py`。

### Changed(變更)
- `_plan_x_grid` 新增「從隔牆挑軸線」策略,零孤柱 by construction;
  `BAY_RANGE` 上限 4→6。
- `_generate_house` / `_house_frame` 尺寸改由面積程式決定(取代固定比例)。
- 客廳/溢位家具改貼南外牆,避免擋門迴轉。

### Fixed(修正)
- **客廳過細長**:多樓層非天井客廳長寬比 4.95~6.80 → 2.13~2.20。
- 開口壓柱浮點誤判(`_blocked` +1mm)。
- 客廳段雙窗躲不開柱時自動退回單窗(不再生成失敗)。

### Benchmark
- 34/34 生成成功;通過 20 · 警告 14 · **失敗 0**。
- **Regression = 0**;**DXF 100%**(34/34 存檔+重讀+實體檢核)。
- 客廳過細長(範圍內)全部清零;新增 49 間 Overflow Room。

### Tests
- 472 個測試全數通過。

### Known Limitations
- 天井版 / 孝親房版 / 單層 尚未套 Living Overflow。
- 寬基地的家庭廳本身仍偏長;北帶西段書房未加上限。
- 詳見 [docs/releases/v0.5-beta.md](docs/releases/v0.5-beta.md)。

---

## 更早(施工圖階段,A1~E4)

v0.5 之前的進度(畫圖元素 → 規則式格局 → 自然語言 → 網頁化 → 產品化六件套)
記錄於專案記憶與舊 `ROADMAP.md`,未逐條列入本 CHANGELOG。重點里程碑:

- **E4**:關鍵數字/圖面表格/剖面樓梯/PDF 圖冊/歷史方案/多輪修改。
- **E1~E2**:網頁化(FastAPI)+ 設計變體(seed)。
- **D1~D3**:多樓層(標準層疊高、柱位對齊、剖面/立面)。
- **C1~C2**:規則式格局生成 + 自然語言介面(Gemini)。
- **A~B**:繪圖生產線 + 圖面元素(軸網/牆/門窗/樓梯/尺寸鏈/圖框/家具)。
