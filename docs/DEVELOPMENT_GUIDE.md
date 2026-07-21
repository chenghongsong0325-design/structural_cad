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
```
