"""門(Door)與窗(Window)—— 做成可重用的 DXF 圖塊,能對齊牆洞口。

建築平面的第三塊(前兩塊:wall.py、room.py)。設計:
  * Door / Window 用「單位圖塊 + 插入變換」實作——這是 AutoCAD 放門窗的標準做法:
    圖塊只定義「單位大小」的符號一次,之後每個門/窗都是一次插入(INSERT),
    用位置/旋轉/縮放/鏡射把單位符號擺到牆洞口上。
      - 門圖塊:單位門(鉸鏈在原點、洞口沿 +X 到 1、門扇開到 +Y、開啟弧半徑 1)。
        四種開啟方向(左/右鉸鏈 × 內/外開)全用「同一個圖塊 + 鏡射/旋轉」達成。
      - 窗圖塊:單位窗(沿 +X 0..1、跨牆厚 -0.5..0.5),n 條平行線代表玻璃(雙線/三線)。
  * 圖塊內部實體掛在圖層 "0",插入時繼承 blockref 的圖層(A-DOOR / A-GLAZ),
    因此同一個圖塊定義可用在任何樓層前綴,不必為每個前綴各建一份。
  * place_in_wall(wall, opening):把門/窗自動對齊到牆上某個洞口——寬度取洞口寬,
    位置/角度由牆的方向與洞口位置算出。門扇寬 = 洞口寬;窗跨度 = 牆厚。

⚠️ 待確認假設(詳見模組結尾 PENDING 區塊):門寬/窗寬預設值、門扇畫成單線、
   開啟角度固定 90°、「內/外開」對應牆法線哪一側、窗高不在平面圖表現等。

典型用法::

    from src.drafting.door_window import Door, Window
    from src.drafting.wall import Wall, Opening

    op = Opening(position=2000, width=900)
    wall = Wall(start=(0,0), end=(4000,0), thickness=240, openings=[op])
    draw_wall(msp, wall, layers["A-WALL"])
    Door(hinge="left", swing="out").place_in_wall(msp, wall, op, layers)
    Window(lines=3).place_in_wall(msp, wall, window_opening, layers)
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from src.drafting.wall import Opening, Wall

# 預設尺寸(mm)——⚠️ 預設值,待確認
DEFAULT_DOOR_WIDTH = 900     # 室內門常見 80~90cm,取 90。預設值,待確認
DEFAULT_WINDOW_WIDTH = 1200  # 窗寬常見值。預設值,待確認

DOOR_BLOCK = "DOOR"
DOOR_SWING_ANGLE = 90.0      # 開啟弧線角度(度)。固定 90°,待確認


# ---------------------------------------------------------------------------
# 圖塊定義(單位大小,內部實體掛圖層 "0" 以繼承插入圖層)
# ---------------------------------------------------------------------------
def create_door_block(doc, *, name: str = DOOR_BLOCK) -> str:
    """建立(或取得)單位門圖塊。已存在就直接回傳名稱。

    單位門:鉸鏈在 (0,0),洞口沿 +X 到 (1,0),門扇開到 (0,1),
    開啟弧線為半徑 1、從 0° 到 90° 的四分之一圓。
    """
    if name in doc.blocks:
        return name
    blk = doc.blocks.new(name)
    # 門扇(開啟後的門板,以單線表示)。
    blk.add_line((0, 0), (0, 1), dxfattribs={"layer": "0"})
    # 開啟弧線。
    blk.add_arc((0, 0), radius=1, start_angle=0, end_angle=DOOR_SWING_ANGLE, dxfattribs={"layer": "0"})
    return name


def window_block_name(lines: int) -> str:
    return f"WINDOW_{lines}LINE"


def create_window_block(doc, lines: int = 3) -> str:
    """建立(或取得)單位窗圖塊(n 條平行線)。已存在就直接回傳名稱。

    單位窗:沿 +X 從 0 到 1(洞口寬方向),跨牆厚方向 y 從 -0.5 到 0.5。
    lines 條平行線在 y 方向均分(如 3 線 → y = -0.5, 0, +0.5),各沿 x 0→1。
    """
    if lines < 2:
        raise ValueError(f"窗至少要 2 條線(雙線),目前 lines={lines}")
    name = window_block_name(lines)
    if name in doc.blocks:
        return name
    blk = doc.blocks.new(name)
    for i in range(lines):
        y = -0.5 + i / (lines - 1)   # 在 [-0.5, 0.5] 均分
        blk.add_line((0, y), (1, y), dxfattribs={"layer": "0"})
    return name


# ---------------------------------------------------------------------------
# 共用:牆角度、洞口端點
# ---------------------------------------------------------------------------
def _wall_angle_deg(wall: Wall) -> float:
    ux, uy = wall.unit_vector
    return math.degrees(math.atan2(uy, ux))


def _opening_jambs(opening: Opening) -> tuple[float, float]:
    """洞口沿牆中心線的兩端距離 (d0=近起點側, d1=近終點側)。"""
    half = opening.width / 2
    return (opening.position - half, opening.position + half)


# ---------------------------------------------------------------------------
# 門
# ---------------------------------------------------------------------------
@dataclass
class Door:
    """一扇門的「開啟方式」。實際寬度/位置在放進牆洞口時由洞口決定。

    hinge: "left"/"right" —— 鉸鏈在洞口「近牆起點側(left)」或「近牆終點側(right)」的門樘。
    swing: "out"/"in"     —— 門往牆法線 +n(out)或 -n(in)側開。
                             ⚠️ +n 是牆行進方向(start→end)的左手側;哪一側是室內/室外
                             取決於牆怎麼定義,見 PENDING。
    width: 若指定則覆寫洞口寬(預設 None = 用洞口寬,自動對齊)。
    """

    hinge: str = "left"
    swing: str = "out"
    width: float | None = None

    def place_in_wall(self, msp, wall: Wall, opening: Opening, layers: dict[str, str]):
        """把這扇門對齊放進 wall 的 opening,回傳插入的 blockref。"""
        create_door_block(msp.doc)

        w = self.width if self.width is not None else opening.width
        d0, d1 = _opening_jambs(opening)
        theta = _wall_angle_deg(wall)

        if self.hinge == "left":
            hinge_dist, latch_angle = d0, theta
        elif self.hinge == "right":
            hinge_dist, latch_angle = d1, theta + 180
        else:
            raise ValueError(f"hinge 只能是 'left' 或 'right',收到 {self.hinge!r}")

        if self.swing == "out":
            desired_swing = theta + 90
        elif self.swing == "in":
            desired_swing = theta - 90
        else:
            raise ValueError(f"swing 只能是 'out' 或 'in',收到 {self.swing!r}")

        hinge_pt = wall.point_at(hinge_dist)
        # 單位門的 +Y(門扇開啟方向)在旋轉 latch_angle 後指向 latch_angle+90;
        # 若和 desired_swing 差 180° 就用 yscale 負值鏡射過去。
        delta = (desired_swing - (latch_angle + 90)) % 360
        yscale = w if delta < 1 or delta > 359 else -w

        return msp.add_blockref(
            DOOR_BLOCK,
            hinge_pt,
            dxfattribs={
                "layer": layers["A-DOOR"],
                "xscale": w,
                "yscale": yscale,
                "rotation": latch_angle,
            },
        )


# ---------------------------------------------------------------------------
# 窗
# ---------------------------------------------------------------------------
@dataclass
class Window:
    """一扇窗的符號樣式(平面圖)。lines=2 雙線、3 三線…寬度/位置由洞口決定。

    width: 若指定則覆寫洞口寬(預設 None = 用洞口寬)。
    """

    lines: int = 3
    width: float | None = None

    def place_in_wall(self, msp, wall: Wall, opening: Opening, layers: dict[str, str]):
        """把這扇窗對齊放進 wall 的 opening,回傳插入的 blockref。

        窗沿洞口寬方向(牆長)展開,跨度 = 牆厚(yscale=wall.thickness),
        因此三條線分別落在牆的兩面與中線。
        """
        create_window_block(msp.doc, self.lines)

        w = self.width if self.width is not None else opening.width
        d0, _ = _opening_jambs(opening)
        theta = _wall_angle_deg(wall)
        start_pt = wall.point_at(d0)

        return msp.add_blockref(
            window_block_name(self.lines),
            start_pt,
            dxfattribs={
                "layer": layers["A-GLAZ"],
                "xscale": w,
                "yscale": wall.thickness,
                "rotation": theta,
            },
        )


# =============================================================================
# PENDING(待確認假設彙整)
# =============================================================================
# 1. 門寬:DEFAULT_DOOR_WIDTH=900、窗寬 DEFAULT_WINDOW_WIDTH=1200(mm),為常見值,
#    非公司標準。實際放進洞口時是用「洞口寬」自動對齊,這兩個常數只是預設參考。
# 2. 門扇畫法:門扇以「單線」表示(非門板厚度矩形);開啟弧線固定 90°。若公司圖例
#    用門板矩形或不同開啟角度,再改。
# 3. 內/外開:swing "out"/"in" 對應牆法線 +n/-n(+n = 牆 start→end 的左手側)。
#    哪一側是室內/室外,取決於這道牆在戶型裡怎麼定義,本模組不判斷。待確認。
# 4. 左/右鉸鏈:hinge "left"/"right" 指洞口「近牆起點/近牆終點」的門樘,而非以人
#    面向門的左右(那需要先定義從哪一側看)。待確認。
# 5. 窗符號:以 n 條平行線(預設 3 線 = 兩面 + 中線)表示,跨度取牆厚。雙線/三線
#    的實際選用與是否加窗框、開窗方向記號,待確認。
# 6. 窗高/窗台高:平面圖不表現(那是立面/剖面的資訊)。此模組只畫平面符號;
#    若日後要帶窗高資料供立面用,再於 Window 加欄位。待確認。
# 7. 圖塊內部實體掛圖層 "0" 以繼承插入圖層;門窗圖層 A-DOOR/A-GLAZ 為 AIA 暫定
#    代碼與色號(綠/青),見 default.yaml。待確認。
# =============================================================================
