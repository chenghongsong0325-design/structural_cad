"""房間關係圖 → 真的平面圖(混合式 AI 建築師第 2 步:圖 → 矩形)。

room_graph.py 讓 LLM 當「設計師」提出**拓撲**(誰挨著誰,無尺寸)。這一步是
中間那隻手:把拓撲**落實成鋪滿建築的矩形**,再交給既有的 rooms_to_spec 補牆/門/窗
出 DXF。做法正是先前講的「把 LLM 拓撲當軟約束餵進搜尋」:

    切很多種分割(BSP slicing)→ 把房間指派到格子,讓 LLM 要的相鄰關係
    滿足最多 → 挑最好那組 → rooms_to_spec → 真圖

跟 bsp_layout 的搜尋同精神(生多樣、評分、挑最佳),差別在**評分依據換成
「符不符合 LLM 這張關係圖」**——所以格局是 LLM 設計的,不是寫死的模板。

⚠️ 第一版範圍(MVP):單層落實(多層/樓梯對齊是下一步)。樓梯只當一間房放,
   還沒放實際踏階幾何。指派用暴力枚舉(房間數 ≤7);更多房間走貪婪近似。
"""
from __future__ import annotations

import itertools
import math
import random
import sys
from pathlib import Path
from typing import Optional

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from src.design.layout.bsp_layout import rooms_to_spec
# 管道間尺寸與規則版共用一組(單一來源;narrow_house 不在模組層匯入本檔,無循環)。
from src.design.layout.narrow_house import SHAFT_D, SHAFT_W

# 幾何參數(mm)。
MIN_CELL = 1500.0        # 每格最短邊:低於此不切(浴廁塞得下 → 別設太大)
EDGE_MIN = 800.0         # 兩格算「相鄰」要共邊多長(門開得下才算)
SNAP = 1.0

# 評分權重。
W_STRONG = 2.0           # door/open 相鄰有滿足 → 加分(這是硬需求)
W_WEAK = 1.0             # near 相鄰有滿足 → 加分
P_STRONG_MISS = 1.0      # door/open 相鄰沒滿足 → 扣分
# 要採光的房落在**開得了窗的**外牆 → 加分。
# ⚠️ 原本 0.5,比一條相鄰關係(W_STRONG=2.0)輕四倍 —— 但 §40 採光是法規硬
#    要求,擺錯位置整份設計會被擋掉,相鄰關係頂多是「不夠好」。權重要反過來。
DAYLIGHT_BONUS = 2.5
ENTRY_FRONT_BONUS = 1.0  # 大門那間貼南面(臨路)→ 加分
W_SIZE = 1.0             # 房間大小離「合理範圍」的扣分權重
SIZE_CAP = 1.5           # 單間大小扣分上限(免得一間爆掉壓過相鄰)
SIZE_HARD = 1.5          # 超出合理帶這個倍數以上 = 離譜,扣分不再封頂
W_ASPECT = 1.0           # 房間細長(像走廊)的扣分權重
ASPECT_OK = 2.2          # 長寬比在這以內算方正(真實住宅居室多為 1:1~1:2)
ASPECT_CAP = 1.5         # 單間細長扣分上限
# 這些房間本來就細長,不罰:走道是線性動線、豎井/天井是設備井、陽台是帶狀。
SKINNY_OK_KINDS = {"corridor", "pipe_shaft", "patio", "balcony", "storage",
                   "utility"}

# 每種房間的合理面積帶(㎡):落在帶內不扣分,超出越多扣越兇。
# 依台灣住宅常識抓的粗範圍——不是要精準,是要把「浴廁 9㎡、玄關 14㎡」這種腫房間壓下去。
AREA_BAND = {
    "living": (16, 32), "dining": (6, 14), "kitchen": (5, 11),
    # ⚠️ 這張表跟 room_program.ROOM_PROGRAM 是**同一個判準的兩份實作**,改一邊
    #    漏一邊就會出現「AI 版說合格、規則版說過大」。下限兩邊必須一致;上限
    #    這邊可以更嚴(AI 產線靠它觸發重切,寬鬆就切不動),但不得比規則版寬鬆。
    #    有測試釘住(test_room_program.test_兩條產線用同一把尺)。
    "bedroom": (9, 18), "master_bedroom": (12, 24), "bathroom": (2.5, 6),
    "toilet": (1.5, 4), "stair": (3.5, 7), "corridor": (2, 8),
    "patio": (2, 9), "storage": (1.5, 6), "study": (6, 14),
    "elder_room": (9, 18), "garage": (12, 30), "balcony": (2, 8),
    "utility": (2, 8),
    # ⚠️ family 是「溢位」房間(見 _grow_cells_until_no_giant)。沒有 band 的
    #    kind 在 _score 裡**完全不扣分** → 等於可以無限長大,那正是這條產線
    #    「一間房吃掉半層」的幫兇之一。新增 kind 一定要順手補一條 band。
    "family": (10, 24),
}

# 一格切完仍然這麼大(㎡),就當它「太大」,再多切一格 —— 取 living 的上限,
# 因為客廳是這裡面合理上限最大的居室,超過它就沒有房間撐得起這個面積了。
GIANT_CELL_M2 = 32.0
# 一層最多補幾間溢位房。
# ⚠️ 原本設 3,結果 12×12m(每層 144㎡)這種大樓層補滿 3 間仍留著 37.6㎡ 的主臥
#    —— 而 37.6㎡ 依 §40 要 3.92m 寬的窗,超過單扇窗 3.6m 的上限、旁邊又沒空檔
#    開第二扇 → 整份設計被採光擋掉。上限要夠大,`GIANT_CELL_M2` 才真的是上限。
MAX_OVERFLOW_ROOMS = 8

# LLM 房間種類 → rooms_to_spec 認得的 kind(門/窗邏輯用)。
# study 不映射(保留 → 家具引擎才會給書桌/書櫃而非床)。
KIND_MAP = {
    "master_bedroom": "bedroom", "elder_room": "bedroom",
    "toilet": "bathroom", "utility": "storage",
}
# kind → 中文標籤(畫在圖上)。
LABEL = {
    "living": "客廳", "dining": "餐廳", "kitchen": "廚房", "bedroom": "臥室",
    "master_bedroom": "主臥", "bathroom": "浴廁", "toilet": "廁所",
    "stair": "樓梯", "corridor": "走道", "patio": "天井", "storage": "儲藏",
    "study": "書房", "elder_room": "孝親房", "garage": "車庫",
    "balcony": "陽台", "utility": "工作間", "pipe_shaft": "管道間",
}

Rect = tuple[float, float, float, float]      # (x0, y0, x1, y1),mm

# 垂直核(每層同位 → 樓梯/管道間上下對齊)與結構柱。
# 樓梯間進深:牆縫 150 + **起步平台 900**(門開進來先站平地再上階)+ 梯跑 9×260
# + **折返平台 ≥梯段寬**(施工編 §33,核寬 2.6m 時梯段 1175 → 平台也要 1175)。
# 少了起步平台就會「開門即踏step」——門扇掃到階梯、人沒落腳處。
STAIR_DEPTH = 4600.0
# 管道間(機電豎管:給排水/電氣)= SHAFT_W×SHAFT_D(寬 40~80cm、深 40~60cm,
# 使用者 2026-07-29 定調)。**不做天井**:住宅一律不設採光天井,原本天井那塊面積
# 還給房間;管道帶剩下的長度做成壁櫃(房間要鋪滿建築,牆才推得出來)。
COLUMN_SIZE = 400.0      # 結構柱斷面(mm 見方)
COLUMN_SPAN = 6000.0     # 柱距目標(沿進深,6m 經濟跨度;藏外牆內、上下對齊)
COLUMN_MAX_SPAN = 9000.0  # 跨度上限(超過就多一道柱線;使用者定調 6~9m 最經濟)

# AI 設計師模式(混合式收斂管線)實測可穩定落實的建築尺寸範圍。與 narrow_house
# 的規則產生器分開:這個搜尋式引擎比規則版更耐尺寸,而且會**依平面比例自動換骨架**:
#   * 窄長(≤13m 寬)→ 西側垂直核 + 東側房間(透天:東西共壁不開窗)
#   * 寬扁/方形(>13m 寬)→ **中央核骨架**(Phase 3):服務核置中、房間繞一圈,
#     視為獨棟 → 四面都能開窗(否則整棟只剩前後採光會變暗房)
# >9m 寬的窄長骨架自動走多跨柱;中央核骨架的柱軸線直接落在中央核的左右牆。
# 實測 5~34m 寬 × 8~24m 深全部 0 硬錯誤;上限保守設 30m(再大已非單戶住宅尺度)。
AI_MIN_WIDTH = 5000.0    # <5m:西側核(樓梯+管道柱)吃掉寬度,東側住不了
AI_MAX_WIDTH = 30000.0   # 實測 34m 仍乾淨;30m 以上已非單戶住宅尺度
AI_MIN_DEPTH = 9000.0    # 核(樓梯 4.3m)+ 前後房間;實測 9m 起穩定


