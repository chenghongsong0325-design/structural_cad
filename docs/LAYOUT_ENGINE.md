# Layout Engine — 格局引擎文件(v0.7)

這份文件說明 **Layout Engine 的分析堆疊**:一句話進去、一張合格的圖出來之後,
我們怎麼**驗證、量化、找碴、微調**它。

---

## 1. 全貌:生成 → 修復 → 分析

```
   自然語言 / HouseBrief
            │
            ▼
   ┌───────────────────┐
   │ Layout Generator  │  規則式生成:面積程式 → 兩帶骨架 → 房間/牆/門窗/家具
   │ layout_generator  │  (唯一「產生」spec 的地方)
   └─────────┬─────────┘
             │ FloorPlanSpec
             ▼
   ┌───────────────────┐
   │ Collision Engine  │  v0.6:家具 × 牆/門迴轉/天井/樓梯/柱
   │ design/collision  │  偵測 → 主動修復(會改 spec.fixtures)
   └─────────┬─────────┘
             │
             ▼
   ┌───────────────────┐
   │ validate_spec()   │  硬性守門(法規/採光/家具/開口壓柱)——不過就 raise
   └─────────┬─────────┘
             │ 合格的 FloorPlanSpec
             ▼
   ═══════════ 以下是 v0.7 分析堆疊(預設全部唯讀,不接進生成流程)═══════════

   ① Validator     layout_validation   多邊形/重疊/孤立房/門/走道中斷
   ② Graph         connectivity        Adjacency / Room / Space / Door 四張圖
   ③ Corridor      corridor            走道寬/瓶頸/盡端/步行距離/最長路徑
   ④ Scoring       scoring             七面向加權評分
   ⑤ Constraint    constraints         設計常規規則(可登錄、可略過)
   ⑥ Optimizer     optimizer           單步微調(★ 唯一會改 spec 的分析層)
   ⑦ Benchmark     optimization_benchmark   Before → Optimize → After
```

**依賴方向是單向的**(無循環):

```
connectivity ──► layout_validation
     │  │
     │  └──────► corridor ──┐
     │                      ├──► scoring ──┐
     └──► constraints ──────┴──────────────┴──► optimizer ──► optimization_benchmark
```

`connectivity` 是幾何與連通判定的**單一來源**,其餘各層一律重用它,不各寫一份。

---

## 2. 為什麼分析層預設唯讀

① ~ ⑤ **完全不改 spec**,⑦ 只在自己的 deepcopy 上作業。只有 ⑥ Optimizer 會改,
而它**刻意不接進生成流程**——由呼叫端主動叫用。

這帶來一個很重要的性質:**這整層對既有輸出零影響**。v0.7 全程 Benchmark
維持 `34/34 · 20 pass / 14 warn / 0 fail` 逐字不變,Regression = 0 是結構性的,
不是靠運氣。

---

## 3. 各層說明與 API

所有 Report 都遵守同一個序列化契約(見 §4)。

### ① Validator — `src/design/layout_validation.py`

```python
from src.design.layout_validation import validate_layout
report = validate_layout(spec)      # -> LayoutReport
report.ok, report.errors, report.warnings, report.summary(), report.to_dict()
```

五項檢查:Room Polygon 是否封閉、Room 是否重疊、是否有孤立 Room、
Door 是否落在房間邊界、Corridor 是否中斷。

> **連通判定的關鍵**:牆會擋人,但 `kind=="door"` 的洞口要扣掉——開放式餐廚、
> 客廳↔家庭廳的連通口是「有洞口、無 DoorPlacement」。只看 `spec.doors` 會把
> 它們誤判成不連通(初版就是這樣,34 案誤報 253 次)。窗不能走,不扣。

### ② Graph — `src/design/connectivity.py`

```python
from src.design.connectivity import analyze_connectivity, build_graphs
graphs = build_graphs(spec)              # -> ConnectivityGraphs
report = analyze_connectivity(spec)      # -> ConnectivityReport
```

