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

from src.design.collision.geometry import door_swing_obstacles
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


MIN_COUNTER_LEN = 1200.0    # 截短到比這還短 = 不成一段廚具,改用整排外推
COLUMN_NUDGE_MAX = 400.0    # 為了閃柱最多挪這麼多(500 的舊柱凸 190,夠用)


def clear_fixtures_off_columns(spec) -> int:
    """把壓在柱上的家具挪開;**挪不開就不動**。回傳挪動了幾件。

    ⚠️ 這是「盡力而為」,不是硬性修復 —— 三件事的嚴重程度不一樣:

        穿牆   圖面硬錯誤,一定要修(即使代價是家具離牆 19cm)
        擋門   人進不去,一定不能製造出來
        壓柱   醜、家具擺不進去,但**忍得住**(柱藏在牆內時本來就會有接觸)

    所以順序是 **穿牆 > 擋門 > 壓柱**:挪開柱之後如果反而撞到牆、別的家具或
    門的迴轉,就整件退回原位,寧可留著壓柱。第一版把柱直接併進 `_wall_union`
    (= 壓柱與穿牆同等級),結果 18.4×14.5m/seed4242 的鞋櫃為了閃柱退進門的
    開啟弧裡,整份設計被檢核否決 —— 那是把「忍得住」的事修成「不能忍」的事。

    嘗試順序:先沿牆兩側平移(柱只佔牆的一小段,讓到旁邊最自然,家具也還
    貼著牆),都不行再往室內推(沙發離牆 10cm 是可以接受的樣子)。
    """
    cols = _column_polys(spec)
    fixtures = getattr(spec, "fixtures", None)
    if not cols or not fixtures:
        return 0

    walls = _wall_union(spec)
    swings = [o.poly for o in door_swing_obstacles(spec)]
    moved = 0
    for fx in fixtures:
        if isinstance(fx, Counter):
            continue                    # 廚具走截短那條路,不平移
        if sum(_footprint(fx).intersection(c).area for c in cols) <= OVERLAP_TOL:
            continue
        others = [_footprint(o) for o in fixtures if o is not fx]
        nx, ny = _inward(fx)
        # 沿牆(法線轉 90°)兩側 → 最後才往室內
        dirs = [(-ny, nx), (ny, -nx), (nx, ny)]
        placed = False
        for dx, dy in dirs:
            gone = 0.0
            for _ in range(int(COLUMN_NUDGE_MAX / NUDGE_STEP)):
                _shift(fx, dx * NUDGE_STEP, dy * NUDGE_STEP)
                gone += NUDGE_STEP
                mine = _footprint(fx)
                if sum(mine.intersection(c).area for c in cols) > OVERLAP_TOL:
                    continue
                if walls is not None and mine.intersection(walls).area > OVERLAP_TOL:
                    continue
                if any(mine.intersection(o).area > OVERLAP_TOL
                       for o in others + swings):
                    continue
                placed = True
                break
            if placed:
                # 記下「挪了多少、挪到哪」:柱之後會縮細並外推,那時 settle_
                # fixtures_to_wall 要能沿原路把它還原(見該函式說明)。
                setattr(fx, DODGE_MARK,
                        (dx * gone, dy * gone,
                         fx.start if isinstance(fx, Counter) else fx.insert))
                break
            _shift(fx, -dx * gone, -dy * gone)          # 這個方向不行 → 退回
        if placed:
            moved += 1
    return moved


DODGE_MARK = "_column_dodge"        # (dx, dy, 當時的位置):閃柱挪了多少、挪到哪


def settle_fixtures_to_wall(spec) -> int:
    """把「為了閃柱而挪開、但柱後來自己躲掉了」的家具還原;回傳還原了幾件。

    為什麼需要這一道 —— **順序問題**:擺家具的時候柱還是舊的 500、還沒外推,
    貼牆家具為了閃柱讓開了一段;等 `column_design.apply_column_design` 把柱縮細
    又推到室外,讓路的理由就沒了,沒人叫它回來就會停在半空中(實測 19×13 的沙發
    離牆 18cm,圖上看起來像擺錯)。

    ⚠️ **只還原自己挪過的,而且只在它之後沒被別人動過的時候。**
       第一版是「所有家具都盡量往牆邊推」,結果把 `_declutter_for_circulation`
       特地挪開讓出通道的家具又推回去 —— 淺透天掃描冒出 4 案 circulation_blocked。
       挪動家具的模組不只一個,誰都不該去動別人的決定。
    """
    fixtures = getattr(spec, "fixtures", None)
    if not fixtures:
        return 0
    walls = _wall_union(spec)
    if walls is None:
        return 0
    cols = _column_polys(spec)
    swings = [o.poly for o in door_swing_obstacles(spec)]

    # ⚠️ 還原是「錦上添花」,不能把圖弄壞。挪動家具的模組不只一個,而閃柱發生在
    #    動線修復器之前 —— 家具一還原,它當初批准的通道就變了。所以先拍快照、
    #    還原完再驗一次動線與門的迴轉,只要比還原前差就**整層退回去**。
    #    (實測:少了這道,淺透天冒出 circulation_blocked、AI 版冒出
    #     door_swing_blocked 而整份設計被 422 擋掉。使用者說沙發離牆 10cm 也可以,
    #     所以退回去是完全可接受的結果 —— 寧可醜一點,不要壞掉。)
    snapshot = [(fx, fx.start if isinstance(fx, Counter) else fx.insert)
                for fx in fixtures if getattr(fx, DODGE_MARK, None)]
    if not snapshot:
        return 0
    before = _plan_faults(spec)

    settled = 0
    for fx in fixtures:
        mark = getattr(fx, DODGE_MARK, None)
        if mark is None:
            continue
        dx, dy, at = mark
        try:
            delattr(fx, DODGE_MARK)             # 一次性:還原過就不再試
        except AttributeError:
            pass
        if (fx.start if isinstance(fx, Counter) else fx.insert) != at:
            continue                            # 之後被別人動過 → 那是別人的決定
        others = [_footprint(o) for o in fixtures if o is not fx]
        blockers = others + swings + cols
        steps = int(max(abs(dx), abs(dy)) / NUDGE_STEP)
        ux, uy = (dx / steps / NUDGE_STEP, dy / steps / NUDGE_STEP) if steps else (0, 0)
        back = 0
        for _ in range(steps):                  # 沿原路走回去,走得回多少算多少
            _shift(fx, -ux * NUDGE_STEP, -uy * NUDGE_STEP)
            mine = _footprint(fx)
            if (mine.intersection(walls).area > OVERLAP_TOL
                    or any(mine.intersection(b).area > OVERLAP_TOL
                           for b in blockers)):
                _shift(fx, ux * NUDGE_STEP, uy * NUDGE_STEP)    # 這一步過頭 → 退回
                break
            back += 1
        if back:
            settled += 1

    if settled and _plan_faults(spec) > before:      # 還原反而弄壞了 → 整層退回
        for fx, pos in snapshot:
            if isinstance(fx, Counter):
                dx, dy = pos[0] - fx.start[0], pos[1] - fx.start[1]
                _shift(fx, dx, dy)
            else:
                fx.insert = pos
        return 0
    return settled


