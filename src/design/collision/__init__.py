"""Furniture Collision Engine(v0.6 Phase 1)—— 家具碰撞的偵測與修復,獨立模組。

定位:插在「家具生成」與「validate_spec」之間。目前引擎的碰撞處理是
「validate 偵測到就整份設計失敗」+「各放置函式手刻守門」;本模組把碰撞
變成「有系統地偵測 → 主動修復(移動/丟棄裝飾家具)」,validate 退為安全網。

抽象核心是 Obstacle(不是 Furniture vs Furniture):牆/柱/門迴轉/窗/樓梯/
天井/停車/保留區/家具都能包成 Obstacle,detector 只認「movable vs 其他」,
未來加新障礙 = 加一個 provider,detector/resolver 不必改。

模組(依 v0.6 Phase 1 架構):
    obstacle.py   Obstacle 資料模型 + ObstacleKind
    geometry.py   FloorPlanSpec 各元素 → Obstacle 的 provider
    detector.py   偵測 movable vs 其他的重疊 → Collision 清單
    priority.py   家具優先序 / 可移動 / 可丟棄(Step 2)
    resolver.py   try_move / try_rotate / try_drop(Step 2)
    engine.py     CollisionEngine:collect → check → resolve(Step 3)

對外入口(choke point 呼叫這個):
    resolve_collisions(spec) -> ResolveReport   （Step 3 才接進生成流程)

⚠️ Phase 1 的偵測範圍刻意「與 validate_spec 現有檢核完全一致」(家具×家具、
家具×門迴轉),確保接進流程後對現有合格案例零改動(零 regression)。其餘
障礙 provider 已實作、備而不用,之後再逐步納入。
"""
from __future__ import annotations

from src.design.collision.detector import (
    Collision,
    collision_problems,
    column_contacts,
    find_collisions,
)
from src.design.collision.engine import (
    CollisionEngine,
    ResolveReport,
    resolve_collisions,
)
from src.design.collision.furniture_engine import (
    TALL_FIXTURES,
    CollisionResult,
    FurnitureCollisionEngine,
)
from src.design.collision.obstacle import Obstacle
from src.design.collision.placement_optimizer import (
    FurniturePlacementOptimizer,
    PlacementCandidate,
    PlacementResult,
    PlacementWeights,
)

__all__ = [
    "Obstacle", "Collision", "find_collisions", "collision_problems",
    "column_contacts",
    "CollisionEngine", "ResolveReport", "resolve_collisions",
    "FurnitureCollisionEngine", "CollisionResult", "TALL_FIXTURES",
    "FurniturePlacementOptimizer", "PlacementResult", "PlacementCandidate",
    "PlacementWeights",
]
