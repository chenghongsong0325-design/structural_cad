"""規則式格局產生器(C1)—— 需求 → FloorPlanSpec → 完整平面圖。

「說出需求就設計出圖」的核心引擎。階段 B 的圖面元素已全數完成,所以這裡
產出的是**完整的圖**:牆/門窗/家具設備/房型帶框標籤/四邊三層尺寸鏈/樓層
標示/北向箭頭/A3 圖框,一次到位。

支援兩種建築類型:

  1. HouseBrief(單戶住宅):兩帶式格局——北帶臥室區(主臥加大,≥3 房
     自動加主臥套衛)、南帶公共區(客廳西、餐廳中、東側服務核=浴廁+廚房)。
     走道規則(C1.5b):房間多才需要走道——≥3 房在兩帶之間留走道(臥室門
     開向走道、走道經正對大門軸線的通道口連通客廳);2 房動線融入客餐廳、
     預設不設(門直開客餐廳,被柱位擠壓時退回設走道);1 房不設。
     玄關(C1.5b):大門內側 2.2×1.5m 落塵區(短隔屏牆+鞋櫃),客廳挖角
     成 L 形。餐廳太窄自動併入客廳。家具設備依房型自動擺放。
  2. CorridorBrief(集合住宅):用 B6 的標準單元(place_unit)沿雙邊走廊
     重複 N 戶鏡射對排;units 給清單則同排混不同房型(套房/一房一廳,
     寬窄戶並存,柱藏分戶牆、上下對齊——真實建案差距 A2)。

共同規則:
  * 軸網跨距自動決定(目標 4.5m、合理範圍內),柱在交點、隔間帶分界牆
    與軸線對齊(柱長在牆裡);臥室隔牆/服務核西牆距軸線 <0.6m 時吸附到
    軸線上「坐樑」(C1.5c)。
  * 機能規則(C1.5c):居室採光深度 ≤6m(客廳太寬自動補南窗)、家具不
    互撞不擋門(方桌自動讓開大門迴轉)。
  * 門窗自動配置且自動躲柱:洞口撞柱先平移、窗塞不下自動縮寬(最小 800)、
    加分項的窗塞不下就略過、必要的門塞不下則明確報錯「設計不成立」。
  * validate_spec() 檢核每份產出(房間不重疊/面積覆蓋/每房有門/臥室客廳
    有窗/洞口不壓柱),generate 完自動跑,不過就報錯——大量出圖、張張合格。

典型用法::

    from src.design.layout_generator import HouseBrief, CorridorBrief, generate_floor_plan

    spec = generate_floor_plan(HouseBrief(site_width=16000, site_depth=14000, bedrooms=3))
    spec = generate_floor_plan(CorridorBrief(units_per_row=6))
    draw_floor_plan(msp, spec, layers)   # 直接出圖

⚠️ 待確認假設見模組結尾 PENDING 區塊(設計規則常數、家具擺放規則等)。
"""
from __future__ import annotations

import random
import sys
from dataclasses import dataclass, replace
from itertools import combinations
from pathlib import Path
from types import SimpleNamespace
from typing import Optional, Union

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from shapely.geometry import Point as SPoint
from shapely.geometry import Polygon
from shapely.ops import unary_union

from src.drafting.apartment_plan import (
    DoorPlacement,
    FloorPlanSpec,
    WindowPlacement,
    build_grid,
    resolve_columns,
)
from src.drafting.door_window import Door, Window
from src.drafting.fixtures import Counter, FixturePlacement
from src.drafting.room import Room
# _t_rotation/_t_swing:B6 已驗證的鏡射語意(家具轉角/門向翻轉),單一來源。
from src.drafting.unit import UnitSpec, _t_rotation, _t_swing, one_room_unit, place_unit
from src.drafting.wall import (
    EXTERIOR_WALL_THICKNESS as EXT,
    INTERIOR_WALL_THICKNESS as INT,
    Opening,
    Wall,
)
# 房間面積程式(F3):房間該多大由 room_program 決定,這裡只負責把面積目標
# 變成刀位(牆的位置)。⚠️ 面積是目標不是命令——柱網吸附之後會偏移,允許
# ±AREA_TOLERANCE;柱網、結構、牆線永遠優先(使用者 2026-07-20 定調)。
from src.design.room_program import (
    AREA_TOLERANCE,
    allocate_areas,
    compact_width,
    requirement,
    select_overflow_program,
    solve_band,
)
from src.design.collision import resolve_collisions

Point = tuple[float, float]

# ── 設計規則常數(mm)——待確認:皆為合理經驗值 ────────────────────────────
MIN_BEDROOM_WIDTH = 2800     # 臥室最小淨寬
MAX_BEDROOM_WIDTH = 4200     # 臥室最大合理寬(建築寬度收斂用,見 _house_frame)
MASTER_RATIO = 1.35          # 主臥室寬度加大倍率
MIN_DINING_WIDTH = 2700      # 餐廳最小寬,低於此併入客廳成「客餐廳」
SERVICE_WIDTH_RANGE = (2600, 3400)   # 服務核(廚房+浴廁)寬度範圍
BATH_DEPTH_RANGE = (2000, 2800)      # 浴廁進深範圍
MIN_GALLEY_DEPTH = 1400      # 一字型廚房的最小進深(流理台 600 + 通行 800)
NORTH_BAND_RANGE = (3600, 5500)      # 臥室帶進深範圍
TARGET_BAY = 6000            # 軸網目標跨距(經濟跨距 6~9m 的下緣)
# X 向跨數上下限。上限放到 6:房間會隨基地長大之後(F3 面積程式),建築也
# 跟著變寬,20m 寬的透天用 4 跨就得每跨 5m 以上、跟 3.9m 一道的臥室隔牆對
# 不上(柱只好凸在房間裡)。跨距本身仍由 BAY_SPAN_LIMITS(3~9m)與
# BAY_RATIO_MAX 把關,所以放寬跨數不會生出不合理的柱網,只是讓「每根柱都
# 藏得進牆」多幾種可能。
BAY_RANGE = (2, 6)
BAY_SPAN_LIMITS = (3000, 9000)   # 單跨合理範圍(結構經濟)
BAY_RATIO_MAX = 1.6          # 跨距 max/min 上限(柱網要規則、近似等距)
GRID_SNAP_TOL = 1500         # 軸線向主要隔牆吸附的容差(柱藏進牆交點)
DOOR_WIDTH = 900
ENTRY_DOOR_WIDTH = 1000
WINDOW_WIDTHS = {"bedroom": 1500, "living": 1800, "dining": 1200,
                 "kitchen": 1200, "bathroom": 800}
ROOM_CODES = {"living": "X03", "dining": "X04", "bedroom": "X05",
              "kitchen": "X07", "bathroom": "X08", "corridor": "X00",
              "stair": "X01", "elevator": "X02", "storage": "X09",
              "foyer": "X10", "patio": "X11", "study": "X12",
              "parking": "X13", "ramp": "X14", "family": "X15"}
ENSUITE_W, ENSUITE_D = 1800, 2000     # 主臥套房衛浴尺寸(≥3房自動加)
CORE_W = 3100                          # 集合住宅端部逃生核(樓梯+電梯)的開間寬
RAMP_GAP = 2700                        # 地下室車道口寬(限一跨內躲柱;機車坡道)
STAIRWELL_W = 2800                     # 透天樓梯間開間寬(D2 多樓層,每層同位)
# 透天改用「單跑直梯 + 中央扶手」(使用者 2026-07-20 依實際樓梯平面圖定調):
# 單跑不折返、梯跑較長,樓梯間(北帶)要加深才放得下整段梯——但南帶(客廳帶)
# 不得淺於採光/使用下限,故加深有上限、且不得讓南帶低於 MIN_SOUTH_BAND_DEPTH。
HOUSE_STAIR_TREAD = 250                # 級深(踏步深度,mm)
HOUSE_STAIR_TOP_LANDING = 400          # 梯段頂與樓梯間北牆之間留的平台(mm)
STAIRWELL_MIN_DEPTH = 4700             # 直梯樓梯間(北帶)最小進深(mm)
MIN_SOUTH_BAND_DEPTH = 3000            # 南帶(客廳帶)最小進深,加深北帶時的下限(mm)
WET_W = 2000                           # 透天濕區開間寬(1F/2F 衛浴、B1F 機房疊同管道)
HALL_DEPTH = 1200                      # 單戶走道進深(臥室帶與公共帶之間,C1.5b)
PASSAGE_WIDTH = 1500                   # 走道↔客廳的開放通道寬(正對大門軸線)
WALL_SNAP_TOL = 600                    # 隔間牆距軸線小於此值就吸附坐樑(C1.5c)
DAYLIGHT_DEPTH_MAX = 6000              # 居室採光深度上限(距窗最遠 6m,C1.5c)
# 建築深度上限(深基地收斂用):兩帶式格局 = 北帶臥室(≤NORTH_BAND_RANGE 上限)
# + 南帶客廳/起居(只靠南牆採光,深度 ≤DAYLIGHT_DEPTH_MAX)。基地更深時房間
# 再深既不合結構(Y 跨 >9m)也不合法規(採光照不到)——建築深度封頂、
# 前後留院置中,跟寬基地「收斂置中留側院」同一個道理(真實透天也是這樣蓋)。
MAX_HOUSE_DEPTH = NORTH_BAND_RANGE[1] + DAYLIGHT_DEPTH_MAX   # 11.5m(兩帶式)
# 天井帶(深基地第三帶,多樓層透天):基地深到兩帶+前後院也消化不完時,
# 在南北帶之間插一條「天井帶」——西段天井(採光井,各層同位直落,把光引進
# 建築中段)+ 北側 1.2m 走道 + 東段餐廳/家庭廳。台灣街屋消化深基地的經典
# 解法。基地更深,天井跟著長大成「中庭」(名稱依深度自動切換,見
# _patio_name),建築深度可到 MAX_HOUSE_DEPTH + 6.7 = 18.2m(帶跨 6.7m
# 仍在 9m 結構跨距內);再深才封頂留前後院。範圍含走道 HALL_DEPTH。
PATIO_BAND_RANGE = (2800, 6700)
FOYER_W, FOYER_D = 2200, 1500          # 玄關落塵區(大門內側,C1.5b)
COLUMN_CLEARANCE = 300       # 洞口與柱面的最小淨距(柱要避開開口部,不貼門窗)
# 指定房間需求(E3)——
MIN_STUDY_WIDTH = 2400       # 書房最小淨寬(比臥室略窄可接受:一桌一椅一櫃)
STUDY_RATIO = 0.85           # 書房相對臥室的寬度分配比(略小,不搶臥室空間)
CAR_STALL = (2500, 5500)     # 單一汽車位淨尺寸(寬×長,mm;台灣法定約 2.5×5.5m)
CAR_AISLE = 5500             # 車道/迴轉淨深(垂直式停車進出車道,mm)


# ---------------------------------------------------------------------------
# 需求
# ---------------------------------------------------------------------------
@dataclass
class HouseBrief:
    """單戶住宅需求:基地寬深 + 臥室數(1~4)+ 方位約束(選填)。

    方位約束(C2:「主臥要在西南角,廚房靠北」):
      * master_corner:主臥落在哪個角落——"NW"(預設格局)/"NE"/"SW"/"SE"。
      * kitchen_side:廚房靠哪一側——"N"/"S"/"E"/"W"(預設格局在東南)。
    實作方式是把整張圖左右/上下鏡射(門向/家具/流理台跟著翻),所以兩個
    約束可能衝突(例:主臥 NW 佔北帶 → 廚房必在南,再要求廚房靠北就
    不成立),衝突會明確報錯。
    """

    site_width: float
    site_depth: float
    bedrooms: int = 3
    setback: float = 2000
    column_size: float = 500
    floor_label: str = "1F"
    master_corner: Optional[str] = None   # "NW"/"NE"/"SW"/"SE"
    kitchen_side: Optional[str] = None    # "N"/"S"/"E"/"W"
    seed: int = 0                         # 設計變體種子(E2:同 seed 同方案,
                                          # 換 seed 換方案;見 _house_variant)
    # ── 指定房間需求(E3:使用者點名要某種房間)─────────────────────────
    want_study: bool = False              # 要書房(臥室帶多一格,擺書桌;
                                          # 單層與多樓層臥室層皆可,見 _north_program)
    want_elder_room: bool = False         # 要孝親房(一樓臥室,共用該層衛浴;
                                          # 單層=臥室帶多一格、多樓層=1F 北帶西端)
    car_spaces: int = 0                   # 汽車停車位數(0=不要;需地下車庫,
                                          # 沒地下室時由 _building_from_data 自動補 B1F)


@dataclass
class CorridorBrief:
    """集合住宅需求:標準單元 × 每排戶數,雙邊走廊鏡射對排。

    房型組合(真實建案差距 A2):
      * units=None:每排放 units_per_row 戶相同單元(unit,預設套房)。
      * units=[套房, 一房, 套房, …]:每排依清單放不同房型(寬窄戶並存)。
        各房型「寬度可不同、深度須相同」(同一排是一條等深的帶);柱都落在
        分戶牆上,故寬窄並存不生孤柱(檢核放寬,見 validate_spec)。
      上下兩排用同一份清單(鏡射),分戶牆 x 對齊 → 柱上下對齊。
    """

    units_per_row: int = 4
    unit: Optional[UnitSpec] = None          # None → 套房(one_room_unit)
    units: Optional[list[UnitSpec]] = None   # 給清單 → 每排混不同房型(蓋過上兩者)
    corridor_width: float = 1800
    setback: float = 2000
    column_size: float = 500
    floor_label: str = "3F"


Brief = Union[HouseBrief, CorridorBrief]


def _house_slot_names(roles: list) -> list:
    """單層臥室帶各格顯示名:主臥室 / 臥室A,B,C… / 書房 / 孝親房(E3)。"""
    names, letter = [], 0
    for r in roles:
        if r == "master":
            names.append("主臥室")
        elif r == "study":
            names.append("書房")
        elif r == "elder":
            names.append("孝親房")
        else:
            names.append(f"臥室{'ABC'[letter]}")
            letter += 1
    return names


# ---------------------------------------------------------------------------
# 工具:洞口躲柱
# ---------------------------------------------------------------------------
def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _find_clear_position(desired: float, width: float, lo: float, hi: float,
                         blocked: list[tuple[float, float]],
                         step: float = 100) -> Optional[float]:
    """在 [lo,hi] 找離 desired 最近、洞口區間不碰 blocked 的位置;無解回 None。"""
    half = width / 2
    lo_c, hi_c = lo + half, hi - half
    if lo_c > hi_c:
        return None

    def clear(pos: float) -> bool:
        a, b = pos - half, pos + half
        return all(b <= s or a >= e for s, e in blocked)

    offsets = [0.0]
    n = 1
    while n * step <= (hi_c - lo_c):
        offsets += [n * step, -n * step]
        n += 1
    for off in offsets:
        pos = _clamp(desired + off, lo_c, hi_c)
        if clear(pos):
            return pos
    return None


def _blocked(col_positions: list[float], col: float) -> list[tuple[float, float]]:
    # +1mm:洞口的期望位置常常「剛好」落在柱心上(例如通道口取兩牆中點,而
    # 那裡正好有一根柱),_find_clear_position 以 100mm 為步長找位置,結果會
    # 停在「剛好貼齊淨距邊界」的地方;validate_spec 用同一個 r 再算一次,浮點
    # 誤差就可能讓它判定壓柱。把禁區放大 1mm,永遠停在邊界外一點點。
    r = col / 2 + COLUMN_CLEARANCE + 1.0
    return [(p - r, p + r) for p in col_positions]


def _plan_x_grid(bx0: float, W: float, majors: list[float],
                 prefer: int = 0) -> list[float]:
    """單戶 X 向軸網:等分起手、軸線吸附主要隔牆、跨數擇優(柱網原則)。

    對每個候選跨數(目標跨距的前後一跨、等分跨距在合理範圍):
      * 中間軸線距最近主要隔牆(臥室隔牆/服務核西牆)≤GRID_SNAP_TOL、
        且挪過去之後跨距仍規則(range 內、max/min ≤ 上限)→ 軸線挪到牆位,
        柱正好站在隔牆交點、被牆包住。
      * 挪不過去的中間軸線是「孤柱列」(柱凸在房間邊的牆上,難看)。
    選孤柱列最少的方案;平手時看 prefer(E2 設計變體抽選):
      0=跨距最接近 TARGET_BAY(原行為)、-1=偏好大跨(少柱)、+1=偏好密柱。
    無論 prefer 為何,都只在「孤柱最少」的合格方案裡挑,不會犧牲柱網品質。
    """
    n0 = round(W / TARGET_BAY)
    best: Optional[tuple] = None
    for nxc in (n0, n0 - 1, n0 + 1):
        if not BAY_RANGE[0] <= nxc <= BAY_RANGE[1]:
            continue
        span = W / nxc
        if not BAY_SPAN_LIMITS[0] <= span <= BAY_SPAN_LIMITS[1]:
            continue
        grid = [bx0 + i * span for i in range(nxc + 1)]
        orphans = 0
        for gi in range(1, nxc):
            m = min(majors, key=lambda v: abs(v - grid[gi]))
            if abs(m - grid[gi]) <= GRID_SNAP_TOL:
                trial = grid[:gi] + [m] + grid[gi + 1:]
                spans = [trial[j + 1] - trial[j] for j in range(nxc)]
                if (min(spans) >= BAY_SPAN_LIMITS[0]
                        and max(spans) <= BAY_SPAN_LIMITS[1]
                        and max(spans) / min(spans) <= BAY_RATIO_MAX):
                    grid = trial
                    continue
            if abs(m - grid[gi]) > 1:
                orphans += 1
        # 次要排序鍵依 prefer:0 取近目標跨距;-1 取少跨(nxc 小);+1 取多跨。
        tiebreak = {0: abs(span - TARGET_BAY), -1: nxc, 1: -nxc}[prefer]
        score = (orphans, tiebreak)
        if best is None or score < best[0]:
            best = (score, grid)

    # 反過來做:直接從主要隔牆裡挑軸線 —— 每條中間軸線天生就坐在牆上,
    # 零孤柱 by construction。等分起手法在「房間寬度」與「經濟跨距」湊不
    # 起來時會留下孤柱(房數多、基地寬時最常見:牆每 3.9m 一道、跨距想要
    # 5.5m,兩者永遠對不上),那時就得反過來讓柱網遷就牆。跨距的規則性
    # 仍由 BAY_SPAN_LIMITS / BAY_RATIO_MAX 把關,挑不出合格組合就沿用上面
    # 的等分方案。(柱網優先於房間尺寸,使用者 2026-07-20 定調。)
    inner = sorted(m for m in majors if bx0 + 1 < m < bx0 + W - 1)
    for nxc in range(BAY_RANGE[0], BAY_RANGE[1] + 1):
        if nxc - 1 > len(inner):
            break
        for pick in combinations(inner, nxc - 1):
            grid = [bx0] + list(pick) + [bx0 + W]
            spans = [grid[j + 1] - grid[j] for j in range(nxc)]
            if (min(spans) < BAY_SPAN_LIMITS[0]
                    or max(spans) > BAY_SPAN_LIMITS[1]
                    or max(spans) / min(spans) > BAY_RATIO_MAX):
                continue
            tiebreak = {0: abs(W / nxc - TARGET_BAY), -1: nxc, 1: -nxc}[prefer]
            score = (0, tiebreak)
            if best is None or score < best[0]:
                best = (score, grid)

    if best is None:
        raise ValueError(f"建築寬 {W/1000:.1f}m 找不到合理柱網(跨距需 3~9m)")
    return best[1]


