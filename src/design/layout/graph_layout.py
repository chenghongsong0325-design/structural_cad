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
DAYLIGHT_BONUS = 0.5     # 要採光的房落在外框(有窗)→ 加分
ENTRY_FRONT_BONUS = 1.0  # 大門那間貼南面(臨路)→ 加分
W_SIZE = 1.0             # 房間大小離「合理範圍」的扣分權重
SIZE_CAP = 1.5           # 單間大小扣分上限(免得一間爆掉壓過相鄰)
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
    "bedroom": (9, 18), "master_bedroom": (12, 24), "bathroom": (2.5, 6),
    "toilet": (1.5, 4), "stair": (3.5, 7), "corridor": (2, 8),
    "patio": (2, 9), "storage": (1.5, 6), "study": (6, 14),
    "elder_room": (9, 18), "garage": (12, 30), "balcony": (2, 8),
    "utility": (2, 8),
}

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

# 垂直核(每層同位 → 樓梯/管道間/天井上下對齊)與結構柱。
# 樓梯間進深:牆縫 150 + **起步平台 900**(門開進來先站平地再上階)+ 梯跑 9×260
# + 折返平台 ~900。少了起步平台就會「開門即踏step」——門扇掃到階梯、人沒落腳處。
STAIR_DEPTH = 4300.0
# 管道間(機電豎管:給排水/電氣)= SHAFT_W×SHAFT_D(寬 40~80cm、深 40~60cm,
# 使用者 2026-07-29 定調)。**不做天井**:住宅一律不設採光天井,原本天井那塊面積
# 還給房間;管道帶剩下的長度做成壁櫃(房間要鋪滿建築,牆才推得出來)。
COLUMN_SIZE = 400.0      # 結構柱斷面(mm 見方)
COLUMN_SPAN = 6000.0     # 柱距目標(沿進深,6m 經濟跨度;藏外牆內、上下對齊)

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


def _split(c: Rect, rng: random.Random, min_cell: float) -> list[Rect]:
    x0, y0, x1, y1 = c
    w, h = x1 - x0, y1 - y0
    dirs = []
    if w >= 2 * min_cell:
        dirs.append("V")
    if h >= 2 * min_cell:
        dirs.append("H")
    if rng.choice(dirs) == "V":
        cx = rng.uniform(x0 + min_cell, x1 - min_cell)
        return [(x0, y0, cx, y1), (cx, y0, x1, y1)]
    cy = rng.uniform(y0 + min_cell, y1 - min_cell)
    return [(x0, y0, x1, cy), (x0, cy, x1, y1)]


def _partition_from(seed: list[Rect], n: int, rng: random.Random,
                    min_cell: float = MIN_CELL) -> Optional[list[Rect]]:
    """從一組已鋪滿的矩形(seed)繼續二分,切到共 n 個。切不出來回 None。

    seed 可以是 [整個 envelope](一般情形)或 [挖掉樓梯後的 L 形兩塊](對齊多層用)。"""
    cells = list(seed)
    if n < len(cells):
        return None
    while len(cells) < n:
        splittable = [c for c in cells if _can_split(c, min_cell)]
        if not splittable:
            return None
        # 依面積加權挑格子來切(大格較可能被切 → 尺寸較均勻,又保有隨機變化)
        areas = [(c[2] - c[0]) * (c[3] - c[1]) for c in splittable]
        c = rng.choices(splittable, weights=areas, k=1)[0]
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


def _on_perimeter(c: Rect, env: Rect) -> bool:
    x0, y0, x1, y1 = c
    ex0, ey0, ex1, ey1 = env
    return (abs(x0 - ex0) <= SNAP or abs(x1 - ex1) <= SNAP
            or abs(y0 - ey0) <= SNAP or abs(y1 - ey1) <= SNAP)


def _on_front(c: Rect, env: Rect) -> bool:
    """貼南面(y 最小側 = 臨路面)。"""
    return abs(c[1] - env[1]) <= SNAP


# ── 3) 把房間指派到格子:讓 LLM 要的相鄰滿足最多 ────────────────────────────
def _size_penalty(area_m2: float, kind: str) -> float:
    """房間面積離合理帶多遠(0=帶內;超出用相對誤差,上限 SIZE_CAP)。"""
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
    return min(SIZE_CAP, over)


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


