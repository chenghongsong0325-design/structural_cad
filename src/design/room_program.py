"""房間面積程式(F3)—— 把「房間該多大」從「房間長什麼形狀」裡拆出來。

在這支模組出現以前,格局引擎是「固定尺寸 + 餘量全給客廳」:臥室帶進深卡在
NORTH_BAND_RANGE 上限、臥室寬卡在 MAX_BEDROOM_WIDTH,撞到上限就凍結,基地
再大也只有客廳(和天井)會長大。實測 20×16m 放大到 32×26m(基地 +160%),
主臥 23.1m² → 23.1m²、臥室 17.1 → 17.1,一格沒動;起居室卻有 91m²,是臥室
的五倍——那不是設計,那是把剩下的地板都塞給客廳。

改成「面積程式(program)」之後,設計流程分成五段(使用者 2026-07-20 定調):

    Room Requirement → Room Area → Room Shape → Room Position → Wall/Door/Window
    (要幾間、各自 ┐  (每間幾 m²,  (幾 m² 要   (刀位、柱網   (既有流程,
     的面積範圍)    水位法分配)   長成幾×幾)   吸附、守門)    完全不動)

本模組負責前三段,後兩段仍由 layout_generator 既有的幾何/柱網邏輯處理。
拆開的好處是「面積」與「形狀」不再綁死:18m² 可以是 3×6、4×4.5、5×3.6,
由 solve_band 依可用寬度與長寬比偏好自己挑,而不是寫死 room.width = 3.0。

⚠️ 誰讓誰(使用者 2026-07-20 定調):**柱網、結構、牆線優先**。本模組算出來
的面積是「目標」不是「命令」——layout_generator 拿到目標後仍要跑柱網吸附與
可行性守門,吸附會把牆挪到軸線上、面積因此偏移。允許 ±AREA_TOLERANCE(10%)
的落差;守不住就以柱網為準,絕不為了湊面積讓柱子凸進房間。

典型用法::

    from src.design.room_program import ROOM_PROGRAM, allocate_areas, solve_band

    plan = allocate_areas(["master_bedroom", "bedroom", "bedroom", "living"],
                          budget_m2=120)
    plan.areas          # [22.4, 16.1, 16.1, 48.0]
    plan.leftover_m2    # 17.4(沒人吃得下的餘量 → 由呼叫端變成院子,不塞客廳)

⚠️ 待確認假設見模組結尾 PENDING 區塊(面積範圍、優先序權重、長寬比偏好)。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# 面積目標的容許誤差(使用者 2026-07-20 定調):柱網吸附/最小寬守門會把牆挪
# 開,實際面積跟目標對不齊是正常的。±10% 內視為達標,超過才算沒做到。
AREA_TOLERANCE = 0.10

# 優先序 → 分配權重:數字小的先吃、也吃得多。權重差距刻意做得溫和
# (不是「填滿高優先才輪到下一個」),因為使用者【4】要的是「一起變大」
# 而不是「客廳一個人變大」的鏡像版本(高優先一個人變大)。
PRIORITY_WEIGHT = {1: 1.00, 2: 0.85, 3: 0.70, 4: 0.55, 5: 0.45}
_DEFAULT_WEIGHT = 0.35


# ---------------------------------------------------------------------------
# 第一段:Room Requirement —— 每種房間的面積範圍與硬性幾何下限
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class RoomRequirement:
    """一種房間的需求:面積範圍(m²)+ 形狀約束(mm)+ 分配優先序。

    面積三段(使用者 2026-07-20 指定):
      * min_area:低於此就不能住人/不好用,分不到就該報「基地太小」。
      * preferred_area:舒適的目標值,水位法會優先把每間都拉到這裡。
      * max_area:再大只是浪費(客廳 60m² 已經很大了);到頂就不再吃餘量,
        餘量讓給別的房間,最後真的沒人吃得下才變成院子。

    形狀約束(硬性,來自家具/法規/結構,**不是**美感偏好,故不參與面積分配):
      * min_width / min_depth:擺得下該房間的家具的最小淨尺寸。
      * max_depth:採光深度上限——離窗超過這個距離就是暗房(見引擎的
        DAYLIGHT_DEPTH_MAX 與 validate_spec 的 C1.5c 檢核)。
      * aspect_target / aspect_max:長邊/短邊的偏好與上限。細長的房間
        (1:3 以上)擺不下床、也不好用,shape 階段用它挑長寬組合。
    """

    kind: str
    label: str
    min_area: float
    preferred_area: float
    max_area: Optional[float]          # None = 無上限(目前無人使用)
    priority: int
    min_width: float = 2400
    min_depth: float = 2400
    max_depth: Optional[float] = 6000
    aspect_target: float = 1.20
    aspect_max: float = 2.60

    @property
    def weight(self) -> float:
        return PRIORITY_WEIGHT.get(self.priority, _DEFAULT_WEIGHT)

    def within_tolerance(self, area_m2: float) -> bool:
        """實際面積算不算「達標」(±AREA_TOLERANCE,且不得低於 min)。"""
        if area_m2 < self.min_area * (1 - AREA_TOLERANCE):
            return False
        if self.max_area is not None:
            return area_m2 <= self.max_area * (1 + AREA_TOLERANCE)
        return True


# 面積程式表 —— **所有房間尺寸的唯一來源**。要調整房子的大小感,改這裡,
# 不要回去改 layout_generator 的常數(那些常數現在只剩「結構/家具硬下限」)。
#
# 優先序(使用者 2026-07-20 指定):1 主臥 → 2 次臥 → 3 浴室 → 4 客廳 → 5 廚房。
# 客廳給了 max=60(使用者定調):超過 60m² 的客廳只是把地板攤平,餘量該給
# 別的空間或留成院子。
ROOM_PROGRAM: dict[str, RoomRequirement] = {
    "master_bedroom": RoomRequirement(
        "master_bedroom", "主臥室", 12, 18, 28, priority=1,
        min_width=3000, min_depth=3000),
    "bedroom": RoomRequirement(
        "bedroom", "臥室", 9, 13, 20, priority=2,
        min_width=2800, min_depth=2800),
    "bathroom": RoomRequirement(
        "bathroom", "浴室", 4, 6, 10, priority=3,
        min_width=1800, min_depth=2000, aspect_max=3.00),
    # 客廳的形狀約束(使用者 2026-07-21 指定,真正參與生成、不只 benchmark 評分):
    # min_width/min_depth 3.8m、aspect_max 2.2。客廳寬到超過 深×aspect_max 時,
    # Layout Engine 會切出 Living Overflow 交給 select_overflow_program(不硬拉長
    # 客廳)。這三個值改這裡就生效,不寫死在 layout_generator。
    "living": RoomRequirement(
        "living", "客廳", 20, 35, 60, priority=4,
        min_width=3800, min_depth=3800, aspect_max=2.20),
    "kitchen": RoomRequirement(
        "kitchen", "廚房", 6, 9, 15, priority=5,
        min_width=2400, min_depth=2400, aspect_max=3.00),
    "dining": RoomRequirement(
        "dining", "餐廳", 7, 11, 18, priority=5,
        min_width=2700, min_depth=2700),
    "study": RoomRequirement(
        "study", "書房", 5, 8, 14, priority=5,
        min_width=2400, min_depth=2400),
    "storage": RoomRequirement(
        "storage", "儲藏室", 2, 4, 8, priority=6,
        min_width=1500, min_depth=1500, max_depth=None, aspect_max=3.50),
    # 家庭廳/多功能室 —— Living Overflow 切出來的「彈性起居空間」。它不是客廳
    # (兩間 living 會被 validate 當成兩戶要兩座梯)、也不是走道(corridor 會觸發
    # 「臥室門須通走道」而客廳側的臥室門搆不到)。獨立一種 kind="family",
    # 只需一扇門一扇窗,長寬比放寬(沿街的家庭廳本來就可以是長形起居帶)。
    "family": RoomRequirement(
        "family", "家庭廳", 6, 12, None, priority=5,
        min_width=2400, min_depth=2400, max_depth=None, aspect_max=3.50),
}


def requirement(kind: str) -> RoomRequirement:
    """取一種房間的需求;沒登記過的種類回一個保守的預設(不會炸掉呼叫端)。"""
    if kind in ROOM_PROGRAM:
        return ROOM_PROGRAM[kind]
    return RoomRequirement(kind, kind, 5, 8, 16, priority=6)


# ---------------------------------------------------------------------------
# 第二段:Room Area —— 依可用面積,把預算分配到各房間(水位法)
# ---------------------------------------------------------------------------
@dataclass
class AreaPlan:
    """一次分配的結果:各房間目標面積(m²,順序同輸入)+ 沒人吃得下的餘量。"""

    kinds: list[str]
    areas: list[float]
    leftover_m2: float = 0.0
    reqs: list[RoomRequirement] = field(default_factory=list)

    @property
    def total_m2(self) -> float:
        return sum(self.areas)

    def area_of(self, kind: str) -> float:
        """某種房間的面積合計(同種多間就加總)。"""
        return sum(a for k, a in zip(self.kinds, self.areas) if k == kind)

    def shortfalls(self, actual: list[float]) -> list[str]:
        """實際面積 vs 目標,回傳超出容許誤差的清單(空 = 全部達標)。"""
        out = []
        for req, target, got in zip(self.reqs, self.areas, actual):
            if target <= 0:
                continue
            if abs(got - target) > target * AREA_TOLERANCE:
                out.append(f"{req.label} 目標 {target:.1f}m² 實際 {got:.1f}m² "
                           f"(差 {(got - target) / target * 100:+.0f}%)")
        return out


def _fill(base: list[float], caps: list[float], weights: list[float],
          budget: float) -> tuple[list[float], float]:
    """加權水位法:把 budget 依 weights 比例加到 base 上,誰先碰到 caps 誰封頂,
    封頂的人讓出來的份再分給還沒滿的人,直到預算用完或全部封頂。

    這是「一起變大」的關鍵(使用者【4】):不是填滿高優先才輪到下一個,而是
    大家同時漲、高優先漲得快。回傳 (分配後的值, 沒用掉的預算)。
    """
    cur = list(base)
    active = [i for i in range(len(cur)) if caps[i] > cur[i] + 1e-9]
    remaining = budget
    while remaining > 1e-9 and active:
        wsum = sum(weights[i] for i in active)
        if wsum <= 0:
            break
        # 這一輪每個人「照權重」可以分到多少;有人會超過自己的 cap。
        capped: list[int] = []
        for i in active:
            share = remaining * weights[i] / wsum
            room = caps[i] - cur[i]
            if share >= room:
                capped.append(i)
        if not capped:                       # 沒人封頂 → 全部照比例吃完,結束
            for i in active:
                cur[i] += remaining * weights[i] / wsum
            remaining = 0.0
            break
        # 有人封頂:先讓封頂的人吃到 cap,剩下的預算下一輪重分
        used = 0.0
        for i in capped:
            used += caps[i] - cur[i]
            cur[i] = caps[i]
        remaining -= used
        active = [i for i in active if i not in capped]
    return cur, max(0.0, remaining)


def allocate_areas(kinds: list[str], budget_m2: float,
                   *, weights: Optional[list[float]] = None,
                   caps: Optional[list[float]] = None) -> AreaPlan:
    """房間清單 + 可用面積 → 各房間目標面積(m²)。

    三階段(使用者【5】的優先序,用加權水位法實現):
      1. 全員先到 min_area —— 不夠就報錯(基地真的太小,不該硬塞)。
      2. 餘額往 preferred_area 漲:大家一起漲、高優先漲得快(權重見
         PRIORITY_WEIGHT)。這是「基地大 20% 每間都變大」的來源。
      3. 還有餘額就繼續往 max_area 漲,同樣加權。
      4. 全員封頂後仍有剩 → 放進 leftover_m2 **原封不動退回**,不塞給客廳
         (使用者 2026-07-20 定調)。呼叫端該把它變成院子/陽台。

    weights:選填,蓋掉優先序權重(給呼叫端做情境微調,目前未使用)。
    caps:選填,再收緊每間的上限(取 min(max_area, caps[i]))。**幾何容量**
      要從這裡進來:格局是帶狀的,臥室帶再怎麼分配也不可能超過「帶寬 × 帶深
      上限(採光深度)」。不收緊的話,窄基地會分到裝不下的面積,幾何階段才
      發現塞不下 → 整份設計判定失敗;收緊後會自動退回較小但放得下的方案。
    """
    reqs = [requirement(k) for k in kinds]
    if not reqs:
        return AreaPlan(kinds=[], areas=[], leftover_m2=max(0.0, budget_m2))

    mins = [r.min_area for r in reqs]
    need = sum(mins)
    if budget_m2 < need - 1e-9:
        raise ValueError(
            f"可配置面積 {budget_m2:.1f}m² 不足最低需求 {need:.1f}m²"
            f"({'、'.join(f'{r.label}{r.min_area:g}' for r in reqs)}),"
            f"請加大基地或減少房間")

    w = weights if weights is not None else [r.weight for r in reqs]
    tops = [r.max_area if r.max_area is not None else float("inf") for r in reqs]
    if caps is not None:
        # 幾何容量收緊上限;但不得低於 min_area(低於就是真的放不下,讓
        # 幾何階段的最小寬守門去報錯,那裡的訊息比較具體)。
        tops = [max(mins[i], min(tops[i], caps[i])) for i in range(len(reqs))]
    prefs = [min(r.preferred_area, tops[i]) for i, r in enumerate(reqs)]

    areas, rest = _fill(mins, prefs, w, budget_m2 - need)      # → preferred
    if rest > 1e-9:
        areas, rest = _fill(areas, tops, w, rest)              # → maximum
    return AreaPlan(kinds=list(kinds), areas=areas, leftover_m2=rest, reqs=reqs)


# ---------------------------------------------------------------------------
# 第三段:Room Shape —— 幾 m² 要長成幾 × 幾
# ---------------------------------------------------------------------------
def depth_for_area(area_m2: float, req: RoomRequirement,
                   *, width_mm: Optional[float] = None) -> float:
    """一間房的目標進深(mm)。

    給了 width_mm 就直接除(寬度已被別的條件定死);沒給就依長寬比偏好挑一個
    「好用的形狀」——18m² 挑出 4.1×4.4 而不是 2.4×7.5。結果夾在 min_depth 與
    max_depth(採光深度)之間。
    """
    area_mm2 = area_m2 * 1_000_000
    if width_mm and width_mm > 0:
        d = area_mm2 / width_mm
    else:
        # 面積 = w×d、長寬比 = d/w = aspect_target → d = sqrt(area × aspect)
        d = (area_mm2 * req.aspect_target) ** 0.5
    lo = req.min_depth
    hi = req.max_depth if req.max_depth is not None else float("inf")
    return max(lo, min(hi, d))


def solve_band(targets: list[float], reqs: list[RoomRequirement],
               *, width_avail: float,
               depth_bounds: tuple[float, float]) -> tuple[float, list[float]]:
    """一條「共用進深」的帶 → (帶進深 mm, 各格寬度 mm)。

    這是本專案格局的骨架形狀:臥室們並排在同一條帶裡、共用一個進深(帶進深
    由樑/柱跨決定,不是每間各自伸縮)。所以「面積 → 形狀」在這裡是個一維問題:
      1. 先由「平均一間的目標面積 + 長寬比偏好」推一個理想帶進深;
      2. 夾進 depth_bounds(樓梯間最小進深、採光深度上限等硬約束);
      3. 帶總寬 = 總面積 / 進深;塞不進 width_avail 就把進深加深補回來
         (面積守住、形狀讓步——使用者定調:結構/牆線優先於形狀偏好);
      4. 各格寬 = 各自面積 / 進深,再守每格的 min_width(不足的補到下限,
         多出來的從最寬的那格扣——扣不動就照比例縮,面積由呼叫端的 ±10%
         容許誤差吸收)。

    回傳的寬度**還沒**做柱網吸附;吸附(以及吸附造成的面積偏移)是呼叫端
    _house_frame 的事,柱網在那裡才是最高優先。
    """
    n = len(targets)
    total = sum(targets)
    if n == 0 or total <= 0:
        return depth_bounds[0], []

    avg_req = reqs[0]
    depth = depth_for_area(total / n, avg_req)
    depth = max(depth_bounds[0], min(depth_bounds[1], depth))

    # 帶總寬塞不下 → 加深帶(面積優先於長寬比偏好),仍受 depth_bounds 上限
    if total * 1_000_000 / depth > width_avail:
        depth = min(depth_bounds[1], total * 1_000_000 / width_avail)

    widths = [t * 1_000_000 / depth for t in targets]
    mins = [r.min_width for r in reqs]

    # 守每格最小寬:補到下限的份,從「離下限最遠」的格子按比例扣回來。
    deficit = sum(max(0.0, mins[i] - widths[i]) for i in range(n))
    if deficit > 0:
        widths = [max(widths[i], mins[i]) for i in range(n)]
        slack = [max(0.0, widths[i] - mins[i]) for i in range(n)]
        pool = sum(slack)
        if pool > 0:
            take = min(deficit, pool)
            widths = [widths[i] - take * slack[i] / pool for i in range(n)]

    # 總寬超過可用寬 → 等比縮(縮完仍守下限;守不住就是真的塞不下,由呼叫端
    # 的最小寬守門報錯)。
    span = sum(widths)
    if span > width_avail and span > 0:
        widths = [w * width_avail / span for w in widths]
    return depth, widths


# ---------------------------------------------------------------------------
# Living Overflow —— 客廳過細長時,切出溢位空間交給 Program Selector
# ---------------------------------------------------------------------------
def compact_width(depth_mm: float, req: RoomRequirement) -> float:
    """一間房在給定進深下,「仍守得住 aspect_max」的最大寬度(= aspect_max × 深)。

    客廳橫跨整條南帶時寬=建築全寬、深只有 3~3.5m → 長寬比 5~7 的長條(benchmark
    首要問題)。這函式給出「客廳最寬能到哪還不算過細」,超過的寬度就是 overflow。
    """
    return req.aspect_max * depth_mm


def select_overflow_program(*, floor: str, bedrooms: int, want_study: bool,
                            has_study: bool, has_family: bool,
                            width_mm: float, depth_mm: float) -> tuple[str, str]:
    """Program Selector —— 決定 Living Overflow 那塊空間要當成什麼房間。

    不固定切成同一種(使用者 2026-07-21):依樓層/已有房間/房數/尺寸決定。回傳
    (kind, name)。中島(kitchen island)不在此——它屬於廚房區的開放餐廚,由
    layout_generator._kitchen_island 處理,跟南帶的客廳溢位在不同帶,幾何上不相鄰。

      floor       "public"(1F 公共層)/ "upper"(2F+ 臥室層)
      has_study   這棟已經有書房(避免重複切一間書房)
      has_family  這棟已經有家庭廳
      width_mm    溢位寬(沿街方向)· depth_mm 帶進深
    """
    study_req = ROOM_PROGRAM["study"]
    # 太窄放不下一張桌椅 → 當儲藏室(收納永遠有需求)。
    if width_mm < study_req.min_width:
        return "storage", "儲藏室"

    long_side, short_side = max(width_mm, depth_mm), max(1.0, min(width_mm, depth_mm))
    compact = long_side / short_side <= 2.3          # 方正到適合當「一間房」

    if floor == "upper":                             # 臥室層:起居/家庭機能為主
        if not has_family:
            return "family", "家庭廳"                 # 臥室層最自然的溢位=家庭廳
        if not has_study and not want_study and compact:
            return "study", "書房"
        return "family", "多功能室"
    # 1F 公共層:書齋/家庭機能
    if not has_study and not want_study and compact:
        return "study", "書房"
    if not has_family:
        return "family", "家庭廳"
    return "family", "多功能室"


# =============================================================================
# PENDING(待確認假設彙整)
# =============================================================================
# 1. ROOM_PROGRAM 的面積三段(min/preferred/max)取自使用者 2026-07-20 指定的
#    主臥 12/18/28、次臥 9/13/20、浴室 4/6/10、客廳 20/35/60;廚房/餐廳/書房/
#    儲藏是照同一個尺度補的經驗值,未經使用者確認。
# 2. PRIORITY_WEIGHT 的權重差距(1.00 → 0.45)是憑感覺定的:太平均會讓主臥的
#    優先序沒意義,太懸殊會退化成「高優先吃到飽」。待實際看圖後調整。
# 3. aspect_target=1.20(略長方形)是住宅房間的常見比例;浴室/廚房放寬到
#    aspect_max=3.0(一字型廚房本來就細長)。未查規範。
# 4. AREA_TOLERANCE=10% 是使用者定調的驗收線,但目前只用於 shortfalls() 報告,
#    沒有做成 validate_spec 的硬檢核(柱網優先,面積超差不該擋出圖)。
# 6. Living Overflow(2026-07-21):客廳過細改由 compact_width 判斷該切多寬、
#    select_overflow_program 決定溢位當什麼房。目前只切「一塊」溢位——寬基地
#    的溢位本身可能還是長形(當家庭廳可接受、長廊式家庭空間是真的),要更細
#    可再切多塊,同 solve_band 的裝箱路線。中島仍歸廚房區(_kitchen_island)。
# 5. 本模組只管「一條帶共用進深」的骨架(solve_band):同一帶的房間必須同深,
#    面積差異只能靠寬度。使用者要的「主臥可以有不同深度」目前**沒有生效**——
#    layout_generator 的 master_bonus 算得出突出量,但兩帶骨架的南帶只有
#    3.0~4.5m 深,扣掉它自己的下限後主臥能突出的餘裕僅 0~1.5m(實測多為
#    0.2m),不值得為此改動門的掛牆邏輯。要讓「每間各自進深」真的有意義,
#    solve_band 得換成二維裝箱(房間不再共用帶),那才是擴充到所有房間的路。
# =============================================================================
