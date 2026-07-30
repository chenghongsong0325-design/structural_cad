"""門與動線規範(使用者 2026-07-30 定調的產圖指令)→ 可執行的硬規則 + 門連通表。

前面的 plan_check 已經擋掉「沒門/室內斷開/沒大門」這類斷裂;這一層把使用者那份
規範裡**還沒被程式檢查**的條文補上,並產出一張「門連通表」(房間 → 門 → 通往哪裡)
供人核對。原則同 plan_check:**error = 換個切法就能解決 → 不合格就重生**。

新增的硬規則:

    entry_door_narrow   對外大門淨寬 <90cm
    room_door_narrow    居室門 <80cm / 衛浴門 <75cm
    door_swing_blocked  門的開啟弧線撞到牆、柱、家具或另一扇門
    bath_door_to_kitchen 衛浴門直接開向廚房(衛生/風水慣例)
    through_bedroom     要穿越別人的臥室才到得了某個空間(套內衛浴除外)
    stair_wrapped       樓梯間只能從私人房間進(樓梯口沒接到公共動線)

⚠️ 刻意**不**在這裡重做 plan_check 已有的規則(門卡角落、門開在階梯上、每房有門、
   同層連通),避免同一件事兩個地方判、判準還會漂。
"""
from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field

from shapely.geometry import Point, Polygon
from shapely.ops import unary_union

from src.design.layout.plan_check import (
    EDGE_TOL,
    VOID_KINDS,
    PlanIssue,
    _wall_bodies,
)

# 淨寬下限(mm)。使用者規範:主要出入口 ≥90、一般居室 ≥80、衛浴 ≥75。
ENTRY_DOOR_MIN = 900.0
ROOM_DOOR_MIN = 800.0
BATH_DOOR_MIN = 750.0
# 門扇掃過的區域與障礙物重疊超過這麼多(mm²)才算撞到(容差,避免貼齊誤判)。
SWING_OVERLAP_TOL = 20000.0     # 0.02 ㎡
SWING_STEPS = 6                 # 開啟弧線取樣段數(扇形近似)

# 私人房間:動線不該「穿過」這些空間才到得了別處。
PRIVATE_KINDS = {"bedroom", "master_bedroom", "elder_room"}
# 公共動線空間:樓梯口至少要接到其中之一。
PUBLIC_KINDS = {"corridor", "foyer", "living", "dining", "stair_hall", "kitchen",
                "garage", "parking"}
# 衛浴門不得直接開向這些空間。
BATH_DOOR_FORBIDDEN = {"kitchen", "shrine"}
BATH_KINDS = {"bathroom", "toilet"}


# ── 門連通表 ────────────────────────────────────────────────────────────────
@dataclass
class DoorLink:
    """一扇門:在哪裡、多寬、連通哪兩個空間(對外門的另一側是「室外」)。"""

    floor: str
    x: float
    y: float
    width: float
    sides: list           # ["客廳", "樓梯間"] 或 ["客廳", "室外"]
    kinds: list           # 對應的 kind
    exterior: bool = False

    def to_dict(self) -> dict:
        d = asdict(self)
        d["x"], d["y"] = round(self.x, 1), round(self.y, 1)
        return d


@dataclass
class DoorTable:
    """門連通表(整棟)。可序列化,也可印成人看的表格。"""

    links: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"n_doors": len(self.links),
                "links": [ln.to_dict() for ln in self.links]}

    def to_json(self, **kw) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, **kw)

    def summary(self) -> str:
        """人看的「房間 → 門 → 通往空間」表。"""
        if not self.links:
            return "門連通表:沒有門"
        out = [f"門連通表(共 {len(self.links)} 扇)", "  樓層  淨寬   房間 → 通往"]
        for ln in self.links:
            a = ln.sides[0] if ln.sides else "?"
            b = ln.sides[1] if len(ln.sides) > 1 else "室外"
            out.append(f"  {ln.floor:<4} {ln.width:>5.0f}  {a} → {b}")
        return "\n".join(out)