def _score(perm: tuple, rooms: list, edges: list, cells: list[Rect],
           cell_adj: set[frozenset], env: Rect, entry_id: Optional[str]) -> float:
    """perm[k] = 第 k 個房間放進哪個格子。回總分(越高越符合 LLM 的關係圖)。"""
    pos = {rooms[k]["id"]: perm[k] for k in range(len(rooms))}
    s = 0.0
    for a, b, conn in edges:
        if a not in pos or b not in pos:
            continue
        near = frozenset((pos[a], pos[b])) in cell_adj
        if conn in ("door", "open"):
            s += W_STRONG if near else -P_STRONG_MISS
        elif near:
            s += W_WEAK
    for k, r in enumerate(rooms):
        c = cells[perm[k]]
        if r.get("wants_daylight") and _on_perimeter(c, env):
            s += DAYLIGHT_BONUS
        if entry_id and r["id"] == entry_id and _on_front(c, env):
            s += ENTRY_FRONT_BONUS
        area_m2 = (c[2] - c[0]) * (c[3] - c[1]) / 1e6
        s -= W_SIZE * _size_penalty(area_m2, r["kind"])       # 房間大小合不合理
        s -= W_ASPECT * _aspect_penalty(c, r["kind"])         # 房間會不會細長像走廊
    return s


