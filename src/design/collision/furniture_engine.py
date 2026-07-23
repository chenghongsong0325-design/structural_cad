"""FurnitureCollisionEngine(v0.7 Phase 6-1)—— 家具「放置前」的可行性查詢。

⚠️ **這不是第二套碰撞偵測**。v0.6 的 Collision Engine 是**事後修復**
(生成完 → 偵測 → 移動/丟棄);本模組是**事前查詢**:

    「我想把這件家具放在這裡,可以嗎?」→ CollisionResult(valid, reason, overlap_area)

兩者共用同一組 Obstacle provider(`geometry.py`)與同一組容差(`detector.py`),
**不重複實作判定邏輯**——差別只在「問法」不同:事後修復問「哪裡撞了」,
事前查詢問「這個位置行不行,不行是為什麼」。

檢查項目(依嚴重度排序,第一個不通過就回報):

    wall     家具不得越過所屬房間邊界        突出面積 > WALL_TOLERANCE_MM
    column   家具不得壓在柱上                穿入深度 > COLUMN_TOLERANCE_MM
    door     家具不得擋門的迴轉範圍          重疊面積 > OVERLAP_TOL
    stair    家具不得壓在梯段上              重疊面積 > OVERLAP_TOL
    void     家具不得掉進天井/挑空           重疊面積 > OVERLAP_TOL
    window   **高家具**不得擋窗              重疊面積 > OVERLAP_TOL
    furniture 家具不得與既有家具重疊          重疊面積 > OVERLAP_TOL

⚠️ window 只對 TALL_FIXTURES 生效。實測 100 層:把窗前淨空當成通用規則會擋掉
369~449 件家具——**床頭靠窗、沙發靠窗、流理台在窗下(水槽對窗)都是正確擺法**。
只有高櫃擋窗才是真問題。

⚠️ 本模組**不接進生成流程**,也沒有把 WINDOW 加進 `collect_active`,故對既有
輸出零影響。

典型用法::

    from src.design.collision.furniture_engine import FurnitureCollisionEngine
    from src.drafting.fixtures import FixturePlacement

    engine = FurnitureCollisionEngine(spec)
    res = engine.check(FixturePlacement("wardrobe", (3000, 4200), 0))
    if not res.valid:
        print(res.reason, res.overlap_area, res.detail)
"""
from __future__ import annotations

from dataclasses import dataclass

from shapely.geometry import Polygon

from src.design.collision.detector import (
    COLUMN_TOLERANCE_MM,
    OVERLAP_TOL,
    WALL_TOLERANCE_MM,
    penetration,
)
from src.design.collision.geometry import (
    _containing_room,
    _room_polys,
    column_obstacles,
    door_swing_obstacles,
    fixture_obstacles,
    stair_obstacles,
    void_obstacles,
    wall_obstacles,
    window_obstacles,
)
from src.design.report import JsonReport
from src.drafting.fixtures import (
    Counter,
    FixturePlacement,
    counter_footprint,
    fixture_collision_footprint,
    fixture_footprint,
)

# 會擋光/擋景的「高」家具——只有這些才受窗前淨空約束。
# 床/沙發/流理台/馬桶等低於窗台,靠窗擺是正常且常見的做法,不列入。
TALL_FIXTURES = frozenset({
    "wardrobe", "bookshelf", "fridge", "shoe_cabinet",
})

# 窗前淨空要被擋掉多少比例才算「擋窗」(佔該窗淨空區面積的比例)。
# ⚠️ 不能用 OVERLAP_TOL(100mm²)那種絕對門檻:實測 100 層有 28 件衣櫃「擦到」
# 窗前淨空,重疊 2772~12673mm²,換算只有 9~42mm 的櫃寬,佔窗淨空區不到 4%
# ——那是家具排在窗戶旁邊的擦邊,不是擋窗。取 0.20:遮住兩成以上才算真的擋。
WINDOW_BLOCK_RATIO = 0.20

# 檢查順序:先擋最嚴重的(越界/壓柱),再擋機能性的(門/梯/天井),
# 最後才是軟性的(擋窗)與家具互撞。
REASON_WALL = "wall"
REASON_COLUMN = "column"
REASON_DOOR = "door_swing"
REASON_STAIR = "stair"
REASON_VOID = "void"
REASON_WINDOW = "window_clearance"
REASON_FURNITURE = "furniture"


@dataclass
class CollisionResult(JsonReport):
    """一次「能不能放」的查詢結果。

    valid        True = 可以放;False = 不行
    reason       不行的原因(REASON_* 之一);可以放時為空字串
    overlap_area 造成不通過的量(mm²)。牆是「突出房間的面積」、柱是「穿入
                 深度換算的交集面積」、其餘是「交集面積」;通過時為 0
    """

    valid: bool = True
    reason: str = ""
    overlap_area: float = 0.0
    obstacle_tag: str = ""
    detail: str = ""

    def __bool__(self) -> bool:                     # if engine.check(fx): ...
        return self.valid

    def to_dict(self) -> dict:
        return {
            "valid": self.valid,
            "reason": self.reason,
            "overlap_area": round(self.overlap_area, 1),
            "obstacle_tag": self.obstacle_tag,
            "detail": self.detail,
        }

    def __str__(self) -> str:
        if self.valid:
            return "OK"
        return (f"{self.reason}:{self.detail}"
                f"(overlap {self.overlap_area:.0f}mm²)")


