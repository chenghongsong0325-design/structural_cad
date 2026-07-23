"""Room Semantic Engine(v0.7 Phase 6-6)—— 房間功能語意的規則與評分。

一條規則 = RoomSemanticRule(room_kind, required, preferred, forbidden,
min_count, max_count)。評分把一個房間裡的家具清單對到它的規則:

    required    這個房間**必須**有的家具類別(缺一項重扣)。
    preferred   有更好、沒有也不扣(加分項,不強制)。
    forbidden   **不該**出現的家具類別(出現重扣)。
    min_count   家具總數下限(太空扣分)。
    max_count   家具總數上限(太擠扣分)。

家具類別是 canonical type(sofa3→sofa、tv_cabinet→tv、table4→dining_table、
Counter→counter…見 SEM_TYPE);房間類別也做別名歸一(family→living、
single→bedroom、foyer→entrance…見 ROOM_ALIAS)。

⚠️ **獨立、唯讀、純軟分數**:evaluate_room_semantics 只回 0~100 分與 missing/
extra/violations,**不**改 spec、**不**能讓非法佈局變合法。合法與否永遠由
FurnitureCollisionEngine(硬閘門)決定。

⚠️ 各房間的家具期望為常見住宅做法,非任何規範——見模組結尾 PENDING。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from src.design.report import JsonReport
from src.drafting.fixtures import Counter, FixturePlacement

# 扣分權重(分)。
PENALTY_MISSING = 30.0        # 每缺一項 required
PENALTY_FORBIDDEN = 40.0      # 每出現一項 forbidden
PENALTY_UNDER = 20.0          # 家具數 < min_count
PENALTY_OVER = 15.0           # 家具數 > max_count
PENALTY_EXTRA = 5.0           # 每項既非 required 也非 preferred 的意外家具

# 圖塊名 → 房間語意用的 canonical 家具類別。未列者用原名(fallback)。
SEM_TYPE = {
    "sofa3": "sofa", "armchair": "armchair", "tv_cabinet": "tv",
    "coffee_table": "coffee_table", "bar_stool": "bar_stool",
    "bed_single": "bed", "bed_double": "bed", "nightstand": "nightstand",
    "table4": "dining_table",
    "desk": "desk", "bookshelf": "bookshelf", "wardrobe": "wardrobe",
    "fridge": "fridge",
    "toilet": "toilet", "basin": "basin", "bathtub": "bathtub",
    "shoe_cabinet": "shoe_cabinet", "car": "car",
}

# 房間 kind → canonical 房間類別(別名歸一)。
ROOM_ALIAS = {
    "living": "living", "family": "living", "hall": "living",
    "dining": "dining",
    "bedroom": "bedroom", "single": "bedroom", "master": "bedroom",
    "kitchen": "kitchen",
    "bathroom": "bathroom",
    "laundry": "laundry", "utility": "laundry",
    "study": "study",
    "foyer": "entrance", "entrance": "entrance",
    "balcony": "balcony",
}


def placement_type(obj) -> str:
    """一件家具的 canonical 語意類別。"""
    if isinstance(obj, Counter):
        return "counter"
    if isinstance(obj, FixturePlacement):
        return SEM_TYPE.get(obj.name, obj.name)
    if hasattr(obj, "type"):
        return obj.type
    if hasattr(obj, "name"):
        return SEM_TYPE.get(obj.name, obj.name)
    return ""


def canonical_room(kind: str) -> str:
    """房間 kind 的 canonical 類別(別名歸一);未知者原樣回傳。"""
    return ROOM_ALIAS.get(kind, kind)


# ── 規則 ────────────────────────────────────────────────────────────────────
@dataclass
class RoomSemanticRule(JsonReport):
    """一種房間的功能語意規則。"""

    room_kind: str
    required: frozenset = frozenset()
    preferred: frozenset = frozenset()
    forbidden: frozenset = frozenset()
    min_count: int = 0
    max_count: int = 99

    def to_dict(self) -> dict:
        return {
            "room_kind": self.room_kind,
            "required": sorted(self.required),
            "preferred": sorted(self.preferred),
            "forbidden": sorted(self.forbidden),
            "min_count": self.min_count,
            "max_count": self.max_count,
        }


# 預設規則(常見住宅做法,非規範;見 PENDING)。
ROOM_SEMANTIC_RULES: dict[str, RoomSemanticRule] = {
    "living": RoomSemanticRule(
        "living", required=frozenset({"sofa"}),
        preferred=frozenset({"tv", "coffee_table", "armchair"}),
        forbidden=frozenset({"toilet", "bed", "bathtub", "counter"}),
        min_count=1, max_count=8),
    "dining": RoomSemanticRule(
        "dining", required=frozenset({"dining_table"}),
        preferred=frozenset({"bar_stool"}),
        forbidden=frozenset({"toilet", "bed", "bathtub"}),
        min_count=1, max_count=10),
    "bedroom": RoomSemanticRule(
        "bedroom", required=frozenset({"bed"}),
        preferred=frozenset({"wardrobe", "desk", "nightstand"}),
        forbidden=frozenset({"toilet", "bathtub", "sofa", "counter"}),
        min_count=1, max_count=8),
    "kitchen": RoomSemanticRule(
        "kitchen", required=frozenset({"counter", "fridge"}),
        preferred=frozenset(),
        forbidden=frozenset({"bed", "sofa", "toilet", "bathtub"}),
        min_count=1, max_count=12),
    "bathroom": RoomSemanticRule(
        "bathroom", required=frozenset({"toilet"}),
        preferred=frozenset({"basin", "bathtub"}),
        forbidden=frozenset({"bed", "sofa", "fridge", "counter", "desk"}),
        min_count=1, max_count=6),
    "laundry": RoomSemanticRule(
        "laundry", required=frozenset({"washer"}),
        preferred=frozenset({"basin"}),
        forbidden=frozenset({"bed", "sofa", "toilet"}),
        min_count=1, max_count=6),
    "study": RoomSemanticRule(
        "study", required=frozenset({"desk"}),
        preferred=frozenset({"bookshelf"}),
        forbidden=frozenset({"toilet", "bed", "bathtub"}),
        min_count=1, max_count=8),
    "entrance": RoomSemanticRule(
        "entrance", required=frozenset(),
        preferred=frozenset({"shoe_cabinet"}),
        forbidden=frozenset({"bed", "toilet", "sofa", "bathtub"}),
        min_count=0, max_count=4),
    "balcony": RoomSemanticRule(
        "balcony", required=frozenset(),
        preferred=frozenset({"washer"}),
        forbidden=frozenset({"bed", "sofa", "tv", "toilet"}),
        min_count=0, max_count=4),
}


def get_room_rule(kind: str) -> RoomSemanticRule | None:
    """某房間 kind 對應的語意規則(別名歸一後查表);未涵蓋者回 None。"""
    return ROOM_SEMANTIC_RULES.get(canonical_room(kind))


# ── 查詢結果 ────────────────────────────────────────────────────────────────
@dataclass
class RoomSemanticResult(JsonReport):
    """一次房間語意評分的結果。

    score       0~100(滿分起,依 missing/forbidden/count/extra 扣分)。
    missing     缺少的 required 家具類別。
    extra       既非 required 也非 preferred 也非 forbidden 的意外家具類別。
    violations  造成扣分的具體問題(缺必要 / 出現禁止 / 太少 / 太多)的文字。
    """

    room: str = ""
    room_kind: str = ""
    score: float = 100.0
    missing: list = field(default_factory=list)
    extra: list = field(default_factory=list)
    violations: list = field(default_factory=list)

    def __bool__(self) -> bool:
        return not self.violations

    def to_dict(self) -> dict:
        return {
            "room": self.room,
            "room_kind": self.room_kind,
            "score": round(self.score, 1),
            "missing": list(self.missing),
            "extra": list(self.extra),
            "violations": list(self.violations),
            "summary": self.summary(),
        }

    def summary(self) -> str:
        return (f"RoomSemantic:{self.room}({self.room_kind}) {self.score:.0f} 分"
                f" · 缺 {self.missing or '無'}"
                f" · 多 {self.extra or '無'}"
                f" · 違反 {len(self.violations)}")

    def __str__(self) -> str:
        return self.summary()


# ── Evaluator ──────────────────────────────────────────────────────────────
class RoomSemanticEvaluator:
    """對一個房間的家具清單評「功能語意」。**唯讀**:不改任何東西。"""

    def __init__(self, rules: dict[str, RoomSemanticRule] | None = None):
        self.rules = dict(rules) if rules is not None else dict(ROOM_SEMANTIC_RULES)

    def evaluate_room_semantics(self, room, placements) -> RoomSemanticResult:
        """room 這個房間裝了 placements 這些家具,功能語意符不符合?

        room:有 .kind / .name 的房間物件。placements:該房間的家具清單
        (FixturePlacement / Counter;其餘以 type 歸一)。
        """
        kind = canonical_room(getattr(room, "kind", ""))
        res = RoomSemanticResult(room=getattr(room, "name", ""), room_kind=kind)
        rule = self.rules.get(kind)
        if rule is None:                             # 未涵蓋的房間 → 跳過,不扣分
            return res

        present = {placement_type(p) for p in placements}
        count = len(placements)

        missing = sorted(rule.required - present)
        forbidden = sorted(present & rule.forbidden)
        extra = sorted(t for t in present
                       if t not in rule.required and t not in rule.preferred
                       and t not in rule.forbidden)

        score = 100.0
        score -= PENALTY_MISSING * len(missing)
        score -= PENALTY_FORBIDDEN * len(forbidden)
        score -= PENALTY_EXTRA * len(extra)
        if count < rule.min_count:
            score -= PENALTY_UNDER
        if count > rule.max_count:
            score -= PENALTY_OVER

        for m in missing:
            res.violations.append(f"缺必要家具:{m}")
        for f in forbidden:
            res.violations.append(f"不該出現:{f}")
        if count < rule.min_count:
            res.violations.append(f"家具太少({count} < {rule.min_count})")
        if count > rule.max_count:
            res.violations.append(f"家具太多({count} > {rule.max_count})")

        res.missing = missing
        res.extra = extra
        res.score = max(0.0, min(100.0, score))
        return res


# =============================================================================
# PENDING(待確認假設彙整)
# =============================================================================
# 1. 各房間的 required/preferred/forbidden 與 min/max_count 為常見住宅做法,非
#    任何規範:臥室要床、廚房要流理台+冰箱、浴室要馬桶且不該有床/沙發…待確認。
# 2. laundry 的 required washer / balcony 的 preferred washer 目前無對應圖塊
#    (fixtures 沒有 washer),屬宣告式規則;之後補洗衣機圖塊即可生效。
# 3. min/max_count 以「家具件數」計,不分大小;之後可改為依房間坪數的密度門檻。
# =============================================================================