def _best_perm(rooms, edges, cells, cell_adj, env, entry_id):
    """房間 ≤7 暴力枚舉最佳指派;更多用貪婪近似。回 (score, perm)。"""
    n = len(rooms)
    if n <= 7:
        best = None
        for perm in itertools.permutations(range(n)):
            sc = _score(perm, rooms, edges, cells, cell_adj, env, entry_id)
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
                        rooms, edges, cells, cell_adj, env, entry_id)
            if best_sc is None or sc > best_sc:
                best_sc, best_cell = sc, c
        assign[k] = best_cell
        used.add(best_cell)
    return (_score(tuple(assign), rooms, edges, cells, cell_adj, env, entry_id),
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


def _core_courtyard(env: Rect):
    """中央核骨架(Phase 3):樓梯+管道柱擺在平面**中央**,房間繞成一圈。

    寬扁/方形基地不能用長條骨架(西側核+東側長條)——那會把房間拉成 3×10m 的
    細長條。作法是**服務核置中**,四周一圈房間各自貼一段外牆採光,誰都不會變暗房
    (原本核裡還有一座中庭,依使用者 2026-07-29 定調拿掉——住宅不設天井;四帶各自
    臨外牆,採光本來就不靠中庭)。

    回 (stair_rect, shaft_rect, patio_rect, seed, core_w, xlines);
    seed = 中央核以外鋪滿的四塊(南帶/北帶/西帶/東帶),交給房間去細分。
    xlines = 中央核的左右邊 → 柱軸線落在核的牆上(柱藏牆內,不會站在房間中央)。
    放不下(基地太小)回 None,由呼叫端退回長條骨架。"""
    ex0, ey0, ex1, ey1 = env
    W, D = ex1 - ex0, ey1 - ey0

    core_w = COURT_STAIR_W + SHAFT_W
    core_d = STAIR_DEPTH
    if W - core_w < 2 * RING_MIN or D - core_d < 2 * RING_MIN:
        return None                                  # 兩側/前後圈不夠厚 → 不適用

    cx0 = ex0 + (W - core_w) / 2.0                   # 水平置中
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
    return stair_rect, shaft_rect, closet_rect, seed, core_w, [cx0, cx1]


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


def _assign_core(free, fixed, allcells, cell_adj, env, entry_id, edges):
    """free 房間指派到剩餘格;fixed=[(room, 固定格index)] 為核件(樓梯/管道間/天井)。

    核件位置固定,但仍參與相鄰評分(讓走道自然貼樓梯、廚房貼天井)。回 ((score, perm), rooms_full)。"""
    rooms_full = free + [r for r, _ in fixed]
    fixed_idx = tuple(i for _, i in fixed)
    m = len(free)
    if m <= 7:
        best = None
        for perm in itertools.permutations(range(m)):
            pf = perm + fixed_idx
            sc = _score(pf, rooms_full, edges, allcells, cell_adj, env, entry_id)
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
            sc = _score(pf, rooms_full, edges, allcells, cell_adj, env, entry_id)
            if bs is None or sc > bs:
                bs, bc = sc, c
        assign[k] = bc
        used.add(bc)
    pf = tuple(assign) + fixed_idx
    return (_score(pf, rooms_full, edges, allcells, cell_adj, env, entry_id),
            pf), rooms_full


DECLUTTER_SKIP = ("patio", "parking", "garage", "stair", "balcony")


def _declutter_for_circulation(spec, max_removals: int = 20) -> int:
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
    g_stair = next((r for r in rooms if r["kind"] == "stair"), None)
    free = [r for r in rooms if r["kind"] not in ("stair", "patio")]
    stair_room = g_stair or {"id": f"stair_{floor_label}", "kind": "stair",
                             "wants_daylight": False}
    closet_room = {"id": f"closet_{floor_label}", "kind": "storage",
                   "wants_daylight": False}
    shaft_room = {"id": f"shaft_{floor_label}", "kind": "pipe_shaft",
                  "wants_daylight": False}
    m = len(free)

    # 多跨(>9m 寬):在內柱軸線處預切 seed → 每層同位置都有隔間牆,內柱藏其中、
    # 上下對齊。房間夠多才強制(否則分帶填不滿 → 退回原 seed,寧可少一道柱線牆)。
    xlines = ((core_xlines or _bay_xlines(env)) if bay_walls else [])
    if xlines:
        banded = _apply_bay_walls(seed, xlines)
        if m >= len(banded):
            seed = banded

    best = None
    for _ in range(tries):
        cells = _partition_from(seed, m, rng)
        if cells is None:
            continue
        allcells = cells + [stair_rect, shaft_rect, closet_rect]
        cadj = _cell_adjacency(allcells)
        fixed = [(stair_room, m), (shaft_room, m + 1), (closet_room, m + 2)]
        (sc, pf), rooms_full = _assign_core(free, fixed, allcells, cadj, env,
                                            entry_id, edges)
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
        named.append((KIND_MAP.get(r["kind"], r["kind"]),
                      LABEL.get(r["kind"], r["kind"]),
                      [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]))

    setback = env[0]
    site_w = (env[2] - env[0]) + 2 * setback
    site_d = (env[3] - env[1]) + 2 * setback
    spec = rooms_to_spec(named, env, site_w, site_d, setback)

    # 收尾門洞(重用 narrow_house):去重複門/界牆窗/天井門、浴室只留 1 門、1F 補前門。
    from src.design.layout.narrow_house import (
        _add_stair_guard_walls, _door_kinds, _ensure_floor_connected,
        _ensure_room_doors, _ensure_room_windows, _fix_openings,
        _remove_openings, _stair,
    )
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
    _ensure_room_windows(spec, env[0], env[1], env[2], env[3], party_walls=party)

    # 結構柱:面寬與進深各每 ~6m 一道軸線,柱放外框軸網交點(藏牆內、每層同位=上下
    # 對齊)。面寬 >9m 時分多跨,內柱落在上面預切的柱線牆內,不孤立在房間中央。
    W, D = env[2] - env[0], env[3] - env[1]
    ny = max(1, round(D / COLUMN_SPAN))
    spec.grid_origin = (env[0], env[1])
    if core_xlines:                             # 中央核骨架:柱軸線=中央核的左右牆
        xs = [env[0], *core_xlines, env[2]]
        spec.x_spacings = [b - a for a, b in zip(xs, xs[1:])]
    else:
        nx = _n_bays_x(W)
        spec.x_spacings = [W / nx] * nx
    spec.y_spacings = [D / ny] * ny
    spec.column_centers = None                 # None → 自動放在軸網交點
    spec.column_size = COLUMN_SIZE
    spec.floor_label = floor_label

    if furnish:
        from src.design.layout.auto_furnish import furnish_spec
        furnish_spec(spec)
        _declutter_for_circulation(spec)          # 移掉擋動線的家具 → 每房走得通
    return spec, satisfied, len(edges), score


CIRC_RETRIES = 6         # 某層落實不合格 → 換這麼多個切法種子重試找合格的


def _floor_errors(spec, env, level: int, label: str = "") -> list:
    """這層違反了哪些**硬規則**(沒門/斷開/穿牆家具/動線不通/門通往空中)。

    空清單 = 這層是合格圖。設計面問題(內間沒光、房間過大)不在此列——那要改設計,
    不是換切法能解決的,由 critique/收斂迴圈回饋給 LLM。"""
    from src.design.layout.plan_check import check_floor
    return [i for i in check_floor(spec, env, level, label)
            if i.severity == "error"]


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
    min_free = min(
        sum(1 for r in graph["rooms"]
            if r["floor"] == f and r["kind"] not in ("stair", "patio"))
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
        if not (os.environ.get("GEMINI_API_KEY")
                or os.environ.get("GOOGLE_API_KEY")):
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
