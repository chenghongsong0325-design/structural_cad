"""Layout-level scoring(v0.7 Phase 6-7)—— 整棟/整層住宅的總評分。

Phase 6-1~6-6 都在評「單一家具擺得好不好」;本層往上一級,評「**整張圖**好不
好」:把碰撞、走道、人體淨空、家具偏好、房間語意、空間效率、採光……收斂成一個
overall_score 與 grade(A+~D),並可對多份 Layout 排序、算平均、出 JSON/CSV。

⚠️ 唯讀、不接進生成流程:只讀 spec 算分,不改任何東西。
"""
from __future__ import annotations

from src.design.layout.global_score import (
    DEFAULT_LAYOUT_WEIGHTS,
    SCORE_ITEMS,
    LayoutBenchmark,
    LayoutScore,
    LayoutScoreEngine,
    grade_of,
)
from src.design.layout.multi_room_optimizer import (
    ROOM_ORDER,
    MultiRoomOptimizer,
    MultiRoomResult,
    RoomScore,
)

__all__ = [
    "LayoutScore",
    "LayoutScoreEngine",
    "LayoutBenchmark",
    "DEFAULT_LAYOUT_WEIGHTS",
    "SCORE_ITEMS",
    "grade_of",
    "MultiRoomOptimizer",
    "MultiRoomResult",
    "RoomScore",
    "ROOM_ORDER",
]