| 圖 | 意義 |
|---|---|
| `adjacency` | 房間**實體相鄰**(共用邊界 ≥ `MIN_SHARE`),不管通不通 |
| `room_graph` | 房間**可通行**,邊有型別:`door` / `open`;是 adjacency 的子圖 |
| `spaces` / `space_graph` | 用 `open` 邊把房間併成 Space(客廳+餐廚=一個開放空間),Space 之間以門相連 |
| `doors` | 每扇門服務哪些房間:內門(2 房)/ 對外門(1 房)/ 孤兒門(0 房) |

`ConnectivityReport` 分辨 Dead Room(完全沒有出入口)、Unreachable(有路但接不到
入口)、Unreachable Space、Disconnected Area、Orphan Door。天井(`patio`)豁免。

### ③ Corridor — `src/design/corridor.py`

```python
from src.design.corridor import analyze_corridors
report = analyze_corridors(spec)    # -> CorridorReport
```

量走道寬/長、Bottleneck、Dead End、Walking Distance(Dijkstra)、Longest Path。

距離模型:邊權重 = `房A形心 → 通行點 → 房B形心`。通行點取該邊的門位置或共用邊界
中點——人必須穿過門洞,比形心直線距離貼近真實步行。

門檻依實測設定:`MIN_CORRIDOR_WIDTH = 900`(實測走道寬 min 1200)、
`MIN_OPENING_WIDTH = 750`(實測 636 個門洞 min 750)。

> **已知近似**:寬度用最小外接矩形短邊,直線走道準,**L 形走道會被高估**。
> 這只會讓「過窄」漏報、不會誤報。

### ④ Scoring — `src/design/scoring.py`

```python
from src.design.scoring import score_layout, ScoreWeights
score = score_layout(spec, ScoreWeights(privacy=2.0))   # -> LayoutScore
score.total, score.grade, score.get("privacy").detail
```

```
total = Σ(subscore × weight) / Σ(weight)      每個 subscore ∈ [0, 100]
grade = A≥90 · B≥80 · C≥70 · D≥60 · F<60
```

| 面向 | 預設權重 | 公式 |
|---|---|---|
| connectivity | 2.0 | `100 × (1 − (dead+unreachable)/非豁免房)`,孤兒門每個 −10 |
| circulation | 1.5 | `100 − 20×瓶頸 − 25×盡端 − 繞路罰分` |
| privacy | 1.0 | `100 × (1 − 直開公共空間的私密房 / 私密房)` |
| lighting | 1.5 | 有窗 1 分、有窗但採光深度 >6m 0.5 分、無窗 0 分 |
| utilization | 1.0 | `100 × (1 − 純走道面積 / 總樓地板)` |
| furniture | 1.0 | `100 × (1 − 缺家具房 / 該配家具房)` |
| collision | 2.0 | `100 − 25 × 殘留碰撞數` |

> **公私空間語意**:`family`(家庭廳)/`study` 屬**半私密**,不算公共空間。
> 透天臥室層「家庭廳 + 四周臥室」是正常設計(實測 bedroom→family 75 次),
> 把它當公共會讓 privacy 中位數變成 0,等於懲罰正確格局。

### ⑤ Constraint — `src/design/constraints.py`

```python
from src.design.constraints import check_constraints, RULES
report = check_constraints(spec)          # -> ConstraintReport
report.by_rule("bathroom_not_facing_dining")
```

| Rule ID | 規則 | 嚴重度 |
|---|---|---|
| `bedroom_not_facing_kitchen` | 臥室不可直接面廚房 | error |
| `bathroom_not_facing_dining` | 衛浴不可直接面餐廳 | error |
| `entrance_not_facing_toilet` | 入口不可直視馬桶(視線幾何) | error |
| `bedroom_avoids_public_adjacency` | 臥室避免緊鄰廚房/餐廳 | warn |
| `kitchen_near_dining` | 廚房應接近餐廳(相通**或**共牆) | warn |

條件不成立的規則列入 `skipped`(例:餐廚合一時不套用餐廳規則),不會被誤判成
通過或違反。新增規則 = 加一個函式 + 登錄一筆,引擎不動。

### ⑥ Optimizer — `src/design/optimizer.py`(★ 會改 spec)

```python
from src.design.optimizer import optimize_step
step = optimize_step(spec)      # -> OptimizeStep;至多套用「一個」改動
```