# ── 1) 把建築範圍切成 n 個矩形(遞迴二分,保證鋪滿、不重疊)──────────────────
def _can_split(c: Rect, min_cell: float) -> bool:
    x0, y0, x1, y1 = c
    return (x1 - x0) >= 2 * min_cell or (y1 - y0) >= 2 * min_cell


#: 一刀切下去,兩邊各至少要佔這個比例 —— 也就是最偏只能切成 3:7。
#
# ⚠️ 原本這裡是 `rng.uniform(x0 + min_cell, x1 - min_cell)`:切點在整段上**均勻
#    亂取**,所以一刀切成 1:9 跟切成 5:5 機率一樣大。格子因此忽大忽小,
#    「一間房吃掉半層」與「4㎡ 的臥室」是同一個原因的一體兩面。
#    真實住宅的隔間不會這樣切:一戶裡最大的居室跟最小的臥室差不多就 2~3 倍。
#    夾在 [0.3, 0.7] 仍然保有隨機變化(同需求會給不同格局),但不會再歪到離譜。
SPLIT_BALANCE = 0.3


def _split_at(lo: float, hi: float, rng: random.Random, min_cell: float) -> float:
    """在 [lo, hi] 裡挑一個切點,盡量靠近中間(見 SPLIT_BALANCE)。

    min_cell 是硬下限,永遠優先——寧可切得偏,也不能切出放不下人的格子。"""
    span = hi - lo
    a = max(lo + min_cell, lo + span * SPLIT_BALANCE)
    b = min(hi - min_cell, hi - span * SPLIT_BALANCE)
    if a > b:                       # 平衡區間被 min_cell 吃掉 → 退回原本的範圍
        a, b = lo + min_cell, hi - min_cell
    return rng.uniform(a, b)


def _split(c: Rect, rng: random.Random, min_cell: float) -> list[Rect]:
    x0, y0, x1, y1 = c
    w, h = x1 - x0, y1 - y0
    dirs = []
    if w >= 2 * min_cell:
        dirs.append("V")
    if h >= 2 * min_cell:
        dirs.append("H")
    # ⚠️ 切哪個方向以前是 50/50 亂選。骨架給的 seed 是**整條進深的長條**
    #    (核以東 7.4m × 12m),一路垂直切下去就會切出 2.6×8.2m 這種長條房間:
    #    長寬比 3.2、外牆只有 2.6m 寬 → 窗開好開滿也不到 §40 要的樓地板 1/8,
    #    整份設計被採光擋掉。改成**偏向切長的那一邊**,格子自然趨近方正。
    #    仍留 1/4 機率切短邊,不然每張圖的切法會長得一模一樣。
    if len(dirs) == 2:
        longer = "V" if w >= h else "H"
        other = "H" if longer == "V" else "V"
        choice = rng.choices([longer, other], weights=[3, 1], k=1)[0]
    else:
        choice = dirs[0]
    if choice == "V":
        cx = _split_at(x0, x1, rng, min_cell)
        return [(x0, y0, cx, y1), (cx, y0, x1, y1)]
    cy = _split_at(y0, y1, rng, min_cell)
    return [(x0, y0, x1, cy), (x0, cy, x1, y1)]


def _partition_from(seed: list[Rect], n: int, rng: random.Random,
                    min_cell: float = MIN_CELL) -> Optional[list[Rect]]:
    """從一組已鋪滿的矩形(seed)繼續二分,切到共 n 個。切不出來回 None。

    seed 可以是 [整個 envelope](一般情形)或 [挖掉樓梯後的 L 形兩塊](對齊多層用)。"""
    cells = list(seed)
    if n < len(cells):
        return None
    cap = GIANT_CELL_M2 * 1e6
    while len(cells) < n:
        splittable = [c for c in cells if _can_split(c, min_cell)]
        if not splittable:
            return None
        area = lambda c: (c[2] - c[0]) * (c[3] - c[1])   # noqa: E731
        # ⚠️ 只要還有「大到沒有房間撐得起」的格子,就**一定**先切最大的那格。
        #    原本這裡一律是依面積加權亂抽 —— 大格比較容易被抽中,但只是「比較
        #    容易」:巨無霸格子連續好幾輪沒被抽中是常事,切完仍然留著一格
        #    55㎡ 的房間(實測 10×11m 的 2F 就是這樣跑出 55㎡ 的走道/浴廁)。
        #    超標的格子不該交給運氣,剩下的才回到加權亂抽保留格局變化。
        giants = [c for c in splittable if area(c) > cap]
        if giants:
            c = max(giants, key=area)
        else:
            c = rng.choices(splittable, weights=[area(c) for c in splittable],
                            k=1)[0]
        cells.remove(c)
        cells.extend(_split(c, rng, min_cell))
    return cells


def partition_envelope(rect: Rect, n: int, rng: random.Random,
                       min_cell: float = MIN_CELL) -> Optional[list[Rect]]:
    """把 rect 切成 n 個矩形(遞迴二分)。切不出來(太小)回 None。"""
    return _partition_from([rect], n, rng, min_cell)


def _n_bays_x(building_w: float) -> int:
    """面寬方向的柱跨數:每跨 ≤9m(經濟上限);9m 以下 1 跨,免多內柱擾動格局。"""
    nx = 1
    while building_w / nx > 9000.0 + 1e-6:
        nx += 1
    return nx


def _bay_xlines(env: Rect) -> list[float]:
    """內部柱軸線的 x 座標(不含左右外牆)。1 跨時為空 → 無內柱。"""
    ex0, _, ex1, _ = env
    nx = _n_bays_x(ex1 - ex0)
    return [ex0 + k * (ex1 - ex0) / nx for k in range(1, nx)]


def _apply_bay_walls(seed: list[Rect], xlines: list[float]) -> list[Rect]:
    """在每條內柱軸線處,把被它穿過的 seed 矩形垂直切開 → 保證該處有一道隔間牆
    (內柱藏其中、上下對齊)。房間之後在各柱跨帶內細分,不跨越柱線。"""
    cells = list(seed)
    for x in xlines:
        out: list[Rect] = []
        for (x0, y0, x1, y1) in cells:
            if x0 + SNAP < x < x1 - SNAP:          # 柱線穿過這格內部 → 切開
                out.append((x0, y0, x, y1))
                out.append((x, y0, x1, y1))
            else:
                out.append((x0, y0, x1, y1))
        cells = out
    return cells


# ── 2) 格子之間的相鄰關係 / 是否貼外框 ──────────────────────────────────────
def _adjacent(a: Rect, b: Rect, thr: float = EDGE_MIN) -> bool:
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    if abs(ax1 - bx0) <= SNAP or abs(bx1 - ax0) <= SNAP:      # 垂直共邊
        return min(ay1, by1) - max(ay0, by0) > thr
    if abs(ay1 - by0) <= SNAP or abs(by1 - ay0) <= SNAP:      # 水平共邊
        return min(ax1, bx1) - max(ax0, bx0) > thr
    return False


def _cell_adjacency(cells: list[Rect]) -> set[frozenset]:
    n = len(cells)
    return {frozenset((i, j))
            for i in range(n) for j in range(i + 1, n)
            if _adjacent(cells[i], cells[j])}


def _on_perimeter(c: Rect, env: Rect, party: bool = False) -> bool:
    """這個格子貼到外牆沒有 —— party=True 時**只認前後(南北)**。

    ⚠️ 共壁透天的東西兩面是跟鄰居共用的牆,開不了窗(`_fix_openings` 會把那側的
    窗刪掉)。以前這裡四面都算,評分就以為「貼到東牆的臥室有採光」,實際上是
    全暗的房間 → §40 擋掉整份設計。共壁與否產線本來就知道(`core_xlines is
    None`),只是沒傳進評分。
    """
    x0, y0, x1, y1 = c
    ex0, ey0, ex1, ey1 = env
    if abs(y0 - ey0) <= SNAP or abs(y1 - ey1) <= SNAP:
        return True
    if party:
        return False
    return abs(x0 - ex0) <= SNAP or abs(x1 - ex1) <= SNAP


def _on_front(c: Rect, env: Rect) -> bool:
    """貼南面(y 最小側 = 臨路面)。"""
    return abs(c[1] - env[1]) <= SNAP


# ── 3) 把房間指派到格子:讓 LLM 要的相鄰滿足最多 ────────────────────────────
def _size_penalty(area_m2: float, kind: str) -> float:
    """房間面積離合理帶多遠(0=帶內;超出用相對誤差)。

    ⚠️ 上限 `SIZE_CAP` 存在的理由是「別讓一間房的面積誤差壓過所有相鄰關係」,
    這對**小幅**超標是對的。但它以前無差別套用到所有超標,結果是

        浴廁上限 6㎡ → 擺 12㎡ 罰 1.0,擺 32㎡ 也只罰 1.5

    「大一倍」跟「大四倍」幾乎一樣痛 → 指派時把最大的格子丟給浴廁完全不吃虧,
    實測就這樣生出 32㎡ 的廁所。所以超過 `SIZE_HARD` 倍之後改成**不封頂**:
    小幅超標仍然溫和(格局有彈性),離譜到不像那個房間就一路罰下去。
    """
    band = AREA_BAND.get(kind)
    if band is None:
        return 0.0
    lo, hi = band
    if area_m2 < lo:
        over = (lo - area_m2) / lo
    elif area_m2 > hi:
        over = (area_m2 - hi) / hi
    else:
        return 0.0
    if over <= SIZE_HARD:
        return min(SIZE_CAP, over)
    return SIZE_CAP + (over - SIZE_HARD)      # 離譜區:不封頂


