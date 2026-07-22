"""detector —— 偵測碰撞(只找,不改)。

兩個入口:
  * find_collisions(obstacles):通用原語,回 Collision 清單(engine/resolver 用)。
  * collision_problems(spec):**與 validate_spec 現有家具檢核逐字一致**的問題
    清單——同順序、同閾值(area>100)、同訊息,將來讓 validate 改呼叫這個
    (單一來源),先不動 validate。
"""
from __future__ import annotations

from dataclasses import dataclass

from src.design.collision.geometry import (
    column_obstacles,
    door_swing_obstacles,
    fixture_obstacles,
)
from src.design.collision.obstacle import (
    COLUMN,
    DOOR_SWING,
    STAIR,
    VOID,
    WALL,
    Obstacle,
)

# 「硬障礙」:家具完全不得壓上去的靜態禁區(與門迴轉同語意,area > tol)。
# 天井/挑空沒有樓板、樓梯是垂直動線——壓到多少就錯多少,沒有「貼著算合法」
# 的餘地(這正是它們與 WALL 的差別:牆要用突出判定,家具貼牆是合法的)。
HARD_KINDS = (VOID, STAIR)

# 重疊面積門檻(mm²)——與 validate_spec 一致(>100 才算撞,避免邊緣觸碰誤判)。
OVERLAP_TOL = 100.0

# 穿牆容差(mm²,v0.6 Phase 2,可配置)——家具 footprint 突出所屬房間多邊形
# (=牆中心線)超過此面積才算「穿牆」。實測(34 案 941 件家具,table4 用收緊
# 碰撞 footprint 後):正常貼牆家具突出 ≤1mm²、真穿牆 ≥170000mm²,兩者相差
# 五個數量級。取 5000 留裕度:貼牆的微量突出/浮點誤差穩過,真穿牆穩抓。
WALL_TOLERANCE_MM = 5000.0

# 壓柱容差(v0.6 Phase 3-3,可配置)——⚠️ 單位是**穿入深度 mm**(不是面積,
# 與上面 WALL_TOLERANCE_MM 的 mm² 不同)。柱藏在牆內,家具貼牆必然壓到柱伸進
# 室內的半邊:Step 0 實測 34 案 941 件家具,283 件(30%)如此,穿入最深 175mm
# (理論上限 = 柱半 250 − 內牆半厚 60 = 190mm),全部合法。取 300 = 柱 500×0.6,
# 高於合法上限 190 仍留 110mm 裕度;低於 200 會誤判那 283 件合法貼牆家具。
COLUMN_TOLERANCE_MM = 300.0


@dataclass
class Collision:
    """一組碰撞:a 一定 movable(家具);b 是另一件家具或 static 障礙。"""

    a: Obstacle
    b: Obstacle
    area: float


def penetration(a, b) -> float:
    """a 穿入 b 的深度(mm)= 交集矩形的較短邊;無交集回 0。

    家具旋轉皆 90° 倍數、柱為軸對齊方形 → 交集是軸對齊矩形,較短邊正好是
    垂直穿入的深度。這是柱專用的線性判準(面積會被家具尺寸稀釋:大沙發貼牆
    的合法交集面積遠大於小家具真的壓在柱上,用面積分不開)。"""
    inter = a.intersection(b)
    if inter.is_empty or inter.area <= 0:
        return 0.0
    minx, miny, maxx, maxy = inter.bounds
    return min(maxx - minx, maxy - miny)


