---
name: newrule
description: 在 plan_check 加一條「什麼叫合格的圖」的硬規則,並跑完四條產線的完整驗收 —— 判 error/warning、寫進 check_floor、補測試、四條產線各掃一輪、出圖看過。使用者指出圖上某個東西畫錯了、說「這種圖不該出得來」「以後都不要再出現這個問題」,或你自己在預覽圖上看到規則還沒涵蓋的缺口時用。
---

# /newrule

完整的做法、坑、判讀方式寫在 **[docs/workflows/newrule.md](../../../docs/workflows/newrule.md)**
(那份有進版本控制,Codex 之類的其他 agent 也讀得到)。

**先把那份讀完再動手**,不要照記憶做 —— 尺寸區間與常數的唯一出處是程式碼,
流程的唯一出處是那份文件。這裡不再放第二份,免得兩邊漂移。
