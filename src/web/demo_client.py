"""離線示範用的「錄音式」LLM 客戶端(DEMO_MODE)。

為什麼要有:AI 設計師模式一次請求要呼叫 Gemini 2~3 次(解析需求 → 設計關係圖 →
依 critique 重新設計),免費額度每天約 20 次 —— 也就是**一天只能示範七次左右**,
發表/口試當天很容易剛好掛掉。

做法:把一組合用的 LLM 回覆「錄」在 samples/ai_demo.json,DEMO_MODE=1 時用這個
客戶端依序回放。**產線本身照跑**(關係圖 → 搜尋落實 → 收斂 → 出圖 → 兩道關卡),
只有「LLM 講了什麼」是錄好的,所以示範出來的圖是真的算出來的,不是截圖。

⚠️ 誠實原則:走這條時 API 回應會帶 `demo: true`,前端要顯示「示範模式(離線回放)」,
   不可以讓人以為當場打了 Gemini。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

_SAMPLE = (Path(__file__).resolve().parents[2] / "samples" / "ai_demo.json")


def demo_enabled() -> bool:
    """DEMO_MODE=1 且錄音檔在 → 走離線回放。"""
    return bool(os.environ.get("DEMO_MODE")) and _SAMPLE.is_file()


class _Response:
    def __init__(self, text: str):
        self.text = text


class _Models:
    def __init__(self, payloads: list):
        self._payloads = payloads
        self._i = 0

    def generate_content(self, **_kwargs):
        """依序回放;放完就一直回最後一則(收斂迴圈可能多問幾次)。"""
        payload = self._payloads[min(self._i, len(self._payloads) - 1)]
        self._i += 1
        return _Response(json.dumps(payload, ensure_ascii=False))


class DemoClient:
    """介面與 google-genai 的 client 相同(只用到 .models.generate_content)。"""

    def __init__(self, payloads: list | None = None):
        self.models = _Models(payloads if payloads is not None else load_payloads())


def load_payloads() -> list:
    """讀錄音檔 → LLM 回覆清單(順序 = 產線呼叫順序)。"""
    data = json.loads(_SAMPLE.read_text(encoding="utf-8"))
    return data["responses"]
