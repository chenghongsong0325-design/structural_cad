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
# ⚠️ 3.5~4.0m 面寬的圖合法但很擠(客廳淨寬僅約 2.9m),屬老街屋尺度。
MIN_WIDTH, MAX_WIDTH = 3500.0, 8000.0
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
# 梯段旁邊剩不到這麼寬就不留通道,改讓**梯段填滿整間**(兩側直接靠牆)——留一條
# 走不了人的窄縫,只會讓梯段有一側懸空。
PASSAGE_MIN = 1000.0
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


def _core_widths(W: float, min_service: bool = False):
    """中段核面寬分配 → (服務格, 樓梯間)。天井取消後,核只剩這兩格。

    樓梯間要 STAIRWELL_TOTAL(梯段+側邊通道)才走得通,但不能把服務格擠到放不下
    衛浴;面寬不夠時優先保住衛浴的最小寬,樓梯間縮到仍留得下通道的下限。

    ⚠️ **面寬寬的時候要反過來夾**:服務格拿「面寬減掉樓梯間」的全部剩餘,
    8m 面寬就變成 4.4m 寬的服務格 —— 浴廁根本用不到那麼寬(BATH_MAX_W 2400),
    多的會被 `_core` 當成空格丟給隔壁居室,把那間房撐得更大。剩下的寬度給
    樓梯間(=樓梯旁通道變寬),前後段才有位置從中間切成兩間(見 `_band_split_x`:
    西半間的門只能往北開進樓梯間,樓梯間西牆越往西,切點才能越靠中間)。

    min_service:把服務格再壓到浴廁的**最小**寬。6m 級的面寬服務格只有 1.875m
    (還沒碰到 BATH_MAX_W,上面那段夾不到它),但樓梯間也因此不夠往西,前段切不
    成兩間 → 生出 28㎡ 的臥室。浴廁窄 37cm 換一間大小正常的房間,划得來 ——
    由呼叫端在「真的切不動、而且房間會過大」時才開。"""
    sw = min(STAIRWELL_TOTAL, W - BATH_MIN_W)
    svc = W - sw
    if svc > BATH_MAX_W:                # 面寬寬 → 服務格只留浴廁該有的寬
        svc, sw = BATH_MAX_W, W - BATH_MAX_W
    if min_service and svc > BATH_MIN_W:
        svc, sw = BATH_MIN_W, W - BATH_MIN_W
    return svc, sw


