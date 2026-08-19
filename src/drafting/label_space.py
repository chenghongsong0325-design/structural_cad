"""圖面文字不要黏在一起 —— 已占位置的登記簿。

## 為什麼需要這支

同一張平面圖上的文字來自五、六個互不相識的模組:室名與面積(room)、樓梯的
「UP 18」(stair)、軸網編號(gridlines)、門窗編號與牆厚引線(annotations)、
標題欄(titleblock)。每一支都只知道**自己**要寫在哪,結果就是使用者
2026-08-19 截圖抓到的:

    「UP 18樓梯間」        梯級標示黏著室名
    「12cm 磚牆」壓在      浴室的面積數字上
    「D2」壓在             室名上

⚠️ 光是「不重疊」不夠。實測那張圖的「UP 18」與「樓梯間」量出來的重疊是 0 ——
   它們只是**貼著**,中間沒有空隙,讀起來還是連成一串。所以判準是**留白**:
   兩段文字之間至少要有 LABEL_GAP。

## 怎麼用

先把「不能動的字」登記進去(室名、面積、軸網編號、樓梯標示——它們的位置有
意義,動了就對不上東西),再讓「可以動的字」(門窗編號、牆厚引線)從幾個候選
位置裡挑第一個沒撞到的:

    space = LabelSpace()
    space.scan(msp)                     # 現有的字全部登記
    pick = space.take(candidates)       # 回第一個乾淨的候選(都髒回 None)

⚠️ 挑不到就**照原本的位置畫**,不要不畫 —— 少一個門窗編號比疊到字嚴重得多。
"""
from __future__ import annotations

from typing import Iterable, Optional

from ezdxf.enums import TextEntityAlignment

#: 兩段文字之間至少要留的空白(模型單位 mm;1:100 出圖 ≈ 1.5mm)。
LABEL_GAP = 150.0

#: 中文字在標楷體裡約 1.34 個字高寬,ASCII 約 0.67 —— 量字寬用。
#: (跟 ezdxf 對 kaiu.ttf 的實測一致;這裡自己算是為了不在畫圖時載字型檔。)
_W_CJK = 1.34
_W_ASCII = 0.67

_CENTER_H = (TextEntityAlignment.MIDDLE_CENTER, TextEntityAlignment.CENTER,
             TextEntityAlignment.BOTTOM_CENTER, TextEntityAlignment.TOP_CENTER)
_RIGHT_H = (TextEntityAlignment.MIDDLE_RIGHT, TextEntityAlignment.RIGHT,
            TextEntityAlignment.BOTTOM_RIGHT, TextEntityAlignment.TOP_RIGHT)
_MIDDLE_V = (TextEntityAlignment.MIDDLE_CENTER, TextEntityAlignment.MIDDLE_LEFT,
             TextEntityAlignment.MIDDLE_RIGHT)

Box = tuple[float, float, float, float]


def text_width(text: str, height: float) -> float:
    """一段文字畫出來大概多寬。全形算 1.34 個字高、半形 0.67。"""
    w = 0.0
    for ch in text:
        w += _W_CJK if ord(ch) > 0x2E80 else _W_ASCII
    return w * height


def text_box(text: str, height: float, x: float, y: float,
             align: Optional[TextEntityAlignment] = None) -> Box:
    """文字的外框。align 跟 `set_placement(align=…)` 用的是同一個東西。"""
    w = text_width(text, height)
    if align in _CENTER_H:
        x -= w / 2.0
    elif align in _RIGHT_H:
        x -= w
    if align in _MIDDLE_V:
        y -= height / 2.0
    return (x, y, x + w, y + height)


