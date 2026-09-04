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

#: 洞口寬到這裡(mm)就改畫**子母門**(一寬一窄兩片,各一條弧)。
#: 台灣的透天大門是 1m 級的雙扇門;單一片 1m 寬的門扇太重,真實圖不會這樣畫。
PAIR_DOOR_MIN_W = 1000.0
#: 子母門的「母」(平常在用的那片)佔洞口寬多少。
PAIR_DOOR_MAIN_FRAC = 0.68


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


#: 交錯型窗的窗扇線離牆中心線多遠(佔牆厚的比例)。
SLIDING_SASH_OFFSET = 0.20
#: 兩片窗扇各佔洞口寬的比例(>0.5 才會「交錯」重疊,那正是這個符號的意思)。
SLIDING_SASH_SPAN = 0.55


def sliding_window_block_name() -> str:
    return "WINDOW_SLIDING"


def create_sliding_window_block(doc) -> str:
    """建立(或取得)**交錯型窗**的單位圖塊 —— 台灣住宅最常見的左右滑動玻璃窗。

    使用者 2026-09-03 給的符號對照表:三條等距平行線是**固定窗**(打不開),
    住宅的窗幾乎都是交錯型 —— 兩片窗扇在不同的軌道上左右滑動,平面上畫成
    **兩條錯開、在中段重疊**的線,外加牆兩面的框線。

    單位窗:沿 +X 從 0 到 1(洞口寬方向),跨牆厚方向 y 從 -0.5 到 0.5。
    """
    name = sliding_window_block_name()
    if name in doc.blocks:
        return name
    blk = doc.blocks.new(name)
    for y in (-0.5, 0.5):                       # 牆兩面的窗框線
        blk.add_line((0, y), (1, y), dxfattribs={"layer": "0"})
    off, span = SLIDING_SASH_OFFSET, SLIDING_SASH_SPAN
    blk.add_line((0.0, -off), (span, -off), dxfattribs={"layer": "0"})
    blk.add_line((1.0 - span, off), (1.0, off), dxfattribs={"layer": "0"})
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

    sliding: bool = False   # 橫拉門:門扇沿牆滑開,平面不畫開門弧(門前空間不足時用)
    # sliding 門扇旁邊註記的字。捲門(車庫)平面上也是「沿牆滑開、不畫弧」的畫法,
    # 但圖上不能寫「拉門」——看圖的師傅要知道那是往上捲的鐵捲門。
    label: str = "拉門"
    hinge: str = "left"
    swing: str = "out"
    width: float | None = None

    def place_in_wall(self, msp, wall: Wall, opening: Opening, layers: dict[str, str]):
        """把這扇門對齊放進 wall 的 opening,回傳插入的 blockref。

        sliding=True 時畫**橫拉門**:門扇平行牆面畫在洞口旁,並註明「拉門」——
        開門弧線撞牆/撞家具時的正解(門與動線規範:空間不足時改用橫拉門並註明)。"""
        if self.sliding:
            return self._place_sliding(msp, wall, opening, layers)
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

        # 子母門:洞口寬到一定程度(台灣的透天大門)就畫成**一寬一窄兩片**,
        # 各自一條弧、往同一側開 —— 使用者給的〈平面圖標示符號〉寫的就是
        # 「兩片門一寬一窄,平常使用較寬的門」。一片 1m 寬的門扇在真實圖上
        # 幾乎不會出現(太重),看圖的人一眼就知道那不是大門的畫法。
        if w >= PAIR_DOOR_MIN_W:
            main = self._leaf(msp, wall, hinge_dist, latch_angle, desired_swing,
                              w * PAIR_DOOR_MAIN_FRAC, layers)
            # 子扇:鉸鏈在**另一端**,開啟方向與母扇同側。
            other_dist = d1 if self.hinge == "left" else d0
            other_angle = theta + 180 if self.hinge == "left" else theta
            self._leaf(msp, wall, other_dist, other_angle, desired_swing,
                       w * (1.0 - PAIR_DOOR_MAIN_FRAC), layers)
            return main
        return self._leaf(msp, wall, hinge_dist, latch_angle, desired_swing,
                          w, layers)

    def _leaf(self, msp, wall: Wall, hinge_dist: float, latch_angle: float,
              desired_swing: float, w: float, layers: dict[str, str]):
        """畫一片門扇(門板 + 開啟弧)。單開門一片,子母門兩片。"""
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


    def _place_sliding(self, msp, wall: Wall, opening: Opening, layers: dict):
        """橫拉門:門扇畫成貼著牆面、與洞口等寬的一條線 + `label` 字樣。"""
        import math

        d0, d1 = _opening_jambs(opening)
        theta = math.radians(_wall_angle_deg(wall))
        nx, ny = -math.sin(theta), math.cos(theta)      # 牆法線
        off = wall.thickness / 2.0 + 30.0               # 門扇貼在牆的一側
        side = 1.0 if self.swing == "out" else -1.0
        p0 = wall.point_at(d0)
        p1 = wall.point_at(d1)
        a = (p0[0] + nx * off * side, p0[1] + ny * off * side)
        b = (p1[0] + nx * off * side, p1[1] + ny * off * side)
        line = msp.add_line(a, b, dxfattribs={"layer": layers["A-DOOR"]})
        mid = ((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0)
        # ⚠️ 中文一定要指定 style="STRUCT":預設的 Standard 樣式用 txt.shx,沒有
        #    中文字形,AutoCAD 會把「拉門」畫成「??」(2026-08-03 在圖上看到)。
        txt = msp.add_text(self.label, height=150,
                           dxfattribs={"layer": layers.get("A-TEXT",
                                                           layers["A-DOOR"]),
                                       "style": "STRUCT"})
        txt.set_placement((mid[0] + nx * 180 * side, mid[1] + ny * 180 * side))
        return line


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
    #: "sliding" = 交錯型窗(左右滑動,台灣住宅預設);"fixed" = 固定窗(n 條平行線)。
    #: ⚠️ 鏡射與 unit.py 複製窗的時候**要把這個欄位帶著走** —— 本專案「鏡射弄丟
    #:    東西」已經踩過四次(拉門、陽台、捲門 label、核的款式)。
    style: str = "sliding"

    def place_in_wall(self, msp, wall: Wall, opening: Opening, layers: dict[str, str]):
        """把這扇窗對齊放進 wall 的 opening,回傳插入的 blockref。

        窗沿洞口寬方向(牆長)展開,跨度 = 牆厚(yscale=wall.thickness),
        因此三條線分別落在牆的兩面與中線。
        """
        if self.style == "sliding":
            name = create_sliding_window_block(msp.doc)
        else:
            create_window_block(msp.doc, self.lines)
            name = window_block_name(self.lines)

        w = self.width if self.width is not None else opening.width
        d0, _ = _opening_jambs(opening)
        theta = _wall_angle_deg(wall)
        start_pt = wall.point_at(d0)

        return msp.add_blockref(
            name,
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