def _aspect_penalty(c: Rect, kind: str) -> float:
    """房間長寬比離「方正」多遠(0=夠方正)。走道/豎井本來就細長,不罰。

    為什麼要罰:切法只顧面積時會生出 2.9×10.2m 這種「像走廊的餐廳」——面積合格但
    完全不能用。真實住宅居室長寬比大多在 1:1~1:2 之間。"""
    if kind in SKINNY_OK_KINDS:
        return 0.0
    w, h = c[2] - c[0], c[3] - c[1]
    if w <= 0 or h <= 0:
        return ASPECT_CAP
    ar = max(w, h) / min(w, h)
    if ar <= ASPECT_OK:
        return 0.0
    return min(ASPECT_CAP, (ar - ASPECT_OK) / ASPECT_OK)


def _fit_table(rooms: list, cells: list[Rect], env: Rect,
               entry_id: Optional[str], party: bool = False) -> list[list[float]]:
    """table[k][c] = 第 k 間房放進第 c 格「自己」得幾分(採光/朝前/大小/細長)。

    這四項**只跟這一間房和這一格有關**,跟別的房間放哪裡無關 —— 所以同一組格子
    只要算一次(m×m 次),就能餵給全部 m! 種排列。以前是每個排列重算一次,
    7 間房 = 5040 排列 × 7 間 = 35280 次幾何運算,現在 49 次。

    ⚠️ **不是逐位元相同。** 原本這四項是一項一項加進總分,現在先加成一個數再加,
       浮點數的加法順序一變,結果會差 1e-15 上下(實測 5.4 萬個排列裡 2.2 萬個
       有差,最大 7.1e-15)。分數幾乎一樣、但**同分排列的勝負可能對調** ——
       所以選出來的格局可能換一個(同等好的另一個),不是「完全不變」。
       實測換過去的結果反而更好(22×13 的孤柱 1 → 0),但這件事要講清楚,
       不能宣稱成無副作用的重構。
    """
    return [[(DAYLIGHT_BONUS
              if r.get("wants_daylight") and _on_perimeter(c, env, party) else 0.0)
             + (ENTRY_FRONT_BONUS
                if entry_id and r["id"] == entry_id and _on_front(c, env) else 0.0)
             - W_SIZE * _size_penalty((c[2] - c[0]) * (c[3] - c[1]) / 1e6, r["kind"])
             - W_ASPECT * _aspect_penalty(c, r["kind"])
             for c in cells]
            for r in rooms]


def _edge_index(rooms: list, edges: list) -> list[tuple[int, int, bool]]:
    """關係圖的邊改用**房間序號**表示:[(ka, kb, 是不是強連結)]。

    原本每算一個排列都要重建一次 id→格子 的 dict、再逐條邊查字典;序號版只要
    `perm[ka]` 一次索引。兩端有房間不在這層的邊在這裡就先濾掉(原本是每次重濾)。
    """
    idx = {r["id"]: k for k, r in enumerate(rooms)}
    out = []
    for a, b, conn in edges:
        ka, kb = idx.get(a), idx.get(b)
        if ka is None or kb is None:
            continue
        out.append((ka, kb, conn in ("door", "open")))
    return out


def _adj_matrix(cell_adj: set[frozenset], n: int) -> list[list[bool]]:
    """格子相鄰關係改用二維表:`adjm[i][j]`,取代每次配一個 frozenset 去查 set。"""
    m = [[False] * n for _ in range(n)]
    for fs in cell_adj:
        pair = tuple(fs)
        i = pair[0]
        j = pair[1] if len(pair) > 1 else i      # 同一格 = frozenset 只有一個元素
        if i < n and j < n:
            m[i][j] = m[j][i] = True
    return m


def _score(perm: tuple, rooms: list, edges: list, cells: list[Rect],
           cell_adj: set[frozenset], env: Rect, entry_id: Optional[str],
           party: bool = False, table: Optional[list[list[float]]] = None,
           eidx: Optional[list] = None,
           adjm: Optional[list[list[bool]]] = None) -> float:
    """perm[k] = 第 k 個房間放進哪個格子。回總分(越高越符合 LLM 的關係圖)。

    table / eidx / adjm 是預先算好的查表(見 `_fit_table` / `_edge_index` /
    `_adj_matrix`)。這三個東西**同一組格子算一次就能餵給全部 m! 種排列**,
    不給的話這裡自己算(結果一樣,只差在快慢;浮點誤差見 `_fit_table` 的說明)。

    ⚠️ 為什麼要在意快慢:這支被呼叫 300 萬次以上(4 層 × 300 種切法 × 最多
    5040 種排列)。柱線牆改成必做之後格子變多、排列數跟著爆炸,出一張圖從
    15 秒變 72 秒 —— 網站(免費方案有請求逾時)會直接跑不出來。
    """
    if table is None:
        table = _fit_table(rooms, cells, env, entry_id, party)
    if eidx is None:
        eidx = _edge_index(rooms, edges)
    if adjm is None:
        adjm = _adj_matrix(cell_adj, len(cells))
    s = 0.0
    for ka, kb, strong in eidx:
        near = adjm[perm[ka]][perm[kb]]
        if strong:
            s += W_STRONG if near else -P_STRONG_MISS
        elif near:
            s += W_WEAK
    for k in range(len(rooms)):
        s += table[k][perm[k]]
    return s


def _best_perm(rooms, edges, cells, cell_adj, env, entry_id, party=False):
    """房間 ≤7 暴力枚舉最佳指派;更多用貪婪近似。回 (score, perm)。"""
    n = len(rooms)
    table = _fit_table(rooms, cells, env, entry_id, party)
    eidx = _edge_index(rooms, edges)
    adjm = _adj_matrix(cell_adj, len(cells))
    if n <= 7:
        best = None
        for perm in itertools.permutations(range(n)):
            sc = _score(perm, rooms, edges, cells, cell_adj, env, entry_id,
                        party, table, eidx, adjm)
            if best is None or sc > best[0]:
                best = (sc, perm)
        return best
    # 貪婪:連結度高的房間先放,逐一挑當下加分最多的空格。
    order = sorted(range(n), key=lambda k: -sum(
        1 for a, b, _ in edges if rooms[k]["id"] in (a, b)))
    assign = [-1] * n
    used = set()
    for k in order:
        best_cell, best_sc = None, None
        for c in range(n):
            if c in used:
                continue
            assign[k] = c
            sc = _score(tuple(x if x >= 0 else 0 for x in assign),
                        rooms, edges, cells, cell_adj, env, entry_id, party,
                        table, eidx, adjm)
            if best_sc is None or sc > best_sc:
                best_sc, best_cell = sc, c
        assign[k] = best_cell
        used.add(best_cell)
    return (_score(tuple(assign), rooms, edges, cells, cell_adj, env, entry_id,
                   party, table, eidx, adjm),
            tuple(assign))


# ── 4) 落實一層 ─────────────────────────────────────────────────────────────
def realize_floor(rooms: list, edges: list, entry_id: Optional[str],
                  build_rect: Rect, site_w: float, site_d: float,
                  setback: float, rng: Optional[random.Random] = None,
                  tries: int = 300, furnish: bool = True):
    """一層的房間清單 + 相鄰邊 → (spec, 滿足邊數, 總邊數, 分數)。

    切 tries 種分割,各自求最佳指派,挑全域最佳,再交 rooms_to_spec 出 spec,
    最後(furnish=True)用 Phase 6 家具引擎就地擺家具。
    """
    if rng is None:
        rng = random.Random(0)
    n = len(rooms)
    if n == 0:
        raise ValueError("這一層沒有房間")

    best = None                        # (score, cells, perm)
    for _ in range(tries):
        cells = partition_envelope(build_rect, n, rng)
        if cells is None:
            continue
        cell_adj = _cell_adjacency(cells)
        sc, perm = _best_perm(rooms, edges, cells, cell_adj, env=build_rect,
                              entry_id=entry_id)
        if best is None or sc > best[0]:
            best = (sc, cells, perm)
    if best is None:
        raise ValueError(f"建築範圍太小,切不出 {n} 間房")

    score, cells, perm = best
    cell_adj = _cell_adjacency(cells)
    pos = {rooms[k]["id"]: perm[k] for k in range(n)}
    satisfied = sum(1 for a, b, _ in edges
                    if a in pos and b in pos
                    and frozenset((pos[a], pos[b])) in cell_adj)

    named = []
    for k, r in enumerate(rooms):
        x0, y0, x1, y1 = cells[perm[k]]
        kind = KIND_MAP.get(r["kind"], r["kind"])
        label = LABEL.get(r["kind"], r["kind"])
        named.append((kind, label, [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]))

    spec = rooms_to_spec(named, build_rect, site_w, site_d, setback)
    if furnish:
        from src.design.layout.auto_furnish import furnish_spec
        furnish_spec(spec)                     # Phase 6:依房間用途擺家具
    return spec, satisfied, len(edges), score