def _room_at(polys, p: Point):
    """點落在哪個房間(含邊界容差)。"""
    for r, poly in polys:
        if poly.contains(p) or poly.exterior.distance(p) < 1.0:
            return r
    return None


def _door_sides(spec, wall, op, polys, env):
    """一扇門兩側的房間(往牆法線兩側各探 300mm)。回 (rooms, 是否對外)。"""
    cx, cy = wall.point_at(op.position)
    nx, ny = wall.normal_vector
    found = []
    for s in (1, -1):
        r = _room_at(polys, Point(cx + nx * 300.0 * s, cy + ny * 300.0 * s))
        found.append(r)
    on_env = (abs(cy - env[1]) < EDGE_TOL or abs(cy - env[3]) < EDGE_TOL
              or abs(cx - env[0]) < EDGE_TOL or abs(cx - env[2]) < EDGE_TOL)
    return found, on_env and any(r is None for r in found)


def door_table(floors, env=None) -> DoorTable:
    """整棟的門連通表。floors = [(label, spec), ...]。"""
    from src.design.layout.plan_check import building_env
    links = []
    for item in floors:
        label, spec = item[0], item[1]
        e = building_env(spec) if env is None else env
        polys = [(r, Polygon(r.points)) for r in spec.rooms]
        for wi, w in enumerate(spec.walls):
            for op in w.openings:
                if op.kind != "door":
                    continue
                sides, ext = _door_sides(spec, w, op, polys, e)
                names = [(r.name if r else "室外") for r in sides]
                kinds = [(r.kind if r else "outside") for r in sides]
                cx, cy = w.point_at(op.position)
                links.append(DoorLink(label, cx, cy, op.width, names, kinds, ext))
    return DoorTable(links)


# ── 開啟弧線 ────────────────────────────────────────────────────────────────
def _swing_sector(wall, op, door) -> Polygon:
    """門扇掃過的扇形(90 度,半徑=門寬),依 hinge/swing 決定在哪一側、繞哪一邊。"""
    (sx, sy), (ex, ey) = wall.start, wall.end
    L = math.hypot(ex - sx, ey - sy) or 1.0
    ux, uy = (ex - sx) / L, (ey - sy) / L          # 沿牆單位向量
    nx, ny = -uy, ux                               # 法線(牆行進方向左手側)
    half = op.width / 2.0
    cx, cy = wall.point_at(op.position)
    # 鉸鏈在洞口的哪一端(left=近 start 側)
    sign_h = -1.0 if getattr(door, "hinge", "left") == "left" else 1.0
    hx, hy = cx + ux * half * sign_h, cy + uy * half * sign_h
    # 門扇往法線哪一側掃(out=+n)
    sign_s = 1.0 if getattr(door, "swing", "out") == "out" else -1.0
    r = op.width
    # 起始邊 = 關著的門扇(從鉸鏈指向另一個門樘),終止邊 = 開到底(垂直牆面)
    a0 = math.atan2(-uy * sign_h, -ux * sign_h)
    a1 = math.atan2(ny * sign_s, nx * sign_s)
    # 取兩角之間較短的那 90 度
    d = (a1 - a0 + math.pi) % (2 * math.pi) - math.pi
    pts = [(hx, hy)]
    for i in range(SWING_STEPS + 1):
        a = a0 + d * i / SWING_STEPS
        pts.append((hx + r * math.cos(a), hy + r * math.sin(a)))
    poly = Polygon(pts)
    if not poly.is_valid or poly.is_empty:
        return Polygon()
    # 扣掉「牆自己那一片」:關著的門扇本來就貼在牆面上,牆(以及同一直線上的鄰段牆)
    # 不算擋住自己。不扣的話每扇門都會固定被判撞牆 0.05㎡ 起跳。
    from shapely.geometry import LineString
    far = 4000.0
    plane = LineString([(cx - ux * far, cy - uy * far),
                        (cx + ux * far, cy + uy * far)]).buffer(
        wall.thickness / 2.0 + 20.0, cap_style=2, join_style=2)
    poly = poly.difference(plane)
    return poly if not poly.is_empty else Polygon()


