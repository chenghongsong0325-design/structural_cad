"""Auto Furnish(v0.7 Phase 7.1b)—— 給「只有房間與牆」的平面自動配家具。

BSP 產生的平面(Phase 7.1a)只有房間/牆/門/窗,沒有家具,Phase 6 的家具類子分數
全都評不出東西。本模組補上這段:**依房間用途決定該擺什麼,再用 Phase 6 既有的
FurniturePlacementOptimizer 挑合法且分數最好的位置**。

⚠️ **不自己寫擺放邏輯**:位置一律由 Phase 6 的 optimizer 決定(內含 collision 硬
閘門 + 走道/人體淨空/擺放偏好/家具關聯/房間語意軟分數),本模組只負責「這種房
該有哪些家具」與「逐件依序放」。

⚠️ 逐件重建 optimizer:前一件擺好後會成為下一件看到的障礙,故同房家具不會重疊
(貪婪、逐件更新;與 MultiRoomOptimizer 同策略)。

⚠️ 流理台(Counter)是參數式、非固定圖塊,optimizer 無法挑位,改用「沿廚房最長
邊貼牆」的幾何擺法,並**先於冰箱**擺(讓冰箱看得到它、不會撞上)。

典型用法::

    spec = bsp_to_spec(theta, flags, 20000, 14000, 3)   # 7.1a:只有牆
    furnish_spec(spec)                                   # 7.1b:補家具
    score_report(spec)                                   # Phase 6 就評得出東西了
"""
from __future__ import annotations

from shapely.geometry import Polygon

from src.design.collision.placement_optimizer import (
    FurniturePlacementOptimizer,
    PlacementWeights,
)
from src.design.layout.multi_room_optimizer import ROOM_ORDER
from src.design.semantic.room_semantic import canonical_room
from src.drafting.fixtures import Counter, counter_footprint

# 每種房間該有的家具(依序擺)。tuple = 依序嘗試,第一個放得下的就用
# (例如主臥先試雙人床,放不下退而求其次單人床)。
FURNITURE_PROGRAM: dict[str, list] = {
    "bedroom": [("bed_double", "bed_single"), "wardrobe", "nightstand"],
    "living": ["sofa3", "tv_cabinet", "coffee_table"],
    "dining": ["table4"],
    "kitchen": ["fridge"],                       # 流理台另由 _add_counter 處理
    "bathroom": ["toilet", "basin", "bathtub"],  # 浴缸放不下就自動略過
    "foyer": ["shoe_cabinet"],
    "study": ["desk", "bookshelf"],
}

COUNTER_DEPTH = 600.0        # 流理台檯面深(mm)
COUNTER_INSET = 300.0        # 兩端離牆角留的距離(mm)
COUNTER_MIN_LEN = 1500.0     # 太短就不擺流理台


def _room_priority(room) -> int:
    kind = canonical_room(room.kind)
    return ROOM_ORDER.index(kind) if kind in ROOM_ORDER else len(ROOM_ORDER)


def _add_counter(spec, room) -> bool:
    """沿廚房最長邊貼牆擺一段流理台(檯面往室內側伸)。放得下回 True。"""
    poly = Polygon(room.points)
    x0, y0, x1, y1 = poly.bounds
    w, d = x1 - x0, y1 - y0
    if w >= d:                                   # 沿南牆(往北 +y 伸進室內)
        length = w - 2 * COUNTER_INSET
        if length < COUNTER_MIN_LEN or d < COUNTER_DEPTH * 1.5:
            return False
        start = (x0 + COUNTER_INSET, y0)
        end = (x1 - COUNTER_INSET, y0)           # 方向 +x → 左手側 = +y(室內)
    else:                                        # 沿東牆(往西 -x 伸進室內)
        length = d - 2 * COUNTER_INSET
        if length < COUNTER_MIN_LEN or w < COUNTER_DEPTH * 1.5:
            return False
        start = (x1, y0 + COUNTER_INSET)
        end = (x1, y1 - COUNTER_INSET)           # 方向 +y → 左手側 = -x(室內)
    counter = Counter(start=start, end=end, depth=COUNTER_DEPTH,
                      sink=True, stove=True)
    # 檯面必須落在房間內才算數(L 形廚房的 bbox 可能超出實際房間)。
    if not poly.buffer(1.0).contains(Polygon(counter_footprint(counter))):
        return False
    spec.fixtures.append(counter)
    return True


def furnish_spec(spec, *, weights: PlacementWeights | None = None):
    """就地把 spec 的每個房間配上家具(回傳同一個 spec,方便串接)。

    位置一律由 Phase 6 的 FurniturePlacementOptimizer 決定;擺不下的家具直接略過
    (不硬塞、不產生非法佈局)。"""
    rooms = sorted(spec.rooms, key=_room_priority)
    for room in rooms:
        kind = canonical_room(room.kind)
        if kind == "kitchen":                    # 流理台先擺,冰箱才看得到它
            _add_counter(spec, room)
        for item in FURNITURE_PROGRAM.get(kind, []):
            names = item if isinstance(item, tuple) else (item,)
            for name in names:
                opt = FurniturePlacementOptimizer(spec)
                res = opt.place(name, room, weights=weights)
                if res.found:
                    spec.fixtures.append(res.best.placement())
                    break                        # 這個位置的家具已定,換下一項
    return spec