# ── 走道:一律不做(使用者 2026-08-19 定調)────────────────────────────────
# 「走道這麼大佔整個建築的一半根本不合理」「一般建築好像也沒有走道」。
# 這跟使用者 2026-07-12 就定調、兩帶式產線早就照做的原則是同一條:
#   走道 = 多房間共用的動線才設;房間少的小宅,動線融入客廳。
# AI 這條產線一直沒套用,所以 LLM 想放就放。
#
# ⚠️ 走道跟天井/儲藏室**不一樣,不能直接刪**。天井、儲藏室是末端房間,刪掉不影響
#    別人;走道是**動線樞紐**——臥室、浴廁的門全都開在它上面。直接刪 → 那些房間
#    沒地方開門 → `room_no_door` / `circulation_blocked` → 整層無限重生。
#
# 所以這裡做的是**收縮(contract)**,不是刪除:把走道併進它「開放連通」的那間
# (通常 1F 是客廳、樓上是樓梯間),原本開在走道上的門改開在那一間上。實體上就是
# 真實小宅的做法——臥室門直接開向客廳或樓梯平台,不另闢走廊。
_CONN_RANK = {"open": 3, "door": 2, "near": 1}


def _corridor_host(cid: str, rooms: list, edges: list) -> Optional[str]:
    """走道要併進誰。挑不到(孤立的走道)回 None → 那間就真的刪掉。

    優先序刻意這樣排:
      1. **開放連通的客廳/餐廳** —— 使用者定調的「動線融入客廳」。
      2. 其他開放連通的房間(樓上通常是樓梯間 = 樓梯平台當緩衝)。
      3. 樓梯間(就算只是 door/near 相連)。
      4. 隨便一個鄰居,總比讓門無處可去好。
    """
    kind = {r["id"]: r.get("kind") for r in rooms}
    nb = []                                    # [(對方 id, 連通方式)]
    for a, b, conn in edges:
        if a == cid and b != cid:
            nb.append((b, conn))
        elif b == cid and a != cid:
            nb.append((a, conn))
    if not nb:
        return None
    for want_open, want_kinds in ((True, ("living", "dining")),
                                  (True, None),
                                  (False, ("stair",)),
                                  (False, None)):
        for other, conn in nb:
            if want_open and conn != "open":
                continue
            if want_kinds and kind.get(other) not in want_kinds:
                continue
            return other
    return None


def _overflow_rooms(n: int, free: list, floor_label: str,
                    cell_w: float, cell_d: float) -> list:
    """要多切 n 格,就補 n 間房來住它們。回新增的 room dict。

    用途不是寫死的,交給 `room_program.select_overflow_program` 決定
    ——兩帶式的北帶溢位用的是同一支,兩條產線切出來的「多出來那間」才會是
    同一套邏輯(1F 偏書房、樓上偏家庭廳,已經有了就換多功能室)。
    """
    from src.design.room_program import select_overflow_program

    kinds = [r.get("kind") for r in free]
    bedrooms = sum(1 for k in kinds if k in ("bedroom", "master_bedroom"))
    has_study = "study" in kinds
    has_family = "family" in kinds
    out = []
    for i in range(n):
        prog = select_overflow_program(
            floor="public" if floor_label == "1F" else "upper",
            bedrooms=bedrooms, want_study=False,
            has_study=has_study, has_family=has_family,
            width_mm=cell_w, depth_mm=cell_d)
        # ⚠️ 選配器排完用途會回 None(NG08「用不到的房間」:沒用途就不要切一間
        #    出來)。**但這條產線退不掉** —— 格子是 BSP 先切好的,每一格一定要
        #    有房間住,否則那塊樓地板誰也不屬於。
        #
        # ⚠️ **走錯過的路(已退回,不要再試一次)**:這裡改成 ("storage","儲藏室")
        #    看起來比「多功能室」誠實,但儲藏室的面積需求小很多(max 8㎡ vs
        #    家庭廳無上限)→ BSP 換一種切法 → 牆線跟著變 →
        #    `test_柱網同時滿足藏牆內與等距[22000-13000]` 的 2F 冒出 **2 根孤柱**
        #    (藏牆率 100% → 83%)。加分項不得讓原本好好的東西壞掉。
        #
        # 所以 NG08 目前**只在規則版兩帶式修掉**(那條退得掉:牆還沒立,面積
        # 併給南帶的客廳/家庭廳就好)。AI 關係圖版仍會生出「多功能室」——
        # 這是已知缺口,真正的解在更上游:**別把格子切那麼多**
        # (`_cells_without_a_giant` 的停止條件),不是在這裡換個房名。
        kind, name = prog if prog is not None else ("family", "多功能室")
        # ⚠️ wants_daylight=False 是刻意的:溢位房間是「多出來的坪數」,不該跟
        #    LLM 原本設計的臥室/客廳**搶外牆**。實測沒設 False 時,3F 的主臥被
        #    擠到內間 → §40 採光不足 → 整份設計被擋掉。
        out.append({"id": f"overflow{i}_{floor_label}", "kind": kind,
                    "label": name, "wants_daylight": False,
                    "overflow": True})
        has_study = has_study or kind == "study"
        has_family = has_family or kind == "family"
    return out


def _cells_without_a_giant(seed: list, m: int, rng: random.Random):
    """切格子 —— 切到「格子數 = 房間數」**還不夠**,要切到沒有巨無霸格子。

    ⚠️ 這是這條產線「一間房吃掉半層」的根。原本的停止條件只看數量:

        建築 10×11m(每層 110㎡)、關係圖只有 5 間房
          → 切成 5 格就收手 → [45.8, 19.0, 16.6, 8.5, 6.6] ㎡
          → 那格 45.8㎡ 指給誰誰就變成 55㎡ 的走道 / 廁所

    面積表 `AREA_BAND` 當時只在**事後評分**用,而且單間扣分有上限
    (`SIZE_CAP`)—— 大 10 倍和大 2 倍扣一樣多,搜尋根本不在乎。

    兩帶式產線 2026-08-13 踩過一模一樣的坑(`_west_zone_cut` 只切一刀 →
    「讓兩間一起無限長大」),解法是切到每間都在合理範圍。這裡照做。

    回 (cells, 要補幾間房)。切不動就回目前最好的,**絕不比原本差**。
    """
    # ⚠️ 房間數可能**少於** seed 的塊數(骨架把核以外切成兩塊,但這層只剩 1 間房
    #    ——拿掉走道之後就會發生)。`_partition_from` 遇到 n < 塊數會直接回 None,
    #    整層生不出來。這種情形要補房間去住那些塊,不是放棄。
    extra = max(0, len(seed) - m)
    cells = _partition_from(seed, m + extra, rng)
    if cells is None:
        return None, 0
    while extra < MAX_OVERFLOW_ROOMS:
        if max((c[2] - c[0]) * (c[3] - c[1]) for c in cells) <= GIANT_CELL_M2 * 1e6:
            break

        more = _partition_from(seed, m + extra + 1, rng)
        if more is None:            # 再切就低於 MIN_CELL → 收手,留現有的
            break
        cells, extra = more, extra + 1
    return cells, extra


def drop_corridors(rooms: list, edges: list, entry_id: Optional[str]):
    """把走道從一層的關係圖裡收掉,回 (rooms, edges, entry_id)。

    沒有走道時原樣回傳(同一個 list 物件),既有樓層的輸出逐位元不變、不回歸。
    """
    corridors = [r["id"] for r in rooms if r.get("kind") == "corridor"]
    if not corridors:
        return rooms, edges, entry_id

    host = {}
    for cid in corridors:
        h = _corridor_host(cid, rooms, edges)
        # 走道併走道的話要一路追到非走道那間(LLM 偶爾會串兩段動線)。
        seen = {cid}
        while h in host and h not in seen:
            seen.add(h)
            h = host[h]
        host[cid] = h

    def resolve(x):
        seen = set()
        while x in host and x not in seen:
            seen.add(x)
            x = host[x]
        return x

    merged: dict = {}
    for a, b, conn in edges:
        na, nb = resolve(a), resolve(b)
        if na is None or nb is None or na == nb:
            continue                     # 走道自己那條邊、或併進同一間 → 沒了
        key = (na, nb) if na < nb else (nb, na)
        old = merged.get(key)
        # 同一對房間收到兩條邊時留「比較開放」的那條(open > door > near):
        # 併進客廳的走道口本來就是開放的,不該退化成一扇門。
        if old is None or _CONN_RANK.get(conn, 0) > _CONN_RANK.get(old, 0):
            merged[key] = conn
    new_edges = [(a, b, c) for (a, b), c in merged.items()]

    new_rooms = [r for r in rooms if r["id"] not in host]
    new_entry = resolve(entry_id) if entry_id is not None else None
    # 大門本來開在走道(玄關)上 → 改開在併進去的那間(客廳)。併不到就交給
    # 落實端自己找外牆開大門,總之不能留一個指向不存在房間的 entry。
    if new_entry is not None and new_entry not in {r["id"] for r in new_rooms}:
        new_entry = None
    return new_rooms, new_edges, new_entry


