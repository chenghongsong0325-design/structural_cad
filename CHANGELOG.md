# Changelog

本檔記錄各版本的變更。格式參考 [Keep a Changelog](https://keepachangelog.com/),
版本語意採 `主.次-階段`。日期為 ISO 8601(YYYY-MM-DD)。

## 2026-09-24 — 真實圖面的逐筆素描動畫

- 新圖與首次切換的樓層，使用原始生成 SVG 的暫時副本逐筆描線，附紙色背景與移動筆尖；完成或跳過即還原原圖，DXF／PDF 不變。
- 支援慢速（20 秒）、一般（10 秒）、快速（5 秒）、重播及直接看完成圖；尊重系統減少動態效果偏好。
- 切換圖紙會取消舊影格並移除副本。新方案有不同張數時，舊動畫的完成事件不會讀到新方案的無效樓層。
- 瀏覽器測試：`python tests/sketch_preview.py --result output/web/<job>/result.json`，開啟 `http://127.0.0.1:8016/` 執行 8 項真實 SVG 測試，`/preview` 操作前端。使用既有結果、不呼叫模型。驗證包含原圖逐字還原、虛線／填色／轉換、速度、跳過、連續重播、減少動態及移除圖紙。

## 2026-09-23 — 空間分析、衝突定位與指定房數

- 以實際多邊形與門洞建立逐層房間關係圖，區分相鄰、門及開放通道；可點選查看連通路徑、尺寸、家具與檢核依據，匯出 JSON。
- 保留問題代碼與原始量測；同名房間以候選位置標示，容量不足與必要條件失敗提供結構化診斷。
- 必要且精確的臥室總數可在窄透天上層既有區塊中分配；先決定用途再重建門窗家具，最後重數房間並通過現有硬規則。
- 低階不帶 bedroom_target 的 API 保留歷史骨架；不更動檢查門檻，不將目前容量解釋為全域不可行。
- 詳見 docs/SPATIAL_REASONING.md，驗證紀錄見 docs/SPATIAL_VERIFICATION.md。

## 2026-09-22 — 需求契約、最終驗證與案例適用條件

- 保存必要／偏好、原句與比較方式；修改時保護未提到的條件。明確房數與車位數另做文字核對，明確取消保留為禁止條件。
- 由實際樓層、房間及汽車配置核對需求。缺漏或異常的檢核、必要需求未滿足／未驗證，不產生成功圖面與下載檔。
- AI 候選先比較可行性，再比較評分；重試保留必要條件，保存調整與引擎切換紀錄。評分保存實際生成的配置，避免重畫後評另一張圖。
- RAG 文件新增尺寸基準、範圍、共壁、開窗側與人工核對紀錄；已知不相容案例先排除，再做原有 384 維語意排序。網頁可編輯條件及更新索引。
- 網頁顯示逐項核對與來源限制，定位改為配置初稿。未改動骨架幾何與硬規則門檻；窄透天的任意房數／地下室能力仍有限制。
- 詳見 `docs/REQUIREMENTS.md`，含本輪測試範圍與既有基準失敗。

## 2026-09-19 — 網路搜尋與參考資料入庫

- 新增主題搜尋按鈕、`research` CLI 與 `/api/rag/research`，自動讀取公開網頁／文字 PDF 並建立本機向量索引；不需額外搜尋 API 金鑰。
- 保留原始 URL、擷取日期與文字證據，內容去重；不把搜尋摘要當正文，逐筆顯示跳過原因與索引狀態。
- 遵守來源擷取規則，限制下載量、頁數及內網連線；通行碼驗證先於搜尋。網路資料僅進設計階段，維持原有幾何檢核。
- 新增網路讀取、來源抽取、重複匯入、部分失敗與 API 權限測試；更新靜態資源版本以避免舊前端快取。

## 2026-09-18 — 本機建築知識 RAG

- 新增 PDF／DWG／DXF／圖片自動匯入、OCR、來源與頁碼／圖層保存、內容去重，以及網頁多檔上傳與文字核對。
- 新增 `import`、`prepare-dwg` 命令，固定版本與 SHA-256 驗證的本機 LibreDWG 轉檔工具；匯入文件限定設計階段，不混入需求解析。

- 新增固定版本 multilingual-e5-small 的 384 維中文語意檢索、SQLite 索引及 prepare/index/search 命令。
- 六份種子知識文件依需求解析、透天選配與房間關係圖用途分流；文件更新或刪除自動更新索引。
- 初次設計與修改流程附加有來源的段落，保留既有 schema、幾何與法規檢核。
- 網頁新增「設計參考資料」，API／歷史方案保存實際提供給模型的引用；依賴或模型缺少時明示退回。
- 新增索引、故障處理、各模型接點及 API 引用保存的離線測試。安裝與限制見 `docs/RAG.md`。

---

## [v0.7.0] — 2026-07-22

Layout Analysis Stack:圖生出來之後,能回答「這張圖好不好」。

### Added(新增)
- **Layout Validation**:`layout_validation.py` — 多邊形封閉/房間重疊/孤立房/
  門是否連通/走道是否中斷,產出 `LayoutReport`。
- **Connectivity Graph**:`connectivity.py` — Adjacency / Room / Space / Door
  四張圖 + `ConnectivityReport`(Dead Room、Unreachable、Disconnected Area、
  Orphan Door)。**分析層連通判定的單一來源**。
- **Corridor Analyzer**:`corridor.py` — 走道寬/瓶頸/盡端/步行距離(Dijkstra)/
  最長路徑。
- **Layout Scoring**:`scoring.py` — 七面向加權評分(connectivity / circulation /
  privacy / lighting / utilization / furniture / collision),權重可調。
- **Constraint Engine**:`constraints.py` — 五條可登錄的設計常規規則,
  條件不成立自動列入 skipped。
- **Layout Optimizer**:`optimizer.py` — 單步微調(門位置 / 分界推移 / 房間旋轉),
  propose → verify → accept,安全閘門把關。
- **Optimization Benchmark**:`optimization_benchmark.py` — Before → Optimize →
  After,輸出 JSON + 前後 DXF/PNG。
- **Report 契約**:`report.py` 的 `JsonReport` — 所有 Report 統一提供
  `to_dict()` / `to_json()`。
- 文件:`docs/LAYOUT_ENGINE.md`、`docs/ARCHITECTURE_V0.7.md`;README 全面改寫
  (原本仍停留在 Phase 0「Hello CAD」)。
- 測試:layout_validation / connectivity / corridor / scoring / constraints /
  optimizer / optimization_benchmark / report 序列化 / **API 契約**。

### Changed(變更)
- `layout_validation` 的連通判定改為委派 `connectivity`(單一來源),移除其重複的
  `wall_cover` / `shared_edge` / `door_points` / `entry_index` 實作。
- 既有四個 Report(`ResolveReport` / `LayoutReport` / `ConnectivityReport` /
  `CorridorReport`)補上 `to_dict()` / `to_json()`;圖一律以 **edge list** 序列化,
  避免 int-key dict 被 json 悄悄轉成字串。
- `DEVELOPMENT_GUIDE.md` 新增「工程紀律」與「環境雷區」。

### Fixed(修正)
- 連通模型漏看「有洞口、無 DoorPlacement」的開放連通口(開放式餐廚、
  客廳↔家庭廳),曾造成 34 案 **253 次孤立房誤報** → 修正後 0。
- Privacy 誤把「家庭廳」當公共空間,懲罰透天臥室層的正常設計
  (實測 bedroom→family 75 次),中位數 0 分 → 修正後 60。
- 「臥室避免鄰近公共空間」觸發率 88%(臥室與客廳共牆在小宅無可避免)
  → 只保留廚房/餐廳,降到 21%。
- Optimizer 候選只認 4 點矩形房,漏掉 L 形走道/客餐廳;最窄房邊防護誤用絕對
  門檻造成全部候選連坐否決 → 兩者修正後才真正能作用。

### Benchmark
- 34/34 生成成功;通過 20 · 警告 14 · **失敗 0**(v0.7 全程逐字不變)。
- **Regression = 0**;DXF 100% / PNG 100%;二次 `resolve()` 100/100 層 no-op。
- LayoutReport 乾淨樓層 100/100;ConnectivityReport 通過 100/100。
- Optimization Benchmark(8 層 × 最多 3 步):改善 5 · **退步 0** · 平均 +0.015。

### Tests
- **617 個測試全數通過**(v0.6 為 501)。

### Known Limitations
- Optimizer 增益極小(+0.01 分),且三種微調**修不掉 constraint error**
  (實測掃過 690/839 個門位置)——生成器輸出已接近局部最佳。
- `room_rotation` 在軸對齊鋪滿平面上必然被否決,為日後自由平面預留。
- L 形走道寬度被最小外接矩形高估(只會漏報,不會誤報)。
- 6 個樓層存在 `bathroom_not_facing_dining`(衛浴直開餐廳)——分析層只回報,
  修正屬 Generator 範疇,尚未處理。
- 詳見 [docs/releases/v0.7.0.md](docs/releases/v0.7.0.md)。

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