class LabelSpace:
    """已經被文字占掉的位置。"""

    def __init__(self, gap: float = LABEL_GAP) -> None:
        self.gap = float(gap)
        self._boxes: list[Box] = []

    # ── 登記 ────────────────────────────────────────────────────────────
    def occupy(self, box: Box) -> None:
        self._boxes.append(box)

    def scan(self, msp, skip=()) -> int:
        """把 msp 上現有的 TEXT 全部登記起來。回登記了幾段。

        skip:待會要搬家的那些字(例如樓梯的 UP/DN)。先登記它們的話,它們會
        跟自己撞,永遠挑不到位置。

        ⚠️ 空白字串不算(有些模組會寫空字串佔位),不然會擋掉一大片。"""
        skip = set(id(e) for e in skip)
        n = 0
        for e in msp.query("TEXT"):
            t = str(e.dxf.text)
            if not t.strip() or id(e) in skip:
                continue
            try:
                align = e.get_align_enum()
            except Exception:                        # noqa: BLE001
                align = None
            p = (e.dxf.align_point
                 if (e.dxf.halign or e.dxf.valign) else e.dxf.insert)
            self.occupy(text_box(t, float(e.dxf.height),
                                 float(p.x), float(p.y), align))
            n += 1
        return n

    # ── 查詢 ────────────────────────────────────────────────────────────
    def is_clear(self, box: Box) -> bool:
        """這個框(含四周 gap 的留白)有沒有撞到已登記的字。"""
        g = self.gap
        x0, y0, x1, y1 = box[0] - g, box[1] - g, box[2] + g, box[3] + g
        for b in self._boxes:
            if x0 < b[2] and b[0] < x1 and y0 < b[3] and b[1] < y1:
                return False
        return True

    def take(self, candidates: Iterable) -> Optional[tuple]:
        """從候選裡挑第一個乾淨的,順手登記起來。都髒回 None。

        candidates 每項是 (box, payload);回傳挑中的那一項原樣。"""
        for item in candidates:
            if self.is_clear(item[0]):
                self.occupy(item[0])
                return item
        return None


# ── 樓梯的 UP/DN 標示 ───────────────────────────────────────────────────────
#: 梯級標示每次挪動的距離(沿梯跑方向)。
_STAIR_STEP = 400.0
#: 最多往前挪幾階距離 —— 再遠就跑出梯段了,寧可留在原地。
_STAIR_TRIES = 6


#: 「可以搬家」的字。共同點:**位置只要大致對就讀得懂**,不像室名/面積/軸網
#: 編號那樣位置本身帶資訊。
#:   UP 18 / DN  梯級標示 —— 只要落在梯段上就行(`stair.flight_label` 的產出)
#:   拉門        橫拉門註記 —— 只要在那個門洞旁邊就行(`door_window` 畫的)
_MOVABLE_WORDS = ("拉門",)


def is_flight_label(text: str) -> bool:
    """這段字是不是樓梯的梯級標示(`stair.flight_label` 產出的形狀)。

    只認 `UP 18` / `UP` / `DN` 三種 —— 跟 stair.flight_label 是同一份約定,
    那邊改寫法的話這裡要跟著改(已有測試釘住兩邊一致)。
    """
    t = text.strip()
    if t in ("UP", "DN"):
        return True
    return t.startswith("UP ") and t[3:].strip().isdigit()


def is_movable_label(text: str) -> bool:
    """這段字可不可以為了留白而搬家。"""
    return is_flight_label(text) or text.strip() in _MOVABLE_WORDS


def relax_flight_labels(msp, space: "LabelSpace") -> int:
    """把黏在別的字上的梯級標示往梯跑方向挪開。回挪了幾個。

    使用者 2026-08-19 截圖抓到「UP 18樓梯間」黏成一串 —— 量出來重疊是 0,
    兩段字只是**貼著**,所以不能只看重疊,要看留白。

    為什麼挪的是梯級標示、不是室名:室名與面積在房間正中央是製圖慣例,位置有
    意義;梯級標示只要**落在梯段上**就讀得懂,沿著梯跑滑一點完全不影響。
    """
    labels = [e for e in msp.query("TEXT")
              if is_movable_label(str(e.dxf.text))]
    if not labels:
        return 0
    space.scan(msp, skip=labels)

    moved = 0
    for e in labels:
        t = str(e.dxf.text)
        h = float(e.dxf.height)
        try:
            align = e.get_align_enum()
        except Exception:                            # noqa: BLE001
            align = None
        p = e.dxf.align_point if (e.dxf.halign or e.dxf.valign) else e.dxf.insert
        x, y = float(p.x), float(p.y)
        here = text_box(t, h, x, y, align)
        if space.is_clear(here):
            space.occupy(here)
            continue
        # 沿四個方向試,每次退一階;梯跑通常是南北向,但兩種都試比較穩。
        best = None
        for dx, dy in ((0, -1), (0, 1), (-1, 0), (1, 0)):
            for k in range(1, _STAIR_TRIES + 1):
                step = max(_STAIR_STEP * 0.4, h * 1.6)   # 小字小步走
                nx, ny = x + dx * step * k, y + dy * step * k
                b = text_box(t, h, nx, ny, align)
                if space.is_clear(b):
                    best = (b, nx, ny)
                    break
            if best:
                break
        if best is None:
            space.occupy(here)       # 挪不開就留原地,不能不畫
            continue
        b, nx, ny = best
        e.set_placement((nx, ny), align=align or None)
        space.occupy(b)
        moved += 1
    return moved