def realize_graph_floor(graph: dict, floor: int, building_w: float,
                        building_d: float, setback: float = 2000.0,
                        rng: Optional[random.Random] = None, tries: int = 300,
                        furnish: bool = True):
    """LLM 關係圖的某一層 → 真 spec。envelope 尺寸另外給(拓撲不含尺寸)。"""
    rooms = [r for r in graph["rooms"] if r["floor"] == floor]
    if not rooms:
        raise ValueError(f"關係圖裡沒有第 {floor} 層")
    ids = {r["id"] for r in rooms}
    edges = [(e["a"], e["b"], e["connection"]) for e in graph["adjacencies"]
             if e["a"] in ids and e["b"] in ids]        # 只留同層的邊
    entry = graph.get("entry") if graph.get("entry") in ids else None
    rooms, edges, entry = drop_corridors(rooms, edges, entry)   # 走道一律不做

    build_rect = (setback, setback, setback + building_w, setback + building_d)
    site_w, site_d = building_w + 2 * setback, building_d + 2 * setback
    return realize_floor(rooms, edges, entry, build_rect, site_w, site_d,
                         setback, rng=rng, tries=tries, furnish=furnish)


# ── 多層 + 對齊垂直核(樓梯+管道間+天井)+ 結構柱 ─────────────────────────────
def _core_width(building_w: float) -> float:
    """西側垂直核面寬(mm):容 U 形折返梯兩段。"""
    return min(max(building_w * 0.34, 2000.0), 2600.0)


COURTYARD_MIN_W = 13000.0    # 面寬超過這個改用中央核骨架(長條骨架會生出細長房間)
COURT_STAIR_W = 2600.0       # 中央核裡樓梯間的面寬(容 U 形折返梯)
RING_MIN = 2800.0            # 中央核四周「房間圈」每一帶的最小厚度(擺得下居室)


def bay_lines(x0: float, length: float,
              max_span: float = COLUMN_MAX_SPAN) -> tuple[list[float], float]:
    """把一段長度等分成柱跨,回 (內部柱線座標, 跨度)。

    規則很簡單,就是使用者 2026-07-12 定調的那條:**規則等距、跨度 6~9m 最經濟**。
    先用上限求最少跨數,再看能不能更少(跨數越少柱越少,只要不超過上限)。

    ⚠️ 這支是「柱線該長什麼樣」的**單一出處**。以前三個骨架各算各的:
        AI 西側核  建築寬等分            ← 唯一算對的
        AI 中央核  直接拿服務核的左右牆  ← 30m 寬會生出 [13.3, 3.4, 13.3]
        兩帶式     拿帶的分界線          ← 全部 <6m
    柱線變成「別的東西決定完剩下的副產品」,而不是自己被設計出來的,這就是使用者
    2026-08-19 說「柱子的位子不合邏輯」的根。
    """
    n = max(1, math.ceil(length / max_span - 1e-9))
    while n > 1 and length / (n - 1) <= max_span + 1e-9:
        n -= 1
    span = length / n
    return [x0 + k * span for k in range(1, n)], span


def _core_courtyard(env: Rect):
    """中央核骨架(Phase 3):樓梯+管道柱擺在平面**中央**,房間繞成一圈。

    寬扁/方形基地不能用長條骨架(西側核+東側長條)——那會把房間拉成 3×10m 的
    細長條。作法是**服務核置中**,四周一圈房間各自貼一段外牆採光,誰都不會變暗房
    (原本核裡還有一座中庭,依使用者 2026-07-29 定調拿掉——住宅不設天井;四帶各自
    臨外牆,採光本來就不靠中庭)。

    回 (stair_rect, shaft_rect, patio_rect, seed, core_w, xlines);
    seed = 中央核以外鋪滿的四塊(南帶/北帶/西帶/東帶),交給房間去細分。
    xlines = **建築寬等分**的柱線(見 bay_lines);核吸附到其中一條,所以核的
             西牆也長在柱線上。以前這裡回的是核的左右邊,跨度因此完全被核寬綁架。
    放不下(基地太小)回 None,由呼叫端退回長條骨架。"""
    ex0, ey0, ex1, ey1 = env
    W, D = ex1 - ex0, ey1 - ey0

    core_w = COURT_STAIR_W + SHAFT_W
    core_d = STAIR_DEPTH
    if W - core_w < 2 * RING_MIN or D - core_d < 2 * RING_MIN:
        return None                                  # 兩側/前後圈不夠厚 → 不適用

    # 柱線:建築寬等分(見 bay_lines)。**不再拿服務核的左右牆當柱線** ——
    # 那會讓跨度變成「核多寬就多寬」,30m 的房子跑出 [13.3, 3.4, 13.3](13m 跨的
    # 樑做不出來)。柱線先定,核再去配合柱線。
    xlines, _span = bay_lines(ex0, W)

    # 核水平置中,但**吸附到最近的柱線**:這樣核的西牆就長在柱線上,那一排柱直接
    # 藏在核的牆裡。吸附後若貼太近外牆就放棄吸附,退回純置中(寧可柱靠 bay 牆,
    # 也不能讓核擠掉外圈房間)。
    cx0 = ex0 + (W - core_w) / 2.0                   # 水平置中
    for line in sorted(xlines, key=lambda v: abs(v - cx0)):
        if ex0 + RING_MIN <= line <= ex1 - core_w - RING_MIN:
            cx0 = line
            break
    cy0 = ey0 + max(RING_MIN, (D - core_d) * 0.45)   # 前段(南)略大於後段
    cy0 = min(cy0, ey1 - core_d - RING_MIN)
    cx1, cy1 = cx0 + core_w, cy0 + core_d

    stair_rect = (cx0, cy0, cx0 + COURT_STAIR_W, cy1)
    xs = cx0 + COURT_STAIR_W                         # 管道柱西緣
    shaft_rect = (xs, cy0, cx1, cy0 + SHAFT_D)
    closet_rect = (xs, cy0 + SHAFT_D, cx1, cy1)
    seed = [(ex0, ey0, ex1, cy0),                    # 南帶(臨路,含玄關/客廳)
            (ex0, cy1, ex1, ey1),                    # 北帶
            (ex0, cy0, cx0, cy1),                    # 西帶
            (cx1, cy0, ex1, cy1)]                    # 東帶
    return stair_rect, shaft_rect, closet_rect, seed, core_w, xlines


def _choose_core(env: Rect, min_free_rooms: int = 99):
    """依平面比例選骨架:寬扁/方形走中央核,窄長走西側核(既有,不動)。

    min_free_rooms:各樓層「核以外的房間數」最少的那層。中央核的房間圈是四塊帶,
    某層房間少於四間就填不滿(切不出來)→ 退回西側核。骨架必須整棟一致(樓梯/柱
    要上下對齊),所以用**最少的那層**決定。"""
    if (env[2] - env[0]) > COURTYARD_MIN_W and min_free_rooms >= 4:
        court = _core_courtyard(env)
        if court is not None:
            return court
    return _core_column(env)


def _core_column(env: Rect):
    """西側垂直核(每層同位):樓梯 + 貼著它的管道柱(管道間 + 上方收納)。

    回 (stair_rect, shaft_rect, closet_rect, seed, cw)。seed = 核以外鋪滿的兩塊
    矩形(東側整條 + 核北側),給 LLM 房間去切。核件每層同位 → 樓梯/機電豎管天生
    上下對齊(符合柱網原則,也是真實透天的固定服務核)。"""
    ex0, ey0, ex1, ey1 = env
    cw = _core_width(ex1 - ex0)
    y1 = ey0 + STAIR_DEPTH
    y2 = y1 + SHAFT_D
    stair_rect = (ex0, ey0, ex0 + cw, y1)
    # ⚠️ 管道間排在樓梯**北側**、不擋樓梯的東牆:樓梯只有東側能開門(南/西是外牆、
    #    北是折返平台),東牆一旦被服務格佔住,整層就進不了樓梯 → 室內斷開。
    #    同一條 500 深的帶子:西端 800 是管道間(真實尺寸),其餘做成壁櫃填滿。
    shaft_rect = (ex0, y1, ex0 + SHAFT_W, y2)
    closet_rect = (ex0 + SHAFT_W, y1, ex0 + cw, y2)
    seed = [(ex0 + cw, ey0, ex1, ey1), (ex0, y2, ex0 + cw, ey1)]
    return stair_rect, shaft_rect, closet_rect, seed, cw, None   # None=柱距均分