# ---------------------------------------------------------------------------
# 產生器:單戶住宅(兩帶式)
# ---------------------------------------------------------------------------
def _generate_house(brief: HouseBrief) -> FloorPlanSpec:
    if not 1 <= brief.bedrooms <= 4:
        raise ValueError(f"支援 1~4 間臥室,收到 {brief.bedrooms}")

    bx0 = by0 = brief.setback
    bx1 = brief.site_width - brief.setback
    by1 = brief.site_depth - brief.setback
    W, D = bx1 - bx0, by1 - by0
    if W < 8000 or D < 7000:
        raise ValueError(
            f"建築範圍 {W/1000:.1f}m×{D/1000:.1f}m 太小(至少約 8m×7m)")

    # 深基地:建築深度封頂(MAX_HOUSE_DEPTH)、南北留院置中——南帶再深
    # 會超過 9m 跨距與 6m 採光深度,基地深不該讓房間跟著失控。
    if D > MAX_HOUSE_DEPTH:
        side = (D - MAX_HOUSE_DEPTH) / 2
        by0, by1, D = by0 + side, by1 - side, MAX_HOUSE_DEPTH

    # 臥室帶分間程式(E3):臥室(主臥在西端 index 0)+ 選填書房 + 選填孝親房
    # (東端,近南側服務核/衛浴;孝親房=一樓臥室、共用該層衛浴)。
    roles = (["master"] + ["bedroom"] * (brief.bedrooms - 1)
             + (["study"] if brief.want_study else [])
             + (["elder"] if brief.want_elder_room else []))
    n = len(roles)
    _ROLE_KIND = {"master": "master_bedroom", "bedroom": "bedroom",
                  "study": "study", "elder": "bedroom"}
    bed_kinds = [_ROLE_KIND[r] for r in roles]
    bed_reqs = [requirement(k) for k in bed_kinds]
    mins = [r.min_width for r in bed_reqs]

    # ── 分區:尺寸由面積程式決定(F3)────────────────────────────────
    # 單層的骨架:北帶 = 臥室們(橫跨整個建築寬)、南帶 = 客廳|餐廳|服務核
    # (浴廁南 + 廚房北)。所以建築寬由臥室帶決定、南帶進深由公共區的面積
    # 決定,兩者都不再是「攤滿基地」——房間吃飽(max_area)就停,多的地留院。
    # (F3 之前:bed_w = W×比例、完全沒有上限,30×16m 基地會生出 54m² 的主臥。)
    south_kinds = ["living", "kitchen", "bathroom"]
    if not brief.want_elder_room:
        south_kinds.insert(1, "dining")
    # 小基地先砍獨立餐廳:它本來就會併進客廳成「客餐廳」(merged_dining),
    # 硬把它當必要房間會讓「其實蓋得出來」的小基地被判定放不下。
    gross_m2 = W * D / 1_000_000
    while ("dining" in south_kinds
           and gross_m2 < sum(requirement(k).min_area
                              for k in bed_kinds + south_kinds)):
        south_kinds.remove("dining")
    prog_kinds = bed_kinds + south_kinds
    lo_d = float(NORTH_BAND_RANGE[0])
    # ≥3 房會自動加主臥套衛(見下方 has_ensuite):套衛要 ENSUITE_D 深、主臥
    # 自己還要留得下床,故臥室帶進深有硬下限——這是幾何約束,先於面積分配。
    if brief.bedrooms >= 3:
        lo_d = max(lo_d, float(ENSUITE_D + 2200))
    # 南帶的硬下限:東端服務核是「浴廁(南)+ 廚房(北)」上下疊起來的,
    # 淺到浴廁被壓扁(東牆連一扇 800 的窗都塞不下)就不行。廚房這一半可以
    # 很淺(一字型流理台靠牆),故只用 MIN_GALLEY_DEPTH 當它的下限。
    kit_req, bath_req = requirement("kitchen"), requirement("bathroom")
    min_south = max(float(MIN_SOUTH_BAND_DEPTH),
                    BATH_DEPTH_RANGE[0] + MIN_GALLEY_DEPTH)
    hi_d = min(float(DAYLIGHT_DEPTH_MAX), D - min_south)
    if hi_d < lo_d:
        raise ValueError(
            f"可建進深 {D/1000:.1f}m 不足:臥室帶需 {lo_d/1000:.1f}m + "
            f"公共帶需 {min_south/1000:.1f}m")
    w_avail = W
    # 幾何容量:兩條帶各有「帶寬 × 帶深上限」的天花板,超過就是分了也放不下
    # (同 _solve_frame_program 的作法,理由見那裡的註解)。
    south_reqs = [requirement(k) for k in south_kinds]

    def _scale(reqs: list, capacity: float) -> list[float]:
        top = sum(r.max_area for r in reqs) or 1.0
        return [r.max_area * min(1.0, capacity / top) for r in reqs]

    caps = (_scale(bed_reqs, w_avail * hi_d / 1_000_000)
            + _scale(south_reqs,
                     w_avail * min(float(DAYLIGHT_DEPTH_MAX), D - lo_d)
                     / 1_000_000))
    plan = allocate_areas(prog_kinds, w_avail * D / 1_000_000, caps=caps)

    # 兩條帶共用同一個建築寬 W:北帶 = 臥室們、南帶 = 客廳|餐廳|服務核,
    # 各自的進深 = 該帶面積 ÷ W。所以 W 要同時讓兩帶的進深都落在合格範圍內
    # (太窄 → 帶太深、超過採光深度;太寬 → 帶太淺、不能用)。先算出 W 的
    # 可行區間,再在區間內取「儘量先用進深、不夠才加寬」的那一端——房子長
    # 得像房子,而不是又寬又扁的一條。
    north_a = sum(plan.areas[:n]) * 1_000_000
    south_a = sum(plan.areas[n:]) * 1_000_000
    min_south_w = requirement("living").min_width + SERVICE_WIDTH_RANGE[0]
    w_lo = max(north_a / hi_d, south_a / float(DAYLIGHT_DEPTH_MAX),
               sum(mins), min_south_w)
    w_hi = min(w_avail, north_a / lo_d, south_a / min_south)
    if w_lo > w_avail:
        raise ValueError(
            f"建築範圍寬 {w_avail/1000:.1f}m 放不下這個房間程式"
            f"(至少需 {w_lo/1000:.1f}m),請加大基地或減少房間")
    if plan.leftover_m2 > 1e-6:
        # 房間都吃飽了還有剩 → 建築縮小、多的地留成院子(這才是「設計」)。
        W = _clamp((north_a + south_a) / D, w_lo, max(w_lo, w_hi))
    else:
        # 基地才是瓶頸(沒有餘量)→ 用滿可建寬,不要無故縮小房子;帶進深各自
        # 夾在合格範圍內,幾何下限吃掉的那點面積由帶內等比放大補回房間。
        W = w_avail
    dn = _clamp(north_a / W, lo_d, hi_d)
    ds = _clamp(south_a / W, min_south,
                min(float(DAYLIGHT_DEPTH_MAX), D - dn))

    _, bed_w = solve_band(plan.areas[:n], bed_reqs,
                          width_avail=W, depth_bounds=(dn, dn))
    if any(bed_w[i] < mins[i] - 1 for i in range(n)):
        raise ValueError(
            f"這些房間分下來寬度不足(最窄 {min(bed_w)/1000:.2f}m),"
            f"請加大基地或減少房間")
    scale = W / sum(bed_w)                     # 臥室帶鋪滿建築寬(帶內等比微調)
    bed_w = [w * scale for w in bed_w]

    # 建築置中,多出來的基地留成側院/前後院(不再攤滿可建範圍)。
    bx0 += (w_avail - W) / 2
    bx1 = bx0 + W
    used_d = ds + dn
    if D > used_d + 1:
        side = (D - used_d) / 2
        by0, by1, D = by0 + side, by1 - side, used_d
    bed_x = [bx0]
    for w in bed_w:
        bed_x.append(bed_x[-1] + w)
    yd = by0 + ds                                # 帶分界牆 y

    # 服務核寬(廚房+浴廁疊在一起,共用南帶東端一個開間):由兩者的面積目標
    # 回推,守各自的最小寬。
    ws = _clamp(max(plan.area_of("kitchen") * 1_000_000 / (ds - 1),
                    kit_req.min_width, bath_req.min_width),
                *SERVICE_WIDTH_RANGE)
    sx = bx1 - ws

    # ── 軸網(先定;柱網原則:規則等距、柱藏牆交點、孤柱列最少——
    # 使用者 2026-07-12 定調,見 _plan_x_grid)────────────────────────
    grid_x = _plan_x_grid(bx0, W, bed_x[1:-1] + [sx])
    nx = len(grid_x) - 1
    col = brief.column_size

    # C1.5c:隔間坐樑(補正)——隔牆距軸線 <WALL_SNAP_TOL 就吸附到軸線上
    # (牆下有樑);吸附後仍須守 最小房寬 / 主臥最大 / 套衛放得下。
    for i in range(1, n):
        g = min(grid_x, key=lambda v: abs(v - bed_x[i]))
        if 0 < abs(g - bed_x[i]) < WALL_SNAP_TOL:
            trial = bed_x[:i] + [g] + bed_x[i + 1:]
            widths = [trial[j + 1] - trial[j] for j in range(n)]
            ok = (all(widths[j] >= mins[j] for j in range(n))
                  and widths[0] >= max(widths))
            if brief.bedrooms >= 3:
                ok = ok and widths[0] >= ENSUITE_W + 2700
            if ok:
                bed_x = trial
    # 服務核西牆同樣吸附軸線(吸附後寬度仍須在合理範圍)。
    g = min(grid_x, key=lambda v: abs(v - sx))
    if (0 < abs(g - sx) < WALL_SNAP_TOL
            and SERVICE_WIDTH_RANGE[0] <= bx1 - g <= SERVICE_WIDTH_RANGE[1]):
        sx, ws = g, bx1 - g
    # 客廳|餐廳 的分界:由兩者的面積目標決定各佔多寬(以前是固定 55%/45%),
    # 餐廳分不到 MIN_DINING_WIDTH 就併成「客餐廳」(原規則保留)。
    wl_zone = W - ws
    dining_t = plan.area_of("dining")
    dining_w = (dining_t * 1_000_000 / ds) if dining_t else 0.0
    merged_dining = (dining_w < MIN_DINING_WIDTH
                     or wl_zone - dining_w < requirement("living").min_width)
    living_e = sx if merged_dining else sx - dining_w
    # 浴廁進深:同樣由面積目標回推(浴廁與廚房上下疊在服務核裡,共用寬 ws)。
    bath_d = _clamp(plan.area_of("bathroom") * 1_000_000 / ws,
                    *BATH_DEPTH_RANGE)
    bath_d = max(float(BATH_DEPTH_RANGE[0]),
                 min(bath_d, ds - MIN_GALLEY_DEPTH))    # 廚房(北半)也要留得下
    yb = by0 + bath_d

    # ── 軸網(X 向已在上面先定)──────────────────────────────────────
    grid_y = [by0, yd, by1]

    blocked_s = _blocked([gx - bx0 for gx in grid_x], col)
    blocked_n = _blocked([bx1 - gx for gx in grid_x], col)
    blocked_e = _blocked([gy - by0 for gy in grid_y], col)
    blocked_w = _blocked([by1 - gy for gy in grid_y], col)
    blocked_band = _blocked([gx - bx0 for gx in grid_x], col)   # 帶分界牆(4)

    # C1.5b:走道 =「連接多個獨立房間的共用動線」,房間多才需要——
    # ≥3 房必設(3+ 扇臥室門不宜全開進客餐廳);2 房動線融入客餐廳、
    # 預設不設,但東臥門若被服務核+柱位擠到只能開進廚房,退回設走道;
    # 1 房必不設。(集合住宅的走廊是另一套,見 _generate_corridor。)
    has_hall = n >= 3
    pos_direct: Optional[float] = None           # 2房直開客餐廳時的東臥門位
    if n == 2:
        pos_direct = _find_clear_position(
            (bed_x[-2] + sx) / 2 - bx0, DOOR_WIDTH,
            bed_x[-2] - bx0 + 150, sx - bx0 - 150, blocked_band)
        if pos_direct is None:
            has_hall = True
    yc = yd - (HALL_DEPTH if has_hall else 0)    # 走道下緣(=公共帶上緣)

    # C1.5b:走道東端 x_he——東側臥室在服務核上方,門要落在走道正面。
    # 先找東臥「最靠西」的可用門位(躲柱),走道東端跟著門走:必要時越過
    # 服務核西牆、吃進廚房上方一角(廚房變 L 形,門改由走道端進出)。
    x_he = sx
    pos_e: Optional[float] = None
    if has_hall:
        lo_e = bed_x[-2] - bx0 + 150                 # 讓開臥室隔牆
        pos_e = _find_clear_position(lo_e + DOOR_WIDTH / 2, DOOR_WIDTH,
                                     lo_e, W, blocked_band)
        if pos_e is None:
            raise ValueError("東側臥室找不到可開門的位置(柱擋住),請調整基地")
        x_he = max(sx, bx0 + pos_e + DOOR_WIDTH / 2 + 150)
        if x_he > sx:
            for gx in grid_x:                        # 端牆讓開柱
                if abs(x_he - gx) < 600:
                    x_he = gx + 600
            if bx1 - x_he < 1500:
                raise ValueError(
                    f"走道端部吃進廚房太深(剩 {(bx1-x_he)/1000:.1f}m <1.5m),請加大基地")
    has_notch = has_hall and x_he > sx               # 走道是否吃進廚房角

    # ── 牆 ───────────────────────────────────────────────────────────
    walls = [
        Wall((bx0, by0), (bx1, by0), EXT),    # 0 南
        Wall((bx1, by0), (bx1, by1), EXT),    # 1 東
        Wall((bx1, by1), (bx0, by1), EXT),    # 2 北
        Wall((bx0, by1), (bx0, by0), EXT),    # 3 西
        Wall((bx0, yd), (bx1, yd), INT),      # 4 帶分界牆
        # 5 服務核西牆(走道吃進廚房角時,只到走道下緣,以免切斷走道)。
        Wall((sx, by0), (sx, yc if has_notch else yd), INT),
        Wall((sx, yb), (bx1, yb), INT),       # 6 浴廁/廚房分界
    ]
    for i in range(1, n):                     # 7.. 臥室帶隔牆(含書房/孝親房)
        walls.append(Wall((bed_x[i], yd), (bed_x[i], by1), INT))

    doors: list[DoorPlacement] = []
    windows: list[WindowPlacement] = []

    def add_opening(wi: int, desired: float, width: float, kind: str,
                    lo: float, hi: float, blocked, optional=False) -> Optional[int]:
        widths = [width]
        if kind == "window":
            w = width - 200
            while w >= 800:
                widths.append(w)
                w -= 200
        for w in widths:
            pos = _find_clear_position(desired, w, lo, hi, blocked)
            if pos is not None:
                walls[wi].openings.append(Opening(pos, w, kind))
                return len(walls[wi].openings) - 1
        if optional:
            return None
        raise ValueError(f"牆段 [{lo:.0f},{hi:.0f}] 塞不下{kind}(寬{width:.0f}),設計不成立")

    # 大門(南牆、客廳段中央)。
    living_cx = (bx0 + living_e) / 2
    op = add_opening(0, living_cx - bx0, ENTRY_DOOR_WIDTH, "door", 0, living_e - bx0, blocked_s)
    doors.append(DoorPlacement(0, op, Door(hinge="left", swing="out")))

    # 玄關(C1.5b:大門內側 2.2×1.5m 落塵區)——跟著大門實際位置走
    # (大門躲柱偏移玄關就跟著偏);東側立短隔屏牆與客廳區隔,西側貼到
    # 外牆就靠牆。客廳淺時玄關深度自動縮(留 ≥0.7m 通行)。
    entry_x = bx0 + walls[0].openings[0].position
    fy1 = by0 + min(FOYER_D, yc - by0 - 700)
    fx0 = max(bx0, entry_x - FOYER_W / 2)
    fx1 = min(living_e, entry_x + FOYER_W / 2)
    walls.append(Wall((fx1, by0), (fx1, fy1), INT))    # 玄關短隔屏牆

    # 主臥套房衛浴(C1.5a:≥3 房自動加,位於主臥西南角)——先算範圍,
    # 臥室門/西窗要讓開它。
    has_ensuite = brief.bedrooms >= 3
    ex1 = ey1 = 0.0
    if has_ensuite:
        if dn < ENSUITE_D + 2200 or bed_w[0] < ENSUITE_W + 2700:
            raise ValueError(
                f"主臥({bed_w[0]/1000:.1f}m 寬 × {dn/1000:.1f}m 深)放不下"
                f"套房衛浴({ENSUITE_W/1000:.1f}×{ENSUITE_D/1000:.1f}m),請加大基地")
        ex1, ey1 = bx0 + ENSUITE_W, yd + ENSUITE_D

    # 臥室:門在帶分界牆(有走道時門要落在走道正面 x ≤ x_he;主臥的門
    # 讓開套衛牆段;東臥用預先算好的門位)、窗在北牆。
    for i in range(n):
        x_l, x_r = bed_x[i], bed_x[i + 1]
        cx = (x_l + x_r) / 2
        if has_hall and i == n - 1:
            door_lo, door_hi, door_desired = pos_e - 450, pos_e + 450, pos_e
        elif not has_hall and pos_direct is not None and i == n - 1:
            # 2 房無走道:東臥門直開客餐廳(位置已預先算好,避開廚房正面)。
            door_lo, door_hi = pos_direct - 450, pos_direct + 450
            door_desired = pos_direct
        elif i == 0 and has_ensuite:
            door_lo, door_hi = ex1 - bx0, x_r - bx0
            door_desired = (ex1 + x_r) / 2 - bx0
        else:
            door_lo, door_hi, door_desired = x_l - bx0, x_r - bx0, cx - bx0
        op = add_opening(4, door_desired, DOOR_WIDTH, "door",
                         door_lo, door_hi, blocked_band)
        doors.append(DoorPlacement(4, op, Door(hinge="left", swing="out")))
        op = add_opening(2, bx1 - cx, WINDOW_WIDTHS["bedroom"], "window",
                         bx1 - x_r, bx1 - x_l, blocked_n)
        windows.append(WindowPlacement(2, op))

    # ── 套衛的牆/門/窗(範圍已在上面算好)──────────────────────────────
    if has_ensuite:
        # 套衛東牆(門開向浴內 = +n 西側)與北牆。
        walls.append(Wall((ex1, yd), (ex1, ey1), INT,
                          openings=[Opening(ENSUITE_D / 2, 750, "door")]))
        doors.append(DoorPlacement(len(walls) - 1, 0, Door(hinge="left", swing="out")))
        walls.append(Wall((bx0, ey1), (ex1, ey1), INT))
        # 套衛西窗(對外採光/通風;在主臥西窗下方的牆段)。
        op = add_opening(3, by1 - (yd + ENSUITE_D / 2), 800, "window",
                         by1 - ey1, by1 - yd, blocked_w)
        windows.append(WindowPlacement(3, op))

    # 走道南牆(兩段,中間留通道口正對大門軸線 → 進門直達走道;C1.5b)。
    if has_hall:
        for seg_a, seg_b in ((bx0, living_cx - PASSAGE_WIDTH / 2),
                             (living_cx + PASSAGE_WIDTH / 2, x_he)):
            if seg_b - seg_a > 1:
                walls.append(Wall((seg_a, yc), (seg_b, yc), INT))
    # 走道端牆(吃進廚房角時):廚房門開在這裡(走道 → 廚房)。
    if has_notch:
        walls.append(Wall((x_he, yc), (x_he, yd), INT,
                          openings=[Opening(HALL_DEPTH / 2, DOOR_WIDTH, "door")]))
        doors.append(DoorPlacement(len(walls) - 1, 0, Door(hinge="left", swing="in")))

    # 主臥西窗(有套衛時讓開套衛牆段)、客廳西窗。
    master_hi = (by1 - ey1) if has_ensuite else (by1 - yd)
    op = add_opening(3, master_hi / 2, WINDOW_WIDTHS["bedroom"], "window",
                     0, master_hi, blocked_w)
    windows.append(WindowPlacement(3, op))
    living_cy = (by0 + yc) / 2
    op = add_opening(3, by1 - living_cy, WINDOW_WIDTHS["living"], "window",
                     by1 - yc, by1 - by0, blocked_w)
    windows.append(WindowPlacement(3, op))

    # C1.5c:客廳太寬(只靠西窗會超過採光深度上限)→ 南牆補窗
    # (玄關隔屏牆以東的客廳段)。
    if living_e - bx0 > DAYLIGHT_DEPTH_MAX:
        op = add_opening(0, (living_cx + living_e) / 2 - bx0,
                         WINDOW_WIDTHS["living"], "window",
                         fx1 + 150 - bx0, living_e - bx0, blocked_s)
        windows.append(WindowPlacement(0, op))

    # 餐廳南窗(加分項)。
    if not merged_dining:
        op = add_opening(0, (living_e + sx) / 2 - bx0, WINDOW_WIDTHS["dining"], "window",
                         living_e - bx0, sx - bx0, blocked_s, optional=True)
        if op is not None:
            windows.append(WindowPlacement(0, op))

    # 浴廁門+東窗、廚房門+東窗。
    bath_cy = (by0 + yb) / 2
    op = add_opening(5, bath_cy - by0, 800, "door", 0, yb - by0, [])
    doors.append(DoorPlacement(5, op, Door(hinge="left", swing="in")))
    op = add_opening(1, bath_cy - by0, WINDOW_WIDTHS["bathroom"], "window",
                     0, yb - by0, blocked_e)
    windows.append(WindowPlacement(1, op))
    # 廚房門:走道吃進廚房角時已開在走道端牆;否則開在服務核西牆,
    # 並讓開走道南牆搭在西牆上的 T 形交接點(牆頭不能戳進門洞)。
    kitchen_cy = (yb + yd) / 2
    kitchen_door_y = kitchen_cy            # 廚房門的**實際** y(冰箱要讓開它)
    if not has_notch:
        junction = [(yc - by0 - 150, yc - by0 + 150)] if has_hall else []
        op = add_opening(5, kitchen_cy - by0, DOOR_WIDTH, "door",
                         yb - by0, yd - by0, junction)
        doors.append(DoorPlacement(5, op, Door(hinge="left", swing="in")))
        # 門為了躲柱會偏離 kitchen_cy;冰箱的守門要用「門真正在哪」,不然
        # 門一偏就壓到冰箱(檢核會抓到「家具擋住門的迴轉」而整份設計失敗)。
        kitchen_door_y = by0 + walls[5].openings[op].position
    op = add_opening(1, kitchen_cy - by0, WINDOW_WIDTHS["kitchen"], "window",
                     yb - by0, yd - by0, blocked_e)
    windows.append(WindowPlacement(1, op))

    # ── 房間 ─────────────────────────────────────────────────────────
    rooms: list[Room] = []
    # 客廳(或客餐廳)挖掉西南側的玄關角 → L 形;玄關自成一室(開放,無門)。
    lv_e = sx if merged_dining else living_e
    if fx0 <= bx0:            # 玄關貼西外牆
        lv_pts = [(fx1, by0), (lv_e, by0), (lv_e, yc), (bx0, yc), (bx0, fy1), (fx1, fy1)]
    else:
        lv_pts = [(bx0, by0), (fx0, by0), (fx0, fy1), (fx1, fy1), (fx1, by0),
                  (lv_e, by0), (lv_e, yc), (bx0, yc)]
    rooms.append(Room("客餐廳" if merged_dining else "客廳", lv_pts,
                      kind="living", code=ROOM_CODES["living"]))
    rooms.append(Room("玄關", [(fx0, by0), (fx1, by0), (fx1, fy1), (fx0, fy1)],
                      kind="foyer", code=ROOM_CODES["foyer"]))
    if not merged_dining:
        rooms.append(Room("餐廳", [(living_e, by0), (sx, by0), (sx, yc), (living_e, yc)],
                          kind="dining", code=ROOM_CODES["dining"]))
    if has_hall:
        rooms.append(Room("走道", [(bx0, yc), (x_he, yc), (x_he, yd), (bx0, yd)],
                          kind="corridor", code=ROOM_CODES["corridor"]))
    rooms.append(Room("浴廁", [(sx, by0), (bx1, by0), (bx1, yb), (sx, yb)],
                      kind="bathroom", code=ROOM_CODES["bathroom"]))
    if has_notch:
        # 廚房 L 形(西北角讓給走道端)。
        rooms.append(Room("廚房",
                          [(sx, yb), (bx1, yb), (bx1, yd), (x_he, yd),
                           (x_he, yc), (sx, yc)],
                          kind="kitchen", code=ROOM_CODES["kitchen"]))
    else:
        rooms.append(Room("廚房", [(sx, yb), (bx1, yb), (bx1, yd), (sx, yd)],
                          kind="kitchen", code=ROOM_CODES["kitchen"]))
    slot_names = _house_slot_names(roles)
    for i in range(n):
        if i == 0 and has_ensuite:
            # 主臥 L 形(西南角讓給套衛)+ 主臥浴。
            rooms.append(Room("主臥室",
                              [(ex1, yd), (bed_x[1], yd), (bed_x[1], by1),
                               (bx0, by1), (bx0, ey1), (ex1, ey1)],
                              kind="bedroom", code=ROOM_CODES["bedroom"]))
            rooms.append(Room("主臥浴",
                              [(bx0, yd), (ex1, yd), (ex1, ey1), (bx0, ey1)],
                              kind="bathroom", code=ROOM_CODES["bathroom"]))
        else:
            kind = "study" if roles[i] == "study" else "bedroom"
            rooms.append(Room(slot_names[i],
                              [(bed_x[i], yd), (bed_x[i + 1], yd),
                               (bed_x[i + 1], by1), (bed_x[i], by1)],
                              kind=kind, code=ROOM_CODES[kind]))

    # ── 家具設備(依房型規則自動擺放)──────────────────────────────────
    fixtures: list = []
    # 臥室:床+衣櫃+床頭櫃;書房:書桌+書櫃(皆空間守門,見共用組件)。
    for i in range(n):
        x_l, x_r = bed_x[i], bed_x[i + 1]
        if roles[i] == "study":
            _study_set(fixtures, x_l, x_r, yd, by1)
        else:                        # 主臥/孝親房雙人床,其餘單人
            _bedroom_set(fixtures, x_l, x_r, by1,
                         double=roles[i] in ("master", "elder"))
    # 玄關:鞋櫃貼短隔屏牆內側(讓開大門迴轉方塊)。
    fixtures.append(FixturePlacement(
        "shoe_cabinet", (fx1 - 60, by0 + (fy1 - by0) / 2), 90))
    # 客廳:沙發背靠西牆 + 茶几(沙發前;參考客廳圖塊組——客廳的主角是
    # 沙發+茶几+電視,不是餐桌)。併餐時方桌保留(它就是餐桌),茶几要
    # 讓開它;獨立餐廳時客廳不再擺方桌。
    fixtures.append(FixturePlacement("sofa3", (bx0 + 75, living_cy), 270))
    table_w_edge = living_e - 60                    # 茶几東側的讓位邊界
    if merged_dining:
        # 併餐:方桌居中偏東(原邏輯——避大門迴轉、避玄關,擺不下就略過)。
        tx = (bx0 + living_e) / 2 + 500
        if living_cy - 780 < by0 + ENTRY_DOOR_WIDTH:
            tx = max(tx, entry_x + ENTRY_DOOR_WIDTH / 2 + 780 + 150)
        if living_cy - 780 < fy1:
            tx = max(tx, fx1 + 780 + 150)
        if tx + 780 <= living_e - 60:
            fixtures.append(FixturePlacement("table4", (tx, living_cy), 0))
            table_w_edge = tx - 780 - 150
    # 茶几(沙發前;客廳東側是開放餐廳、沒有牆,故單層不放電視櫃——不硬加)。
    ccx = bx0 + 75 + 850 + 400 + 300                # 沙發前留 400 走道
    if (ccx + 300 <= table_w_edge
            and living_cy - 300 >= max(by0 + 1100, fy1 + 100)):
        fixtures.append(FixturePlacement("coffee_table", (ccx, living_cy), 90))
    # 餐廳(獨立時):餐桌。
    if not merged_dining:
        fixtures.append(FixturePlacement("table4", ((living_e + sx) / 2, living_cy), 0))
    # 浴廁:馬桶+洗手台靠東牆(避開西側門的迴轉)。
    fixtures.append(FixturePlacement("toilet", (bx1 - 75, by0 + bath_d - 500), 90))
    fixtures.append(FixturePlacement("basin", (bx1 - 75, by0 + 600), 90))
    # 主臥浴(套衛):馬桶+洗手台貼西牆(門在東側,迴轉已讓開)。
    if has_ensuite:
        fixtures.append(FixturePlacement("toilet", (bx0 + 75, yd + 550), 270))
        fixtures.append(FixturePlacement("basin", (bx0 + 75, yd + 1450), 270))
    # 廚房:L 型流理台(東牆段+北段,水槽北段、爐具東段;北段東端讓開轉角、
    # 西端讓開廚房門迴轉——門在走道端牆或服務核西牆,皆以 x_he 為準)。
    north_end = x_he + 1000
    if (bx1 - 675) - north_end >= 600:
        fixtures.append(Counter(start=(bx1 - 75, yb + 60), end=(bx1 - 75, yd - 60),
                                stove=True))
        fixtures.append(Counter(start=(bx1 - 675, yd - 60), end=(north_end, yd - 60), sink=True))
    else:                                   # 北段太短 → 一字型,水槽爐具同段
        fixtures.append(Counter(start=(bx1 - 75, yb + 60), end=(bx1 - 75, yd - 60),
                                sink=True, stove=True))
    # 冰箱:貼浴廚分界牆(yb)北側近西端——讓開廚房門的迴轉(用門的**實際**
    # 位置 kitchen_door_y,見上面)與東牆流理台;小廚房擠不下就略過(不硬加)。
    # 迴轉半徑 = 門寬,冰箱上緣要在迴轉圈之外才安全。
    if (yb + 760 <= kitchen_door_y - DOOR_WIDTH / 2 - 150
            and sx + 850 <= bx1 - 675 - 100):
        fixtures.append(FixturePlacement("fridge", (sx + 500, yb + 60), 0))

    spec = FloorPlanSpec(
        site_boundary=[(0, 0), (brief.site_width, 0),
                       (brief.site_width, brief.site_depth), (0, brief.site_depth)],
        setback=brief.setback,
        x_spacings=[grid_x[i + 1] - grid_x[i] for i in range(nx)],
        y_spacings=[ds, dn],
        grid_origin=(bx0, by0),
        column_size=col,
        walls=walls, rooms=rooms, doors=doors, windows=windows, fixtures=fixtures,
        dim_chains=True, sheet=False,
        floor_label=brief.floor_label, north_arrow=True,
    )
    return spec


