"""樓梯(Stair)—— 平面圖的踏步線、折斷線、上/下方向箭頭。

建築平面元素(ROADMAP 階段 B1)。沿用專案一貫模式:
  * 資料模型(Stair)與畫圖函式(draw_stair)分開,方便單元測試。
  * 圖層不寫死:踏步/折斷線/箭頭掛 HANDRAIL(競賽規範:樓梯、扶手、陽台),
    「上/下」文字掛 TEXT(經 A-TEXT 別名),由呼叫端的 layers 對照表決定。

平面圖畫法(單跑梯段,v1):
  * 踏步線:垂直於行進方向的等距線(間距 = 踏步深度 tread),橫跨梯寬。
  * 折斷線:一條帶鋸齒的斜線,畫在梯段約 60% 處——平面圖的剖切高度以上
    (折斷線之後)的踏步用「虛線」表示(HIDDEN 線型,by-entity 蓋掉圖層線型)。
  * 方向箭頭:沿梯段中心線,從起步端畫到折斷線前,末端加箭頭;起步端放
    「上」(或「下」)文字。

座標約定:樓梯間矩形以 origin(左下角)+ width(垂直行進方向)+ length
(沿行進方向)描述;direction 指「上樓的行進方向」(north/south/east/west)。
踏步總長 = steps × tread,必須放得進 length,否則報錯(設計不成立)。

典型用法::

    from src.drafting.stair import Stair, draw_stair

    stair = Stair(origin=(6500, 2400), width=1200, length=2700,
                  direction="north", steps=10, tread=260)
    draw_stair(msp, stair, layers)

⚠️ 待確認假設見模組結尾 PENDING 區塊(踏步尺寸、折斷線畫法、箭頭樣式)。
"""
from __future__ import annotations

from dataclasses import dataclass

from ezdxf.enums import TextEntityAlignment

Point = tuple[float, float]

# 預設踏步深度(mm)。建築技術規則一般樓梯級深約 24~26cm,取 260。預設值,待確認。
DEFAULT_TREAD = 260

# 折斷線位置(佔踏步總長的比例)與鋸齒大小。畫法慣例,待確認。
BREAK_POSITION_RATIO = 0.6
BREAK_ZIGZAG = 150       # 鋸齒凸出量(mm)
BREAK_SKEW = 300         # 折斷線兩端沿行進方向的錯開量(斜線效果,mm)

ARROW_HEAD_LEN = 200     # 箭頭斜邊長(mm)
ARROW_HEAD_HALF_W = 70   # 箭頭半寬(mm)

_DIRECTIONS = ("north", "south", "east", "west")


@dataclass
class Stair:
    """一座單跑樓梯(平面圖)。

    origin:    樓梯間矩形的「左下角」世界座標(不論方向,一律最小 x/y 角)。
    width:     梯寬(垂直於行進方向,mm)。
    length:    樓梯間沿行進方向的長度(mm);踏步總長需 ≤ length。
    direction: 上樓的行進方向:"north"(+Y)/"south"(-Y)/"east"(+X)/"west"(-X)。
    steps:     踏步數(級數)。
    tread:     踏步深度(級深,mm)。
    label:     起步端文字,預設「上」;畫下行梯段時傳「下」。
    """

    origin: Point
    width: float
    length: float
    direction: str = "north"
    steps: int = 10
    tread: float = DEFAULT_TREAD
    label: str = "上"

    def __post_init__(self) -> None:
        if self.direction not in _DIRECTIONS:
            raise ValueError(f"direction 只能是 {_DIRECTIONS},收到 {self.direction!r}")
        if self.steps < 2:
            raise ValueError(f"踏步數至少 2,收到 {self.steps}")
        if self.flight_length > self.length + 1e-6:
            raise ValueError(
                f"踏步總長 {self.flight_length:.0f}(= {self.steps} 級 × {self.tread:.0f})"
                f" 超過樓梯間長度 {self.length:.0f},放不下"
            )

    @property
    def flight_length(self) -> float:
        """踏步總長 = 踏步數 × 踏步深度。"""
        return self.steps * self.tread

    def to_world(self, t: float, s: float) -> Point:
        """局部座標 → 世界座標(t=橫向 0..width,s=沿行進方向,s=0 起步端)。"""
        return _to_world(self.origin, self.length, self.direction, t, s)


