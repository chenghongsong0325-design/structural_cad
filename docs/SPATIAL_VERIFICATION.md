# 空間功能驗證紀錄

日期：2026-09-23。針對房間動線分析、衝突定位與指定臥室數分配驗收。

## 自動測試

| 範圍 | 通過 | 失敗 | 跳過 |
|---|---:|---:|---:|
| 修改前三項既有失敗重測 | 0 | 3 | 0 |
| 修改後完整測試（含 slow） | 1716 | 3 | 2 |
| 功能相關測試 | 97 | 0 | 0 |
| 同步至原專案後的入口檢查 | 3 | 0 | 0 |

功能相關測試包含 requirements、web_app、townhouse_options、design_loop 與新增 spatial_reasoning。涵蓋實際臥室與床具、彈性起居沒有床、同名房間、窗不能通行、缺少入口不假設可達、必要需求未滿足不得產生下載、容量不足回傳結構化診斷。

先前完整基準已記錄三項方案差異失敗。本輪另啟動的重複完整基準於 63% 中止，以保留修改後完整測試並重現這三項問題；該中止執行不列為完整通過。上表的修改前一列只代表三項重測，並非另一套完整測試。

完整測試尚有下列既有失敗，沒有因此降低原斷言，也不能稱為全套全綠：

- tests.test_plan_variants::test_variants_actually_change_the_plan
- tests.test_plan_variants::test_options_are_valid_and_different
- tests.test_plan_variants::test_distance_and_signature

跳過項目：test_narrow_domain_still_passes_both_gates[10000.0-7000.0], test_narrow_domain_still_passes_both_gates[10000.0-8000.0]。

## 隨機尺寸與房數掃描

採固定種子，面寬 4.5–7.5m、進深 14–18m、三至四層、一至四房，混合有／無一樓車庫。此處固定 ref 參考骨架；自動選型另以慢案例及完整測試檢查。

- 80 案房間幾何掃描，不配置家具：77 案通過，3 案容量不足而拒絕。
- 12 案含家具掃描：11 案通過，1 案容量不足而拒絕。
- 成功案例的實際房數均等於要求，且通過既有 plan_check / code_check。拒絕不列為成功；未配置家具的 80 案不代表家具測試通過。
- 含家具案例保留的警告分布：{'no_cross_ventilation': 6, 'room_oversize': 9}。警告未被刪除。
- 另測 7×17m、四層、三房、seed=8376、自動骨架選型與家具：通過既有圖面／尺寸檢查。併行驗收時此案例耗時較長，追蹤顯示原始骨架搜尋是主要耗時部分；不是對使用者生成時間的保證。

重現：

```powershell
.venv\Scripts\python.exe -m pytest -n 4 -q --junitxml=output/full.xml
.venv\Scripts\python.exe scripts/scan_bedroom_program.py --n 80 --seed 0 --no-furnish --core-style ref --json output/geometry_scan.json
.venv\Scripts\python.exe scripts/scan_bedroom_program.py --n 12 --seed 17 --core-style ref --json output/furnished_scan.json
```

## 畫面驗收

- 在瀏覽器輸入三層、4.5×14m、三房，實際圖面二樓兩房、三樓一房；必要條件 6/6 滿足。
- 點選二樓主臥查看尺寸、門洞、床具與本層連通路徑。
- 修改為四房，重新生成後實際四房，原樓層與尺寸要求保留。
- 單層三房在保留一樓公共功能的目前骨架中容量為零，回傳 422 與各層容量；未產生本次成功圖面。
- 已查看二樓／三樓渲染預覽，以及互動頁的彈性起居家具和路徑。
- 此輪 UI 驗收使用固定模型輸出的離線回放，幾何由真實程式產生；不測量 LLM 解析準確率或證明 RAG 效益。
- 原專案已同步並重新啟動 8014；確認正式模式（demo=false）與原有 API 設定可用，歷史方案仍可讀取，前端錯誤紀錄為空。正式服務驗證沒有額外呼叫付費模型。

## 範圍

分析線條是逐層房間拓撲，不是實際行走軌跡或跨層逃生驗證。房數分配在既有骨架區塊內進行，重建仍可能依原有規則退讓或換核心；容量紀錄為初篩。通過既有檢核不代表完整法規、結構或施工審查。詳細演算法及限制見《房間動線與房數配置說明》。