def _plan_faults(spec) -> int:
    """這層現在有幾條「家具惹出來的」硬錯誤(動線不通 / 擋住門的迴轉)。

    只數這兩類:還原家具只可能影響它們,其他規則(沒門、樓上外門⋯⋯)與家具無關,
    數進來只是白花時間。⚠️ 檢查本身壞掉不該讓還原變成「一定退回」,所以吞例外
    回 0(= 當作沒問題),頂多少還原幾件家具。
    """
    try:
        from src.design.layout.plan_check import check_floor
        return sum(1 for i in check_floor(spec)
                   if i.severity == "error"
                   and i.code in ("circulation_blocked", "door_swing_blocked"))
    except Exception:
        return 0


def _column_polys(spec) -> list:
    from src.design.column_design import column_footprints
    return column_footprints(spec)


def trim_counters_at_columns(spec) -> int:
    """流理台被柱角咬到**端部**時,把檯面截短讓開;回傳截短了幾段。

    為什麼流理台要另外處理:它是**貼牆的固定廚具**,不是可以隨手挪的家具。
    整排往室內推 10cm 既不像真的(檯面後面留一道縫),還會頂到旁邊的冰箱 ——
    師傅遇到柱是**把那一段切短**。實測 5.4m 的檯面兩端各被咬掉約 10cm,
    截短後仍有 5.2m,完全夠用。

    柱剛好落在檯面**中段**時截不了(會把一段切成兩段),就不動它,留給
    push_fixtures_out_of_walls 用整排外推收尾(那才是真的沒別的辦法)。
    """
    cols = _column_polys(spec)
    fixtures = getattr(spec, "fixtures", None)
    if not cols or not fixtures:
        return 0

    trimmed = 0
    for fx in fixtures:
        if not isinstance(fx, Counter):
            continue
        for _ in range(len(cols)):          # 兩端可能各咬一根,逐根收
            (x1, y1), (x2, y2) = fx.start, fx.end
            L = fx.length
            ux, uy = (x2 - x1) / L, (y2 - y1) / L
            foot = _footprint(fx)
            cut = None
            for c in cols:
                inter = foot.intersection(c)
                if inter.is_empty or inter.area <= OVERLAP_TOL:
                    continue
                ts = [((px - x1) * ux + (py - y1) * uy)
                      for px, py in inter.exterior.coords]
                t0, t1 = max(0.0, min(ts)), min(L, max(ts))
                if t0 <= 1.0:                       # 咬在起點端 → 起點往前縮
                    cut = ("start", t1)
                elif t1 >= L - 1.0:                 # 咬在終點端 → 終點往回縮
                    cut = ("end", t0)
                if cut:
                    break
            if cut is None:
                break
            side, t = cut
            new_len = (L - t) if side == "start" else t
            if new_len < MIN_COUNTER_LEN:
                break                               # 截到不成廚具 → 交給外推
            if side == "start":
                fx.start = (x1 + ux * t, y1 + uy * t)
            else:
                fx.end = (x1 + ux * t, y1 + uy * t)
            trimmed += 1
    return trimmed


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
    trim_counters_at_columns(spec)      # 貼牆廚具遇柱先截短(不是整排推開)
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
    # 牆的問題收完(硬錯誤優先)之後,再盡力把壓在柱上的家具挪開。
    # ⚠️ 還原(settle_fixtures_to_wall)**不在這裡做** —— 要等柱定案(縮細+外推)
    #    之後才知道還需不需要讓路,呼叫點在 column_design.apply_column_design。
    clear_fixtures_off_columns(spec)
    return (moved, dropped)