def find_collisions(obstacles: list[Obstacle], tol: float = OVERLAP_TOL,
                    wall_tol: float = WALL_TOLERANCE_MM,
                    col_tol: float = COLUMN_TOLERANCE_MM) -> list[Collision]:
    """通用偵測。五類碰撞(a 一定 movable):
      * movable × movable  —— 交集面積 > tol(家具互撞)。
      * movable × 門迴轉    —— 交集面積 > tol(壓門)。
      * movable × 硬障礙    —— 交集面積 > tol(HARD_KINDS:天井 Phase 3-1、
        樓梯 Phase 3-2)。正向禁區,與門迴轉同語意用 area>tol,不用牆的突出
        判定——沒有樓板/是垂直動線,壓到多少就錯多少。
      * movable × 牆        —— **突出所屬房間面積 > wall_tol**(穿牆);
        用 collision_poly(桌椅組收緊),不是 area>100。這是 kind=WALL 專用判定,
        不影響家具×家具/門迴轉/硬障礙的判定(仍 area>tol)。
      * movable × 柱        —— **穿入深度 > col_tol**(壓柱;v0.6 Phase 3-3)。
        ⚠️ 只偵測、不修復:engine 不對柱碰撞做 try_move/try_drop,也不把柱當
        blocker(見 engine.resolve)。柱藏牆內、家具貼牆合法壓到柱的室內半邊,
        故用線性穿入深度判定而非面積。

    static×static 不管(牆/柱本來就固定)。牆障礙不參與 area 重疊(家具貼牆
    footprint 會壓到牆內半邊,那是合法的,不能用 area 判)。"""
    movs = [o for o in obstacles if o.movable]
    swings = [o for o in obstacles if o.kind == DOOR_SWING]
    hards = [o for o in obstacles if o.kind in HARD_KINDS]
    walls = [o for o in obstacles if o.kind == WALL]
    columns = [o for o in obstacles if o.kind == COLUMN]
    cols: list[Collision] = []
    for i in range(len(movs)):
        for j in range(i + 1, len(movs)):
            area = movs[i].poly.intersection(movs[j].poly).area
            if area > tol:
                cols.append(Collision(movs[i], movs[j], area))
    for m in movs:
        for s in swings:
            area = m.poly.intersection(s.poly).area
            if area > tol:
                cols.append(Collision(m, s, area))
    for m in movs:                                   # 硬障礙:掉進天井、壓住樓梯
        for h in hards:
            area = m.poly.intersection(h.poly).area
            if area > tol:
                cols.append(Collision(m, h, area))
    for m in movs:                                   # 穿牆:突出所屬房間 > wall_tol
        if m.room is None:
            continue
        cpoly = m.collision_poly or m.poly
        out = cpoly.difference(m.room).area
        if out > wall_tol:
            wall = _wall_for(m, walls)
            cols.append(Collision(m, wall, out))
    for m in movs:                                   # 壓柱:穿入深度 > col_tol
        for c in columns:
            if penetration(m.poly, c.poly) > col_tol:
                cols.append(Collision(m, c, m.poly.intersection(c.poly).area))
    return cols


def _wall_for(m: Obstacle, walls: list[Obstacle]) -> Obstacle:
    """回傳家具 m 所屬房間對應的 WALL 障礙(形心落在其房內);找不到給個佔位。"""
    c = m.poly.centroid
    for w in walls:
        if w.poly.contains(c):
            return w
    return Obstacle(poly=m.room, kind=WALL, tag=(m.tag + " 所屬房"))


def collision_problems(spec, tol: float = OVERLAP_TOL) -> list[str]:
    """家具碰撞問題清單,**逐字對齊 validate_spec 的現有輸出**:
      * 家具兩兩重疊 → "家具重疊:A×B"
      * 家具壓門迴轉 → "家具 X 擋住門的迴轉(牆 N 的門)"
    順序與 validate 相同(先家具兩兩,再門×家具),閾值相同(>100)。"""
    furs = fixture_obstacles(spec)
    doors = door_swing_obstacles(spec)
    problems: list[str] = []
    for i in range(len(furs)):
        for j in range(i + 1, len(furs)):
            if furs[i].poly.intersection(furs[j].poly).area > tol:
                problems.append(f"家具重疊:{furs[i].tag}×{furs[j].tag}")
    for d in doors:
        for f in furs:
            if d.poly.intersection(f.poly).area > tol:
                problems.append(f"家具 {f.tag} 擋住門的迴轉({d.tag})")
    return problems


def column_contacts(spec, col_tol: float = COLUMN_TOLERANCE_MM) -> list[dict]:
    """**所有**家具×柱的接觸清單(不設門檻,報表/診斷用),深到淺排序。

    每筆:家具名、柱心座標、穿入深度(mm)、交集面積(mm²)、是否超過容差。
    * over_tolerance=True  → find_collisions 會回報的「壓柱」(真問題)。
    * over_tolerance=False → 柱藏牆內、家具貼牆的合法接觸(Step 0 實測 283 件,
      最深 175mm)——列出來供人檢視,但不是缺陷、也不會被修復。"""
    furs = fixture_obstacles(spec)
    cols = column_obstacles(spec)
    out: list[dict] = []
    for f in furs:
        for c in cols:
            pen = penetration(f.poly, c.poly)
            if pen <= 0:
                continue
            out.append({
                "fixture": f.tag,
                "column": tuple(round(v, 1) for v in c.ref.center),
                "penetration_mm": round(pen, 1),
                "area_mm2": round(f.poly.intersection(c.poly).area, 1),
                "over_tolerance": pen > col_tol,
            })
    out.sort(key=lambda d: d["penetration_mm"], reverse=True)
    return out