def _swing_obstacles(spec, wall, op):
    """會擋住這扇門的東西:牆體(自己這道除外)、柱、家具、其他門的開啟弧線。"""
    from src.design.collision.geometry import fixture_obstacles
    bodies = []
    for i, w in enumerate(spec.walls):
        if w is wall:
            continue
        bodies.append(_wall_bodies(spec)[i])
    for c in getattr(spec, "column_centers", None) or []:
        cs = getattr(spec, "column_size", 500.0) or 500.0
        bodies.append(Point(*c).buffer(cs / 2.0, cap_style=3))
    bodies += [o.poly for o in fixture_obstacles(spec)]
    return bodies


def _other_door_sectors(spec, skip_dp) -> list:
    """除了這扇門以外,其他門的開啟弧線(兩扇門不能互相打到)。"""
    out = []
    for dp in getattr(spec, "doors", None) or []:
        if dp is skip_dp or getattr(dp.door, "sliding", False):
            continue
        try:
            w = spec.walls[dp.wall_index]
            op = w.openings[dp.opening_index]
        except (IndexError, AttributeError):
            continue
        if op.kind != "door":
            continue
        sec = _swing_sector(w, op, dp.door)
        if not sec.is_empty:
            out.append(sec)
    return out


def check_door_rules(spec, env=None, level: int = 1, label: str = "") -> list:
    """一層:檢查使用者那份「門與動線規範」裡 plan_check 還沒管到的條文。"""
    from src.design.layout.plan_check import building_env
    env = building_env(spec) if env is None else env
    lb = label or getattr(spec, "floor_label", "") or f"{level}F"
    issues: list[PlanIssue] = []
    polys = [(r, Polygon(r.points)) for r in spec.rooms]

    # ① 門淨寬(對外 ≥90cm、居室 ≥80cm、衛浴 ≥75cm)
    for w in spec.walls:
        for op in w.openings:
            if op.kind != "door":
                continue
            sides, ext = _door_sides(spec, w, op, polys, env)
            kinds = {r.kind for r in sides if r}
            if ext:
                if op.width < ENTRY_DOOR_MIN - 1.0:
                    issues.append(PlanIssue(
                        "error", "entry_door_narrow", lb, "",
                        f"對外大門淨寬 {op.width:.0f} < {ENTRY_DOOR_MIN:.0f}"))
                continue
            need = BATH_DOOR_MIN if kinds & BATH_KINDS else ROOM_DOOR_MIN
            if op.width < need - 1.0:
                who = "/".join(sorted(r.name for r in sides if r))
                issues.append(PlanIssue(
                    "error", "room_door_narrow", lb, who,
                    f"房門淨寬 {op.width:.0f} < {need:.0f}"))

    # ② 開啟弧線不得撞牆/柱/家具/另一扇門
    swings = []
    for dp in getattr(spec, "doors", None) or []:
        try:
            w = spec.walls[dp.wall_index]
            op = w.openings[dp.opening_index]
        except (IndexError, AttributeError):
            continue
        if op.kind != "door" or getattr(dp.door, "sliding", False):
            continue                        # 橫拉門沿牆滑開,沒有開啟弧線
        swings.append((dp, w, op, _swing_sector(w, op, dp.door)))
    for i, (_dp, w, op, sec) in enumerate(swings):
        if sec.is_empty:
            continue
        obs = _swing_obstacles(spec, w, op)
        obs += [s for j, (_d2, _w2, _o2, s) in enumerate(swings)
                if j != i and not s.is_empty]
        hit = unary_union([o for o in obs if o.intersects(sec)]) if obs else None
        area = sec.intersection(hit).area if hit is not None and not hit.is_empty else 0.0
        if area > SWING_OVERLAP_TOL:
            px, py = w.point_at(op.position)
            issues.append(PlanIssue(
                "error", "door_swing_blocked", lb, "",
                f"門洞({px:.0f},{py:.0f})的開啟弧線被擋住 {area/1e6:.3f}㎡"
                f"(撞到牆/柱/家具/另一扇門)"))

    # ③ 衛浴門不可直接開向廚房/神明廳
    for w in spec.walls:
        for op in w.openings:
            if op.kind != "door":
                continue
            sides, _ext = _door_sides(spec, w, op, polys, env)
            kinds = {r.kind for r in sides if r}
            if kinds & BATH_KINDS and kinds & BATH_DOOR_FORBIDDEN:
                issues.append(PlanIssue(
                    "error", "bath_door_to_kitchen", lb,
                    "/".join(sorted(r.name for r in sides if r)),
                    "衛浴門直接開向廚房/神明廳(應開向走道或臥室內部)"))

    # ④ 不得穿越別人的臥室才到得了(套內衛浴除外)
    issues += _through_bedroom_issues(spec, polys, env, level, lb)

    # ⑤ 樓梯不得被房間包住:樓梯間的門若全部只通私人房間,而**這層明明有**公共
    #    空間(客廳/餐廳/走道…),就是把樓梯包進臥室了。整層只有臥室+衛浴的小
    #    透天樓層沒有公共空間可接,樓梯間本身就是那層的梯廳,不算缺陷。
    if getattr(spec, "stairs", None):
        floor_public = {r.kind for r, _p in polys
                        if r.kind in PUBLIC_KINDS and r.kind != "stair_hall"}
        for hall in [r for r, _p in polys if r.kind == "stair_hall"]:
            nbrs = [n for n in _door_neighbors(spec, polys, env, hall) if n]
            if not nbrs or not floor_public:
                continue
            if not ({n.kind for n in nbrs} & PUBLIC_KINDS):
                issues.append(PlanIssue(
                    "error", "stair_wrapped", lb, hall.name,
                    f"樓梯間只接到 {[n.name for n in nbrs]},"
                    f"這層有公共空間卻沒接上(上下樓要穿私人房間)"))
    return issues