def _to_world(origin: Point, length: float, direction: str, t: float, s: float) -> Point:
    """樓梯局部座標 → 世界座標(直梯與折返梯共用)。

    t = 橫向(垂直於行進方向);s = 縱向(沿行進方向,s=0 是起步端)。
    origin 一律是樓梯間矩形的最小 x/y 角,所以四個方向的對應不同。
    """
    ox, oy = origin
    if direction == "north":   # 起步端在南,往 +Y 上樓
        return (ox + t, oy + s)
    if direction == "south":   # 起步端在北,往 -Y 上樓
        return (ox + t, oy + length - s)
    if direction == "east":    # 起步端在西,往 +X 上樓
        return (ox + s, oy + t)
    # west:起步端在東,往 -X 上樓
    return (ox + length - s, oy + t)


# ---------------------------------------------------------------------------
# 畫圖
# ---------------------------------------------------------------------------
def draw_stair(msp, stair: Stair, layers: dict[str, str], text_height: float = 250) -> None:
    """把一座樓梯畫到 modelspace(踏步線 + 折斷線 + 方向箭頭 + 文字)。

    踏步線在折斷線之前用實線、之後用 HIDDEN 虛線(平面剖切高度以上的部分)。
    """
    rail = layers["HANDRAIL"]
    text_layer = layers["A-TEXT"]
    w = stair.width
    break_s = stair.flight_length * BREAK_POSITION_RATIO

    # (1) 踏步線:s = i×tread,i = 1..steps。
    for i in range(1, stair.steps + 1):
        s = i * stair.tread
        attribs = {"layer": rail}
        if s > break_s:
            attribs["linetype"] = "HIDDEN"   # 剖切線以上 → 虛線
        msp.add_line(stair.to_world(0, s), stair.to_world(w, s), dxfattribs=attribs)

    # (2) 折斷線:斜線 + 中央鋸齒(局部座標算好再轉世界)。
    #     兩端沿行進方向錯開 BREAK_SKEW 形成斜線;中央凸出/凹入各一次形成鋸齒。
    s0 = break_s - BREAK_SKEW / 2
    s1 = break_s + BREAK_SKEW / 2
    mid_s = (s0 + s1) / 2
    pts_local = [
        (0.0, s0),
        (w * 0.42, s0 + (s1 - s0) * 0.42),
        (w * 0.46, mid_s + BREAK_ZIGZAG),   # 凸
        (w * 0.54, mid_s - BREAK_ZIGZAG),   # 凹
        (w * 0.58, s0 + (s1 - s0) * 0.58),
        (w, s1),
    ]
    msp.add_lwpolyline(
        [stair.to_world(t, s) for t, s in pts_local],
        dxfattribs={"layer": rail},
    )

    # (3) 方向箭頭:中心線從起步端畫到折斷線前,末端加兩撇箭頭。
    t_mid = w / 2
    tail_s = min(500.0, break_s * 0.2)          # 箭尾(留空間給文字)
    head_s = s0 - 100                            # 箭頭尖(折斷線前)
    msp.add_line(
        stair.to_world(t_mid, tail_s), stair.to_world(t_mid, head_s),
        dxfattribs={"layer": rail},
    )
    for dt in (+ARROW_HEAD_HALF_W, -ARROW_HEAD_HALF_W):
        msp.add_line(
            stair.to_world(t_mid, head_s),
            stair.to_world(t_mid + dt, head_s - ARROW_HEAD_LEN),
            dxfattribs={"layer": rail},
        )

    # (4) 「上/下」文字:起步端、中心線上。
    msp.add_text(
        stair.label,
        height=text_height,
        dxfattribs={"layer": text_layer, "style": "STRUCT"},
    ).set_placement(stair.to_world(t_mid, max(tail_s - 250, 100)),
                    align=TextEntityAlignment.MIDDLE_CENTER)