# ---------------------------------------------------------------------------
# 產生器:集合住宅(單元重複,B6)
# ---------------------------------------------------------------------------
def _resolve_units(brief: CorridorBrief) -> list[UnitSpec]:
    """每排的房型清單:給了 units 就照混,否則同一種單元重複 units_per_row 戶。"""
    if brief.units is not None:
        unit_list = list(brief.units)
    else:
        unit_list = [brief.unit or one_room_unit()] * brief.units_per_row
    n = len(unit_list)
    if not 2 <= n <= 10:
        raise ValueError(f"每排 2~10 戶,收到 {n}")
    depth = unit_list[0].depth
    if any(u.depth != depth for u in unit_list):
        raise ValueError("同一排各房型深度須相同(等深帶);寬度才可不同")
    return unit_list


def _corridor_shell(brief: CorridorBrief, unit_list: list[UnitSpec],
                    stair_windows: bool = True) -> dict:
    """集合住宅的「不變骨架」:外殼牆 + 兩端逃生核(樓梯間/電梯廳+電梯/儲藏)。

    C1.5a:走廊兩端各設一個「核」開間(寬 CORE_W)——北側樓梯間(折返梯,
    門通走廊 → 兩個逃生方向)、南側西端為 電梯廳+電梯、東端為儲藏室。

    標準層(_generate_corridor)與 B1F 地下室(generate_corridor_basement,
    D2)共用同一副骨架:同外殼、同軸網、同逃生核位置 → 疊成多樓層時柱位
    與垂直動線(樓梯/電梯)上下貫通(柱網原則)。stair_windows=False 給
    地下層用(地面下無對外窗)。

    回傳 dict:walls(14 道)/doors/windows/rooms(⚠️ 不含走廊——標準層縱貫
    全樓、地下室只剩兩端短段,由呼叫端自加)/stairs/elevators/geo(關鍵
    座標 SimpleNamespace)/spec_kw(FloorPlanSpec 的共用參數)。
    """
    from src.drafting.balcony_elevator import Elevator
    from src.drafting.stair import UStair

    n = len(unit_list)
    depth = unit_list[0].depth

    x0 = y0 = brief.setback
    widths = [u.width for u in unit_list]
    unit_x = [x0 + CORE_W + sum(widths[:i]) for i in range(n)]   # 各戶左緣(累加)
    y_corr = y0 + depth                         # 走廊下緣
    y_top = y_corr + brief.corridor_width       # 走廊上緣
    xw = x0 + CORE_W                            # 西核/單元分界
    bx1 = xw + sum(widths) + CORE_W             # 建築東緣
    xe = bx1 - CORE_W                           # 單元/東核分界
    by1 = y_top + depth
    y_hall = y_corr - 2200                      # 電梯廳/儲藏分界(西核)

    walls = [
        Wall((x0, y0), (x0, by1), EXT),                   # 0 西端牆
        Wall((bx1, y0), (bx1, by1), EXT),                 # 1 東端牆
        # 核/單元分戶牆(走廊段留空 → 走廊縱貫)。
        Wall((xw, y0), (xw, y_corr), EXT),                # 2
        Wall((xw, y_top), (xw, by1), EXT),                # 3
        Wall((xe, y0), (xe, y_corr), EXT),                # 4
        Wall((xe, y_top), (xe, by1), EXT),                # 5
        # 核開間的南北外牆段(樓梯間開對外窗:採光/排煙;地下層無窗)。
        Wall((x0, by1), (xw, by1), EXT,
             openings=[Opening(CORE_W / 2, 1200, "window")] if stair_windows else []),   # 6 北W
        Wall((xe, by1), (bx1, by1), EXT,
             openings=[Opening(CORE_W / 2, 1200, "window")] if stair_windows else []),   # 7 北E
        Wall((x0, y0), (xw, y0), EXT),                    # 8 南W
        Wall((xe, y0), (bx1, y0), EXT),                   # 9 南E
        # 樓梯間南牆(門通走廊,開向梯間)。
        Wall((x0, y_top), (xw, y_top), EXT,
             openings=[Opening(CORE_W / 2, 900, "door")]),      # 10 W
        Wall((xe, y_top), (bx1, y_top), EXT,
             openings=[Opening(CORE_W / 2, 900, "door")]),      # 11 E
        # 西核:電梯廳/儲藏分界(門進儲藏);東核:儲藏北牆(門通走廊)。
        Wall((x0, y_hall), (xw, y_hall), INT,
             openings=[Opening(850, 800, "door")]),             # 12
        Wall((xe, y_corr), (bx1, y_corr), INT,
             openings=[Opening(CORE_W / 2, 800, "door")]),      # 13
    ]
    doors = [
        DoorPlacement(10, 0, Door(hinge="left", swing="out")),  # 西梯間(+n=北=梯間)
        DoorPlacement(11, 0, Door(hinge="left", swing="out")),  # 東梯間
        DoorPlacement(12, 0, Door(hinge="left", swing="in")),   # 西儲藏(南側)
        DoorPlacement(13, 0, Door(hinge="left", swing="in")),   # 東儲藏(南側)
    ]
    # 梯間對外窗(地下層無)。
    windows = [WindowPlacement(6, 0), WindowPlacement(7, 0)] if stair_windows else []

    rooms = [
        Room("樓梯間", [(x0, y_top), (xw, y_top), (xw, by1), (x0, by1)],
             kind="stair", code=ROOM_CODES["stair"]),
        Room("樓梯間", [(xe, y_top), (bx1, y_top), (bx1, by1), (xe, by1)],
             kind="stair", code=ROOM_CODES["stair"]),
        Room("電梯廳", [(x0, y_hall), (xw - 1400, y_hall), (xw - 1400, y_corr), (x0, y_corr)],
             kind="hall", code=ROOM_CODES["elevator"]),
        Room("電梯", [(xw - 1400, y_hall), (xw, y_hall), (xw, y_corr), (xw - 1400, y_corr)],
             kind="elevator", code=ROOM_CODES["elevator"]),
        Room("儲藏室", [(x0, y0), (xw, y0), (xw, y_hall), (x0, y_hall)],
             kind="storage", code=ROOM_CODES["storage"]),
        Room("儲藏室", [(xe, y0), (bx1, y0), (bx1, y_corr), (xe, y_corr)],
             kind="storage", code=ROOM_CODES["storage"]),
    ]

    # 折返梯 × 2(每端一座,靠梯間北牆,入口在南、留出門的迴轉):
    # 11 級 × 260 = 2860 run + 平台 1200 = 4060 深。
    stair_len = 4060
    stairs = [
        UStair(origin=(x0 + (CORE_W - 2500) / 2, by1 - 75 - stair_len),
               width=2500, length=stair_len, direction="north",
               steps_per_flight=11, tread=260, label="上"),
        UStair(origin=(xe + (CORE_W - 2500) / 2, by1 - 75 - stair_len),
               width=2500, length=stair_len, direction="north",
               steps_per_flight=11, tread=260, label="上"),
    ]
    # 電梯(西核,貼分戶牆;門開西面通電梯廳,電梯廳與走廊開放連通)。
    elevators = [Elevator(origin=(xw - 1400, y_hall), width=1400, depth=2200,
                          door_side="west")]

    spec_kw = dict(
        site_boundary=[(0, 0), (bx1 + brief.setback, 0),
                       (bx1 + brief.setback, by1 + brief.setback),
                       (0, by1 + brief.setback)],
        setback=brief.setback,
        x_spacings=[CORE_W] + widths + [CORE_W],
        y_spacings=[depth, brief.corridor_width, depth],
        grid_origin=(x0, y0),
        column_size=brief.column_size,
        dim_chains=True, sheet=False, north_arrow=True,
    )
    geo = SimpleNamespace(x0=x0, y0=y0, xw=xw, xe=xe, bx1=bx1, by1=by1,
                          y_corr=y_corr, y_top=y_top, y_hall=y_hall,
                          unit_x=unit_x, widths=widths, depth=depth)
    return dict(walls=walls, doors=doors, windows=windows, rooms=rooms,
                stairs=stairs, elevators=elevators, geo=geo, spec_kw=spec_kw)


def _generate_corridor(brief: CorridorBrief) -> FloorPlanSpec:
    """集合住宅標準層:骨架(_corridor_shell)+ 單元×N 鏡射對排 + 雙邊走廊。

    走廊縱貫全樓(含核開間,= 梯廳兼走廊)。
    """
    unit_list = _resolve_units(brief)
    sh = _corridor_shell(brief, unit_list)
    g = sh["geo"]

    rooms = [
        Room("走廊", [(g.x0, g.y_corr), (g.bx1, g.y_corr),
                      (g.bx1, g.y_top), (g.x0, g.y_top)],
             kind="corridor", code=ROOM_CODES["corridor"]),
        *sh["rooms"],
    ]
    spec = FloorPlanSpec(walls=sh["walls"], rooms=rooms, doors=sh["doors"],
                         windows=sh["windows"], stairs=sh["stairs"],
                         elevators=sh["elevators"],
                         floor_label=brief.floor_label, **sh["spec_kw"])
    for u, ux in zip(unit_list, g.unit_x):
        place_unit(spec, u, origin=(ux, g.y_top))                  # 上排
        place_unit(spec, u, origin=(ux, g.y0), mirror_y=True)      # 下排(對排)
    return spec


def generate_corridor_basement(brief: CorridorBrief) -> FloorPlanSpec:
    """集合住宅 B1F 地下室(D2):同骨架(外殼/軸網/逃生核),單元帶換成
    機車停車場 + 車道坡道;核的儲藏室改為 機房(西)/蓄水池(東)。

    * 全樓無對外窗(地面下);外殼牆連續,只在南牆最東一跨開車道口
      (寬 RAMP_GAP,置於跨中央躲柱——分戶牆軸線上都有柱)。
    * 停車空間開放無隔牆:結構柱自然站成柱列,車停柱間(真實地下室慣例)。
    * 走廊只剩兩端核前短段(梯/電梯 → 短走廊 → 停車場)。
    柱位與標準層完全對齊(同軸網;building_generator.check_column_alignment
    把關)。
    """
    unit_list = _resolve_units(brief)
    sh = _corridor_shell(brief, unit_list, stair_windows=False)
    g = sh["geo"]

    # 跨界(分戶牆軸線)→ 車道口取最東一跨的中央,離柱最遠。
    bounds = [g.xw]
    for w_ in g.widths:
        bounds.append(bounds[-1] + w_)
    ramp_x0 = bounds[-2]                        # 坡道帶 = 最東一跨
    ramp_c = (bounds[-2] + bounds[-1]) / 2

    walls = sh["walls"] + [
        # 14 北外牆(標準層由各單元自帶,地下室自己補;無窗)。
        Wall((g.xw, g.by1), (g.xe, g.by1), EXT),
        # 15 南外牆 + 車道口(無門扇的洞)。
        Wall((g.xw, g.y0), (g.xe, g.y0), EXT,
             openings=[Opening(ramp_c - g.xw, RAMP_GAP, "door")]),
    ]

    rooms = [
        # 走廊短段(核前,與停車場開放連通)。
        Room("走廊", [(g.x0, g.y_corr), (g.xw, g.y_corr),
                      (g.xw, g.y_top), (g.x0, g.y_top)],
             kind="corridor", code=ROOM_CODES["corridor"]),
        Room("走廊", [(g.xe, g.y_corr), (g.bx1, g.y_corr),
                      (g.bx1, g.y_top), (g.xe, g.y_top)],
             kind="corridor", code=ROOM_CODES["corridor"]),
        # 停車場(L 形:單元帶全域扣掉東南角坡道)。
        Room("機車停車場", [(g.xw, g.y0), (ramp_x0, g.y0), (ramp_x0, g.y_corr),
                            (g.xe, g.y_corr), (g.xe, g.by1), (g.xw, g.by1)],
             kind="parking", code=ROOM_CODES["parking"]),
        Room("車道坡道", [(ramp_x0, g.y0), (g.xe, g.y0),
                          (g.xe, g.y_corr), (ramp_x0, g.y_corr)],
             kind="ramp", code=ROOM_CODES["ramp"]),
        *sh["rooms"],
    ]
    # 核的儲藏室 → 機電空間(地下室慣例:機房/蓄水池集中在地下)。
    for r in rooms:
        if r.kind == "storage":
            r.name = "機房" if r.points[0][0] == g.x0 else "蓄水池"

    spec = FloorPlanSpec(walls=walls, rooms=rooms, doors=sh["doors"],
                         windows=sh["windows"], stairs=sh["stairs"],
                         elevators=sh["elevators"],
                         floor_label="B1F", **sh["spec_kw"])
    _validate_or_raise(spec, "集合住宅 B1F 地下室")
    return spec


# ---------------------------------------------------------------------------
# 透天多樓層(D2 層別分化):B1F 車庫層 / 1F 公共層 / 2F+ 臥室層
# ---------------------------------------------------------------------------
def _validate_or_raise(spec: FloorPlanSpec, what: str) -> None:
    # Collision Engine(v0.6):validate 之前跑一次家具碰撞修復——有碰撞才動
    # (移動/丟裝飾),沒碰撞完全不動;修不動的留給 validate 報錯(安全網)。
    # 放在這個共用閘門 → 涵蓋透天各層(經 _finish_house)與集合住宅地下室。
    resolve_collisions(spec)
    problems = validate_spec(spec)
    if problems:
        raise ValueError(f"{what} 未通過檢核:\n  - " + "\n  - ".join(problems))


def _finish_house(spec: FloorPlanSpec, f: SimpleNamespace,
                  what: str) -> FloorPlanSpec:
    """透天各層收尾:依變體套整張圖鏡射(樓梯/服務帶方位),再跑檢核。

    骨架與房間都在「標準朝向」(樓梯東、服務帶北)畫好,最後一次鏡射翻成
    這個 seed 抽到的朝向。各層用同一 brief(同 mx/my)→ 翻法一致、柱位仍
    上下對齊。檢核跑在鏡射後的最終結果上。

    Collision Engine(v0.6)在 _validate_or_raise 內、validate 之前跑,故此處
    只要把鏡射定案的 spec 交給它即可。
    """
    if f.v.mx or f.v.my:
        spec = _mirror_spec(spec, f.v.mx, f.v.my)
    _validate_or_raise(spec, what)
    return spec


def _slot(desired: float, widths: list[float], lo: float, hi: float,
          blocked: list[tuple[float, float]], what: str) -> tuple[float, float]:
    """在 [lo,hi] 找不壓柱的開口位置(絕對座標);widths 由寬到窄逐一嘗試
    (躲不開柱就縮小開口),都放不進去就報錯。回傳 (中心位置, 實際寬)。"""
    for w in widths:
        pos = _find_clear_position(desired, w, lo, hi, blocked)
        if pos is not None:
            return pos, w
    raise ValueError(f"{what} 在 {lo/1000:.1f}~{hi/1000:.1f}m 內找不到不壓柱的位置")


# ---------------------------------------------------------------------------
# 設計變體(E2:同一句需求,換 seed 換方案)
# ---------------------------------------------------------------------------
# 「食譜」裡本來寫死的決定,改成「合格範圍內抽籤」。抽的來源是 brief.seed,
# 所以:同 seed → 同一組抽選 → 同一個方案(可重現、可測試);換 seed → 換
# 方案。抽選一律落在既有檢核守得住的範圍內,抽完照跑 validate,不會生出不能
# 住的圖。多樓層各層用同一個 brief(同 seed)→ 抽選一致 → 柱位仍上下對齊。
#
# 兩個大方向靠現成的整張圖鏡射(_mirror_spec)達成,不必重寫房間座標:
#   * mx(左右翻):樓梯/服務核 東端 ↔ 西端。
#   * my(上下翻):服務帶(廚房/臥室)北 ↔ 南。
# 這是「內部實作技巧」——配上開放廚房、主臥比例、開窗位置等其他抽選,兩個
# 不同 seed 的方案不會是彼此的鏡像,而是整體都不同的設計。
@dataclass
class HouseVariant:
    """一組抽定的設計選擇(從 seed 算出,純函式)。

    安全軸靠整張圖鏡射達成、翻完仍藏柱:樓梯東西(mx)、服務帶南北(my);
    另有開放/獨立廚房(開放版留中島腳包柱)、開窗位置(跨內抖動,仍躲柱)。

    主臥倍率(master_ratio)/柱網跨數(bay_pref)也開放抽選了(E2 第二步)——
    它們會改臥室隔牆位置、進而動到三層共用的軸網,所以搭配兩道保護:
      * 1F 廚房牆改成「坐在軸線上」(廚房靠管道牆 xb 側,牆貼軸線→藏柱;
        餐廳內若還有軸線就立短牆包柱),不再獨立算位置跟 2F 臥室牆搶軸線。
      * _house_frame 抽到的 (master_ratio, bay_pref) 若讓軸網無法完全落在
        臥室隔牆上(柱會凸進房間),自動退回較溫和的值——柱網規則性優先
        (使用者定調),見 _house_frame 的可行性守門。
    抽選範圍見 _MASTER_CHOICES / _BAY_CHOICES。
    """

    mx: bool                # 左右翻:樓梯/服務核 東(F)/西(T)
    my: bool                # 上下翻:服務帶 南(T)/北(F)
    kitchen_open: bool      # 1F 廚房 開放式(併入餐廳)/獨立間
    master_ratio: float = MASTER_RATIO   # 主臥加大倍率(抽選;過大會被守門退回)
    bay_pref: int = 0                    # 柱網跨數偏好(-1 少柱大跨 / 0 近目標 / +1 密柱)

    @property
    def note(self) -> str:
        """給使用者看的一行設計說明。"""
        if self.master_ratio >= 1.3:
            master = "主臥放大"
        elif self.master_ratio <= 1.05:
            master = "各房均等"
        else:
            master = "主臥略大"
        bay = {-1: "大跨少柱", 0: "標準柱距", 1: "密柱短跨"}[self.bay_pref]
        # my 不再抽選(客廳固定朝南),朝向不放進說明——說固定的事是噪音。
        return " · ".join([
            f"樓梯{'西' if self.mx else '東'}側",
            "開放式廚房" if self.kitchen_open else "獨立廚房",
            master, bay,
        ])


_MASTER_CHOICES = (1.0, 1.2, 1.35, 1.5)   # 主臥倍率抽選(1.0=各房均等)
_BAY_CHOICES = (-1, 0, 1)                  # 柱網跨數偏好抽選


def _house_variant(brief: HouseBrief) -> HouseVariant:
    """brief.seed → 一組設計選擇(deterministic;同 seed 同結果)。

    ⚠️ 南北鏡射(my)已從抽選池拿掉(建築師檢視 2026-07-20):朝向不是
    風格是物理——隨機上下翻會把客廳/大門翻到背陽的北面、讓廚衛佔住南面
    最好的陽光,那不是「另一個方案」,是把採光邏輯翻反的方案。客廳/起居
    固定朝南;要翻只能來自使用者明確的方位約束(master_corner/kitchen_side,
    單層 C2 路徑)。
    """
    rng = random.Random(brief.seed)
    mx = rng.random() < 0.5
    rng.random()      # 佔位:舊 my 抽籤(保留抽籤順位,同 seed 其他選項不變)
    return HouseVariant(
        mx=mx,
        my=False,     # 客廳/起居固定朝南(見 docstring)
        kitchen_open=rng.random() < 0.5,
        master_ratio=rng.choice(_MASTER_CHOICES),
        bay_pref=rng.choice(_BAY_CHOICES),
    )


