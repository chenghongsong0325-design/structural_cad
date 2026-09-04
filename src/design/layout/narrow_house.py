"""Narrow-Frontage Townhouse(v0.7 Phase N1)—— 窄面寬透天產生器。

台灣常見的透天厝面寬只有 4~7 米、往深發展(12~20 米),房間**前後串聯**(不是
兩帶式的左右並排),**單樓梯**,兩側是與鄰房共用的界牆(不開窗),靠前後採光。
既有的 layout_generator 是「兩帶式」(南客廳帶+北臥室帶並排+東側服務核),天生要
≥10 米寬,做不出窄面寬透天。本模組是另一套骨架。

⚠️ **不設天井**(使用者 2026-07-29 定調:所有尺寸的住宅都不要天井)。中段核原本
   西側是 1.4~2.2m 寬的採光天井,在 5~7m 面寬的房子裡等於吃掉 1/4 的面寬;取消
   後那塊地還給衛浴/收納,中段房間改為不對外採光的服務空間(住宅慣例)。

⚠️ 座標:x=面寬(東西),y=進深(南北);**前(臨路/入口)在 y 小的南側**,後院在
y 大的北側。使用者給的是**建築物尺寸**(不是基地);基地=建築+四周退縮,反推得到。

⚠️ 重用:牆/門/窗一律交給 bsp_layout.rooms_to_spec 由房間矩形推導(不重造)。本模組
只負責「窄深 envelope 怎麼切成前後串聯房間 + 中段核」與門窗的收尾修正。

骨架:
  * 前室(客廳 / 前臥,滿面寬,南向採光)
  * 中段核 = 服務格(西:浴廁 + 上方儲藏)+ 樓梯間(東:折返梯貼東界牆、西側留通道)
  * 後室(滿面寬;1F=餐廳+廚房,樓上=後臥)
  * 中段核**每層同位** → 樓梯天生上下對齊(符合柱網原則)

收尾修正(_fix_openings):去界牆窗、浴室只留 1 門、1F 加臨路前門。

N1 範圍:多層 + 單樓梯 + 衛浴 + 管道間 + 家具(Phase 6 擺位),能畫 DXF、能被
Phase 6 評分與 room_circulation 檢查。地下室/結構柱另議。

典型用法::

    floors = generate_narrow_building(7000, 12000, floors=3)   # 建築 7×12、三層
    for label, spec in floors:
        draw_floor_plan(msp, spec, layers)
"""
from __future__ import annotations

import itertools
import random
from dataclasses import dataclass

from shapely.geometry import Point, Polygon

from src.drafting.apartment_plan import DoorPlacement
from src.drafting.door_window import Door
from src.drafting.stair import UStair
from src.drafting.wall import Opening

from src.design.layout.bsp_layout import rooms_to_spec

# 建築線退縮(mm),對齊 HouseBrief.setback,反推基地用。
# ⚠️ 這是**獨棟**的規則(四周都留空地)。連棟街屋左右與鄰戶共壁、側邊不退縮,
#    那條路走 `lot` 參數(見 `_build_floor`)—— 基地由 `design/zoning.py` 推導,
#    不是這裡的 W + 2*SETBACK。
SETBACK = 2000.0

# 面寬 / 進深 合理範圍(mm)。窄面寬透天的定義域。
# ⚠️ 下限是**實測**出來的(掃描 3.0~7.0m × 8.0~12.0m,兩道關卡 plan_check +
#    code_check 都過才算數),不是估的:
#      * 寬 3.5m:再窄(3.0m)後段餐廳只剩 0.3㎡、前後段動線斷掉,是真的塞不下。
#        3.5m 時服務格 1.5m + 樓梯間 2.0m,梯段淨寬 875mm(§33 要 ≥750),剛好卡住。
#      * 深 9.5m:9.0m 仍過硬關卡,但臥室短邊 <1.8m(擺了床沒走道)會出警告;
#        9.5m 起零錯誤零警告。8.0m 則硬斷(前後段互通不了)。
#    舊下限 5×11m 是樓梯核改版前留下的保守值,不是幾何極限(2026-07-30 放寬)。
# ⚠️ 4.0~4.5m 面寬的圖合法但很擠(客廳淨寬僅約 3.4m),屬老街屋尺度。
# ⚠️ **下限 3.5 → 4.0m(2026-09-04,使用者:「樓梯只要做折返梯就好」)。**
#    這不是保守值,是量出來的幾何極限。中段核那一排要並排放下:
#        外牆 150 + 廁所 1030 + 折返梯 1600 + 導牆 150 + 走道 865 ≈ 3.8m
#    (廁所 1030 = 一扇門 850 + 兩側牆角淨距 90×2 —— 它只有南北兩面牆開得了門,
#     牆長就是它的寬度;走道 865 = 開得出通道口的最小寬,見 PASSAGE_HARD_MIN)
#    3.8m 剛好排得下但門與動線沒有餘裕(實測三層全出 `circulation_blocked`),
#    4.0m 起每個尺寸都乾淨。**以前 3.5~3.9m 靠的是單跑直梯**:一道梯段省下
#    0.9m,那 0.9m 正好就是這條走道 —— 梯型統一之後那個尺度就排不下了。
#    要救回來得把廁所移出中段核(等於改骨架),不是調參數,見 AGENTS.md。
MIN_WIDTH, MAX_WIDTH = 4000.0, 8000.0
# ⚠️ 上限 7.0 → 8.0m(2026-08-25,使用者:「較大的基地會到 6~8 米」)。
#    放寬本身不需要新骨架 —— 8m 本來就排得下、也不違規;真正的問題是
#    「一段當一間房」會生出 8×5.4m ≈ 43㎡ 的臥室。配套是 `_band_split_x`
#    (前/後段左右切成兩間)+ `_core_widths` 夾住服務格寬度,兩件事一起做
#    才有意義:只放寬會生出一堆 room_oversize,只切不放寬則 8m 仍 raise。
MIN_DEPTH = 9500.0              # 三段(前+核[含起步平台]+後)放得下的下限
# 面寬越寬,後段房間就越「寬而淺」(像走廊),故 6m 以上的面寬要多留一點進深。
WIDE_WIDTH, WIDE_MIN_DEPTH = 6000.0, 10500.0
# ⚠️ 進深**上限**(超過就封頂 + 留前後院,不是報錯):前後段是單面採光,再深也
#    只有臨路/臨後院那一面能開窗,窗開好開滿仍不足 §40 的 1/8 → 深房必然違規。
#
# 這張表是**量出來的**(2026-08-25,使用者:「進深 12~18 米,15 米左右是主流」):
# 每個面寬從最小進深起、每次加 0.5m,3 個 seed × 3 層全部要過 plan_check +
# code_check;再深一級就出 `daylight_area` 的那一級,就是這裡的值。
# 舊值(<4m 給 12.0m、其餘一律 13.5m)比實際能力保守 1~2.5m —— 5m 面寬其實
# 撐得住 14.45m、8m 面寬撐得住 16.45m,主流的 15m 進深因此被無謂地砍掉。
#
# ⚠️ **這是起點估計,不是保證**:只量過這幾個面寬,中間靠內插。真正的答案由
#    `_fit_depth` 當場拿 code_check 量,量不過就收一級進深重蓋(本專案的規矩是
#    「實際量過才算數」)。所以這張表可以樂觀一點,不必抓到最保守。
# ⚠️ 6.45m 那個**凹陷**是量出來的,不是筆誤:前後段在那個尺寸開始切成左右兩間
#    (見 `_band_split_x`),每間能開窗的外牆長度砍半,窗反而更難開滿。
_MAX_DEPTH_POINTS = (
    (3500.0, 12500.0), (3950.0, 13450.0), (4450.0, 14450.0),
    (4950.0, 14950.0), (5450.0, 15450.0), (5950.0, 15450.0),
    (6450.0, 14950.0), (6950.0, 15450.0), (7450.0, 16450.0),
)
# 進深退讓階梯:表估得太樂觀時,一次收這麼多(多的留成院子),最多退這麼多次。
DEPTH_RETREAT_STEP = 500.0
DEPTH_RETREAT_TRIES = 4
# ⚠️ **天井不會讓你蓋得更深**(2026-08-26 實測,別再試一次)。
# 直覺上「中段補一個採光面 → 前後段各自要服務的進深變短」,但實際量出來
# 「有天井」與「無天井」的最深建築進深**一模一樣**(3.5m 面寬都是 12.5m、
# 5.45m 都是 16.0m;浴廁在南在北都試過)。道理:天井只貼著服務格的一側,
# 只服務得到前段**或**後段其中一段,而進深是前後對稱長的 —— 擋住你的永遠是
# 「沒貼到天井的那一段」。要靠天井加深,得做成貫穿整個面寬的**天井帶**
# (就是 2026-07-29 拿掉的那個東西),那會吃掉整整一排樓地板。
#
# ⚠️ 第一次量出來是「一律 +2.0m」,那是**假的**:探測跑的是
#    `generate_narrow_building`,它裡面有進深退讓階梯(最多退 4×500=2000mm)——
#    量到的「輸入 16m 還是乾淨的」其實是退讓救回來的,不是天井的功勞。
#    +2.0m 剛好等於退讓上限就是線索。量這種東西要繞過 `_fit_depth` 直接叫
#    `_build_floor`,量**實際蓋出來的深度**。


def min_depth_for(width: float, garage: bool = False) -> float:
    """該面寬的最小進深(mm)。有車庫時前段要放得下一個車位,下限跟著長。"""
    base = WIDE_MIN_DEPTH if width > WIDE_WIDTH else MIN_DEPTH
    return max(base, sum(z[2] for z in _zones(garage))) if garage else base


def max_depth_for(width: float) -> float:
    """該面寬的最大**建築**進深(mm);基地更深時多的部分留成前後院。

    ⚠️ 參數是**建築**面寬(已經扣掉留柱位的 `STRUCT_MARGIN`),不是基地面寬。
    ⚠️ 開不開天井**不影響**這個上限,見上面 PATIO 那段的實測結論。"""
    pts = _MAX_DEPTH_POINTS
    if width <= pts[0][0]:
        return pts[0][1]
    for (w0, d0), (w1, d1) in zip(pts, pts[1:]):
        if width <= w1:
            return d0 + (d1 - d0) * (width - w0) / (w1 - w0)
    return pts[-1][1]

# 樓梯:U 形折返梯(填滿樓梯間)。中段核只有 ~3.6m 深,單跑直梯爬一層會太陡
# (需 ~4.7m 才緩);折返梯上一段+平台轉身+再上一段,同深度內每階升高正常(~178mm)。
STAIR_TREAD = 260.0
WALL_GAP = 75.0
STAIR_WELL_GAP = 100.0                              # 兩梯段間的梯井縫
# 起步平台:門進到樓梯間之後、踩上第一階之前要站的那塊平地。沒有它,門一開就是
# 踏step——門扇會掃到階梯、人也沒有落腳處,是真實圖面不會出現的錯誤。
ENTRY_LANDING = 900.0
STAIRWELL_W = 2075.0                                # 梯段本身佔的面寬(兩梯段各 ~910)
# 樓梯旁的通道:前後段要互通就得走過樓梯間,沒有這條通道等於要人踩著階梯走過去。
# 寬度要容得下「門(850)+ 兩側牆角淨距 + 導牆」,門才有合法位置可開;950 太緊,
# 合法窗口只剩 ~75mm,常常開不成門 → 只好穿浴室當通道(不合理)。
GUARD_WALL_T = 150.0                                # 梯段側邊「導牆」厚(見下)
PASSAGE_W = 1350.0 + GUARD_WALL_T                   # 通道淨寬 1350 + 導牆
STAIRWELL_TOTAL = STAIRWELL_W + PASSAGE_W           # 樓梯間總面寬(梯段 + 通道)
PASSAGE_MIN = 1000.0
# 建築技術規則施工編 §33:**住宅樓梯寬度 ≥75cm**。這是梯段可以窄到的下限。
# ⚠️ 舊做法是二選一:留得下 1000mm 通道就用舒適寬,否則**梯段填滿整間**。
#    後者等於「這層沒有走道」,而折返梯中間那塊平台在**半層高** —— 前段到
#    後段實際上根本走不過去(使用者 2026-08-27 指著 4.5m 的圖說「沒有路可以
#    到廚房」;60 案掃描 16 案走不通,全部落在 3.6~5.2m 面寬)。
#    實務做法是**把折返梯做窄一點**,先保住走道 —— 那才是真實街屋的樣子。
FLIGHT_MIN_W = 750.0                                # 單一梯段淨寬的法定下限
STAIR_MIN_TOTAL = 2 * FLIGHT_MIN_W + STAIR_WELL_GAP  # 折返梯總寬的下限 1600
STAIR_W_STEP = 25.0                                 # 梯段退讓級距
# 走道淨寬的物理下限。⚠️ 2026-09-04 從 750 改成 865,不是放寬也不是收緊憑感覺 ——
# 走道兩端現在是**開放通道口**(`_open_passage_mouth`,使用者 2026-09-02 要的),
# 而那支開得成的條件是「這一段扣掉牆垛還有一扇內門那麼寬」:
#     INTERIOR_DOOR_WIDTH 850 + PASSAGE_MOUTH_PIER 90 − WALL_GAP 75 = 865
# 走道 750 的時候開口開不出來 → 那一層照樣斷成兩塊(實測 3.8~4.2m 面寬:
# 走道明明有 750,`_has_passage` 全是 False、`circulation_blocked` 三個尺寸全中)。
# **同一件事兩把尺**在本檔已經第七次:量走道的尺與開通道口的尺要對得起來。
# (常數寫在這裡是因為 INTERIOR_DOOR_WIDTH / PASSAGE_MOUTH_PIER 定義在後面;
#  `test_passage_hard_min_matches_the_mouth_rule` 釘住這條算式,免得日後漂掉。)
PASSAGE_HARD_MIN = 865.0
# ❌ 走錯過的路(2026-08-27 加、2026-09-04 依使用者指示整批拿掉):折返梯擠不出
#    走道時改用**單跑直梯**當備案(少一道梯段,面寬立刻省 0.9m)。使用者
#    2026-09-04:「樓梯只要做折返梯就好,其他樓梯幫我移除」——**全產線只有一種
#    梯型**,不再依面寬換梯型。窄面寬改回「把折返梯縮到法定下限來擠走道」
#    (`_flight_width`),擠不出來就沒有走道 —— 那時由 `_fit_core_style` 換一款核。
# 樓梯間要有這麼寬,才擠得出「法定下限的折返梯 + 開得出通道口的走道」。
STAIRWELL_HARD_MIN = (2 * WALL_GAP + STAIR_MIN_TOTAL
                      + GUARD_WALL_T + PASSAGE_HARD_MIN)      # 2765
# 走道還要**開得出一扇門**:門寬 + 兩側牆角淨距 + 一點餘裕。走道只求「走得過去」
# 的話,後段那扇門的合法窗口會只剩幾十 mm,補門機制只好改走浴廁。
# (PASSAGE_DOOR_NEED = 門寬 850 + 2×90 牆角淨距 = 1030,定義在下面,這裡寫值)
STAIRWELL_DOOR_MIN = (2 * WALL_GAP + STAIR_MIN_TOTAL
                      + GUARD_WALL_T + 1030.0 + 200.0)        # 3130
FLOOR_HEIGHT = 3200.0                               # 層高(算級數/每階升高用)
MAX_RISER = 190.0                                   # 每階升高上限(住宅舒適下限步距)
MIN_TREAD = 210.0                                   # 踏面下限(建築技術規則:住宅 ≥21cm)
TURN_LANDING_MIN = 700.0                            # 折返端平台最小深(轉身用)

# 管道間(給排水/電氣豎管)尺寸:真實住宅常見 寬 40~80cm、深 40~60cm(使用者
# 2026-07-29 定調)。⚠️ **窄透天不放管道間**(使用者 2026-07-29 決定:5~7m 面寬
# 的房子多切一根管道柱,只會在西牆邊留一條 80cm×3.2m 的長條收納,礙眼又佔地);
# 這組常數是給 graph_layout(AI 版)的核用的,尺寸定義放這裡當單一來源。
# 取各自的上限(80×60cm):管道帶剩下的長度做成壁櫃,寬 60cm 扣掉兩側牆只剩 48cm
# 淨寬、人站不進去;深 60cm 則是讓「管道帶北側那道牆」離樓梯夠遠,門開得下
#(500 深時門前站人的空間會踩到折返平台)。
SHAFT_W, SHAFT_D = 800.0, 600.0

# 中段核服務格(浴廁):面寬吃剩的都給它,但太寬就往淺切(免得變一間大空房)。
BATH_MIN_W, BATH_MAX_W = 1500.0, 2400.0
# 浴廁還擠得下的**極限**寬:淨寬 1050,放得下 900 的淋浴間與馬桶洗手台。
# ⚠️ 只在「不讓寬就沒有走道」時才退到這裡 —— 4~4.5m 面寬的街屋,浴廁窄 20cm
#    換一條走得通的走道,划得來;走不通的樓層是廢圖,窄一點的浴廁只是難用。
BATH_TIGHT_W = 1200.0
#: 最後一級:只放得下一個馬桶的窄廁所。3.8~4.0m 面寬配折返梯時,浴廁不退到
#: 這裡就擠不出走道 —— 那一層會走不通,而一間窄廁所遠勝過走不通的一層樓。
#: ⚠️ 下限不是「馬桶區 80cm」(書上 Space 6 的**設備**尺寸),是**開得出一扇門**:
#:    這間浴廁只有南北兩面牆開得了門(東面是梯段、西面是共同壁),牆長就是它的
#:    寬度 → 850 的門 + 兩側牆角淨距 90×2 = 1030。訂 800 的話門根本開不出來,
#:    實測 3.5/3.6m 直接 `room_no_door` + `floor_split`(走錯過的路,別再訂小)。
#:    (= INTERIOR_DOOR_WIDTH 850 + 2×DOOR_CORNER_MIN 90;兩個常數都定義在後面,
#:    所以這裡寫值,由 `test_ultra_narrow_wc_can_take_a_door` 釘住算式。)
BATH_WC_W = 1030.0
BATH_AREA_MAX = 6.0e6           # 浴廁面積上限(mm²)
BATH_MIN_D, BATH_MAX_D = 1800.0, 2600.0
STORE_MIN_D = 1200.0            # 服務格上段(儲藏)最小進深

ENTRY_WIDTH = 1000.0            # 臨路大門寬
GARAGE_DOOR_W = 3000.0          # 捲門淨寬(常見值)
GARAGE_DOOR_MIN_W = 2500.0      # 捲門最窄:等於法定車位寬,再窄車開不進去
INTERIOR_DOOR_WIDTH = 850.0     # 補內門寬(對齊 bsp_layout.DOOR_WIDTH)
# 補門保證的例外:機電豎管、採光天井本來就不設走入門(封閉服務豎井)。
NO_DOOR_KINDS = {"pipe_shaft", "patio"}
# 補門時偏好接的鄰室(越公共/動線越優先);越前面優先度越高。
_DOOR_NEIGHBOR_PREF = ("corridor", "foyer", "stair_hall", "living", "dining",
                       "kitchen", "stair")

# 三段進深:(段名, 分配權重, 最小進深mm)。前室 / 中段核 / 後室。
# ⚠️ 中段核權重 0:核只要放得下樓梯就夠(4.3m),多出來的進深一律給前後居室
#    ——天井取消後,核裡是衛浴/儲藏,再深只會變成一間深而無用的大空房。
ZONES = [
    ("front", 0.5, 3300.0),
    # 核最小深 = 牆縫 150 + 起步平台 900 + 梯跑 9×260 + 折返平台 ≥梯段寬(§33)
    ("core", 0.0, 4400.0),
    ("rear", 0.5, 3200.0),
]


# ── 1F 車庫(使用者 2026-08-26:「做出有車庫的」——參考平面圖的有車庫版)────────
# 車庫**不另外插第四帶**,而是把 1F 的**前段整段**拿去停車。插第四帶會把中段核
# 往北推,樓上的核卻沒跟著推 → 樓梯上下對不齊,那是這條產線的根本要求。改成
# 「前段的下限換成一個車位長」之後,每層的核位置完全一樣,樓梯照常對齊。
#
# ⚠️ 代價是 **1F 沒有客廳**:客廳往上挪到 2F 前段(台灣透天最常見的做法,而且
#    2F 前段本來就有前陽台可接)。**不把客廳塞在車庫北邊是故意的** —— 那間房
#    南邊是車庫、北邊是中段核,一面外牆都沒有,§40 採光直接違規;真實圖面也
#    不會這樣配。連帶:一層樓的透天不能配車庫(會連客廳都沒有)。
def garage_min_depth() -> float:
    """車庫最小進深(mm)= 法定停車位長。

    ⚠️ 車位尺寸的單一出處是 `layout_generator.CAR_STALL`(2.5×5.5m),不要在
    這裡另抄一份數字(本檔「同一件事、兩個地方兩把尺」已經踩過四次)。
    延遲 import:layout_generator 反過來會用到本模組。"""
    from src.design.layout_generator import CAR_STALL
    return float(CAR_STALL[1])


def _zones(garage: bool = False):
    """三段的 (段名, 權重, 最小進深)。有車庫時前段的下限換成一個車位長。"""
    if not garage:
        return ZONES
    return [("front", ZONES[0][1], max(ZONES[0][2], garage_min_depth())),
            ZONES[1], ZONES[2]]


# ── 設計變體:同一套規則的合法選擇,抽樣後仍要過 plan_check / code_check ──────
@dataclass(frozen=True)
class NarrowVariant:
    """一棟窄透天的設計選擇(不是亂數擾動,每個欄位都是設計師會做的決定)。

    mirror      東西鏡射:樓梯核換到另一邊(整層左右翻)
    bath_north  中段核服務格:浴廁在北(儲藏在南),預設浴廁在南
    open_kitchen 1F 後段做**開放餐廚**(一間)還是隔成餐廳|廚房兩間。
                真實設計師本來就在這兩者間選;參考平面圖畫的是開放餐廚,
                所以預設開放。⚠️ 後段大到連「餐廚」的併合上限都撐不住時,
                不管這個開關一律切兩間(否則會生出 44㎡ 的巨型餐廚)。
                ⚠️ 它**取代**了舊的 `rear_swap`(廚房/餐廳左右對調):併成一間
                之後對調沒有任何作用,方案多樣性因此掉到 9%(要 >10%)。
                「開放 vs 分間」是房間**種類**層級的差別,看得出來得多。
    entry_frac  臨路大門在南牆的位置比例(0.22 偏西 / 0.5 置中 / 0.78 偏東)
    """

    mirror: bool = False
    bath_north: bool = False
    open_kitchen: bool = True
    entry_frac: float = 0.22


DEFAULT_VARIANT = NarrowVariant()
_ENTRY_FRACS = (0.22, 0.5, 0.78)


def variant_from_seed(seed: int) -> NarrowVariant:
    """種子 → 設計變體(同種子同結果,方便重現與比較)。"""
    rng = random.Random(seed)
    return NarrowVariant(mirror=rng.random() < 0.5,
                         bath_north=rng.random() < 0.5,
                         open_kitchen=rng.random() < 0.5,
                         entry_frac=rng.choice(_ENTRY_FRACS))


def all_variants() -> list:
    """所有變體組合(24 種),給「挑差最多的 N 個方案」用。"""
    return [NarrowVariant(m, b, o, f)
            for m in (False, True) for b in (False, True)
            for o in (False, True) for f in _ENTRY_FRACS]


def _split_depth(total_d: float, zones) -> list[float]:
    mins = [z[2] for z in zones]
    extra = max(0.0, total_d - sum(mins))
    wsum = sum(z[1] for z in zones) or 1.0
    return [m + extra * z[1] / wsum for z, m in zip(zones, mins)]


def _rect(x0, y0, x1, y1):
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


def _hall_of(stair_w: float) -> float:
    """樓梯間寬 → 梯段旁那條走道的**淨寬**(梯段取法定最窄的折返梯)。

    `_core_widths` 要拿它判「浴廁該不該再退一級」,而 `_passage_span` 拿實際的
    梯段寬算同一件事 —— 兩邊算的是同一條走道,式子寫在這裡一處
    (本檔「同一件事兩把尺」已經踩過六次)。"""
    return stair_w - 2 * WALL_GAP - STAIR_MIN_TOTAL - GUARD_WALL_T


def _core_widths(W: float, min_service: bool = False,
                 patio: bool = False):
    """中段核面寬分配 → (服務格, 樓梯間)。天井取消後,核只剩這兩格。

    樓梯間要 STAIRWELL_TOTAL(梯段+側邊通道)才走得通,但不能把服務格擠到放不下
    衛浴;面寬不夠時優先保住衛浴的最小寬,樓梯間縮到仍留得下通道的下限。

    ⚠️ **面寬寬的時候要反過來夾**:服務格拿「面寬減掉樓梯間」的全部剩餘,
    8m 面寬就變成 4.4m 寬的服務格 —— 浴廁根本用不到那麼寬(BATH_MAX_W 2400),
    多的會被 `_core` 當成空格丟給隔壁居室,把那間房撐得更大。剩下的寬度給
    樓梯間(=樓梯旁通道變寬),前後段才有位置從中間切成兩間(見 `_band_split_x`:
    西半間的門只能往北開進樓梯間,樓梯間西牆越往西,切點才能越靠中間)。

    ⚠️ 還有一段**比 BATH_MIN_W 更狠的退讓**:樓梯間窄到擠不出走道時,浴廁退到
    `BATH_TIGHT_W`(1200)。前後段走不通的樓層是廢圖,浴廁窄 20cm 只是難用一點 ——
    4~4.5m 面寬的真實街屋就是這個尺寸。

    ⚠️ 2026-09-04「樓梯只做折返梯」之後**再多一級** `BATH_WC_W`(1030):折返梯
    比原本備用的單跑直梯寬 0.9m,4.0~4.2m 面寬連「浴廁 1200 + 折返梯 1600 +
    導牆 + 走道」都排不下。退到 1030 是**這間浴廁還開得出一扇門**的寬度,那正是
    老街屋只放一個馬桶的窄廁所 —— 一間窄廁所仍然遠勝過一層走不通的樓。
    ⚠️ 這一級**只在走道真的生不出來時**才用:判準是「退到 BATH_TIGHT_W 之後
    走道還是 <PASSAGE_HARD_MIN」,不是面寬幾米(寫成尺寸就會有一天對不上)。

    min_service:把服務格再壓到浴廁的**最小**寬。6m 級的面寬服務格只有 1.875m
    (還沒碰到 BATH_MAX_W,上面那段夾不到它),但樓梯間也因此不夠往西,前段切不
    成兩間 → 生出 28㎡ 的臥室。浴廁窄 37cm 換一間大小正常的房間,划得來 ——
    由呼叫端在「真的切不動、而且房間會過大」時才開。"""
    sw = min(STAIRWELL_TOTAL, W - BATH_MIN_W)
    # 走道貼側牆(見 _core)之後,後段那扇門的合法窗口是「走道寬 − 門寬 − 牆角
    # 淨距」;走道剛好 975 時窗口只剩 35mm,實測 5/40 案開不成 → 只好穿過浴廁
    # 才到得了餐廚。所以門檻從「擠得出 750 走道」放寬成「走道要夠開一扇門」。
    if sw < STAIRWELL_DOOR_MIN and not patio:
        # 走道擠不出來 → 浴廁讓寬(退到 BATH_TIGHT_W)。
        # ⚠️ 開天井時不讓:天井就切在服務格裡浴廁沒用完的那一塊,服務格一窄
        #    天井就小到 code_check 不認(§41 採光不算),等於天井白開。
        #    天井預設是關的,要開的人是拿「走道」換「浴廁樓梯有自然光」。
        sw = max(sw, min(STAIRWELL_HARD_MIN, W - BATH_TIGHT_W))
        if _hall_of(sw) < PASSAGE_HARD_MIN:         # 連走得過去都擠不出來
            sw = max(sw, min(STAIRWELL_HARD_MIN, W - BATH_WC_W))
    svc = W - sw
    if svc > BATH_MAX_W:                # 面寬寬 → 服務格只留浴廁該有的寬
        svc, sw = BATH_MAX_W, W - BATH_MAX_W
    if min_service and svc > BATH_MIN_W:
        svc, sw = BATH_MIN_W, W - BATH_MIN_W
    return svc, sw