# ---------------------------------------------------------------------------
# 折返梯(U 形)——真實建築最常見的梯型(參考使用者提供的實際建案梯間圖)
# ---------------------------------------------------------------------------
@dataclass
class UStair:
    """一座折返梯(U 形,兩平行梯段 + 中央梯井 + 端部平台)。

    origin:    樓梯間矩形「左下角」世界座標(最小 x/y 角)。
    width:     總寬 = 兩梯段寬 + 梯井縫(垂直於行進方向,mm)。
    length:    樓梯間沿行進方向的長度(含端部平台,mm)。
    direction: 「起步梯段」的上樓行進方向(north/south/east/west);
               起步梯段在行進方向的右側(t 大的一側),折返梯段在左側。
    steps_per_flight: 每個梯段的踏步數。
    tread:     踏步深度(mm)。
    well_gap:  梯井縫寬(兩梯段之間,mm;參考圖為 10cm)。
    label:     起步端文字,如「上23」(級數自己寫進去)。
    """

    origin: Point
    width: float
    length: float
    direction: str = "north"
    steps_per_flight: int = 10
    tread: float = DEFAULT_TREAD
    well_gap: float = 100
    label: str = "上"

    def __post_init__(self) -> None:
        if self.direction not in _DIRECTIONS:
            raise ValueError(f"direction 只能是 {_DIRECTIONS},收到 {self.direction!r}")
        if self.steps_per_flight < 2:
            raise ValueError(f"每梯段踏步數至少 2,收到 {self.steps_per_flight}")
        if self.flight_width < 600:
            raise ValueError(
                f"梯段寬 {self.flight_width:.0f} 太窄(<600),"
                f"請加大總寬或縮小梯井縫"
            )
        if self.landing_depth < 600:
            raise ValueError(
                f"平台深 {self.landing_depth:.0f} 不足(<600):"
                f"踏步 {self.steps_per_flight}×{self.tread:.0f}={self.flight_run:.0f}"
                f" 佔掉太多樓梯間長度 {self.length:.0f}"
            )

    @property
    def flight_width(self) -> float:
        """單一梯段寬 =(總寬 - 梯井縫)/ 2。"""
        return (self.width - self.well_gap) / 2

    @property
    def flight_run(self) -> float:
        """梯段水平長 = 每梯段踏步數 × 踏步深度。"""
        return self.steps_per_flight * self.tread

    @property
    def landing_depth(self) -> float:
        """端部平台深 = 樓梯間長 - 梯段水平長。"""
        return self.length - self.flight_run

    def to_world(self, t: float, s: float) -> Point:
        return _to_world(self.origin, self.length, self.direction, t, s)


