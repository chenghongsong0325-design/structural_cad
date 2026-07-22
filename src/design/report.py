"""報告序列化共用基底(v0.7)。

各分析層的 Report 都要能被程式再處理(寫進 benchmark.json、餵給 report.html、
給網頁 API 回傳),不能只有給人看的 `summary()`。故統一契約:

    report.to_dict()   純原生型別(dict/list/str/int/float/bool/None),可直接
                       json.dumps;巢狀 dataclass 一律展開。
    report.to_json()   字串,**建在 to_dict() 之上**(單一來源,不各寫一份
                       序列化邏輯)。

⚠️ 這個 repo 的三個序列化陷阱,實作 to_dict() 時務必避開:
  * shapely 幾何物件(Polygon/Point)不能直接放進 dict —— 轉座標或不放。
  * `set` 要轉 `sorted(list)`,否則 json.dumps 直接丟 TypeError。
  * 以 int 為 key 的 dict 會被 json 悄悄轉成字串 key —— 改用 edge list 或
    明確轉 str,不要讓它隱式發生。

`ensure_ascii=False` 是預設值:報告內容是中文房名,不該被轉成 \\uXXXX。
"""
from __future__ import annotations

import json


class JsonReport:
    """混入類別:子類別實作 to_dict(),即免費獲得 to_json()。"""

    def to_dict(self) -> dict:
        raise NotImplementedError(
            f"{type(self).__name__} 必須實作 to_dict()")

    def to_json(self, indent: int | None = 2,
                ensure_ascii: bool = False) -> str:
        """序列化成 JSON 字串。中文房名保持原樣(ensure_ascii=False)。"""
        return json.dumps(self.to_dict(), indent=indent,
                          ensure_ascii=ensure_ascii)