def _passage_span(bx0: float, bx1: float, min_service: bool = False,
                  patio: bool = False,
                  core_style: str = "default", y1: float = 0.0,
                  y2: float = 0.0) -> tuple | None:
    """樓梯旁那條走道在 x 上的範圍(未鏡射座標);梯段填滿樓梯間時回 None。

    梯段貼**西**牆(緊鄰服務格,見 `_stair(hug="west")`),所以走道在梯段東側:
    從梯段東面(含導牆)到建築東界牆 bx1 —— 走道貼著界牆,不夾在樓梯與浴廁中間。
    ⚠️ 只用面寬推,不必等 `_core` 把樓梯造出來 —— 切點(`_splits`)是在那之前
    決定的,而切點正是需要閃開這條走道的人。"""
    if core_style == "mid":
        plan = _mid_core_plan(bx0, bx1, y1, y2)
        return None if plan is None else (plan[0], bx1)
    if core_style == "zone3":
        plan = _zone3_core_plan(bx0, bx1, y1, y2)
        return None if plan is None else (plan[0], bx1)
    if core_style == "ref":
        # 參考圖版:走道是核裡固定的一條(貼界牆、跑滿核的進深),寬度與樓梯
        # 是同一個決定 —— 一律問 `_ref_core_plan`,不要在這裡另外算一次。
        plan = _ref_core_plan(bx0, bx1, y1, y2)
        return None if plan is None else (plan[0], bx1)
    svc, _sw = _core_widths(bx1 - bx0, min_service, patio)
    xs = bx0 + svc
    span = (bx1 - xs) - 2 * WALL_GAP
    fw = _flight_width(span)                          # 與 _stair 同一個決定
    # 梯段貼**西**牆(緊鄰服務格),走道因此落在東側 —— 貼著建築的東界牆。
    # ⚠️ 走道要貼牆,不能夾在樓梯與浴廁中間(使用者 2026-08-27:「正常的走道
    #    應該靠在旁邊的牆上」;參考平面圖畫的就是 廁所|樓梯|走道 貼牆那條)。
    east = xs + WALL_GAP + fw + GUARD_WALL_T
    return (east, bx1) if bx1 - east >= PASSAGE_HARD_MIN else None


#: 一扇門在走道上要佔多少牆:門寬 + 兩側最起碼的牆角淨距
#: (DOOR_CORNER_MIN=90 定義在下面,這裡直接寫值避免前向參照)。
PASSAGE_DOOR_NEED = INTERIOR_DOOR_WIDTH + 2 * 90.0
#: 再加這麼多餘裕,門才有真的挑得動的位置(剛好 1030 的話合法窗口是 0)。
PASSAGE_DOOR_MARGIN = 250.0


def _passage_split_ok(xm: float, span) -> bool:
    """這個切點對走道口可不可以?

    後段那兩間房的門**都只能開在走道那一段牆上**(牆的其餘部分不是浴廁就是
    折返平台 —— 半層高、開不了門)。所以切點只有兩種是好的:

      ① 落在走道**外面** → 走道整條給其中一間(另一間走姊妹房的內門,
         例如 1F 的餐廳|廚房);
      ② 落在走道**裡面,但兩側各留得下一扇門** → 兩間各開各的。

    卡在中間(走道被切成兩段、哪一段都塞不下一扇帶牆角淨距的門)才是壞的 ——
    那時補門機制只好改走浴廁或隔壁臥室,動線變成「穿過廁所才到得了餐廳」。"""
    if span is None:
        return True                       # 沒有走道 → 這條限制不適用
    lo, hi = span
    if xm <= lo + 1e-6 or xm >= hi - 1e-6:
        return True
    return (xm - lo >= PASSAGE_DOOR_NEED) and (hi - xm >= PASSAGE_DOOR_NEED)


def _split_clears_passage(xm: float, span) -> bool:
    return _passage_split_ok(xm, span)


def _fit_flight_width(span: float, targets=None) -> float:
    """梯段總寬的**退讓階梯**:從舒適寬一路縮到法定下限,取還留得下走道的最寬那級。

    ⚠️ 這條路徑的舊寫法是二選一 ——「留得下 1000mm 通道就用舒適寬,否則梯段填滿
    整間」。填滿整間的意思是**這層沒有走道**:前段到後段只能踩過樓梯,而折返梯
    中間那塊平台在**半層高**(9 階 × 188 ≈ 1.69m),門開在那一段等於開進一個
    1.1m 深、頭頂 1.69m 的凹洞,人根本走不過去。實務上遇到這種面寬是**把折返梯
    做窄一點**(住宅法定下限 75cm/段),先保住走道。

    ⚠️ 判準用「淨走道」= span − 梯段 − 導牆:導牆(`_add_stair_guard_walls`)佔的
    150mm 不是人走得到的地方,算進去會高估一個門的一半寬。
    """
    comfy = STAIRWELL_W - 2 * WALL_GAP
    for target in (targets or (PASSAGE_MIN, PASSAGE_HARD_MIN)):
        w = comfy
        while w >= STAIR_MIN_TOTAL - 1e-6:
            if span - w - GUARD_WALL_T >= target:
                return w
            w -= STAIR_W_STEP
    return span             # 連最窄的走道都擠不出來 → 梯段填滿整間(舊行為)


def _flight_width(span: float) -> float:
    """折返梯的梯段總寬(單一出處)。**走道優先**,由寬往窄退三級目標:

      ① 走道**開得出一扇門**(門寬 + 牆角淨距 + 餘裕)—— 後段那扇門才挑得到位置
      ② 走道好走(`PASSAGE_MIN`)
      ③ 走道**走得過去**(一扇門的寬度)

    三級都擠不出來時 `_fit_flight_width` 回 span = 梯段填滿整間 = **這層沒有
    走道**。那不是好結果(前後段只能踩過折返平台,而平台在半層高),但它是
    **幾何上唯一的答案** —— 最窄的那批面寬,核帶連法定下限的折返梯 1600 配上
    最窄的浴廁都擠不出一條開得出通道口的走道。真正救得動的是**換一款核**(`_fit_core_style` 會拿
    plan_check 實際量過再決定),不是換梯型(使用者 2026-09-04:只做折返梯)。
    """
    return _fit_flight_width(span, (PASSAGE_DOOR_NEED + PASSAGE_DOOR_MARGIN,
                                    PASSAGE_MIN, PASSAGE_HARD_MIN))


