---
name: preview
description: 生成指定尺寸的平面圖並輸出 PNG,讓人和 AI 都直接看到畫出來長什麼樣 —— 跑 generate_building_auto → build_sheets → 每張圖存 PNG + plan_check 報告,然後**用 Read 打開 PNG 實際看過再回報**。使用者說「出張圖看看」「畫一張 X×Y 的」「看一下改完長怎樣」「產預覽」,或你剛改完格局/製圖相關程式碼要驗收時用。
---

# /preview

完整的做法、坑、判讀方式寫在 **[docs/workflows/preview.md](../../../docs/workflows/preview.md)**
(那份有進版本控制,Codex 之類的其他 agent 也讀得到)。

**先把那份讀完再動手**,不要照記憶做 —— 尺寸區間與常數的唯一出處是程式碼,
流程的唯一出處是那份文件。這裡不再放第二份,免得兩邊漂移。