def draw_u_stair(msp, stair: UStair, layers: dict[str, str], text_height: float = 250) -> None:
    """畫一座折返梯:起步梯段(右)+ 折返梯段(左)+ 梯井線 + 平台 + 箭頭文字。

    平面剖切慣例:起步梯段在折斷線前實線、之後虛線;折返梯段整段在剖切面
    以上 → 全部虛線(HIDDEN)。
    """
    rail = layers["HANDRAIL"]
    text_layer = layers["A-TEXT"]
    w = stair.width
    fw = stair.flight_width
    run = stair.flight_run
    break_s = run * BREAK_POSITION_RATIO

    # (1) 踏步線。起步梯段:t ∈ [w-fw, w];折返梯段:t ∈ [0, fw]。
    for i in range(1, stair.steps_per_flight + 1):
        s = i * stair.tread
        up_attribs = {"layer": rail}
        if s > break_s:
            up_attribs["linetype"] = "HIDDEN"
        msp.add_line(stair.to_world(w - fw, s), stair.to_world(w, s), dxfattribs=up_attribs)
        # 折返梯段全在剖切面以上 → 一律虛線。
        msp.add_line(stair.to_world(0, s), stair.to_world(fw, s),
                     dxfattribs={"layer": rail, "linetype": "HIDDEN"})

    # (2) 梯井線:兩條,從起步端到平台邊。
    for t in (fw, w - fw):
        msp.add_line(stair.to_world(t, 0), stair.to_world(t, run), dxfattribs={"layer": rail})

    # (3) 平台邊線(梯段結束處,橫跨全寬)。
    msp.add_line(stair.to_world(0, run), stair.to_world(w, run), dxfattribs={"layer": rail})

    # (4) 折斷線:只畫在起步梯段上(斜線 + 鋸齒)。
    s0 = break_s - BREAK_SKEW / 2
    s1 = break_s + BREAK_SKEW / 2
    mid_s = (s0 + s1) / 2
    t0 = w - fw
    pts_local = [
        (t0, s0),
        (t0 + fw * 0.42, s0 + (s1 - s0) * 0.42),
        (t0 + fw * 0.46, mid_s + BREAK_ZIGZAG),
        (t0 + fw * 0.54, mid_s - BREAK_ZIGZAG),
        (t0 + fw * 0.58, s0 + (s1 - s0) * 0.58),
        (t0 + fw, s1),
    ]
    msp.add_lwpolyline(
        [stair.to_world(t, s) for t, s in pts_local], dxfattribs={"layer": rail}
    )

    # (5) 方向箭頭 + 文字(沿起步梯段中心)。
    t_mid = w - fw / 2
    tail_s = min(500.0, break_s * 0.2)
    head_s = s0 - 100
    msp.add_line(stair.to_world(t_mid, tail_s), stair.to_world(t_mid, head_s),
                 dxfattribs={"layer": rail})
    for dt in (+ARROW_HEAD_HALF_W, -ARROW_HEAD_HALF_W):
        msp.add_line(
            stair.to_world(t_mid, head_s),
            stair.to_world(t_mid + dt, head_s - ARROW_HEAD_LEN),
            dxfattribs={"layer": rail},
        )
    msp.add_text(
        stair.label, height=text_height,
        dxfattribs={"layer": text_layer, "style": "STRUCT"},
    ).set_placement(stair.to_world(t_mid, max(tail_s - 250, 100)),
                    align=TextEntityAlignment.MIDDLE_CENTER)


# =============================================================================
# PENDING(待確認假設彙整)
# =============================================================================
# 1. 踏步深度預設 260mm(建築技術規則一般樓梯級深約 24~26cm 的常見值);
#    梯寬、級高不在平面圖表現(級高是剖面資訊),故本模組不管。待確認。
# 2. 折斷線畫法:位置在踏步總長 60% 處、兩端沿行進方向錯開 300 形成斜線、
#    中央鋸齒凸出 ±150。各事務所畫法略異(有的用雙折斷線)。待確認。
# 3. 剖切線以上的踏步用 HIDDEN 虛線表示(by-entity 蓋掉 HANDRAIL 圖層線型);
#    也有畫法是直接省略不畫。待確認。
# 4. 箭頭樣式:單線箭桿 + 兩撇開放式箭頭(長 200、半寬 70);「上」字在起步端。
#    考題常見「上 N」含級數、或箭尾畫小圓圈,皆可再加。待確認。
# 5. 支援單跑直梯(Stair)與折返梯(UStair);L形梯、螺旋梯之後視需要擴充。
#    折返梯的畫法:起步梯段在右、折返在左;折返梯段全畫虛線(剖切面以上)、
#    折斷線只畫在起步梯段——與參考的實際建案圖一致,但各事務所畫法略異。待確認。
# 6. 樓梯間的圍牆不歸本模組(用 Wall 照常畫);本模組只畫梯段符號本身。
#    ⚠️ 真實建築的樓梯必在牆圍起來的樓梯間內——呼叫端(FloorPlanSpec)要配好
#    樓梯間的牆與門,見 apartment_plan 的 demo_spec 示範。
# =============================================================================
