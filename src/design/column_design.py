"""柱的斷面概算 —— 負擔面積法。

⚠️ **這是概算,不是結構計算。**
   真正的結構設計要算地震力、風力、彎矩、韌性配筋、基礎,是結構技師簽證的
   責任。本模組只回答「這根柱大概該多粗」,讓圖上的柱不再是憑空寫死的固定
   值 —— 一樓比頂樓粗這件事,圖上看得出來。**任何實際工程都不得引用。**

為什麼要做:原本全棟柱一律 500×500(競賽構造尺寸),但真實設計師是反過來的
—— 先看這根柱頭上壓了多少東西,再回推斷面。三層透天的柱跟十層公寓的柱不會
一樣粗,同一棟樓一樓的柱也一定比頂樓粗。

流程(真實結構技師的簡化版)::

    負擔面積 (tributary area)      每根柱負擔「周圍到隔壁柱一半距離」圍出來的面積
      ↓ × 上方樓層數 × 單位重
    軸力 P                         這根柱從屋頂累積下來要撐的重量
      ↓ ÷ 容許軸壓應力
    斷面積 Ag → 邊長               進位到 5cm 級距,守規範下限

⚠️ 只算**軸力**(垂直壓下來的重量)。地震的水平力會讓柱承受彎矩,實際斷面
   通常比純軸力大 —— 這也是容許應力取 0.35f'c(而不是更高)的原因:用一個
   保守的應力上限,粗略把彎矩的影響吃進去。

⚠️ 活載重**沒有做樓層折減**(規範允許多層累積時折減)。不折減 = 偏保守,
   對「概算」而言是安全的方向。

典型用法::

    from src.design.column_design import size_building_columns

    report = size_building_columns(building)     # 只算不改
    print(report.summary())
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field

Point = tuple[float, float]

# ── 載重(tf/m²)────────────────────────────────────────────────────────
# 靜載重:RC 樓板 15cm×2.4 = 0.36、地坪粉刷 ≈0.10、輕隔間分攤 ≈0.10、
#         梁自重分攤 ≈0.15 → 約 0.70。住宅的概算慣用值。
DEAD_LOAD = 0.70
# 活載重:建築技術規則構造編 —— 住宅用途 200 kgf/m²。
LIVE_LOAD = 0.20

# ── 材料與容許應力 ──────────────────────────────────────────────────────
FC = 210.0              # 混凝土抗壓強度 f'c(kgf/cm²),住宅常用 210~280
ALLOW_RATIO = 0.35      # 容許軸壓應力 = ALLOW_RATIO × f'c
                        # 純軸壓理論值可到 0.85f'c,取 0.35 是把偏心與地震
                        # 彎矩的影響粗略吃進來(概算慣用的保守值)。

# ── 斷面限制 ────────────────────────────────────────────────────────────
# 下限 30cm:混凝土/耐震規範對抗彎矩構架的 RC 柱短邊有最小尺寸要求。
# ⚠️ 論文引用前請自行核對現行條文版本。
MIN_SIDE = 300.0
MAX_SIDE = 900.0        # 超過這個尺度已非住宅,回報但不再長大
SIDE_STEP = 50.0        # 實務以 5cm 為級距(模板尺寸)

# ── 實務起始斷面(經驗值,**不是規範也不是計算**)────────────────────────
# 為什麼需要這張表:上面的重力概算跑真實住宅的結果是「幾乎每根柱都只需要
# 30cm」——低層住宅的柱根本不是被壓垮決定的。真正決定住宅柱斷面的是**耐震**
# (地震水平力造成的彎矩、以及「強柱弱梁」的韌性要求),而那要先設計梁、
# 算地震力,遠超本專題範圍。
#
# 所以這裡補一張實務初步設計常用的「起始斷面」對照表,鍵是**這根柱上方有
# 幾層樓板**。最終採用 = max(重力概算, 這張表)。
#
# ⚠️ 表中數字是**經驗值**,沒有法源。要寫進論文的話,建議拿真實建案的結構
#    圖核對過再引用(你在營造廠實習過,工地的結構圖就是最好的對照來源)。
EMPIRICAL_START = {1: 300.0, 2: 300.0, 3: 350.0, 4: 400.0,
                   5: 400.0, 6: 450.0, 7: 450.0, 8: 500.0}
EMPIRICAL_TALL_BASE, EMPIRICAL_TALL_STEP = 500.0, 50.0   # 9 層以上每 2 層 +5cm


def empirical_start_side(floors_above: int) -> float:
    """實務初步設計的起始斷面(mm)。⚠️ 經驗值,非規範、非計算。"""
    if floors_above <= 0:
        return MIN_SIDE
    if floors_above in EMPIRICAL_START:
        return EMPIRICAL_START[floors_above]
    extra = ((floors_above - 8) + 1) // 2 * EMPIRICAL_TALL_STEP
    return min(MAX_SIDE, EMPIRICAL_TALL_BASE + extra)

CLUSTER_TOL = 200.0     # 柱心分行分列的容差(mm);柱位微調過的同一排要算同一列
EDGE_TOL = 60.0         # 判斷柱在不在建築外緣的容差(mm)
# 這些「房間」是封閉豎井,人不會站在裡面,柱凸進去沒差(與 plan_check 同一套)。
VOID_ROOM_KINDS = {"pipe_shaft", "patio"}


# ---------------------------------------------------------------------------
# 報告
# ---------------------------------------------------------------------------
@dataclass
class FloorColumnDesign:
    """一層樓的柱斷面概算結果。"""

    label: str
    floors_above: int          # 這層的柱要扛幾層樓板(含屋頂)
    max_tributary_m2: float    # 該層負擔面積最大的那根柱
    axial_load_tf: float       # 那根柱的軸力
    gravity_side: float        # 純重力概算需要的邊長(mm)
    empirical_side: float      # 實務起始斷面(經驗值,mm)
    required_side: float       # 兩者取大 = 建議斷面(mm)
    applied_side: float        # 圖上實際採用的邊長(mm)
    n_columns: int

    @property
    def governed_by(self) -> str:
        """誰決定了這個斷面 —— 講清楚才不會讓人誤以為全都是算出來的。"""
        return "重力" if self.gravity_side >= self.empirical_side else "經驗值"

    def to_dict(self) -> dict:
        return {**asdict(self), "governed_by": self.governed_by}


@dataclass
class ColumnDesignReport:
    """整棟樓的柱斷面概算。"""

    floors: list[FloorColumnDesign] = field(default_factory=list)
    note: str = "概算,非結構計算;僅供圖面尺寸參考,不得用於實際工程"

    @property
    def sizes(self) -> dict[str, float]:
        """{樓層標示: 採用邊長}。"""
        return {f.label: f.applied_side for f in self.floors}

    def summary(self) -> str:
        if not self.floors:
            return "柱斷面概算:這棟樓沒有柱(窄透天/淺透天不設柱)"
        lines = ["柱斷面概算(負擔面積法 + 實務起始斷面;⚠️ 概算,非結構計算)"]
        for f in self.floors:
            lines.append(
                f"  {f.label:>4}  柱 {f.applied_side:.0f}×{f.applied_side:.0f}"
                f"  ({f.n_columns} 根)  扛 {f.floors_above} 層"
                f"  負擔 {f.max_tributary_m2:.1f}m² → 軸力 {f.axial_load_tf:.0f}tf"
                f"  重力需 {f.gravity_side:.0f} / 經驗 {f.empirical_side:.0f}"
                f"  → {f.required_side:.0f}mm({f.governed_by}控制)")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {"floors": [f.to_dict() for f in self.floors],
                "sizes": self.sizes, "note": self.note}

    def to_json(self, **kw) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, **kw)


# ---------------------------------------------------------------------------
# 負擔面積
# ---------------------------------------------------------------------------
def _cluster(values: list[float], tol: float = CLUSTER_TOL) -> list[float]:
    """把接近的座標併成同一條軸線,回傳由小到大的代表值。

    為什麼要併:柱位微調(_tuck)會把分界牆那一排柱往北推,同一排的 y 不會
    完全相等;不併的話那一排會被當成好幾條軸線,負擔面積就算歪了。
    """
    out: list[float] = []
    for v in sorted(values):
        if not out or v - out[-1] > tol:
            out.append(v)
    return out


def _spans(axes: list[float]) -> list[float]:
    """每條軸線的「負擔寬度」= 左半跨 + 右半跨(最外側只算內側那半)。"""
    if len(axes) == 1:
        return [0.0]
    out = []
    for i, a in enumerate(axes):
        left = (a - axes[i - 1]) / 2 if i > 0 else 0.0
        right = (axes[i + 1] - a) / 2 if i < len(axes) - 1 else 0.0
        out.append(left + right)
    return out


def tributary_areas(centers: list[Point]) -> list[float]:
    """每根柱的負擔面積(m²),順序同 centers。

    柱網是矩形格子 → 負擔面積 = 該柱所在 X 軸線的負擔寬 × Y 軸線的負擔寬。
    最外圈的柱只負擔內側那半跨,所以角柱最小、中間柱最大(通常是 4 倍)。
    """
    if not centers:
        return []
    xs = _cluster([c[0] for c in centers])
    ys = _cluster([c[1] for c in centers])
    wx = dict(zip(xs, _spans(xs)))
    wy = dict(zip(ys, _spans(ys)))

    def _near(axes: list[float], v: float) -> float:
        return min(axes, key=lambda a: abs(a - v))

    return [wx[_near(xs, cx)] * wy[_near(ys, cy)] / 1_000_000
            for cx, cy in centers]


# ---------------------------------------------------------------------------
# 軸力 → 斷面
# ---------------------------------------------------------------------------
def axial_load(tributary_m2: float, floors_above: int,
               dead: float = DEAD_LOAD, live: float = LIVE_LOAD) -> float:
    """柱頂累積軸力(tf)= 負擔面積 × 上方樓層數 × 每層單位重。"""
    if floors_above < 0:
        raise ValueError(f"上方樓層數不可為負,收到 {floors_above}")
    return tributary_m2 * floors_above * (dead + live)


def required_side(load_tf: float, fc: float = FC) -> float:
    """由軸力回推方柱邊長(mm),進位到 5cm 級距、守 30cm 下限。

    Ag ≥ P / (0.35 f'c) → 邊長 = √Ag。
    """
    allow = ALLOW_RATIO * fc                     # kgf/cm²
    ag_cm2 = load_tf * 1000.0 / allow            # tf → kgf → cm²
    side_mm = (ag_cm2 ** 0.5) * 10.0
    stepped = -(-side_mm // SIDE_STEP) * SIDE_STEP        # 無條件進位到級距
    return float(min(MAX_SIDE, max(MIN_SIDE, stepped)))


# ---------------------------------------------------------------------------
# 整棟
# ---------------------------------------------------------------------------
def design_building_columns(building) -> ColumnDesignReport:
    """整棟樓逐層算柱斷面(**只算,不改** building)。

    每層的柱要扛「它上面所有樓層」的樓板 —— 最底層扛最多,頂層只扛屋頂。
    同一層所有柱採同一尺寸(取該層負擔面積最大那根的需求),這是小型建築
    的實務做法:柱種類越少,模板與施工越單純。
    """
    from src.design.building_generator import _column_centers   # 避免循環匯入

    report = ColumnDesignReport()
    n = len(building.floors)
    for i, fl in enumerate(building.floors):
        centers = _column_centers(fl.spec)
        if not centers:
            continue
        trib = tributary_areas(centers)
        worst = max(trib)
        above = n - i                       # 這層的柱要扛幾層樓板(含屋頂)
        load = axial_load(worst, above)
        grav = required_side(load)
        emp = empirical_start_side(above)
        report.floors.append(FloorColumnDesign(
            label=fl.label, floors_above=above, max_tributary_m2=worst,
            axial_load_tf=load, gravity_side=grav, empirical_side=emp,
            required_side=max(grav, emp),
            applied_side=float(fl.spec.column_size), n_columns=len(centers)))
    return report


# ---------------------------------------------------------------------------
# 柱座落品質(柱有沒有藏進牆裡)
# ---------------------------------------------------------------------------
@dataclass
class ColumnSeatingReport:
    """柱位品質 —— 有多少柱真的坐在牆上。

    真實設計師擺柱的第一原則是「柱藏在隔間牆內」,不然客廳中間會杵一根方柱。
    這份報告把它量化,改柱網演算法時才知道有沒有真的變好(不是憑感覺)。

    ⚠️ 判準是「牆的中心線有沒有穿過柱的斷面」,不是「柱心到牆中心線的距離」。
       因為柱位微調(_tuck)會刻意把柱推離牆中心線、讓柱的一面貼齊牆面 ——
       用柱心距離判,那些**最正確**的柱反而會被判成沒藏好。

    ⚠️ **孤柱不一定是缺點,要看樓層用途。** 地下停車場本來就是大開放空間、
       沒有隔間牆,柱單獨立著是正確的(實測集合住宅 B1F 藏牆率 72~79%,
       地上各層 100%)。這個指標只有對**有隔間的居住樓層**才是品質紅線。
    """

    junction: int = 0      # 交點柱:兩個方向的牆都穿過 → 最理想
    edge: int = 0          # 邊柱:只有一個方向的牆穿過
    orphan: int = 0        # 孤柱:沒有牆穿過,柱單獨杵在房間裡
    orphan_points: list[Point] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.junction + self.edge + self.orphan

    @property
    def seated_pct(self) -> float:
        """藏進牆裡的比例(交點柱 + 邊柱)。"""
        return 100.0 * (self.junction + self.edge) / self.total if self.total else 0.0

    def summary(self) -> str:
        return (f"柱座落:{self.total} 根 —— 交點柱 {self.junction}、"
                f"邊柱 {self.edge}、孤柱 {self.orphan}"
                f"(藏牆率 {self.seated_pct:.0f}%)")

    def to_dict(self) -> dict:
        return {"junction": self.junction, "edge": self.edge,
                "orphan": self.orphan, "total": self.total,
                "seated_pct": round(self.seated_pct, 1),
                "orphan_points": self.orphan_points}

    def to_json(self, **kw) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, **kw)


def column_footprints(spec) -> list:
    """一層樓所有柱的斷面多邊形(世界座標);沒有柱的骨架回空清單。

    ⚠️ **柱是實心的** —— 家具不能擺進去、人也走不過去。但專案裡一直只有
    「牆的實體」(`plan_check._wall_bodies` / `fixture_fix._wall_union`),
    柱從來沒有對應的東西,結果擺位器與圖面關卡**都當柱不存在**
    (實測 18×13 三層:10 件家具壓在柱上,1F 兩張沙發各壓 0.035㎡)。
    這裡是柱實體的單一出處,兩邊共用。
    """
    from shapely.geometry import box

    from src.design.building_generator import _column_centers

    try:
        centers = _column_centers(spec)
    except Exception:               # 窄透天/淺透天沒有柱網,解不出來就是沒有柱
        return []
    half = float(spec.column_size) / 2.0
    return [box(cx - half, cy - half, cx + half, cy + half)
            for cx, cy in centers]


def column_seating(spec) -> ColumnSeatingReport:
    """量測一層樓的柱有多少坐在牆上(交點柱 / 邊柱 / 孤柱)。"""
    from shapely.geometry import LineString, box

    from src.design.building_generator import _column_centers

    rep = ColumnSeatingReport()
    half = float(spec.column_size) / 2.0
    for cx, cy in _column_centers(spec):
        foot = box(cx - half, cy - half, cx + half, cy + half)
        dirs = set()
        for w in spec.walls:
            (x1, y1), (x2, y2) = w.start, w.end
            if (x1, y1) == (x2, y2):
                continue
            if not LineString([w.start, w.end]).intersects(foot):
                continue
            dirs.add("x" if abs(x2 - x1) >= abs(y2 - y1) else "y")
        if len(dirs) >= 2:
            rep.junction += 1
        elif dirs:
            rep.edge += 1
        else:
            rep.orphan += 1
            rep.orphan_points.append((round(cx, 1), round(cy, 1)))
    return rep


# ---------------------------------------------------------------------------
# 室內看不看得到柱(使用者 2026-08-07 定調的設計原則)
# ---------------------------------------------------------------------------
@dataclass
class ColumnVisibilityReport:
    """站在房間裡,看得到多少柱角。

    設計原則(使用者定調):**盡量讓室內的人看不到柱子。** 這比「柱藏在牆內」
    更嚴格 —— 柱坐在牆上,柱角照樣會凸進房間(柱比牆胖)。

    量法:柱斷面扣掉所有牆的實體範圍,剩下的再與**縮到牆內面的房間**相交,
    交到的才算「室內看得到」。凸到室外、凸進牆體、凸進管道間的都不算。
    """

    visible_m2: float = 0.0                 # 室內看得到的柱角總面積
    exterior_columns: int = 0               # 外牆上、有露臉的柱數
    interior_columns: int = 0               # 內部、有露臉的柱數
    by_room_kind: dict = field(default_factory=dict)

    def summary(self) -> str:
        top = sorted(self.by_room_kind.items(), key=lambda t: -t[1])[:3]
        worst = "、".join(f"{k} {v:.2f}m²" for k, v in top) or "無"
        return (f"室內看得到的柱角 {self.visible_m2:.3f} m²"
                f"(外牆柱 {self.exterior_columns} 根 / 內部柱 "
                f"{self.interior_columns} 根);最明顯:{worst}")

    def to_dict(self) -> dict:
        return {"visible_m2": round(self.visible_m2, 4),
                "exterior_columns": self.exterior_columns,
                "interior_columns": self.interior_columns,
                "by_room_kind": {k: round(v, 4)
                                 for k, v in self.by_room_kind.items()}}

    def to_json(self, **kw) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, **kw)


def column_visibility(spec) -> ColumnVisibilityReport:
    """量測一層樓「站在房間裡看得到多少柱角」。"""
    from shapely.geometry import LineString, Polygon, box
    from shapely.ops import unary_union

    from src.design.building_generator import _column_centers
    from src.design.layout.auto_furnish import _inner_room

    rep = ColumnVisibilityReport()
    bands = [LineString([w.start, w.end]).buffer(w.thickness / 2, cap_style=2)
             for w in spec.walls if w.start != w.end]
    if not bands:
        return rep
    walls = unary_union(bands)
    bx0, by0, bx1, by1 = walls.bounds
    inners = [Polygon(_inner_room(spec, r).points) for r in spec.rooms
              if r.kind not in VOID_ROOM_KINDS]

    half = float(spec.column_size) / 2.0
    for cx, cy in _column_centers(spec):
        naked = box(cx - half, cy - half, cx + half, cy + half).difference(walls)
        if naked.is_empty:
            continue
        seen = 0.0
        for room, inner in zip((r for r in spec.rooms
                                if r.kind not in VOID_ROOM_KINDS), inners):
            a = naked.intersection(inner).area / 1_000_000
            if a > 1e-6:
                rep.by_room_kind[room.kind] = rep.by_room_kind.get(room.kind, 0.0) + a
                seen += a
        if seen <= 1e-6:
            continue
        rep.visible_m2 += seen
        on_edge = min(abs(cx - bx0), abs(cx - bx1),
                      abs(cy - by0), abs(cy - by1)) < half + EDGE_TOL
        if on_edge:
            rep.exterior_columns += 1
        else:
            rep.interior_columns += 1
    return rep


# ---------------------------------------------------------------------------
# 套用到圖上
# ---------------------------------------------------------------------------
def _shrink_tuck(centers: list[Point], axes_y: list[float],
                 old: float, new: float) -> list[Point]:
    """柱變細之後,把「柱位微調」的推移量跟著縮回去。

    背景:兩帶式為了不讓 500 的柱探進客廳,把分界牆那排柱往北推了
    `柱寬/2 − 牆厚/2`,讓柱的南面**貼齊牆的南面**(2026-07-15 使用者截圖指正)。
    柱一旦變細,原來的推移量就太多了 —— 柱會整根離開牆面、在牆線上開一個缺口。
    推移量必須跟著改成 `新柱寬/2 − 牆厚/2`,也就是往回退 (舊−新)/2。

    沒被推過的柱(推移量 0,例如外牆柱)不動。
    """
    delta = (old - new) / 2.0
    if delta <= 0 or not axes_y:
        return centers
    out = []
    for cx, cy in centers:
        axis = min(axes_y, key=lambda a: abs(a - cy))
        off = cy - axis
        if abs(off) < 1e-6:
            out.append((cx, cy))
            continue
        sign = 1.0 if off > 0 else -1.0
        out.append((cx, axis + sign * max(0.0, abs(off) - delta)))
    return out


def _push_exterior_out(spec, size: float) -> list[Point]:
    """把外牆上的柱整根往**室外**推,讓室內那一面與牆內面齊平。

    設計原則(使用者 2026-08-07 定調):**盡量讓室內的人看不到柱子。**
    外牆柱原本坐在牆中心線上,柱比牆胖 → 兩邊各凸出 `(柱寬−牆厚)/2`,朝室內
    那一半就是房間角落一根礙眼的柱。但外牆柱有個內部柱沒有的特權:**它可以
    往室外躲**。推出去之後室內牆面完全平整、一根柱都看不到;凸在外面的部分
    變成立面上的垂直線條 —— 真實建築本來就是這樣處理的。

    只推「柱心落在建築外緣上」的柱;角柱兩個方向都推。推完仍不得越過建築線
    (地界線內縮 setback),越過就不推那一根(寧可室內看得到,也不能違建)。
    """
    from shapely.geometry import box

    centers = spec.column_centers
    if not centers:
        return centers

    # ⚠️ 建築外緣要用牆的**中心線**算,不能用牆實體(buffer)的外框:柱心坐在
    #    中心線上,而中心線離牆外皮有半個牆厚(100mm)—— 用外框判,外牆柱一根
    #    都認不出來(這個 bug 讓第一版整個沒生效)。
    segs = [w for w in spec.walls if w.start != w.end]
    if not segs:
        return centers
    xs = [p[0] for w in segs for p in (w.start, w.end)]
    ys = [p[1] for w in segs for p in (w.start, w.end)]
    bx0, bx1, by0, by1 = min(xs), max(xs), min(ys), max(ys)

    # 外牆厚度:取貼著外緣那些牆的最大厚度(通常整圈同厚)。
    ext_t = max((w.thickness for w in segs
                 if min(abs(w.start[0] - bx0), abs(w.start[0] - bx1),
                        abs(w.start[1] - by0), abs(w.start[1] - by1)) < EDGE_TOL),
                default=0.0)
    if ext_t <= 0:
        return centers
    push = (size - ext_t) / 2.0
    if push <= 0:                       # 柱比牆還薄 → 本來就藏得住
        return centers

    # 建築線 = 地界線往內縮 setback。柱推出去**不得越線**,所以能推多少要看
    # 那一側還剩多少餘裕 —— 建築剛好蓋滿可建範圍時餘裕是 0,一根都推不動。
    # 實測:19×13 與 21×12 餘裕 0mm、24×16 剩 88mm、30×14 剩 531mm。
    # 因此這裡做的是「**有多少推多少**」的部分推移,不是全有全無。
    sb = float(getattr(spec, "setback", 0.0) or 0.0)
    site = getattr(spec, "site_boundary", None)
    if site:
        sxs = [p[0] for p in site]
        sys_ = [p[1] for p in site]
        lx0, lx1 = min(sxs) + sb, max(sxs) - sb
        ly0, ly1 = min(sys_) + sb, max(sys_) - sb
    else:                                # 沒有基地資訊 → 不冒險,照全推
        lx0 = ly0 = float("-inf")
        lx1 = ly1 = float("inf")

    half = size / 2.0

    def _room(coord: float, limit: float, outward: int) -> float:
        """這根柱往 outward 方向最多還能推多少(mm),不得讓柱身越過建築線。"""
        edge = coord - half if outward < 0 else coord + half
        gap = (edge - limit) if outward < 0 else (limit - edge)
        return max(0.0, min(push, gap))

    # ⚠️ 外推是**加分項,不能把圖弄壞**:柱往室外挪之後可能剛好蓋住某扇門的開啟
    #    弧(AI 產線的門是獨立擺的,不像兩帶式會先 `_blocked` 躲開柱)。實測 AI 版
    #    12×12 三層因此冒出 door_swing_blocked、整份設計被 422 擋掉。
    #    所以推完要驗一次:擋到門就這根不推(其他根照推)。
    swings = []
    try:
        from src.design.collision.geometry import door_swing_obstacles
        swings = [o.poly for o in door_swing_obstacles(spec)]
    except Exception:                    # 沒有門資訊 → 沒東西好擋,照推
        swings = []

    def _hits_door(px: float, py: float) -> bool:
        if not swings:
            return False
        foot = box(px - half, py - half, px + half, py + half)
        return any(foot.intersection(s).area > 1000.0 for s in swings)

    out = []
    for cx, cy in centers:
        nx, ny = cx, cy
        if abs(cx - bx0) < EDGE_TOL:
            nx = cx - _room(cx, lx0, -1)
        elif abs(cx - bx1) < EDGE_TOL:
            nx = cx + _room(cx, lx1, +1)
        if abs(cy - by0) < EDGE_TOL:
            ny = cy - _room(cy, ly0, -1)
        elif abs(cy - by1) < EDGE_TOL:
            ny = cy + _room(cy, ly1, +1)
        if (nx, ny) != (cx, cy) and _hits_door(nx, ny) and not _hits_door(cx, cy):
            nx, ny = cx, cy              # 推過去會擋門 → 這根留在原位
        out.append((nx, ny))
    return out


def apply_column_design(building, report: ColumnDesignReport | None = None
                        ) -> ColumnDesignReport:
    """把概算出來的斷面套到各層 spec 上(會改 building)。

    做兩件事,順序不能顛倒:

    1. **縮斷面**(只縮不放)。概算結果永遠 ≤ 預設的 500,而縮小的柱一定完全
       落在原本大柱的範圍內 → 不可能新撞到門窗或家具(放大才會)。所以不必
       重排格局,改斷面 + 把柱位微調量縮回去就好。
    2. **外牆柱往室外推**,讓室內看不到柱(見 `_push_exterior_out`)。要在
       縮完之後推,推的量才是依**新**斷面算的。

    兩步都是**冪等**的:斷面已到目標就不再縮;柱一旦推出去,柱心就離開建築
    外緣超過容差,再呼叫一次不會被重複推。
    """
    from src.drafting.apartment_plan import build_grid

    report = report or design_building_columns(building)
    by_label = {f.label: f for f in report.floors}
    for fl in building.floors:
        item = by_label.get(fl.label)
        if item is None:
            continue
        spec = fl.spec
        old = float(spec.column_size)
        new = min(old, item.required_side)     # 只縮不放
        if new < old and spec.column_centers is not None:
            axes_y = [a.position for a in build_grid(spec).y_axes]
            spec.column_centers = _shrink_tuck(spec.column_centers, axes_y,
                                               old, new)
        spec.column_size = new
        item.applied_side = new
        # ⚠️ `column_centers is None` 代表「柱放在每個軸網交點」(窄透天/淺透天
        #    走這條)。要外推就得先把柱位**具體化**成一份清單,否則柱心永遠等於
        #    軸線交點、一根都推不動 —— 這個 if 少了 else 分支的時候,那兩條產線
        #    的外牆柱全部露臉(實測 6.5×14 三層 6 根全露)。
        from src.design.building_generator import _column_centers
        if spec.column_centers is None:
            spec.column_centers = _column_centers(spec)
        spec.column_centers = _push_exterior_out(spec, new)

    # ⚠️ 柱動完一定要讓家具重新貼牆。擺家具那時候柱還是**舊的 500、還沒外推**,
    #    貼牆家具為了閃柱讓開了一段;等這裡把柱縮細又推到室外,讓路的理由就沒了,
    #    沒人叫它回來的話就會停在半空中(實測 19×13 的沙發離牆 18cm,圖上看起來
    #    像擺錯)。settle 只會在「不製造任何新重疊」的前提下往牆邊挪,是安全的。
    from src.design.layout.fixture_fix import settle_fixtures_to_wall
    for fl in building.floors:
        settle_fixtures_to_wall(fl.spec)
    return report
