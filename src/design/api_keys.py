"""Gemini 金鑰:多把輪替 —— 一把用完自動換下一把。

**為什麼要這個**:Gemini 免費額度是「每把金鑰、每個模型、每天 20 次請求」分開算
的。AI 設計師模式一次請求吃 2~3 次呼叫,一把金鑰大概 7 次就見底。所以多一把金鑰
就多一份額度,對口試 demo 是實際的差別(1 把 ≈ 7 次 / 5 把 ≈ 35 次)。

⚠️ **同一個 Google 專案底下開兩把金鑰,額度是共用的、不會變兩倍** —— 要多一份額度
   就得在**不同專案**(或不同 Google 帳號)底下開。

⚠️ **金鑰絕對不可以進 git。** 這裡讀兩個來源:

    ① 環境變數 `GEMINI_API_KEY` / `GOOGLE_API_KEY`,以及 `GEMINI_API_KEYS`
       (一個變數塞多把,逗號分隔——雲端主機通常只能設固定幾個變數名)
    ② 專案根目錄的 `api_keys.json`(已列入 .gitignore,格式 `{"keys": [...]}`)

⚠️ **雲端主機(Render 免費方案)的硬碟是暫時的**:寫進 `api_keys.json` 的金鑰
   一休眠就沒了,雲端請一律用環境變數。本機才靠 json 檔方便。

用法(取代 `genai.Client()`)::

    from src.design.api_keys import make_client
    client = make_client()                    # 沒有金鑰會 raise
    client.models.generate_content(...)       # 撞到額度上限會自動換下一把重試
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Callable, Optional

# 專案根目錄的金鑰檔(gitignored)。
KEY_FILE = Path(__file__).resolve().parents[2] / "api_keys.json"
# 單把金鑰的環境變數名(依序讀,重複的只留一份)。
ENV_SINGLE = ("GEMINI_API_KEY", "GOOGLE_API_KEY")
# 一個變數塞多把用的名字(逗號或分號分隔)。
ENV_MULTI = ("GEMINI_API_KEYS", "GOOGLE_API_KEYS")
# 這些字樣出現在例外訊息裡,就當成「這把金鑰的額度用完了」→ 換下一把。
# ⚠️ 只認額度類錯誤:金鑰打錯、網路斷掉那種換幾把都一樣,換了只是白花時間。
QUOTA_HINTS = ("resource_exhausted", "quota", "429", "rate limit",
               "rate_limit_exceeded")


def _split(raw: str) -> list[str]:
    out = []
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if part:
            out.append(part)
    return out


def load_keys() -> list[str]:
    """所有可用金鑰(環境變數優先,再接 api_keys.json);去重、保持順序。"""
    keys: list[str] = []

    def add(k: str) -> None:
        k = (k or "").strip()
        if k and k not in keys:
            keys.append(k)

    for name in ENV_SINGLE:
        add(os.environ.get(name, ""))
    for name in ENV_MULTI:
        for k in _split(os.environ.get(name, "")):
            add(k)
    try:
        with open(KEY_FILE, encoding="utf-8") as f:
            for k in json.load(f).get("keys", []):
                add(k)
    except (OSError, ValueError):
        pass
    return keys


def have_key() -> bool:
    """有沒有任何一把可用的金鑰(網頁層用它決定要不要開 AI 模式)。"""
    return bool(load_keys())


def mask(k: str) -> str:
    """遮過的金鑰(可以印在畫面/log 上)。"""
    return f"{k[:6]}…{k[-4:]}" if len(k) > 12 else "…"


def _is_quota_error(exc: Exception) -> bool:
    text = f"{type(exc).__name__} {exc}".lower()
    return any(h in text for h in QUOTA_HINTS)


class _Models:
    """`client.models.xxx(...)` 的轉接層(只轉接,不改參數)。"""

    def __init__(self, outer: "RotatingClient") -> None:
        self._outer = outer

    def __getattr__(self, name: str) -> Callable:
        def call(*args, **kw):
            return self._outer._call(name, *args, **kw)
        return call


class RotatingClient:
    """介面跟 `genai.Client` 一樣,但撞到額度上限時自動換下一把金鑰重試。

    只在**額度**類錯誤時換(見 QUOTA_HINTS);金鑰打錯或網路問題直接往上丟,
    不要拿其他金鑰再試一次 —— 那只是把同一個錯誤重複 N 遍、log 也更難看。
    """

    def __init__(self, keys: list[str],
                 factory: Optional[Callable[[str], object]] = None) -> None:
        if not keys:
            raise RuntimeError(
                "找不到 Gemini 金鑰:請設環境變數 GEMINI_API_KEY,"
                "或在專案根目錄放 api_keys.json({\"keys\": [...]})")
        self._keys = list(keys)
        self._factory = factory or _default_factory
        self._i = 0                       # 目前用第幾把(換過就停在那把)
        self._clients: dict[int, object] = {}

    # ── 對外介面 ────────────────────────────────────────────────────────
    @property
    def models(self) -> _Models:
        return _Models(self)

    @property
    def key_count(self) -> int:
        return len(self._keys)

    @property
    def current_key(self) -> str:
        return self._keys[self._i]

    # ── 內部 ────────────────────────────────────────────────────────────
    def _client(self, i: int) -> object:
        if i not in self._clients:
            self._clients[i] = self._factory(self._keys[i])
        return self._clients[i]

    def _call(self, method: str, *args, **kw):
        last: Optional[Exception] = None
        for step in range(len(self._keys)):
            i = (self._i + step) % len(self._keys)
            try:
                result = getattr(self._client(i).models, method)(*args, **kw)
                self._i = i          # 這把還能用 → 下次從它開始
                return result
            except Exception as exc:                    # noqa: BLE001
                if not _is_quota_error(exc):
                    raise
                last = exc
        raise RuntimeError(
            f"{len(self._keys)} 把金鑰的今日額度都用完了(最後一個錯誤:{last})"
        ) from last


def _default_factory(key: str) -> object:
    from google import genai
    return genai.Client(api_key=key)


def make_client(factory: Optional[Callable[[str], object]] = None
                ) -> RotatingClient:
    """建一個會自動換金鑰的客戶端。沒有任何金鑰時 raise(訊息講清楚怎麼設)。

    factory:測試用的注入點(金鑰 → 假客戶端)。
    """
    return RotatingClient(load_keys(), factory=factory)
