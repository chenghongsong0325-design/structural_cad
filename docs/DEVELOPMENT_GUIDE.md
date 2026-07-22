# Development Guide — 開發原則

本專案的鐵則:**禁止直接開始寫程式**。每次新增功能都必須走完九步流程。
這不是官僚,是這個專案踩過教訓後定調的做法——格局引擎的模組彼此高度耦合
(面積 → 骨架 → 幾何 → 柱網 → 檢核),沒先分析就動手,常常修 A 壞 B。

---

## 新增功能的九步流程

> 對應到與使用者的協作:**先講清楚要做什麼、比較怎麼做、等拍板,才動手;
> 做完用 Benchmark 證明沒壞、更新文件、才 commit。**

### 1. 先分析
- 讀相關程式,搞清楚**現況怎麼運作**、問題的**根因**在哪。
- 用實際數據佐證(跑一段 script 量測),不要憑印象。
- 產出:「目前 X 在哪個檔案/函式決定、為什麼會有這個問題」。

### 2. 提出方案(至少 2~3 種)
- 每種方案講清楚**做法**與**改動範圍**。
- 不要只提一種——沒有對照就看不出取捨。

### 3. 比較方案
- 逐項比較:優點 / 缺點 / 對現有架構影響 / 是否影響 DXF / 是否影響
  Benchmark / 是否影響柱網。
- 給出**建議**,但把選擇權留給使用者。

### 4. 等確認
- **停下來等使用者拍板**,不要自作主張開始改。
- 使用者可能調整方案、縮小範圍、或指定不同做法。

### 5. 開始修改
- 按拍板的方案動手。**小步前進**,一次改一塊,邊改邊測。
- 只改講好的範圍;發現順手可改的別的東西,先記下來、不夾帶。

### 6. 跑 Benchmark
- `python -m src.design.benchmark`,產出 `output/benchmark/report.html`。
- 比較**修改前 / 後**:客廳長寬比、房間比例警告、Overflow 房間、DXF 狀態。

### 7. Regression = 0
- 全套測試必須全綠:`python -m pytest tests/ -q`。
- Benchmark **不得有新的生成失敗、狀態不得變差**(regression)。
- DXF 必須維持 100%。
- 有 regression → 回第 5 步修,不是放著。

### 8. 更新文件
- 更新 `CHANGELOG.md`(這次改了什麼)。
- 需要時更新 `docs/`(架構/路線圖/release note)。
- 更新專案記憶(重大決策、踩過的坑)。

### 9. Commit
- 訊息講清楚**為什麼**改(不只是改了什麼)。
- 一個 commit 一件事,可回溯。
- **使用者說 commit 才 commit**——不主動 commit。

---

## 硬規則

- ❌ **禁止直接開始寫程式**(跳過第 1~4 步)。
- ❌ 不改講好範圍以外的演算法(想改要另外提)。
- ❌ 不留 `print()` 除錯碼、`TODO`、註解掉的死碼在 library 邏輯裡
  (CLI `main()` 的輸出 print 例外)。
- ❌ 不為了湊面積/美觀犧牲柱網(柱網/結構/牆線優先)。
- ✅ 房間尺寸只改 `room_program.py`,不寫死在 `layout_generator.py`。
- ✅ 每份 `FloorPlanSpec` 出廠前必過 `validate_spec`。
- ✅ 面積是「目標」不是「命令」,允許 ±10% 誤差。

---

## 每版收斂條件(進下一版前)

1. 該版 Benchmark 全綠或只剩**已知且記錄**的限制。
2. Regression = 0、DXF = 100%、測試全數通過。
3. `CHANGELOG.md` 與 release note 更新完成。
4. 「已知限制」與「下一版目標」寫清楚(見 `docs/ROADMAP.md`)。

---

## 常用指令

```bash
# 全套測試
python -m pytest tests/ -q

# 只跑格局相關測試
python -m pytest tests/test_room_program.py tests/test_building_generator.py -q

# Benchmark(全 34 案,產 report.html）
python -m src.design.benchmark

# Benchmark 快速版(前 N 案、不算 PNG)
python -m src.design.benchmark --limit 6 --no-render

# Optimization Benchmark(Before → Optimize → After)
python -m src.design.optimization_benchmark --limit 8 --steps 3
```

---

## 工程紀律(v0.6 / v0.7 定調)

這三條不是理論,是實際救回好幾個模組的做法。

### 1. 門檻先量測,再設定

**憑感覺設的門檻幾乎都是錯的。** 每個容差/門檻都要先掃 34 案 100 層拿到分布,
再決定數值,並把依據寫進常數旁的註解。

實例:
- 柱容差若照直覺設 150mm,會**誤判 283 件合法貼牆家具**(實測合法穿入最深
  175mm)→ 正解 300mm。
- 牆容差:貼牆突出 ≤1mm² vs 真穿牆 ≥170000mm²,差五個數量級 → 取 5000mm²。
- 走道寬門檻:實測 min 1200mm → 取 900mm,現況零誤報。

### 2. 零誤報優先於多抓

**一條在 88% 的合格圖上都會叫的規則,等於沒有規則。** 遇到高觸發率,
先診斷語意是不是搞錯,不要調參數硬壓。

實例:
- Privacy 第一版中位數 0 分,因為把「家庭廳」當公共空間——但透天臥室層
  「家庭廳 + 四周臥室」是**正常設計**(實測 75 次)。修正是**語意的**
  (family 改列半私密),不是調權重。
- 「臥室避免鄰近公共空間」第一版觸發 88%,因為臥室與客廳共牆在小宅無可避免
  (實測 42 次)→ 只保留廚房/餐廳,降到 21%。

### 3. 每條規則都要有「注入缺陷必觸發」的測試

只測零誤報的話,**一個永遠回空的壞偵測器也會通過**。實際上 constraint 引擎有
兩條規則在真實資料上觸發率是 0%,若沒有注入測試就無從得知它們是否還活著。

### 4. 唯讀層必須有「不得改動」的測試

分析層(validator / graph / corridor / scoring / constraint)一律唯讀,
每個都要有 `test_*_does_not_mutate_spec`,比對 rooms/walls/openings/doors/fixtures
前後逐字相同。可寫的層(collision、optimizer)則要測**可寫範圍的界線**
(例:optimizer 不得碰 fixtures)。

### 5. Report 一律提供 `to_dict()` / `to_json()`

繼承 `src/design/report.py` 的 `JsonReport`,實作 `to_dict()` 即獲得 `to_json()`。
測試要**真的 `json.dumps` + `json.loads` round-trip**,而不是只斷言方法存在——
本 repo 的三個陷阱(shapely 物件、`set`、int-key dict)只有實際序列化才抓得到。

---

## 環境雷區

- **不要用 PowerShell 的 `Get-Content | Set-Content` 對含中文的檔案做原地取代**
  ——會破壞 UTF-8 編碼,整份檔案變亂碼。請用編輯工具直接改。
- 跑 scratchpad 腳本時要設 `$env:PYTHONPATH` 指到專案根目錄,否則 `import src.*`
  會失敗。
