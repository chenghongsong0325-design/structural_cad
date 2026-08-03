"""家具貼牆修正 —— 把「畫出來卡進牆裡」的家具推回室內。

為什麼會卡進牆:家具的插入點是「貼牆邊中點」,而樣板/擺位器算的是**牆中心線**;
牆有厚度(內牆 120、外牆/界牆 150),中心線到牆面還有半個牆厚。樣板寫死 60(當作
120 的牆)時,一旦那道牆是 150 的界牆,家具就會嵌進牆面 15mm ——
圖面上就是「櫃子穿牆」,plan_check 的 furniture_in_wall 會擋圖。

做法:沿家具的「背面法線」往室內推,一次 10mm,直到不再與牆體重疊為止。
推超過 NUDGE_MAX 還在牆裡 = 這件家具根本擺錯位置(不是貼牆誤差),就移除它
——寧可少一件家具,也不要出一張穿牆的圖。
"""
from __future__ import annotations

import math

from shapely.geometry import LineString, Polygon
from shapely.ops import unary_union

from src.drafting.fixtures import Counter, counter_footprint, fixture_footprint

# 與 plan_check.WALL_OVERLAP_TOL 對齊:重疊小於此視為貼齊誤差,不算穿牆。
OVERLAP_TOL = 1000.0        # mm²
NUDGE_STEP = 10.0           # 每次往室內推的距離(mm)
NUDGE_MAX = 500.0           # 推到這麼多還在牆裡 = 擺錯位置,不是貼牆誤差
                            # (餐桌組含椅子 1.56m,壓到牆時要推的距離比櫃子大)


def _wall_union(spec):
    walls = getattr(spec, "walls", None) or []
    if not walls:
        return None
    return unary_union([
        LineString([w.start, w.end]).buffer(w.thickness / 2.0,
                                            cap_style=2, join_style=2)
        for w in walls])


def _footprint(fx) -> Polygon:
    """家具/流理台的佔地矩形(兩種都要顧:fixtures 清單裡混著 Counter)。

    ⚠️ 用**畫圖尺寸**(fixture_footprint),不是碰撞尺寸:plan_check 的
    furniture_in_wall 判的是「畫出來會不會穿牆」,兩邊要用同一個框,否則
    這裡看不到重疊、關卡卻擋圖(實測餐桌組 1560 vs 碰撞框 900 就是這樣漏掉)。"""
    pts = (counter_footprint(fx) if isinstance(fx, Counter)
           else fixture_footprint(fx))
    return Polygon(pts)


def _overlap(fx, bodies) -> float:
    poly = _footprint(fx)
    if poly.is_empty or bodies is None:
        return 0.0
    return poly.intersection(bodies).area


def _inward(fx) -> tuple:
    """「往室內」的單位向量:家具是區域 +Y 轉旋轉角;流理台是靠牆邊的左手側。"""
    if isinstance(fx, Counter):
        (x1, y1), (x2, y2) = fx.start, fx.end
        L = fx.length or 1.0
        ux, uy = (x2 - x1) / L, (y2 - y1) / L
        return (-uy, ux)
    a = math.radians(getattr(fx, "rotation", 0.0))
    return (-math.sin(a), math.cos(a))


def _shift(fx, dx, dy) -> None:
    if isinstance(fx, Counter):
        fx.start = (fx.start[0] + dx, fx.start[1] + dy)
        fx.end = (fx.end[0] + dx, fx.end[1] + dy)
    else:
        fx.insert = (fx.insert[0] + dx, fx.insert[1] + dy)


def push_fixtures_out_of_walls(spec) -> tuple:
    """把嵌進牆體的家具往室內推;推不出來的移除。回 (推了幾件, 移除了幾件)。

    家具的區域座標是「原點在貼牆邊中點、往 +Y 進入室內」,所以往室內 = 沿旋轉後
    的 +Y 方向;推的過程只動插入點,不改尺寸也不改朝向(仍然貼著同一道牆)。"""
    bodies = _wall_union(spec)
    fixtures = getattr(spec, "fixtures", None)
    if bodies is None or not fixtures:
        return (0, 0)

    moved, dropped, keep = 0, 0, []
    for fx in fixtures:
        if _overlap(fx, bodies) <= OVERLAP_TOL:
            keep.append(fx)
            continue
        others = [_footprint(o) for o in fixtures if o is not fx]
        nx, ny = _inward(fx)
        ok, total, fallback = False, 0.0, None
        for _ in range(int(NUDGE_MAX / NUDGE_STEP)):
            _shift(fx, nx * NUDGE_STEP, ny * NUDGE_STEP)
            total += NUDGE_STEP
            if _overlap(fx, bodies) > OVERLAP_TOL:
                continue
            mine = _footprint(fx)
            if any(mine.intersection(o).area > OVERLAP_TOL for o in others):
                if fallback is None:
                    fallback = total        # 出得了牆,但會碰到別的家具
                continue
            ok = True
            break
        if not ok and fallback is not None:
            # 兩全其美做不到時,**先保證不穿牆**(那是圖面硬錯誤);家具互相碰到
            # 由碰撞引擎(resolve_collisions)接手處理,它會再搬一次。
            _shift(fx, -nx * (total - fallback), -ny * (total - fallback))
            ok = True
        if ok:
            moved += 1
            keep.append(fx)
            continue
        # 沿「背面法線」推不出來 → 再試反方向與沿牆兩側(家具可能是被別的東西
        # 逼到牆裡,不是朝向錯)。真的怎麼推都在牆裡才丟掉。
        _shift(fx, -nx * total, -ny * total)        # 先回原位
        ox, oy = (fx.start if isinstance(fx, Counter) else fx.insert)
        for dx, dy in [(-nx, -ny), (ny, -nx), (-ny, nx)]:
            moved_by = 0.0
            for _ in range(int(NUDGE_MAX / NUDGE_STEP)):
                _shift(fx, dx * NUDGE_STEP, dy * NUDGE_STEP)
                moved_by += NUDGE_STEP
                if _overlap(fx, bodies) <= OVERLAP_TOL:
                    ok = True
                    break
            if ok:
                break
            _shift(fx, -dx * moved_by, -dy * moved_by)
        if ok:
            moved += 1
            keep.append(fx)
        else:                                       # 不是貼牆誤差 → 這件擺錯了
            dropped += 1
    if dropped:
        spec.fixtures = keep
    return (moved, dropped)
