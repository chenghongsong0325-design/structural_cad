"""房間(Room)—— 由牆或角點圍成的封閉區域,自動算面積、標名稱。

建築平面的第二塊(第一塊是 wall.py)。沿用專案一貫模式:
  * 資料模型(Room)與畫圖函式(draw_room_label)分開,方便單元測試。
  * 圖層不寫死,由呼叫端傳入 layers 對照表的值(建築文字圖層代碼 A-TEXT,
    見 default.yaml,待確認),天然支援樓層前綴。

房間的兩種建立方式:
  1. 直接給角點:Room(name="客廳", points=[(0,0), (6000,0), ...])
  2. 給一組牆:Room.from_walls("客廳", [w1, w2, w3, w4])
     ——把各道牆的「中心線」串成封閉迴路(牆的順序、方向可以亂給,
     只要端點能彼此銜接;串不起來或不封閉會報錯)。

面積與形心:
  * 面積用鞋帶公式(shoelace)計算,角點順時針或逆時針皆可。
  * 形心(centroid)= 多邊形面積形心,標註文字放這裡。
  * 單位:座標是 mm,面積換算 m²(÷ 10^6)與坪(1 坪 = 3.30578 m²,台灣慣用)。

⚠️ 待確認假設(詳見模組結尾 PENDING 區塊):
  * 面積以「牆中心線」圍成的多邊形計算,不是扣掉牆厚的室內淨面積。
  * 標註格式(名稱一行、面積一行)與文字高度為暫定。

典型用法::

    from src.drafting.room import Room, draw_room_label

    room = Room(name="客廳", points=[(0,0), (6000,0), (6000,8000), (0,8000)])
    room.area_m2    # 48.0
    room.area_ping  # 14.52
    draw_room_label(msp, room, layers["A-TEXT"])
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from ezdxf.enums import TextEntityAlignment

from src.drafting.wall import Wall

Point = tuple[float, float]

# 1 坪 = 3.30578 m²(台灣慣用;由 400/121 m² 而來)。
M2_PER_PING = 3.30578

# 標註格式——⚠️ 暫定,待確認(公司圖面上面積寫法/小數位數可能不同)。
AREA_FORMAT = "{m2:.1f}㎡"


# ---------------------------------------------------------------------------
# 資料模型
# ---------------------------------------------------------------------------
@dataclass
class Room:
    """一個房間:名稱 + 圍成它的角點(封閉多邊形,首點不必重複)+ 用途類型。"""

    name: str
    points: list[Point]
    kind: str = ""   # 用途類型(如 "living"/"bedroom"/"bathroom"),目前僅記錄
    code: str = ""   # 房間類型代碼(如 "X05"),真實建案圖的帶框標籤用;空=不畫標籤
    note: str = ""   # 附註(如「機械排風」)。不畫在圖上,僅供 validate_spec 檢核用

    def __post_init__(self) -> None:
        if len(self.points) < 3:
            raise ValueError(f"Room 至少需要 3 個角點,目前只有 {len(self.points)} 個")

    # -- 幾何 ---------------------------------------------------------------
    @property
    def _signed_area_mm2(self) -> float:
        """鞋帶公式的「有號」面積(逆時針為正);形心計算需要保留正負號。"""
        s = 0.0
        n = len(self.points)
        for i in range(n):
            x1, y1 = self.points[i]
            x2, y2 = self.points[(i + 1) % n]
            s += x1 * y2 - x2 * y1
        return s / 2

    @property
    def area_mm2(self) -> float:
        """面積(mm²),角點順時針或逆時針皆可。"""
        return abs(self._signed_area_mm2)

    @property
    def area_m2(self) -> float:
        """面積(m²)。"""
        return self.area_mm2 / 1_000_000

    @property
    def area_ping(self) -> float:
        """面積(坪)。1 坪 = 3.30578 m²。"""
        return self.area_m2 / M2_PER_PING

    @property
    def centroid(self) -> Point:
        """多邊形面積形心(凸房間的標註放置點)。"""
        a = self._signed_area_mm2
        if a == 0:
            raise ValueError("Room 面積為 0,無法計算形心")
        cx = cy = 0.0
        n = len(self.points)
        for i in range(n):
            x1, y1 = self.points[i]
            x2, y2 = self.points[(i + 1) % n]
            cross = x1 * y2 - x2 * y1
            cx += (x1 + x2) * cross
            cy += (y1 + y2) * cross
        return (cx / (6 * a), cy / (6 * a))

    @property
    def label_point(self) -> Point:
        """標註文字的放置點。

        凸房間(矩形等)用形心。凹房間(如挖掉玄關角的 L 形客廳、含套房
        衛浴的 L 形主臥)形心會被缺角拉進凹處,壓到鄰室標籤;這時改用
        「離所有牆邊最遠的內部點」(visual center),讓字落在最大的那塊。
        """
        if self._is_convex():
            return self.centroid
        return self._visual_center()

    def _is_convex(self) -> bool:
        """所有轉角同向 → 凸多邊形(繞行方向不拘)。"""
        pts = self.points
        n = len(pts)
        sign = 0
        for i in range(n):
            x0, y0 = pts[i]
            x1, y1 = pts[(i + 1) % n]
            x2, y2 = pts[(i + 2) % n]
            cross = (x1 - x0) * (y2 - y1) - (y1 - y0) * (x2 - x1)
            if cross != 0:
                s = 1 if cross > 0 else -1
                if sign == 0:
                    sign = s
                elif s != sign:
                    return False
        return True

    def _point_inside(self, p: Point) -> bool:
        """射線法判斷點是否在多邊形內(邊界算內)。"""
        x, y = p
        pts = self.points
        n = len(pts)
        inside = False
        j = n - 1
        for i in range(n):
            xi, yi = pts[i]
            xj, yj = pts[j]
            if (yi > y) != (yj > y):
                xin = (xj - xi) * (y - yi) / (yj - yi) + xi
                if x < xin:
                    inside = not inside
            j = i
        return inside

    def _clearance(self, p: Point) -> float:
        """點到最近牆邊(線段)的距離——內部點取最大即 visual center。"""
        x, y = p
        pts = self.points
        n = len(pts)
        best = math.inf
        for i in range(n):
            x1, y1 = pts[i]
            x2, y2 = pts[(i + 1) % n]
            dx, dy = x2 - x1, y2 - y1
            seg2 = dx * dx + dy * dy
            t = 0.0 if seg2 == 0 else ((x - x1) * dx + (y - y1) * dy) / seg2
            t = max(0.0, min(1.0, t))
            px, py = x1 + t * dx, y1 + t * dy
            best = min(best, math.hypot(x - px, y - py))
        return best

    def _visual_center(self) -> Point:
        """離所有邊最遠的內部點(pole of inaccessibility):粗網格找、再局部細找。"""
        xs = [p[0] for p in self.points]
        ys = [p[1] for p in self.points]
        minx, maxx, miny, maxy = min(xs), max(xs), min(ys), max(ys)
        best = self.centroid
        best_d = self._clearance(best) if self._point_inside(best) else -math.inf
        steps = 32
        cell_x = (maxx - minx) / steps
        cell_y = (maxy - miny) / steps
        for gx in range(steps + 1):
            for gy in range(steps + 1):
                p = (minx + cell_x * gx, miny + cell_y * gy)
                if not self._point_inside(p):
                    continue
                d = self._clearance(p)
                if d > best_d:
                    best_d, best = d, p
        # 在勝出格子附近再細找一輪
        rx, ry = cell_x, cell_y
        for _ in range(4):
            rx, ry = rx / 2, ry / 2
            cx, cy = best
            for gx in range(-2, 3):
                for gy in range(-2, 3):
                    p = (cx + rx * gx, cy + ry * gy)
                    if not self._point_inside(p):
                        continue
                    d = self._clearance(p)
                    if d > best_d:
                        best_d, best = d, p
        return best

    # -- 由牆建立 -------------------------------------------------------------
    @classmethod
    def from_walls(
        cls,
        name: str,
        walls: list[Wall],
        kind: str = "",
        tol: float = 1.0,
    ) -> "Room":
        """把一組牆的「中心線」串成封閉迴路,建立 Room。

        牆的順序與方向可以任意:每次從目前端點找「起點或終點能銜接上」的下一道牆
        (端點距離 <= tol,單位 mm)。全部串完後必須回到起點,否則視為不封閉。

        Raises:
            ValueError: 牆串不成一條連續迴路,或迴路沒有封閉。
        """
        if len(walls) < 3:
            raise ValueError(f"至少需要 3 道牆才能圍出房間,目前只有 {len(walls)} 道")

        def dist(p: Point, q: Point) -> float:
            return math.hypot(p[0] - q[0], p[1] - q[1])

        remaining = list(walls)
        first = remaining.pop(0)
        pts: list[Point] = [first.start, first.end]

        while remaining:
            cur = pts[-1]
            for i, w in enumerate(remaining):
                if dist(w.start, cur) <= tol:
                    pts.append(w.end)
                    remaining.pop(i)
                    break
                if dist(w.end, cur) <= tol:
                    pts.append(w.start)
                    remaining.pop(i)
                    break
            else:
                raise ValueError(
                    f"牆無法串成連續迴路:走到 {cur} 後,剩下 {len(remaining)} 道牆都接不上"
                )

        if dist(pts[-1], pts[0]) > tol:
            raise ValueError(f"牆迴路沒有封閉:終點 {pts[-1]} 沒有回到起點 {pts[0]}")

        return cls(name=name, points=pts[:-1], kind=kind)


# ---------------------------------------------------------------------------
# 標註
# ---------------------------------------------------------------------------
def room_label_lines(room: Room, area_format: str = AREA_FORMAT) -> tuple[str, str]:
    """產生房間標註的兩行文字:(名稱, 面積)。

    >>> room_label_lines(Room("客廳", [(0,0), (6000,0), (6000,8000), (0,8000)]))
    ('客廳', '48.0㎡')
    """
    area_line = area_format.format(m2=room.area_m2, ping=room.area_ping)
    return (room.name, area_line)


def draw_room_label(
    msp,
    room: Room,
    layer: str,
    text_height: float = 250,
    style: str = "STRUCT",
    area_format: str = AREA_FORMAT,
) -> None:
    """在房間放置點標註「名稱 + 面積」兩行文字(名稱在上、面積在下)。

    放置點見 Room.label_point(凸房間=形心;L 形等凹房間=離牆最遠的內部點)。
    行距 = 1.6 × 文字高度(暫定,待確認)。兩行都水平置中對齊在放置點的鉛直線上。
    """
    name_line, area_line = room_label_lines(room, area_format)
    cx, cy = room.label_point
    half_gap = text_height * 0.8   # 放置點上下各半行距

    msp.add_text(
        name_line,
        height=text_height,
        dxfattribs={"layer": layer, "style": style},
    ).set_placement((cx, cy + half_gap), align=TextEntityAlignment.BOTTOM_CENTER)

    msp.add_text(
        area_line,
        height=text_height,
        dxfattribs={"layer": layer, "style": style},
    ).set_placement((cx, cy - half_gap), align=TextEntityAlignment.TOP_CENTER)


def draw_room_tag(
    msp,
    room: Room,
    layer: str,
    text_height: float = 250,
    offset: Point = (0.0, 900.0),
) -> None:
    """房間類型帶框標籤(真實建案畫法):「代碼 名稱」放在方框裡,框與字同掛一層。

    預設放在放置點上方(offset=(0,900)),避開 draw_room_label 的名稱/面積兩行。
    框寬用字數估算:全形字寬 ≈ 字高、半形 ≈ 0.55 字高,左右各留 0.5 字高邊距。
    room.code 為空時不畫(直接 return)。
    """
    if not room.code:
        return
    text = f"{room.code} {room.name}"
    cx, cy = room.label_point
    px, py = cx + offset[0], cy + offset[1]

    est_w = sum(text_height if ord(ch) > 127 else text_height * 0.55 for ch in text)
    half_w = est_w / 2 + text_height * 0.5
    half_h = text_height * 0.9

    msp.add_lwpolyline(
        [(px - half_w, py - half_h), (px + half_w, py - half_h),
         (px + half_w, py + half_h), (px - half_w, py + half_h)],
        close=True, dxfattribs={"layer": layer},
    )
    msp.add_text(
        text, height=text_height,
        dxfattribs={"layer": layer, "style": "STRUCT"},
    ).set_placement((px, py), align=TextEntityAlignment.MIDDLE_CENTER)


# =============================================================================
# PENDING(待確認假設彙整)
# =============================================================================
# 1. 面積算法:以「牆中心線」圍成的多邊形計算(from_walls 串的是 wall.start/end,
#    也就是中心線端點)。實務上房間面積可能要用「室內淨面積」(扣掉半個牆厚)或
#    「含牆面積」,依用途(建照/銷售/裝修)而不同。待確認。
# 2. 坪換算:1 坪 = 3.30578 m²(= 400/121),取 6 位有效數字。若公司慣用
#    3.3058 或 3.306,面積大時會差零點幾坪。待確認。
# 3. 標註格式:AREA_FORMAT = "{m2:.1f}㎡"(m² 一位小數,不加坪數),名稱/面積
#    兩行、行距 1.6×字高、放 label_point。公司圖面的寫法待確認。
# 4. ㎡ 符號(U+33A1)需字型支援;標楷體(kaiu.ttf)有此字,若日後換 .shx 字型
#    可能缺字,屆時改用 "m2"。待確認。
# 5. 文字圖層:A-TEXT 為暫定代碼(同 A-WALL,AIA 慣例),色號 7。待確認。
# 6. kind(用途類型)目前只是記錄欄位,尚未影響畫法(例如廁所鋪磁磚填充)。
# =============================================================================
