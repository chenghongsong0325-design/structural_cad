# 報告前驗證與現場操作（2026-11-18）

網站：https://structural-cad-6623.onrender.com/

## 本次處理的問題

- Gemini 備援模型由不存在的 `gemini-3.8-flash-lite` 改為官方列出的 `gemini-3.5-flash-lite`。主模型仍可由 `GEMINI_MODEL` 覆寫，備援可由 `GEMINI_FALLBACK_MODEL` 覆寫。若 Render 曾手動設定舊名稱，環境變數也需要更新。
- 需求解析遇到模型不存在時直接換備援；忙碌與連線逾時各模型最多兩次呼叫，每次設定 20 秒逾時，停用 SDK 疊加重試。金鑰或權限錯誤立即回報。多金鑰輪替仍受金鑰數影響，這不是整個設計流程的總時間保證。
- 一般 PDF 固定 A3 橫式，線寬依 mm／pt 換算，避免家具、文字和尺寸被粗黑線遮住；既有製圖設定檔的紙張與比例保留。
- PDF 改用新版快取並先完整寫入暫存檔；舊檔保留，產生失敗的半份 PDF 不會被當成完成品下載。
- 將無法滿足指定兩房的淺基地範例改為經實際出圖驗證的 8.5×9.5 米案例。原本必要房數檢查保留。
- 基地尺寸不再四捨五入成整數；摘要明確標為「建築軸線」，不把軸線跨度當成外牆總尺寸。
- 限額訊息改為核對實際專案／模型配額，移除固定每日次數及不存在的操作選項。

模型與限額依據：[官方模型清單](https://ai.google.dev/gemini-api/docs/models)、[官方速率限制](https://ai.google.dev/gemini-api/docs/rate-limits)。免費額度以當時的專案、模型及層級為準，同專案換金鑰不保證增加用量。

## 驗證範圍

修正前完整基準：**1817 passed、2 skipped、0 failed**，約 59 分鐘。略過項目為既有不支援深度的窄透天案例。此次未修改格局骨架、家具幾何或檢查規則；修改後針對需求解析、金鑰、網頁流程、PDF、製圖設定檔與閱讀預覽的回歸為 **92 passed、0 failed**（約 137 秒，含新增 15 項回歸案例），套件相依檢查也通過。完整基準與修正後回歸是兩次不同的驗證，不代表修正後重跑了全部 1819 項。

驗證命令（PowerShell，設定 `RAG_ENABLED=0`，測試注入固定解析回應，不消耗雲端額度）：

```powershell
.venv\Scripts\python.exe -m pytest -n 6 -o addopts='' -q --tb=short --junitxml=output/report-readiness-20261117.xml
.venv\Scripts\python.exe -m pytest -n 4 -o addopts='' -q --tb=short tests/test_report_readiness.py tests/test_nl_parser.py tests/test_api_keys.py tests/test_web_app.py tests/test_drawing_profile.py tests/test_readable_preview.py --junitxml=output/report-readiness-fixed.xml
.venv\Scripts\python.exe -m pip check
```

本機固定需求演練包含四房透天、較寬透天、一樓車庫、淺基地兩房；逐案驗證需求、圖面、程式規則、結果重讀及評分一致性，再開啟 DXF 檢查、PDF 紙張和頁數。固定需求測試不等於真實 Gemini 解析測試，線上服務需另行確認。

檢查通過表示本系統實作的關卡通過，並非建築師簽證、結構計算或建照核准。這批案例仍有家具／衛浴操作空間建議未滿足，報告應保留提醒，不宣稱所有人因項目全綠。

## 三分鐘展示順序

1. 提早開啟網站；Render 免費服務休眠後第一次載入可能較久。
2. 選「4.5×14 三層四房」範例並生成，等畫面完成後依序切換 1F、2F、3F、剖面及立面。
3. 使用整張圖還原與全螢幕，展示房間與動線分析；說明必要需求檢查與未解的人因提醒。
4. 下載 CAD ZIP、PDF 和設計報告，說明同一份最終幾何用於預覽、檢查與匯出。
5. 若外部 AI 當下無法服務，開啟「報告展示備份_2026-11-18／開啟離線展示.html」。離線版展示事先完成的四個真實出圖結果，不能描述成現場呼叫 AI。

雲端歷史與輸出使用暫存磁碟，請事先下載；重要圖面不要只留在 Render。
