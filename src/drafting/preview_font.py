"""預覽/向量輸出時的中文字型替身。

## 為什麼需要這支

`config/standards/default.yaml` 的 STRUCT 文字樣式用**標楷體 kaiu.ttf** —— 這是給
AutoCAD 顯示與競賽出圖用的,不能改。

但把 kaiu.ttf 的中文字**轉成向量路徑**時(SVG / PDF / matplotlib PNG 都走這條),
複雜筆畫的字(樓梯間、衛浴、客餐廳…)輪廓會自我相交、破碎成「筆畫裂開、局部塗黑」
的樣子。這是 kaiu.ttf 字型檔本身的問題,**只在轉向量路徑時出現**;AutoCAD 用它自己
的渲染器,不會犯這個錯 —— 所以**下載下來的 DXF 是好的**,壞的只有我們畫出來的預覽。

實測(同一段字、同一支渲染器):

    kaiu.ttf      → 破碎
    msjh.ttc      → 乾淨      ← 採用
    mingliu.ttc / simsun.ttc / msyh.ttc → 也乾淨

⚠️ 這支只覆寫**送去轉圖的那份文件**,而且用完就還原(try/finally)。DXF 下載檔
仍然是標楷體,不管呼叫順序(先存檔或先轉圖)都不會被汙染。

⚠️ 任何「把 doc 畫成圖片」的新入口都要包上 `preview_font(doc)`,否則中文又會破。
   已知入口:`src/web/render.py`(SVG/PDF)、`scripts/preview_plan.py`(PNG)。
"""
from __future__ import annotations

from contextlib import contextmanager

#: 轉向量路徑時拿來頂替標楷體的字型(微軟正黑體,Windows 內建)。
PREVIEW_FONT = "msjh.ttc"

#: 要被頂替的文字樣式名(default.yaml 裡唯一帶中文字型的那個)。
STYLE_NAME = "STRUCT"


@contextmanager
def preview_font(doc, font: str = PREVIEW_FONT):
    """暫時把 STRUCT 樣式的字型換成 `font`,離開時還原。

    文件裡沒有 STRUCT 樣式(例如手寫的小測試檔)就什麼都不做,不要炸掉。
    """
    try:
        style = doc.styles.get(STYLE_NAME)
    except Exception:
        style = None

    original = style.dxf.font if style is not None else None
    if style is not None:
        style.dxf.font = font
    try:
        yield
    finally:
        if style is not None:
            style.dxf.font = original