def house_design_note(brief: Brief) -> str:
    """這個 brief 的設計說明(給網頁顯示);非透天回空字串。"""
    return _house_variant(brief).note if isinstance(brief, HouseBrief) else ""


def _win_rng(brief: HouseBrief, tag: str) -> random.Random:
    """某層開窗位置抖動用的獨立亂數源(同 seed+層別 → 同結果)。"""
    return random.Random(f"{brief.seed}-{tag}")


def _jitter(rng: random.Random, center: float, lo: float, hi: float,
            span: float = 0.35) -> float:
    """把 center 在 [lo,hi] 內隨機挪動(±span 比例),供開窗位置抽選。
    只給 _slot 當「期望位置」——實際仍會躲柱,躲不掉就退回最近合法位置。"""
    if hi <= lo:
        return center
    reach = (hi - lo) * span / 2
    return _clamp(center + rng.uniform(-reach, reach), lo, hi)


# 同一道牆上兩扇窗之間的最小淨距(牆墩寬)。窄於這個值的牆墩既難施工
# (磚砌不起來、RC 也要特別配筋)、立面也醜——實務兩窗要嘛拉開、要嘛併成
# 一扇寬窗。建築師檢視(2026-07-20)在生成圖上抓到 21cm 牆墩,故加此保護。
MIN_PIER_WIDTH = 600


def _paired_windows(rng: random.Random, lo: float, hi: float,
                    c1: float, c2: float, blocked: list,
                    what1: str, what2: str) -> tuple:
    """同一道牆上的兩扇窗:各在自己半段內抖動,中線兩側各讓 MIN_PIER_WIDTH/2
    → 兩窗淨距永遠 ≥ MIN_PIER_WIDTH,抖動再怎麼抽都不會擠出畸零牆墩。

    回傳 ((pos1, w1), (pos2, w2))。c1/c2 是期望中心(會被夾回各自半段)。
    """
    mid = (lo + hi) / 2
    hi1, lo2 = mid - MIN_PIER_WIDTH / 2, mid + MIN_PIER_WIDTH / 2
    win1 = _slot(_jitter(rng, c1, lo, hi1), [1800, 1500], lo, hi1, blocked, what1)
    win2 = _slot(_jitter(rng, c2, lo2, hi), [1800, 1500], lo2, hi, blocked, what2)
    return win1, win2


def _frame_slot_kinds(brief: HouseBrief) -> list[str]:
    """多樓層透天臥室帶的分間種類:臥室×n(主臥在 index 0)+ 選填書房(東端)。

    書房與臥室同帶(樓上臥室層),孝親房不在此(1F,見 generate_house_public)。
    """
    kinds = ["bedroom"] * brief.bedrooms
    if brief.want_study:
        kinds.append("study")
    return kinds


# ── 臥室帶共用組件(單層/多樓層臥室層共用;書房邏輯只寫一次)──────────────
def _band_slot_openings(f: SimpleNamespace, bed_x: list, kinds: list,
                        rng: random.Random, wall4: int = 4) -> tuple:
    """臥室帶各格的門(帶分界牆 wall4)+ 北窗(北外牆 index 1);書房同樣配門窗。

    回傳 (band_open, doors, win_open, windows):band_open/win_open 是要塞進
    對應牆的 Opening 清單,doors/windows 是對應的放置。呼叫端接著補衛浴/樓梯。
    """
    band_open: list[Opening] = []
    doors: list[DoorPlacement] = []
    win_open: list[Opening] = []
    windows: list[WindowPlacement] = []
    for i in range(len(kinds)):
        label = "書房" if kinds[i] == "study" else f"臥室{i+1}"
        cx = (bed_x[i] + bed_x[i + 1]) / 2
        dpos, dw = _slot(cx, [DOOR_WIDTH, 750], bed_x[i], bed_x[i + 1],
                         f.blocked, f"{label}門")
        doors.append(DoorPlacement(wall4, len(band_open),
                                   Door(hinge="left", swing="out")))
        band_open.append(Opening(dpos - f.bx0, dw, "door"))
        wpos, ww = _slot(_jitter(rng, cx, bed_x[i], bed_x[i + 1]),
                         [1500, 1200, 900], bed_x[i], bed_x[i + 1],
                         f.blocked, f"{label}窗")
        windows.append(WindowPlacement(1, len(win_open)))
        win_open.append(Opening(wpos - f.bx0, ww, "window"))
    return band_open, doors, win_open, windows


def _band_rooms(bed_x: list, kinds: list, ydiv: float, ytop: float) -> list:
    """臥室帶的房間:臥室(主臥在 index 0)+ 書房(kind=study,code X12)。"""
    rooms: list = []
    bedno = 0
    for i in range(len(kinds)):
        pts = [(bed_x[i], ydiv), (bed_x[i + 1], ydiv),
               (bed_x[i + 1], ytop), (bed_x[i], ytop)]
        if kinds[i] == "study":
            rooms.append(Room("書房", pts, kind="study", code=ROOM_CODES["study"]))
        else:
            bedno += 1
            name = "主臥室" if bedno == 1 else f"臥室{bedno}"
            rooms.append(Room(name, pts, kind="bedroom", code=ROOM_CODES["bedroom"]))
    return rooms


def _bedroom_set(fixtures: list, x_l: float, x_r: float, ytop: float,
                 double: bool) -> None:
    """一間臥室的家具:床(頭靠 ytop 北牆)+ 衣櫃(東牆角)。

    (使用者定調:家具成套只升級客廳,臥室維持床+衣櫃的簡潔畫法。)"""
    bed_half = 800 if double else 500
    bed = "bed_double" if double else "bed_single"
    cx = min((x_l + x_r) / 2, x_r - bed_half - 710)
    fixtures.append(FixturePlacement(bed, (cx, ytop - 75), 180))
    fixtures.append(FixturePlacement("wardrobe", (x_r - 60, ytop - 75 - 750), 90))


def _study_set(fixtures: list, x_l: float, x_r: float,
               ydiv: float, ytop: float) -> None:
    """書房家具:書桌(貼北牆,椅朝南)。"""
    fixtures.append(FixturePlacement("desk", ((x_l + x_r) / 2, ytop - 75), 180))


def _band_fixtures(f: SimpleNamespace, bed_x: list, kinds: list,
                   ydiv: Optional[float] = None) -> list:
    """臥室帶家具:臥室=床+衣櫃+床頭櫃;書房=書桌+書櫃(皆空間守門)。"""
    fixtures: list = []
    bedno = 0
    for i in range(len(kinds)):
        x_l, x_r = bed_x[i], bed_x[i + 1]
        if kinds[i] == "study":
            _study_set(fixtures, x_l, x_r,
                       ydiv if ydiv is not None else f.yn, f.by1)
            continue
        bedno += 1
        _bedroom_set(fixtures, x_l, x_r, f.by1, double=(bedno == 1))
    return fixtures


def _sofa_suite(fixtures: list, x_w: float, cy: float,
                y_lo: float, y_hi: float,
                tv_x: Optional[float] = None,
                tv_lo: Optional[float] = None,
                tv_hi: Optional[float] = None) -> None:
    """客廳成套家具(參考客廳圖塊組的擺法):沙發背西牆 + 茶几,放得下再加
    對坐單人沙發×2(茶几南北側)與東牆電視櫃——塞不下就各自略過(不硬加)。
    """
    fixtures.append(FixturePlacement("sofa3", (x_w + 75, cy), 270))
    ccx = x_w + 75 + 850 + 400 + 300               # 沙發前緣留 400 走道,茶几半寬
    fixtures.append(FixturePlacement("coffee_table", (ccx, cy), 90))
    # 對坐單椅(900×850):茶几南北各一張,面向茶几;南北向都留 150 淨距。
    if cy - 1775 >= y_lo + 150 and cy + 1775 <= y_hi - 150:
        fixtures.append(FixturePlacement("armchair", (ccx, cy - 1775), 0))
        fixtures.append(FixturePlacement("armchair", (ccx, cy + 1775), 180))
    # 電視櫃(1600×450)貼東牆面向沙發:牆段夠長、且離茶几/單椅群夠遠才放。
    if tv_x is not None and tv_lo is not None and tv_hi is not None:
        tcy = (tv_lo + tv_hi) / 2
        if (tv_hi - tv_lo >= 1600 + 300
                and tv_x - 510 >= ccx + 450 + 150):
            fixtures.append(FixturePlacement("tv_cabinet", (tv_x - 60, tcy), 90))


# 中島吧台(開放餐廚專用):檯面長×深、兩張吧檯椅間距。
ISLAND_LEN = 1400
ISLAND_DEPTH = 550
ISLAND_STOOL_GAP = 500


def _kitchen_island(fixtures: list, xk: float, xb: float,
                    yd: float, by1: float) -> None:
    """開放餐廚的中島吧台:廚房區(xk~xb)南側地坪加一座獨立中島
    (檯面 + 2 張吧檯椅),界定「烹飪動線」與「開放地坪」。

    不加這個的話,一整間 40+m² 的餐廚只有沿北牆一字型流理台,中段大片
    地坪完全空著——不像有人設計過(建築師檢視 2026-07-20)。位置固定在
    廚房區(靠管道牆 xb 那側)南側、遠離北牆主流理台/冰箱與西側餐廳的
    餐桌,兩者間隔皆 >1m(見呼叫端座標推導),不會互相碰撞。

    放不下(廚房區太窄,或北帶太淺以致南側地坪不足)就略過,不硬加——
    同 _kitchen_fridge/_sofa_suite 的守門風格。
    """
    zone_w = xb - xk
    if zone_w < ISLAND_LEN + 900 or by1 - yd < 4200:
        return
    cx = xk + zone_w * 0.55                        # 略偏東,讓開西側餐廳/走道
    y_back = yd + 1700                              # 檯面北緣(離北牆主流理台/冰箱夠遠)
    fixtures.append(Counter(
        start=(cx + ISLAND_LEN / 2, y_back),
        end=(cx - ISLAND_LEN / 2, y_back), depth=ISLAND_DEPTH))
    stool_y = y_back - ISLAND_DEPTH - 300           # 吧檯椅(南側,面北)
    for dx in (-ISLAND_STOOL_GAP / 2, ISLAND_STOOL_GAP / 2):
        fixtures.append(FixturePlacement("bar_stool", (cx + dx, stool_y)))


def _kitchen_fridge(fixtures: list, xb: float, ytop: float,
                    door_y_max: float) -> None:
    """冰箱(700×700)貼廚房管道牆(xb),在北牆流理台下方——冰箱底緣要離
    「南側門的迴轉範圍上緣」(door_y_max)≥100 才放(小廚房塞不下就略過)。"""
    cy = ytop - 675 - 100 - 350                    # 流理台(深 600+牆 75)下方
    if cy - 350 >= door_y_max + 100:
        fixtures.append(FixturePlacement("fridge", (xb - 60, cy), 90))


def _frame_program_kinds(kinds: list[str]) -> list[str]:
    """臥室帶的分間種類 → 面積程式的房間種類(index 0 一律是主臥)。

    _frame_slot_kinds 給的是幾何用的分間表(["bedroom","bedroom","study"]),
    面積程式要區分主臥/次臥(兩者面積範圍與優先序都不同),在這裡轉換。
    """
    return [k if k == "study" else ("master_bedroom" if i == 0 else "bedroom")
            for i, k in enumerate(kinds)]


# 主臥獨立加深的上限(mm)。使用者 2026-07-20:「第一版只允許主臥採用不同
# 深度,其餘維持同一條帶深」。⚠️ 目前只計算不套用到幾何——這副南北兩帶的
# 骨架給不出突出空間(南帶自己只有 3.0~4.5m 深),詳見 _solve_frame_program
# 內的說明。常數留著,等 solve_band 換成二維裝箱後才會真正生效。
MASTER_DEPTH_BONUS_MAX = 2400

# Living Overflow 值得切出來的最小寬(mm):窄於此就不切,寧可客廳略超 aspect
# (差一點點不值得多一道牆);≥此就交給 Program Selector 決定當什麼房間。
OVERFLOW_MIN_WIDTH = 1500


def _solve_frame_program(slot_kinds: list[str], w_avail: float, d_avail: float,
                         master_ratio: float) -> SimpleNamespace:
    """Requirement → Area → Shape(多樓層透天骨架的前三段)。

    輸入這層樓「可用的外框」(w_avail × d_avail,已扣退縮與天井帶),輸出
    骨架需要的幾個刀位尺寸:

        dn        北帶(臥室帶)進深 —— 帶內各房共用
        ds        南帶(起居/客廳帶)進深
        bonus     主臥額外加深的「保留量」(只算不用,原因見下方註解)
        bed_w[]   臥室帶各格寬度
        bath_w    衛浴(濕區)開間寬 —— 以前是寫死的 WET_W=2000

    面積預算 = 外框面積 - 樓梯間(垂直動線核,不是「房間」,不參與面積分配)。
    dn 會回頭改變樓梯間佔掉的面積,所以跑三輪讓它收斂(實測第二輪就穩定)。

    ⚠️ 硬約束(結構/法規/家具,**不是**美感偏好,故凌駕面積目標):
      * dn ≥ STAIRWELL_MIN_DEPTH:單跑直梯要這麼長才放得下。
      * dn + bonus ≤ DAYLIGHT_DEPTH_MAX:離北窗太遠就是暗房(C1.5c)。
      * ds ≥ MIN_SOUTH_BAND_DEPTH、≤ DAYLIGHT_DEPTH_MAX(南窗同理)。
      * 每格寬 ≥ 該房型的 min_width(擺得下家具)。
    面積目標守不住時一律讓步給上面這些,由呼叫端的 ±10% 容許誤差吸收。
    """
    prog_kinds = list(slot_kinds) + ["bathroom", "living"]
    reqs = [requirement(k) for k in prog_kinds]
    bed_reqs, bath_req, live_req = reqs[:-2], reqs[-2], reqs[-1]
    n_bed = len(bed_reqs)

    lo_d = float(STAIRWELL_MIN_DEPTH)
    hi_d = min(float(DAYLIGHT_DEPTH_MAX), d_avail - MIN_SOUTH_BAND_DEPTH)
    if hi_d < lo_d:
        raise ValueError(
            f"可建進深 {d_avail/1000:.1f}m 不足:樓梯間需 "
            f"{STAIRWELL_MIN_DEPTH/1000:.1f}m + 公共帶需 "
            f"{MIN_SOUTH_BAND_DEPTH/1000:.1f}m")

    # 幾何容量 —— 帶狀格局的硬天花板,必須先於面積分配算出來:
    #   * 臥室帶再怎麼分,也超不過「帶寬 × 帶深上限(採光深度)」;
    #   * 起居帶同理,受剩下的進深限制。
    # 不先收緊的話,窄基地會分到裝不下的面積,到幾何階段才發現塞不下,整份
    # 設計就被判失敗(F3 初版的 bug:18×13m 三房本來生得出來,卻報「最窄房寬
    # 不足」)。收緊之後會自動退回「較小但放得下」的方案。
    band_w = w_avail - STAIRWELL_W - bath_req.min_width
    bed_cap_total = band_w * hi_d / 1_000_000
    bed_max_sum = sum(r.max_area for r in bed_reqs) or 1.0
    bed_scale = min(1.0, bed_cap_total / bed_max_sum)
    caps = ([r.max_area * bed_scale for r in bed_reqs]
            + [bath_req.max_area,
               w_avail * min(float(DAYLIGHT_DEPTH_MAX), d_avail - lo_d) / 1_000_000])

    dn = lo_d                                   # 起始估計(用來估樓梯間佔掉的面積)
    bed_w: list[float] = []
    bath_w = bath_req.min_width
    plan = None
    for _ in range(3):
        budget = (w_avail * d_avail - STAIRWELL_W * dn) / 1_000_000
        plan = allocate_areas(prog_kinds, budget, caps=caps)
        targets = list(plan.areas)
        # 主臥倍率(E2 抽選)只是「在合格範圍內偏好大一點/小一點的主臥」,
        # 現在改成微調主臥的面積目標,而不是直接乘寬度——這樣同一顆 seed
        # 還是換得到方案,但主臥不會脹到超過 max_area。
        if n_bed:
            targets[0] = min(bed_reqs[0].max_area,
                             targets[0] * master_ratio / MASTER_RATIO)
        bath_t, live_t = targets[-2], targets[-1]
        band_w_avail = w_avail - STAIRWELL_W - bath_req.min_width
        dn, bed_w = solve_band(targets[:n_bed], bed_reqs,
                               width_avail=band_w_avail,
                               depth_bounds=(lo_d, hi_d))
        bath_w = max(bath_req.min_width, bath_t * 1_000_000 / dn)

    # 主臥寬度上限 + (保留的)獨立進深。
    #
    # 主臥的目標面積(max 28m²)比次臥(max 20m²)大四成。全靠**寬度**吃下去
    # 的話,臥室隔牆就會一格寬一格窄,柱跨跟著忽大忽小——BAY_RATIO_MAX 守不住,
    # 或守住了但軸線挑不到牆(孤柱)。所以先把主臥的寬度壓在「次臥平均寬 ×
    # master_ratio」以內,隔牆間距回到均勻,柱跨才規則(柱網優先,使用者定調)。
    #
    # 被壓掉的那點面積,原本要靠「主臥往南突出、獨立加深」補回來(使用者
    # 2026-07-20:第一版只允許主臥用不同深度)。bonus 就是那個突出量,**但目前
    # 只算不用**——量測後發現這副骨架給不出空間:南帶(起居/客廳)本身只有
    # 3.0~4.5m 深,扣掉它自己的下限 MIN_SOUTH_BAND_DEPTH 後,主臥能突出的餘裕
    # 只剩 0~1.5m,實際算出來多半是 ~0.2m。為了 20cm 去改「門掛在哪道牆上」
    # 的接線(主臥門得從帶分界牆搬到突出後的新南牆)不划算,故第一版保留計算、
    # 不動幾何。真正要讓「每間各自進深」有意義,得等 solve_band 換成二維裝箱
    # (見 room_program 的 PENDING 5),那時各房不必再共用一條帶。
    bonus = 0.0
    if n_bed > 1:
        others = bed_w[1:]
        cap_w = master_ratio * sum(others) / len(others)   # 主臥寬度上限(相對次臥)
        if bed_w[0] > cap_w + 1:
            surplus = (bed_w[0] - cap_w) * dn              # 被砍掉的面積(mm²)
            bed_w[0] = cap_w
            bonus = min(surplus / cap_w, float(MASTER_DEPTH_BONUS_MAX),
                        float(DAYLIGHT_DEPTH_MAX) - dn)
            bonus = max(0.0, bonus)

    # 南帶進深:起居室要拿到目標面積,還要補回被主臥挖走的那一塊。
    width = sum(bed_w) + bath_w + STAIRWELL_W
    ds = (live_t * 1_000_000 + (bed_w[0] * bonus if n_bed else 0.0)) / width
    ds = _clamp(ds, MIN_SOUTH_BAND_DEPTH + bonus, float(DAYLIGHT_DEPTH_MAX))
    if dn + ds > d_avail:                       # 進深不夠 → 先縮南帶,再縮突出
        ds = d_avail - dn
        if ds < MIN_SOUTH_BAND_DEPTH + bonus:
            bonus = max(0.0, ds - MIN_SOUTH_BAND_DEPTH)
    return SimpleNamespace(dn=dn, ds=ds, bonus=bonus, bed_w=bed_w,
                           bath_w=bath_w, width=width, plan=plan)