def _stair(x_west, x_east, y1, y2, label):
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
    flight_w = (STAIRWELL_W - 2 * WALL_GAP
                if span - STAIRWELL_W >= PASSAGE_MIN else span)
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
    return UStair(origin=(x_east - WALL_GAP - flight_w, y1 + WALL_GAP + landing),
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


def _core(bx0, bx1, y1, y2, label, bath_name, bath_north=False,
          absorb_spare=False, min_service=False, patio=False):
    """中段核 → (房間清單, 樓梯, 讓出來的空格);服務格(西:浴廁)| 樓梯間(東)。

    樓梯間貼**東界牆**:梯段靠東牆、西側留通道,通道正對後段餐廳/後臥。核每層
    同寬同位 → 樓梯 origin 固定 → 上下對齊。

    ⚠️ **不設儲藏室**(使用者 2026-07-30 定調):服務格只放浴廁,浴廁吃不完的那
    一小塊(1.5~2.4m 寬 × 約 1.8m)不另外隔成房間,而是回傳給呼叫端**併進隔壁
    居室**(客廳/餐廳/臥室變成 L 形)——隔成儲藏室只會多一扇門、多一間沒人用的
    小房;併進去則是實際可用的坪數。"""
    W, d = bx1 - bx0, y2 - y1
    svc, _sw = _core_widths(W, min_service)
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
    return rooms, _stair(xs, bx1, y1, y2, label), spare


# 併了空格之後,接收的那間房長寬比不得超過這個(對齊 plan_check 的 room_skinny
# 判準;此處不 import plan_check —— plan_check 反過來要 import 本模組)。
SPARE_ASPECT_LIMIT = 2.5
# 會被查長寬比的房間(對齊 plan_check:走道/豎井/陽台/儲藏本來就細長,不查)。
SKINNY_CHECK_KINDS = {"living", "dining", "kitchen", "bedroom",
                      "master_bedroom", "study", "elder_room", "bathroom"}


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


def _band_split_x(bx0, bx1, hall_x0, west_kind="bedroom", east_kind="bedroom"):
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
    """
    lo = max(bx0 + BAND_SPLIT_MIN_W, hall_x0 + BAND_DOOR_ADJ)
    hi = min(bx1 - BAND_SPLIT_MIN_W, bx1 - BAND_DOOR_ADJ)
    if lo > hi:                                     # 面寬不夠切
        return None
    cw, ce = _room_area_cap(west_kind), _room_area_cap(east_kind)
    frac = cw / (cw + ce) if cw + ce < float("inf") else 0.5   # 沒訂上限 → 對半
    return min(max(bx0 + (bx1 - bx0) * frac, lo), hi)


def _floor_rooms(level, top, bx0, by0, bx1, by1, variant=DEFAULT_VARIANT,
                 force_absorb=False, force_bath_south=False,
                 allow_min_service=True, patio=False, garage=False):
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
    bath_north = variant.bath_north and not force_bath_south
    # 有車庫時 1F 前段是車庫、客廳上 2F(見 _zones 上面那段)。前段該用哪一把
    # 面積尺量,跟著它是哪種房間走 —— 拿臥室的上限去量客廳會把 24㎡ 的客廳切成
    # 兩間小房。
    front_kind = "living" if (garage and level == 2) else "bedroom"
    front_east = "study" if front_kind == "living" else "bedroom"
    big_front = (bx1 - bx0) * d_front > _room_area_cap(front_kind)
    big_rear = (bx1 - bx0) * d_rear > _room_area_cap("bedroom")

    def _splits(min_service):
        xs = bx0 + _core_widths(bx1 - bx0, min_service)[0]   # 服務格東緣=樓梯間西牆
        hall_front = bx0 if bath_north else xs               # 浴廁擋住的是哪一段
        hall_rear = xs if bath_north else bx0
        return (xs,
                (_band_split_x(bx0, bx1, hall_front, front_kind, front_east)
                 if big_front else None),
                (_band_split_x(bx0, bx1, hall_rear, "bedroom", "study")
                 if big_rear else None),
                hall_rear)

    xs, xf, xr, hall_rear = _splits(False)
    # 該切卻切不動(浴廁把那一段的北牆吃掉太多)→ 把服務格壓到浴廁最小寬再試。
    # 這是拿「浴廁窄 37cm」換「臥室不要 28㎡」;壓了還是切不動就維持原樣。
    if allow_min_service and ((big_front and xf is None)
                              or (big_rear and xr is None)):
        alt = _splits(True)
        if ((big_front and alt[1] is not None) or (big_rear and alt[2] is not None)):
            xs, xf, xr, hall_rear = alt
            min_service = True
        else:
            min_service = False
    else:
        min_service = False

    # 核每層同構(服務格+樓梯間)→ 樓梯上下對齊。
    if level == 1:                                  # 1F:客廳 / 核 / 餐廳|廚房
        core, stair, spare = _core(bx0, bx1, y1, y2, label, "浴廁", bath_north,
                                   absorb_spare=force_absorb,
                                   min_service=min_service, patio=patio)
        # 前段:有車庫就整段停車(捲門由 _add_garage_doors 開在南向臨路牆)。
        fk, fn = ("garage", "車庫") if garage else ("living", "客廳")
        front_1f = (fk, fn, _with_spare(bx0, by0, bx1, y1, spare))
        # 後段左右分:切在**樓梯旁通道**的東緣(=梯段導牆中心線),讓通道正對餐廳 →
        # 前後段那扇門開得成,且門洞離導牆自動有牆角淨距(切在幾何中線時,通道與餐廳
        # 只重疊一小段,門塞不下,整層就會被判走不通)。
        xm = min(max(stair.origin[0] - GUARD_WALL_T / 2.0, bx0 + 2000.0),
                 bx1 - 2000.0)
        west, east = (("dining", "餐廳"), ("kitchen", "廚房"))
        # ── 開放式餐廚(使用者 2026-08-26 指著參考平面圖:「餐廳/廚房」是一間)──
        # 真實透天的 1F 後段就是一間開放餐廚,不是隔成兩間 —— 4.5m 面寬硬切,
        # 廚房只剩 2m 寬(走廊狀),而且中間那道牆在生活上沒有意義。
        # 併起來的上限是**兩間相加**(plan_check.MERGED_ROOM_PARTS 已經這樣算),
        # 所以只有大到連併合上限都撐不住的後段(8×18m 那種 44㎡)才切成兩間。
        if variant.open_kitchen and (bx1 - bx0) * d_rear <= _room_area_cap("餐廚"):
            rooms = [front_1f, *core,
                     ("dining", "餐廚",
                      _with_spare(bx0, y2, bx1, by1, spare))]
            return rooms, stair
        # 1F 後段本來就是兩間,但舊切法貼著通道東緣 → 面寬一寬,西室(廚房或
        # 餐廳)就大得離譜(8m 面寬的廚房 19㎡,上限 11㎡)。西室真的超標時才
        # 改用對中的切點,而且**只往西縮不往東放**,窄面寬的既有格局不受影響。
        if (xm - bx0) * d_rear > _room_area_cap(west[0]):
            xm_mid = _band_split_x(bx0, bx1, hall_rear, west[0], east[0])
            if xm_mid is not None:
                xm = min(xm, xm_mid)
        # 儲藏室取消後多出來的空格 → 併進緊鄰的那間居室(南=客廳、北=後段西室)
        rooms = [front_1f, *core,
                 (west[0], west[1], _with_spare(bx0, y2, xm, by1, spare)),
                 (east[0], east[1], _rect(xm, y2, bx1, by1))]
        return rooms, stair

    core, stair, spare = _core(bx0, bx1, y1, y2, label, f"浴室{level}F",
                               bath_north, absorb_spare=force_absorb,
                               min_service=min_service, patio=patio)
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
    else:
        front = ([("bedroom", f"前臥室{level}F",
                   _with_spare(bx0, by0, xf, y1, spare)),
                  ("bedroom", f"小孩房{level}F", _rect(xf, by0, bx1, y1))]
                 if xf is not None else
                 [("bedroom", f"前臥室{level}F",
                   _with_spare(bx0, by0, bx1, y1, spare))])
    rear = ([("bedroom", f"後臥室{level}F",
              _with_spare(bx0, y2, xr, by1, spare)),
             ("study", f"書房{level}F", _rect(xr, y2, bx1, by1))]
            if xr is not None else
            [("bedroom", f"後臥室{level}F",
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

    # 每間浴室只留 1 門(偏好開向公共鄰室,其餘刪掉)
    for room in [r for r in spec.rooms if r.kind == "bathroom"]:
        bp = Polygon(room.points)
        adj = [dp for dp in spec.doors
               if bp.exterior.distance(
                   Point(spec.walls[dp.wall_index].point_at(
                       spec.walls[dp.wall_index]
                       .openings[dp.opening_index].position))) < 50.0]
        if len(adj) > 1:
            def _pref(dp):
                ks = set(_door_kinds(spec, dp)) - {"bathroom"}
                return next((i for i, k in enumerate(_BATH_DOOR_PREF)
                             if k in ks), len(_BATH_DOOR_PREF))
            adj.sort(key=_pref)
            for dp in adj[1:]:
                remove.add((dp.wall_index, dp.opening_index))

    _remove_openings(spec, remove)

    if level == 1:                                  # 臨路大門:南向外牆
        if garage:                                  # 前段是車庫 → 捲門(+人行門)
            _add_garage_doors(spec, bx0, by0, bx1, entry_frac)
        else:
            _add_front_door(spec, bx0, by0, bx1, entry_frac)


def _add_front_door(spec, bx0, by0, bx1, entry_frac=0.22):
    """在南向(臨路)外牆上加一扇大門,避開既有洞口。entry_frac=偏好位置比例。"""
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
        for clear in DOOR_CLEAR_STEPS:              # 先求舒適淨距,不行再放寬
            for pos in cands:
                a, b = pos - ENTRY_WIDTH / 2, pos + ENTRY_WIDTH / 2
                if a < 0 or b > length:
                    continue
                if not all(b < t0 or a > t1 for t0, t1 in taken):   # 撞既有洞口
                    continue
                if not _door_pos_ok(spec, w, pos, ENTRY_WIDTH, clear):
                    continue                        # 卡在房間角落
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


def _door_candidates(spec, wall, lo, hi):
    """開門位置候選(世界座標,沿牆方向):先梯段沒擋住那幾段,再等距掃描。

    每段給三個點:中點 + **緊貼兩端**。貼端點很重要——樓梯間的合法窗口常常只有
    幾十 mm(門要同時「北端不碰到第一階」且「南端離牆角夠遠」),那個位置一定
    在可用段的端點上,中點會差幾十 mm 就開不成。"""
    half = INTERIOR_DOOR_WIDTH / 2.0
    pts = []
    for a, c in _stair_free_spans(spec, wall, lo, hi):
        if c - a < INTERIOR_DOOR_WIDTH:
            continue
        pts += [(a + c) / 2, c - half, a + half]
    return pts + [min(lo, hi) + abs(hi - lo) * f for f in _DOOR_FRACS]


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
    """各座樓梯「**踏step**」佔的矩形——兩端平台都不算。

    起步平台在 origin 之前(見 _stair);折返端平台在梯段跑完之後(s ≥ flight_run),
    那是一塊站得住人的平地,門開在正對平台的牆上是合理的(真實透天的梯廳門就是這樣
    開的)。只有正對踏step 的位置才算「門一開就踩階梯」。"""
    from shapely.geometry import box
    out = []
    for st in getattr(spec, "stairs", []) or []:
        try:
            ox, oy = st.origin
            w = st.width
            # ⚠️ 踩過的坑:這裡本來只寫 `st.flight_run`,那是**折返梯**(UStair)
            #    才有的欄位。兩帶式/集合住宅用的是單跑直梯(Stair),沒有這個欄位
            #    → AttributeError → `continue` → **整座樓梯被當成不存在**。
            #    於是「門不得直接開在階梯上」「梯段不得有一側沒牆」這兩條硬規則,
            #    對那兩條產線**從來沒有生效過**(實測每一層的樓梯間門都只離第一階
            #    150mm,一開門就踩上踏step)。直梯的踏step 長度 = flight_length。
            run = getattr(st, "flight_run", None)
            if run is None:
                run = getattr(st, "flight_length", None)
            if run is None:
                continue
            run = min(float(run), st.length)         # 踏step 的長度(不含折返平台)
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


def _fit_margin(build):
    """替外牆柱留位置,**留不滿就少留一點**(與兩帶式 `_house_frame` 同一套)。

    ⚠️ 鐵則:留柱位不得讓原本生得出來的案子生不出來。窄透天的下限很緊
       (面寬 3.5m 起跳),留滿 275mm 會直接把最窄的那批擠掉,所以做成退讓階梯,
       一路退到 0 時行為與改動前完全一致。
    """
    from src.design.layout_generator import STRUCT_MARGIN, STRUCT_MARGIN_STEP

    last: Exception | None = None
    for m in range(int(STRUCT_MARGIN), -1, -STRUCT_MARGIN_STEP):
        try:
            return build(float(m))
        except ValueError as exc:
            last = exc
    raise last


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
                free = _free_intervals(w, lo, hi, along, blk)
                w.openings = keep
                spans = [(a, b) for a, b in free if b - a >= op.width]
                if not spans:
                    continue                      # 這級淨距排不下 → 再讓一級
                new_pos = _nearest_ok_position(spec, w, op, spans)
                if new_pos is not None:
                    break
            if new_pos is None:
                continue        # 真的挪不開 → 原地不動,交給關卡回報,不偷偷縮小
            op.position = new_pos
            moved += 1
    return moved


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
    for clear in (*DOOR_CLEAR_STEPS, DOOR_CORNER_MIN):
        for pos in positions:
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


def _open_door_on_wall(spec, wi, start_along, a, b) -> bool:
    """在牆 wi 的 [a,b] 段開一扇內門(避開既有洞口)。成功回 True。"""
    w = spec.walls[wi]
    taken = [(op.position - op.width / 2, op.position + op.width / 2)
             for op in w.openings]
    for clear in DOOR_CLEAR_STEPS:                  # 連通是硬需求,淨距可分級退讓
        for m in _door_candidates(spec, w, a, b):
            pos = abs(m - start_along)
            lo, hi = pos - INTERIOR_DOOR_WIDTH / 2, pos + INTERIOR_DOOR_WIDTH / 2
            if lo < 0 or hi > w.length:
                continue
            if not all(hi < t0 or lo > t1 for t0, t1 in taken):
                continue
            if not _door_pos_ok(spec, w, pos, INTERIOR_DOOR_WIDTH, clear):
                continue                            # 不卡牆角
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
        for _rank, _len, se, wi, start_along in cands:
            if _open_door_on_wall(spec, wi, start_along, se[2], se[3]):
                opened = True
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


def _add_interior_door(spec, room, bx0, by0, bx1, level, only_kinds=None):
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

        pos = None
        for clear in DOOR_CLEAR_STEPS:
            for m in _door_candidates(spec, w, lo, hi):
                p = abs(m - start_along)
                a, b = p - INTERIOR_DOOR_WIDTH / 2, p + INTERIOR_DOOR_WIDTH / 2
                if not all(b < t0 or a > t1 for t0, t1 in taken):
                    continue
                if not _door_pos_ok(spec, w, p, INTERIOR_DOOR_WIDTH, clear):
                    continue                        # 不卡牆角
                # ⚠️ 一道牆可能同時貼著**兩間**鄰室(前段北牆:西半是浴廁、東半
                #    才是樓梯間)。指定鄰室時,位置合法還不夠 —— 得問這個位置的
                #    另一邊是不是要接的那一間,不是就**換位置再試**。原本只試第
                #    一個合法位置就定案:中點正好落在浴廁那半 → 整道牆被判出局,
                #    「一定要有門直通公共動線」的修復等於沒生效。
                if only_kinds is not None:
                    nb_room = _neighbor_at(m)
                    if nb_room is None or nb_room.kind not in only_kinds:
                        continue
                pos, mid = p, m
                break
            if pos is not None:
                break
        if pos is None:
            continue

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


def _build_floor(level, top, W, D, floor_label, furnish=True,
                 variant=DEFAULT_VARIANT, force_absorb=False,
                 force_bath_south=False, margin=0.0, depth_cap=None,
                 allow_min_service=True, lot=None, depth_limit=None,
                 want_patio=False, garage=False):
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
    rooms, stair = _floor_rooms(level, top, bx0, by0, bx1, by1, variant,
                                force_absorb, force_bath_south,
                                allow_min_service, patio=use_patio,
                                garage=garage)
    spec = rooms_to_spec(rooms, (bx0, by0, bx1, by1), site_w, site_d,
                         setback=edge)
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
    from src.design.layout.balcony import add_balconies
    add_balconies(spec, level, env=(bx0, by0, bx1, by1))  # 2F 以上前後挑陽台
    _ensure_room_windows(spec, bx0, by0, bx1, by1)   # 居室補窗(前後外牆/天井側)
    # ⚠️ 這個判斷要在**修門之前**做。不合格的話整層會 force_absorb 重生,修門
    #    (轉門把 → 改橫拉門)是整條產線最貴的一段之一,放在判斷後面等於每一層
    #    都白修一次門。判斷看的是房間形狀與窗寬,修門只改門 —— 實測 12 個尺寸
    #    ×5 變體、620 次比對,修門前後的答案一次都沒有不同
    #    (`test_spare_hosts_verdict_survives_door_repair` 釘住這個前提)。
    if not force_absorb and not _spare_hosts_ok(spec):   # 居室吃不下空格 → 給樓梯間
        return _build_floor(level, top, W, D, floor_label, furnish, variant,
                            force_absorb=True, margin=margin,
                            depth_cap=depth_cap,
                            allow_min_service=allow_min_service,
                            lot=lot, depth_limit=depth_limit,
                            want_patio=want_patio, garage=garage)
    from src.design.layout.door_rules import repair_doors
    repair_doors(spec, bx0, by0, bx1, level)         # 門與動線規範:改門(不改切法)
    if force_absorb and not force_bath_south and not _floor_connected(spec):
        # 空格併在樓梯間南端時,樓梯間北端只剩梯段 → 後段接不上。浴廁搬回南側。
        return _build_floor(level, top, W, D, floor_label, furnish, variant,
                            force_absorb=True, force_bath_south=True,
                            margin=margin, depth_cap=depth_cap,
                            allow_min_service=allow_min_service,
                            lot=lot, depth_limit=depth_limit,
                            want_patio=want_patio, garage=garage)
    spec.floor_label = floor_label
    _set_structural_grid(spec, bx0, by0, Wb, build_d)
    # ⚠️ 柱到這一步才存在,而**柱是實心的**:門扇掃到柱就打不開。上面第 1340 行
    #    那次修門看到的是「還沒有柱」的世界,所以這裡一定要再修一次
    #    (不furnish 的樓層沒有下面那段,不補這一行就完全沒人管)。
    repair_doors(spec, bx0, by0, bx1, level)
    if furnish:                                     # 家具:沿用 Phase 6 擺位(必合法)
        from src.design.layout.auto_furnish import furnish_spec
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
        repair_doors(spec, bx0, by0, bx1, level)    # 家具擺完再修一次門(弧線會不會撞家具)
    if variant.mirror:                              # 整層東西鏡射(樓梯核換邊)
        from src.design.layout_generator import _mirror_spec
        spec = _mirror_spec(spec, True, False)
        spec.floor_label = floor_label
    return spec


def _check_dims(W, D, garage: bool = False):
    if not MIN_WIDTH <= W <= MAX_WIDTH:
        raise ValueError(
            f"窄面寬透天面寬需 {MIN_WIDTH/1000:.1f}~{MAX_WIDTH/1000:.1f}m,收到 "
            f"{W/1000:.1f}m(更寬請用一般兩帶式產生器)")
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
                             seed=None, lot=None, patio: bool = False,
                             garage: bool = False):
    """窄面寬透天多層 → [(樓層標示, FloorPlanSpec)]。

    每層共用同一垂直核(樓梯間+浴廁),樓梯上下對齊,並配家具(Phase 6 擺位)。
    building_w/d 是**建築物**尺寸;頂層樓梯標「下」,其餘標「上」。

    lot:連棟街屋的基地(`design/zoning.TownhouseLot`)。給了就照它畫地界線 ——
    **左右與鄰戶共壁、側邊不退縮**,前後留建蔽率吃剩的法定空地,建築進深另外
    受 `lot.building_d` 上限夾住。不給則沿用獨棟的舊行為(四周各留 SETBACK
    反推基地)。⚠️ 給 lot 時 `building_w_mm` 要等於 `lot.building_w`(=基地面寬),
    `building_d_mm` 要給**基地**進深 —— 建築進深由建蔽率與骨架上限一起決定。"""
    W, D = float(building_w_mm), float(building_d_mm)
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
    return _fit_service(lambda ams: _fit_depth(lambda cap: _fit_margin(lambda m: [
        (f"{lv}F",
         _build_floor(lv, floors, W, D, f"{lv}F", furnish, variant,
                      margin=m, depth_cap=cap, allow_min_service=ams,
                      lot=lot, depth_limit=limit, want_patio=patio,
                      garage=garage))
        for lv in range(1, floors + 1)])))


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
