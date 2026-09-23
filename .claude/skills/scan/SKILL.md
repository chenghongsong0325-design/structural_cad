---
name: scan
description: 隨機掃描平面圖產線抓系統性錯誤 —— 亂數抽 N 組尺寸/房數/樓層需求跑 generate_building_auto,每張圖送 plan_check 把關,統計硬錯誤排行並定位到具體規則與產線。改完任何骨架(narrow_house / shallow_house / graph_layout / layout_generator / 兩帶式 building_generator)或動 plan_check 規則之後用它做回歸驗收。使用者說「掃一輪」「掃描 N 案」「看有沒有系統性錯誤」「驗收這次改動」時用。
---

# /scan

完整的做法、坑、判讀方式寫在 **[docs/workflows/scan.md](../../../docs/workflows/scan.md)**
(那份有進版本控制,Codex 之類的其他 agent 也讀得到)。

**先把那份讀完再動手**,不要照記憶做 —— 尺寸區間與常數的唯一出處是程式碼,
流程的唯一出處是那份文件。這裡不再放第二份,免得兩邊漂移。