def _house_frame(brief: HouseBrief) -> SimpleNamespace:
    """透天多樓層的「不變骨架」(D2):外殼、南北兩帶、東端[濕區|樓梯間]、軸網。

    層別分化的關鍵不變量——各層(B1F/1F/2F+)共用:
      * 同外殼、同軸網 → 柱位上下對齊(check_column_alignment 把關);
      * 樓梯間同位(xs~bx1 × 北帶東端)→ 梯上下貫通;
      * 濕區同位(xb~xs × 北帶):1F 衛浴、2F 衛浴、B1F 機房疊同一條
        給排水管道(管道間上下對齊,實務常識)。

    柱藏牆交點(使用者反饋 2026-07-14:柱要站在兩道豎牆的交會處,不能凸在
    房間牆段中間):臥室隔牆位置在骨架裡就先定好,列入軸網的吸附目標
    (_plan_x_grid),再做反向吸附(隔牆挪到軸線上,同 C1.5c 隔間坐樑);
    1F 廚房東牆同樣吸附。中間軸線的柱因此都落在豎牆交點,不會凸進房間。
    """
    bx0 = by0 = brief.setback
    bx1 = brief.site_width - brief.setback
    by1 = brief.site_depth - brief.setback
    W, D = bx1 - bx0, by1 - by0
    if W < 10000 or D < 7000:
        raise ValueError(
            f"多樓層透天需建築範圍約 ≥10×7m,現為 {W/1000:.1f}×{D/1000:.1f}m")
    if not 1 <= brief.bedrooms <= 4:
        raise ValueError(f"支援 1~4 間臥室,收到 {brief.bedrooms}")

    v = _house_variant(brief)                       # 這棟樓的設計抽選(同 seed 同結果)

    # 深基地兩段式消化(使用者反饋 2026-07-15 兩則:19×19 生不出來/20×38
    # 只蓋 11.5m 深「不像真正的設計師」):
    #   1. 深到兩帶塞不下 → 插「天井帶」變三帶(南帶 6m + 天井帶 2.8~4.3m +
    #      北帶 5.5m,最深 15.8m),中段靠天井採光——真實街屋的做法;
    #   2. 更深 → 封頂留前後院置中(基地再深房子也不會無限深,院子才是對的)。
    # 各層同一組帶深 → 外殼/軸網/天井位置上下一致。
    has_patio = D >= MAX_HOUSE_DEPTH + PATIO_BAND_RANGE[0]
    dp = _clamp(D - MAX_HOUSE_DEPTH, *PATIO_BAND_RANGE) if has_patio else 0.0
    d_band = D - dp                            # 南北兩帶可用的總進深(扣掉天井帶)

    # 臥室隔牆 + 軸網,由「主臥倍率 mr、跨數偏好 bp」一起決定(見下方守門)。
    # 臥室帶的分間程式(E3):臥室×n(主臥在西端 index 0)+ 選填書房(東端,
    # 靠濕區,略窄)。孝親房不在此(單層另加、多樓層在 1F,不上樓)。
    kinds = _frame_slot_kinds(brief)
    nslot = len(kinds)
    prog_kinds = _frame_program_kinds(kinds)
    mins = [requirement(k).min_width for k in prog_kinds]

    # 建築尺寸現在由面積程式決定(F3),不再是「攤滿可建範圍再封頂」——
    # 房間各有 min/preferred/max 面積,全員吃飽仍有剩的地就留成院子
    # (使用者 2026-07-20 定調:餘量不再無腦塞給客廳)。⚠️ 尺寸隨 mr 變,
    # 故 mr 一變整個外殼跟著重算,守門才能真的換掉會產生孤柱的方案。
    def _try(mr: float, bp: int):
        try:
            pg = _solve_frame_program(prog_kinds, W, d_band, mr)
        except ValueError:
            return None
        bw, w = pg.bed_w, pg.width
        if w > W + 1:                          # 程式要的比可建範圍還寬 → 不可行
            return None
        if any(bw[i] < mins[i] - 1 for i in range(nslot)):   # 每格各有最小寬
            return None
        x0 = bx0 + (W - w) / 2                 # 建築在可建範圍內置中(留側院)
        x1 = x0 + w
        xs_ = x1 - STAIRWELL_W                 # 樓梯間西牆
        xb_ = xs_ - pg.bath_w                  # 濕區西牆(管道牆)
        bx = [x0]
        for bwi in bw:
            bx.append(bx[-1] + bwi)            # bx[-1] == xb_
        gx = _plan_x_grid(x0, w, bx[1:-1] + [xb_], prefer=bp)
        # 反向吸附:隔牆距軸線 < WALL_SNAP_TOL 就挪到軸線上(柱正好站在
        # 隔牆與帶分界牆/北外牆的交點);吸附後房寬仍須合格。
        # ⚠️ 這一步會把面積推離目標——刻意的:柱網優先於面積(使用者定調),
        #    偏移量由 AREA_TOLERANCE(±10%)吸收。
        for i in range(1, nslot):
            g = min(gx, key=lambda t: abs(t - bx[i]))
            if 0 < abs(g - bx[i]) < WALL_SNAP_TOL:
                trial = bx[:i] + [g] + bx[i + 1:]
                tw = [trial[j + 1] - trial[j] for j in range(nslot)]
                if (all(tw[j] >= mins[j] for j in range(nslot))
                        and tw[0] >= max(tw)):
                    bx = trial
        cover = set(bx) | {xb_}
        orphans = sum(1 for g in gx[1:-1]
                      if not any(abs(g - c) < 1 for c in cover))
        return SimpleNamespace(bx0=x0, bx1=x1, W=w, xs=xs_, xb=xb_,
                               bed_x=bx, grid_x=gx, orphans=orphans,
                               dn=pg.dn, ds=pg.ds, master_bonus=pg.bonus,
                               plan=pg.plan)

    # 守門:抽到的 (mr, bp) 先試;若軸網有孤柱(柱凸進房間),往主臥較小、
    # 跨數近目標退,取第一個「零孤柱」方案(柱網規則性優先,使用者定調)。
    eff_mr, eff_bp, chosen = v.master_ratio, v.bay_pref, None
    for mr, bp in [(v.master_ratio, v.bay_pref), (v.master_ratio, 0),
                   (MASTER_RATIO, v.bay_pref), (MASTER_RATIO, 0),
                   (1.0, v.bay_pref), (1.0, 0)]:
        r = _try(mr, bp)
        if r is None:
            continue
        if chosen is None:                      # 保底:可行方案(可能仍有孤柱)
            chosen, eff_mr, eff_bp = r, mr, bp
        if r.orphans == 0:                      # 零孤柱 → 定案
            chosen, eff_mr, eff_bp = r, mr, bp
            break
    if chosen is None:
        # 讓面積程式自己講原因(哪一種房間的最低需求湊不出來);它若過得了,
        # 就是卡在最小房寬。
        _solve_frame_program(prog_kinds, W, d_band, MASTER_RATIO)
        raise ValueError(
            f"{brief.bedrooms} 房分下來最窄房寬不足 {MIN_BEDROOM_WIDTH/1000:.1f}m,"
            f"請加大基地或減臥室")
    bx0, bx1, W = chosen.bx0, chosen.bx1, chosen.W
    xs, xb, bed_x, grid_x = chosen.xs, chosen.xb, chosen.bed_x, chosen.grid_x
    dn, ds, master_bonus = chosen.dn, chosen.ds, chosen.master_bonus

    # 客廳 min_depth 參與生成(使用者 2026-07-21):有多餘進深(本來要留前後院
    # 的)就先加深南帶,讓客廳達到 min_depth——深院不如深客廳。⚠️ 只吃「原本
    # 要變院子」的餘量,絕不吃臥室帶的深度(那會讓臥室變窄,踩過 18×13 的坑);
    # 上限採光深度。淺基地沒餘量就維持原 ds,結構/基地優先。
    live_req = requirement("living")
    slack = D - dn - ds - dp
    if slack > 1 and ds < live_req.min_depth:
        ds += min(slack, live_req.min_depth - ds, float(DAYLIGHT_DEPTH_MAX) - ds)

    # 建築進深也由面積程式決定 → 多出來的地變前後院(置中)。
    d_used = ds + dp + dn
    if D > d_used:
        side = (D - d_used) / 2
        by0, by1, D = by0 + side, by1 - side, d_used
    yd = by0 + ds                              # 南帶北緣(帶分界牆 1)
    yn = yd + dp                               # 北帶南緣(無天井時 = yd)

    # ── Living Overflow(2026-07-21):客廳/起居室過細長就切出溢位空間 ────
    # 兩帶式的客廳橫跨整條南帶,寬=建築全寬、深只 3~4m → 寬房子長寬比 5~7 的
    # 長條(benchmark 首要問題)。這裡算「客廳最寬能到 aspect_max×深」,超出的
    # 寬度 = overflow,切在西端(客廳留東端,靠 1F 玄關/2F 樓梯);overflow 當
    # 什麼房間由各層的 select_overflow_program 依脈絡決定。天井版另有骨架
    # (家庭廳繞天井),不走這條。x_lv = 客廳西牆 = overflow 東牆。
    x_lv = None
    if not has_patio:
        live_req = requirement("living")
        cw = compact_width(ds, live_req)       # 客廳守 aspect_max 的最大寬
        overflow_w = W - cw
        if overflow_w >= OVERFLOW_MIN_WIDTH and cw >= live_req.min_width:
            x_raw = bx1 - cw                   # 客廳西牆(overflow 在其西)
            # 貼近軸線就吸附(柱藏牆內);吸附不得讓客廳變寬到破 aspect,
            # 也不得讓 overflow 窄於下限——否則維持 x_raw(輕隔間,不落柱)。
            g = min(grid_x, key=lambda t: abs(t - x_raw))
            if (abs(g - x_raw) <= WALL_SNAP_TOL and g >= x_raw - 1
                    and g - bx0 >= OVERFLOW_MIN_WIDTH):
                x_lv = g
            else:
                x_lv = x_raw

    # 1F 廚房|餐廳的分界牆坐在軸線上——廚房靠管道牆 xb 側(與衛浴共用給排水
    # 立管,真實常識),牆貼最東一條內部軸線 → 1F 的柱也藏進豎牆,與 2F 臥室
    # 牆共用同一批軸線,不再各算各的位置搶軸線。餐廳(西側)內若還有更西的
    # 軸線,由呼叫端立短牆(柱包)蓋住;無內部軸線可藏就給廚房固定寬。
    west_lines = [g for g in grid_x[1:-1] if g < xb - 1]
    if west_lines:
        xk = max(west_lines)
    else:
        xk = xb - _clamp((xb - bx0) * 0.3, *SERVICE_WIDTH_RANGE)

    # 天井位置(有天井帶時):貼西外牆的矩形採光井 [bx0, xp]×[yd, ypn],
    # 北側留 HALL_DEPTH 走道(臥室門一律開向走道、不會開進露天的天井)。
    # 天井東牆 xp 儘量吸附軸線(柱藏進天井角的牆交點);各層同一位置直落。
    xp = ypn = xdz = None
    if has_patio:
        xp = bx0 + _clamp(W * 0.28, 3000, 4500)
        g = min(grid_x, key=lambda t: abs(t - xp))
        if abs(g - xp) <= GRID_SNAP_TOL and 2800 <= g - bx0 <= 5500:
            xp = g
        ypn = yn - HALL_DEPTH                  # 天井北牆(走道南緣)

        # 天井帶東段(1F 餐廳 / 2F 家庭廳)大到超過合理上限就切一刀:餐廳是
        # L 形——東段(寬 bx1-xp、深 dp)+ 北側走道段(寬 xp-bx0、深
        # HALL_DEPTH,連接天井與東段的固定通道,不能切掉)。dp 只依 D 決定、
        # 跟房間面積無關,基地一深東段就無限拉長(改造中途實測 32×26 基地
        # 量出 89m² 的「餐廳」——跟兩帶版被砍掉的「大餐廳」問題同一個病)。
        # 切點靠面積程式的「餐廳」需求回推(扣掉走道段面積,那塊不會消失、
        # 仍算在餐廳頭上);兩層樓共用同一個 xdz(骨架不變的原則),切出來
        # 的東段落在建築東外牆上,改叫「儲藏室」(不需要窗,見 validate_spec)。
        east_w = bx1 - xp
        walk_area = (xp - bx0) * HALL_DEPTH        # 走道段面積(固定,算進餐廳)
        dine_req = requirement("dining")
        if (dine_req.max_area is not None and east_w > 0
                and (east_w * dp + walk_area) / 1_000_000
                > dine_req.max_area * (1 + AREA_TOLERANCE)):
            keep = _clamp((dine_req.preferred_area * 1_000_000 - walk_area) / dp,
                          dine_req.min_width, east_w - MIN_STUDY_WIDTH)
            if keep > 0 and east_w - keep >= MIN_STUDY_WIDTH:
                xdz = xp + keep

    blocked = _blocked(grid_x, brief.column_size)   # 開口躲柱用(X 向各牆線通用)

    # 柱位微調:柱(500 見方)比牆(INT120)胖,壓在軸線交點上會兩邊各凸出
    # 約 190mm,朝房間那側就變成「柱探進房間」(使用者反饋 2026-07-15,附
    # AutoCAD 截圖指的是南北帶分界牆上的 T 型柱:南側是大客廳/起居室,柱下
    # 半截凸進去)。把分界牆這一排柱心往北(服務/臥室帶)推,讓南面貼齊分界
    # 牆南皮 → 南帶淨空;分界牆上的垂直骨架牆(濕區/樓梯間西牆)都往北走,
    # 柱正好藏進交點。外牆柱維持在軸線上(不動,避免與門窗定位打架)。
    # 推移量只依分界牆(每層都相同的骨架)決定 → 各層推法一致,柱位仍上下
    # 對齊(check_column_alignment 照過)。
    s_div = brief.column_size / 2 - INT / 2         # 往北推、南面貼分界牆南皮
    div_rows = [yd] + ([yn] if has_patio else [])   # 帶分界牆的 y(天井時兩條)
    y_axes = [by0] + div_rows + [by1]

    def _tuck(x: float, y: float) -> Point:
        dy = s_div if any(abs(y - r) < 1 for r in div_rows) else 0.0
        return (x, y + dy)

    column_centers = [_tuck(x, y) for x in grid_x for y in y_axes]

    spec_kw = dict(
        site_boundary=[(0, 0), (brief.site_width, 0),
                       (brief.site_width, brief.site_depth),
                       (0, brief.site_depth)],
        setback=brief.setback,
        x_spacings=[grid_x[i + 1] - grid_x[i] for i in range(len(grid_x) - 1)],
        y_spacings=[ds, dp, dn] if has_patio else [ds, dn],
        grid_origin=(bx0, by0),
        column_size=brief.column_size,
        column_centers=column_centers,
        dim_chains=True, sheet=False, north_arrow=True,
    )
    return SimpleNamespace(bx0=bx0, by0=by0, bx1=bx1, by1=by1, W=W, D=D,
                           dn=dn, ds=ds, yd=yd, yn=yn, dp=dp,
                           has_patio=has_patio, xp=xp, ypn=ypn, xdz=xdz,
                           x_lv=x_lv, xs=xs, xb=xb, xk=xk,
                           west_lines=west_lines, bed_x=bed_x, grid_x=grid_x,
                           slot_kinds=kinds, prog_kinds=prog_kinds,
                           master_bonus=master_bonus, area_plan=chosen.plan,
                           blocked=blocked, v=v, eff_master_ratio=eff_mr,
                           eff_bay_pref=eff_bp, spec_kw=spec_kw)


def _house_stair(f: SimpleNamespace, label: str = "上"):
    """樓梯間裡的單跑直梯 + 中央扶手(每層同一座、同一位置)。

    使用者 2026-07-20 依實際樓梯平面圖定調:透天用單跑直梯(不折返),
    中央一道扶手 + 兩端立柱(畫圖層 draw_stair 負責)。級數由樓梯間可用
    進深回推(填滿 f.dn,頂端留 HOUSE_STAIR_TOP_LANDING 平台),故北帶已
    先加深(STAIRWELL_MIN_DEPTH),整段梯跑放得下、不會報放不下。

    樓梯間在北帶東端,南緣 = f.yn(無天井時 = f.yd,有天井時 = 天井帶北緣)。
    """
    from src.drafting.stair import Stair
    length = f.dn - 300                                    # 樓梯間內淨長(南北牆各縮 150)
    run = length - HOUSE_STAIR_TOP_LANDING                 # 梯跑可用長(頂端留平台)
    steps = max(2, int(run / HOUSE_STAIR_TREAD))           # 級數填滿可用長
    return Stair(origin=(f.xs + 150, f.yn + 150), width=STAIRWELL_W - 300,
                 length=length, direction="north",
                 steps=steps, tread=HOUSE_STAIR_TREAD, label=label)


# ── Living Overflow 共用組件(1F 客廳 / 2F 起居室 共用)────────────────────
def _plan_overflow(f: SimpleNamespace, brief: HouseBrief, floor: str, *,
                   has_study: bool, has_family: bool) -> Optional[SimpleNamespace]:
    """骨架給了溢位幾何(f.x_lv = 客廳西牆),這裡叫 Program Selector 決定那塊
    空間當什麼房間。回傳 None(無溢位)或 SimpleNamespace(x0/x1 西東界、
    kind/name 用途、connect "open"=無門扇通道 / "door"=有門扇)。

    溢位在南帶西端 [bx0, x_lv];客廳留東端(靠 1F 玄關/2F 樓梯的生活重心)。"""
    if f.x_lv is None:
        return None
    kind, name = select_overflow_program(
        floor=floor, bedrooms=brief.bedrooms, want_study=brief.want_study,
        has_study=has_study, has_family=has_family,
        width_mm=f.x_lv - f.bx0, depth_mm=f.ds)
    connect = "open" if kind == "family" else "door"
    return SimpleNamespace(x0=f.bx0, x1=f.x_lv, kind=kind, name=name,
                           connect=connect)


# 客廳段要夠寬才放得下兩扇窗(各 ~1.5m + 窗間牆墩 + 兩端讓柱);窄於此放一扇。
TWO_WINDOW_MIN_SPAN = 5000


def _living_south_windows(f: SimpleNamespace, rng: random.Random,
                          ov: Optional[SimpleNamespace], tag: str,
                          live_hi: Optional[float] = None) -> list:
    """南外牆的窗:有溢位就先給溢位一扇,再給客廳段(在 [客廳西界, live_hi])
    的窗——段夠寬放雙窗(牆墩 ≥MIN_PIER_WIDTH),窄就放一扇。回傳 Opening
    清單(相對 bx0,已躲柱)。live_hi=None → 客廳段東界=bx1(2F 無玄關);
    1F 傳 xf(玄關以西才是客廳南牆)。"""
    ops: list = []
    if ov is not None:
        ow, oww = _slot(_jitter(rng, (ov.x0 + ov.x1) / 2, ov.x0 + 150, ov.x1 - 150),
                        [1800, 1500, 1200, 900], ov.x0 + 150, ov.x1 - 150,
                        f.blocked, f"{ov.name}南窗")
        ops.append(Opening(ow - f.bx0, oww, "window"))
    lo = f.bx0 if ov is None else ov.x1
    hi = f.bx1 if live_hi is None else live_hi
    span = hi - lo
    if span >= TWO_WINDOW_MIN_SPAN:
        try:                                     # 夠寬先試雙窗
            (wl1, wl1w), (wl2, wl2w) = _paired_windows(
                rng, lo, hi, lo + span * 0.25, lo + span * 0.72,
                f.blocked, f"{tag}南窗1", f"{tag}南窗2")
            ops.append(Opening(wl1 - f.bx0, wl1w, "window"))
            ops.append(Opening(wl2 - f.bx0, wl2w, "window"))
            return ops
        except ValueError:
            pass                                 # 兩扇躲不開柱 → 退回單窗
    w, ww = _slot(_jitter(rng, (lo + hi) / 2, lo + 150, hi - 150),
                  [1800, 1500, 1200, 900], lo + 150, hi - 150,
                  f.blocked, f"{tag}南窗")
    ops.append(Opening(w - f.bx0, ww, "window"))
    return ops


def _add_overflow(walls: list, doors: list, rooms: list,
                  f: SimpleNamespace, ov: SimpleNamespace) -> None:
    """溢位牆(x_lv,南帶全高)+ 連通口 + 溢位房間加進清單。連通口在牆中段
    (離兩端柱夠遠);家庭廳=開放通道(無門扇),書房/儲藏=門扇。"""
    cy = f.by0 + f.ds / 2
    if ov.connect == "open":
        walls.append(Wall((ov.x1, f.by0), (ov.x1, f.yd), INT,
                          openings=[Opening(cy - f.by0, PASSAGE_WIDTH, "door")]))
    else:
        walls.append(Wall((ov.x1, f.by0), (ov.x1, f.yd), INT,
                          openings=[Opening(cy - f.by0, DOOR_WIDTH, "door")]))
        doors.append(DoorPlacement(len(walls) - 1, 0, Door(hinge="left", swing="in")))
    rooms.append(Room(ov.name,
                      [(ov.x0, f.by0), (ov.x1, f.by0), (ov.x1, f.yd), (ov.x0, f.yd)],
                      kind=ov.kind, code=ROOM_CODES[ov.kind]))


def _furnish_overflow(fixtures: list, f: SimpleNamespace, ov: SimpleNamespace,
                      cy: float) -> None:
    """溢位房間家具:一律貼南外牆擺(那道牆沒有門,只有溢位窗)、且偏西讓開
    東側連通口——避開北側帶分界牆的門迴轉與東側連通口(踩過 sofa 擋門的坑)。
    書房=書桌、家庭廳/多功能=沙發、儲藏=不放。"""
    cx = ov.x0 + (ov.x1 - ov.x0) * 0.4
    if ov.kind == "study":
        fixtures.append(FixturePlacement("desk", (cx, f.by0 + 75), 0))
    elif ov.kind == "family":
        fixtures.append(FixturePlacement("sofa3", (cx, f.by0 + 75), 0))