def _footprints(placement):
    """(畫圖用完整 footprint, 碰撞判定用 footprint)。"""
    if isinstance(placement, Counter):
        poly = Polygon(counter_footprint(placement))
        return poly, poly
    return (Polygon(fixture_footprint(placement)),
            Polygon(fixture_collision_footprint(placement)))


def _tag_of(placement) -> str:
    return "counter" if isinstance(placement, Counter) else placement.name


class FurnitureCollisionEngine:
    """對一份 FloorPlanSpec 提供「家具放置前」的碰撞查詢。**唯讀**:
    不修改 spec,只回答可不可以。

    障礙集合在建構時算一次,之後每次 check() 都重用——連續試放很多候選位置
    時不會重算牆/柱/門/梯/天井/窗。
    """

    def __init__(self, spec, *, window_check: bool = True):
        self.spec = spec
        self.window_check = window_check
        self.room_polys = _room_polys(spec)
        self.walls = wall_obstacles(spec)
        self.columns = column_obstacles(spec)
        self.doors = door_swing_obstacles(spec)
        self.stairs = stair_obstacles(spec)
        self.voids = void_obstacles(spec)
        self.windows = window_obstacles(spec)
        self.furniture = fixture_obstacles(spec)

    # ── 單件查詢 ──────────────────────────────────────────────────────────
    def check(self, placement, *, ignore=None,
              tol: float = OVERLAP_TOL,
              wall_tol: float = WALL_TOLERANCE_MM,
              col_tol: float = COLUMN_TOLERANCE_MM) -> CollisionResult:
        """這件家具放在這個位置可不可以?

        ignore:要略過比對的既有家具物件(通常是「這件家具自己」,用於檢查
        既有擺放時避免自己撞自己)。
        """
        poly, cpoly = _footprints(placement)
        tag = _tag_of(placement)

        # 1) 牆:不得越過所屬房間邊界
        room = _containing_room(poly.centroid, self.room_polys)
        if room is not None:
            out = cpoly.difference(room).area
            if out > wall_tol:
                return CollisionResult(
                    False, REASON_WALL, out, "所屬房間",
                    f"{tag} 超出房間邊界(穿牆)")
        else:
            return CollisionResult(
                False, REASON_WALL, poly.area, "(無)",
                f"{tag} 的位置不在任何房間內")

        # 2) 柱:穿入深度
        for c in self.columns:
            pen = penetration(poly, c.poly)
            if pen > col_tol:
                return CollisionResult(
                    False, REASON_COLUMN, poly.intersection(c.poly).area,
                    c.tag, f"{tag} 壓在柱上(穿入 {pen:.0f}mm)")

        # 3~5) 門迴轉 / 樓梯 / 天井:硬障礙,重疊即不可
        for obs, reason in ((self.doors, REASON_DOOR),
                            (self.stairs, REASON_STAIR),
                            (self.voids, REASON_VOID)):
            for o in obs:
                area = poly.intersection(o.poly).area
                if area > tol:
                    return CollisionResult(
                        False, reason, area, o.tag,
                        f"{tag} 與「{o.tag}」重疊")

        # 6) 窗前淨空:只擋高家具,且要遮住足夠比例才算(擦邊不算)
        if self.window_check and tag in TALL_FIXTURES:
            for o in self.windows:
                area = poly.intersection(o.poly).area
                zone = o.poly.area
                if zone > 0 and area / zone > WINDOW_BLOCK_RATIO:
                    return CollisionResult(
                        False, REASON_WINDOW, area, o.tag,
                        f"{tag} 是高家具,遮住窗前淨空 "
                        f"{area / zone * 100:.0f}%")

        # 7) 既有家具
        skip = set()
        if ignore is not None:
            skip = {id(x) for x in (ignore if isinstance(ignore, (list, tuple,
                                                                  set))
                                    else [ignore])}
        for f in self.furniture:
            if id(f.ref) in skip:
                continue
            area = poly.intersection(f.poly).area
            if area > tol:
                return CollisionResult(
                    False, REASON_FURNITURE, area, f.tag,
                    f"{tag} 與既有家具「{f.tag}」重疊")

        return CollisionResult(True)

    # ── 便利查詢 ──────────────────────────────────────────────────────────
    def can_place(self, name: str, insert, rotation: float = 0.0,
                  **kw) -> CollisionResult:
        """用名稱/座標直接問,省得自己組 FixturePlacement。"""
        return self.check(FixturePlacement(name, insert, rotation), **kw)

    def check_existing(self) -> list[tuple[str, CollisionResult]]:
        """回頭檢查 spec 裡**既有**的每一件家具(每件都略過自己)。

        合格的圖應該全部 valid——這是用來驗證「事前查詢」與 v0.6「事後修復」
        對同一張圖的判斷一致。"""
        out = []
        for fx in self.spec.fixtures:
            res = self.check(fx, ignore=fx)
            out.append((_tag_of(fx), res))
        return out