# ── 修復:把違規的門改到合法的鄰室(換切法之前先試著救) ──────────────────────
def repair_doors(spec, bx0, by0, bx1, level) -> bool:
    """依規範修門:①衛浴門不開向廚房 ②每間房都有一扇門直通非私人空間。

    回傳有沒有動過。修不動的(例如那道牆根本沒地方開門)就留給關卡擋下、換切法
    重生 —— 這裡只做「改門」,不改房間切法。"""
    from src.design.layout.narrow_house import (
        _add_interior_door, _remove_openings,
    )
    from src.design.layout.plan_check import building_env

    env = building_env(spec)
    polys = [(r, Polygon(r.points)) for r in spec.rooms]
    changed = False

    # ① 衛浴門開向廚房/神明廳 → 先在允許的鄰室開一扇,成功了才刪掉原來那扇
    for r, poly in polys:
        if r.kind not in BATH_KINDS:
            continue
        bad = []
        for wi, w in enumerate(spec.walls):
            for oi, op in enumerate(w.openings):
                if op.kind != "door":
                    continue
                if poly.exterior.distance(Point(*w.point_at(op.position))) >= EDGE_TOL:
                    continue
                sides, _ext = _door_sides(spec, w, op, polys, env)
                if {x.kind for x in sides if x} & BATH_DOOR_FORBIDDEN:
                    bad.append((wi, oi))
        if not bad:
            continue
        allow = (PUBLIC_KINDS | PRIVATE_KINDS) - BATH_DOOR_FORBIDDEN
        if _add_interior_door(spec, r, bx0, by0, bx1, level, only_kinds=allow):
            _remove_openings(spec, set(bad))
            changed = True

    # ②a 開啟弧線撞東西 → 先轉門(換鉸鏈邊/開啟方向),四種都不行就改**橫拉門**
    #     (門與動線規範:空間不足時改用橫拉門並註明)。
    #     跑兩輪:兩扇門互撞時,第一輪轉了其中一扇,第二輪另一扇就有位置了。
    for _round in range(2):
        for dp in getattr(spec, "doors", None) or []:
            try:
                w = spec.walls[dp.wall_index]
                op = w.openings[dp.opening_index]
            except (IndexError, AttributeError):
                continue
            if op.kind != "door" or getattr(dp.door, "sliding", False):
                continue
            obs = _swing_obstacles(spec, w, op) + _other_door_sectors(spec, dp)
            best = None
            for hinge in ("left", "right"):
                for swing in ("out", "in"):
                    sec = _swing_sector(w, op, type(dp.door)(hinge=hinge,
                                                             swing=swing))
                    hit = sum(sec.intersection(o).area
                              for o in obs if o.intersects(sec))
                    if best is None or hit < best[0]:
                        best = (hit, hinge, swing)
            if best is None:
                continue
            hit, hinge, swing = best
            if (hinge, swing) != (dp.door.hinge, dp.door.swing):
                dp.door.hinge, dp.door.swing = hinge, swing
                changed = True
            if hit > SWING_OVERLAP_TOL:             # 轉遍了還是撞 → 橫拉門
                dp.door.sliding = True
                changed = True

    # ② 只能穿臥室才進得去的空間 → 補一扇門直通公共動線
    polys = [(r, Polygon(r.points)) for r in spec.rooms]
    for iss in _through_bedroom_issues(spec, polys, env, level, ""):
        room = next((r for r, _p in polys if r.name == iss.room), None)
        if room is None:
            continue
        if _add_interior_door(spec, room, bx0, by0, bx1, level,
                              only_kinds=PUBLIC_KINDS):
            changed = True
    return changed