def _house_public_patio(brief: HouseBrief, f: SimpleNamespace) -> FloorPlanSpec:
    """透天 1F 公共層(深基地三帶+天井):南帶 客廳+玄關;天井帶 天井(西)+
    餐廳(L 形繞天井);北帶 儲藏|廚房|衛浴|樓梯間。

    天井 = 採光井:各層同一位置直落(_house_frame 定死),把光引進建築中段,
    深基地的房間才不會又深又暗。餐廳繞天井呈 L 形(北側走道段+東段),
    天井北牆開門+窗(進出天井、借光);廚房仍靠管道牆(給排水直落)。
    """
    xf = f.bx1 - FOYER_W
    foy_n = f.by0 + FOYER_D
    rng = _win_rng(brief, "1F")
    yd, yn, ypn, xp, xk = f.yd, f.yn, f.ypn, f.xp, f.xk

    # 南帶開口(同兩帶式):大門(玄關段)+ 客廳南窗×2(半段抖動,牆墩保護)。
    entry, ew = _slot((xf + f.bx1) / 2, [ENTRY_DOOR_WIDTH], xf, f.bx1, f.blocked, "大門")
    (wl1, wl1w), (wl2, wl2w) = _paired_windows(
        rng, f.bx0, xf, f.bx0 + f.W * 0.20, f.bx0 + f.W * 0.55,
        f.blocked, "客廳南窗1", "客廳南窗2")
    # 北帶開口:廚房/衛浴北窗(書房北窗在分間後決定,見下)。
    wk, wkw = _slot(_jitter(rng, (xk + f.xb) / 2, xk, f.xb),
                    [1200, 900], xk, f.xb, f.blocked, "廚房北窗")
    wb, wbw = _slot((f.xb + f.xs) / 2, [800, 600], f.xb, f.xs, f.blocked, "衛浴北窗")
    # 客廳 → 餐廳的開放通道(帶分界牆 1 東段,無門扇)。
    dpz, dpw = _slot((xp + f.xb) / 2, [PASSAGE_WIDTH, 1200],
                     xp + 150, f.bx1 - 150, f.blocked, "客餐通道")

    doors: list[DoorPlacement] = [
        DoorPlacement(0, 0, Door(hinge="left", swing="out"))]      # 大門

    # 帶分界牆 2(yn):西端房間(依軸線分間,每間一門)|廚房|衛浴|樓梯間。
    # 大基地時北帶西端很寬——第一間(靠西外牆)升級成「書房」開北窗
    # (真正的設計師不會在大房子裡塞一間 40m² 的無窗儲藏室);其餘仍儲藏。
    # 使用者已明確指定書房(E3)時樓上臥室層已配一間,這裡不再自動升級、回歸儲藏。
    div_x = ([f.bx0]
             + [g for g in f.grid_x[1:-1] if f.bx0 + 500 < g < xk - 500]
             + [xk])
    # 孝親房(E3):西端格改一樓臥室(共用 1F 衛浴);與自動書房互斥(擇一)。
    elder = brief.want_elder_room
    if elder and div_x[1] - div_x[0] < MIN_BEDROOM_WIDTH:
        raise ValueError(
            f"孝親房(1F 西端 {(div_x[1]-div_x[0])/1000:.1f}m 寬)不足 "
            f"{MIN_BEDROOM_WIDTH/1000:.1f}m,請加大基地寬")
    has_study = ((div_x[1] - div_x[0] >= 2800)
                 and not brief.want_study and not elder)
    west_win = has_study or elder             # 西端格(書房/孝親房)要開北窗
    band_open: list[Opening] = []
    for i in range(len(div_x) - 1):
        if i == 0 and elder:
            what = "孝親房"
        elif i == 0 and has_study:
            what = "書房"
        else:
            what = f"儲藏室{i+1}"
        pos, dw = _slot((div_x[i] + div_x[i + 1]) / 2, [DOOR_WIDTH, 750],
                        div_x[i], div_x[i + 1], f.blocked, f"{what}門")
        doors.append(DoorPlacement(5, len(band_open),
                                   Door(hinge="left", swing="out")))
        band_open.append(Opening(pos - f.bx0, dw, "door"))
    if f.v.kitchen_open:
        # 開放式廚房:對餐廳開整段寬通道(無門扇)。
        dk, dkw = _slot((xk + f.xb) / 2, [2400, 1800, 1200], xk, f.xb,
                        f.blocked, "餐廚通道")
        band_open.append(Opening(dk - f.bx0, dkw, "door"))
    else:
        dk, dkw = _slot((xk + f.xb) / 2, [DOOR_WIDTH], xk, f.xb, f.blocked, "廚房門")
        doors.append(DoorPlacement(5, len(band_open),
                                   Door(hinge="left", swing="out")))
        band_open.append(Opening(dk - f.bx0, dkw, "door"))
    # 衛浴門貼樓梯間側(不放正中):正中的門正對餐桌/沙發視線——衛生空間
    # 的門要躲開公共空間的正面(建築師檢視 2026-07-20)。675 = 門半寬+牆邊留距。
    db, dbw = _slot(f.xs - 675, [750], f.xb, f.xs, f.blocked, "衛浴門")
    doors.append(DoorPlacement(5, len(band_open), Door(hinge="left", swing="out")))
    band_open.append(Opening(db - f.bx0, dbw, "door"))
    dst, dstw = _slot((f.xs + f.bx1) / 2, [DOOR_WIDTH], f.xs, f.bx1,
                      f.blocked, "樓梯間門")
    doors.append(DoorPlacement(5, len(band_open), Door(hinge="left", swing="out")))
    band_open.append(Opening(dst - f.bx0, dstw, "door"))

    # 西端房北窗(書房/孝親房才開)。
    north_open = [Opening(wk - f.bx0, wkw, "window"),
                  Opening(wb - f.bx0, wbw, "window")]
    if west_win:
        ws_, wsw = _slot((div_x[0] + div_x[1]) / 2, [1500, 1200, 900],
                         div_x[0], div_x[1], f.blocked, "西端房北窗")
        north_open.append(Opening(ws_ - f.bx0, wsw, "window"))

    # 天井北牆(走道側):門(進出天井)+ 窗(借光)。這道牆不在軸線上,
    # 整段無柱,開口不必躲柱。
    pl = xp - f.bx0                                    # 牆長
    patio_wall = Wall((f.bx0, ypn), (xp, ypn), INT,
                      openings=[Opening(pl * 0.30, 800, "door"),
                                Opening(pl * 0.72, min(900, pl * 0.30), "window")])
    doors.append(DoorPlacement(6, 0, Door(hinge="left", swing="in")))

    walls = [
        Wall((f.bx0, f.by0), (f.bx1, f.by0), EXT,           # 0 南外牆
             openings=[Opening(entry - f.bx0, ew, "door"),
                       Opening(wl1 - f.bx0, wl1w, "window"),
                       Opening(wl2 - f.bx0, wl2w, "window")]),
        Wall((f.bx0, f.by1), (f.bx1, f.by1), EXT,           # 1 北外牆
             openings=north_open),
        Wall((f.bx0, f.by0), (f.bx0, f.by1), EXT),          # 2 西外牆
        Wall((f.bx1, f.by0), (f.bx1, f.by1), EXT),          # 3 東外牆
        Wall((f.bx0, yd), (f.bx1, yd), INT,                 # 4 帶分界牆1(通道)
             openings=[Opening(dpz - f.bx0, dpw, "door")]),
        Wall((f.bx0, yn), (f.bx1, yn), INT, openings=band_open),  # 5 帶分界牆2
        patio_wall,                                         # 6 天井北牆(門+窗)
        Wall((xp, yd), (xp, ypn), INT),                     # 7 天井東牆
        Wall((xk, yn), (xk, f.by1), INT),                   # 8 儲|廚
        Wall((f.xb, yn), (f.xb, f.by1), INT),               # 9 廚|衛(管道牆)
        Wall((f.xs, yn), (f.xs, f.by1), INT),               # 10 衛|梯
        Wall((xf, f.by0), (xf, foy_n), INT),                # 11 玄關隔屏
    ]
    for xi in div_x[1:-1]:                                  # 12.. 儲藏隔牆(坐軸線)
        walls.append(Wall((xi, yn), (xi, f.by1), INT))

    # 餐廳東段大到超過合理上限(f.xdz 見 _house_frame)就切一刀:西段留餐廳,
    # 東段(落在建築東外牆上)改「儲藏室」——不讓單一房間吸光基地變大的餘量。
    dz_wall = None
    if f.xdz is not None:
        dz_wall = len(walls)
        walls.append(Wall((f.xdz, yd), (f.xdz, yn), INT,
                          openings=[Opening((yn - yd) * 0.5, 750, "door")]))
        doors.append(DoorPlacement(dz_wall, 0, Door(hinge="left", swing="in")))

    windows = [WindowPlacement(0, 1), WindowPlacement(0, 2),
               WindowPlacement(1, 0), WindowPlacement(1, 1),
               WindowPlacement(6, 1)]
    if west_win:
        windows.append(WindowPlacement(1, 2))

    dine_e = f.xdz if f.xdz is not None else f.bx1          # 餐廳東界(切開就縮短)
    rooms = [
        Room("客廳", [(f.bx0, f.by0), (xf, f.by0), (xf, foy_n), (f.bx1, foy_n),
                      (f.bx1, yd), (f.bx0, yd)],
             kind="living", code=ROOM_CODES["living"]),
        Room("玄關", [(xf, f.by0), (f.bx1, f.by0), (f.bx1, foy_n), (xf, foy_n)],
             kind="foyer", code=ROOM_CODES["foyer"]),
        Room(_patio_name(f), [(f.bx0, yd), (xp, yd), (xp, ypn), (f.bx0, ypn)],
             kind="patio", code=ROOM_CODES["patio"]),
        # 餐廳 L 形:東段(xp~dine_e)+ 天井北側走道段。
        Room("餐廳", [(xp, yd), (dine_e, yd), (dine_e, yn), (f.bx0, yn),
                      (f.bx0, ypn), (xp, ypn)],
             kind="dining", code=ROOM_CODES["dining"]),
        Room("廚房", [(xk, yn), (f.xb, yn), (f.xb, f.by1), (xk, f.by1)],
             kind="kitchen", code=ROOM_CODES["kitchen"]),
        Room("衛浴", [(f.xb, yn), (f.xs, yn), (f.xs, f.by1), (f.xb, f.by1)],
             kind="bathroom", code=ROOM_CODES["bathroom"]),
        Room("樓梯間", [(f.xs, yn), (f.bx1, yn), (f.bx1, f.by1), (f.xs, f.by1)],
             kind="stair", code=ROOM_CODES["stair"]),
    ]
    if f.xdz is not None:
        rooms.append(Room("儲藏室",
                          [(f.xdz, yd), (f.bx1, yd), (f.bx1, yn), (f.xdz, yn)],
                          kind="storage", code=ROOM_CODES["storage"]))
    for i in range(len(div_x) - 1):
        pts = [(div_x[i], yn), (div_x[i + 1], yn),
               (div_x[i + 1], f.by1), (div_x[i], f.by1)]
        if i == 0 and elder:
            rooms.append(Room("孝親房", pts, kind="bedroom",
                              code=ROOM_CODES["bedroom"]))
        elif i == 0 and has_study:
            rooms.append(Room("書房", pts, kind="study",
                              code=ROOM_CODES["study"]))
        else:
            rooms.append(Room("儲藏室", pts, kind="storage",
                              code=ROOM_CODES["storage"]))

    # 家具:客廳成套(沙發+茶几+單椅+電視櫃,守門);餐桌在天井帶東段
    # (偏南,讓開北側各室門的迴轉);冰箱貼廚房管道牆讓開 yn 門迴轉。
    cy_s = (f.by0 + yd) / 2
    fixtures: list = [
        FixturePlacement("shoe_cabinet", (f.bx1 - 75, (f.by0 + foy_n) / 2), 90),
        Counter(start=(f.xb - 75, f.by1 - 75), end=(xk + 60, f.by1 - 75),
                sink=True, stove=True),
        FixturePlacement("toilet", (f.xs - 60, f.by1 - 500), 90),
        FixturePlacement("basin", (f.xs - 60, f.by1 - 1300), 90),
    ]
    if f.by1 - yn >= 3600:                     # 全深衛浴 → 浴缸貼管道牆
        fixtures.append(FixturePlacement(
            "bathtub", (f.xb + 60, (yn + f.by1) / 2), 270))
    _sofa_suite(fixtures, f.bx0, cy_s, f.by0, yd,
                tv_x=f.bx1, tv_lo=foy_n, tv_hi=yd)
    _kitchen_fridge(fixtures, f.xb, f.by1, yn + 900)
    if elder:                                  # 孝親房(西端格):床+衣櫃+床頭櫃
        _bedroom_set(fixtures, div_x[0], div_x[1], f.by1, double=True)
    elif has_study:                            # 自動書房:書桌+書櫃(守門)
        _study_set(fixtures, div_x[0], div_x[1], yn, f.by1)
    if dine_e - xp >= 2400:
        # 餐桌 y:天井帶淺時貼南(讓開 yn 各室門的迴轉 900),深(中庭)時
        # 往帶中挪——上限守「桌組頂 ≤ yn-900-100」。dine_e(非 f.xb):切開
        # 東段後餐桌只在餐廳自己的範圍內置中,不會跑進切出去的儲藏室。
        ty = min(yd + max(850, f.dp * 0.35), yn - 900 - 780 - 100)
        fixtures.append(FixturePlacement("table4", ((xp + dine_e) / 2, ty), 0))

    spec = FloorPlanSpec(walls=walls, rooms=rooms, doors=doors, windows=windows,
                         fixtures=fixtures,
                         stairs=[_house_stair(f)], floor_label="1F", **f.spec_kw)
    return _finish_house(spec, f, "透天 1F 公共層(天井)")


def _house_upper_patio(brief: HouseBrief, f: SimpleNamespace) -> FloorPlanSpec:
    """透天臥室層(深基地三帶+天井):南帶 起居室;天井帶 天井(挑空直落)+
    家庭廳(L 形走道,臥室門開向它);北帶 臥室×n|衛浴|樓梯間。

    天井在樓上是挑空(光井),家庭廳靠天井窗借光;臥室門一律開在北帶南牆
    (走道正面),不會開向露天的天井。
    """
    bed_x = f.bed_x
    kinds = f.slot_kinds                                    # 臥室×n(+選填書房,E3)
    rng = _win_rng(brief, "2F")
    yd, yn, ypn, xp = f.yd, f.yn, f.ypn, f.xp

    band_open, doors, win_open, windows = _band_slot_openings(f, bed_x, kinds, rng)
    # 衛浴門貼樓梯間側(不放正中):正中的門正對餐桌/沙發視線——衛生空間
    # 的門要躲開公共空間的正面(建築師檢視 2026-07-20)。675 = 門半寬+牆邊留距。
    db, dbw = _slot(f.xs - 675, [750], f.xb, f.xs, f.blocked, "衛浴門")
    doors.append(DoorPlacement(4, len(band_open), Door(hinge="left", swing="out")))
    band_open.append(Opening(db - f.bx0, dbw, "door"))
    dst, dstw = _slot((f.xs + f.bx1) / 2, [DOOR_WIDTH], f.xs, f.bx1,
                      f.blocked, "樓梯間門")
    doors.append(DoorPlacement(4, len(band_open), Door(hinge="left", swing="out")))
    band_open.append(Opening(dst - f.bx0, dstw, "door"))
    wb_, wbw = _slot((f.xb + f.xs) / 2, [800, 600], f.xb, f.xs, f.blocked, "衛浴北窗")
    windows.append(WindowPlacement(1, len(win_open)))
    win_open.append(Opening(wb_ - f.bx0, wbw, "window"))
    (wl1, wl1w), (wl2, wl2w) = _paired_windows(
        rng, f.bx0, f.bx1, f.bx0 + f.W * 0.30, f.bx0 + f.W * 0.70,
        f.blocked, "起居南窗1", "起居南窗2")
    dpz, dpw = _slot((xp + f.xb) / 2, [PASSAGE_WIDTH, 1200],
                     xp + 150, f.bx1 - 150, f.blocked, "起居通道")

    pl = xp - f.bx0
    walls = [
        Wall((f.bx0, f.by0), (f.bx1, f.by0), EXT,           # 0 南外牆(起居窗)
             openings=[Opening(wl1 - f.bx0, wl1w, "window"),
                       Opening(wl2 - f.bx0, wl2w, "window")]),
        Wall((f.bx0, f.by1), (f.bx1, f.by1), EXT, openings=win_open),  # 1 北外牆
        Wall((f.bx0, f.by0), (f.bx0, f.by1), EXT),          # 2 西外牆
        Wall((f.bx1, f.by0), (f.bx1, f.by1), EXT),          # 3 東外牆
        Wall((f.bx0, yn), (f.bx1, yn), INT, openings=band_open),  # 4 帶分界牆2
        Wall((f.bx0, yd), (f.bx1, yd), INT,                 # 5 帶分界牆1(通道)
             openings=[Opening(dpz - f.bx0, dpw, "door")]),
        Wall((f.bx0, ypn), (xp, ypn), INT,                  # 6 天井北牆(借光窗)
             openings=[Opening(pl * 0.5, min(1200, pl * 0.5), "window")]),
        Wall((xp, yd), (xp, ypn), INT),                     # 7 天井東牆
    ]
    windows.insert(0, WindowPlacement(0, 0))
    windows.insert(1, WindowPlacement(0, 1))
    windows.append(WindowPlacement(6, 0))
    for xi in bed_x[1:]:                                    # 臥室隔牆 + 管道牆(xb)
        walls.append(Wall((xi, yn), (xi, f.by1), INT))
    walls.append(Wall((f.xs, yn), (f.xs, f.by1), INT))      # 衛|梯

    # ⚠️ 家庭廳(2F)不比照 1F 餐廳切割:它是「所有臥室門共用的走道」
    # (validate_spec 的 C1.5b 規則——有走道時每間臥室門都要通到走道),
    # 必須貫通到樓梯間那一端,任何臥室門才都摸得到它。切開會讓靠東的臥室門
    # (通常是最後一間、貼樓梯間側)構到的變成「儲藏室」而不是「家庭廳」,
    # 檢核判它「門未通走道」。1F 的餐廳沒有這個功能性角色(1F 沒有走道規則
    # 要顧),才切得掉——面積大不代表都能切,要看那間房間有沒有被結構性
    # 規則綁住(這裡是柱網優先原則的另一種體現:功能性硬約束優先於面積目標)。
    rooms = [
        Room("起居室", [(f.bx0, f.by0), (f.bx1, f.by0), (f.bx1, yd), (f.bx0, yd)],
             kind="living", code=ROOM_CODES["living"]),
        Room(_patio_name(f), [(f.bx0, yd), (xp, yd), (xp, ypn), (f.bx0, ypn)],
             kind="patio", code=ROOM_CODES["patio"]),
        Room("家庭廳", [(xp, yd), (f.bx1, yd), (f.bx1, yn), (f.bx0, yn),
                        (f.bx0, ypn), (xp, ypn)],
             kind="corridor", code=ROOM_CODES["corridor"]),
        Room("衛浴", [(f.xb, yn), (f.xs, yn), (f.xs, f.by1), (f.xb, f.by1)],
             kind="bathroom", code=ROOM_CODES["bathroom"]),
        Room("樓梯間", [(f.xs, yn), (f.bx1, yn), (f.bx1, f.by1), (f.xs, f.by1)],
             kind="stair", code=ROOM_CODES["stair"]),
    ]
    rooms += _band_rooms(bed_x, kinds, yn, f.by1)          # 臥室(+書房)

    fixtures = _band_fixtures(f, bed_x, kinds)
    cy_s = (f.by0 + yd) / 2
    # 起居室沙發+方桌(不成套,使用者定調:成套只留 1F 客廳)。
    fixtures.append(FixturePlacement("sofa3", (f.bx0 + 75, cy_s), 270))
    fixtures.append(FixturePlacement("table4", ((f.bx0 + f.bx1) / 2, cy_s), 0))
    fixtures.append(FixturePlacement("toilet", (f.xs - 60, f.by1 - 500), 90))
    fixtures.append(FixturePlacement("basin", (f.xs - 60, f.by1 - 1300), 90))
    if f.by1 - yn >= 3600:                     # 全深衛浴 → 浴缸貼管道牆
        fixtures.append(FixturePlacement(
            "bathtub", (f.xb + 60, (yn + f.by1) / 2), 270))

    spec = FloorPlanSpec(walls=walls, rooms=rooms, doors=doors, windows=windows,
                         fixtures=fixtures,
                         stairs=[_house_stair(f)], floor_label="2F", **f.spec_kw)
    return _finish_house(spec, f, "透天臥室層(天井)")


def _patio_name(f: SimpleNamespace) -> str:
    """天井的顯示名:長到中庭尺度(淨深 ≥3.2m)就叫「中庭」——大基地的
    天井帶會自動加深(PATIO_BAND_RANGE 上限 6.7m),名稱跟著升級。"""
    return "中庭" if (f.ypn - f.yd) >= 3200 else "天井"


def max_house_bedrooms(brief: HouseBrief) -> int:
    """這塊基地(多樓層透天骨架)最多放得下幾房(0 = 連 1 房都塞不下)。

    給「設計建議」用:基地很大、使用者只要 3 房時,告訴他其實放得下 4 房。
    只跑骨架計算(_house_frame),不出圖,很快。
    """
    best = 0
    for n in range(1, 5):                    # HouseBrief 支援 1~4 房
        try:
            _house_frame(replace(brief, bedrooms=n))
            best = n
        except ValueError:
            pass
    return best


def _west_zone_cut(f: SimpleNamespace, zone_hi: float,
                   x_max: float) -> Optional[float]:
    """1F 北帶西段 [bx0, zone_hi] 該不該切一間附屬房出來?回傳切點 x(不切=None)。

    餐廳(或開放式的餐廚)大過 max_area 就切:餘量給別的空間,不要讓單一房間
    無限長大(使用者 2026-07-20 定調)。

    切點位置由「餐廳想留多寬」回推,附近有軸線就吸附過去(牆坐在軸線上,柱
    正好藏進牆交點,更好);沒有也照切——這道牆是輕隔間,不落樑、不新增柱,
    所以柱網完全不受影響(同玄關隔屏 xf 的作法)。「柱藏在牆裡」要求的是每根
    **柱**都有牆包住,不是每道**牆**都要有柱。
    """
    req = requirement("dining")
    zone_w = zone_hi - f.bx0
    if req.max_area is None:
        return None
    if zone_w * f.dn / 1_000_000 <= req.max_area * (1 + AREA_TOLERANCE):
        return None                                  # 還在合理範圍,不必切
    keep = req.preferred_area * 1_000_000 / f.dn     # 餐廳想留的寬(靠廚房那側)
    lo = f.bx0 + requirement("storage").min_width    # 西邊那間也要放得下
    # 切點不得越過 x_max(=廚房西牆):那之東是廚房的地盤,牆與窗都已排定。
    hi = min(zone_hi - req.min_width, x_max - req.min_width)
    if hi < lo:
        return None
    x = _clamp(zone_hi - keep, lo, hi)
    g = min(f.grid_x, key=lambda t: abs(t - x))      # 附近有軸線就坐上去
    return g if abs(g - x) <= WALL_SNAP_TOL and lo <= g <= hi else x


