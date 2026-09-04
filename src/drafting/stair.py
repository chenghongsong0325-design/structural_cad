"""樓梯(UStair)—— 平面圖的踏步線、折斷線、上/下方向箭頭。

⚠️ **本專案只做折返梯(U 形)**(使用者 2026-09-04:「樓梯只要做折返梯就好,
其他樓梯幫我移除」)。以前還有一個單跑直梯的 `Stair`,窄面寬擠不出走道時當備案
用;梯型統一之後整個拿掉了 —— 四條產線出的圖現在只會有一種樓梯。
要再加梯型(L 形、螺旋)請照 `UStair` 的樣子另開一個類別,不要把直梯撿回來。

建築平面元素(ROADMAP 階段 B1)。沿用專案一貫模式:
  * 資料模型(UStair)與畫圖函式(draw_u_stair)分開,方便單元測試。
  * 圖層不寫死:踏步/折斷線/箭頭掛 HANDRAIL(競賽規範:樓梯、扶手、陽台),
    「上/下」文字掛 TEXT(經 A-TEXT 別名),由呼叫端的 layers 對照表決定。

平面圖畫法(每個梯段):
  * 踏步線:垂直於行進方向的等距線(間距 = 踏步深度 tread),橫跨梯寬。
  * 折斷線:一條帶鋸齒的斜線,畫在梯段約 60% 處——平面圖的剖切高度以上
    (折斷線之後)的踏步用「虛線」表示(HIDDEN 線型,by-entity 蓋掉圖層線型)。
  * 方向箭頭:沿梯段中心線(兩扶手線之間),從起步端畫到折斷線前,末端加
    箭頭;起步端放「上」(或「下」)文字。

座標約定:樓梯間矩形以 origin(左下角)+ width(垂直行進方向)+ length
(沿行進方向)描述;direction 指「上樓的行進方向」(north/south/east/west)。
踏步總長 = steps × tread,必須放得進 length,否則報錯(設計不成立)。

典型用法::

    from src.drafting.stair import UStair, draw_u_stair

    stair = UStair(origin=(6500, 2400), width=1400, length=2700,
                   direction="north", steps_per_flight=8, tread=260)
    draw_u_stair(msp, stair, layers)

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

#: 起步端的實心圓點半徑(mm)。使用者 2026-09-03 給的符號對照表:樓梯 =
#: 踏step線 + 上樓方向箭頭 + **起步端一個圓點** —— 圓點才看得出「從這裡開始爬」。
START_DOT_R = 60
ARROW_HEAD_LEN = 200     # 箭頭斜邊長(mm)
ARROW_HEAD_HALF_W = 70   # 箭頭半寬(mm)

_DIRECTIONS = ("north", "south", "east", "west")


# 起步端文字:參考圖(丙級檢定術科)寫「UP 16」/「DN」—— 上行標**級數**、下行不標。
# 資料層仍用「上」/「下」(spec 的語意),換算成圖面寫法集中在這裡一處。
UP_WORD, DOWN_WORD = "UP", "DN"


def flight_label(label: str, steps: int) -> str:
    """(label, 總級數) → 圖上的文字。

    以前只寫「上」/「下」,看圖的人不知道這座梯爬幾階;參考圖一律寫「UP 16」。
    級數由樓梯自己算(Stair.steps / UStair 的兩梯段合計),不必呼叫端自己寫進
    label —— 寫進去遲早會跟實際級數對不上。"""
    if label.strip() in ("下", DOWN_WORD):
        return DOWN_WORD
    return f"{UP_WORD} {steps}" if steps else UP_WORD


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
def _draw_start_dot(msp, stair, t_mid: float, tail_s: float, rail: str) -> None:
    """箭頭尾端(=起步端)的實心圓點 —— 箭頭只講方向,圓點才講「從這裡起步」。

    畫成一圈細環 + 中心填實(HATCH 太重,兩個同心圓在 1:100 就看得出是實心點)。
    """
    c = stair.to_world(t_mid, tail_s)
    for r in (START_DOT_R, START_DOT_R * 0.55, START_DOT_R * 0.2):
        msp.add_circle(c, radius=r, dxfattribs={"layer": rail})


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
    _draw_start_dot(msp, stair, t_mid, tail_s, rail)
    msp.add_text(
        flight_label(stair.label, stair.steps_per_flight * 2), height=text_height,
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
# 4b. 中央扶手(2026-07-20 加在單跑直梯上)隨著直梯一起拿掉了(2026-09-04:
#    只做折返梯)。折返梯的扶手畫法各事務所略異(中央一道 vs 兩側各一),
#    要補的話是新功能,不是把舊的搬過來。
# 5. **只支援折返梯(UStair)**;L形梯、螺旋梯之後視需要擴充。
#    折返梯的畫法:起步梯段在右、折返在左;折返梯段全畫虛線(剖切面以上)、
#    折斷線只畫在起步梯段——與參考的實際建案圖一致,但各事務所畫法略異。待確認。
# 6. 樓梯間的圍牆不歸本模組(用 Wall 照常畫);本模組只畫梯段符號本身。
#    ⚠️ 真實建築的樓梯必在牆圍起來的樓梯間內——呼叫端(FloorPlanSpec)要配好
#    樓梯間的牆與門,見 apartment_plan 的 demo_spec 示範。
# =============================================================================
