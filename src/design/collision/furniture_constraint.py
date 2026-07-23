"""Furniture Constraint(v0.7 Phase 6-4)—— 每種家具的「擺放偏好」規格。

前面三個 Phase 回答的是「這裡能不能放」(硬碰撞)與「哪裡比較好」(軟評分),
但兩者都不知道**每種家具本身的擺放常識**:衣櫃背要靠牆、書桌要留椅子拉開的
空間、餐桌不靠牆擺中央、床頭朝哪都行……這些是**家具的屬性**,不是某張圖的
性質。本模組把這份常識抽成資料:

    每種家具一筆 FurnitureConstraint,含三個欄位——

        preferred_wall         背該靠哪面牆(南/北/東/西的集合;可全接受=任意,
                               或空集合=不靠牆的自由站立家具如餐桌/汽車)。
        minimum_clearance      正前方要留多少淨空(mm)才好用:衣櫃要能開門、
                               書桌要能拉椅子、沙發前要走得過。
        preferred_orientation  正面該朝哪個方位(集合;通常=靠牆的反向,但獨立
                               表示才能處理「靠任意牆、但要面向電視」這種需求)。

⚠️ **唯讀、不接進生成流程**(比照 Phase 6-1~6-3):只提供「這件家具這樣擺,
符不符合它自己的偏好」的查詢,**不**改 spec、**不**自動吸附牆面。要不要採納由
呼叫端決定。硬碰撞仍由 FurnitureCollisionEngine 把關——本模組是**軟偏好**,
違反不代表「不能放」,只代表「不是這件家具慣常的擺法」。

座標約定沿用 fixtures.py:貼牆家具原點=貼牆邊中點、朝局部 +Y 伸出;rotation
南牆 0°(朝北)/ 東牆 90°(朝西)/ 北牆 180°(朝南)/ 西牆 270°(朝東)。
故「靠哪面牆」與「正面朝哪」互為反向,但兩者分開宣告(見上)。

典型用法::

    from src.design.collision.furniture_constraint import evaluate_constraint
    from src.drafting.fixtures import FixturePlacement

    res = evaluate_constraint(FixturePlacement("wardrobe", (3000, 4200), 0), room)
    if not res.satisfied:
        print(res.reason)      # 例:「正前方淨空不足(需 600mm)」

⚠️ 各家具的偏好值為常見居家做法,非任何規範——見模組結尾 PENDING。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from shapely.geometry import Polygon

from src.design.collision.detector import OVERLAP_TOL
from src.design.report import JsonReport
from src.drafting.fixtures import (
    FIXTURE_SIZES,
    FixturePlacement,
    fixture_footprint,
)
from src.drafting.fixtures import _CENTER_ORIGIN as CENTER_ORIGIN

# ── 方位語彙 ────────────────────────────────────────────────────────────────
NORTH, SOUTH, EAST, WEST = "north", "south", "east", "west"
CARDINALS = (NORTH, EAST, SOUTH, WEST)
ANY_WALL = frozenset(CARDINALS)        # 靠哪面牆都行
ANY_FACING = frozenset(CARDINALS)      # 朝哪個方位都行
FREESTANDING = frozenset()             # 不靠牆(自由站立:餐桌/茶几/汽車…)

# 判定「背貼在某面牆上」的容差(mm):原點離該牆這距離內就算靠著。
WALL_TOUCH_MM = 250.0


@dataclass
class FurnitureConstraint(JsonReport):
    """一種家具的擺放偏好規格(軟偏好,非硬碰撞)。

    preferred_wall         可接受靠的牆面集合。ANY_WALL=任意牆;FREESTANDING
                           (空集合)=不靠牆的自由站立家具。
    minimum_clearance      正前方(局部 +Y 側)要留的淨空深度,mm。
    preferred_orientation  正面可接受朝向的方位集合。ANY_FACING=不拘朝向。
    """

    name: str
    preferred_wall: frozenset = ANY_WALL
    minimum_clearance: float = 0.0
    preferred_orientation: frozenset = ANY_FACING

    @property
    def freestanding(self) -> bool:
        """不靠牆的自由站立家具(preferred_wall 為空)。"""
        return not self.preferred_wall

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "preferred_wall": sorted(self.preferred_wall),
            "minimum_clearance": float(self.minimum_clearance),
            "preferred_orientation": sorted(self.preferred_orientation),
            "freestanding": self.freestanding,
        }


# ── 各家具的偏好(常見居家做法;非規範,見 PENDING)─────────────────────────
# 靠牆家具:背靠任意牆,正面朝室內(=靠牆的反向,故 orientation 用 ANY_FACING
# 交給 preferred_wall + 幾何一起約束)。自由站立家具:preferred_wall=FREESTANDING。
FURNITURE_CONSTRAINTS: dict[str, FurnitureConstraint] = {
    # 衛浴:背靠牆,前方留使用/膝部空間
    "toilet": FurnitureConstraint("toilet", ANY_WALL, 500.0),
    "basin": FurnitureConstraint("basin", ANY_WALL, 550.0),
    "bathtub": FurnitureConstraint("bathtub", ANY_WALL, 600.0),
    # 臥室:床頭/櫃背靠牆
    "bed_single": FurnitureConstraint("bed_single", ANY_WALL, 700.0),
    "bed_double": FurnitureConstraint("bed_double", ANY_WALL, 700.0),
    "wardrobe": FurnitureConstraint("wardrobe", ANY_WALL, 600.0),   # 開門+站立
    "nightstand": FurnitureConstraint("nightstand", ANY_WALL, 300.0),
    # 客廳:沙發/電視櫃背靠牆
    "sofa3": FurnitureConstraint("sofa3", ANY_WALL, 900.0),         # 茶几+走道
    "armchair": FurnitureConstraint("armchair", ANY_WALL, 600.0),
    "tv_cabinet": FurnitureConstraint("tv_cabinet", ANY_WALL, 500.0),
    # 玄關/書房/廚房:櫃背靠牆
    "shoe_cabinet": FurnitureConstraint("shoe_cabinet", ANY_WALL, 500.0),
    "desk": FurnitureConstraint("desk", ANY_WALL, 750.0),           # 椅子拉開
    "bookshelf": FurnitureConstraint("bookshelf", ANY_WALL, 750.0),  # 取書/走道
    "fridge": FurnitureConstraint("fridge", ANY_WALL, 750.0),        # 開門+站立
    # 自由站立(不靠牆):餐桌四面拉椅、茶几/吧檯椅、汽車進出
    "table4": FurnitureConstraint("table4", FREESTANDING, 750.0),
    "coffee_table": FurnitureConstraint("coffee_table", FREESTANDING, 300.0),
    "bar_stool": FurnitureConstraint("bar_stool", FREESTANDING, 300.0),
    "car": FurnitureConstraint("car", FREESTANDING, 700.0),          # 開車門
}


def get_constraint(name: str) -> FurnitureConstraint:
    """取某種家具的偏好;未登錄者回一筆寬鬆預設(任意牆、無淨空要求)。"""
    return FURNITURE_CONSTRAINTS.get(name, FurnitureConstraint(name))


# ── 幾何:朝向 / 靠牆 / 淨空區 ──────────────────────────────────────────────
def facing_of(rotation: float) -> str:
    """一件家具的正面(局部 +Y)朝向哪個方位(就近取整為東南西北)。

    0°→北 90°→西 180°→南 270°→東(逆時針,與 fixtures.py 一致)。"""
    a = math.radians(rotation)
    fx, fy = -math.sin(a), math.cos(a)             # (0,1) 逆時針轉 rotation
    if abs(fy) >= abs(fx):
        return NORTH if fy >= 0 else SOUTH
    return EAST if fx >= 0 else WEST


def _room_rect(room):
    xs = [p[0] for p in room.points]
    ys = [p[1] for p in room.points]
    return min(xs), min(ys), max(xs), max(ys)


def wall_against(placement: FixturePlacement, room,
                 tol: float = WALL_TOUCH_MM) -> str | None:
    """這件家具的背貼在房間的哪面牆(貼牆邊原點離最近牆 ≤ tol 才算)。

    自由站立家具(原點在中心)或離牆太遠 → 回 None。"""
    if placement.name in CENTER_ORIGIN:
        return None
    x0, y0, x1, y1 = _room_rect(room)
    ix, iy = placement.insert
    dist = {SOUTH: abs(iy - y0), NORTH: abs(iy - y1),
            WEST: abs(ix - x0), EAST: abs(ix - x1)}
    wall = min(dist, key=dist.get)
    return wall if dist[wall] <= tol else None


def _transform(local, rotation, insert):
    a = math.radians(rotation)
    ca, sa = math.cos(a), math.sin(a)
    ix, iy = insert
    return [(ix + x * ca - y * sa, iy + x * sa + y * ca) for x, y in local]


def clearance_zone(placement: FixturePlacement,
                   constraint: FurnitureConstraint | None = None) -> Polygon:
    """家具正前方要保持淨空的區域。

    靠牆家具:footprint 前緣(局部 +Y = d)往外再拉 minimum_clearance 的矩形。
    自由站立家具:footprint 向外緩衝 minimum_clearance 的一圈(四面都要能用)。
    minimum_clearance=0 → 回空多邊形(該家具不要求前方淨空)。"""
    c = constraint or get_constraint(placement.name)
    clr = c.minimum_clearance
    if clr <= 0:
        return Polygon()
    if c.freestanding or placement.name in CENTER_ORIGIN:
        return Polygon(fixture_footprint(placement)).buffer(clr,
                                                            join_style=2)
    w, d = FIXTURE_SIZES[placement.name]
    half = w / 2
    local = [(-half, d), (half, d), (half, d + clr), (-half, d + clr)]
    return Polygon(_transform(local, placement.rotation, placement.insert))


# ── 查詢結果 ────────────────────────────────────────────────────────────────
@dataclass
class ConstraintResult(JsonReport):
    """一次「這樣擺符不符合這件家具的偏好」的查詢結果。

    satisfied      三項偏好是否全部通過。
    wall_ok        背靠的牆在 preferred_wall 內(自由站立家具:不靠牆即通過)。
    orientation_ok 正面朝向在 preferred_orientation 內。
    clearance_ok   正前方淨空落在房間內、且(給了 engine 時)不被別的家具佔住。
    """

    name: str
    satisfied: bool = True
    wall_ok: bool = True
    orientation_ok: bool = True
    clearance_ok: bool = True
    wall: str = ""
    facing: str = ""
    reason: str = ""

    def __bool__(self) -> bool:
        return self.satisfied

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "satisfied": self.satisfied,
            "wall_ok": self.wall_ok,
            "orientation_ok": self.orientation_ok,
            "clearance_ok": self.clearance_ok,
            "wall": self.wall,
            "facing": self.facing,
            "reason": self.reason,
        }

    def __str__(self) -> str:
        return "OK" if self.satisfied else self.reason


def evaluate_constraint(placement: FixturePlacement, room, *,
                        engine=None,
                        tol: float = OVERLAP_TOL) -> ConstraintResult:
    """這件家具這樣擺,符不符合它自己的擺放偏好?**唯讀**,不改 spec。

    engine:可選的 FurnitureCollisionEngine——給了才檢查「正前方淨空是否被別的
    家具佔住」(重用它已算好的 furniture 障礙);不給只檢查淨空是否落在房間內。
    """
    c = get_constraint(placement.name)
    wall = wall_against(placement, room)
    facing = facing_of(placement.rotation)
    res = ConstraintResult(placement.name, wall=wall or "", facing=facing)

    # 1) 靠牆偏好
    if c.freestanding:
        res.wall_ok = wall is None            # 自由站立家具不該被塞去貼牆
        if not res.wall_ok:
            res.reason = f"{placement.name} 是自由站立家具,不該靠牆({wall})"
    else:
        res.wall_ok = wall in c.preferred_wall
        if not res.wall_ok:
            want = "/".join(sorted(c.preferred_wall))
            res.reason = (f"{placement.name} 背未靠偏好牆面"
                          f"(在 {wall or '無'},偏好 {want})")

    # 2) 朝向偏好
    res.orientation_ok = facing in c.preferred_orientation
    if res.wall_ok and not res.orientation_ok:
        want = "/".join(sorted(c.preferred_orientation))
        res.reason = f"{placement.name} 正面朝 {facing},偏好朝 {want}"

    # 3) 正前方淨空
    zone = clearance_zone(placement, c)
    if not zone.is_empty:
        room_poly = Polygon(room.points)
        if zone.difference(room_poly).area > tol:
            res.clearance_ok = False
            if res.wall_ok and res.orientation_ok:
                res.reason = (f"{placement.name} 正前方淨空不足"
                              f"(需 {c.minimum_clearance:.0f}mm,超出房間)")
        elif engine is not None:
            for f in engine.furniture:
                if f.ref is placement:                 # 別擋到自己
                    continue
                if zone.intersection(f.poly).area > tol:
                    res.clearance_ok = False
                    if res.wall_ok and res.orientation_ok:
                        res.reason = (f"{placement.name} 正前方淨空被"
                                      f"「{f.tag}」佔住")
                    break

    res.satisfied = res.wall_ok and res.orientation_ok and res.clearance_ok
    return res


# =============================================================================
# PENDING(待確認假設彙整)
# =============================================================================
# 1. 各家具的 minimum_clearance(mm)為常見居家/人因做法,非任何規範:衣櫃/
#    冰箱開門+站立 600~750、書桌拉椅 750、沙發前走道+茶几 900、馬桶膝部 500…
#    待確認。
# 2. preferred_wall 目前只分「任意牆(ANY_WALL)」與「不靠牆(FREESTANDING)」
#    兩級,尚未表達「靠特定牆」(例如電視櫃靠窗的對牆、書桌靠窗)——那需要
#    知道窗/門的位置,屬後續 Program Selector 決策,不在家具本身屬性內。
# 3. preferred_orientation 目前多為 ANY_FACING(朝向由靠牆自然決定)。分開留
#    欄位是為了日後「靠任意牆但要面向電視/窗」這類需求;現階段不主動約束。
# 4. 自由站立家具的淨空用「footprint 向外緩衝一圈」近似,四面等距;實際餐桌
#    可能只需三面(一面靠餐邊櫃)。之後接進 Program Selector 再細分。
# =============================================================================