def _stair(x_west, x_east, y1, y2, label, hug: str = "east"):
    """樓梯間內 U 形折返梯(往北上),**南側(門側)留起步平台**。

    級數由層高回推、每階升高 ≤MAX_RISER(住宅舒適);分兩段折返,故梯跑只需一半深度。
    ⚠️ 樓梯不是從門邊就開始踩:南端讓出 ENTRY_LANDING 當起步平台,門開進來先站平地
    再上階(否則門扇掃到踏step、人也沒落腳處)。平台不夠深時自動縮短踏面(仍 ≥法定
    210mm)把空間讓出來。"""
    total = max(4, -(-int(FLOOR_HEIGHT) // int(MAX_RISER)))      # ceil,爬完一層的總級數
    spf = max(2, -(-total // 2))                                 # 每段級數(向上取半)
    # 梯段靠**東側**擺,西側留通道(人不必踩階梯走過去);通道與梯段之間補一道導牆
    # (_add_stair_guard_walls),梯段兩側才都靠著牆。剩下的寬度不夠當通道時,梯段
    # 直接填滿整間 —— 寧可梯段寬一點,也不要有一側是空的。
    span = (x_east - x_west) - 2 * WALL_GAP
    flight_w = _flight_width(span)
    # 建築技術規則施工編 §33:**平臺深度不得小於樓梯(梯段)寬度**。梯段越寬,
    # 折返端的平台就要越深;不夠時先縮踏面(仍 ≥法定 210),再不夠就是樓梯間太淺。
    one_flight = (flight_w - STAIR_WELL_GAP) / 2.0
    need_turn = max(TURN_LANDING_MIN, one_flight)
    usable = (y2 - y1) - 2 * WALL_GAP
    landing = min(ENTRY_LANDING, max(0.0, usable - spf * MIN_TREAD - need_turn))
    run_len = usable - landing                                   # 梯跑+折返平台的長度
    tread = STAIR_TREAD
    if spf * tread + need_turn > run_len:                        # 太擠 → 縮踏面(≥法定)
        tread = max(MIN_TREAD, (run_len - need_turn) / spf)
    # ⚠️ 折返平台**該多深就多深**,不要把剩下的長度全吃掉。多吃的那一截是
    #    畫在圖上的**半層高**平台(不是地板),樓梯間就白白少了那麼多可走的
    #    地。實測淺透天有一案平台被拉到 3.3m 深(真正需要 0.75m)。
    run_len = min(run_len, spf * tread + need_turn)
    fx = (x_west + WALL_GAP if hug == "west"
          else x_east - WALL_GAP - flight_w)
    return UStair(origin=(fx, y1 + WALL_GAP + landing),
                  width=flight_w,
                  length=run_len, direction="north",
                  steps_per_flight=spf, tread=tread,
                  well_gap=STAIR_WELL_GAP, label=label)


def _patio_ok(rect) -> bool:
    """這塊空格夠不夠格當天井(太小的天井採不到光,`code_check` 也不認)。"""
    from src.design.layout.code_check import PATIO_MIN_AREA_M2, PATIO_MIN_SIDE

    x0, y0, x1, y1 = rect
    return (min(x1 - x0, y1 - y0) >= PATIO_MIN_SIDE
            and (x1 - x0) * (y1 - y0) / 1.0e6 >= PATIO_MIN_AREA_M2)


# ── 參考平面圖版的中段核(使用者 2026-08-28 給的「方案 B」)──────────────────
#: 走道的候選寬度:由舒適往下退,取「樓梯還排得下」的最寬那一級。
REF_PASSAGE_WIDTHS = tuple(float(v) for v in range(1300, 1105, -25))


def _ref_stair(x_west, x_east, y1, max_d, label):
    """**橫置**樓梯:梯跑沿著面寬跑,只吃掉核的一小段進深。

    回 (樓梯, 佔掉的進深);排不下回 (None, 0.0)。

    ⚠️ 起步端在**東**(貼著走道那一側),所以 `direction="west"` —— 人沿著走道
    走到樓梯前,轉身往內上樓。走道本身就是起步平台,不必另外留。

    做法是把折返梯**轉 90 度**(淺基地產線用的同一招),梯跑只要一半。排不下
    回 None。⚠️ 參考平面圖上畫的其實是一道橫過面寬的**單跑直梯**,以前也是先
    試它 —— 使用者 2026-09-04 指示「樓梯只要做折返梯」,那一段已拿掉;差別是
    橫置折返梯比較寬(吃掉的進深多一點),窄面寬因此更容易退回 `mid` 核。
    """
    run = (x_east - x_west) - 2 * WALL_GAP
    steps = max(4, -(-int(FLOOR_HEIGHT) // int(MAX_RISER)))
    spf = max(2, -(-steps // 2))
    top = min(STAIRWELL_W, max_d - 2 * WALL_GAP)
    w = top - (top % STAIR_W_STEP)
    while w >= STAIR_MIN_TOTAL:
        # §33:折返端平台深度不得小於梯段寬 —— 梯段越寬,平台吃掉的梯跑越多。
        turn = max(TURN_LANDING_MIN, (w - STAIR_WELL_GAP) / 2.0)
        tread = min(STAIR_TREAD, (run - turn) / spf)
        if tread >= MIN_TREAD:
            return (UStair(origin=(x_west + WALL_GAP, y1 + WALL_GAP),
                           width=w, length=spf * tread + turn,
                           direction="west", steps_per_flight=spf, tread=tread,
                           well_gap=STAIR_WELL_GAP, label=label),
                    w + 2 * WALL_GAP)
        w -= STAIR_W_STEP
    return None, 0.0


def _ref_core_plan(bx0, bx1, y1, y2, label="上"):
    """參考圖版核的幾何決定 → (走道西緣 xp, 樓梯, 樓梯佔的進深);排不下回 None。

    ⚠️ 走道寬與樓梯是**同一個決定**:走道越寬,橫置梯跑得到的長度就越短。所以
    兩件事一起試,取「樓梯排得下」的最寬走道 —— 切點(`_splits`)也要問同一支,
    才不會兩邊各算一次而算出不同的答案(本檔「同一件事兩把尺」已經踩過五次)。

    ⚠️ 走道**加寬到放得下兩扇門**(讓前後段切得成兩間)這條路試過了,不要再試:
    6~8m 面寬的 12 個尺寸實測 `room_oversize` 26 → 25 件,幾乎沒有用,而且有兩
    個尺寸反而**少了一間房**(走道吃掉的面積比切出來的還多)。原因是幾何上兜
    不攏:走道貼著東界牆,西半那間房**碰不到走道**,門開不出去 —— 要兩間都開得
    了門,得把走道整條延伸進前後段(那是另一種骨架,不是加寬走道)。
    """
    for pw in REF_PASSAGE_WIDTHS:
        xp = bx1 - pw
        st, ds = _ref_stair(bx0, xp, y1, (y2 - y1) - BATH_MIN_D, label)
        if st is not None:
            return xp, st, ds
    return None


# ── 服務格在中間的核(使用者 2026-08-28:「還想做一款廁所門是開向走道的」)────
#: 樓梯間的候選寬度(梯段本身,旁邊不留走道):折返梯由舒適往下退到法定下限。
#: ⚠️ 以前尾巴還接著一段更窄的單跑直梯寬度,使用者 2026-09-04 指示拿掉 ——
#:    退不到 1600 就回 None,由 `_fit_core_style` 退回別款核。
MID_FLIGHT_WIDTHS = tuple(float(v) for v in
                          range(int(STAIRWELL_W), int(STAIR_MIN_TOTAL) - 1,
                                -int(STAIR_W_STEP)))
#: 走道至少要開得出一扇門(與預設核同一把尺)。
MID_PASSAGE_MIN = PASSAGE_DOOR_NEED + PASSAGE_DOOR_MARGIN


def _mid_stair(x_west, x_east, y1, y2, label):
    """服務格在中間版的樓梯:梯段**填滿**樓梯間,旁邊不留走道。

    走道在浴廁的另一邊,所以這座樓梯不必自己讓出通道 —— 這正是「廁所夾在樓梯與
    走道之間」買到的東西:同樣的面寬,梯段可以用得比較寬。

    梯段窄到擺不下兩段(<1600)就回 None,由呼叫端退回預設核。"""
    span = (x_east - x_west) - 2 * WALL_GAP
    usable = (y2 - y1) - 2 * WALL_GAP
    steps = max(4, -(-int(FLOOR_HEIGHT) // int(MAX_RISER)))
    if span >= STAIR_MIN_TOTAL:
        spf = max(2, -(-steps // 2))
        # §33:折返端平台深度不得小於梯段寬。
        need_turn = max(TURN_LANDING_MIN, (span - STAIR_WELL_GAP) / 2.0)
        landing = min(ENTRY_LANDING,
                      max(0.0, usable - spf * MIN_TREAD - need_turn))
        run_len = usable - landing
        tread = STAIR_TREAD
        if spf * tread + need_turn > run_len:
            tread = max(MIN_TREAD, (run_len - need_turn) / spf)
        if spf * tread + need_turn <= run_len + 1e-6:
            return UStair(origin=(x_west + WALL_GAP, y1 + WALL_GAP + landing),
                          width=span, length=spf * tread + need_turn,
                          direction="north", steps_per_flight=spf, tread=tread,
                          well_gap=STAIR_WELL_GAP, label=label)
    return None


def _mid_core_plan(bx0, bx1, y1, y2, label="上"):
    """服務格在中間的核 → (走道西緣 xp, 服務格西緣 xs, 樓梯);排不下回 None。

    面寬由西往東分成三段:樓梯間 | 浴廁 | 走道。樓梯先拿(由寬往窄退),走道拿
    「開得出一扇門」的下限,剩下的給浴廁(封頂 `BATH_MAX_W`,再多的還給走道 ——
    寬面寬的案子走道會變成一小塊梯廳,前後段因此還切得成兩間)。"""
    W = bx1 - bx0
    for fw in MID_FLIGHT_WIDTHS:
        stair_w = fw + 2 * WALL_GAP
        rest = W - stair_w
        if rest < MID_PASSAGE_MIN + BATH_TIGHT_W:
            continue
        bath_w = min(BATH_MAX_W, rest - MID_PASSAGE_MIN)
        xs = bx0 + stair_w
        st = _mid_stair(bx0, xs, y1, y2, label)
        if st is not None:
            return xs + bath_w, xs, st
    return None


def _core_mid(bx0, bx1, y1, y2, label, bath_name):
    """服務格在中間的中段核 → (房間清單, 樓梯, 空格);排不下回 None。

        ┌──────────────┬────────┬──────┐  ← 北(接後段)
        │              │  浴廁  │  走  │
        │    樓梯       ├────────┤  道  │
        │  (順著進深)   │        │      │
        └──────────────┴────────┴──────┘  ← 南(接前段)

    使用者 2026-08-28:「還想做一款廁所門是開向走道的」—— 指的是**預設核**那一版
    (樓梯順著進深跑)。預設核是「浴廁|樓梯|走道」,廁所被樓梯隔開,門只開得了
    南北兩面(1F 開向餐廚、車庫版開向車庫);把服務格搬到樓梯與走道**中間**,
    廁所的東牆就直接貼著走道。

    ⚠️ **浴廁放北邊**是必要的,不是選擇:南邊那一段要留給樓梯的**起步平台** ——
       人從前段進來要先站得到平地才上得了樓,而起步端在南。浴廁放南邊的話,
       前段就只剩走道那 1.3m 進得來,樓梯反而接不上。
    ⚠️ 這個核**不放天井**:三段已經把面寬用完,再切一塊就沒有一段夠寬。要天井
       請用 `core_style="ref"`(那一版把樓梯橫置,面寬才騰得出第四段)。
    """
    plan = _mid_core_plan(bx0, bx1, y1, y2, label)
    if plan is None:
        return None
    xp, xs, stair = plan
    d = y2 - y1
    bath_w = xp - xs
    bath_d = min(max(BATH_AREA_MAX / bath_w, BATH_MIN_D), BATH_MAX_D, d)
    rooms = [("bathroom", bath_name, _rect(xs, y2 - bath_d, xp, y2)),
             ("stair_hall", "樓梯間",
              [(bx0, y1), (bx1, y1), (bx1, y2), (xp, y2),
               (xp, y2 - bath_d), (xs, y2 - bath_d), (xs, y2), (bx0, y2)])]
    return rooms, stair, None


def _core_ref(bx0, bx1, y1, y2, label, bath_name, patio=True):
    """參考平面圖版的中段核 → (房間清單, 樓梯, 空格);排不下回 None。

        ┌──────────────┬────────┬──────┐  ← 北(接後段)
        │     天井      │  廁所  │  走  │
        ├──────────────┴────────┤  道  │
        │      樓梯(橫置)        │      │
        └───────────────────────┴──────┘  ← 南(接前段)

    跟預設核的三個差別(都是使用者 2026-08-28 指著方案 B 要的):

      ① **廁所貼著走道** —— 預設核是「浴廁|樓梯|走道」,廁所被樓梯隔開,門只好
         開向餐廚(車庫版是開向車庫);這裡廁所的門直接開在走道上。
      ② **天井**回來,貼著另一側界牆、與廁所並排(街屋中段唯一的採光來源)。
      ③ **樓梯橫置**,只吃掉核的一小段進深,不是整條。

    ⚠️ 走道是這個核**唯一**的動線:前後段的門都只能開在走道那一段牆上(其餘不是
       樓梯就是廁所/天井)。所以走道寬的判準跟預設核一樣是「開得出一扇門」。
    ⚠️ 天井放不下時**不硬塞**:剩下那塊當空格併給後段(與預設核的 spare 同一套)。
    """
    from src.design.layout.code_check import PATIO_MIN_SIDE

    plan = _ref_core_plan(bx0, bx1, y1, y2, label)
    if plan is None:
        return None
    xp, stair, ds = plan
    ys = y1 + ds
    row_d = y2 - ys
    avail = xp - bx0
    rooms, spare = [], None
    bath_w = min(BATH_MAX_W, BATH_AREA_MAX / row_d, avail - PATIO_MIN_SIDE)
    # ⚠️ 廁所的下限用 `BATH_TIGHT_W`(1200)不是 BATH_MIN_W:4~4.5m 面寬的真實
    #    街屋廁所就是這個尺寸,而天井是這個核的重點,不該為了寬 30cm 的廁所犧牲。
    if patio and bath_w >= BATH_TIGHT_W and _patio_ok((bx0, ys, xp - bath_w, y2)):
        rooms.append(("patio", "天井", _rect(bx0, ys, xp - bath_w, y2)))
    else:
        bath_w = min(avail, BATH_MAX_W, BATH_AREA_MAX / row_d)
        if avail - bath_w > 1.0:                # 天井放不下 → 剩下的當空格
            spare = (bx0, ys, xp - bath_w, y2)
    rooms.append(("bathroom", bath_name, _rect(xp - bath_w, ys, xp, y2)))
    rooms.append(("stair_hall", "樓梯間",
                  [(bx0, y1), (bx1, y1), (bx1, y2), (xp, y2), (xp, ys),
                   (bx0, ys)]))
    return rooms, stair, spare


# ── 三區版的中段核(使用者 2026-08-31:「主要分成三個區域…照著這個格式切」)──
#: 走道的候選寬度(由舒適往下退)。與參考圖版共用同一組 —— 走道要寬到開得出
#: 一扇門,這件事跟核的款式無關。
ZONE3_PASSAGE_WIDTHS = REF_PASSAGE_WIDTHS
#: 樓梯北邊那條的最小進深:樓上要當浴室,所以下限跟浴廁同一把尺。
ZONE3_STRIP_MIN_D = BATH_MIN_D
#: 1F 的儲藏室用不完、要讓給餐廚的那一塊至少這麼寬,否則細到不成為房間的一部分。
ZONE3_SPARE_MIN_W = 900.0


def _zone3_core_plan(bx0, bx1, y1, y2, label="上"):
    """三區版核的幾何 → (走道西緣 xp, 樓梯, 樓梯佔的進深);排不下回 None。

        ┌────────────────┬──────┐  ← 北(接第三區:廚房)
        │  儲藏室(1F)      │  走  │
        │  /浴室(2F 以上)  │  道  │
        ├────────────────┤      │
        │   樓梯(橫置)     │      │
        └────────────────┴──────┘  ← 南(接第一區:客廳)

    ⚠️ **這個排法是使用者自己在 AutoCAD 上畫出來的**(2026-09-01,他把產線畫的
    1F 改過再丟回來)。量到的三件事:樓梯從「順著進深」改成**橫置**、只吃掉核
    的 1928mm 進深、北邊空出來那條 2415mm 是**儲藏空間**。他同一天稍早說過
    「樓梯基本上都是這樣擺設的」(那時看的是順著進深的版本)—— **以圖為準**。

    橫置樓梯直接借參考圖版的 `_ref_stair`(起步端在東、接著走道;面寬夠長用
    單跑直梯,不夠就把折返梯轉 90 度)。與 `_ref_core_plan` 的差別只有北邊
    那條放什麼:參考圖版放「天井 | 廁所」,這一版整條放一間房。

    ⚠️ 走道寬與樓梯是**同一個決定**(走道越寬,橫置梯跑得到的長度越短),所以
    兩件事一起試 —— 與 `_ref_core_plan` 同一個理由,不要拆成兩處各算一次。
    """
    for pw in ZONE3_PASSAGE_WIDTHS:
        xp = bx1 - pw
        st, ds = _ref_stair(bx0, xp, y1, (y2 - y1) - ZONE3_STRIP_MIN_D, label)
        if st is not None:
            return xp, st, ds
    return None


#: 樓梯下那間廁所的目標淨尺寸(mm,寬 × 深):一個馬桶 + 洗手台 + 站得下人。
UNDER_STAIR_WC = (900.0, 1500.0)
#: 小於這個就不畫 —— 一間放不進馬桶的廁所畫出來只會誤導師傅。
#: ⚠️ 寬的下限就是**梯段的法定最小寬**(750):梯段有多寬,它底下那條就有多寬,
#:    再窄的樓梯本來就不存在。實測 4.0m 面寬的橫置折返梯剛好是這個值 ——
#:    下限訂 850 的話那批面寬會**一間廁所都畫不出來**,而那正是使用者要的東西。
UNDER_STAIR_WC_MIN = (750.0, 1300.0)


def _under_stair_wc(stair):
    """樓梯下那間廁所的矩形 → (x0,y0,x1,y1);塞不下回 None。

    使用者 2026-09-02:「你就正常化廁所,只是用虛線畫出來,而且畫在樓梯裡」——
    所以它是**正常尺寸的廁所**,只是位置疊在樓梯底下、圖上用虛線表示
    (畫在 `apartment_plan.draw_under_stair_wc`,不進 `spec.rooms`)。

    ⚠️ **位置由「哪裡有頭高」決定,不是挑個好位置。** 用樓梯自己的局部座標
    (`t` 橫向、`s` 沿行進方向,`s=0` 是起步端)算,四個方向與兩種梯型才會是
    同一套邏輯 —— 寫成世界座標的話,樓梯一轉向(三區版的樓梯是**橫置**的)
    整段就會靜靜地算錯地方。

    折返梯段(`t ∈ [0, flight_width]`)從半層高爬回整層高,而它的高端就在
    **起步端**(`s=0`)—— 人是繞一圈回到原處、高一層。折返平台底下只有半層高
    (≈1.69m)站不了人,不能放。

    ⚠️ 以前這裡還有一支單跑直梯的分支(高端在梯跑盡頭、只有高的四成有頭高)。
       梯型統一成折返梯之後(使用者 2026-09-04)那支拿掉了。
    """
    want_w, want_d = UNDER_STAIR_WC
    min_w, min_d = UNDER_STAIR_WC_MIN
    run = stair.flight_run
    depth = min(want_d, run)
    s0, s1 = 0.0, depth                             # 高端 = 起步端
    width = min(want_w, stair.flight_width)
    if width < min_w or depth < min_d:
        return None
    a = stair.to_world(0.0, s0)
    b = stair.to_world(width, s1)
    return (min(a[0], b[0]), min(a[1], b[1]),
            max(a[0], b[0]), max(a[1], b[1]))


def _core_zone3(bx0, bx1, y1, y2, label, bath_name, level=1):
    """三區版中段核 → (房間清單, 樓梯, 空格);排不下回 None。

    橫置樓梯佔核的南段,北邊那條照使用者畫的:**1F 是儲藏室,2F 以上是浴室**
    (使用者 2026-09-01:「二樓的廁所會設計在房間裡面,或是樓梯旁邊」—— 這條
    正好就在樓梯旁邊,而且它的東牆直接貼著走道,門開在走道上)。

    ⚠️ 那條的**東牆就是走道**,門開在走道上。1F 用不完的西半把牆拿掉讓給餐廚
    (使用者 2026-09-01),樓上則整條都是浴室 —— 理由見下面那兩段註解。
    """
    plan = _zone3_core_plan(bx0, bx1, y1, y2, label)
    if plan is None:
        return None
    xp, stair, ds = plan
    ys = y1 + ds
    row_d = y2 - ys
    avail = xp - bx0
    if avail < BATH_TIGHT_W:            # 連最窄的廁所都排不下 → 這個核不成立
        return None
    if level == 1:
        # 1F:這一條是儲藏室,**用不完的那塊把牆拿掉、讓給餐廚**(使用者
        # 2026-09-01:「儲藏室那格也可以把跟廚房的牆拿掉,把廚房做大一點」)。
        # 4~5m 面寬整條就是儲藏室(=他畫的那張);6~8m 才會有剩。
        kind, name = "storage", f"儲藏室{level}F"
        w = min(avail, _room_area_cap(kind) / row_d)
        if avail - w < ZONE3_SPARE_MIN_W:   # 剩下的細到不成房間的一部分 → 整條給它
            w = avail
        spare = (bx0, ys, xp - w, y2) if avail - w > 1.0 else None
        rooms = [(kind, name, _rect(xp - w, ys, xp, y2))]
    else:
        # 2F 以上:這一條**整條就是浴室**,不切也不留空格。
        # ⚠️ 樓上不可以照 1F 那樣把剩下的讓給後面那間臥室:臥室多出來的那條腿,
        #    南牆**就坐在橫置樓梯上** —— 補門機制把門開在上面、門又被「不得開在
        #    階梯上」刪掉,那一整塊就此斷開(5.5×9.5、6×12.45、6×15.4 的 2F/3F
        #    實測都是 `floor_split`)。1F 沒有這個問題:餐廚沿著走道還有一整面
        #    牆開得了門,臥室那一段沒有。
        # ⚠️ 「西半再切一間儲藏室」也試過:它被界牆/樓梯/浴室/臥室夾在中間,
        #    5.5×15.45 與 6×15.4 補不出門 → `room_no_door`。
        kind, name, spare = "bathroom", bath_name, None
        rooms = [(kind, name, _rect(bx0, ys, xp, y2))]
    rooms.append(("stair_hall", "樓梯間",
                  [(bx0, y1), (bx1, y1), (bx1, y2), (xp, y2), (xp, ys),
                   (bx0, ys)]))
    return rooms, stair, spare


def _core(bx0, bx1, y1, y2, label, bath_name, bath_north=False,
          absorb_spare=False, min_service=False, patio=False):
    """中段核 → (房間清單, 樓梯, 讓出來的空格);服務格(西:浴廁)| 樓梯間(東)。

    樓梯間貼**東界牆**,裡面的排法是 **浴廁 | 梯段 | 走道**:梯段緊貼服務格那一
    側,走道因此落在**東界牆**邊(使用者 2026-08-27:「正常的走道應該靠在旁邊的
    牆上,不會在樓梯跟廁所的中間」;參考平面圖畫的就是這三條並排)。核每層同寬
    同位 → 樓梯 origin 固定 → 上下對齊。

    ⚠️ **不設儲藏室**(使用者 2026-07-30 定調):服務格只放浴廁,浴廁吃不完的那
    一小塊(1.5~2.4m 寬 × 約 1.8m)不另外隔成房間,而是回傳給呼叫端**併進隔壁
    居室**(客廳/餐廳/臥室變成 L 形)——隔成儲藏室只會多一扇門、多一間沒人用的
    小房;併進去則是實際可用的坪數。"""
    W, d = bx1 - bx0, y2 - y1
    svc, _sw = _core_widths(W, min_service, patio)
    xs = bx0 + svc                      # 服務格東緣 = 樓梯間西牆
    # 衛浴太寬就往淺切(面積封頂),免得核裡出現一間大空房;剩下的併給隔壁居室。
    bath_d = min(max(BATH_AREA_MAX / svc, BATH_MIN_D), BATH_MAX_D, d)
    if patio:
        # 要開天井就得**先把天井的位置留出來**:剩下的空格差 0.3㎡ 就不夠格當
        # 採光天井(4.5m 面寬實測 1.5×1.8=2.7㎡),浴廁淺一點就補得回來。
        # ⚠️ 但浴廁有自己的最小進深,壓不下去就維持原樣、天井那步自然不會成立。
        from src.design.layout.code_check import PATIO_MIN_AREA_M2, PATIO_MIN_SIDE
        need = max(PATIO_MIN_AREA_M2 * 1.0e6 / svc, PATIO_MIN_SIDE)
        bath_d = max(BATH_MIN_D, min(bath_d, d - need))
    if bath_north:                      # 變體:浴廁在北 → 空格在南(併給前室)
        rooms = [("bathroom", bath_name, _rect(bx0, y2 - bath_d, xs, y2))]
        spare = (bx0, y1, xs, y2 - bath_d)
    else:                               # 預設:浴廁在南 → 空格在北(併給後室)
        rooms = [("bathroom", bath_name, _rect(bx0, y1, xs, y1 + bath_d))]
        spare = (bx0, y1 + bath_d, xs, y2)
    if spare[3] - spare[1] < 1.0:       # 浴廁剛好吃滿 → 沒有空格
        spare = None
    # ── 天井(使用者 2026-08-26 指著參考平面圖要求)────────────────────────
    # 連棟街屋左右是共同壁,只有前後兩端能對外 —— 房子一深,中段就沒有採光來源。
    # 真實街屋的解法是在中段開**天井**,而位置正好就是服務格裡浴廁沒吃完的那一塊
    # (參考圖上天井就貼著廁所)。⚠️ 天井是**貫穿到屋頂的洞**,每層都要有、每層
    # 都少掉這塊樓地板 —— 所以只在「深到單面採光服務不了」時才開,淺的房子開了
    # 只是白白損失坪數(使用者 2026-07-29 拿掉天井時的理由)。
    if spare is not None and patio and _patio_ok(spare):
        rooms.append(("patio", "天井", _rect(*spare)))
        spare = None
    if spare is not None and absorb_spare:
        # 隔壁居室撐不住這塊空格(併上去會變走廊狀或採光不足)→ 併進**樓梯間**,
        # 當樓梯前的緩衝空間(不需採光、也不怕細長,比硬塞給居室健康)。
        sy0, sy1 = spare[1], spare[3]
        hall = ([(bx0, y1), (bx1, y1), (bx1, y2), (xs, y2), (xs, sy1), (bx0, sy1)]
                if bath_north else
                [(xs, y1), (bx1, y1), (bx1, y2), (bx0, y2), (bx0, sy0), (xs, sy0)])
        spare = None
    else:
        hall = _rect(xs, y1, bx1, y2)
    rooms.append(("stair_hall", "樓梯間", hall))
    return rooms, _stair(xs, bx1, y1, y2, label,
                         hug="west"), spare


# 併了空格之後,接收的那間房長寬比不得超過這個(對齊 plan_check 的 room_skinny
# 判準;此處不 import plan_check —— plan_check 反過來要 import 本模組)。
SPARE_ASPECT_LIMIT = 2.5
# 會被查長寬比的房間(對齊 plan_check:走道/豎井/陽台/儲藏本來就細長,不查)。
SKINNY_CHECK_KINDS = {"living", "dining", "kitchen", "bedroom",
                      "master_bedroom", "study", "elder_room", "bathroom"}


def _beds_ok(spec) -> bool:
    """這層每間臥室都擺得出床嗎?(**實際量過才算數**,不用公式猜)

    ⚠️ 為什麼需要:主臥切出更衣室之後,主臥就多了一個要走得到的目的地,
    `_declutter_for_circulation` 為了保住那條通道會**把床搬走** —— 一間沒有床的
    主臥比沒有更衣室糟得多。呼叫端量到就退掉更衣室重生一次(同 `_fit_service` /
    `_fit_margin` 的鐵則:加分項不得讓本來好好的東西壞掉)。
    """
    for room in spec.rooms:
        if room.kind not in ("bedroom", "master_bedroom"):
            continue
        poly = Polygon(room.points)
        if not any(getattr(f, "name", "").startswith("bed")
                   and poly.contains(Point(*f.insert)) for f in spec.fixtures):
            return False
    return True


def _spare_hosts_ok(spec) -> bool:
    """這層把空格併進居室之後,那間房還健康嗎?

    **實際量過才算數**(不用公式猜):併大了會有兩種病——變成走廊狀(長寬比 >2.5),
    或大到前後外牆開不出 §40 要求的窗(透天只有前後能開窗)。任一種出現,呼叫端就
    改把空格併進樓梯間重生一次。"""
    for room in spec.rooms:
        poly = Polygon(room.points)
        x0, y0, x1, y1 = poly.bounds
        long_s, short_s = max(x1 - x0, y1 - y0), max(min(x1 - x0, y1 - y0), 1.0)
        if room.kind in SKINNY_CHECK_KINDS and long_s / short_s > SPARE_ASPECT_LIMIT:
            return False
        if room.kind in WINDOW_KINDS:
            have = sum(op.width for w in spec.walls for op in w.openings
                       if op.kind == "window"
                       and poly.exterior.distance(
                           Point(*w.point_at(op.position))) < 60)
            if have < _need_window_width(room) - 1.0:
                return False
    return True


def _with_spare(x0, y0, x1, y1, spare):
    """矩形房間 + 貼在它南/北側的空格(核裡浴廁沒吃完的西半塊)→ L 形多邊形。

    取消儲藏室後多出來的那一塊併進隔壁居室,房間因此變成 L 形(真實平面很常見:
    客廳往浴廁旁邊凹進去一塊)。空格不貼著就原樣回傳矩形。"""
    if spare is None:
        return _rect(x0, y0, x1, y1)
    _sx0, sy0, sx1, sy1 = spare
    if abs(sy1 - y0) < 1.0:                     # 空格在房間南側
        return [(x0, sy0), (sx1, sy0), (sx1, y0), (x1, y0), (x1, y1), (x0, y1)]
    if abs(sy0 - y1) < 1.0:                     # 空格在房間北側
        return [(x0, y0), (x1, y0), (x1, y1), (sx1, y1), (sx1, sy1), (x0, sy1)]
    return _rect(x0, y0, x1, y1)


# ── 主臥 + 更衣室(使用者 2026-08-27 給的 2F 參考平面)────────────────────────
# 參考圖的 2F 是「前陽台|主臥室(含更衣)|樓梯+天井+衛浴|次臥|後陽台」。骨架本來
# 就長這樣,差的是**主次之分**與**更衣室**。
CLOSET_MIN_W = 1200.0           # 更衣室最小寬(一排吊衣桿 + 人轉身)
CLOSET_MIN_D, CLOSET_MAX_D = 1500.0, 2200.0   # 進深:太淺放不下、太深浪費主臥
# 切掉更衣室之後,主臥的北牆至少要留這麼長,門才開得出去
# (= 門寬 850 + 兩側舒適牆角淨距 350×2,與 BAND_DOOR_ADJ 同一個算法;那個常數
#  定義在本檔更下面,模組載入時還讀不到,故寫成數值並在此註明)。
CLOSET_DOOR_KEEP = 1550.0
# 主臥「要不要切成兩間」的門檻,比 plan_check 判違規的 1.5 倍**嚴**。
# ⚠️ 1.5 倍是「不會被判違規」的底線,不是設計目標:8m 面寬的前段 32㎡ 過得了
#    關卡,但那是一間 10 坪的臥室,而參考平面的主臥是 24㎡。超過理想上限 25%
#    就切成「主臥 + 小孩房」,兩間都還有正常大小。
MASTER_SPLIT_RATIO = 1.25

# 前/後段左右切一刀時,每半間至少要這麼寬(擺得下床 + 北牆開得出門)。
BAND_SPLIT_MIN_W = 2400.0
# 半間房要跟樓梯間貼這麼長的牆,門才塞得下:門寬 + 兩側**舒適**牆角淨距。
# ⚠️ 別用退讓階梯的下限(100)去算:那是「擠得進去」的物理極限,拿它當切點條件
#    會生出剛好 1050mm 的接觸面,門一放就貼著兩個牆角,repair_doors 補不出來,
#    整層變成「要穿越別人的臥室」(實測 8m 面寬留 1325mm 就補不到門)。
BAND_DOOR_ADJ = INTERIOR_DOOR_WIDTH + 2 * 350.0    # = DOOR_CLEAR_STEPS[0]
#    (那個常數定義在本檔更下面,模組載入時還讀不到,故寫成數值並在此註明)
# plan_check 判「過大」的倍率(AREA_BAND 上限的幾倍),兩邊要一致。
BAND_OVERSIZE_RATIO = 1.5


def _room_area_cap(kind: str) -> float:
    """這種房間大到多少(mm²)就會被 plan_check 判「過大」;沒訂上限的回 inf。

    參數也可以是**併合房名**(「餐廚」「客餐」)—— 那種房的上限是兩間相加,
    判準同樣走 `plan_check.MERGED_ROOM_PARTS`(唯一出處)。

    ⚠️ 面積判準的單一出處是 `graph_layout.AREA_BAND`,不要在這裡另抄一份數字
    (抄了就會出現「切法說夠小、關卡說過大」)。延遲 import:graph_layout
    反過來要 import 本模組。"""
    from src.design.layout.graph_layout import AREA_BAND
    from src.design.layout.plan_check import MERGED_ROOM_PARTS

    parts = MERGED_ROOM_PARTS.get(kind)
    if parts is not None:
        bands = [AREA_BAND.get(k) for k in parts]
        if all(b is not None for b in bands):
            return sum(b[1] for b in bands) * BAND_OVERSIZE_RATIO * 1.0e6
    band = AREA_BAND.get(kind)
    return band[1] * BAND_OVERSIZE_RATIO * 1.0e6 if band else float("inf")


def _master_split_cap() -> float:
    """主臥大到這個面積(mm²)就切成兩間 —— 見 `MASTER_SPLIT_RATIO`。"""
    from src.design.layout.graph_layout import AREA_BAND
    return AREA_BAND["master_bedroom"][1] * MASTER_SPLIT_RATIO * 1.0e6


def _band_split_x(bx0, bx1, hall_x0, west_kind="bedroom", east_kind="bedroom",
                  avoid=None):
    """前/後段左右切一刀要切在哪?切不動(面寬不夠)回 None。

    ⚠️ 切點**不能自由選**。這一段只有一面朝向中段核,兩間房的門都得開在那面
    牆上,而那面牆不是整條都貼著樓梯間 —— 貼著**浴廁**的那一段開不了門
    (`hall_x0` 就是樓梯間在這條牆上的西端)。切點若太靠西,西半間跟樓梯間只
    剩一小段接觸面,門補不出來,那間房就變成「要穿越隔壁臥室才進得去」。

    ⚠️ `hall_x0` 隨變體改變:浴廁在南(預設)時擋的是**前段**,浴廁在北時擋的
    是**後段** —— 兩段不能共用同一個值。

    「要不要切」由呼叫端判(拿房間會不會過大去問 `_room_area_cap`),這裡只回答
    「切得動的話切在哪」。

    切點**照兩間房各自該有的大小按比例分**,不是一律切中間:1F 後段是廚房|餐廳,
    廚房的合理上限(11㎡)比餐廳(14㎡)小,對半切會讓廚房超標。兩邊同 kind
    (樓上前段兩間臥室)時比例是 1:1,退化成切中間。

    ⚠️ `avoid`=(lo, hi) 是**樓梯旁走道**在這條牆上的範圍,切點不得落在裡面。
    走道口被切成兩半的話,兩間房各只分到走道的一部分,誰都塞不下一扇帶牆角淨距
    的門 → 補門機制改走浴廁或隔壁臥室,動線變成「穿過廁所/穿過別人的房間」。
    切點推到走道邊緣;推出去會讓某一間太窄就**不切**(併成一間比走不通好)。
    """
    lo = max(bx0 + BAND_SPLIT_MIN_W, hall_x0 + BAND_DOOR_ADJ)
    hi = min(bx1 - BAND_SPLIT_MIN_W, bx1 - BAND_DOOR_ADJ)
    if lo > hi:                                     # 面寬不夠切
        return None
    cw, ce = _room_area_cap(west_kind), _room_area_cap(east_kind)
    frac = cw / (cw + ce) if cw + ce < float("inf") else 0.5   # 沒訂上限 → 對半
    x = min(max(bx0 + (bx1 - bx0) * frac, lo), hi)
    if avoid is None or _passage_split_ok(x, avoid):
        return x
    cands = [c for c in (avoid[0], avoid[1]) if lo - 1e-6 <= c <= hi + 1e-6]
    if not cands:
        return None                                 # 推不出走道 → 這一段不切
    return min(cands, key=lambda c: abs(c - x))


def _master_level(garage: bool = False) -> int:
    """主臥在哪一層的前段。1F 讓給車庫時 2F 前段是客廳 → 主臥往上一層。"""
    return 3 if garage else 2


def _master_suite(bx0, by0, bx1, y1, spare, xs, bath_north, level,
                  closet=True):
    """主臥層的前段:主臥室(+ 放得下就切一間更衣室)。

    更衣室切在**北緣、貼著浴廁的那一端** —— 那一段北牆本來就開不了主臥的門
    (門要開向樓梯間),拿來當更衣室剛好;主臥的門仍然開在剩下那段牆上。
    浴廁在北(bath_north)時前段整條北牆都是樓梯間,更衣室改切在西端。

    ⚠️ 空格(服務格沒吃完的那塊)已經併進前段時**不切** —— 那時前段是 L 形,
    再切一刀會生出奇形怪狀的房間,而 L 形的凹角本來就是收納的位置。
    """
    poly = _with_spare(bx0, by0, bx1, y1, spare)
    room = ("master_bedroom", "主臥室", poly)
    if not closet:                                  # 量過發現床擺不下 → 不切
        return [room]
    if len(poly) != 4:                              # 前段是 L 形(帶空格)→ 不切
        return [room]
    depth = min(CLOSET_MAX_D, (y1 - by0) / 3.0)     # 別吃掉主臥超過 1/3 進深
    if depth < CLOSET_MIN_D:
        return [room]
    # 切完之後北牆要留得下主臥自己的門。
    avail = (bx1 - bx0) - CLOSET_DOOR_KEEP
    if bath_north:
        # 前段整條北牆都是樓梯間 → 更衣室切在西端,寬度給到夠用就好。
        width = min(CLOSET_MIN_W * 1.5, avail)
    else:
        # 北牆的 [bx0, xs] 這段貼著浴廁,本來就開不了門 —— 更衣室就切那一段。
        width = min(xs - bx0, avail)
    if width < CLOSET_MIN_W:
        return [room]
    cx0, cx1 = (bx0, bx0 + width)
    return [("master_bedroom", "主臥室",
             _l_minus_corner(bx0, by0, bx1, y1, cx0, cx1, y1 - depth)),
            ("storage", f"更衣室{level}F", _rect(cx0, y1 - depth, cx1, y1))]


def _l_minus_corner(x0, y0, x1, y1, cx0, cx1, cy):
    """矩形挖掉「北緣、西端」那一塊(cx0~cx1 × cy~y1)後剩下的 L 形。"""
    return [(x0, y0), (x1, y0), (x1, y1), (cx1, y1), (cx1, cy), (x0, cy)]


def _floor_rooms(level, top, bx0, by0, bx1, by1, variant=DEFAULT_VARIANT,
                 force_absorb=False, force_bath_south=False,
                 force_bath_north=False,
                 allow_min_service=True, patio=False, garage=False,
                 closet=True, core_style="default", core_out=None):
    """一層的房間矩形 + 樓梯(依設計變體微調服務格與後段配置)。

    force_absorb:取消儲藏室後多出來的空格,改併進樓梯間而不是隔壁居室
    (由 _build_floor 量過發現居室吃不下時重跑一次)。
    force_bath_south:連併進樓梯間都讓整層斷開時的最後手段——浴廁搬回南側,
    樓梯間北端才是完整一條(門開得出去);此時 bath_north 變體失效。"""
    d_front, d_core, d_rear = _split_depth(by1 - by0, _zones(garage))
    y1, y2 = by0 + d_front, by0 + d_front + d_core
    label = "下" if level == top else "上"
    # 前/後段能不能從中間切一刀,取決於樓梯間在那條牆上從哪裡開始(見
    # _band_split_x)。浴廁在哪一側,擋住的就是哪一段。
    # 參考圖版的核沒有「服務格在南還是在北」這回事(廁所固定貼著走道),
    # 而且走道與切點都只看 xp 一個數字 —— 這個變體對它無效。
    # 三區版:樓上的浴室就在核裡(樓梯北邊那條),不必從第三區挖 —— 所以這裡
    # 只問核本身排不排得下,每層的答案一致(核要同構)。
    zone3_plan = (_zone3_core_plan(bx0, bx1, y1, y2, label)
                  if core_style == "zone3" else None)
    ref_plan = (_ref_core_plan(bx0, bx1, y1, y2, label)
                if core_style == "ref"
                or (core_style == "zone3" and zone3_plan is None) else None)
    # 核的退讓階梯 **ref → mid → default**(使用者 2026-08-31:「每一個尺寸都
    # 設計成類似這個格式」)。橫置樓梯要一整段面寬跑得完,3.5~3.9m 排不下 ——
    # 但「廁所的門開在走道上」那件事 `mid` 也做得到,退到 `mid` 比一路退回
    # `default`(廁所門開向餐廚)更接近使用者要的格式。
    mid_plan = (_mid_core_plan(bx0, bx1, y1, y2, label)
                if core_style == "mid"
                or (core_style in ("ref", "zone3") and ref_plan is None)
                else None)
    bath_north = ((variant.bath_north or force_bath_north)
                  and not force_bath_south
                  and ref_plan is None and mid_plan is None
                  and zone3_plan is None)

    def _note_core(style):
        """記下這一層真正用到的核,並算出走道兩端的**開口**位置。

        走道的兩個出入口(南接前段、北接後段)**不設門、也不隔牆**
        (使用者 2026-09-02 說了三次、2026-09-03 再確認:「要把空間視覺化大一
        點」)—— 見 `_open_passage_mouth`。

        ⚠️ 走道在 x 上的範圍一律問 `_passage_span`(四款核共用的單一出處),
        不要照著各款核的幾何再算一次:同一件事兩把尺在本檔已經踩過六次。
        ⚠️ 要用**實際落地的那款**核去問,不是呼叫端要的那款 —— ref 排不下會退到
        mid、再退到 default,拿原本那款去問會算到另一條走道的位置。
        """
        if core_out is None:
            return
        core_out["style"] = style
        av = _passage_span(bx0, bx1, min_service, patio,
                           style, y1, y2)
        core_out["mouths"] = ([(y1, av[0], bx1), (y2, av[0], bx1)]
                              if av is not None else [])

    def _make_core(bath_name):
        if zone3_plan is not None:
            got = _core_zone3(bx0, bx1, y1, y2, label, bath_name, level)
            if got is not None:
                _note_core("zone3")
                return got
        if ref_plan is not None:
            got = _core_ref(bx0, bx1, y1, y2, label, bath_name, patio=True)
            if got is not None:
                _note_core("ref")
                return got
        if mid_plan is not None:
            got = _core_mid(bx0, bx1, y1, y2, label, bath_name)
            if got is not None:
                _note_core("mid")
                return got
        _note_core("default")
        return _core(bx0, bx1, y1, y2, label, bath_name, bath_north,
                     absorb_spare=force_absorb, min_service=min_service,
                     patio=patio)
    # 有車庫時 1F 前段是車庫、客廳上 2F(見 _zones 上面那段)。前段該用哪一把
    # 面積尺量,跟著它是哪種房間走 —— 拿臥室的上限去量客廳會把 24㎡ 的客廳切成
    # 兩間小房。
    # 主臥放在「最低的那個前段是臥室的樓層」:一般是 2F;1F 讓給車庫時 2F 前段
    # 是客廳,主臥就上 3F。主臥用自己的面積尺(12~24㎡,比次臥寬)—— 拿次臥的
    # 上限去量會把一間正常的主臥切成兩間小房。
    is_master = level == _master_level(garage)
    front_kind = ("living" if (garage and level == 2)
                  else "master_bedroom" if is_master else "bedroom")
    front_east = "study" if front_kind == "living" else "bedroom"
    big_front = (bx1 - bx0) * d_front > (_master_split_cap()
                                         if front_kind == "master_bedroom"
                                         else _room_area_cap(front_kind))
    big_rear = (bx1 - bx0) * d_rear > _room_area_cap("bedroom")

    def _splits(min_service, bf, br):
        if zone3_plan is not None:
            # 三區版:走道貼東界牆,前後段的門都開在那一段(梯段那半是樓梯)。
            xs = zone3_plan[0]
            hall_front = hall_rear = xs
        elif ref_plan is not None:
            # 參考圖版:前後段的門都只開得在走道那一段(其餘不是樓梯就是廁所
            # /天井),所以兩段共用同一個起點 = 走道西緣。
            xs = ref_plan[0]
            hall_front = hall_rear = xs
        elif mid_plan is not None:
            # 服務格在中間:前段面對的是樓梯的**起步平台**那一側(浴廁在北),
            # 所以整條 [樓梯東緣, bx1] 都開得了門;後段只剩走道那一段。
            xs = mid_plan[1]
            hall_front, hall_rear = xs, mid_plan[0]
        else:
            xs = bx0 + _core_widths(bx1 - bx0, min_service, patio)[0]  # 服務格東緣
            hall_front = bx0 if bath_north else xs           # 浴廁擋住的是哪一段
            hall_rear = xs if bath_north else bx0
        # ⚠️ 只有**後段**的切點要讓開走道。前段面對的是樓梯的**起步平台**
        #    (那一端就是樓層地板高度,整條牆都開得了門);後段面對的是
        #    **折返平台**那一端,只有旁邊那條走道走得過去。兩段一起套的話,
        #    7~8m 面寬的前段會切不成兩間 → 8 條測試冒出 35㎡ 的 room_oversize。
        av = _passage_span(bx0, bx1, min_service, patio,
                           core_style, y1, y2)
        return (xs,
                (_band_split_x(bx0, bx1, hall_front, front_kind, front_east)
                 if bf else None),
                (_band_split_x(bx0, bx1, hall_rear, "bedroom", "study",
                               avoid=av) if br else None),
                hall_rear)

    # ⚠️ 服務格要不要壓到最窄是**整棟**的決定,不是這一層的 —— 核每層同構,
    #    樓梯才對得齊,浴廁與樓梯間之間那道牆也才上下對得上。所以用「最需要壓
    #    的那種樓層」去問:臥室的面積上限最小(27㎡ < 主臥 36 < 客廳 48),拿它
    #    當探針就等於問「這棟有沒有任何一層需要壓」。
    #    (踩過:主臥層改用主臥的尺之後不必切了 → 那層就不壓 → 2F 的浴廁比
    #     1F/3F 寬 1㎡、牆對不上。)
    probe_front = (bx1 - bx0) * d_front > _room_area_cap("bedroom")
    _xs, pf, pr, _hr = _splits(False, probe_front, big_rear)
    min_service = False
    if allow_min_service and ((probe_front and pf is None)
                              or (big_rear and pr is None)):
        # 這是拿「浴廁窄 37cm」換「臥室不要 28㎡」;壓了還是切不動就維持原樣。
        _xs2, af, ar, _hr2 = _splits(True, probe_front, big_rear)
        if (probe_front and af is not None) or (big_rear and ar is not None):
            min_service = True
    xs, xf, xr, hall_rear = _splits(min_service, big_front, big_rear)

    # 核每層同構(服務格+樓梯間)→ 樓梯上下對齊。
    if level == 1:                                  # 1F:客廳 / 核 / 餐廳|廚房
        core, stair, spare = _make_core("浴廁")
        # 前段:有車庫就整段停車(捲門由 _add_garage_doors 開在南向臨路牆)。
        fk, fn = ("garage", "車庫") if garage else ("living", "客廳")
        front_1f = (fk, fn, _with_spare(bx0, by0, bx1, y1, spare))
        # 後段左右分:切點照**兩間房各自的合理面積**按比例分(`_band_split_x`),
        # 而且不得切爛走道口(`avoid`)。
        # ⚠️ 舊寫法是「切在梯段導牆中心線」—— 那是梯段貼**東**牆時代的幾何:
        #    導牆中心線恰好就在走道旁邊,切在那裡等於把走道整條讓給一間房。
        #    走道改貼東界牆、梯段改貼西之後(使用者 2026-08-27),同一條式子算出
        #    來的切點跑到服務格東緣 → 整個樓梯間的寬度全歸東室,7m 面寬的廚房
        #    17㎡、8m 21㎡(上限 11)。切點的依據要回到「兩間房該多大」,
        #    走道那件事交給 `avoid` 管就好。
        av = _passage_span(bx0, bx1, min_service, patio,
                           core_style, y1, y2)
        west, east = (("dining", "餐廳"), ("kitchen", "廚房"))
        # ⚠️ 切點**不得落在走道口上**。4.5m 面寬那兩個下限(每間後室 ≥2.4m)會把
        #    切點拉回走道正中 → 走道north端一半對廚房、一半對餐廳,兩邊都塞不下
        #    一扇帶牆角淨距的門 → 補門機制只好改走浴廁,動線變成「穿過廁所才
        #    到得了餐廳」(而 plan_check 沒有這條規則,靜悄悄地過)。
        #    切不到走道外面就**乾脆不切**(後段併成一間開放餐廚)—— 併房是本來
        #    就有的選項,而走不通的動線是廢圖。
        xm = _band_split_x(bx0, bx1, hall_rear, west[0], east[0], avoid=av)
        if xm is None or not _split_clears_passage(xm, av):
            big_rear = False                    # → 走下面的開放餐廚那條路
        # ── 開放式餐廚(使用者 2026-08-26 指著參考平面圖:「餐廳/廚房」是一間)──
        # 真實透天的 1F 後段就是一間開放餐廚,不是隔成兩間 —— 4.5m 面寬硬切,
        # 廚房只剩 2m 寬(走廊狀),而且中間那道牆在生活上沒有意義。
        # 併起來的上限是**兩間相加**(plan_check.MERGED_ROOM_PARTS 已經這樣算),
        # 所以只有大到連併合上限都撐不住的後段(8×18m 那種 44㎡)才切成兩間。
        if ((variant.open_kitchen or not big_rear)
                and (bx1 - bx0) * d_rear <= _room_area_cap("餐廚")):
            rooms = [front_1f, *core,
                     ("dining", "餐廚",
                      _with_spare(bx0, y2, bx1, by1, spare))]
            return rooms, stair
        # ⚠️ 走到這裡代表「併成一間會超過**餐廚**的上限」(8×18m 那種 44㎡)——
        #    非切不可。切點閃不開走道口(xm is None)時也得切:兩間房裡有一間的
        #    門會不好開,但那是 plan_check 擋得住、換個切法就能解的問題;帶著
        #    None 走下去則是直接 crash(參考圖版的走道只有一個門的寬度,實測
        #    7.8×15.9m 就會踩到)。
        if xm is None:
            xm = _band_split_x(bx0, bx1, hall_rear, west[0], east[0])
        if xm is None:
            xm = (bx0 + bx1) / 2.0
        # 儲藏室取消後多出來的空格 → 併進緊鄰的那間居室(南=客廳、北=後段西室)
        rooms = [front_1f, *core,
                 (west[0], west[1], _with_spare(bx0, y2, xm, by1, spare)),
                 (east[0], east[1], _rect(xm, y2, bx1, by1))]
        return rooms, stair

    core, stair, spare = _make_core(f"浴室{level}F")
    # 前/後段:面寬寬的時候各切成兩間(整段當一間房會大到不合理 —— 8m 面寬
    # 的前段是 8×5.4m ≈ 43㎡)。空格(浴廁沒吃完的西半塊)一定落在西半間。
    if front_kind == "living":                      # 1F 讓給車庫 → 客廳在這裡
        # ⚠️ 名字不能也叫「書房{level}F」—— 後段切開時的東半間就叫這個,同一層
        #    會出現**兩間同名房**。`door_rules.repair_doors` 修 through_bedroom
        #    時是**用名字**去找那間房的(本檔「房間不能用名稱比對」那條坑),
        #    撞名就會修錯間、修了等於沒修(實測 7.96×16.36 seed95 卡在這裡)。
        front = ([("living", "客廳", _with_spare(bx0, by0, xf, y1, spare)),
                  ("study", f"前書房{level}F", _rect(xf, by0, bx1, y1))]
                 if xf is not None else
                 [("living", "客廳",
                   _with_spare(bx0, by0, bx1, y1, spare))])
    elif is_master:                                 # 主臥層
        # 前段大到要切兩間時(寬面寬),西半間仍然是主臥 —— 不能整層都沒有主臥。
        front = ([("master_bedroom", "主臥室",
                   _with_spare(bx0, by0, xf, y1, spare)),
                  ("bedroom", f"小孩房{level}F", _rect(xf, by0, bx1, y1))]
                 if xf is not None else
                 _master_suite(bx0, by0, bx1, y1, spare, xs, bath_north, level,
                               closet))
    else:
        front = ([("bedroom", f"前臥室{level}F",
                   _with_spare(bx0, by0, xf, y1, spare)),
                  ("bedroom", f"小孩房{level}F", _rect(xf, by0, bx1, y1))]
                 if xf is not None else
                 [("bedroom", f"前臥室{level}F",
                   _with_spare(bx0, by0, bx1, y1, spare))])
    rear_name = f"次臥{level}F" if is_master else f"後臥室{level}F"
    rear = ([("bedroom", rear_name,
              _with_spare(bx0, y2, xr, by1, spare)),
             ("study", f"書房{level}F", _rect(xr, y2, bx1, by1))]
            if xr is not None else
            [("bedroom", rear_name,
              _with_spare(bx0, y2, bx1, by1, spare))])
    return [*front, *core, *rear], stair


# ── 開口收尾:去界牆窗 / 去天井門 / 浴室單門 / 加前門 ────────────────────────
def _is_party_wall(wall, bx0, bx1) -> bool:
    (sx, _), (ex, _) = wall.start, wall.end
    if abs(sx - ex) > 1.0:
        return False
    return abs(sx - bx0) < 50.0 or abs(sx - bx1) < 50.0


def _door_kinds(spec, dp) -> list:
    """一扇門兩側鄰接的房間 kind(往牆法線兩側各探 150mm)。"""
    w = spec.walls[dp.wall_index]
    op = w.openings[dp.opening_index]
    cx, cy = w.point_at(op.position)
    nx, ny = w.normal_vector
    out = []
    for s in (1, -1):
        p = Point(cx + nx * 150 * s, cy + ny * 150 * s)
        for r in spec.rooms:
            if Polygon(r.points).contains(p):
                out.append(r.kind)
                break
    return out


def _remove_openings(spec, targets: set):
    """安全刪除一組 (wall_index, opening_index):重建各牆洞口序列並重映射門/窗索引。"""
    if not targets:
        return
    by_wall: dict = {}
    for wi, oi in targets:
        by_wall.setdefault(wi, set()).add(oi)
    remap: dict = {}
    for wi, wall in enumerate(spec.walls):
        drop = by_wall.get(wi, set())
        kept = []
        for oi, op in enumerate(wall.openings):
            if oi in drop:
                remap[(wi, oi)] = None
            else:
                remap[(wi, oi)] = len(kept)
                kept.append(op)
        wall.openings = kept
    spec.doors = [dp for dp in spec.doors
                  if remap.get((dp.wall_index, dp.opening_index)) is not None]
    for dp in spec.doors:
        dp.opening_index = remap[(dp.wall_index, dp.opening_index)]
    spec.windows = [wp for wp in spec.windows
                    if remap.get((wp.wall_index, wp.opening_index)) is not None]
    for wp in spec.windows:
        wp.opening_index = remap[(wp.wall_index, wp.opening_index)]


#: 走道口兩端各留這麼一小段牆:讓開口不要正好切在垂直牆(導牆/界牆)的牆體上。
PASSAGE_MOUTH_PIER = 90.0

#: 走道口**可以不裝門扇**的鄰室 —— 只有公共空間。
#:
#: 使用者 2026-09-02 說「走道出入口不用設門也不用隔牆」,講的是參考平面圖上那條
#: 走道:一端接客廳、一端接餐廚,兩端都是公共空間,中間本來就不該有門。
#: 2026-09-05 補上另一半:「走道如果出口或入口是房間就要放門」「房間門口都要有門」
#: —— 樓上的走道兩端接的是**臥室/書房**(1F 車庫版接的是車庫),那不是通道,
#: 那是房間的門口。沒有門的臥室不是設計選擇,是漏畫。
PASSAGE_OPEN_KINDS = frozenset({
    "living", "dining", "kitchen", "foyer", "stair_hall", "corridor", "balcony",
})


def _mouth_far_room(spec, wall, pos: float):
    """走道口的**另一邊**是哪一間房(走道自己那一側不算)。取不到給 None。

    兩側各探 300mm:一側是走道(樓梯間),另一側就是這個口通往的房間。判斷要裝不
    裝門扇全看它 —— 見 `PASSAGE_OPEN_KINDS`。
    """
    px, py = wall.point_at(pos)
    got = []
    for sgn in (1.0, -1.0):
        pt = Point(px, py + sgn * 300.0)
        got.append(next((r for r in spec.rooms
                         if Polygon(r.points).contains(pt)), None))
    far = [r for r in got if r is not None
           and r.kind not in ("stair_hall", "corridor")]
    return far[0] if len(far) == 1 else None


def _open_passage_mouth(spec, y, x0, x1) -> bool:
    """走道的出入口:接公共空間就整條開成通道,接房間就給它一扇門。回有沒有動到。

    使用者 2026-09-02:「走道出入口都不用設置門,也不用用牆隔起來」。真實透天的
    走道就是這樣 —— 從客廳走進去、從走道走出到餐廚,中間沒有門扇也沒有牆。

    ⚠️ 但那句話只對**公共空間**成立。使用者 2026-09-05:「走道如果出口或入口是
    房間就要放門」「房間門口都要有門」—— 樓上的走道兩端接的是臥室/書房,整條開
    掉等於那間臥室沒有門。所以這裡先問另一邊是誰(`_mouth_far_room`):公共空間
    照舊整條開,房間就改成一扇**正常寬度的內門**(位置仍是走道口的中心)。

    做法是把那一段開成一個**沒有門扇的洞口**(`Opening` 本身就只是「把牆斷開」,
    見 `wall.solid_segments`),原本開在那裡的門連同門扇一起拆掉。所以圖上那一段
    就是一個缺口,不會畫門弧;`room_circulation._room_openings` 也認得它
    (`Opening(kind="door")` 一律算通道),動線判定不受影響。

    ⚠️ **不是把牆整段刪掉。** 刪掉的話那一段的柱子會變成沒有牆遮的孤柱(使用者
       嫌過的東西),而且牆一拆就要重排 `spec.doors` / `spec.windows` 的索引。
       留著牆、開一個滿寬的洞,畫出來是一樣的缺口,風險小得多。
    """
    from dataclasses import replace as _replace
    from src.drafting.wall import Opening

    for wi, w in enumerate(spec.walls):
        (sx, sy), (ex, ey) = w.start, w.end
        if abs(sy - ey) > 1.0 or abs(sy - y) > 60.0:
            continue
        lo, hi = min(sx, ex), max(sx, ex)
        # ⚠️ **牆垛只留在「牆還要繼續」的那一端。** 走道的另一側就是界牆,開口
        #    收在那裡的話會留下一小段從界牆凸出來的牆頭(使用者 2026-09-02 圈
        #    出來的就是這個:「這兩個突出來的牆也不需要設計,要讓空間最大化」)。
        #    開口直接切齊牆的盡頭,那一段就乾乾淨淨地開到底。
        a = max(lo, x0)
        b = min(hi, x1)
        if a > lo + 1.0:
            a += PASSAGE_MOUTH_PIER
        if b < hi - 1.0:
            b -= PASSAGE_MOUTH_PIER
        if b - a < INTERIOR_DOOR_WIDTH:
            continue
        mid = (a + b) / 2.0
        pos = mid - sx if sx < ex else sx - mid
        far = _mouth_far_room(spec, w, pos)
        # 另一邊是房間 → 裝門扇(門寬用一般內門,不是整條走道那麼寬);
        # 是客廳/餐廚那種公共空間、或看不出來是誰 → 照舊整條開成通道。
        leaf = far is not None and far.kind not in PASSAGE_OPEN_KINDS
        # 這一段上原有的門/窗先拆掉(門扇一起),再開洞。
        drop = set()
        for oi, op in enumerate(w.openings):
            cx = w.point_at(op.position)[0]
            if a - op.width / 2 - 1.0 <= cx <= b + op.width / 2 + 1.0:
                drop.add((wi, oi))
        _remove_openings(spec, drop)
        op = Opening(position=pos,
                     width=INTERIOR_DOOR_WIDTH if leaf else b - a, kind="door")
        # ⚠️ 標記成「走道口」:躲柱那支會把它當一般門洞沿著牆挪開,結果就是
        #    使用者圈出來的那兩截凸出來的牆頭。通道的兩端是**結構**決定的
        #    (一邊是導牆、一邊是界牆),不是可以挑位置的門。裝了門扇也一樣 ——
        #    走道只有一扇門寬時,門的兩側本來就是那兩道牆,那不是「卡在牆角」。
        op.is_passage = True
        if leaf:
            op.passage_door = True      # ⚠️ 後掛標記 → `_mirror_spec` 要跟著帶
        spec.walls[wi].openings.append(op)
        if leaf:
            # 門扇往**房間**那一側開:往走道開的話,一條 0.9m 的走道會被門擋死。
            ux, uy = w.unit_vector
            nx, ny = -uy, ux
            px, py = w.point_at(pos)
            fc = Polygon(far.points).centroid
            swing = "out" if (fc.x - px) * nx + (fc.y - py) * ny > 0 else "in"
            spec.doors.append(DoorPlacement(wi, len(spec.walls[wi].openings) - 1,
                                            Door(swing=swing)))
        return True
    return False


def _has_passage(spec) -> bool:
    """這一層有沒有「樓梯旁那條走道」的開放通道口?

    走道兩端不設門也不隔牆(使用者 2026-09-02),所以它在圖上的痕跡就是
    `is_passage` 的開口。梯段填滿整個樓梯間(擠不出走道)時一個都沒有 ——
    那一層的前段到後段就只能踩過折返平台,是走不通的。"""
    return any(getattr(op, "is_passage", False)
               for w in spec.walls for op in w.openings)


# 浴室留門時,偏好開向的公共鄰室(越前面越優先)。
_BATH_DOOR_PREF = ("stair_hall", "corridor", "living", "dining", "kitchen")


def _fix_openings(spec, bx0, by0, bx1, level, party_walls: bool = True,
                  entry_frac: float = 0.22, garage: bool = False):
    """去重複門、去界牆窗、去天井門、浴室只留 1 門;1F 補臨路前門。

    party_walls:東西外牆是不是與鄰房共用的界牆(透天=True,不開窗)。獨棟(中庭
    骨架)給 False → 四面都可開窗,否則整棟只剩前後採光、房間會變暗。"""
    remove: set = set()

    # 重複門:同一位置被開了兩扇(相鄰兩室互開)→ 只留一扇
    seen: dict = {}
    for dp in spec.doors:
        w = spec.walls[dp.wall_index]
        px, py = w.point_at(w.openings[dp.opening_index].position)
        key = (round(px / 10), round(py / 10))
        if key in seen:
            remove.add((dp.wall_index, dp.opening_index))
        else:
            seen[key] = True

    # 界牆(東西共用牆)不開窗——獨棟不適用(party_walls=False 時四面都能開)
    if party_walls:
        for wi, w in enumerate(spec.walls):
            if _is_party_wall(w, bx0, bx1):
                for oi, op in enumerate(w.openings):
                    if op.kind == "window":
                        remove.add((wi, oi))

    # 天井不設走入門(採光豎井)
    for dp in spec.doors:
        if "patio" in _door_kinds(spec, dp):
            remove.add((dp.wall_index, dp.opening_index))

    # 開在梯段旁的門要拿掉:門一開就是踏step,人沒有落腳處。刪掉後由 _ensure_floor_
    # connected 改開在起步平台那一側(那裡才有平地)。
    if _stair_boxes(spec):
        for dp in spec.doors:
            w = spec.walls[dp.wall_index]
            op = w.openings[dp.opening_index]
            if not _door_clear_of_stairs(spec, w, op.position, op.width):
                remove.add((dp.wall_index, dp.opening_index))

    # 每間浴室/儲藏室只留 1 門(偏好開向公共鄰室,其餘刪掉)
    # ⚠️ 儲藏室**也要**:三區版核的儲藏室夾在樓梯與第三區之間,兩邊都開得了門
    #    → 廚房的門就開進儲藏室,變成「要穿過儲藏室才進得了廚房」。儲藏室跟浴室
    #    一樣是**目的地不是通道**,一扇門就夠,而偏好表把走道排在最前面,剛好
    #    就是使用者畫的那扇。(更衣室也是 storage,它本來就只有一扇門,無影響。)
    for room in [r for r in spec.rooms if r.kind in ("bathroom", "storage")]:
        bp = Polygon(room.points)
        adj = [dp for dp in spec.doors
               if bp.exterior.distance(
                   Point(spec.walls[dp.wall_index].point_at(
                       spec.walls[dp.wall_index]
                       .openings[dp.opening_index].position))) < 50.0]
        if len(adj) > 1:
            # ⚠️ **先問「門前面走得到嗎」,再問開向哪一種房間**(2026-08-28)。
            #    偏好表把樓梯間排在最前面,但梯段盡頭那塊地繞不過去 —— 留下一扇
            #    走不到的門、刪掉一扇走得到的,浴室就此進不去。
            areas = _stair_room_areas(spec)

            def _walk(dp):
                w = spec.walls[dp.wall_index]
                op = w.openings[dp.opening_index]
                px, py = w.point_at(op.position)
                along = py if abs(w.start[0] - w.end[0]) < 1.0 else px
                return _door_front_walkable(spec, w, along, areas)

            def _pref(dp):
                ks = set(_door_kinds(spec, dp)) - {"bathroom"}
                return next((i for i, k in enumerate(_BATH_DOOR_PREF)
                             if k in ks), len(_BATH_DOOR_PREF))
            adj.sort(key=lambda dp: (not _walk(dp), _pref(dp)))
            for dp in adj[1:]:
                remove.add((dp.wall_index, dp.opening_index))

    _remove_openings(spec, remove)

    if level == 1:                                  # 臨路大門:南向外牆
        if garage:                                  # 前段是車庫 → 捲門(+人行門)
            _add_garage_doors(spec, bx0, by0, bx1, entry_frac)
        else:
            _add_front_door(spec, bx0, by0, bx1, entry_frac)


#: 大門**不該**直接開進去的房間 —— 一進門就是廁所/臥室是台灣室內設計的經典 NG
#: (書上〈9 種 NG 格局〉的同一族:機能與隱私都不對)。
ENTRY_BAD_KINDS = {"bathroom", "toilet", "bedroom", "master_bedroom", "storage"}


def _entry_room_ok(spec, wall, pos: float) -> bool:
    """這個位置開大門,門後面那間房適合當玄關嗎。

    ⚠️ 以前 `_add_front_door` 只問「撞不撞洞口、離不離牆角夠遠」,**從來沒問過
    門後面是哪間房** —— 淺基地的浴廁就排在前段西端,而預設的 `entry_frac=0.22`
    正好偏西,於是 7 個尺寸有 **7 個**大門直接開進廁所(2026-09-04 量的)。
    """
    from src.design.semantic.room_semantic import canonical_room
    px, py = wall.point_at(pos)
    for room in spec.rooms:
        poly = Polygon(room.points)
        if poly.exterior.distance(Point(px, py)) > DOOR_TOUCH_TOL:
            continue
        if canonical_room(room.kind) in ENTRY_BAD_KINDS:
            return False
    return True


def _add_front_door(spec, bx0, by0, bx1, entry_frac=0.22):
    """在南向(臨路)外牆上加一扇大門,避開既有洞口。entry_frac=偏好位置比例。

    ⚠️ **兩段式**(與門的 `_door_front_walkable` 同一招):先只收「門後面不是
    廁所/臥室」的位置跑完整條淨距退讓階梯,整面牆都沒有才退而求其次 ——
    **排序不是過濾**,一扇開在廁所前的大門仍然勝過一棟沒有大門的房子
    (`no_entry` 是硬錯誤)。
    """
    for wi, w in enumerate(spec.walls):
        (sx, sy), (ex, ey) = w.start, w.end
        if abs(sy - by0) > 50 or abs(ey - by0) > 50 or abs(sx - ex) < 1:
            continue                                # 只找南向水平外牆
        length = w.length
        taken = [(op.position - op.width / 2, op.position + op.width / 2)
                 for op in w.openings]
        # 先試慣用比例,再沿牆掃描 → 一定落在「不撞洞口 且 離每間房角落夠遠」的位置。
        step = 100.0
        n = max(1, int(length / step))
        cands = [length * entry_frac, length * 0.22,
                 length * 0.78, length * 0.5]
        cands += [i * step for i in range(1, n)]
        for good_room_only in (True, False):        # ①先挑門後面不是廁所/臥室的
            for clear in DOOR_CLEAR_STEPS:          # 先求舒適淨距,不行再放寬
                for pos in cands:
                    a, b = pos - ENTRY_WIDTH / 2, pos + ENTRY_WIDTH / 2
                    if a < 0 or b > length:
                        continue
                    if not all(b < t0 or a > t1 for t0, t1 in taken):  # 撞洞口
                        continue
                    if not _door_pos_ok(spec, w, pos, ENTRY_WIDTH, clear):
                        continue                    # 卡在房間角落
                    if good_room_only and not _entry_room_ok(spec, w, pos):
                        continue                    # 一進門就是廁所/臥室
                    w.openings.append(Opening(pos, ENTRY_WIDTH, "door"))
                    spec.doors.append(DoorPlacement(
                        wi, len(w.openings) - 1, Door(hinge="left", swing="in")))
                    return
        return                                      # 這面牆塞不下就算了(罕見)


def _add_garage_doors(spec, bx0, by0, bx1, entry_frac=0.22):
    """1F 臨路南牆:**捲門**(車進出)+ 人行大門(牆長塞得下才有)。

    捲門平面上用**橫拉門的畫法**(門扇貼牆、不畫開門弧)——捲門是往上捲的,本來
    就沒有開啟弧線;但字樣要寫「捲門」不是「拉門」,看圖的人才知道那是鐵捲門。

    ⚠️ 人行門塞不下時**捲門就是大門**,這不是偷懶:
        牆角淨距 350×2 + 捲門 2500 + 牆垛 600 + 大門 1000 = 4800mm,
    4.5m 面寬的街屋(參考平面圖那張)放不下,真實做法是在捲門上開一扇小門 ——
    平面上仍是同一個洞口。`no_entry` 認的是「外牆上有門」,捲門本身就滿足。
    """
    for wi, w in enumerate(spec.walls):
        (sx, sy), (ex, ey) = w.start, w.end
        if abs(sy - by0) > 50 or abs(ey - by0) > 50 or abs(sx - ex) < 1:
            continue                                # 只找南向水平外牆
        # 車庫佔滿整個前段 → 這面牆上原本推出來的窗全部拿掉(車庫不必採光,而且
        # 那些窗會擋住捲門的位置)。
        _remove_openings(spec, {(wi, oi) for oi in range(len(w.openings))})
        length = w.length
        avail = length - 2 * DOOR_CORNER_CLEAR
        gw = min(GARAGE_DOOR_W, avail)
        if gw < GARAGE_DOOR_MIN_W:                  # 這面牆連車都開不進來
            return
        if avail >= gw + WINDOW_PIER_MIN + ENTRY_WIDTH:
            # 兩個洞口都放得下:人行門靠 entry_frac 那一側,捲門佔另一側。
            if entry_frac < 0.5:
                p_pos = DOOR_CORNER_CLEAR + ENTRY_WIDTH / 2
                g_pos = length - DOOR_CORNER_CLEAR - gw / 2
            else:
                p_pos = length - DOOR_CORNER_CLEAR - ENTRY_WIDTH / 2
                g_pos = DOOR_CORNER_CLEAR + gw / 2
        else:
            g_pos, p_pos = length / 2.0, None
        w.openings.append(Opening(g_pos, gw, "door"))
        spec.doors.append(DoorPlacement(wi, len(w.openings) - 1,
                                        Door(sliding=True, label="捲門")))
        if p_pos is not None:
            w.openings.append(Opening(p_pos, ENTRY_WIDTH, "door"))
            spec.doors.append(DoorPlacement(
                wi, len(w.openings) - 1, Door(hinge="left", swing="in")))
        return


DOOR_TOUCH_TOL = 60.0           # 門洞中心離房間邊界多近算「這扇門開在這間房上」
# 門洞兩端離「垂直方向的牆(房間角落)」的最小淨距(mm)。門擠在角落時,人走不進
# 那個角(房內可站區是牆內縮半個通行寬,角落구域接不上門)→ 動線檢查會判不通。
DOOR_CORNER_CLEAR = 350.0
# 門洞離「階梯本體」的最小淨距:門必須開在起步平台那一側,不能開在梯段旁邊
# (從側面開門一樣是一腳踩在階梯上)。
STAIR_DOOR_CLEAR = 600.0
# 這些房間不能當「穿堂」:已經有門就不再為了連通多開一扇(浴廁只留一扇門)。
_PRIVATE_KINDS = {"bathroom", "toilet"}
# 開門位置候選:先試慣用比例(置中/三七分),再沿整段牆等距掃描——牆上有階梯或
# 牆角要避開時,只試固定幾個點會找不到位置(前後段就會被判成走不通)。
_DOOR_FRACS = (0.5, 0.35, 0.65, 0.25, 0.75, 0.45, 0.55,
               *(i / 24.0 for i in range(1, 24)))
# 分級退讓:先求舒適淨距,擺不下就放寬;最後一級是「動線仍走得通」的物理下限
# (房內可站區是牆內縮半個通行寬,門離角落太近就接不上那塊 → 判動線不通)。
DOOR_CLEAR_STEPS = (350.0, 250.0, 150.0, 100.0)
DOOR_CORNER_MIN = 90.0          # 檢查器判定「卡死在角落」的門檻(動線下限 ~67mm)


def _stair_free_spans(spec, wall, lo, hi):
    """牆段 [lo,hi](沿牆方向的世界座標)扣掉「梯段正對的那幾段」→ 可用區間。

    ⚠️ 這是「樓梯間的門開在哪」的關鍵:樓梯間只有起步平台那一小段開得了門,靠
    `_DOOR_FRACS` 等距掃描是**碰運氣**(合法窗口常常只有幾十 mm,掃描點差一點就
    全落在階梯上 → 整層判成樓梯沒有門)。這裡直接算出可用區間、拿中點當首選。"""
    (sx, sy), (ex, ey) = wall.start, wall.end
    vertical = abs(sx - ex) < 1.0
    spans = [(min(lo, hi), max(lo, hi))]
    for b in _stair_boxes(spec):
        bx0, by0, bx1, by1 = b.bounds
        if vertical:
            if not (bx0 - STAIR_DOOR_CLEAR < sx < bx1 + STAIR_DOOR_CLEAR):
                continue                       # 這座梯不在門前的可站空間裡
            t0, t1 = by0, by1
        else:
            if not (by0 - STAIR_DOOR_CLEAR < sy < by1 + STAIR_DOOR_CLEAR):
                continue
            t0, t1 = bx0, bx1
        out = []
        for a, c in spans:
            if t1 <= a or t0 >= c:
                out.append((a, c))
                continue
            if t0 > a:
                out.append((a, t0))
            if t1 < c:
                out.append((t1, c))
        spans = out
    return sorted(spans, key=lambda t: -(t[1] - t[0]))


# 門前探測:從牆中心線往房內量這麼遠,問「那個位置的門前面站得住人嗎」。
DOOR_FRONT_PROBE = 400.0


def _stair_room_areas(spec):
    """裝了梯段的房間 → [(房間多邊形, 主可走區)]。沒有樓梯就回空。

    主可走區 = 房間扣掉梯段、再侵蝕半個通行寬之後**最大**的那一塊 —— 判準與
    `room_circulation` 的「主動線空間」是同一條(同一件事不要兩把尺)。"""
    from shapely.ops import unary_union

    from src.design.layout.room_circulation import PASSAGE_WIDTH, _components_of

    boxes = _stair_boxes(spec)
    if not boxes:
        return []
    key = (len(getattr(spec, "rooms", []) or []), len(boxes))
    cached = getattr(spec, "_nh_stair_areas", None)
    if cached is not None and cached[0] == key:
        return cached[1]
    out = []
    for r in getattr(spec, "rooms", []) or []:
        try:
            poly = Polygon(r.points)
        except Exception:
            continue
        obs = [b for b in boxes if b.intersection(poly).area > 1.0]
        if not obs:
            continue
        free = poly.difference(unary_union(obs))
        comps = _components_of(free.buffer(-PASSAGE_WIDTH / 2))
        out.append((poly, comps[0] if comps else None))
    try:
        spec._nh_stair_areas = (key, out)
    except Exception:
        pass
    return out


def _door_front_walkable(spec, wall, m, areas) -> bool:
    """牆上位置 m 的門,**兩邊**的地都走得到嗎?

    ⚠️ 這是「樓梯把樓梯間切成兩半」那個坑的解(2026-08-28,使用者指著 7×12 的圖
    說「一定要走過廁所才能到廚房」)。`_stair_free_spans` 只扣掉**正對梯段**的那
    幾段,剩下的一律當成可用 —— 但梯段把樓梯間切開之後,牆的其他段面對的是
    **繞不過去的死角**:預設核的浴廁北邊那塊,三面是浴廁/梯段/外牆,只有從後段
    開門進得去。門開在那裡,前段與後段就此走不通。

    實測 96 個樓層有 **41 個**這樣(使用者那張 7×12 正是其中之一),而兩道關卡
    都看不見:`floor_split` 一間房算**一個**節點(被自己的樓梯切兩半仍算「同一
    塊」),`room_circulation` 的障礙**只有家具**(看不見樓梯)。規則在,但都問
    錯了問題 —— 本檔這個坑的第 N 則。

    判準只認「有樓梯的房間」:其餘的牆前面沒有梯段,本來就不受這條限制。"""
    if not areas:
        return True
    (sx, sy), (ex, ey) = wall.start, wall.end
    vertical = abs(sx - ex) < 1.0
    mx, my = (sx, m) if vertical else (m, sy)
    from src.design.layout.room_circulation import PASSAGE_WIDTH
    reach = PASSAGE_WIDTH / 2 + 80.0
    for poly, main in areas:
        for s in (1.0, -1.0):
            p = Point(mx + (DOOR_FRONT_PROBE * s if vertical else 0.0),
                      my + (0.0 if vertical else DOOR_FRONT_PROBE * s))
            if not poly.contains(p):
                continue
            if main is None or main.distance(p) > reach:
                return False
    return True


def _door_candidates(spec, wall, lo, hi):
    """開門位置候選(世界座標,沿牆方向):先梯段沒擋住那幾段,再等距掃描。

    每段給三個點:中點 + **緊貼兩端**。貼端點很重要——樓梯間的合法窗口常常只有
    幾十 mm(門要同時「北端不碰到第一階」且「南端離牆角夠遠」),那個位置一定
    在可用段的端點上,中點會差幾十 mm 就開不成。

    ⚠️ 最後再把「門前面走不到的位置」排到隊尾(`_door_front_walkable`)——
    **排序不是過濾**:真的只剩死角可開時,一扇開在死角的門仍然勝過沒有門
    (加分項不得讓原本生得出來的案子生不出來)。"""
    half = INTERIOR_DOOR_WIDTH / 2.0
    pts = []
    for a, c in _stair_free_spans(spec, wall, lo, hi):
        if c - a < INTERIOR_DOOR_WIDTH:
            continue
        pts += [(a + c) / 2, c - half, a + half]
    pts += [min(lo, hi) + abs(hi - lo) * f for f in _DOOR_FRACS]
    areas = _stair_room_areas(spec)
    return sorted(pts, key=lambda m: not _door_front_walkable(spec, wall, m, areas))


def _room_edge_span(room_poly, wall, along_start):
    """牆與這間房相鄰的那一段在牆上的範圍 → (lo, hi) 沿牆座標;不相鄰回 None。"""
    (sx, sy), (ex, ey) = wall.start, wall.end
    rx0, ry0, rx1, ry1 = room_poly.bounds
    if abs(sx - ex) < 1.0:                              # 垂直牆
        if not (abs(sx - rx0) < DOOR_TOUCH_TOL or abs(sx - rx1) < DOOR_TOUCH_TOL):
            return None
        lo, hi = max(min(sy, ey), ry0), min(max(sy, ey), ry1)
    elif abs(sy - ey) < 1.0:                            # 水平牆
        if not (abs(sy - ry0) < DOOR_TOUCH_TOL or abs(sy - ry1) < DOOR_TOUCH_TOL):
            return None
        lo, hi = max(min(sx, ex), rx0), min(max(sx, ex), rx1)
    else:
        return None
    if hi - lo <= 0:
        return None
    return abs(lo - along_start), abs(hi - along_start)


def _stair_boxes(spec) -> list:
    """各座樓梯**不能當地板走**的矩形。

    起步平台在 origin 之前(見 _stair),那是這一層的地板高度,門開在正對它的牆上
    是合理的(真實透天的梯廳門就是這樣開的),所以不算。

    ⚠️ **折返平台要算**(2026-08-27,使用者指著 4.5m 的圖說「沒有路可以到廚房」)。
    這裡原本寫「折返端平台是一塊站得住人的平地」—— 那是把**起步**平台的道理套到
    **折返**平台上。折返梯是「爬上去、轉個彎、再爬上去」,中間那塊平台在
    **半層高**(9 階 × 188 ≈ 1.69m):

        踏step  y 8050~10390   從 0 爬到 +1.69m
        折返平台 y 10390~11400  ← 整塊在 +1.69m

    門開在那一段,後面是一個 1.1m 深、頭頂 1.69m 的凹洞,人走不出去。實測 60 案
    有 45 案的門這樣開,16 案因此前後段完全走不通 —— 所以**梯段連同折返平台
    整塊**都算障礙。(以前還要跟單跑直梯分流:直梯只有兩端、兩端都是樓層地板,
    只算踏step 那一段。梯型統一成折返梯之後那支拿掉了。)
    """
    from shapely.geometry import box
    out = []
    for st in getattr(spec, "stairs", []) or []:
        try:
            ox, oy = st.origin
            w = st.width
            # 折返梯:折返平台在半層高,和梯跑一起算成障礙 → 整個 st.length。
            run = float(st.length)
        except Exception:
            continue
        d = getattr(st, "direction", "north")
        far_is_low = d in ("south", "west")          # s=0 在座標大的那一端
        if d in ("north", "south"):
            y0 = oy + (st.length - run) if far_is_low else oy
            out.append(box(ox, y0, ox + w, y0 + run))
        else:
            x0 = ox + (st.length - run) if far_is_low else ox
            out.append(box(x0, oy, x0 + run, oy + w))
    return out


def _flight_sides(stair):
    """梯段兩條**長邊**(人走上去時的左右側)→ [((x,y),(x,y)), ...] 世界座標線段。

    另回行進方向是不是南北向,讓呼叫端知道要往哪一軸補牆。"""
    try:
        ox, oy = stair.origin
        w, ln = stair.width, stair.length
    except Exception:
        return [], True
    vertical = getattr(stair, "direction", "north") in ("north", "south")
    if vertical:
        x0, y0, x1, y1 = ox, oy, ox + w, oy + ln
        return [((x0, y0), (x0, y1)), ((x1, y0), (x1, y1))], True
    x0, y0, x1, y1 = ox, oy, ox + ln, oy + w
    return [((x0, y0), (x1, y0)), ((x0, y1), (x1, y1))], False


# 牆面離梯段這麼近就算「靠著牆」。梯段是離牆**中心線** WALL_GAP(75)擺的,內牆只有
# 120 厚(半厚 60)→ 牆面與梯段之間本來就有 ~15mm 的縫,那不叫沒牆。
WALL_TOUCH_GAP = 100.0


def _side_is_walled(spec, seg, tol: float = 50.0) -> bool:
    """這條邊有沒有牆貼著(牆體覆蓋整段)。"""
    from shapely.geometry import LineString
    line = LineString(seg)
    for w in spec.walls:
        body = LineString([w.start, w.end]).buffer(
            w.thickness / 2.0 + WALL_TOUCH_GAP, cap_style=2, join_style=2)
        if line.intersection(body).length >= line.length - tol:
            return True
    return False


def _add_stair_guard_walls(spec) -> int:
    """梯段旁邊沒有牆就補一道「導牆」——人走在梯段上,旁邊是空的會掉下去。

    只補**梯段(含折返平台)**那一段,起步平台不補:那裡是平地,而且要開口讓人走
    進來。導牆往樓梯間盡端延伸 WALL_GAP 與端牆接成 T 字,起步端留開口(=梯廳)。
    兩條產線共用(規則版窄透天 / AI 關係圖版):梯段填滿樓梯間時本來就兩側靠牆,
    這裡查得到牆就不補,所以呼叫兩次也不會多畫。回補了幾道牆。"""
    from src.drafting.wall import Wall

    added = 0
    for st in getattr(spec, "stairs", []) or []:
        sides, vertical = _flight_sides(st)
        if not sides:
            continue
        far_is_low = getattr(st, "direction", "north") in ("south", "west")
        for (p0, p1), sgn in zip(sides, (-1, +1)):
            if _side_is_walled(spec, (p0, p1)):
                continue                        # 已經靠著牆(外牆/隔間牆)
            half = GUARD_WALL_T / 2.0
            if vertical:                        # 上樓往南北 → 導牆是垂直牆
                cx = p0[0] + sgn * half
                lo = p0[1] - (WALL_GAP if far_is_low else 0.0)
                hi = p1[1] + (0.0 if far_is_low else WALL_GAP)
                start, end = (cx, lo), (cx, hi)
            else:                               # 上樓往東西 → 導牆是水平牆
                cy = p0[1] + sgn * half
                lo = p0[0] - (WALL_GAP if far_is_low else 0.0)
                hi = p1[0] + (0.0 if far_is_low else WALL_GAP)
                start, end = (lo, cy), (hi, cy)
            wall = Wall(start=start, end=end, thickness=GUARD_WALL_T,
                        openings=[])
            wall.stair_guard = True             # 開門時要避開它(見 _door_clear_of_stairs)
            spec.walls.append(wall)
            added += 1
    return added


def _stair_guard_bodies(spec) -> list:
    """導牆的牆體(門前站人的空間不能被它擋住)。"""
    from shapely.geometry import LineString
    return [LineString([w.start, w.end]).buffer(w.thickness / 2.0,
                                                cap_style=2, join_style=2)
            for w in spec.walls if getattr(w, "stair_guard", False)]


def _door_approach_rects(wall, pos: float, width: float, depth: float):
    """門「兩側各一塊站人的空間」= 門寬 × depth 深的矩形(世界座標)。"""
    from shapely.geometry import box
    (sx, sy), (ex, ey) = wall.start, wall.end
    px, py = wall.point_at(pos)
    half = width / 2.0
    if abs(sx - ex) < 1.0:                       # 垂直牆 → 往東西兩側站
        return [box(px, py - half, px + depth, py + half),
                box(px - depth, py - half, px, py + half)]
    return [box(px - half, py, px + half, py + depth),      # 水平牆 → 往南北兩側
            box(px - half, py - depth, px + half, py)]


def _door_clear_of_stairs(spec, wall, pos: float,
                          width: float = INTERIOR_DOOR_WIDTH) -> bool:
    """開門後踩得到平地嗎?

    判準不是「離階梯多遠」,而是**門前面那塊站人的空間有沒有被階梯佔掉**——門開在
    梯段旁 1m 通道的正中是可以的(人站在通道上),但門正對著踏step 就不行。
    梯段側邊的導牆同理:門扇被那道牆擋掉一半也等於走不進去。"""
    boxes = _stair_boxes(spec) + _stair_guard_bodies(spec)
    if not boxes:
        return True
    for rect in _door_approach_rects(wall, pos, width, STAIR_DOOR_CLEAR):
        if any(rect.intersection(b).area > 10000.0 for b in boxes):
            return False                          # 這一側是階梯,站不住
    return True


def _door_pos_ok(spec, wall, pos: float, width: float,
                 clear: float = DOOR_CORNER_CLEAR) -> bool:
    """這個門洞位置合不合格:對**兩側每一間房**都要離房間角落 ≥ clear。

    這條規則所有開門路徑共用(前門/補門/接通用門),避免「門卡在牆角、人走不進去」
    ——那正是動線檢查會判不通、但看圖不明顯的錯誤。"""
    if not _door_clear_of_stairs(spec, wall, pos, width):   # 門前要站得住人
        return False
    (sx, sy), (ex, ey) = wall.start, wall.end
    along_start = sy if abs(sx - ex) < 1.0 else sx
    px, py = wall.point_at(pos)
    a, b = pos - width / 2, pos + width / 2
    for room in spec.rooms:
        poly = Polygon(room.points)
        if poly.exterior.distance(Point(px, py)) > DOOR_TOUCH_TOL:
            continue                                    # 這扇門不在這間房的邊界上
        span = _room_edge_span(poly, wall, along_start)
        if span is None:
            continue
        lo, hi = min(span), max(span)
        if a < lo + clear - 1e-6 or b > hi - clear + 1e-6:
            return False                                # 太靠近這間房的角落
    return True
# 需要對外採光通風的房間(補窗保證的對象);走道/樓梯/儲藏/豎井不強制。
WINDOW_KINDS = {"living", "dining", "kitchen", "bedroom", "master_bedroom",
                "study", "elder_room", "bathroom"}


# 補窗參數。單一扇窗的下限/上限與離牆角、離其他洞口的淨距(mm)。
WINDOW_MIN_W, WINDOW_MAX_W = 600.0, 3600.0
# 最後一小段的例外下限:§40 只差幾公分,牆上卻沒有 60cm 的空檔了 —— 這時開一扇窄
# 高窗把它補滿,比整間房被判違規好(真實圖面也有 45~60cm 的小窗/高側窗)。
WINDOW_LAST_MIN = 450.0
WINDOW_PIER_MIN = 600.0         # 兩個洞口之間的牆垛至少這麼寬(結構;細長牆垛會裂)
WINDOW_MARGIN = 20.0            # 補窗時多給的餘裕(避免「剛好等於法定」被浮點誤差判掉)
WINDOW_EDGE_CLEAR, WINDOW_GAP = 300.0, 200.0


def _window_segments(spec, room, bx0, by0, bx1, by1, party_walls):
    """這間房可以開窗的牆段 → [(rank, wi, lo, hi, along)],rank 小的優先。

    rank 0 = 真正的採光面(對外牆);rank 1 = 天井側(舊圖仍可能有天井)。內牆不列。"""
    from src.design.layout.bsp_layout import MIN_EDGE_FOR_WINDOW
    patios = [Polygon(r.points) for r in spec.rooms if r.kind == "patio"]
    poly = Polygon(room.points)
    rx0, ry0, rx1, ry1 = poly.bounds
    out = []
    for wi, w in enumerate(spec.walls):
        (sx, sy), (ex, ey) = w.start, w.end
        if abs(sx - ex) < 1.0:                          # 垂直牆(東西向外牆)
            if not (abs(sx - rx0) < 60 or abs(sx - rx1) < 60):
                continue
            lo, hi, along = max(min(sy, ey), ry0), min(max(sy, ey), ry1), sy
            on_ext = (not party_walls
                      and (abs(sx - bx0) < 60 or abs(sx - bx1) < 60))
            mid_pt = lambda m: (sx, m)                  # noqa: E731
        elif abs(sy - ey) < 1.0:                        # 水平牆(前後外牆)
            if not (abs(sy - ry0) < 60 or abs(sy - ry1) < 60):
                continue
            lo, hi, along = max(min(sx, ex), rx0), min(max(sx, ex), rx1), sx
            on_ext = abs(sy - by0) < 60 or abs(sy - by1) < 60
            mid_pt = lambda m: (m, sy)                  # noqa: E731
        else:
            continue
        if hi - lo < MIN_EDGE_FOR_WINDOW:
            continue
        if on_ext:
            rank = 0
        else:
            mx, my = mid_pt((lo + hi) / 2)              # 牆另一側是不是天井?
            near = [Point(mx + dx, my + dy)
                    for dx, dy in ((250, 0), (-250, 0), (0, 250), (0, -250))]
            if any(p.contains(q) for p in patios for q in near):
                rank = 1
            else:
                continue                                # 內牆:開窗無意義
        out.append((rank, wi, lo, hi, along))
    out.sort(key=lambda t: (t[0], -(t[3] - t[2])))      # 先真採光面,再長邊
    return out


COLUMN_CLEARANCE = 300.0        # 洞口與柱面的最小淨距(對齊 layout_generator 的檢核)

# 軸網要離建築端點多遠才算「中間的橫牆」(太靠邊的就是外牆本身,不是可吸附的目標)
GRID_EDGE_MARGIN = 300.0


def _fit_margin(build, prefer=None, margins=None):
    """替外牆柱留位置,**留不滿就少留一點**(與兩帶式 `_house_frame` 同一套)。

    ⚠️ 鐵則:留柱位不得讓原本生得出來的案子生不出來。窄透天的下限很緊
       (面寬 4.0m 起跳),留滿 275mm 會直接把最窄的那批擠掉,所以做成退讓階梯,
       一路退到 0 時行為與改動前完全一致。

    `margins`:要試的級數清單(預設整條階梯)。呼叫端可以先用**幾何**篩掉
    「留了就沒有走道」的級數 —— 那些級數蓋出來一定是走不通的圖,蓋完再退等於
    白蓋十一次(見 `generate_narrow_building._margin_steps`)。

    `prefer`:除了「蓋不蓋得出來」以外的第二個判準(蓋得出來但不合意就繼續退)。
    ⚠️ 留柱位是**加分項**,它把建築縮小 —— 縮掉的那 250mm 可能正好讓使用者指定
       的那款核排不下(實測 4.0×11.53m:4000 → 3500,橫置樓梯就跑不完了,於是
       靜靜地退回預設核,使用者要的格局憑空消失)。所以**先只收 prefer 過得了
       的那幾級**,整條階梯都不合意才退而求其次。這跟門的
       `_door_front_walkable` 是同一個兩段式:**排序不是過濾** —— 沒有合意的
       時候,一張留不滿柱位的圖仍然勝過沒有圖。
    """
    from src.design.layout_generator import STRUCT_MARGIN, STRUCT_MARGIN_STEP

    if margins is None:
        margins = [float(m) for m in
                   range(int(STRUCT_MARGIN), -1, -STRUCT_MARGIN_STEP)]
    last: Exception | None = None
    fallback = None
    # ⚠️ **先探最寬的那一級**(留最少柱位 = 建築最寬)。留柱位只會把建築縮小,
    #    而 `prefer` 問的是「指定的那款核排不排得下」—— 最寬都排不下,更窄的
    #    級數一定也排不下,整條階梯不必再跑。不做這個探針的話,3.8m 面寬指名
    #    `ref`/`mid` 會把 12 級 × 天井/浴廁翻面/壓服務格的組合全蓋一次,實測
    #    單一尺寸超過 10 分鐘(合意時只多蓋一次,不合意時省下 11 次)。
    #    (階梯本來就從最寬那級開始時不必多探一次 —— 迴圈第一輪就是它。)
    if (prefer is not None and len(margins) > 1
            and float(margins[0]) != float(min(margins))):
        try:
            probe = build(float(min(margins)))
        except ValueError as exc:
            last = exc
        else:
            if not prefer(probe):
                return probe
    for m in margins:
        try:
            got = build(float(m))
        except ValueError as exc:
            last = exc
            continue
        if prefer is None or prefer(got):
            return got
        if fallback is None:                    # 留著:整條都不合意時的退路
            fallback = got
    if fallback is not None:
        return fallback
    raise last


#: 核的排法退讓階梯:一款排不下(或排下去房間變太大)就換下一款。
#: 順序是「離使用者要的那張參考平面由近到遠」——`ref` 是他自己畫的方案 B,
#: `mid` 至少保住「廁所的門開在走道上」,`default` 是原本的排法。
# ⚠️ `"zone3"`(三區版)**還沒進這條階梯**:它的廁所要疊在樓梯下方,那部分還沒
#    做完,現在挑到它會生出「沒有廁所」的房子。做完再排到最前面。
CORE_STYLE_STEPS = ("ref", "mid", "default")


def _fit_core_style(build, want: str):
    """**整棟**層級的退讓:要的那款核讓房間變太大就換一款(使用者 2026-08-31:
    「我要把每一個尺寸都設計成類似這個格式」)。

    ⚠️ 鐵則第九次登場:**加分項不得讓原本好好的東西壞掉**。參考圖版的核有一個
    先天限制 —— 走道是它唯一的動線,而走道只有**一扇門寬**,所以前/後段切不成
    兩間(兩間房的門都得開在那一段牆上)。6.5~8m 面寬又深的案子因此會生出
    30~43㎡ 的臥室(實測 12 個尺寸 `room_oversize` 26 件,預設核只有 7 件)。
    一張漂亮的核不值得拿一間 42㎡ 的臥室去換。

    判準是**實際蓋出來量過**(與 `_fit_service` / `_fit_patio` / `_fit_bath_side`
    同一條):蓋得出來、沒有硬錯誤、而且沒有「房間過大」才算數;都不乾淨時取
    「房間過大」最少的那款,同分取最接近使用者要的那款(所以順序有意義)。

    ⚠️ 這是**整棟**的決定,不是各層各自決定 —— 核每層同構,樓梯才對得齊。
    ⚠️ 只往「離參考平面更遠」的方向退,不會反過來把 `default` 升級成 `ref`:
       明講要 `default` 的呼叫端(既有測試、`preview_plan.py` 不加開關)行為
       完全不變。
    """
    from src.design.layout.plan_check import check_building

    try:
        steps = CORE_STYLE_STEPS[CORE_STYLE_STEPS.index(want):]
    except ValueError:                       # 不認得的款式 → 照原樣蓋,不多事
        return build(want)
    best = None
    for style in steps:
        try:
            got = build(style)
        except ValueError:
            continue
        rep = check_building(got)
        n_over = sum(1 for i in rep.warnings if i.code == "room_oversize")
        if not rep.errors and not n_over:
            return got
        rank = (bool(rep.errors), n_over)
        if best is None or rank < best[0]:
            best = (rank, got)
    if best is None:
        return build(want)                   # 一款都蓋不出來 → 讓原本的例外冒出來
    return best[1]


def _fit_core_reach(build):
    """**整棟**層級的退讓:量到「有門走不到」就換一種核的排法,重蓋整棟。

    死角來自「浴廁在南、服務格北端的空格併進樓梯間(`absorb_spare`)」——北端是
    梯段的盡頭(折返平台在半層高、直梯也只剩一道 75mm 的牆縫),繞不過去。
    兩級退讓:

      ① `force_bath_north` 把浴廁翻到北邊 → 空格跟著落到南端,而南端是樓梯的
         **起步平台**(這一層的地板高度),走得到 → 樓梯間照樣吃得下,沒有任何
         一間房要因此變大。
      ② 翻不動才 `allow_skinny_spare`,把空格併進居室(那間房細長一點是
         warning,一間走得到的浴室是 error)。

    ⚠️ **這是整棟的決定,不是各層各自決定**(與 `_fit_service` / `_fit_margin` /
    `_fit_patio` 同一個道理):核每層同構、樓梯才對得齊。第一版寫在 `_build_floor`
    裡,結果 4.5×14.4m 的 2F 翻了、1F 沒翻,**核就不同構了**
    (`test_core_is_identical_on_every_floor` 抓到的就是這個)。

    ⚠️ 判準是 `plan_check` 量出來的**硬錯誤**,而且要等到最後一次修門之後 ——
    修門之前問症狀太早(門還開在別處,看起來一切正常),問幾何又太早退
    (沒事的案子也退,連帶弄丟流理台/淋浴間/床頭櫃)。見 AGENTS.md。
    """
    from src.design.layout.plan_check import check_building

    first = None
    for bath_north, skinny in ((False, False), (True, False), (False, True)):
        try:
            floors = build(bath_north, skinny)
        except ValueError:
            continue
        if not check_building(floors).errors:
            return floors
        first = floors if first is None else first
    if first is None:
        raise ValueError("窄透天骨架:核的三種排法都蓋不出來")
    return first


def _fit_patio(build):
    """開天井是**加分項**:開了如果出硬錯誤就不開(與 `_fit_service` 同一條鐵則)。

    ⚠️ 天井會讓 `_core_widths` **跳過浴廁退讓**(服務格一窄,天井就小到 code_check
    不認,等於白開)—— 但那個退讓正是窄面寬唯一擠得出走道的手段。3.6m 面寬開天井
    因此讓 1F **斷成兩塊**(客廳|浴廁|樓梯間 / 餐廚,餐廚進不去)。

    ⚠️ 本檔原本把這件事寫成「拿走道換採光」的設計取捨 —— 那句話對 4.5m 以上成立,
       對 3.6m 不成立:沒了走道那一層根本走不通,那是廢圖,不是取捨。
       **加分項不得讓原本好好的東西壞掉**(這條在本檔已經第八次登場)。

    ⚠️ 判準是 `plan_check` 有沒有**硬錯誤**,不是有沒有 raise —— 這個案子蓋得出來,
       只是圖不合格;只看例外的話這道退讓完全不會啟動(選配版踩過同一個坑)。
    ⚠️ 天井貫穿到屋頂,開不開是**整棟**的決定,不是各層各自決定,所以包在最外層。
    """
    from src.design.layout.plan_check import check_building

    floors = build(True)
    if not check_building(floors).errors:
        return floors
    try:
        return build(False)                         # 不開天井再蓋一次
    except ValueError:                              # 不開反而排不下 → 維持原樣
        return floors


def _fit_patio_auto(build):
    """**浴廁會變暗房才開天井**,開了如果更糟就不開(使用者 2026-09-03:「開天井」)。

    NG06「套房與街屋潛藏暗房危機」(使用者給的〈9 種常見 NG 格局〉):狹長街屋
    只有前後採光,中段核的浴廁/樓梯間夾在建築正中間 —— 側面是共同壁,開不了窗,
    書上的解法只有拆牆、採光罩/天井、玻璃隔間三種,而**我們實作了的只有天井**。

    實測(3 層,`bath_no_window` 當尺):

        4.0~6.0m 面寬 → 自動挑到 `ref` 核,那款自己帶天井 → 浴室是亮的
        6.5~8.0m 面寬 → 退到 `default`(為了房間不要太大)→ **每棟 6 間暗房**
                        開天井後 → **0 間**,硬錯誤 0、`room_oversize` 不增反減

    ⚠️ **不是無條件開**:天井貫穿到屋頂,每層固定花掉約 3㎡,而且會讓
    `_core_widths` 跳過浴廁退讓(窄面寬唯一擠得出走道的手段,見 `_fit_patio`)。
    所以判準三條缺一不可:①開了不能有硬錯誤 ②房間過大不能變多 ③**暗房要真的
    變少**。沒有暗房的案子連試都不試(省一輪重蓋)。

    ⚠️ 這是**整棟**的決定(與 `_fit_service` / `_fit_core_style` 同一族):
    天井貫穿到屋頂,各層各自決定的話核就不同構、樓梯對不齊。
    """
    from src.design.layout.plan_check import check_building

    def _score(floors):
        rep = check_building(floors)
        n = lambda code: sum(1 for i in rep.warnings if i.code == code)
        return (len(rep.errors), n("room_oversize"),
                n("no_cross_ventilation"), n("bath_no_window"))

    base = build(False)
    n_err, n_over, n_vent, n_dark = _score(base)
    if n_dark == 0:
        return base                              # 沒有暗房 → 不必花那 3㎡
    try:
        lit = build(True)
    except ValueError:
        return base                              # 開了排不下 → 維持原樣
    l_err, l_over, l_vent, l_dark = _score(lit)
    # 加分項不得讓別的東西變差(本檔鐵則):硬錯誤、房間過大、**通風對流**
    # 三項任何一項變多就退掉。⚠️ 通風那項是實測補上的 —— 7×15.5 開了天井之後
    # 浴廁採光 3 件 → 0,但 1F 的前後窗被服務格擠得對不上走道軸線,
    # `no_cross_ventilation` 0 件 → 1 件。**修好一條 NG、弄壞另一條不算修好。**
    if l_err > n_err or l_over > n_over or l_vent > n_vent or l_dark >= n_dark:
        return base
    return lit


def _fit_service(build):
    """壓窄服務格(`min_service`)是**加分項**,壓了反而出硬錯誤就不壓。

    壓窄之後樓梯間變寬,梯段、導牆與旁邊通道的相對位置跟著變 —— 實測
    6.8×17.2m 會讓樓梯間的動線走不通。**加分項不得讓原本生得出來的案子壞掉**
    (與 `_fit_margin` 留柱位同一條鐵則)。
    ⚠️ 每層必須用**同一個**決定,否則各層的核不同寬、樓梯不會上下對齊,
       所以判斷放在整棟這一層。
    """
    from src.design.layout.plan_check import check_building

    floors = build(True)
    if not check_building(floors).errors:
        return floors
    try:
        return build(False)
    except ValueError:                              # 不壓反而排不下 → 維持原樣
        return floors


def _fit_depth(build):
    """建築進深退讓階梯:採光補不滿就**收一級進深、多留一點院子**,再蓋一次。

    ⚠️ 為什麼不能只靠 `max_depth_for` 那張表:那是量了幾個面寬之後內插出來的
       起點估計,中間的尺寸不保證準。這個專案的規矩是「實際量過才算數」——
       表負責讓大多數案子一次就過(不花額外時間),這支負責兜底。
    ⚠️ 退讓要在**整棟**這一層做,不能讓每層各退各的:各層進深不一樣的話,上下
       樓的外牆就對不齊了(`_fit_margin` 不讓各層各留各的柱位,同一個道理)。
    ⚠️ 只往**收**的方向走,而且退無可退時回目前這一版 —— 生得出來的案子不能
       因為想讓圖更好看就變成生不出來。
    """
    from src.design.layout.code_check import check_code_floor
    from src.design.layout.plan_check import building_env

    floors = build(None)                            # 先照表估的上限蓋
    for _ in range(DEPTH_RETREAT_TRIES):
        if not any(i.code == "daylight_area"
                   for label, spec in floors
                   for i in check_code_floor(spec, label=label)):
            return floors
        x0, y0, x1, y1 = building_env(floors[0][1])
        cap = (y1 - y0) - DEPTH_RETREAT_STEP
        if cap < min_depth_for(x1 - x0):            # 退到最小進深就別再退了
            break
        try:
            floors = build(cap)
        except ValueError:                          # 收窄之後反而排不下 → 收手
            break
    return floors


def _set_structural_grid(spec, bx0: float, by0: float,
                         W: float, D: float) -> None:
    """替窄透天/淺透天排結構軸網,柱放在軸網交點。

    ⚠️ 這兩條產線以前**根本沒有柱**(`column_centers=[]`、外框只當「單跨」記進
       格線)。一棟三層 RC 透天沒有柱是結構上不可能的,而且進深 13.5m 會變成
       單跨 —— 專案自己的原則是經濟跨距 6~9m(`BAY_SPAN_LIMITS`)。
       使用者 2026-08-10:「每個房子尺寸的柱都這樣設計」。

    做法:直接重用兩帶式的 `_plan_x_grid`(它是**軸向無關**的:給原點、長度、
    可吸附的主要牆位)。面寬 3.5~9m 本來就在單跨範圍內,所以 X 只有兩條外牆
    軸線;進深由中間的**橫牆**撐出 2~3 跨,軸線吸附到牆上 → 柱天生坐在牆交點、
    藏在牆內,不會孤零零站在房間中間。

    `column_centers=None` = 「放在每個軸網交點」(與集合住宅/兩帶式同一套約定)。
    """
    from src.design.layout_generator import BAY_SPAN_LIMITS, _plan_x_grid

    def axis(origin: float, length: float, majors: list) -> list:
        """一個方向的跨距清單。

        ⚠️ `_plan_x_grid` 的 `BAY_RANGE` 下限是 **2 跨** —— 那是寬房子的假設。
           窄透天面寬 3.5~9m 本來就該是**單跨**(還在 9m 經濟跨距內),硬要它
           切兩跨會得到 2.5m 的跨距而被判不合格,整棟生不出來(5×12 實測)。
           所以長度塞得進一跨就直接單跨,長到放不下才叫規劃器切。
        """
        if length <= BAY_SPAN_LIMITS[1]:
            return [length]
        grid = _plan_x_grid(origin, length, majors)
        return [grid[i + 1] - grid[i] for i in range(len(grid) - 1)]

    # 可吸附的主要牆:X 向找縱牆的 x、Y 向找橫牆的 y(都只取建築中段的)。
    verts = sorted({round(w.start[0], 1) for w in spec.walls
                    if abs(w.start[0] - w.end[0]) < 1})
    horis = sorted({round(w.start[1], 1) for w in spec.walls
                    if abs(w.start[1] - w.end[1]) < 1})
    mx = [v for v in verts if bx0 + GRID_EDGE_MARGIN < v < bx0 + W - GRID_EDGE_MARGIN]
    my = [h for h in horis if by0 + GRID_EDGE_MARGIN < h < by0 + D - GRID_EDGE_MARGIN]

    spec.x_spacings = axis(bx0, W, mx)
    spec.y_spacings = axis(by0, D, my)
    spec.grid_origin = (bx0, by0)
    spec.column_centers = None                  # None = 放在每個軸網交點


def _column_centers(spec) -> list:
    """這份 spec 的柱心座標。

    與 apartment_plan.resolve_columns 同一套規則:column_centers 有給就用它
    (窄透天/淺透天給空清單 = 不放柱);給 None 代表「放在每個軸網交點」,
    要從 grid_origin + x/y_spacings 推回來。"""
    centers = getattr(spec, "column_centers", None)
    if centers is not None:
        return list(centers)
    xs = getattr(spec, "x_spacings", None) or []
    ys = getattr(spec, "y_spacings", None) or []
    ox, oy = getattr(spec, "grid_origin", (0.0, 0.0))
    gx, gy, acc = [ox], [oy], ox
    for d in xs:
        acc += d
        gx.append(acc)
    acc = oy
    for d in ys:
        acc += d
        gy.append(acc)
    return [(x, y) for x in gx for y in gy]


def _column_blocks(spec, wall, along, clear: float = COLUMN_CLEARANCE):
    """柱子在這道牆上佔掉的沿牆區間(洞口不能壓柱)。

    窄透天/淺透天沒有柱(column_centers 空),這裡就回空清單;兩帶式與集合住宅
    有柱,補窗時要避開——否則會被 validate_spec 判「洞口壓柱」而整份設計失敗。"""
    centers = _column_centers(spec)
    if not centers:
        return []
    size = float(getattr(spec, "column_size", 500.0) or 500.0)
    (sx, sy), (ex, ey) = wall.start, wall.end
    vertical = abs(sx - ex) < 1.0
    out = []
    for cx, cy in centers:
        off = abs(cx - sx) if vertical else abs(cy - sy)
        if off > wall.thickness / 2.0 + size / 2.0:     # 柱不在這道牆上
            continue
        c = cy if vertical else cx
        t = abs(c - along)
        # 淨距用 validate_spec 的同一個標準(柱半徑 + COLUMN_CLEARANCE),
        # 不能只留 WINDOW_GAP —— 那樣補完窗還是會被判「洞口壓柱」。
        out.append((t - size / 2.0 - clear - 10.0,
                    t + size / 2.0 + clear + 10.0))
    return out


def _free_intervals(wall, lo, hi, along, blocks=(), edge_clear=None):
    """牆段 [lo,hi](世界座標)扣掉既有洞口(與柱)→ 沿牆座標的可用區間 [(a,b)]。"""
    if edge_clear is None:
        edge_clear = WINDOW_EDGE_CLEAR
    a0, b0 = abs(lo - along), abs(hi - along)
    a0, b0 = min(a0, b0) + edge_clear, max(a0, b0) - edge_clear
    free = [(a0, b0)] if b0 > a0 else []
    # 兩扇**窗**之間的牆垛要 ≥600(細長牆垛結構上會裂);窗與門之間用一般淨距即可
    # ——3.5m 面寬的南牆上已經有一扇 1m 大門,若也要求 600 就再也開不出窗了。
    spans = [(op.position - op.width / 2
              - (WINDOW_PIER_MIN if op.kind == "window" else WINDOW_GAP),
              op.position + op.width / 2
              + (WINDOW_PIER_MIN if op.kind == "window" else WINDOW_GAP))
             for op in wall.openings] + list(blocks)
    for t0, t1 in spans:
        nxt = []
        for a, b in free:
            if t1 <= a or t0 >= b:
                nxt.append((a, b))
                continue
            if t0 > a:
                nxt.append((a, min(b, t0)))
            if t1 < b:
                nxt.append((max(a, t1), b))
        free = [(a, b) for a, b in nxt if b - a > 0]
    return sorted(free, key=lambda t: -(t[1] - t[0]))


def shift_openings_off_columns(spec) -> int:
    """把開在柱上的**門窗**沿著牆挪開。回挪了幾扇。

    ⚠️ 使用者 2026-08-19:「柱子怎麼還能放在窗戶裡?」——實測一張圖有 3 個窗被柱
    壓掉約 300mm(整根柱寬)。

    成因是**時序**,不是缺機制:躲柱的 `_column_blocks` 一直都在,但

      * 真正**開窗**的 `_fix_openings` 從來沒有呼叫它(只有「補窗/加寬窗」那段有);
      * 而且柱以前是在開口收尾**之後**才掛上 spec,那時查也是空的。

    門沒事,是因為柱定案後還有一次 `repair_doors` ——**窗沒有對應的第二次**。
    這支就是窗的那一次,要在柱定案之後、家具之前跑。

    做法只挪不縮:窗寬牽動 §40 採光(樓地板 1/8),縮窗會把採光問題變出來。
    挪不開就原地不動,交給圖面關卡回報,不要偷偷縮小。

    ⚠️ 2026-08-20 補上**門**(原本只做窗)。以為門有 `repair_doors` 兜底是錯的:
    那支修的是「門扇打開會撞到什麼」,門洞**本身**跨在柱上它不管 —— 柱在牆內、
    只凸出牆面 (柱寬−牆厚)/2,門開 90 度掃過的方塊常常閃得過去,於是
    `door_swing_blocked` 一聲不吭,圖上卻是柱穿過門框。實測 19×13 三層,
    1F 與 3F 各有一扇門被柱吃掉約 270mm。

    門比窗多一道限制:挪完仍要離房間角落夠遠(`_door_pos_ok`),否則換成
    `door_in_corner`,問題只是搬家。所以門在可用區間裡**逐點試**,
    找不到合格點就不動。
    """
    moved = 0
    for w in spec.walls:
        (sx, sy), (ex, ey) = w.start, w.end
        vertical = abs(sx - ex) < 1.0
        along = sy if vertical else sx           # 牆起點的沿牆座標
        lo, hi = (sy, ey) if vertical else (sx, ex)
        blocks = _column_blocks(spec, w, along)
        if not blocks:
            continue
        for op in list(w.openings):
            if op.kind not in ("window", "door"):
                continue
            # 開放通道(走道口)**只收邊、不平移**:它的兩端是結構決定的,整個
            # 挪開只會在界牆上留下一截凸出來的牆頭(使用者 2026-09-02 圈出來的
            # 就是這個)。但通道開到界牆中心線的話,角柱會凸進洞口 250mm ——
            # 那不是牆頭、是**柱**,收到柱面為止就對了(圖上通道就結束在柱邊,
            # 真實建築本來就是這樣)。
            if getattr(op, "is_passage", False):
                _trim_passage_off_columns(spec, w, op, along)
                continue
            a0, b0 = op.position - op.width / 2.0, op.position + op.width / 2.0
            if not any(t0 < b0 and a0 < t1 for t0, t1 in blocks):
                continue                          # 這扇沒壓到柱
            others = [o for o in w.openings if o is not op]
            # ⚠️ 柱淨距走退讓階梯:先求 COLUMN_CLEARANCE(300)的舒適淨距,牆上排不下
            #    就一級一級降到 0(= 只求「不重疊」)。少了這道階梯,一道排滿洞口的
            #    牆會因為「湊不出 300 淨距」整個放棄,柱就繼續留在窗框裡 ——
            #    貼著柱邊的窗雖然不漂亮,但至少蓋得出來,關卡也才過得了。
            new_pos = None
            for clear in COLUMN_CLEAR_STEPS:
                blk = _column_blocks(spec, w, along, clear)
                keep = list(w.openings)
                w.openings = others               # 算空間時要先把自己拿掉
                # ⚠️ **門不吃窗的邊界淨距**(`WINDOW_EDGE_CLEAR` 300)。門離牆兩端
                #    多遠是 `_door_pos_ok` 在管(門角淨距,分級退讓到 90),拿窗的
                #    300 去夾會讓短牆算出來的可用區間憑空少 600mm ——實測 1.3m 的
                #    牆只剩 700mm,850 的門怎麼算都塞不下,於是整支靜靜地不動,
                #    柱就繼續插在門框裡(4.5m 面寬 2F 固定吃 25mm)。
                free = _free_intervals(w, lo, hi, along, blk,
                                       edge_clear=0.0 if op.kind == "door"
                                       else None)
                w.openings = keep
                spans = [(a, b) for a, b in free if b - a >= op.width]
                if not spans:
                    continue                      # 這級淨距排不下 → 再讓一級
                new_pos = _nearest_ok_position(spec, w, op, spans)
                if new_pos is not None:
                    break
            if new_pos is None:
                # ⚠️ 擋住它的常常**不是柱,是旁邊那扇門** —— 一扇一扇挪永遠挪不
                #    出來,整排一起往旁邊推就排得下(見 `_repack_doors_on_wall`)。
                if _repack_openings_on_wall(spec, w, lo, hi, along):
                    moved += 1
                    break       # 整道牆重排過了,這道牆不必再逐扇試
                continue        # 真的挪不開 → 原地不動,交給關卡回報,不偷偷縮小
            op.position = new_pos
            moved += 1
    return moved


#: 前後窗與走道軸線至少要重疊這麼寬,風才算走得過去(mm)。
VENT_OVERLAP_MIN = 400.0


def _open_passage_span(spec):
    """走道口沿 x 的區間 (x0, x1);沒有開放通道回 None。

    ⚠️ 名字不要叫 `_passage_span` —— 本模組已經有一支同名的(算「走道有多寬」,
    給切點用),重名會把它整支換掉。同一天在 `plan_check._wall_bodies` 踩過
    一次一模一樣的:症狀出現在完全不相干的地方,很難連回來。"""
    lo, hi = None, None
    for w in spec.walls:
        for op in w.openings:
            if not getattr(op, "is_passage", False):
                continue
            cx = w.point_at(op.position)[0]
            a, b = cx - op.width / 2.0, cx + op.width / 2.0
            lo = a if lo is None else max(lo, a)
            hi = b if hi is None else min(hi, b)
    return None if lo is None or hi - lo < VENT_OVERLAP_MIN else (lo, hi)


def _align_windows_for_ventilation(spec, by0, by1) -> int:
    """把前後外牆的窗挪到**走道軸線上**,讓風有一條直線走得過去。回挪了幾扇。

    使用者 2026-09-03 給的〈9 種常見 NG 格局〉NG01:「風走最短直線,沒辦法像人
    一樣轉身」,要對流就得有兩個對外窗,而且**窗與窗之間拉直線不能被牆擋住**。

    連棟街屋只有前後兩面能開窗,中間又橫著一整個核 —— 唯一能讓直線通過的就是
    那條**兩頭全開的走道**。實測:窗沒對齊走道時,1F 每一個尺寸都沒有對流
    (南窗與走道只重疊 61mm、北窗完全沒重疊)。

    ⚠️ **只挪不縮、只在同一道牆上挪**:§40 採光算的是「那間房的開口總寬」,
       沿著同一道牆平移既不改寬度也不換房間(與 `_repack_openings_on_wall`
       同一個理由)。挪不到就維持原位 —— 加分項不得讓原本好好的東西壞掉。
    """
    span = _open_passage_span(spec)
    if span is None:
        return 0
    cx = (span[0] + span[1]) / 2.0
    moved = 0
    for wi, w in enumerate(spec.walls):
        (sx, sy), (ex, ey) = w.start, w.end
        if abs(sy - ey) > 1.0:                      # 只看前後(橫向)外牆
            continue
        if min(abs(sy - by0), abs(sy - by1)) > 60.0:
            continue
        for wp in spec.windows:
            if wp.wall_index != wi:
                continue
            op = w.openings[wp.opening_index]
            here = w.point_at(op.position)[0]
            if (min(here + op.width / 2, span[1])
                    - max(here - op.width / 2, span[0])) >= VENT_OVERLAP_MIN:
                continue                            # 本來就對得上
            lo, hi = min(sx, ex), max(sx, ex)
            others = [o for o in w.openings if o is not op]
            keep = list(w.openings)
            w.openings = others                     # 算空間時先把自己拿掉
            free = _free_intervals(w, lo, hi, sx,
                                   _column_blocks(spec, w, sx))
            w.openings = keep
            want = abs(cx - sx) if sx < ex else abs(sx - cx)
            best = None
            for a, b in free:
                if b - a < op.width:
                    continue
                pos = min(max(want, a + op.width / 2), b - op.width / 2)
                if best is None or abs(pos - want) < abs(best - want):
                    best = pos
            if best is None:
                continue
            op.position = best
            moved += 1
    return moved


def _slide_passage_door_off_columns(spec, wall, op, along, blocks) -> bool:
    """走道口的**門扇**閃柱:寬度保持一扇門,只在原本那段牆的空檔裡讓開。

    ⚠️ 不能沿用開放通道那條「收到柱面」—— 那條的下限是 `OPEN_PASSAGE_MIN`(750,
    沒有門扇,走得過去就好),而這是**居室門**,收到 800 以下 `plan_check` 的
    `room_door_narrow` 就擋圖(實測 5.45×15.45 收成 770,四個尺寸一起紅)。
    ⚠️ 也不能改走一般門的 `_nearest_ok_position`:走道只有一扇門寬,`_door_pos_ok`
    的牆角淨距永遠不可能滿足,那支會直接放棄,柱就留在門框裡。
    """
    from src.design.layout.door_rules import ROOM_DOOR_MIN

    (sx, sy), (ex, ey) = wall.start, wall.end
    vertical = abs(sx - ex) < 1.0
    lo, hi = (sy, ey) if vertical else (sx, ex)
    a, b = op.position - op.width / 2.0, op.position + op.width / 2.0
    if not any(t0 < b and a < t1 for t0, t1 in blocks):
        return False                                  # 這扇沒壓到柱
    keep = list(wall.openings)
    wall.openings = [o for o in keep if o is not op]  # 算空檔時先把自己拿掉
    try:
        free = _free_intervals(wall, lo, hi, along, blocks, edge_clear=0.0)
    finally:
        wall.openings = keep
    best = None                                       # (離原位多遠, -寬, pos, 寬)
    for a0, b0 in free:
        if b0 - a0 < ROOM_DOOR_MIN:
            continue
        w_new = min(op.width, b0 - a0)
        pos = min(max(op.position, a0 + w_new / 2.0), b0 - w_new / 2.0)
        cand = (abs(pos - op.position), -w_new, pos, w_new)
        if best is None or cand[:2] < best[:2]:
            best = cand
    if best is None:
        return False                # 讓不開 → 原地不動,交給關卡回報(不偷偷縮小)
    _d, _w, pos, w_new = best
    op.position, op.width = pos, w_new
    return True


def _trim_passage_off_columns(spec, wall, op, along) -> bool:
    """把開放通道的**端點**收到柱面(只縮不移)。回有沒有收過。

    淨距用 0 —— 只求「不重疊」,跟 `plan_check.opening_on_column` 同一把尺。
    拿 `COLUMN_CLEARANCE`(300)去收的話,通道會離柱 30cm,圖上又變成一截牆頭。
    """
    blocks = _column_blocks(spec, wall, along, 0.0)
    if not blocks:
        return False
    if getattr(op, "passage_door", False):
        return _slide_passage_door_off_columns(spec, wall, op, along, blocks)
    a, b = op.position - op.width / 2.0, op.position + op.width / 2.0
    for t0, t1 in blocks:
        if t0 <= a < t1:                    # 柱蓋住起點那一端
            a = t1
        if t0 < b <= t1:                    # 柱蓋住終點那一端
            b = t0
    # ⚠️ 下限用**開放通道**的下限(750),不是內門寬(850):通道沒有門扇,收到
    #    810 照樣走得過去,拿門的尺去夾會讓「柱吃 250mm」那種案子整支不敢動,
    #    柱就繼續穿過洞口(實測 7.5×16.45 就是這樣漏掉的)。判準的單一出處是
    #    `room_circulation.OPEN_PASSAGE_MIN`,不要在這裡另抄一個數字。
    from src.design.layout.room_circulation import OPEN_PASSAGE_MIN

    if b - a < OPEN_PASSAGE_MIN or (b - a) >= op.width - 1.0:
        return False                        # 收完太窄、或根本沒壓到 → 不動
    op.position, op.width = (a + b) / 2.0, b - a
    return True


def _rooms_across(spec, wall, pos: float) -> tuple:
    """洞口開在這個位置時,牆**兩側**各是哪一間房(用 id;取不到給 None)。

    重排門的時候拿來確認「門的另一邊還是原來那一間」—— 一道牆常常同時貼著兩間
    鄰室(車庫北牆:西半浴廁、東半樓梯間),門一挪過界,那間房就換成從別人家進去。
    """
    px, py = wall.point_at(pos)
    (sx, _sy), (ex, _ey) = wall.start, wall.end
    vertical = abs(sx - ex) < 1.0
    dx, dy = (300.0, 0.0) if vertical else (0.0, 300.0)
    out = []
    for sgn in (1.0, -1.0):
        pt = Point(px + sgn * dx, py + sgn * dy)
        r = next((r for r in spec.rooms if Polygon(r.points).contains(pt)), None)
        out.append(id(r) if r is not None else None)
    return tuple(sorted(out, key=lambda v: (v is None, v)))


def _opening_pier(a, b) -> float:
    """兩個相鄰洞口之間的牆垛至少多寬。

    兩扇**窗**之間要 600(細長牆垛結構上會裂);牽涉到門的用一般淨距就好 ——
    3.5m 面寬的南牆上已經有一扇 1m 大門,兩邊都要求 600 的話再也開不出窗
    (`_free_intervals` 那段註解講的是同一件事)。"""
    return (WINDOW_PIER_MIN if a.kind == "window" and b.kind == "window"
            else WINDOW_GAP)


def _repack_openings_on_wall(spec, wall, lo, hi, along) -> bool:
    """整道牆的洞口**一起**往旁邊推,騰出位置給壓在柱上的那一個。回有沒有成功。

    ⚠️ 單獨挪一扇挪不動時,擋住它的常常不是柱,是**旁邊那個洞口**:車庫北牆一次
    排了三扇 850 的門(浴廁一扇、樓梯間兩扇),最西那扇被角柱吃掉 112mm,而它東
    邊只有 220mm 就是下一扇 —— 逐扇試永遠找不到位置,整排往東推就排得下
    (4000mm 的牆扣掉兩端柱還有 3480,三扇門連牆垛只要 2950)。臨路的南牆是
    「窗 + 大門」的組合,同一件事(實測 4.1×11.5m 的大門被角柱吃掉 10mm)。

    **只挪不縮、只在同一道牆上挪**,所以窗的寬度與所屬房間都不變 —— §40 採光
    算的是「那間房的開口總寬」,沿著同一道牆平移不影響它。重排後逐項驗:左右
    順序不變、每個洞口的**鄰室不變**、門仍離牆角夠遠、窗仍離牆兩端夠遠、而且
    真的沒有柱再壓著;有一項不合就整批還原(加分項不得讓原本好好的東西壞掉)。
    """
    allops = sorted(wall.openings, key=lambda o: o.position)
    # ⚠️ **開放通道不跟著推,但也不因此整道放棄。** 通道的兩端是結構決定的
    # (一邊樓梯導牆、一邊界牆),推它會在界牆上留一截凸出來的牆頭 —— 那正是
    # 使用者指著圖說「這兩個突出來的牆不需要」的東西(見 `_open_passage_mouth`)。
    # ⚠️ 但第一版寫成「牆上有通道 → 回 False」,四款核的走道口全開之後那道核牆
    #    **一定**有通道,這條規則就再也沒有生效過(實測 5.2×14.5m 車庫版:仍然
    #    被呼叫 8 次、牆上確實有 3 個洞口,卻 0 次成功)。
    #    正解是把通道當成**固定的障礙**(跟柱同級),其餘洞口照樣重排。
    ops = [o for o in allops if not getattr(o, "is_passage", False)]
    if len(ops) < 2:
        return False
    before = [o.position for o in ops]
    was = [_rooms_across(spec, wall, p) for p in before]
    blocks = _column_blocks(spec, wall, along, 0.0)
    blocks = list(blocks) + [(o.position - o.width / 2.0,
                              o.position + o.width / 2.0)
                             for o in allops
                             if getattr(o, "is_passage", False)]
    keep = list(wall.openings)
    wall.openings = []                      # 只扣柱,不扣洞口(洞口正要重排)
    free = sorted(_free_intervals(wall, lo, hi, along, blocks, edge_clear=0.0))
    wall.openings = keep
    span = abs(hi - lo)
    cursor, prev = None, None
    for op in ops:                          # 由西往東依序塞,順序自然不變
        # 窗要離牆的兩端 WINDOW_EDGE_CLEAR(門不吃這條 —— 見 `_door_pos_ok`)。
        edge = WINDOW_EDGE_CLEAR if op.kind == "window" else 0.0
        need = op.width if prev is None else op.width + _opening_pier(prev, op)
        for a, b in free:
            base = a if cursor is None else max(a, cursor)
            start = max(base + (need - op.width), edge)
            if start + op.width <= min(b, span - edge):
                op.position = start + op.width / 2.0
                cursor, prev = start + op.width, op
                break
        else:
            for o, p0 in zip(ops, before):
                o.position = p0
            return False
    ok = True
    for op, room_pair in zip(ops, was):
        if _rooms_across(spec, wall, op.position) != room_pair:
            ok = False                      # 洞口跑到別間去了
        elif op.kind == "door" and not any(
                _door_pos_ok(spec, wall, op.position, op.width, c)
                for c in (*DOOR_CLEAR_STEPS, DOOR_CORNER_MIN)):
            ok = False                      # 換成卡在牆角
        else:
            a0, b0 = op.position - op.width / 2.0, op.position + op.width / 2.0
            if any(t0 < b0 and a0 < t1 for t0, t1 in blocks):
                ok = False                  # 還是壓在柱上
    if not ok:
        for o, p0 in zip(ops, before):
            o.position = p0
    return ok


#: 洞口躲柱的淨距退讓階梯:先求舒適的 300,排不下就一級一級降到 0(只求不重疊)。
COLUMN_CLEAR_STEPS = (COLUMN_CLEARANCE, 150.0, 50.0, 0.0)

#: 門挪位時沿著可用區間試的取樣間距(mm)。50 夠細(門角淨距的級距是 100),
#: 又不會讓一道牆試上千點。
_SHIFT_STEP = 50.0


def _nearest_ok_position(spec, wall, op, spans) -> float | None:
    """在可用區間裡挑「離原位最近、而且合格」的洞口中心位置;沒有回 None。

    窗只要塞得進去就好;門還要 `_door_pos_ok`(離房間角落夠遠、門前站得住人)
    —— 少了這道,躲柱會直接換來 `door_in_corner`,問題只是從左手換到右手。

    門角淨距走全產線共用的 `DOOR_CLEAR_STEPS` 退讓階梯(先求舒適的 350,不行才
    一級一級放寬),最後一級是關卡自己的下限 `DOOR_CORNER_MIN` —— 寧可門貼近牆角,
    也不要留一根柱子插在門框裡。"""
    half = op.width / 2.0
    positions = []
    for a, b in spans:
        lo_p, hi_p = a + half, b - half
        if hi_p < lo_p:
            continue
        positions.append(min(max(op.position, lo_p), hi_p))
        positions += [lo_p, hi_p]
        n = int((hi_p - lo_p) / _SHIFT_STEP)
        positions += [lo_p + i * _SHIFT_STEP for i in range(1, n + 1)]
    if not positions:
        return None
    positions.sort(key=lambda t: abs(t - op.position))   # 離原位最近的先試
    if op.kind != "door":
        return positions[0]
    # ⚠️ 躲柱**不得把門挪到走不到的地方**(2026-08-28)。這支只問「離原位近不近、
    #    卡不卡牆角」,不問「挪過去之後那扇門前面站不站得住人」—— 實測 4.0m 面寬:
    #    門本來好端端開在走道上(離角柱只差 38mm),躲柱時被挪到 2.3m 外、樓梯另
    #    一側那個繞不過去的死角,整層前後從此走不通。與「一扇門的位置由誰決定」
    #    同一族的坑:每個修復器都只知道自己那條規則。
    areas = _stair_room_areas(spec)
    vertical = abs(wall.start[0] - wall.end[0]) < 1.0

    def _along(pos):
        """牆上位置 → 世界座標的沿牆值。⚠️ 牆可能是反向的(start 在座標大的那端),
        所以要走 `point_at`,不能寫 start + pos。"""
        px, py = wall.point_at(pos)
        return py if vertical else px

    for walkable_only in (True, False):
        if walkable_only and not areas:
            continue
        for clear in (*DOOR_CLEAR_STEPS, DOOR_CORNER_MIN):
            for pos in positions:
                if walkable_only and not _door_front_walkable(
                        spec, wall, _along(pos), areas):
                    continue
                if _door_pos_ok(spec, wall, pos, op.width, clear):
                    return pos
    return None


def _need_window_width(room) -> float:
    """這間房要多寬的窗(mm)。

    居室依 §40「採光開口 ≥ 樓地板面積 1/8」回推(窗高以 1.2m 估);廚房/浴室不是
    居室,給一扇標準窗就好。"""
    from src.design.layout.code_check import (
        DAYLIGHT_RATIO, HABITABLE_KINDS, WINDOW_H_ASSUMED,
    )
    from src.design.layout.bsp_layout import WINDOW_WIDTH
    if room.kind not in HABITABLE_KINDS:
        return WINDOW_WIDTH
    area = Polygon(room.points).area                    # mm²
    return max(WINDOW_WIDTH, area * DAYLIGHT_RATIO / WINDOW_H_ASSUMED)


def _window_cap(seg_len: float) -> float:
    """單一扇窗的寬度上限(mm):標準上限,但長牆上可以開到**半道牆**那麼寬。

    3.6m 是住宅窗的常見上限,對 3.5~7m 面寬的透天剛好;但兩帶式 19m 寬的房子,
    50㎡ 的客餐廚要 5.4m 的採光開口,硬卡在 3.6m 就永遠補不滿 §40(實測 60 顆種子
    有 2 顆中招)。「一扇窗最寬到牆的一半」是有道理的上限:牆兩端仍留得下結構。"""
    return max(WINDOW_MAX_W, seg_len / 2.0)


def _ensure_room_windows(spec, bx0, by0, bx1, by1, party_walls: bool = True,
                         min_col_clear: float = COLUMN_CLEARANCE) -> int:
    """保證居室採光:窗開得**夠大**(§40 樓地板 1/8),不是有一扇就算。

    為什麼要自己補:透天是共壁,東西外牆不能開窗(_fix_openings 會刪),但配窗時是挑
    「最長的外牆邊」——深長的客廳最長邊正是東側共壁,窗開了又被刪 → 房間全暗。這裡在
    刪窗收尾後補回,並且**依面積把窗加寬/加開第二扇**,直到滿足 1/8(牆面不夠長就
    盡量開,由 code_check 回報)。回補了幾扇窗。

    min_col_clear:窗最少要離柱面多遠。預設 COLUMN_CLEARANCE(300)= **不退讓**。

    ⚠️ 為什麼這是參數而不是固定值:兩個關卡對同一件事的判準不一樣。
         `plan_check.opening_on_column`(AI 產線的關卡)只認「窗框真的穿過柱」
         `layout_generator.validate_spec`(規則版的關卡)要求柱面 300mm 淨距
       所以「窗貼著柱」在 AI 產線是合格圖、在規則版是不合格圖。只有前者能退讓。
       實測把退讓開給全部產線 → 規則版三條全部 raise「洞口壓柱」,383 條測試連鎖倒。

    真正該做的是**讓兩個關卡用同一把尺**(300 是排洞口時想留的餘裕,不是硬規則),
    但那要動 validate_spec 這支老檢核,影響四條產線,不在這次範圍。
    """
    from src.drafting.apartment_plan import WindowPlacement
    from src.drafting.door_window import Window

    added = 0
    for room in spec.rooms:
        if room.kind not in WINDOW_KINDS:
            continue
        poly = Polygon(room.points)
        segs = _window_segments(spec, room, bx0, by0, bx1, by1, party_walls)
        if not segs:
            continue                                    # 真的沒採光面(內間)
        have = sum(op.width for w in spec.walls for op in w.openings
                   if op.kind == "window"
                   and poly.exterior.distance(Point(*w.point_at(op.position))) < 60)
        # 多要 WINDOW_MARGIN:法規是「不得小於 1/8」,補到剛好等於時常被浮點誤差
        # 判掉(實測 6.45㎡ < 6.45㎡),多給 2cm 就穩。
        deficit = _need_window_width(room) + WINDOW_MARGIN - have

        # 柱淨距**退讓階梯**(300 → 150 → 50 → 0),踩到 min_col_clear 為止。
        #
        # 為什麼需要退讓:柱改成在開窗之前定案之後(2026-08-19,為了修使用者說的
        # 「柱子怎麼還能放在窗戶裡」),一根 400mm 的柱連兩側淨距會吃掉整整 1m
        # 牆面。實測 12×12m 的 2F 臥室因此差 215mm 補不滿 §40 → 網站回 422。
        # **寧可窗貼著柱,也不要為了餘裕讓房間變暗房。**
        for col_clear in [c for c in COLUMN_CLEAR_STEPS if c >= min_col_clear]                 or [min_col_clear]:
            if deficit <= 1.0:
                break
            # ① 先**加寬既有的窗**:牆上剩下的空檔常常各只有 40~50cm(開不了一扇新窗
            #    的下限 60cm),但把原本那扇拉寬就綽綽有餘。真實圖也是這樣處理。
            #    做法:把那扇窗暫時拿掉,算出它所在的那段可用區間(已扣掉其他洞口與柱),
            #    再在區間內盡量拉寬。兩輪:正常淨距(300)不夠才放寬到 150。
            for edge_clear in (WINDOW_EDGE_CLEAR, WINDOW_EDGE_CLEAR / 2.0):
                for _rank, wi, lo, hi, along in segs:
                    if deficit <= 1.0:
                        break
                    w = spec.walls[wi]
                    blocks = _column_blocks(spec, w, along, col_clear)
                    for op in list(w.openings):
                        if deficit <= 1.0:
                            break
                        if op.kind != "window":
                            continue
                        if poly.exterior.distance(Point(*w.point_at(op.position))) >= 60:
                            continue
                        others = [o for o in w.openings if o is not op]
                        keep_all = list(w.openings)
                        w.openings = others                     # 暫時拿掉自己再算空間
                        free = _free_intervals(w, lo, hi, along, blocks, edge_clear)
                        w.openings = keep_all
                        span = next(((a, b) for a, b in free
                                     if a - 1 <= op.position <= b + 1), None)
                        if span is None:
                            continue
                        a, b = span
                        width = min(b - a, _window_cap(hi - lo), op.width + deficit)
                        if width <= op.width + 1.0:
                            continue
                        deficit -= width - op.width
                        op.width = width
                        op.position = min(max(op.position, a + width / 2),
                                          b - width / 2)
            # ② 還不夠才開新的窗
            for _rank, wi, lo, hi, along in segs:
                if deficit <= 1.0:
                    break
                w = spec.walls[wi]
                blocks = _column_blocks(spec, w, along, col_clear)   # 洞口不能壓柱
                while deficit > 1.0:
                    free = _free_intervals(w, lo, hi, along, blocks)
                    if not free:
                        break
                    a, b = free[0]
                    # 差額不足一扇窗的下限時,仍開一扇最小窗(寧可略大於法定,也不要
                    # 差 5cm 就判不合格)。
                    width = min(max(deficit, WINDOW_MIN_W), b - a, _window_cap(hi - lo))
                    if width < WINDOW_MIN_W:
                        # 空檔不足一扇標準窗:差額若這段補得完,就開一扇窄高窗補滿
                        # (見 WINDOW_LAST_MIN);否則這段真的沒用,換下一段。
                        if b - a >= WINDOW_LAST_MIN and deficit <= b - a + 1.0:
                            width = b - a
                        else:
                            break
                    pos = (a + b) / 2 if b - a <= width + 1 else a + width / 2
                    if pos - width / 2 < 0 or pos + width / 2 > w.length:
                        break
                    w.openings.append(Opening(pos, width, "window"))
                    spec.windows.append(
                        WindowPlacement(wi, len(w.openings) - 1, Window()))
                    added += 1
                    deficit -= width
    return added



def _door_touching_rooms(spec, wall, op, polys):
    """這個門洞貼到哪幾間房(2 間=內門、1 間=外門)。"""
    p = Point(*wall.point_at(op.position))
    return [i for i, (_r, poly) in enumerate(polys)
            if poly.exterior.distance(p) < DOOR_TOUCH_TOL]


def _room_components(spec):
    """靠門/通道互通的房間分群 → [set(房間index)](豎井/天井不算,它們本來就封閉)。

    這是「從大門走不走得到」的判準:一層若分成兩群,代表其中一群只能從室外進,
    室內走不過去——真實住宅不會這樣。"""
    polys = [(r, Polygon(r.points)) for r in spec.rooms]
    live = [i for i, (r, _p) in enumerate(polys) if r.kind not in NO_DOOR_KINDS]
    adj = {i: set() for i in live}
    for w in spec.walls:
        for op in w.openings:
            if op.kind != "door":
                continue
            touch = [i for i in _door_touching_rooms(spec, w, op, polys)
                     if i in adj]
            for a, b in itertools.combinations(touch, 2):
                adj[a].add(b)
                adj[b].add(a)
    comps, seen = [], set()
    for i in live:
        if i in seen:
            continue
        grp, stack = {i}, [i]
        seen.add(i)
        while stack:
            k = stack.pop()
            for n in adj[k]:
                if n not in grp:
                    grp.add(n)
                    seen.add(n)
                    stack.append(n)
        comps.append(grp)
    return comps


def _wall_covering(spec, kind, line, a, b):
    """找出覆蓋共用邊 (kind, line, [a,b]) 的那道牆 → (wall_index, 沿牆位置) 或 None。"""
    mid = (a + b) / 2
    for wi, w in enumerate(spec.walls):
        (sx, sy), (ex, ey) = w.start, w.end
        if kind == "V":
            if abs(sx - ex) > 1.0 or abs(sx - line) > DOOR_TOUCH_TOL:
                continue
            lo, hi, start_along = min(sy, ey), max(sy, ey), sy
        else:
            if abs(sy - ey) > 1.0 or abs(sy - line) > DOOR_TOUCH_TOL:
                continue
            lo, hi, start_along = min(sx, ex), max(sx, ex), sx
        if lo - SNAP_TOL <= mid <= hi + SNAP_TOL:
            return wi, start_along
    return None


SNAP_TOL = 1.0


def _open_door_on_wall(spec, wi, start_along, a, b,
                       walkable_only: bool | None = None) -> bool:
    """在牆 wi 的 [a,b] 段開一扇內門(避開既有洞口)。成功回 True。

    ⚠️ **「門前面走得到」排在牆角淨距前面**(2026-08-28)。淨距的退讓階梯原本在
    最外層,於是「位置錯但淨距好一級」的門會贏過「位置對但要退一級」的門 ——
    實測 4.0m 面寬:東側走道那個位置只差 12.5mm 就滿足 150 這一級,結果門被開到
    西側那個繞不過去的死角(門前面是階梯,人到不了),後段從此走不通。
    淨距差一級只是「有點擠」,門開在走不到的地方是**廢圖**,兩者不同量級。"""
    w = spec.walls[wi]
    taken = [(op.position - op.width / 2, op.position + op.width / 2)
             for op in w.openings]
    areas = _stair_room_areas(spec)
    modes = (True, False) if walkable_only is None else (walkable_only,)
    for walkable_only in modes:                     # 先只挑走得到的位置
        if walkable_only and not areas:
            continue                                # 沒樓梯 → 這一輪沒有意義
        for clear in DOOR_CLEAR_STEPS:              # 連通是硬需求,淨距可分級退讓
            for m in _door_candidates(spec, w, a, b):
                if walkable_only and not _door_front_walkable(spec, w, m, areas):
                    continue
                pos = abs(m - start_along)
                lo, hi = pos - INTERIOR_DOOR_WIDTH / 2, pos + INTERIOR_DOOR_WIDTH / 2
                if lo < 0 or hi > w.length:
                    continue
                if not all(hi < t0 or lo > t1 for t0, t1 in taken):
                    continue
                if not _door_pos_ok(spec, w, pos, INTERIOR_DOOR_WIDTH, clear):
                    continue                        # 不卡牆角
                w.openings.append(Opening(pos, INTERIOR_DOOR_WIDTH, "door"))
                spec.doors.append(DoorPlacement(wi, len(w.openings) - 1, Door()))
                return True
    return False


def _ensure_floor_connected(spec, max_new_doors: int = 8) -> int:
    """保證整層室內連通:從大門進來走得到每一間房(不必繞到室外)。

    分成多群時,挑「跨群、共用牆最長、且鄰室最公共」的一對開一扇門,重複到只剩一群。
    這是比「每間房有門」更強的保證——房間各自有門但彼此不通(例:柱線牆把一層切兩半,
    客廳只能從室外進)在真實住宅是錯的。回補了幾扇門。"""
    from src.design.layout.bsp_layout import _shared_edge

    added = 0
    for _ in range(max_new_doors):
        comps = _room_components(spec)
        if len(comps) <= 1:
            break
        from src.design.layout.room_circulation import _room_openings
        cands = []                        # (rank, -共用邊長, se, wi, start_along)
        for ca, cb in itertools.combinations(comps, 2):
            for i in ca:
                for j in cb:
                    ri, rj = spec.rooms[i], spec.rooms[j]
                    # 浴廁當穿堂是最後手段(rank 記大一級),優先走其他房間。
                    thru_bath = any(
                        r.kind in _PRIVATE_KINDS
                        and _room_openings(spec, Polygon(r.points))
                        for r in (ri, rj))
                    se = _shared_edge(Polygon(ri.points), Polygon(rj.points))
                    if se is None or se[4] < INTERIOR_DOOR_WIDTH + 200.0:
                        continue
                    cover = _wall_covering(spec, se[0], se[1], se[2], se[3])
                    if cover is None:
                        continue
                    # 兩邊挑「比較公共」的那間當優先度(走道/客廳優先,臥室/浴廁最後)
                    rank = min(_DOOR_NEIGHBOR_PREF.index(r.kind)
                               if r.kind in _DOOR_NEIGHBOR_PREF
                               else len(_DOOR_NEIGHBOR_PREF)
                               for r in (ri, rj))
                    if thru_bath:
                        rank += 100          # 排到最後:浴廁不該變成穿堂
                    cands.append((rank, -se[4], se, cover[0], cover[1]))
        # ⚠️ 要**依序試到成功**:最佳那面牆可能整段都是階梯(開了門會踩在踏step上),
        #    此時得換次佳的牆,不能直接放棄——否則整層就被判成走不通。
        cands.sort(key=lambda c: c[:2])
        opened = False
        # ⚠️ **先掃一輪「開得出走得到的門」的牆**(2026-08-28)。這裡原本一找到能
        #    開門的牆就收工,而「能開門」不問門前面走不走得到 —— 7.5m 面寬的後段
        #    切成 廚房|餐廳,餐廳那一邊的牆整段不是梯段就是梯段旁的死角,門照樣
        #    開得出來,結果後段兩間一起被關在門外。換一面牆(接廚房那面,正對著
        #    走道)就好,所以要先把整份候選掃過一輪再退讓。
        for wo in (True, False):
            for _rank, _len, se, wi, start_along in cands:
                if _open_door_on_wall(spec, wi, start_along, se[2], se[3],
                                      walkable_only=wo):
                    opened = True
                    break
            if opened:
                break
        if not opened:                    # 真的沒有可開門的牆(極罕見)
            break
        added += 1
    return added


def _ensure_room_doors(spec, bx0, by0, bx1, level):
    """保證每間「可進入」的房間至少有一扇門/通道(機電豎管、天井除外)。

    沒門的房間 → 在與鄰室共用的內牆上補一扇門,優先接動線/公共空間(免得補到另一間
    孤立房或會被刪門的豎井);找不到內牆,只有 1F 才退而在南向前牆開(當入口;樓上外牆
    開門會變成通往空中,禁止)。不管建築尺寸,保證「進得了建築、進得了每個房間」。

    ⚠️ 要在所有刪門收尾(含 graph_layout 的管道間去門)之後才呼叫,否則剛補的門或
       既有門可能又被刪掉。"""
    from src.design.layout.room_circulation import _room_openings
    for room in spec.rooms:
        if room.kind in NO_DOOR_KINDS:
            continue
        if _room_openings(spec, Polygon(room.points)):     # 已有門/開放通道
            continue
        _add_interior_door(spec, room, bx0, by0, bx1, level)


def _bath_door_to_hall(spec, bx0, by0, bx1, level) -> int:
    """參考圖版:浴廁的門一定要開在**走道**上,不能開向餐廚/車庫。回改了幾間。

    這正是使用者 2026-08-28 指著方案 B 要的那一點。預設核做不到(廁所被樓梯隔開,
    只剩南北兩面能開門);參考圖版的廁所就貼著走道 —— 但**「貼著」不等於「門開在
    那裡」**:1F 的廁所北邊是餐廚,補門機制先挑到哪一面就開哪一面,實測 40 案
    有 38 個樓層的廁所門開向餐廚,跟改之前一模一樣。所以這裡明講。

    ⚠️ 開不成走道門就**維持原樣** —— 一間沒有門的廁所比一扇開錯邊的門糟得多
    (本檔那條鐵則:加分項不得讓原本好好的東西壞掉)。
    """
    changed = 0
    for room in [r for r in spec.rooms if r.kind == "bathroom"]:
        bp = Polygon(room.points)

        def _adj():
            out = []
            for dp in spec.doors:
                w = spec.walls[dp.wall_index]
                op = w.openings[dp.opening_index]
                if bp.exterior.distance(Point(*w.point_at(op.position))) < 50.0:
                    out.append(dp)
            return out

        if any("stair_hall" in _door_kinds(spec, dp) for dp in _adj()):
            continue                                # 本來就開在走道上
        # ⚠️ `require_walkable`:走道上那扇門**人要走得到**才算數。窄面寬時
        #    參考圖版/中間版的核排不下、會退回預設核 —— 那時浴廁唯一面對樓梯間
        #    的是**北**牆,而北牆外面正是梯段旁邊那塊繞不過去的死角。照樣把門
        #    搬過去,等於把一間本來進得去的廁所變成進不去(實測 96 個樓層 12 個
        #    這樣)。加分項不得讓原本好好的東西壞掉。
        if not _add_interior_door(spec, room, bx0, by0, bx1, level,
                                  only_kinds={"stair_hall"},
                                  require_walkable=True):
            continue                                # 開不成 → 別把原本那扇拆了
        _remove_openings(spec, {(dp.wall_index, dp.opening_index)
                                for dp in _adj()
                                if "stair_hall" not in _door_kinds(spec, dp)})
        changed += 1
    return changed


def _add_interior_door(spec, room, bx0, by0, bx1, level, only_kinds=None,
                       require_walkable: bool = False):
    """給一間沒門的房間補一扇門:挑一道邊界內牆(接最公共/動線的鄰室)開洞。

    only_kinds:限定鄰室的 kind(門與動線規範的修復用——例如「這間一定要有一扇門
    直接通公共動線」、「衛浴的門不能開向廚房」)。給了就只考慮這些鄰室,開不成
    就不開(回 False),由呼叫端決定要不要換切法重生。回傳有沒有開成。"""
    import math

    rp = Polygon(room.points)
    cx, cy = rp.centroid.x, rp.centroid.y
    rminx, rminy, rmaxx, rmaxy = rp.bounds
    need = INTERIOR_DOOR_WIDTH + 200.0                       # 邊夠長才開得下門
    best = None                                             # (rank, -overlap, wi, pos)

    for wi, w in enumerate(spec.walls):
        (sx, sy), (ex, ey) = w.start, w.end
        vertical = abs(sx - ex) < 1.0
        if vertical:
            if not (abs(sx - rminx) < 60 or abs(sx - rmaxx) < 60):
                continue                                    # 非本房左右邊界牆
            lo, hi = max(min(sy, ey), rminy), min(max(sy, ey), rmaxy)
            start_along, midpt_at = sy, lambda m: (sx, m)
        else:
            if not (abs(sy - rminy) < 60 or abs(sy - rmaxy) < 60):
                continue                                    # 非本房上下邊界牆
            lo, hi = max(min(sx, ex), rminx), min(max(sx, ex), rmaxx)
            start_along, midpt_at = sx, lambda m: (m, sy)
        if hi - lo < need:
            continue

        # 段內找一個不撞既有洞口的位置(中點優先,再試三七分)
        taken = [(op.position - op.width / 2, op.position + op.width / 2)
                 for op in w.openings]

        def _neighbor_at(m):
            """門開在這裡的話,門的另一邊是哪一間。"""
            mx, my = midpt_at(m)
            dx, dy = mx - cx, my - cy
            L = math.hypot(dx, dy) or 1.0
            nb = Point(mx + dx / L * 300.0, my + dy / L * 300.0)  # 牆外側 → 鄰室
            return next((r for r in spec.rooms if r is not room
                         and Polygon(r.points).contains(nb)), None)

        # ⚠️ 「門前面走得到」排在牆角淨距前面(理由見 `_open_door_on_wall`):
        #    先只挑走得到的位置跑完整條退讓階梯,真的都不行才准開在死角。
        areas = _stair_room_areas(spec)

        def _pick(walkable_only, w=w, lo=lo, hi=hi, start_along=start_along,
                  taken=taken, areas=areas):
            for clear in DOOR_CLEAR_STEPS:
                for m in _door_candidates(spec, w, lo, hi):
                    if walkable_only and not _door_front_walkable(spec, w, m,
                                                                  areas):
                        continue
                    q = abs(m - start_along)
                    a0 = q - INTERIOR_DOOR_WIDTH / 2
                    b0 = q + INTERIOR_DOOR_WIDTH / 2
                    if not all(b0 < t0 or a0 > t1 for t0, t1 in taken):
                        continue
                    if not _door_pos_ok(spec, w, q, INTERIOR_DOOR_WIDTH, clear):
                        continue                    # 不卡牆角
                    # ⚠️ 一道牆可能同時貼著**兩間**鄰室(前段北牆:西半是浴廁、
                    #    東半才是樓梯間)。指定鄰室時,位置合法還不夠 —— 得問這
                    #    個位置的另一邊是不是要接的那一間,不是就**換位置再試**。
                    #    原本只試第一個合法位置就定案:中點正好落在浴廁那半 →
                    #    整道牆被判出局,「一定要有門直通公共動線」等於沒生效。
                    if only_kinds is not None:
                        nb_room = _neighbor_at(m)
                        if nb_room is None or nb_room.kind not in only_kinds:
                            continue
                    return q, m
            return None

        got = _pick(True) if areas else None
        if got is None and not (require_walkable and areas):
            got = _pick(False)          # 走得到的位置一個都沒有 → 至少要有門
        if got is None:
            continue
        pos, mid = got

        neighbor = _neighbor_at(mid)
        mx, my = midpt_at(mid)
        if only_kinds is not None:          # 修復模式:只准接指定的鄰室
            rank = 0 if (neighbor is not None
                         and neighbor.kind in only_kinds) else 99
        elif neighbor is None:              # 外牆:寧可接室內鄰室,也不要多開一道前門
            rank = 9 if (level == 1 and abs(my - by0) < 60) else 99  # 樓上外牆禁開
        elif neighbor.kind in NO_DOOR_KINDS:                # 豎井/天井:開門無意義
            rank = 99
        elif neighbor.kind in _DOOR_NEIGHBOR_PREF:
            rank = _DOOR_NEIGHBOR_PREF.index(neighbor.kind)
        else:
            rank = 8                                        # 一般房(臥室/浴廁等),末位候選
        cand = (rank, -(hi - lo), wi, pos)
        if best is None or cand[:2] < best[:2]:
            best = cand

    if best is None or best[0] >= 99:                       # 只剩通往空中/豎井 → 寧可不開
        return False
    _, _, wi, pos = best
    w = spec.walls[wi]
    w.openings.append(Opening(pos, INTERIOR_DOOR_WIDTH, "door"))
    spec.doors.append(DoorPlacement(wi, len(w.openings) - 1, Door()))
    return True


def _floor_connected(spec) -> bool:
    """整層室內連不連通(與 plan_check 的 floor_split 同一套判準)。"""
    from src.design.layout.plan_check import _room_graph_components
    return len(_room_graph_components(spec)) <= 1


def _closet_blocks_bath(spec, level) -> bool:
    """更衣室害得浴室只能穿過它才進得去嗎?

    ⚠️ 更衣室切在「主臥貼著浴廁那一段北牆」(那段本來就開不了主臥的門),於是它
    **正好卡在主臥與浴室中間**。浴廁在南時,浴室北邊是梯段盡頭那塊走不到的死角
    → 門只剩南邊可開 → 開向更衣室 → 動線變成 主臥 → 更衣室 → 浴室。
    `through_bedroom` 的套內豁免要求「只有一個鄰室」,更衣室這時有兩個(主臥 +
    浴室)→ 兩間一起被判缺陷。

    退掉更衣室就好(與 `_beds_ok` 同一條:加分項不得讓原本好好的東西壞掉)。"""
    from shapely.geometry import Polygon as _P

    from src.design.layout.door_rules import _through_bedroom_issues
    names = {r.name for r in spec.rooms if r.kind == "storage"}
    if not names:
        return False
    from src.design.layout.plan_check import building_env
    polys = [(r, _P(r.points)) for r in spec.rooms]
    env = building_env(spec)
    return any(iss.room in names
               for iss in _through_bedroom_issues(spec, polys, env, level, ""))


def _stair_circulation_ok(spec) -> bool:
    """每間房的門都走得到嗎(**梯段算障礙**)—— 與 plan_check 的
    `circulation_blocked` 同一套判準(同一件事不要兩把尺)。

    ⚠️ 這條與 `_floor_connected` 不一樣,兩條都要:後者一間房算**一個節點**,
    一間房被自己的樓梯切成兩半它照樣說「連通」。空格併進樓梯間之後,浴廁唯一開得了
    門的方向正好是梯段盡頭那塊繞不過去的死角 —— 只有這條問得出來。"""
    from src.design.layout.room_circulation import analyze_room_circulation
    return analyze_room_circulation(spec).ok


def _build_floor(level, top, W, D, floor_label, furnish=True,
                 variant=DEFAULT_VARIANT, force_absorb=False,
                 force_bath_south=False, force_bath_north=False,
                 margin=0.0, depth_cap=None,
                 allow_min_service=True, lot=None, depth_limit=None,
                 want_patio=False, garage=False, closet=True,
                 core_style="default", allow_skinny_spare=False):
    """組一層 spec(房間 → 牆/門/窗 + 樓梯 + 開口收尾 + 家具)。

    D 超過該面寬的上限時,**建築封頂**、多出來的地留成前後院(置中)——與兩帶式
    產線同一個做法。硬蓋滿只會生出採光不足的深房間(§40)。

    取消儲藏室後多出來的空格預設併進隔壁居室;併完若那間房變走廊狀或窗開不夠
    (_spare_hosts_ok 實際量),就改併進樓梯間重生一次(force_absorb)。"""
    # margin:替外牆柱留的位置(見 layout_generator.STRUCT_MARGIN)。柱能因此
    # 推到室外、室內牆面全平。⚠️ 基地不變、**建築縮小** —— 反過來做(建築不變、
    # 基地長大)會讓圖上宣稱的地比實際大。留不滿由 _fit_margin 的退讓階梯處理。
    # ⚠️ 連棟街屋的側牆是**共同壁,中心線就是地界線**,不能再往內縮留柱位 ——
    #    縮了會在兩戶之間留一條 27.5cm 的縫(現實中沒這種東西),而且建蔽率會
    #    莫名其妙少用掉 7%(5m 寬的地只蓋 4.45m)。柱在共同壁裡是兩戶共用的。
    #    前後仍要留:那兩面是自己的外牆,柱得躲進院子側才不會凸進房間。
    mx = 0.0 if lot is not None else margin
    Wb, Db = W - 2 * mx, D - 2 * margin
    if margin and (Wb < MIN_WIDTH or Db < min_depth_for(Wb, garage)):
        raise ValueError(f"留柱位 {margin:.0f}mm 後放不下(剩 "
                         f"{Wb/1000:.1f}×{Db/1000:.1f}m)")
    # 骨架自己的進深上限(採光)之外,還可能有**建蔽率**給的上限(連棟街屋:
    # 建築進深 = 基地進深 × 建蔽率,見 design/zoning.py)。兩個都要守,取小的。
    # 天井:**設計選擇,不是採光手段**(它不會讓你蓋更深,見上面的實測結論)。
    # 開了每層少約 3㎡ 樓地板(天井貫穿到屋頂,每層都少),換到的是浴廁/樓梯間
    # 有自然採光通風 —— 真實街屋很常見,但要不要付這個坪數是屋主的決定,
    # 所以做成明確的開關、預設關(與 2026-07-29 拿掉天井後的行為一致)。
    use_patio = bool(want_patio)
    limit = max_depth_for(Wb)
    if depth_limit is not None:
        limit = min(limit, depth_limit)
    build_d = min(Db, depth_cap if depth_cap is not None else limit)
    # 車庫要一整個車位長,擠不出來就別硬蓋(前段會溢出建築外框)。這是**真實的
    # 限制**:3.5m 面寬的採光上限只有 12.5m,放不下 5.5+4.4+3.2。
    if garage and build_d + 1.0 < min_depth_for(Wb, True):
        raise ValueError(
            f"車庫需要建築進深 ≥{min_depth_for(Wb, True)/1000:.1f}m,"
            f"這裡只蓋得出 {build_d/1000:.1f}m")
    yard = (Db - build_d) / 2.0
    # 連棟街屋(lot 有給):左右與鄰戶**共壁**,建築側緣就是地界線,不退側院;
    # 前後留的是**法定空地**(建蔽率吃剩的),不是建築線退縮 → setback 記 0。
    edge = 0.0 if lot is not None else SETBACK
    bx0 = edge + mx
    by0 = edge + margin + yard
    bx1, by1 = bx0 + Wb, by0 + build_d
    site_w, site_d = W + 2 * edge, D + 2 * edge     # ⚠️ 基地用原始 D,不是封頂後的
    core_out: dict = {}
    rooms, stair = _floor_rooms(level, top, bx0, by0, bx1, by1, variant,
                                force_absorb, force_bath_south,
                                force_bath_north,
                                allow_min_service, patio=use_patio,
                                garage=garage, closet=closet,
                                core_style=core_style, core_out=core_out)
    spec = rooms_to_spec(rooms, (bx0, by0, bx1, by1), site_w, site_d,
                         setback=edge)
    # 這層**實際**用到的是哪一款核。要的那款排不下時會靜靜地退(ref→mid→
    # default),而「有沒有拿到要的那款」是留柱位退讓階梯的判準之一
    # (見 `_fit_margin` 的 prefer)——不記下來就沒有人問得到。
    spec._nh_core = core_out.get("style", core_style)
    # 三區版的 1F 廁所**畫在樓梯裡**(疊在梯段底下,虛線註記不是房間 —— 使用者
    # 2026-09-02:「你就正常化廁所,只是用虛線畫出來,而且畫在樓梯裡」)。
    # ⚠️ 只有 1F:樓上的浴室要洗澡,而樓梯底下站得起來的只有那麼一小塊。
    if spec._nh_core == "zone3" and level == 1:
        spec.under_stair_wc = _under_stair_wc(stair)
    if lot is not None:
        # 圖上要看得到「這塊地為什麼只蓋這麼深」——答案是建蔽率(見 zoning.py)。
        built = lot.__class__(site_w=lot.site_w, site_d=lot.site_d,
                              zone=lot.zone, coverage=lot.coverage,
                              building_w=bx1 - bx0, building_d=build_d,
                              front_yard=by0 - edge,
                              rear_yard=site_d - edge - (by0 + build_d))
        spec.lot_note = (
            f"基地 {built.site_w/1000:.2f}×{built.site_d/1000:.2f}m "
            f"= {built.site_area_m2:.1f}㎡"
            f"    建築 {built.building_w/1000:.2f}×{built.building_d/1000:.2f}m "
            f"= {built.building_area_m2:.1f}㎡\n"
            f"建蔽率 {built.coverage_used:.0%}"
            f"({built.zone}上限 {built.coverage:.0%})"
            f"    前院 {built.front_yard/1000:.2f}m / "
            f"後院 {built.rear_yard/1000:.2f}m(法定空地)")
    spec.stairs = [stair]                            # 先掛樓梯:開口收尾才避得開梯段
    _add_stair_guard_walls(spec)                     # 梯段兩側都要有牆(不能一側懸空)
    _fix_openings(spec, bx0, by0, bx1, level,
                  entry_frac=variant.entry_frac, garage=garage)
    _ensure_floor_connected(spec)                    # 從大門走得到每一間房
    _ensure_room_doors(spec, bx0, by0, bx1, level)   # 保證每房都有門(不管尺寸)
    # 三區版:走道的兩個出入口不設門、也不隔牆(使用者 2026-09-02)。
    # ⚠️ 要排在補門**之後** —— 前面那幾支還在靠「有沒有門」判斷連不連通,先開成
    #    通道的話它們會以為那裡沒路,再補一扇門進來(白補一扇又被這裡拆掉)。
    for _y, _x0, _x1 in core_out.get("mouths", ()):
        _open_passage_mouth(spec, _y, _x0, _x1)
    from src.design.layout.balcony import add_balconies
    add_balconies(spec, level, env=(bx0, by0, bx1, by1))  # 2F 以上前後挑陽台
    _ensure_room_windows(spec, bx0, by0, bx1, by1)   # 居室補窗(前後外牆/天井側)
    # 通風要**對流**:把前後外牆的窗挪到走道軸線上,風才有一條直線走得過去
    # (使用者 2026-09-03 的 NG01)。要排在補窗之後 —— 窗都排好了才挪得動。
    _align_windows_for_ventilation(spec, by0, by1)
    # ⚠️ 這個判斷要在**修門之前**做。不合格的話整層會 force_absorb 重生,修門
    #    (轉門把 → 改橫拉門)是整條產線最貴的一段之一,放在判斷後面等於每一層
    #    都白修一次門。判斷看的是房間形狀與窗寬,修門只改門 —— 實測 12 個尺寸
    #    ×5 變體、620 次比對,修門前後的答案一次都沒有不同
    #    (`test_spare_hosts_verdict_survives_door_repair` 釘住這個前提)。
    if (not force_absorb and not allow_skinny_spare
            and not _spare_hosts_ok(spec)):             # 居室吃不下空格 → 給樓梯間
        return _build_floor(level, top, W, D, floor_label, furnish, variant,
                            force_absorb=True, margin=margin,
                            depth_cap=depth_cap,
                            allow_min_service=allow_min_service,
                            lot=lot, depth_limit=depth_limit,
                            want_patio=want_patio, garage=garage,
                            closet=closet, core_style=core_style)
    if core_style in ("ref", "mid", "zone3"):
        # 這幾版的重點就是「廁所的門開在走道上」,這裡明講(見該函式)。
        # ⚠️ 三區版也要:樓上的浴室整條貼著走道,但它北邊就是後段那間臥室 ——
        #    補門機制先挑到哪一面就開哪一面,實測會變成「上廁所要穿過次臥」,
        #    而 `through_bedroom` 對套內浴室是豁免的、不會擋(所以關卡全過、
        #    圖卻是錯的)。本檔「幾何擺對了不代表結果會對」同一族。
        _bath_door_to_hall(spec, bx0, by0, bx1, level)
    from src.design.layout.door_rules import repair_doors
    repair_doors(spec, bx0, by0, bx1, level)         # 門與動線規範:改門(不改切法)
    if force_absorb and not force_bath_south and not _floor_connected(spec):
        # 空格併在樓梯間南端時,樓梯間北端只剩梯段 → 後段接不上。浴廁搬回南側。
        return _build_floor(level, top, W, D, floor_label, furnish, variant,
                            force_absorb=True, force_bath_south=True,
                            margin=margin, depth_cap=depth_cap,
                            allow_min_service=allow_min_service,
                            lot=lot, depth_limit=depth_limit,
                            want_patio=want_patio, garage=garage,
                            closet=closet, core_style=core_style,
                            allow_skinny_spare=allow_skinny_spare)
    spec.floor_label = floor_label
    _set_structural_grid(spec, bx0, by0, Wb, build_d)
    # ⚠️ 洞口躲柱:排洞口時柱還不存在(`_column_blocks` 那時看到的是空的),
    #    後來補的門(浴廁門、接通用門)因此可能落在柱上。這支本來只有 AI 產線
    #    在接 —— 本檔「規則存在,但關卡沒接」的第六次。實測窄面寬配車庫有 3 案
    #    的門被柱吃掉 25~75mm。
    shift_openings_off_columns(spec)
    # ⚠️ 柱到這一步才存在,而**柱是實心的**:門扇掃到柱就打不開。上面第 1340 行
    #    那次修門看到的是「還沒有柱」的世界,所以這裡一定要再修一次
    #    (不furnish 的樓層沒有下面那段,不補這一行就完全沒人管)。
    repair_doors(spec, bx0, by0, bx1, level)
    if furnish:                                     # 家具:沿用 Phase 6 擺位(必合法)
        from src.design.layout.auto_furnish import (
            furnish_spec, settle_after_declutter)
        from src.design.layout.fixture_fix import (
            clear_fixtures_off_columns, trim_counters_at_columns)
        from src.design.layout.graph_layout import _declutter_for_circulation
        furnish_spec(spec)
        # 這條產線以前沒有柱,所以從來不必閃柱;現在有了就得閃(掃描原本冒出
        # 45 件 furniture_in_column)。⚠️ 放在 _declutter 之前 —— 動線要有最後
        # 決定權,不能反過來讓閃柱把它讓開的通道又占回去。
        trim_counters_at_columns(spec)
        clear_fixtures_off_columns(spec)
        _declutter_for_circulation(spec)            # 擋動線的家具移掉(與 AI 產線同一套)
        # ⚠️ 動線修復器只看動線:它會拿掉浴室**唯一**的浴缸、臥室的床。換小一號
        #    的補回來(浴缸→淋浴間、雙人床→單人床),沒有床就別留床頭櫃。
        settle_after_declutter(spec)
        # ⚠️ **躲柱要再收一次**:上面那次 `shift_openings_off_columns` 跑在
        #    第一次修門之前,而修門會**補新的門**(接通用門、浴廁門)——
        #    新補的門沒有人問過它壓不壓柱。實測車庫版 30 案有 2 案的門被
        #    角柱吃掉 45~112mm。本檔「規則存在,但關卡沒接」的第七次。
        #    ⚠️ 放在最後一次修門**之前**,順序才對:躲柱可能把門推到門扇
        #    會撞到東西的位置,要留一次修門在後面收。
        shift_openings_off_columns(spec)
        repair_doors(spec, bx0, by0, bx1, level)    # 家具擺完再修一次門(弧線會不會撞家具)
        # 更衣室害得床擺不下、或害得浴室要穿過它才進得去 → 退掉它。
        if closet and (not _beds_ok(spec)
                       or _closet_blocks_bath(spec, level)):
            return _build_floor(level, top, W, D, floor_label, furnish, variant,
                                force_absorb=force_absorb,
                                force_bath_south=force_bath_south,
                                force_bath_north=force_bath_north,
                                margin=margin, depth_cap=depth_cap,
                                allow_min_service=allow_min_service,
                                lot=lot, depth_limit=depth_limit,
                                want_patio=want_patio, garage=garage,
                                closet=False, core_style=core_style,
                                allow_skinny_spare=allow_skinny_spare)
    if variant.mirror:                              # 整層東西鏡射(樓梯核換邊)
        from src.design.layout_generator import _mirror_spec
        core = getattr(spec, "_nh_core", None)
        wc = spec.under_stair_wc
        spec = _mirror_spec(spec, True, False)
        spec.floor_label = floor_label
        if wc:                       # 鏡射弄丟東西:本檔第五次(見下面那段註解)
            sx0 = min(p[0] for p in spec.site_boundary)
            sx1 = max(p[0] for p in spec.site_boundary)
            spec.under_stair_wc = (sx0 + sx1 - wc[2], wc[1],
                                   sx0 + sx1 - wc[0], wc[3])
        # ⚠️ 鏡射會做出一個**新的** spec,私有欄位不會跟過來 —— `_nh_core`
        #    掉了的話 `_fit_margin` 的 prefer 會拿不到答案而一律放行(靜悄悄
        #    地失效:實測 60 案有 32 案的核根本沒被記錄)。本檔「鏡射弄丟東西」
        #    (拉門、陽台、捲門的 label)已經是第四次。
        spec._nh_core = core
    return spec


def _check_dims(W, D, garage: bool = False):
    if not MIN_WIDTH <= W <= MAX_WIDTH:
        raise ValueError(
            f"窄面寬透天面寬需 {MIN_WIDTH/1000:.1f}~{MAX_WIDTH/1000:.1f}m,收到 "
            f"{W/1000:.1f}m(更窄的話「廁所+折返梯+走道」並排放不下,"
            f"見 MIN_WIDTH;更寬請用一般兩帶式產生器)")
    need = min_depth_for(W, garage)
    if D < need:
        raise ValueError(
            f"窄面寬透天({W/1000:.1f}m 面寬)"
            f"{'配車庫' if garage else ''}進深需 ≥{need/1000:.1f}m,"
            f"收到 {D/1000:.1f}m")
    # 面寬太窄時,**採光上限**會先一步把進深卡死,車庫怎麼退讓都放不進去。
    if garage and max_depth_for(W) + 1.0 < need:
        raise ValueError(
            f"{W/1000:.1f}m 面寬的建築進深上限只有 {max_depth_for(W)/1000:.1f}m"
            f"(§40 採光),放不下車庫(需 ≥{need/1000:.1f}m)")


def generate_narrow_building(building_w_mm: float, building_d_mm: float, *,
                             floors: int = 3, bedrooms: int = 3,
                             furnish: bool = True, variant=None,
                             seed=None, lot=None, patio: bool | None = None,
                             garage: bool = False,
                             core_style: str | None = None):
    """窄面寬透天多層 → [(樓層標示, FloorPlanSpec)]。

    每層共用同一垂直核(樓梯間+浴廁),樓梯上下對齊,並配家具(Phase 6 擺位)。
    building_w/d 是**建築物**尺寸;頂層樓梯標「下」,其餘標「上」。

    patio:中段核裡要不要挖天井。

      * `None`(預設)= **自動**:只有在浴廁會變暗房、而且開了不會讓別的東西變差
        時才開(見 `_fit_patio_auto`;使用者 2026-09-03「開天井」)。
      * `True` = 一定試,開了出硬錯誤才退掉(`_fit_patio`)。
      * `False` = 永遠不開(明講不要的呼叫端/測試用)。

    core_style:中段核的排法。

      * `None`(預設)= **自動挑**:從使用者自己畫的參考平面「方案 B」(`"ref"`)
        開始往下退,退到「蓋得出來、沒硬錯誤、房間也沒過大」的第一款
        (使用者 2026-08-31:「我要把每一個尺寸都設計成類似這個格式」)。
      * `"ref"` = 方案 B:樓梯**橫置**在核的南半、天井與廁所並排在北半、廁所的
        門直接開在走道上(見 `_core_ref`)。
      * `"mid"` = 樓梯|浴廁|走道:門一樣開在走道上,但沒有天井。
      * `"default"` = 浴廁|樓梯|走道(走道貼界牆),廁所的門開向餐廚/車庫。

    ⚠️ **明講哪一款就給哪一款**(排不下才退,見下面兩段);只有 `None` 才會為了
       「房間不要太大」換款 —— 不然指名要 ref 的呼叫端會拿到 default,而它不會
       知道自己拿到的不是要的東西。
    ⚠️ 排不下時的退讓是 **ref → mid → default**,不是一路退回 default:橫置樓梯
    要一整段面寬跑得完,3.5~3.9m 排不下 —— 但「廁所的門開在走道上」那件事 `mid`
    也做得到,退到 `mid` 比退到 `default` 接近使用者要的格式。

    lot:連棟街屋的基地(`design/zoning.TownhouseLot`)。給了就照它畫地界線 ——
    **左右與鄰戶共壁、側邊不退縮**,前後留建蔽率吃剩的法定空地,建築進深另外
    受 `lot.building_d` 上限夾住。不給則沿用獨棟的舊行為(四周各留 SETBACK
    反推基地)。⚠️ 給 lot 時 `building_w_mm` 要等於 `lot.building_w`(=基地面寬),
    `building_d_mm` 要給**基地**進深 —— 建築進深由建蔽率與骨架上限一起決定。"""
    W, D = float(building_w_mm), float(building_d_mm)
    auto_core = core_style is None           # None = 讓 _fit_core_style 挑
    core_style = core_style or CORE_STYLE_STEPS[0]
    floors = max(1, int(floors))
    if garage and floors < 2:
        raise ValueError("一層樓的透天不能配車庫:前段整段停車,就沒有客廳了"
                         "(客廳是往上挪到 2F,見 narrow_house._zones)")
    _check_dims(W, D if lot is None else lot.building_d, garage)
    if variant is None:
        variant = DEFAULT_VARIANT if seed is None else variant_from_seed(seed)
    limit = None if lot is None else lot.building_d
    # ⚠️ 各層必須用**同一個** margin 與**同一個**進深,否則軸網對不上、柱不會
    #    上下對齊、外牆也對不齊。
    def _got_core(style):
        def _ok(floors_out) -> bool:
            """留柱位縮掉的那幾百 mm 有沒有把指定的核擠掉(見 `_fit_margin`)。"""
            return all(getattr(sp, "_nh_core", style) == style
                       for _lb, sp in floors_out)
        return _ok

    def _margin_steps(style, want_patio=False):
        """這一款核可以留到哪幾級柱位 —— **留柱位不得把走道吃掉**。

        ⚠️ 2026-09-04「樓梯只做折返梯」之後補的。折返梯比單跑直梯寬 0.9m,
        4.0~4.2m 面寬留了 250mm 柱位就再也擠不出樓梯旁那條走道;沒有走道的
        那一層,前段到後段只能踩過**半層高**的折返平台 → `circulation_blocked`。
        留柱位是加分項,不得讓一層樓走不通(本檔鐵則第 N 次)。

        ⚠️ 要**帶著 `want_patio` 一起問**:開天井時 `_core_widths` 會跳過浴廁的
        退讓(服務格一窄天井就小到 code_check 不認),同一個面寬留得起的柱位因此
        比不開天井時少。不帶的話 4.5m 面寬開天井會停在留滿 275 的那一級 → 沒有
        走道 → `_fit_patio` 把天井退掉,「開了天井卻什麼都沒發生」。
        ⚠️ 用**幾何**先篩、不是蓋完再退:走道在不在只由「核帶有多寬」決定
        (`_passage_span` 是四款核共用的單一出處),一個純函式問得到答案 ——
        蓋完再問的話,留不起走道的那 11 級每一級都要蓋一次,實測慢十倍。
        ⚠️ 一級都留不起時**整條照舊**(排序不是過濾):3.5~3.8m 面寬無論留多少
        柱位都擠不出走道,那時一張留滿柱位的圖仍然勝過沒有圖。
        """
        from src.design.layout_generator import (STRUCT_MARGIN,
                                                 STRUCT_MARGIN_STEP)
        ladder = [float(m) for m in
                  range(int(STRUCT_MARGIN), -1, -STRUCT_MARGIN_STEP)]
        if style != "default":
            return ladder       # ref/mid/zone3 排得下就一定有自己的走道
        keep = [m for m in ladder
                if _passage_span(0.0, W - 2 * (0.0 if lot is not None else m),
                                 False, bool(want_patio), "default") is not None]
        # 保住走道的先試,其餘的仍然留在後面 —— 前面那幾級若全都蓋不出來
        # (raise),還有退路可走(鐵則:留柱位不得讓原本生得出來的案子生不出來)。
        return keep + [m for m in ladder if m not in keep]

    def _all(want_patio, core_style=core_style):
        def _one(bath_north, skinny):
            return _fit_service(lambda ams: _fit_depth(lambda cap: _fit_margin(
                lambda m: [
                    (f"{lv}F",
                     _build_floor(lv, floors, W, D, f"{lv}F", furnish, variant,
                                  margin=m, depth_cap=cap,
                                  allow_min_service=ams,
                                  lot=lot, depth_limit=limit,
                                  want_patio=want_patio,
                                  garage=garage, core_style=core_style,
                                  force_bath_north=bath_north,
                                  allow_skinny_spare=skinny))
                    for lv in range(1, floors + 1)],
                prefer=_got_core(core_style),
                margins=_margin_steps(core_style, want_patio))))

        return _fit_core_reach(_one)

    def _one_style(style):
        # 天井開了出硬錯誤就不開(見 _fit_patio);沒要天井就不必多跑那一輪。
        # ⚠️ 參考圖版的核**自己就帶天井**(那是方案 B 的一部分),`patio` 開關管
        #    的是另外兩款要不要在服務格裡挖一個 —— 所以 `ref` 不進自動那條。
        if patio:
            return _fit_patio(lambda wp: _all(wp, style))
        if patio is None and style != "ref":
            return _fit_patio_auto(lambda wp: _all(wp, style))
        return _all(False, style)

    return (_fit_core_style(_one_style, core_style) if auto_core
            else _one_style(core_style))


def generate_narrow_house(building_w_mm: float, building_d_mm: float, *,
                          bedrooms: int = 3, floor_label: str = "1F",
                          furnish: bool = True):
    """窄面寬透天單層 1F(便捷入口,回單一 FloorPlanSpec;含樓梯核+家具)。"""
    W, D = float(building_w_mm), float(building_d_mm)
    _check_dims(W, D)
    return _fit_service(lambda ams: _fit_depth(lambda cap: [("1F", _fit_margin(
        lambda m: _build_floor(1, 1, W, D, floor_label, furnish,
                               margin=m, depth_cap=cap,
                               allow_min_service=ams)))]))[0][1]