def _assign_core(free, fixed, allcells, cell_adj, env, entry_id, edges,
                 party=False):
    """free 房間指派到剩餘格;fixed=[(room, 固定格index)] 為核件(樓梯/管道間/天井)。

    核件位置固定,但仍參與相鄰評分(讓走道自然貼樓梯、廚房貼天井)。回 ((score, perm), rooms_full)。"""
    rooms_full = free + [r for r, _ in fixed]
    fixed_idx = tuple(i for _, i in fixed)
    m = len(free)
    # 單間房的得分先算成表(m! 種排列共用同一張,見 _fit_table)。
    table = _fit_table(rooms_full, allcells, env, entry_id, party)
    eidx = _edge_index(rooms_full, edges)
    adjm = _adj_matrix(cell_adj, len(allcells))
    if m <= 7:
        best = None
        for perm in itertools.permutations(range(m)):
            pf = perm + fixed_idx
            sc = _score(pf, rooms_full, edges, allcells, cell_adj, env, entry_id,
                        party, table, eidx, adjm)
            if best is None or sc > best[0]:
                best = (sc, pf)
        return best, rooms_full
    order = sorted(range(m), key=lambda k: -sum(          # 房間多 → 貪婪
        1 for a, b, _ in edges if free[k]["id"] in (a, b)))
    assign, used = [-1] * m, set()
    for k in order:
        bc, bs = None, None
        for c in range(m):
            if c in used:
                continue
            assign[k] = c
            pf = tuple(x if x >= 0 else 0 for x in assign) + fixed_idx
            sc = _score(pf, rooms_full, edges, allcells, cell_adj, env, entry_id,
                        party, table, eidx, adjm)
            if bs is None or sc > bs:
                bs, bc = sc, c
        assign[k] = bc
        used.add(bc)
    pf = tuple(assign) + fixed_idx
    return (_score(pf, rooms_full, edges, allcells, cell_adj, env, entry_id,
                   party, table, eidx, adjm),
            pf), rooms_full


DECLUTTER_SKIP = ("patio", "parking", "garage", "stair", "balcony")


def _nudge_into_room(spec, room, fx, step: float = 100.0, span: float = 600.0) -> bool:
    """把擋路的家具沿房間的兩軸小幅平移,找一個讓這間房走得通的位置。

    只挪不轉、也不出房間;挪完仍走不通就回 False(由呼叫端決定要不要移除)。"""
    from src.design.layout.room_circulation import analyze_room

    move = getattr(fx, "insert", None)
    if move is None:                                # 流理台(Counter)不挪:它必須貼牆
        return False
    ox, oy = move
    offsets = [(dx, dy)
               for r in range(1, int(span / step) + 1)
               for dx, dy in ((r * step, 0), (-r * step, 0),
                              (0, r * step), (0, -r * step))]
    from shapely.geometry import Polygon

    from src.design.collision.geometry import fixture_obstacles
    rpoly = Polygon(room.points)
    for dx, dy in offsets:
        fx.insert = (ox + dx, oy + dy)
        here = next((o for o in fixture_obstacles(spec) if o.ref is fx), None)
        if here is None or not rpoly.contains(here.poly.buffer(-1.0)):
            continue                                # 挪出房間了,不算
        if _blocks_a_door(spec, here.poly):
            continue                            # 挪去擋到門的迴轉,等於沒解決
        if _hits_wall(spec, here.poly):
            continue                            # 挪進牆裡 = 畫出來穿牆
        if any(o.ref is not fx and o.poly.intersection(here.poly).area > 1000.0
               for o in fixture_obstacles(spec)):
            continue                            # 撞到別的家具,也不算解決
        if analyze_room(spec, room).ok:
            return True
    fx.insert = (ox, oy)
    return False


def _hits_wall(spec, poly) -> bool:
    """家具在新位置會不會嵌進牆體(房間多邊形是牆**中心線**,不能只看在不在房內)。"""
    from src.design.layout.fixture_fix import OVERLAP_TOL, _wall_union
    bodies = _wall_union(spec)
    return bodies is not None and poly.intersection(bodies).area > OVERLAP_TOL


def _blocks_a_door(spec, poly) -> bool:
    """這件家具在新位置會不會擋住任何一扇門的迴轉空間。

    ⚠️ 用的是 **validate_spec 的同一塊方形**(門寬 × 門寬,開啟側),不是門扇掃過的
    扇形——判準要跟最後把關的那道一致,否則這裡放行、validate 照樣擋圖。"""
    from shapely.geometry import Polygon as _P
    for dp in getattr(spec, "doors", None) or []:
        try:
            w = spec.walls[dp.wall_index]
            op = w.openings[dp.opening_index]
        except (IndexError, AttributeError):
            continue
        if op.kind != "door":
            continue
        cx, cy = w.point_at(op.position)
        ux, uy = w.unit_vector
        nx, ny = w.normal_vector
        sgn = 1.0 if getattr(dp.door, "swing", "out") == "out" else -1.0
        h, e = op.width / 2.0, op.width
        square = _P([
            (cx - ux * h, cy - uy * h),
            (cx + ux * h, cy + uy * h),
            (cx + ux * h + sgn * nx * e, cy + uy * h + sgn * ny * e),
            (cx - ux * h + sgn * nx * e, cy - uy * h + sgn * ny * e),
        ])
        if square.intersection(poly).area > 100.0:
            return True
    return False


def _declutter_for_circulation(spec, max_removals: int = 20,
                               allow_remove: bool = True) -> int:
    """furnish 後修復動線:哪間房被家具擋住,移掉該房最占空間的家具再驗,直到走得通。

    擋路的通常就是那件過大的(穿越型餐廳的餐桌、擠爆浴室的浴缸);寧可少一件也要
    走得通(空房間至少走得進去,家具擺不下是房間太小的設計問題,由 critique 回報)。

    ⚠️ **逐房間物件檢查**,不靠名稱比對:同一層常有兩間都叫「臥室」,用名稱找會永遠
    修到第一間、真正被擋的那間一直沒修好(這正是小坪數樓層動線修不好的原因)。

    回移除件數。"""
    from shapely.geometry import Polygon

    from src.design.collision.geometry import fixture_obstacles
    from src.design.layout.room_circulation import analyze_room
    from src.design.semantic.room_semantic import canonical_room

    removed = 0
    for _ in range(max_removals):
        did = False
        for room in spec.rooms:                      # 直接走房間物件(名稱可能重複)
            if (canonical_room(room.kind) in DECLUTTER_SKIP
                    or room.kind in DECLUTTER_SKIP):
                continue
            if analyze_room(spec, room).ok:
                continue
            rpoly = Polygon(room.points)             # 這間被擋 → 移掉它最大件家具
            cands = [o for o in fixture_obstacles(spec)
                     if o.poly.intersection(rpoly).area > 0.0]
            if not cands:
                continue                             # 沒家具還不通 = 房間本身太小
            victim = max(cands, key=lambda o: o.poly.area)
            # 先試「挪一下」再談移除:餐桌常常只是偏了 10~20cm,導致一側的
            # 通道剩 50cm(差 10cm 就走不過去)。挪得動就別丟——真實圖也是這樣。
            if _nudge_into_room(spec, room, victim.ref):
                did = True
                continue
            if not allow_remove:                 # 只挪不丟(收尾第二輪用)
                continue
            try:
                spec.fixtures.remove(victim.ref)
            except ValueError:
                continue
            removed += 1
            did = True
        if not did:
            break
    return removed