def generate_house_public(brief: HouseBrief) -> FloorPlanSpec:
    """透天 1F 公共層(D2):南帶 客廳+玄關(東南角),北帶 廚房|餐廳|衛浴|樓梯間。

    臥室全部上樓(2F+,generate_house_upper);1F 留白天的生活空間——
    真實透天的標準分層。客餐之間開 1.5m 開放通道(無門扇)。
    深基地(_house_frame 判定)自動改走三帶+天井版(_house_public_patio)。
    """
    f = _house_frame(brief)
    if f.has_patio:
        return _house_public_patio(brief, f)
    xk = f.xk                                                # 廚房|餐廳分界(坐軸線)
    xf = f.bx1 - FOYER_W                                     # 玄關西緣(東南角)
    foy_n = f.by0 + FOYER_D
    rng = _win_rng(brief, "1F")                              # 開窗位置抖動(E2)
    # 孝親房(E3):把 1F 北帶西端「餐廳」格改成一樓臥室(共用 1F 衛浴),餐廳
    # 併入客廳成「客餐廳」。需獨立廚房版(西端有獨立房格可改),且該格夠寬。
    elder = brief.want_elder_room
    if elder and xk - f.bx0 < MIN_BEDROOM_WIDTH:
        raise ValueError(
            f"孝親房(1F 西端 {(xk-f.bx0)/1000:.1f}m 寬)不足 "
            f"{MIN_BEDROOM_WIDTH/1000:.1f}m,請加大基地寬")
    open_kitchen = f.v.kitchen_open and not elder            # 開放式廚房(併入餐廳)
    # 餐廳(西,[bx0,xk])內側若還有更西的軸線,立短牆(柱包)蓋住 tuck 後
    # 凸進服務帶的柱——與開放廚房的中島腳同一手法。廚房(東,[xk,xb])靠管道
    # 牆 xb,與衛浴共用給排水立管。
    inner = sorted(g for g in f.west_lines if g < xk - 1)

    # 大門(絕對 x,躲柱)。客廳南窗在 Living Overflow 決定客廳西界後才放。
    entry, ew = _slot((xf + f.bx1) / 2, [ENTRY_DOOR_WIDTH], xf, f.bx1, f.blocked, "大門")
    # 西端附屬房(F3):北帶西段大到超過餐廳的合理上限,就切一間出來(書房/
    # 儲藏室)。不切的話,基地一大這一格就長成 47~90m² 的「大餐廳」——正是
    # 使用者要修掉的「剩餘空間全給一個房間」,只是換了個房名。切點吸附軸線,
    # 柱仍藏在牆裡(柱網優先)。孝親房佔用西段時不切。
    # zone_hi:餐廳(或開放式的「餐廚」)這一格的東界——開放廚房時廚房併進來,
    # 所以面積要連廚房一起算。但**開窗**永遠以 xk 為界:[xk, xb] 那段是廚房北窗
    # 的地盤,西段的窗跑進去就會兩扇疊在一起(牆墩變負值)。
    zone_hi = f.xb if open_kitchen else xk
    xw = None if elder else _west_zone_cut(f, zone_hi, xk)
    zone_edges = [f.bx0] + ([xw] if xw is not None else []) + [xk]

    # Living Overflow(客廳過細時切西端出來)。1F 北帶西段若已切出書房(見下方
    # as_study),Program Selector 就不會在南帶再切一間書房(避免重複)。孝親房
    # 版的南帶是客餐廳(併餐),不切。
    north_study = (xw is not None and (xw - f.bx0) >= MIN_STUDY_WIDTH
                   and not brief.want_study)
    ov = None if elder else _plan_overflow(
        f, brief, "public",
        has_study=(north_study or brief.want_study), has_family=False)
    live_w0 = f.bx0 if ov is None else ov.x1     # 客廳西界(有溢位就往東縮)

    # 北窗:西段每一間各一扇(切開了就兩扇)+ 廚房 + 衛浴。開口索引動態記錄,
    # 免得加了房間之後 WindowPlacement 的固定索引對不上。
    north_ops: list[Opening] = []
    for i in range(len(zone_edges) - 1):
        a, b = zone_edges[i], zone_edges[i + 1]
        wpos, ww = _slot(_jitter(rng, (a + b) / 2, a, b), [1500, 1200, 900],
                         a, b, f.blocked, "北帶西段窗")
        north_ops.append(Opening(wpos - f.bx0, ww, "window"))
    wkit, wkw = _slot(_jitter(rng, (xk + f.xb) / 2, xk, f.xb),
                      [1200, 900], xk, f.xb, f.blocked, "廚房北窗")
    north_ops.append(Opening(wkit - f.bx0, wkw, "window"))
    wb, wbw = _slot((f.xb + f.xs) / 2, [800, 600], f.xb, f.xs, f.blocked, "衛浴北窗")
    north_ops.append(Opening(wb - f.bx0, wbw, "window"))
    # 衛浴門貼樓梯間側(不放正中):正中的門正對餐桌/沙發視線——衛生空間
    # 的門要躲開公共空間的正面(建築師檢視 2026-07-20)。675 = 門半寬+牆邊留距。
    db, dbw = _slot(f.xs - 675, [750], f.xb, f.xs, f.blocked, "衛浴門")
    dst, dstw = _slot((f.xs + f.bx1) / 2, [DOOR_WIDTH], f.xs, f.bx1, f.blocked, "樓梯間門")

    # 南外牆:大門(SE)+ 溢位南窗(有溢位時)+ 客廳窗(在客廳段 [live_w0, xf]
    # 內,不跨過玄關;段窄放一扇、寬放兩扇)。
    south_win_ops = _living_south_windows(f, rng, ov, "客廳", live_hi=xf)
    south = Wall((f.bx0, f.by0), (f.bx1, f.by0), EXT,       # 0 南外牆
                 openings=[Opening(entry - f.bx0, ew, "door")] + south_win_ops)
    north = Wall((f.bx0, f.by1), (f.bx1, f.by1), EXT,       # 1 北外牆
                 openings=north_ops)
    # 柱包短牆(西段切開處已立整道牆,那裡就不必再包)。
    stubs = [Wall((g, f.yd), (g, f.yd + 900), INT)
             for g in inner if xw is None or abs(g - xw) > 1]

    # 西端附屬房的門(切開時才有),開在帶分界牆上、位置在該房正面。
    west_room_op: Optional[Opening] = None
    if xw is not None:
        dw_, dww = _slot((f.bx0 + xw) / 2, [DOOR_WIDTH, 750], f.bx0, xw,
                         f.blocked, "西端附屬房門")
        west_room_op = Opening(dw_ - f.bx0, dww, "door")

    if open_kitchen:
        # 開放式廚房:拆掉廚|餐隔牆,廚+餐合成一間「餐廚」,對客廳開寬通道。
        z_lo = xw if xw is not None else f.bx0
        dp, dpw = _slot((z_lo + f.xb) / 2, [2400, 1800], z_lo, f.xb,
                        f.blocked, "餐廚通道")
        div_ops = ([west_room_op] if west_room_op else []) + [
            Opening(dp - f.bx0, dpw, "door"),
            Opening(db - f.bx0, dbw, "door"),
            Opening(dst - f.bx0, dstw, "door")]
        divider = Wall((f.bx0, f.yd), (f.bx1, f.yd), INT,   # 4 帶分界牆
                       openings=div_ops)
        # 中島腳:開放廚房拆了廚|餐牆,原藏在牆裡的柱會露進餐廚;每條內部軸線
        # (xk 及更西的 inner)都留一段短半牆把柱包住,兼作餐廚視覺分界。
        walls = [south, north,
                 Wall((f.bx0, f.by0), (f.bx0, f.by1), EXT),  # 2 西外牆
                 Wall((f.bx1, f.by0), (f.bx1, f.by1), EXT),  # 3 東外牆
                 divider,
                 Wall((f.xb, f.yd), (f.xb, f.by1), INT),     # 5 餐廚|衛(管道牆)
                 Wall((f.xs, f.yd), (f.xs, f.by1), INT),     # 6 衛|梯
                 Wall((xf, f.by0), (xf, foy_n), INT),        # 7 玄關隔屏
                 *([Wall((xk, f.yd), (xk, f.yd + 900), INT)]  # 8 中島腳(包柱)
                   if xw is None or abs(xk - xw) > 1 else []),
                 *([Wall((xw, f.yd), (xw, f.by1), INT)]      # 西端附屬房東牆
                   if xw is not None else []),
                 *stubs]
        k = 1 if west_room_op else 0                        # 分界牆開口的起始索引
        doors = [
            DoorPlacement(0, 0, Door(hinge="left", swing="out")),      # 大門
            DoorPlacement(4, k + 1, Door(hinge="left", swing="out")),  # 衛浴
            DoorPlacement(4, k + 2, Door(hinge="left", swing="out")),  # 樓梯間
        ]
        service_rooms = [
            Room("餐廚", [(z_lo, f.yd), (f.xb, f.yd), (f.xb, f.by1), (z_lo, f.by1)],
                 kind="dining", code=ROOM_CODES["dining"]),
        ]
    else:
        # 獨立廚房:廚(東,靠管道牆)|餐(西)有隔牆坐軸線;廚房自帶門,
        # 餐廳對客廳開放通道(無門扇)。
        dk, dkw = _slot((xk + f.xb) / 2, [DOOR_WIDTH], xk, f.xb, f.blocked, "廚房門")
        # 西端格:孝親房時開一扇私密門(門扇);否則客餐開放通道(無門扇)。
        z_lo = xw if xw is not None else f.bx0
        dp, dpw = _slot((z_lo + xk) / 2,
                        [DOOR_WIDTH] if elder else [PASSAGE_WIDTH, 1200],
                        z_lo, xk, f.blocked, "孝親房門" if elder else "客餐通道")
        div_ops = ([west_room_op] if west_room_op else []) + [
            Opening(dp - f.bx0, dpw, "door"),
            Opening(dk - f.bx0, dkw, "door"),
            Opening(db - f.bx0, dbw, "door"),
            Opening(dst - f.bx0, dstw, "door")]
        divider = Wall((f.bx0, f.yd), (f.bx1, f.yd), INT,   # 4 帶分界牆
                       openings=div_ops)
        walls = [south, north,
                 Wall((f.bx0, f.by0), (f.bx0, f.by1), EXT),  # 2 西外牆
                 Wall((f.bx1, f.by0), (f.bx1, f.by1), EXT),  # 3 東外牆
                 divider,
                 Wall((xk, f.yd), (xk, f.by1), INT),         # 5 餐|廚(坐軸線)
                 Wall((f.xb, f.yd), (f.xb, f.by1), INT),     # 6 廚|衛(管道牆)
                 Wall((f.xs, f.yd), (f.xs, f.by1), INT),     # 7 衛|梯
                 Wall((xf, f.by0), (xf, foy_n), INT),        # 8 玄關隔屏
                 *([Wall((xw, f.yd), (xw, f.by1), INT)]      # 西端附屬房東牆
                   if xw is not None else []),
                 *stubs]
        k = 1 if west_room_op else 0                        # 分界牆開口的起始索引
        doors = [
            DoorPlacement(0, 0, Door(hinge="left", swing="out")),      # 大門
            DoorPlacement(4, k + 1, Door(hinge="left", swing="out")),  # 廚房
            DoorPlacement(4, k + 2, Door(hinge="left", swing="out")),  # 衛浴
            DoorPlacement(4, k + 3, Door(hinge="left", swing="out")),  # 樓梯間
        ]
        if elder:
            # 西端 → 孝親房(一樓臥室,門開向客餐廳);餐廳併入客廳。
            doors.append(DoorPlacement(4, k, Door(hinge="left", swing="out")))
            service_rooms = [
                Room("孝親房", [(f.bx0, f.yd), (xk, f.yd), (xk, f.by1), (f.bx0, f.by1)],
                     kind="bedroom", code=ROOM_CODES["bedroom"]),
                Room("廚房", [(xk, f.yd), (f.xb, f.yd), (f.xb, f.by1), (xk, f.by1)],
                     kind="kitchen", code=ROOM_CODES["kitchen"]),
            ]
        else:
            service_rooms = [
                Room("餐廳", [(z_lo, f.yd), (xk, f.yd), (xk, f.by1), (z_lo, f.by1)],
                     kind="dining", code=ROOM_CODES["dining"]),
                Room("廚房", [(xk, f.yd), (f.xb, f.yd), (f.xb, f.by1), (xk, f.by1)],
                     kind="kitchen", code=ROOM_CODES["kitchen"]),
            ]

    windows = ([WindowPlacement(0, i) for i in range(1, len(south.openings))]
               + [WindowPlacement(1, i) for i in range(len(north_ops))])

    # 西端附屬房:夠寬就當書房(使用者已指定書房時樓上已有一間,這裡回歸儲藏)。
    west_rooms: list[Room] = []
    if xw is not None:
        as_study = (xw - f.bx0) >= MIN_STUDY_WIDTH and not brief.want_study
        kind = "study" if as_study else "storage"
        west_rooms.append(Room(
            "書房" if as_study else "儲藏室",
            [(f.bx0, f.yd), (xw, f.yd), (xw, f.by1), (f.bx0, f.by1)],
            kind=kind, code=ROOM_CODES[kind]))
        doors.append(DoorPlacement(4, 0, Door(hinge="left", swing="out")))

    rooms = [
        Room("客餐廳" if elder else "客廳",                  # 孝親房佔餐廳格 → 餐併客
              [(live_w0, f.by0), (xf, f.by0), (xf, foy_n), (f.bx1, foy_n),
               (f.bx1, f.yd), (live_w0, f.yd)],
             kind="living", code=ROOM_CODES["living"]),
        Room("玄關", [(xf, f.by0), (f.bx1, f.by0), (f.bx1, foy_n), (xf, foy_n)],
             kind="foyer", code=ROOM_CODES["foyer"]),
        *west_rooms,
        *service_rooms,
        Room("衛浴", [(f.xb, f.yd), (f.xs, f.yd), (f.xs, f.by1), (f.xb, f.by1)],
             kind="bathroom", code=ROOM_CODES["bathroom"]),
        Room("樓梯間", [(f.xs, f.yd), (f.bx1, f.yd), (f.bx1, f.by1), (f.xs, f.by1)],
             kind="stair", code=ROOM_CODES["stair"]),
    ]
    if ov is not None:                          # 溢位牆 + 房間 + 連通口(客廳↔溢位)
        _add_overflow(walls, doors, rooms, f, ov)

    # 家具設備(皆通過碰撞/門迴轉檢核)。
    cy_s = (f.by0 + f.yd) / 2                   # 南帶(客廳)中心線
    fixtures: list = [
        FixturePlacement("shoe_cabinet",                             # 鞋櫃貼玄關東牆
                         (f.bx1 - 75, (f.by0 + foy_n) / 2), 90),
        # 廚房(東,靠管道牆):一字型流理台沿北牆(水槽靠窗、爐具偏東)。
        Counter(start=(f.xb - 75, f.by1 - 75), end=(xk + 60, f.by1 - 75),
                sink=True, stove=True),
        # 衛浴:馬桶+洗手台靠東牆北半(讓開南側門的迴轉)。
        FixturePlacement("toilet", (f.xs - 60, f.by1 - 500), 90),
        FixturePlacement("basin", (f.xs - 60, f.by1 - 1300), 90),
    ]
    # 全深衛浴(≥3.6m)空間夠 → 配浴缸貼管道牆(給排水最短;建築師檢視:
    # 9m² 衛浴只擺馬桶+洗手台太空,不像有人設計過)。
    if f.by1 - f.yd >= 3600:
        fixtures.append(FixturePlacement(
            "bathtub", (f.xb + 60, (f.yd + f.by1) / 2), 270))
    # 客廳家具:無溢位→成套(沙發貼西牆+茶几+單椅+電視櫃貼東牆);有溢位→
    # 緊湊客廳,沙發貼南牆面北(不擋西側連通口),冰箱貼廚房管道牆。
    if ov is None:
        _sofa_suite(fixtures, live_w0, cy_s, f.by0, f.yd,
                    tv_x=f.bx1, tv_lo=foy_n, tv_hi=f.yd)
    else:
        fixtures.append(FixturePlacement(
            "sofa3", ((live_w0 + xf) / 2, f.by0 + 75), 0))
        _furnish_overflow(fixtures, f, ov, cy_s)
    _kitchen_fridge(fixtures, f.xb, f.by1, f.yd + 900)
    if open_kitchen:
        # 開放餐廚:加中島吧台,界定烹飪動線與開放地坪(放不下就略過)。
        _kitchen_island(fixtures, xk, f.xb, f.yd, f.by1)
    if elder:
        # 孝親房(西端格):床+衣櫃+床頭櫃(空間守門)。
        _bedroom_set(fixtures, f.bx0, xk, f.by1, double=True)
    else:
        fixtures.append(FixturePlacement(                            # 餐桌(西,餐廳)
            "table4", ((f.bx0 + xk) / 2, (f.yd + f.by1) / 2), 0))

    spec = FloorPlanSpec(walls=walls, rooms=rooms, doors=doors, windows=windows,
                         fixtures=fixtures,
                         stairs=[_house_stair(f)], floor_label="1F", **f.spec_kw)
    return _finish_house(spec, f, "透天 1F 公共層")


def generate_house_upper(brief: HouseBrief) -> FloorPlanSpec:
    """透天標準臥室層(2F+,D2):北帶 臥室×n|衛浴|樓梯間,南帶 起居室。

    起居室(家庭廳)= 這層的動線空間,臥室門開向它——透天樓上以起居空間
    兼走道是常態(走道原則:房間更多才另設走道,見 hallway_design_rule)。
    衛浴與 1F 衛浴同開間(管道上下對齊)。
    深基地(_house_frame 判定)自動改走三帶+天井版(_house_upper_patio)。
    """
    f = _house_frame(brief)
    if f.has_patio:
        return _house_upper_patio(brief, f)
    bed_x = f.bed_x        # 隔牆已在骨架吸附軸線(柱藏隔牆交點)
    kinds = f.slot_kinds                                    # 臥室×n(+選填書房,E3)
    rng = _win_rng(brief, "2F")                             # 開窗位置抖動(E2)

    band_open, doors, win_open, windows = _band_slot_openings(f, bed_x, kinds, rng)
    # 衛浴門貼樓梯間側(不放正中):正中的門正對餐桌/沙發視線——衛生空間
    # 的門要躲開公共空間的正面(建築師檢視 2026-07-20)。675 = 門半寬+牆邊留距。
    db, dbw = _slot(f.xs - 675, [750], f.xb, f.xs, f.blocked, "衛浴門")
    doors.append(DoorPlacement(4, len(band_open), Door(hinge="left", swing="out")))
    band_open.append(Opening(db - f.bx0, dbw, "door"))
    dst, dstw = _slot((f.xs + f.bx1) / 2, [DOOR_WIDTH], f.xs, f.bx1, f.blocked, "樓梯間門")
    doors.append(DoorPlacement(4, len(band_open), Door(hinge="left", swing="out")))
    band_open.append(Opening(dst - f.bx0, dstw, "door"))
    wb_, wbw = _slot((f.xb + f.xs) / 2, [800, 600], f.xb, f.xs, f.blocked, "衛浴北窗")
    windows.append(WindowPlacement(1, len(win_open)))
    win_open.append(Opening(wb_ - f.bx0, wbw, "window"))

    # Living Overflow(客廳過細時切出溢位空間,交 Program Selector 決定用途)。
    ov = _plan_overflow(f, brief, "upper",
                        has_study=("study" in kinds), has_family=False)
    south_ops = _living_south_windows(f, rng, ov, "起居")

    walls = [
        Wall((f.bx0, f.by0), (f.bx1, f.by0), EXT, openings=south_ops),  # 0 南外牆
        Wall((f.bx0, f.by1), (f.bx1, f.by1), EXT, openings=win_open),   # 1 北外牆
        Wall((f.bx0, f.by0), (f.bx0, f.by1), EXT),          # 2 西外牆
        Wall((f.bx1, f.by0), (f.bx1, f.by1), EXT),          # 3 東外牆
        Wall((f.bx0, f.yd), (f.bx1, f.yd), INT, openings=band_open),   # 4 帶分界牆
    ]
    for xi in bed_x[1:]:                                    # 臥室隔牆 + 管道牆(xb)
        walls.append(Wall((xi, f.yd), (xi, f.by1), INT))
    walls.append(Wall((f.xs, f.yd), (f.xs, f.by1), INT))    # 衛|梯

    windows = [WindowPlacement(0, i) for i in range(len(south_ops))] + windows

    live_w = f.bx0 if ov is None else ov.x1     # 起居室西牆(有溢位就往東縮)
    rooms = [
        Room("起居室", [(live_w, f.by0), (f.bx1, f.by0), (f.bx1, f.yd), (live_w, f.yd)],
             kind="living", code=ROOM_CODES["living"]),
        Room("衛浴", [(f.xb, f.yd), (f.xs, f.yd), (f.xs, f.by1), (f.xb, f.by1)],
             kind="bathroom", code=ROOM_CODES["bathroom"]),
        Room("樓梯間", [(f.xs, f.yd), (f.bx1, f.yd), (f.bx1, f.by1), (f.xs, f.by1)],
             kind="stair", code=ROOM_CODES["stair"]),
    ]
    rooms += _band_rooms(bed_x, kinds, f.yd, f.by1)        # 臥室(+書房)
    if ov is not None:                          # 溢位牆 + 房間 + 連通口
        _add_overflow(walls, doors, rooms, f, ov)

    # 家具:臥室=床+衣櫃;書房=書桌;起居室沙發+方桌(不成套,使用者定調:
    # 成套只留 1F 客廳);衛浴。
    fixtures = _band_fixtures(f, bed_x, kinds)
    cy_s = (f.by0 + f.yd) / 2
    if ov is None:                              # 起居室橫跨全帶:沙發貼西牆+方桌
        fixtures.append(FixturePlacement("sofa3", (f.bx0 + 75, cy_s), 270))
        fixtures.append(FixturePlacement("table4", ((f.bx0 + f.bx1) / 2, cy_s), 0))
    else:                                       # 緊湊起居室:沙發貼南牆面北
        fixtures.append(FixturePlacement(               # (不擋西側連通口/北側臥室門)
            "sofa3", ((live_w + f.bx1) / 2, f.by0 + 75), 0))
        _furnish_overflow(fixtures, f, ov, cy_s)
    fixtures.append(FixturePlacement("toilet", (f.xs - 60, f.by1 - 500), 90))
    fixtures.append(FixturePlacement("basin", (f.xs - 60, f.by1 - 1300), 90))
    if f.by1 - f.yd >= 3600:                   # 全深衛浴 → 浴缸貼管道牆
        fixtures.append(FixturePlacement(
            "bathtub", (f.xb + 60, (f.yd + f.by1) / 2), 270))

    spec = FloorPlanSpec(walls=walls, rooms=rooms, doors=doors, windows=windows,
                         fixtures=fixtures,
                         stairs=[_house_stair(f)], floor_label="2F", **f.spec_kw)
    return _finish_house(spec, f, "透天臥室層")


def _parking_layout(brief: HouseBrief, bx0: float, bx1: float,
                    by0: float, ytop: float) -> tuple[list, list]:
    """把車庫矩形 [bx0,bx1]×[by0,ytop] 切成 car_spaces 格汽車位(E3)。

    車位貼北(北帶分界牆側,那排門朝北開離車庫、不擋車),南側/兩側剩餘鋪
    「車道」——房間精確鋪滿車庫、無重疊(validate 覆蓋檢核)。每格擺一台
    汽車圖示。放不下(寬不夠/太淺)就報清楚的錯。回傳 (rooms, fixtures)。
    """
    n = brief.car_spaces
    W, depth = bx1 - bx0, ytop - by0
    stall_w, stall_l = CAR_STALL
    if W < n * stall_w:
        raise ValueError(
            f"車庫寬 {W/1000:.1f}m 放不下 {n} 個汽車位"
            f"(每位需 {stall_w/1000:.1f}m,共 {n*stall_w/1000:.1f}m),請加大基地寬")
    if depth < 4800:
        raise ValueError(
            f"地下車庫進深 {depth/1000:.1f}m 太淺(汽車位需 ≥4.8m),請加大基地南北深")

    sl = min(depth, stall_l)                       # 車位長(車庫夠深就在南側留車道)
    y0 = ytop - sl                                 # 車位南緣
    total_w = n * stall_w
    sx0 = bx0 + (W - total_w) / 2                  # 車位帶置中
    rooms: list = []
    fixtures: list = []
    for k in range(n):
        xl, xr = sx0 + k * stall_w, sx0 + (k + 1) * stall_w
        rooms.append(Room(f"車位{k+1}", [(xl, y0), (xr, y0), (xr, ytop), (xl, ytop)],
                          kind="parking", code=ROOM_CODES["parking"]))
        fixtures.append(FixturePlacement("car", ((xl + xr) / 2, (y0 + ytop) / 2), 0))

    def drive(x0: float, x1: float, ya: float, yb: float) -> None:
        if x1 - x0 > 1 and yb - ya > 1:            # 剩餘區 → 車道(鋪滿用)
            rooms.append(Room("車道", [(x0, ya), (x1, ya), (x1, yb), (x0, yb)],
                              kind="parking", code=ROOM_CODES["parking"]))
    drive(bx0, bx1, by0, y0)                       # 南側車道(含車道口)
    drive(bx0, sx0, y0, ytop)                      # 西側剩餘
    drive(sx0 + total_w, bx1, y0, ytop)            # 東側剩餘
    return rooms, fixtures


def generate_house_basement(brief: HouseBrief) -> FloorPlanSpec:
    """透天 B1F 車庫層(D2):南帶 車庫(南牆開車道口),北帶 儲藏|機房|樓梯間。

    地面下無對外窗;機房與樓上衛浴同開間(管道直落)。車道口為無門扇的
    洞(寬 2.5m,自動躲柱)。儲藏區照中間軸線分間(隔牆坐軸線 →
    柱藏牆交點,不凸在牆段中間)。
    深基地(三帶+天井)時:地下沒有天井,車庫連南帶+天井帶一起用
    (yn 才是北帶南緣);yd 那排柱站在車庫中間成開放柱列——車停柱間,
    地下停車場的常態。
    """
    f = _house_frame(brief)
    yn = f.yn                                   # 北帶南緣(無天井 = f.yd)
    gate, gw = _slot((f.bx0 + f.bx1) / 2, [2500, 2300], f.bx0, f.bx1,
                     f.blocked, "車道口")

    # 儲藏區分間界:西端 + 落在儲藏區內的中間軸線 + 濕區西牆。
    div_x = ([f.bx0]
             + [g for g in f.grid_x[1:-1] if f.bx0 + 500 < g < f.xb - 500]
             + [f.xb])

    band_open: list[Opening] = []
    doors: list[DoorPlacement] = []
    for i in range(len(div_x) - 1):             # 每間儲藏室一扇門
        pos, dw = _slot((div_x[i] + div_x[i + 1]) / 2, [DOOR_WIDTH, 750],
                        div_x[i], div_x[i + 1], f.blocked, f"儲藏室{i+1}門")
        doors.append(DoorPlacement(4, len(band_open),
                                   Door(hinge="left", swing="out")))
        band_open.append(Opening(pos - f.bx0, dw, "door"))
    dm, dmw = _slot((f.xb + f.xs) / 2, [750], f.xb, f.xs, f.blocked, "機房門")
    doors.append(DoorPlacement(4, len(band_open), Door(hinge="left", swing="out")))
    band_open.append(Opening(dm - f.bx0, dmw, "door"))
    dst, dstw = _slot((f.xs + f.bx1) / 2, [DOOR_WIDTH], f.xs, f.bx1, f.blocked, "樓梯間門")
    doors.append(DoorPlacement(4, len(band_open), Door(hinge="left", swing="out")))
    band_open.append(Opening(dst - f.bx0, dstw, "door"))

    walls = [
        Wall((f.bx0, f.by0), (f.bx1, f.by0), EXT,           # 0 南外牆 + 車道口
             openings=[Opening(gate - f.bx0, gw, "door")]),
        Wall((f.bx0, f.by1), (f.bx1, f.by1), EXT),          # 1 北外牆(無窗)
        Wall((f.bx0, f.by0), (f.bx0, f.by1), EXT),          # 2 西外牆
        Wall((f.bx1, f.by0), (f.bx1, f.by1), EXT),          # 3 東外牆
        Wall((f.bx0, yn), (f.bx1, yn), INT,                 # 4 帶分界牆
             openings=band_open),
        Wall((f.xs, yn), (f.xs, f.by1), INT),               # 5 機|梯
    ]
    for xi in div_x[1:]:                        # 儲藏隔牆(坐軸線)+ 管道牆(xb)
        walls.append(Wall((xi, yn), (xi, f.by1), INT))

    # 車庫:要汽車位就切成 car_spaces 格車位(+車道、擺車圖);否則整層一間車庫。
    if brief.car_spaces > 0:
        garage_rooms, car_fixtures = _parking_layout(brief, f.bx0, f.bx1, f.by0, yn)
    else:
        garage_rooms = [Room("車庫", [(f.bx0, f.by0), (f.bx1, f.by0),
                                      (f.bx1, yn), (f.bx0, yn)],
                             kind="parking", code=ROOM_CODES["parking"])]
        car_fixtures = []

    rooms = [
        *garage_rooms,
        Room("機房", [(f.xb, yn), (f.xs, yn), (f.xs, f.by1), (f.xb, f.by1)],
             kind="storage", code=ROOM_CODES["storage"]),
        Room("樓梯間", [(f.xs, yn), (f.bx1, yn), (f.bx1, f.by1), (f.xs, f.by1)],
             kind="stair", code=ROOM_CODES["stair"]),
    ]
    for i in range(len(div_x) - 1):
        rooms.append(Room("儲藏室", [(div_x[i], yn), (div_x[i + 1], yn),
                                     (div_x[i + 1], f.by1), (div_x[i], f.by1)],
                          kind="storage", code=ROOM_CODES["storage"]))

    spec = FloorPlanSpec(walls=walls, rooms=rooms, doors=doors, windows=[],
                         fixtures=car_fixtures,
                         stairs=[_house_stair(f)], floor_label="B1F", **f.spec_kw)
    return _finish_house(spec, f, "透天 B1F 車庫層")


