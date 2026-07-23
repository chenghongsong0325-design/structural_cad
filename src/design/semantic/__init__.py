"""Room Semantic(v0.7 Phase 6-6)—— 讓 Layout 理解「房間的功能」,不只家具碰撞。

碰撞/淨空/關聯這些 Phase 6 模組談的都是「家具擺得對不對」;本層談的是更上位的
「這個房間**該有什麼、不該有什麼**」:臥室要有床、廚房要有流理台與冰箱、浴室
不該出現床。RoomSemanticEvaluator 給每個房間一個 0~100 的語意分數,列出缺的
(missing)、多餘的(extra)與違規的(violations)。

⚠️ 純軟分數、唯讀:不改 spec、不接進生成流程,合法與否仍由 collision 硬閘門決定。
"""
from __future__ import annotations

from src.design.semantic.room_semantic import (
    ROOM_SEMANTIC_RULES,
    RoomSemanticEvaluator,
    RoomSemanticResult,
    RoomSemanticRule,
    canonical_room,
    get_room_rule,
    placement_type,
)

__all__ = [
    "RoomSemanticRule",
    "RoomSemanticEvaluator",
    "RoomSemanticResult",
    "ROOM_SEMANTIC_RULES",
    "canonical_room",
    "get_room_rule",
    "placement_type",
]