def _realize_floor_core(rooms, edges, entry_id, env, core, rng, tries,
                        furnish, floor_label, stair_label, bay_walls=True):
    """落實一層:核件(樓梯/管道間/天井)釘同位,LLM 房間切核外空間;加柱、收尾門洞、家具。

    bay_walls:多跨時是否在內柱軸線預切隔間牆(藏內柱)。若某層因此動線卡死,
    上層迴圈會關掉它重試(寧可浮柱也要動線通)。"""
    stair_rect, shaft_rect, closet_rect, seed, _cw, core_xlines = core
    # LLM 若提了樓梯,用它的 id 讓相鄰邊還能評分;沒提就補一個(不影響對齊)。
    # 天井一律不做(使用者 2026-07-29 定調):LLM 提了也丟掉,不佔核、不佔房間數。
    # 儲藏室同樣不做(使用者 2026-07-30 定調:住宅不需要獨立儲藏室)——LLM 提了
    # 也丟掉,那些坪數留給居室,不會憑空多一間沒人用的小房。
    g_stair = next((r for r in rooms if r["kind"] == "stair"), None)
    free = [r for r in rooms
            if r["kind"] not in ("stair", "patio", "storage", "utility")]
    stair_room = g_stair or {"id": f"stair_{floor_label}", "kind": "stair",
                             "wants_daylight": False}
    # 管道帶剩下的 60cm 深長條是**壁櫃**(開門拿東西的櫥櫃),不是儲藏室;
    # kind 仍用 storage(動線/細長檢查的豁免都掛在這個 kind 上),只改圖上的字。
    closet_room = {"id": f"closet_{floor_label}", "kind": "storage",
                   "label": "壁櫃", "wants_daylight": False}
    shaft_room = {"id": f"shaft_{floor_label}", "kind": "pipe_shaft",
                  "wants_daylight": False}
    m = len(free)

    # 多跨:在柱線處預切 seed → 每層同位置都有隔間牆,柱藏其中、上下對齊。
    #
    # ⚠️ 以前的條件是「房間數 ≥ 切完的格數才切」,不夠就整個放棄。柱線改成建築寬
    #    等分之後(2026-08-19)柱線變多,這個條件幾乎永遠不成立 → 牆不長在柱線上
    #    → 柱浮在房間中央(實測藏牆率 100% 掉到 91.7%、每層冒出 1 根孤柱)。
    #    **柱線牆不能是「有空才做」的加分項** —— 它是柱藏牆內的唯一保證。
    #    房間不夠就補房間(`_cells_without_a_giant` 會依 seed 塊數補溢位房),
    #    不要反過來放棄柱線牆。
    xlines = ((core_xlines or _bay_xlines(env)) if bay_walls else [])
    if xlines:
        banded = _apply_bay_walls(seed, xlines)
        if len(banded) - m <= MAX_OVERFLOW_ROOMS:      # 補得完就切
            seed = banded

    # 一格有多大才算合理:拿核外面積除以房間數當基準,量溢位房的尺寸用。
    seed_area = sum((c[2] - c[0]) * (c[3] - c[1]) for c in seed)
    side = math.sqrt(max(seed_area / max(1, m), 1.0))

    best = None
    for _ in range(tries):
        cells, extra = _cells_without_a_giant(seed, m, rng)
        if cells is None:
            continue
        # 多切的格子要有房間住(不然指派不完、格子變成沒人認領的空白)。
        this_free = free + (_overflow_rooms(extra, free, floor_label,
                                            side, side) if extra else [])
        mm = len(this_free)
        allcells = cells + [stair_rect, shaft_rect, closet_rect]
        cadj = _cell_adjacency(allcells)
        fixed = [(stair_room, mm), (shaft_room, mm + 1), (closet_room, mm + 2)]
        # party:西側核骨架=共壁透天,東西外牆開不了窗 → 採光評分只認南北面。
        (sc, pf), rooms_full = _assign_core(this_free, fixed, allcells, cadj,
                                            env, entry_id, edges,
                                            party=core_xlines is None)
        if best is None or sc > best[0]:
            best = (sc, cells, pf, rooms_full)
    if best is None:
        raise ValueError(f"{floor_label}:核外空間太小,擺不下 {m} 間房")

    score, cells, pf, rooms_full = best
    allcells = cells + [stair_rect, shaft_rect, closet_rect]
    cadj = _cell_adjacency(allcells)
    pos = {rooms_full[k]["id"]: pf[k] for k in range(len(rooms_full))}
    satisfied = sum(1 for a, b, _ in edges if a in pos and b in pos
                    and frozenset((pos[a], pos[b])) in cadj)

    named = []
    for k, r in enumerate(rooms_full):
        x0, y0, x1, y1 = allcells[pf[k]]
        kind = r["kind"]
        label = r.get("label") or LABEL.get(kind, kind)
        # ⚠️ 溢位的書房若落到**沒有外牆**的格子,要降級成家庭廳:
        #    `code_check.HABITABLE_KINDS` 把書房算「居室」,居室一定要有 §40 的
        #    採光開口;家庭廳不在名單裡(它是起居空間的延伸,不是獨立居室)。
        #    這不是規避法規——是「這塊沒有窗的空間本來就不該叫書房」。
        if (r.get("overflow") and kind == "study"
                and not _on_perimeter(allcells[pf[k]], env)):
            kind, label = "family", "多功能室"
        named.append((KIND_MAP.get(kind, kind), label,
                      [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]))

    setback = env[0]
    site_w = (env[2] - env[0]) + 2 * setback
    site_d = (env[3] - env[1]) + 2 * setback
    spec = rooms_to_spec(named, env, site_w, site_d, setback)

    # 收尾門洞(重用 narrow_house):去重複門/界牆窗/天井門、浴室只留 1 門、1F 補前門。
    from src.design.layout.narrow_house import (
        _add_stair_guard_walls, _door_kinds, _ensure_floor_connected,
        _ensure_room_doors, _ensure_room_windows, _fix_openings,
        _remove_openings, _stair, shift_openings_off_columns,
    )
    # 結構柱:面寬與進深各每 ~6m 一道軸線,柱放外框軸網交點(藏牆內、每層同位=上下
    # 對齊)。面寬 >9m 時分多跨,內柱落在上面預切的柱線牆內,不孤立在房間中央。
    #
    # ⚠️ **這段一定要在開口收尾之前**(使用者 2026-08-19:「柱子怎麼還能放在
    #    窗戶裡?」)。躲柱的機制(`_column_blocks`)一直都在,但柱以前是在
    #    `_ensure_room_windows` **之後**才掛上 spec —— 開窗當下 spec 上沒有柱,
    #    查到的是空的,窗就開在柱上了。門沒事是因為後面還有一次 `repair_doors`,
    #    窗沒有對應的第二次。
    #    柱位只跟建築外框(env)與核的軸線(core_xlines)有關,兩個在這裡都已經
    #    知道,所以提前定案沒有任何依賴問題。
    W, D = env[2] - env[0], env[3] - env[1]
    spec.grid_origin = (env[0], env[1])
    if core_xlines:                             # 中央核骨架:柱線已由 bay_lines 等分
        xs = [env[0], *core_xlines, env[2]]
        spec.x_spacings = [b - a for a, b in zip(xs, xs[1:])]
    else:
        nx = _n_bays_x(W)
        spec.x_spacings = [W / nx] * nx
    # ⚠️ 進深方向也要走 bay_lines,不能用「除以 6m 四捨五入」。
    #    舊公式 round(D / 6000) 會把 9.0m 深切成 2 跨 × 4.5m —— 但 9.0m 一跨就
    #    在經濟上限內,多切那一排柱是純浪費,而且 4.5m 掉出 6~9m 區間。
    #    只修面寬、不修進深 = 只做半套:30 案隨機掃描裡有 8 案柱網被判不合格,
    #    全部是這個原因(X 方向都是 1.00 倍等分,Y 方向卻不是)。
    yl, yspan = bay_lines(env[1], D)
    spec.y_spacings = [yspan] * (len(yl) + 1)
    spec.column_centers = None                 # None → 自動放在軸網交點
    spec.column_size = COLUMN_SIZE
    spec.floor_label = floor_label

    level = int(floor_label[:-1]) if floor_label[:-1].isdigit() else 1
    sx0, sy0, sx1, sy1 = stair_rect              # 先掛樓梯:開口收尾才避得開梯段
    spec.stairs = [_stair(sx0, sx1, sy0, sy1, stair_label)]
    _add_stair_guard_walls(spec)                 # 梯段兩側都要有牆(不能一側懸空)
    party = core_xlines is None                 # 中央核骨架=獨棟(四面採光);西側核=透天共壁
    _fix_openings(spec, env[0], env[1], env[2], level, party_walls=party)
    _remove_openings(spec, {(dp.wall_index, dp.opening_index) for dp in spec.doors
                            if "pipe_shaft" in _door_kinds(spec, dp)})  # 管道間非走入
    # 開口保證(在所有刪門收尾之後,否則補的門會又被刪):
    #   ① 整層室內連通 → 從大門走得到每一間房(柱線牆不會把一層切成兩半)
    #   ② 仍沒門的房間補一扇(最後手段)
    #   ③ 居室補窗(前後外牆或天井側;共壁窗被刪後不能讓房間全暗)
    _ensure_floor_connected(spec)
    _ensure_room_doors(spec, env[0], env[1], env[2], level)
    # min_col_clear=0:這條產線的關卡是 plan_check(只擋「窗框真的穿過柱」),
    # 所以窗可以貼著柱 —— 規則版走 validate_spec(要求 300mm 淨距)就不行。
    _ensure_room_windows(spec, env[0], env[1], env[2], env[3], party_walls=party,
                         min_col_clear=0.0)
    # 門窗的「柱定案後再修一次」。柱在上面已經定案,這裡把開在柱上的洞口沿牆
    # 挪開(只挪不縮,縮窗會破 §40 採光)。
    # ⚠️ 門也要 —— repair_doors 只管「門扇打開撞到什麼」,門洞本身跨在柱上它不管。
    shift_openings_off_columns(spec)

    if furnish:
        from src.design.layout.auto_furnish import furnish_spec
        from src.design.layout.fixture_fix import push_fixtures_out_of_walls
        furnish_spec(spec)
        push_fixtures_out_of_walls(spec)          # 貼牆家具嵌進牆面 → 推回室內
        _declutter_for_circulation(spec)          # 移掉擋動線的家具 → 每房走得通
        from src.design.layout.auto_furnish import settle_after_declutter
        settle_after_declutter(spec)              # 補回被移掉的床/洗澡設備
    # 門與動線規範:轉門/改橫拉門、衛浴門不朝廚房、補一扇門直通公共動線。
    # 要在家具擺完之後——開啟弧線會不會撞到家具,擺完才知道。
    from src.design.layout.door_rules import repair_doors
    repair_doors(spec, env[0], env[1], env[2], level)
    return spec, satisfied, len(edges), score