# ---------------------------------------------------------------------------
# 方位約束(C2):整張圖鏡射
# ---------------------------------------------------------------------------
# 預設格局(不鏡射)的方位:主臥在西北角(臥室帶西端)、廚房在東南
# (公共帶服務核)。約束靠「左右翻 / 上下翻整張圖」達成:
#   主臥要在東 → 左右翻;主臥要在南 / 廚房要在北 → 上下翻。
_CORNERS = ("NW", "NE", "SW", "SE")
_SIDES = ("N", "S", "E", "W")


def _resolve_mirrors(brief: HouseBrief) -> tuple[bool, bool]:
    """方位約束 → (左右翻?, 上下翻?);互相矛盾就明確報錯。"""
    need: dict[str, tuple[bool, str]] = {}   # axis -> (要不要翻, 誰要求的)

    def ask(axis: str, value: bool, source: str) -> None:
        if axis in need and need[axis][0] != value:
            raise ValueError(
                f"方位約束衝突:「{source}」與「{need[axis][1]}」無法同時成立"
                f"(臥室帶與公共帶南北各佔一半,主臥和廚房必在異側)")
        need[axis] = (value, source)

    if brief.master_corner is not None:
        c = brief.master_corner.upper()
        if c not in _CORNERS:
            raise ValueError(f"master_corner 需為 {_CORNERS},收到 {brief.master_corner!r}")
        ask("x", "E" in c, f"主臥在{c}")
        ask("y", "S" in c, f"主臥在{c}")
    if brief.kitchen_side is not None:
        s = brief.kitchen_side.upper()
        if s not in _SIDES:
            raise ValueError(f"kitchen_side 需為 {_SIDES},收到 {brief.kitchen_side!r}")
        if s in ("E", "W"):
            ask("x", s == "W", f"廚房靠{s}")
        else:
            ask("y", s == "N", f"廚房靠{s}")

    return (need.get("x", (False, ""))[0], need.get("y", (False, ""))[0])


def _mirror_spec(spec: FloorPlanSpec, mx: bool, my: bool) -> FloorPlanSpec:
    """把整張單戶 spec 左右(mx)/上下(my)鏡射——牆/房間/門窗/家具全翻。

    鏡射軸 = 基地中心(單戶的建築置中於基地,翻完 grid_origin 不變)。
    鏡射語意與 B6 place_unit 同一套:洞口位置(距牆起點的距離)是等距
    變換、不變;奇數次鏡射翻門的開向(out↔in);家具旋轉照 _t_rotation;
    流理台交換起訖點保住檯面側。翻完仍會跑 validate_spec 整套檢核。
    """
    xs = [p[0] for p in spec.site_boundary]
    ys = [p[1] for p in spec.site_boundary]
    sx2, sy2 = min(xs) + max(xs), min(ys) + max(ys)   # x' = sx2 - x

    def tp(p: Point) -> Point:
        x, y = p
        return (sx2 - x if mx else x, sy2 - y if my else y)

    mirrored = mx != my            # 奇數次鏡射(左右手翻轉)

    walls = [Wall(start=tp(w.start), end=tp(w.end), thickness=w.thickness,
                  openings=[Opening(op.position, op.width, op.kind)
                            for op in w.openings])
             for w in spec.walls]
    doors = [DoorPlacement(dp.wall_index, dp.opening_index,
                           Door(hinge=dp.door.hinge,
                                swing=_t_swing(mirrored, dp.door.swing),
                                width=dp.door.width))
             for dp in spec.doors]
    windows = [WindowPlacement(wp.wall_index, wp.opening_index,
                               Window(lines=wp.window.lines, width=wp.window.width))
               for wp in spec.windows]
    rooms = [Room(name=r.name, points=[tp(p) for p in r.points],
                  kind=r.kind, code=r.code, note=r.note)
             for r in spec.rooms]
    fixtures: list = []
    for fx in spec.fixtures:
        if isinstance(fx, Counter):
            a, b = tp(fx.start), tp(fx.end)
            if mirrored:
                a, b = b, a
            fixtures.append(Counter(start=a, end=b, depth=fx.depth,
                                    sink=fx.sink, stove=fx.stove))
        else:
            fixtures.append(FixturePlacement(
                name=fx.name, insert=tp(fx.insert),
                rotation=_t_rotation(mx, my, fx.rotation)))

    # 柱心(多樓層透天有指定)也要跟著翻,否則鏡射後柱位對不上牆與各層。
    col_centers = ([tp(c) for c in spec.column_centers]
                   if spec.column_centers is not None else None)

    # 樓梯:翻位置與行進方向(梯井左右手是畫圖寫死的,示意平面不強求)。
    stairs = [_mirror_stair(st, sx2, sy2, mx, my) for st in spec.stairs]

    return replace(
        spec, walls=walls, doors=doors, windows=windows, rooms=rooms,
        fixtures=fixtures, column_centers=col_centers, stairs=stairs,
        x_spacings=list(reversed(spec.x_spacings)) if mx else spec.x_spacings,
        y_spacings=list(reversed(spec.y_spacings)) if my else spec.y_spacings,
    )


def _mirror_stair(st, sx2: float, sy2: float, mx: bool, my: bool):
    """樓梯(Stair/UStair)左右/上下鏡射:重算最小角 origin 與行進方向。

    origin 一律是樓梯間矩形最小 x/y 角。先由 origin+width+length+direction 還原
    矩形 x/y 範圍,鏡射後取新的最小角當 origin;方向 mx 翻東西、my 翻南北。
    梯段左右手(起步/折返、梯井側)是繪圖層寫死,不隨鏡射改(示意圖可接受)。
    """
    ox, oy = st.origin
    if st.direction in ("north", "south"):
        xext, yext = (ox, ox + st.width), (oy, oy + st.length)
    else:
        xext, yext = (ox, ox + st.length), (oy, oy + st.width)
    if mx:
        xext = (sx2 - xext[1], sx2 - xext[0])
    if my:
        yext = (sy2 - yext[1], sy2 - yext[0])
    d = st.direction
    if mx and d in ("east", "west"):
        d = "west" if d == "east" else "east"
    if my and d in ("north", "south"):
        d = "south" if d == "north" else "north"
    return replace(st, origin=(xext[0], yext[0]), direction=d)


def generate_floor_plan(brief: Brief) -> FloorPlanSpec:
    """需求 → FloorPlanSpec(已通過 validate_spec,可直接餵 draw_floor_plan)。"""
    if isinstance(brief, HouseBrief):
        spec = _generate_house(brief)
        mx, my = _resolve_mirrors(brief)          # 方位約束(C2)
        if mx or my:
            spec = _mirror_spec(spec, mx, my)
    elif isinstance(brief, CorridorBrief):
        spec = _generate_corridor(brief)
    else:
        raise TypeError(f"未知需求型別:{type(brief).__name__}")

    # Collision Engine(v0.6):單層住宅 / 集合住宅標準層的碰撞修復——透天各層
    # 走 _finish_house→_validate_or_raise,這條(generate_floor_plan)直接呼叫
    # validate_spec,故在此補上同一個 resolve(有碰撞才動,沒碰撞不動)。
    resolve_collisions(spec)
    problems = validate_spec(spec)
    if problems:
        raise ValueError("產生的設計未通過檢核:\n  - " + "\n  - ".join(problems))
    return spec


# ---------------------------------------------------------------------------
# 設計檢核
# ---------------------------------------------------------------------------
def validate_spec(spec: FloorPlanSpec) -> list[str]:
    """檢核設計是否合理,回傳問題清單(空 = 通過)。

    檢查:房間互不重疊、面積合計=建築面積、每個 臥室/浴廁/廚房 有門、
    臥室/客廳 有窗、所有洞口不壓柱(含淨距)。另含法規/動線規則:
    浴室無窗須標機械排風(C1.5a)、多戶建築 ≥2 樓梯間(C1.5a)、
    有走道時臥室門須通走道、臥室門不得開進廚房(C1.5b)、
    居室採光深度 ≤6m、家具互不重疊且不擋門的迴轉(C1.5c)。
    """
    problems: list[str] = []

    bx0, by0 = spec.grid_origin
    bw, bd = sum(spec.x_spacings), sum(spec.y_spacings)
    building = Polygon([(bx0, by0), (bx0 + bw, by0),
                        (bx0 + bw, by0 + bd), (bx0, by0 + bd)])

    polys = [Polygon(r.points) for r in spec.rooms]
    for i in range(len(polys)):
        for j in range(i + 1, len(polys)):
            if polys[i].intersection(polys[j]).area > 1.0:
                problems.append(f"房間重疊:{spec.rooms[i].name}×{spec.rooms[j].name}")
    union = unary_union(polys)
    if abs(union.area - building.area) > building.area * 1e-6:
        problems.append(
            f"房間面積合計 {union.area/1e6:.2f}m² ≠ 建築 {building.area/1e6:.2f}m²")

    def openings_on(room_poly: Polygon, kind: str) -> int:
        count = 0
        for w in spec.walls:
            for op in w.openings:
                if op.kind != kind:
                    continue
                if room_poly.boundary.distance(SPoint(w.point_at(op.position))) < 1.0:
                    count += 1
        return count

    for room, poly in zip(spec.rooms, polys):
        if room.kind in ("bedroom", "bathroom", "kitchen", "stair", "storage"):
            if openings_on(poly, "door") < 1:
                problems.append(f"{room.name} 沒有門")
        if room.kind in ("bedroom", "living"):
            if openings_on(poly, "window") < 1:
                problems.append(f"{room.name} 沒有窗")
        # C1.5a:浴室要有對外窗,否則必須標示機械排風。
        if room.kind == "bathroom":
            if openings_on(poly, "window") < 1 and "排風" not in room.note:
                problems.append(f"{room.name} 無窗且未標示機械排風")

    # C1.5a:多戶建築(≥2 個居住單元)必須有 ≥2 座樓梯間(兩個逃生方向)。
    # 用 kind="living" 的數量判斷戶數:單戶只有一個客廳,不觸發。
    n_units = sum(1 for r in spec.rooms if r.kind == "living")
    if n_units >= 2:
        n_stairs = sum(1 for r in spec.rooms if r.kind == "stair")
        if n_stairs < 2:
            problems.append(f"多戶建築只有 {n_stairs} 座樓梯間(逃生需 ≥2)")

    # C1.5b:單戶住宅有走道時,每間臥室至少要有一扇門開向走道(臥室門不得只
    # 直開客餐廳)。⚠️ 僅單戶適用——集合住宅的走廊是公設,單元內的臥室門開向
    # 自家客廳才對(否則臥室直通公共走廊),故多戶建築跳過。
    corridor_polys = [p for r, p in zip(spec.rooms, polys) if r.kind == "corridor"]
    if corridor_polys and n_units < 2:
        for room, poly in zip(spec.rooms, polys):
            if room.kind != "bedroom":
                continue
            reaches = any(
                poly.boundary.distance(SPoint(w.point_at(op.position))) < 1.0
                and any(cp.boundary.distance(SPoint(w.point_at(op.position))) < 1.0
                        for cp in corridor_polys)
                for w in spec.walls for op in w.openings if op.kind == "door")
            if not reaches:
                problems.append(f"{room.name} 的門未通走道")

    # C1.5b:臥室門不得開進廚房(動線常識;套衛的門開進浴室是刻意的,不管)。
    kitchen_polys = [p for r, p in zip(spec.rooms, polys) if r.kind == "kitchen"]
    for room, poly in zip(spec.rooms, polys):
        if room.kind != "bedroom":
            continue
        for w in spec.walls:
            for op in w.openings:
                if op.kind != "door":
                    continue
                pt = SPoint(w.point_at(op.position))
                if poly.boundary.distance(pt) < 1.0 and any(
                        kp.boundary.distance(pt) < 1.0 for kp in kitchen_polys):
                    problems.append(f"{room.name} 的門開進廚房")

    # 柱網規則性(結構經濟;使用者定調:柱網儘量規則、近似等距、跨度合理)。
    # X 向查範圍+均勻度;Y 向只查上限(走廊 1.8m 這種窄跨是刻意的)。
    # ⚠️ 集合住宅例外:柱跨依戶寬,寬窄戶(套房/1房/2房)並存是常態,柱都落在
    #    分戶牆上(不生孤柱),故只守 3~9m 經濟範圍、不強求等距(以戶數判別)。
    xs = spec.x_spacings
    # 集合住宅判別:多戶(≥2 客廳)或 ≥2 樓梯間(B1F 地下室沒有住戶客廳,
    # 但兩端逃生核仍在——柱跨仍依上層戶寬,同樣不強求等距)。
    is_corridor = (sum(1 for r in spec.rooms if r.kind == "living") >= 2
                   or sum(1 for r in spec.rooms if r.kind == "stair") >= 2)
    if min(xs) < BAY_SPAN_LIMITS[0] or max(xs) > BAY_SPAN_LIMITS[1]:
        problems.append(
            f"X 向跨距 {min(xs)/1000:.1f}~{max(xs)/1000:.1f}m "
            f"超出 {BAY_SPAN_LIMITS[0]/1000:.0f}~{BAY_SPAN_LIMITS[1]/1000:.0f}m")
    elif not is_corridor and max(xs) / min(xs) > BAY_RATIO_MAX:
        problems.append(
            f"X 向柱網不規則(跨距 max/min = {max(xs)/min(xs):.2f} "
            f"> {BAY_RATIO_MAX})")
    if max(spec.y_spacings) > BAY_SPAN_LIMITS[1]:
        problems.append(f"Y 向跨距 {max(spec.y_spacings)/1000:.1f}m 超過 9m")

    # C1.5b:玄關(落塵區)必須貼著一扇門——大門要開進玄關。
    for room, poly in zip(spec.rooms, polys):
        if room.kind == "foyer" and openings_on(poly, "door") < 1:
            problems.append(f"{room.name} 沒貼任何門(玄關應在大門內側)")

    # C1.5c:採光深度——臥室/客廳沿「窗的法線方向」的房間深度,至少要有
    # 一扇窗 ≤ 上限(離窗太遠的角落照不到光)。無窗的房間上面已抓、不重複報。
    for room, poly in zip(spec.rooms, polys):
        if room.kind not in ("bedroom", "living"):
            continue
        depths = []
        for w in spec.walls:
            nx_, ny_ = w.normal_vector
            for op in w.openings:
                if op.kind != "window":
                    continue
                wx, wy = w.point_at(op.position)
                if poly.boundary.distance(SPoint((wx, wy))) >= 1.0:
                    continue
                depths.append(max(abs((px - wx) * nx_ + (py - wy) * ny_)
                                  for px, py in room.points))
        if depths and min(depths) > DAYLIGHT_DEPTH_MAX + 1.0:
            problems.append(
                f"{room.name} 採光深度 {min(depths)/1000:.1f}m "
                f"超過上限 {DAYLIGHT_DEPTH_MAX/1000:.0f}m(需增開窗)")

    # C1.5c:家具碰撞——家具/流理台互不重疊、不擋任何門的迴轉方塊。
    from src.drafting.fixtures import counter_footprint, fixture_footprint

    fps: list[tuple[str, Polygon]] = []
    for fx in spec.fixtures:
        if isinstance(fx, FixturePlacement):
            fps.append((fx.name, Polygon(fixture_footprint(fx))))
        elif isinstance(fx, Counter):
            fps.append(("counter", Polygon(counter_footprint(fx))))
    for i in range(len(fps)):
        for j in range(i + 1, len(fps)):
            if fps[i][1].intersection(fps[j][1]).area > 100:
                problems.append(f"家具重疊:{fps[i][0]}×{fps[j][0]}")
    for dp in spec.doors:
        w = spec.walls[dp.wall_index]
        op = w.openings[dp.opening_index]
        cx_, cy_ = w.point_at(op.position)
        ux, uy = w.unit_vector
        nx_, ny_ = w.normal_vector
        s = 1.0 if dp.door.swing == "out" else -1.0   # 門扇掃過哪一側
        h, e = op.width / 2, op.width
        square = Polygon([
            (cx_ - ux * h, cy_ - uy * h),
            (cx_ + ux * h, cy_ + uy * h),
            (cx_ + ux * h + s * nx_ * e, cy_ + uy * h + s * ny_ * e),
            (cx_ - ux * h + s * nx_ * e, cy_ - uy * h + s * ny_ * e),
        ])
        for name, fp in fps:
            if square.intersection(fp).area > 100:
                problems.append(
                    f"家具 {name} 擋住門的迴轉(牆 {dp.wall_index} 的門)")

    # 洞口不壓柱。
    columns = resolve_columns(spec, build_grid(spec))
    r = spec.column_size / 2 + COLUMN_CLEARANCE
    for w in spec.walls:
        ux, uy = (None, None)
        for op in w.openings:
            if ux is None:
                ux, uy = w.unit_vector
                nx_, ny_ = w.normal_vector
            for c in columns:
                cx, cy = c.center
                if abs((cx - w.start[0]) * nx_ + (cy - w.start[1]) * ny_) \
                        > w.thickness / 2 + c.width / 2:
                    continue
                col_pos = (cx - w.start[0]) * ux + (cy - w.start[1]) * uy
                if not (op.position + op.width / 2 <= col_pos - r
                        or op.position - op.width / 2 >= col_pos + r):
                    problems.append(
                        f"洞口壓柱:牆 {w.start}->{w.end} 位置 {op.position:.0f} "
                        f"的{op.kind}與柱 {c.center} 衝突")
    return problems


# ---------------------------------------------------------------------------
# 示範:三份不同需求 → 三張完整的圖
# ---------------------------------------------------------------------------
DEMO_BRIEFS: list[tuple[str, Brief]] = [
    ("gen_house_2br", HouseBrief(site_width=12000, site_depth=11000, bedrooms=2)),
    ("gen_house_3br", HouseBrief(site_width=18000, site_depth=14000, bedrooms=3)),
    ("gen_corridor6", CorridorBrief(units_per_row=6)),
]


def main() -> None:
    from src.drafting.apartment_plan import draw_floor_plan
    from src.standards.loader import apply_standard, load_standard, new_document

    out_dir = _PROJECT_ROOT / "output"
    out_dir.mkdir(exist_ok=True)
    for name, brief in DEMO_BRIEFS:
        spec = generate_floor_plan(brief)
        std = load_standard()
        doc = new_document()
        layers = apply_standard(doc, std)
        draw_floor_plan(doc.modelspace(), spec, layers)
        doc.saveas(out_dir / f"{name}.dxf")
        print(f"[OK] {name}.dxf: {len(spec.rooms)} 室 {len(spec.doors)} 門 "
              f"{len(spec.windows)} 窗 {len(spec.fixtures)} 件設備")


if __name__ == "__main__":
    main()


# =============================================================================
# PENDING(待確認假設彙整)
# =============================================================================
# 1. 設計規則常數(臥室最小寬 2.8m、主臥 ×1.35、服務核 2.6~3.4m、目標跨距
#    4.5m、走廊 1.8m、走道 1.2m、通道口 1.5m 等)皆為經驗值。
# 2. 單戶固定「兩帶式、朝南入口」;≥3 房自動加主臥套衛(C1.5a)。走道規則
#    (C1.5b):≥3 房必設、2 房預設不設(擠壓時退回)、1 房不設——依使用者
#    2026-07-12 定調「走道=連接多獨立房間的共用動線,房間多才需要;小宅
#    動線融入客廳」。玄關已加(C1.5b:2.2×1.5m、隔屏牆+鞋櫃,跟著大門
#    實際位置走)。變化維度(朝向/陽台/樓梯電梯核)之後 C1.5b/c 加深。
#    走道通道口固定正對大門「理想位置」;大門若因躲柱被平移,兩者會小錯位。
# 3. 家具擺放規則:床頭靠北外牆、衣櫃貼東側牆近北角、沙發背靠西牆、
#    L 型流理台沿東+北……皆為簡化規則。碰撞檢核已加(C1.5c):家具互不
#    重疊、不擋門的迴轉方塊(以洞口寬×寬的方形近似扇形);「通道走得過去」
#    的通行寬度檢核未做。方桌會自動讓開大門迴轉、擺不下就不擺。
# 4. 集合住宅:單元固定用 one_room_unit(可自帶 UnitSpec);兩端逃生核已加
#    (C1.5a),核開間寬 CORE_W=3.1m、梯段/平台尺寸為經驗值;每戶陽台未加
#    (C1.5b 後續)。
# 5. validate_spec 已含簡化法規/機能檢核(通風/逃生 C1.5a、動線 C1.5b、
#    採光深度 6m/家具碰撞/柱網規則性 C1.5c);真正的法規檢討(採光「面積」
#    比、走廊寬法規、步行逃生距離)未做。採光深度上限 6m 為簡化經驗值
#    (技術規則實為窗高相關)。隔間坐樑吸附容差 0.6m、軸線向隔牆吸附容差
#    0.9m、跨距均勻度上限 1.6 皆為經驗值;僅 X 向吸附(Y 向的帶分界牆
#    天生在軸線上),走道南牆/浴廚分界等次要牆不吸附。
# 6. 柱網(使用者 2026-07-12 定調的結構原則)已落實:等距起手、跨距 3~9m、
#    跨數擇優(孤柱列最少)、軸線挪到主要隔牆(柱藏牆交點)、柱距門窗淨距
#    300。孤柱列無法全消:1 房(無隔牆可吸)、及挪軸會讓跨距失衡(>1.6)
#    的格局會留 1 列孤柱(凸在帶分界牆上)——規則性優先是刻意取捨。
#    未做:上下樓層柱位對齊(單層示範不適用)、停車場柱距配車位、集合住宅
#    柱網(目前 = 單元寬 4m 一跨,偏小;實務常兩戶一跨 8m,需同時對齊
#    分戶牆——待改)。
# =============================================================================