def _door_neighbors(spec, polys, env, room):
    """這個房間透過門連到的其他空間(對外門的另一側回 None)。"""
    poly = Polygon(room.points)
    out = []
    for w in spec.walls:
        for op in w.openings:
            if op.kind != "door":
                continue
            p = Point(*w.point_at(op.position))
            if poly.exterior.distance(p) >= EDGE_TOL:
                continue
            sides, _ext = _door_sides(spec, w, op, polys, env)
            for r in sides:
                if r is not room:
                    out.append(r)
    return out


def _through_bedroom_issues(spec, polys, env, level, lb) -> list:
    """從大門(樓上:從樓梯間)出發,不進臥室走得到的空間;走不到的就是要穿臥室。

    例外:套內衛浴——只跟一間臥室相連的衛浴,本來就是那間房的附屬空間。"""
    from src.design.layout.plan_check import _room_graph_components  # noqa: F401
    rooms = [r for r, _p in polys if r.kind not in VOID_KINDS]
    if not rooms:
        return []
    start = None
    if level == 1:
        for w in spec.walls:
            for op in w.openings:
                if op.kind != "door":
                    continue
                sides, ext = _door_sides(spec, w, op, polys, env)
                if ext:
                    start = next((r for r in sides if r), None)
    if start is None:
        start = next((r for r, _p in polys if r.kind == "stair_hall"), None)
    if start is None:
        return []

    # 廣度優先,但**不從臥室往外擴**(進了臥室就停,等於不允許穿越)
    seen, stack = {id(start)}, [start]
    while stack:
        cur = stack.pop()
        if cur is not start and cur.kind in PRIVATE_KINDS:
            continue                        # 臥室可以「到達」,但不能當通道
        for n in _door_neighbors(spec, polys, env, cur):
            if n is not None and id(n) not in seen:
                seen.add(id(n))
                stack.append(n)

    issues = []
    for r in rooms:
        if id(r) in seen:
            continue
        nbrs = [n for n in _door_neighbors(spec, polys, env, r) if n]
        ensuite = (r.kind in BATH_KINDS and len(nbrs) == 1
                   and nbrs[0].kind in PRIVATE_KINDS)
        if ensuite:
            continue                        # 套內衛浴:合理,不是動線缺陷
        issues.append(PlanIssue(
            "error", "through_bedroom", lb, r.name,
            f"只能穿越 {[n.name for n in nbrs]} 才進得去(臥室不可當通道)"))
    return issues
