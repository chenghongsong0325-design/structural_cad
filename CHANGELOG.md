# Changelog

本檔記錄各版本的變更。格式參考 [Keep a Changelog](https://keepachangelog.com/),
版本語意採 `主.次-階段`。日期為 ISO 8601(YYYY-MM-DD)。

---

## [v0.6.0] — 2026-07-22

Furniture Collision Engine:把碰撞從「validate 抓到就整份失敗」變成
「有系統地偵測 → 主動修復」,validate 退為安全網。

### Added(新增)
- **Collision Engine**:`src/design/collision/` — 獨立模組,抽象核心是
  `Obstacle`(牆/柱/門迴轉/樓梯/天井/家具皆可包成障礙),新增障礙 = 加一個
  provider,detector/resolver 不必改。
  - `obstacle.py` 資料模型 · `geometry.py` provider · `detector.py` 偵測 ·
    `priority.py` 優先序 · `resolver.py` 修復原語 · `engine.py` 編排。
- **Furniture Collision**(Phase 1):家具×家具、家具×門迴轉。偵測範圍與
  `validate_spec` 現有檢核逐字一致 → 接進流程對合格案例零改動。
- **Wall Collision**(Phase 2):以 Room Polygon(牆中心線)為 barrier,
  用「突出所屬房間面積 > `WALL_TOLERANCE_MM`(5000mm²)」判穿牆——家具貼牆
  合法、穿牆才抓。`fixture_collision_footprint` 讓桌椅組(table4)用收緊
  footprint,與牆演算法分離。
- **Void Collision**(Phase 3-1):天井/挑空為硬障礙(`area > OVERLAP_TOL`)。
  補上破口:形心落在天井的家具原本 `room=None`,連穿牆都驗不到。
- **Stair Collision**(Phase 3-2):梯段為硬障礙,88 座樓梯納入保護。
- **Column Detection**(Phase 3-3):`COLUMN_TOLERANCE_MM = 300`(單位為
  **穿入深度 mm**)。新增 `column_contacts()` 報表,列出所有家具×柱接觸
  (含合法貼柱)與是否超標。
- **Column Resolver**(Phase 4):`try_move()` 新增避柱守衛;柱碰撞**只移動、
  不丟棄**,修不動則保留家具並標記 `ResolveReport.unresolved_column`。
- 測試:`tests/test_collision.py`(29 個)。

### Changed(變更)
- `layout_generator` 僅接線兩處 `resolve_collisions()`
  (`_validate_or_raise` 與 `generate_floor_plan`),**生成邏輯零改動**。
- `detector` 抽出 `HARD_KINDS`(天井/樓梯)共用一條硬障礙判定,避免重複邏輯。

### Fixed(修正)
- **B03(18×13m 透天兩層)餐桌穿牆**:原本穿過實心牆伸進儲藏室約 600mm,
  現自動東移 300mm 修回餐廚。

### Measurement(量測依據)
- 柱容差不是猜的:34 案 941 件家具實測,283 件(30%)貼牆合法壓柱,
  最深 175mm(理論上限 = 柱半 250 − 內牆半厚 60 = 190mm)→ 取 300 留裕度。
  容差若設 150mm 會誤判那 283 件合法家具。
- 樓梯接線前先量測:100 層中 88 層有樓梯,誤判 **0** 件才接線。

### Benchmark
- 34/34 生成成功;通過 20 · 警告 14 · **失敗 0**(四階段前後逐字相同)。
- **Regression = 0**;**DXF 100%**(100/100)、**PNG 100%**(100/100)。
- 生成後對每層再跑一次 `resolve()` 皆為 **no-op(100/100)**。
- 殘留碰撞:牆 0 · 天井 0 · 樓梯 0 · 柱 0;`unresolved_column` 0。

### Tests
- 501 個測試全數通過(v0.5 為 472)。

### Known Limitations
- **Column Resolver 目前不會被觸發**:真實圖面的柱全藏在牆內,伸進室內
  最多 250mm < 容差 300mm。它是為未來**獨立柱**(開放空間落柱/中島腳)預留。
- **Elevator / Shaft 未納入**:Shaft 在本 repo 是「管道牆」= 牆,已被 Phase 2
  覆蓋;Elevator 有 `spec.elevators` 資料但無 provider,且不在 benchmark 範圍。
- `engine` 的「換對方讓開」後備路徑因 `blockers2` 未排除 WALL,自 Phase 2 起
  實質為死碼(不影響現行行為)。
- `geometry.py` 有一行重複的 `VOID` import(dead code)。

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