CIRC_RETRIES = 6         # 某層落實不合格 → 換這麼多個切法種子重試找合格的


#: 換個切法就能解決的法規違規 —— 併進重生條件(見 _floor_errors)。
_RECUTTABLE_CODE = ("daylight_area", "vent_area")


def _floor_errors(spec, env, level: int, label: str = "") -> list:
    """這層違反了哪些**硬規則**(沒門/斷開/穿牆家具/動線不通/門通往空中)。

    空清單 = 這層是合格圖。設計面問題(內間沒光、房間過大)不在此列——那要改設計,
    不是換切法能解決的,由 critique/收斂迴圈回饋給 LLM。

    ⚠️ 2026-08-19 補上 **§40/§43 居室採光通風**。它原本只在最後由 code_check 檢查,
    網站看到違規就回 422 —— 但這條完全符合本專案「error = 同一份關係圖、換個切法
    就能解決」的分界:書房被排到沒有外牆的格子,換個切法把它排到外牆邊就好了。
    不併進來的話,產線明明有重生機制卻不會為了採光重生,只能整份設計被擋掉。
    """
    from src.design.layout.code_check import check_code_floor
    from src.design.layout.plan_check import check_floor
    out = [i for i in check_floor(spec, env, level, label)
           if i.severity == "error"]
    try:
        out += [i for i in check_code_floor(spec, env, level, label)
                if i.code in _RECUTTABLE_CODE]
    except Exception:                                 # noqa: BLE001
        pass          # 法規檢查是加分項,它自己壞掉不該讓整層生不出來
    return out


def realize_graph_building(graph: dict, building_w: float, building_d: float,
                           setback: float = 2000.0,
                           rng: Optional[random.Random] = None,
                           tries: int = 300, furnish: bool = True):
    """LLM 關係圖 → 整棟:每層落實,核(樓梯+管道間+天井)與結構柱上下對齊。

    某層第一次落實若動線卡住,換幾個切法種子重試、留第一個全通的(找不到才留原本)。
    第一次一律用傳入的 rng → 一次就全通的樓層(常態)輸出與過去逐位元相同、不回歸。

    回 [(label, spec, 滿足邊, 總邊)]。"""
    if rng is None:
        rng = random.Random(7)
    floors = sorted({r["floor"] for r in graph["rooms"] if r["floor"] >= 1})
    env = (setback, setback, setback + building_w, setback + building_d)
    # 各層「核以外房間數」的最小值 → 決定骨架(中央核的房間圈需要 ≥4 間才填得滿)
    # ⚠️ 這裡要排掉的是「不會變成房間的那些」:樓梯與天井本來就不算,
    #    走道 2026-08-19 起會被 drop_corridors 收掉,同樣不能拿來湊房間數
    #    (拿它湊 → 以為有 4 間 → 選了中央核骨架 → 實際只有 3 間填不滿)。
    min_free = min(
        sum(1 for r in graph["rooms"]
            if r["floor"] == f
            and r["kind"] not in ("stair", "patio", "corridor"))
        for f in floors) if floors else 0
    core = _choose_core(env, min_free)             # 寬扁/方形→中央核,窄長→西側核
    top = max(floors)

    out = []
    for f in floors:
        rooms = [r for r in graph["rooms"] if r["floor"] == f]
        ids = {r["id"] for r in rooms}
        edges = [(e["a"], e["b"], e["connection"]) for e in graph["adjacencies"]
                 if e["a"] in ids and e["b"] in ids]
        entry = graph.get("entry") if graph.get("entry") in ids else None
        rooms, edges, entry = drop_corridors(rooms, edges, entry)   # 走道一律不做
        stair_label = "下" if f == top else "上"

        def realize(alt_rng, bay):
            return _realize_floor_core(rooms, edges, entry, env, core, alt_rng,
                                       tries, furnish, f"{f}F", stair_label,
                                       bay_walls=bay)

        spec, sat, tot, _ = realize(rng, True)        # 常態:傳入 rng + 柱線牆
        # 圖面關卡:不合格(沒門/斷開/家具穿牆/動線不通/門通往空中)就換切法重生。
        # 先保留柱線牆(內柱藏牆內),仍不合格再放棄柱線牆(寧可浮一根內柱也要圖能用)。
        # 第一次一律用傳入 rng → 一次就合格的樓層(常態)輸出逐位元不變、不回歸。
        # 全部重試都不合格時,留「錯誤最少」的那版(永不比原本差)。
        errs = _floor_errors(spec, env, f, f"{f}F") if furnish else []
        if errs:
            best = (len(errs), spec, sat, tot)
            for bay in (True, False):
                base = f * 1009 + 17 + (0 if bay else 500)
                for k in range(CIRC_RETRIES):
                    a_spec, a_sat, a_tot, _ = realize(
                        random.Random(base + k * 31), bay)
                    n = len(_floor_errors(a_spec, env, f, f"{f}F"))
                    if n < best[0]:
                        best = (n, a_spec, a_sat, a_tot)
                    if n == 0:
                        break
                if best[0] == 0:
                    break
            _n, spec, sat, tot = best
        out.append((f"{f}F", spec, sat, tot))
    return out


# ── 命令列:一句需求 → LLM 設計 → 落實整棟 → DXF ────────────────────────────
def main(argv: Optional[list[str]] = None) -> None:
    import json
    import os
    import sys
    from pathlib import Path

    from src.design.layout.room_graph import _fmt_graph, propose_room_graph
    from src.drafting.apartment_plan import draw_floor_plan
    from src.standards.loader import apply_standard, load_standard, new_document

    args = argv if argv is not None else sys.argv[1:]
    W, D = 7000.0, 12000.0
    outdir = Path(__file__).resolve().parents[3] / "output"
    outdir.mkdir(exist_ok=True)
    cache = outdir / "graph_last.json"

    # 可重現:--cached 讀上次的關係圖(不呼叫 Gemini),方便反覆調落實/收尾。
    if args and args[0] == "--cached" and cache.exists():
        graph = json.loads(cache.read_text(encoding="utf-8"))
        print(f"[1/3] 用快取關係圖 {cache.name}(--cached,不呼叫 Gemini)")
    else:
        from src.design.api_keys import have_key
        if not have_key():
            print("需要設定 GEMINI_API_KEY 環境變數")
            raise SystemExit(1)
        brief = " ".join(args) if args else "透天三層,建築物7×12米,三房"
        print(f"需求:「{brief}」  建築 {W/1000:g}×{D/1000:g}m")
        print("[1/3] Gemini 設計關係圖…")
        graph = propose_room_graph(brief, floor_area_m2=W * D / 1e6)
        cache.write_text(json.dumps(graph, ensure_ascii=False, indent=2),
                         encoding="utf-8")
    print(_fmt_graph(graph))

    print("\n[2/3] 逐層落實(對齊核:樓梯+管道間+天井;加結構柱)…")
    floors = realize_graph_building(graph, W, D, rng=random.Random(7))
    for label, spec, sat, tot in floors:
        try:
            from src.design.layout.global_score import score_report
            from src.design.layout.room_circulation import analyze_room_circulation
            ok = analyze_room_circulation(spec).ok
            sc = score_report(spec)
            extra = (f"動線{'✅' if ok else '⚠️'}  "
                     f"{sc['overall_score']:.0f}({sc['grade']})")
        except Exception as exc:                          # noqa: BLE001
            extra = f"(驗證器略過:{exc})"
        doc = new_document()
        layers = apply_standard(doc, load_standard())
        draw_floor_plan(doc.modelspace(), spec, layers)
        doc.saveas(outdir / f"graph_{label}.dxf")
        print(f"  {label}: 相鄰 {sat}/{tot}  {len(spec.rooms)} 室  "
              f"{len(spec.fixtures)} 家具  {extra}  → graph_{label}.dxf")

    print("\n[3/3] 對齊檢查(核 + 結構柱)…")
    stairs = {s.origin for _, sp, _, _ in floors for s in sp.stairs}
    grids = {(sp.grid_origin, tuple(sp.x_spacings), tuple(sp.y_spacings))
             for _, sp, _, _ in floors}
    patios = {next((tuple(map(tuple, r.points))
                    for r in sp.rooms if r.kind == "patio"), None)
              for _, sp, _, _ in floors}
    tick = lambda s: "✅ 對齊" if len(s) == 1 else "⚠️ 沒對齊"    # noqa: E731
    print(f"  樓梯 {tick(stairs)}   天井 {tick(patios)}   結構柱 {tick(grids)}")


if __name__ == "__main__":
    main()