`propose → verify → accept / revert`:候選一律套在 **deepcopy** 上試算,
通過安全閘門且確實更好,才把那**一個**改動寫回。

安全閘門:`validate_spec` 全過 · 格局健檢過 · 連通過 · **碰撞數不增加** ·
房間不得被壓得比原本最窄的房間更窄。
改善判準:constraint error 變少;或 error 持平且分數提高。

三種被允許的微調:`door_position`(門沿牆移動)、`room_position`(推移整條內部
分界線,兩側同時調整)、`room_rotation`(繞形心轉 90°)。

> **已知限制(已寫成測試釘住)**
> * `room_rotation` 在「軸對齊且完全鋪滿」的平面上必然破壞鋪滿,**一定被否決**;
>   保留是為了日後自由平面。
> * 三種微調**修不掉 `bathroom_not_facing_dining`**:實測把門沿牆掃過所有位置
>   (S06 690 個、S08 839 個)都無法改變它開向哪一間——那需要改拓樸。
> * 實測 20 層中 9 層找得到安全改善,增益僅約 **+0.01 分**——生成器輸出已接近
>   這三種操作的局部最佳。

### ⑦ Optimization Benchmark — `src/design/optimization_benchmark.py`

```bash
python -m src.design.optimization_benchmark --limit 8 --steps 3
```

```python
from src.design.optimization_benchmark import run, measure
report = run(limit=8, steps=3)      # -> OptimizationReport
```

Before → Optimize → After,量 Layout Score、Furniture Success Rate、Collision、
Walk Distance、Area Utilization、Constraint Errors,輸出 `output/optimization/`
(JSON + 每層 before/after 各一份 DXF、PNG)。

`regressed` 欄位是最該盯的:分數/碰撞/error 任一變差即為真。

---

## 4. Report 序列化契約

**每個 Report 都必須提供 `to_dict()` 與 `to_json()`**(繼承 `src/design/report.py`
的 `JsonReport`,實作 `to_dict()` 即獲得 `to_json()`)。

```python
report.to_dict()                              # 純原生型別,可直接 json.dumps
report.to_json(indent=2, ensure_ascii=False)  # 建在 to_dict 之上,單一來源
```

三個本 repo 特有的陷阱,實作時務必避開:

1. **shapely 幾何物件**不能放進 dict——轉座標數值或不放。
2. **`set` 要轉 `sorted(list)`**,否則 `json.dumps` 直接 `TypeError`。
3. **int 當 key 的 dict** 會被悄悄轉成字串 key——圖一律用 **edge list** 表示
   (`adjacency` / `room_edges` / `space_edges`)。

`ensure_ascii=False` 是必要的:報告內容是中文房名。

---

## 5. 快速上手

```python
from src.design.layout_generator import HouseBrief, generate_floor_plan
from src.design.layout_validation import validate_layout
from src.design.connectivity import analyze_connectivity
from src.design.corridor import analyze_corridors
from src.design.scoring import score_layout
from src.design.constraints import check_constraints

spec = generate_floor_plan(HouseBrief(site_width=16000, site_depth=14000,
                                      bedrooms=3))

print(validate_layout(spec).summary())
print(analyze_connectivity(spec).summary())
print(analyze_corridors(spec).summary())
print(score_layout(spec).summary())
print(check_constraints(spec).summary())
```

---

## 6. 這一版的工程紀律

v0.6 / v0.7 一路建立的三條規矩,寫在這裡是因為它們實際救回了好幾個模組:

1. **門檻先量測、再設定** —— 柱容差、走道寬、洞口寬、constraint 觸發率,全部
   先掃 34 案 100 層拿到分布,再決定數值。憑感覺設的門檻幾乎都是錯的
   (例:柱若用 150mm 會誤判 283 件合法家具)。
2. **零誤報優先於多抓** —— 一條在 88% 的合格圖上都會叫的規則等於沒有規則。
   遇到高觸發率先診斷語意,不要調參數硬壓。
3. **每條規則都要有「注入缺陷必觸發」的測試** —— 只測零誤報的話,一個永遠回空
   的壞偵測器也會通過。

詳見 [DEVELOPMENT_GUIDE.md](DEVELOPMENT_GUIDE.md)。
