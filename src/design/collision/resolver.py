"""resolver —— 修復策略:try_move / try_rotate / try_drop。

三種手段,由 engine 依序嘗試。原則:只在「房間內」動家具,移不動且是裝飾類
才丟;必要家具(床/馬桶/沙發…)永不丟——修不動就留給 validate 安全網報錯。

⚠️ Phase 1 只實作 try_move(沿牆滑動)與 try_drop(丟裝飾);try_rotate 先保留
介面(回 False),第一版不轉家具。這組原語未來 Incremental Placement(每放一件
就避讓)會重用,不必重寫。
"""
from __future__ import annotations

import math

from shapely.geometry import Polygon

from src.design.collision.detector import (
    COLUMN_TOLERANCE_MM,
    OVERLAP_TOL,
    WALL_TOLERANCE_MM,
    penetration,
)
from src.design.collision.priority import is_droppable
from src.drafting.fixtures import (
    FixturePlacement,
    fixture_collision_footprint,
    fixture_footprint,
)

# 移動搜尋:步長與最大滑動距離(mm)。沿牆滑動找不撞的位置。
MOVE_STEP = 100.0
MOVE_MAX_REACH = 1500.0


def _candidate_offsets(rotation: float, step: float, max_reach: float) -> list:
    """由近到遠的候選位移,每個距離**先沿牆(局部 X)、再垂直牆(局部 Y)**。

    沿牆優先 → 家具盡量留在原牆邊滑動(貼牆語意);沿牆滑不開才往房內垂直
    移(這是把「穿牆家具拉回房內」的必要方向——1D 沿牆解不了穿牆)。"""
    a = math.radians(rotation)
    ax, ay = math.cos(a), math.sin(a)          # 沿牆方向(局部 X)
    px, py = -ay, ax                           # 垂直牆方向(局部 Y)
    out = []
    for k in range(1, int(max_reach / step) + 1):
        d = k * step
        out.append((ax * d, ay * d))           # 沿牆 +
        out.append((-ax * d, -ay * d))         # 沿牆 -
        out.append((px * d, py * d))           # 垂直 +(往房內)
        out.append((-px * d, -py * d))         # 垂直 -
    return out


def try_move(ob, blockers, tol: float = OVERLAP_TOL,
             wall_tol: float = WALL_TOLERANCE_MM,
             step: float = MOVE_STEP, max_reach: float = MOVE_MAX_REACH,
             columns=(), col_tol: float = COLUMN_TOLERANCE_MM) -> bool:
    """在房間內滑動家具,找到「不撞 blockers、不穿牆、不壓柱」的位置。成功就
    **就地改** ob.ref.insert / ob.poly / ob.collision_poly,回 True;找不到回 False。
    Counter(流理台/中島)Phase 1 不移。

    blockers:此家具必須避開的多邊形(其他家具 + 門迴轉 + 天井 + 樓梯,**不含
    牆與柱**)。牆與柱都不能用面積重疊判——家具貼牆、以及貼牆時壓到柱伸進室內
    的半邊,都是合法的:
      * 穿牆 = 碰撞 footprint 突出所屬房間面積 > wall_tol(貼牆微量突出仍合法)。
      * 壓柱 = 完整 footprint 穿入柱的**深度** > col_tol(v0.6 Phase 4);
        columns 傳空序列(預設)時完全不檢查,行為與 Phase 3 相同。"""
    fx = ob.ref
    if not isinstance(fx, FixturePlacement):
        return False
    room = ob.room
    for dx, dy in _candidate_offsets(fx.rotation, step, max_reach):
        ni = (fx.insert[0] + dx, fx.insert[1] + dy)
        moved = FixturePlacement(fx.name, ni, fx.rotation)
        poly = Polygon(fixture_footprint(moved))
        cpoly = Polygon(fixture_collision_footprint(moved))
        if room is not None and cpoly.difference(room).area > wall_tol:
            continue                           # 移到那裡會穿牆 → 不行
        if any(penetration(poly, c) > col_tol for c in columns):
            continue                           # 移到那裡會壓柱 → 不行
        if any(poly.intersection(b).area > tol for b in blockers):
            continue                           # 撞到其他家具/門/天井/梯 → 不行
        fx.insert, ob.poly, ob.collision_poly = ni, poly, cpoly
        return True
    return False


def try_rotate(ob, blockers) -> bool:      # noqa: ARG001
    """(Phase 1 介面保留,不實作)日後可試 90° 對牆轉向再檢查。"""
    return False


def try_drop(ob) -> bool:
    """裝飾家具(優先度 ≤ DROP_MAX)可丟——回 True 由 engine 從 fixtures 移除;
    必要家具回 False(不丟)。"""
    return is_droppable(ob.tag)
