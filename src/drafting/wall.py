"""建築牆產生器 —— 雙線牆、可開洞口(門/窗)。

這是把專案從「純結構」擴展到「建築平面」的第一塊。設計沿用 members.py 的模式:
  * 資料模型(Wall / Opening)與畫圖函式分開,方便單元測試。
  * 用向量運算,水平/垂直/斜向牆都能畫(不假設牆一定水平)。
  * 圖層不寫死,由呼叫端傳入的 layers 對照表決定(見 apply_standard),
    因此天然支援樓層前綴。建築牆圖層代碼為 A-WALL(見 default.yaml,待確認)。

牆的畫法:
  * 「雙線牆」= 沿牆中心線兩側各偏移 thickness/2 的兩條平行線,兩端再封口。
    實作上把每一段「實牆」畫成一個封閉矩形(封閉多義線),矩形的兩長邊就是兩條
    平行線、兩短邊就是封口——一次滿足「兩條平行線 + 兩端封口」。
  * 開洞口:洞口沿牆中心線把牆體「切斷」,洞口範圍內不畫牆,洞口兩側各自封口
    (形成門/窗的門樘/窗樘邊)。做法是先把牆長 [0, length] 減去各洞口區間,
    得到若干段「實牆」,每段各畫一個封閉矩形。

⚠️ 待確認假設(詳見模組結尾的 PENDING 說明與各常數註解):
  * 牆厚預設值(外牆/內牆)為業界常見值,非公司實際標準。
  * 兩端一律封口(未處理牆與牆相接時的角部接合)。
  * 門與窗目前畫法相同(都只是把牆斷開),未畫窗的窗台線、門的開門弧線。

典型用法::

    from src.drafting.wall import Wall, Opening, draw_wall, EXTERIOR_WALL_THICKNESS

    wall = Wall(
        start=(0, 0), end=(4000, 0), thickness=EXTERIOR_WALL_THICKNESS,
        openings=[Opening(position=2000, width=900)],   # 距起點 2m 處開一個 900mm 門洞
    )
    draw_wall(msp, wall, layers["A-WALL"])
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

Point = tuple[float, float]

# ---------------------------------------------------------------------------
# 牆厚預設值(mm)——⚠️ 預設值,待確認
# ---------------------------------------------------------------------------
# 台灣常見:外牆多為 24cm 紅磚(1B)或 RC 20~25cm;內牆多為 12cm(1/2B)或 15cm。
# 這裡取常見代表值當預設,方便示範;公司/業主的實際牆厚標準確認後再改。
EXTERIOR_WALL_THICKNESS = 240   # 外牆預設厚度(mm)。預設值,待確認
INTERIOR_WALL_THICKNESS = 120   # 內牆預設厚度(mm)。預設值,待確認


# ---------------------------------------------------------------------------
# 資料模型
# ---------------------------------------------------------------------------
@dataclass
class Opening:
    """牆上的一個洞口(門或窗)。

    position: 洞口「中心」沿牆中心線、距牆起點(start)的距離(mm)。
    width:    洞口寬度(mm)。
    kind:     "door" / "window",目前畫法相同(都只是把牆斷開);保留欄位供日後
              區分(例如窗畫窗台線、門畫開門弧)。待確認。
    """

    position: float
    width: float
    kind: str = "door"

    @property
    def span(self) -> tuple[float, float]:
        """洞口沿牆中心線的起訖距離 (start_dist, end_dist)。"""
        half = self.width / 2
        return (self.position - half, self.position + half)


@dataclass
class Wall:
    """一道牆,用中心線起訖點 + 厚度描述,可帶若干洞口。"""

    start: Point
    end: Point
    thickness: float = EXTERIOR_WALL_THICKNESS
    openings: list[Opening] = field(default_factory=list)

    @property
    def length(self) -> float:
        (x1, y1), (x2, y2) = self.start, self.end
        return math.hypot(x2 - x1, y2 - y1)

    @property
    def unit_vector(self) -> Point:
        """沿牆中心線方向(start → end)的單位向量。"""
        length = self.length
        if length == 0:
            raise ValueError("Wall 的起點與終點不能相同(長度為 0)")
        (x1, y1), (x2, y2) = self.start, self.end
        return ((x2 - x1) / length, (y2 - y1) / length)

    @property
    def normal_vector(self) -> Point:
        """垂直於牆中心線的單位向量(逆時針轉 90 度),用來往牆兩側偏移半個牆厚。"""
        ux, uy = self.unit_vector
        return (-uy, ux)

    def point_at(self, distance: float) -> Point:
        """牆中心線上、距起點 distance(mm)處的座標。"""
        (x1, y1) = self.start
        ux, uy = self.unit_vector
        return (x1 + ux * distance, y1 + uy * distance)


# ---------------------------------------------------------------------------
# 洞口 → 實牆分段(純計算)
# ---------------------------------------------------------------------------
def solid_segments(length: float, openings: list[Opening]) -> list[tuple[float, float]]:
    """把牆長 [0, length] 扣掉各洞口區間後,回傳剩下的「實牆」分段清單。

    每段是 (起點距離, 終點距離)。洞口會被裁切到 [0, length] 範圍內;重疊或相鄰的
    洞口會自然合併。若洞口蓋滿整道牆,回傳空清單。

    >>> solid_segments(4000, [Opening(2000, 900)])
    [(0.0, 1550.0), (2450.0, 4000.0)]
    """

    # 把每個洞口裁切到 [0, length],丟掉無效(寬度<=0 或完全在牆外)的區間,再依起點排序。
    spans = []
    for op in openings:
        a, b = op.span
        a = max(0.0, a)
        b = min(float(length), b)
        if b > a:
            spans.append((a, b))
    spans.sort()

    segments: list[tuple[float, float]] = []
    cursor = 0.0
    for a, b in spans:
        if a > cursor:
            segments.append((cursor, a))
        cursor = max(cursor, b)
    if cursor < length:
        segments.append((cursor, float(length)))
    return segments


# ---------------------------------------------------------------------------
# 畫圖
# ---------------------------------------------------------------------------
def draw_wall(msp, wall: Wall, layer: str) -> None:
    """畫一道雙線牆(可含洞口):每段實牆畫成一個封閉矩形(= 兩平行線 + 兩端封口)。

    洞口處牆體斷開,洞口兩側各自封口。水平/垂直/斜向牆皆可,因為用向量運算。
    """

    nx, ny = wall.normal_vector
    half_t = wall.thickness / 2

    for a, b in solid_segments(wall.length, wall.openings):
        (ax, ay) = wall.point_at(a)
        (bx, by) = wall.point_at(b)
        points = [
            (ax + nx * half_t, ay + ny * half_t),
            (bx + nx * half_t, by + ny * half_t),
            (bx - nx * half_t, by - ny * half_t),
            (ax - nx * half_t, ay - ny * half_t),
        ]
        msp.add_lwpolyline(points, close=True, dxfattribs={"layer": layer})


# =============================================================================
# PENDING(待確認假設彙整)
# =============================================================================
# 1. 牆厚:EXTERIOR_WALL_THICKNESS=240、INTERIOR_WALL_THICKNESS=120,為業界常見
#    代表值,非公司實際標準。
# 2. 圖層:建築牆用 A-WALL(色號 7 白/黑、實線),為 AIA 常見暫定值;內外牆共用
#    同一圖層,僅以厚度區分。公司若另有建築圖層命名/內外牆分層,再改 default.yaml。
# 3. 封口方式:每段實牆的兩端一律以「垂直於牆的短邊」封口(封閉矩形的短邊)。
#    未處理牆與牆相接的角部接合(T 形/L 形交會),目前每道牆各自獨立封口。
# 4. 洞口:門與窗畫法相同,都只是把牆斷開;未畫窗台線、開門弧線;Opening.kind
#    欄位已預留供日後區分。
# 5. 洞口位置以「距牆起點的距離」描述;若日後要用絕對座標或比例定位,再擴充。
# =============================================================================
