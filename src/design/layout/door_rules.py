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
# 這些不是居室(不住人),門與衛浴同級 75cm 即可。
SERVICE_KINDS = {"storage", "utility", "pipe_shaft"}
# **套內附屬空間**:只跟一間臥室相連時,它就是那間房的一部分(主臥的更衣室、
# 套房的衛浴),不算「要穿越別人的臥室才進得去」。
# ⚠️ 「只有一個鄰室」是關鍵條件 —— 有兩個以上鄰室的儲藏是公共儲藏,
#    那種藏在臥室後面才真的是動線缺陷。
ENSUITE_KINDS = BATH_KINDS | {"storage", "utility"}


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
    # ⚠️ `_wall_bodies` 一次做出**全部**牆的實體。以前這段寫在迴圈裡、每圈只取
    #    第 i 個 —— N 道牆就做了 N×N 個 buffer,丟掉 N²−N 個。修門會對每扇門反覆
    #    呼叫這支,白算的量很可觀。算一次、再篩掉自己那道就好(結果完全相同)。
    bodies = [b for w, b in zip(spec.walls, _wall_bodies(spec)) if w is not wall]
    # ⚠️ 以前這裡寫 `spec.column_centers or []` —— 但 `column_centers is None`
    #    的意思是「柱放在每個軸網交點」(AI 產線與窄/淺透天都用這種存法),
    #    `None or []` 會變成空清單 → **這條規則對那幾條產線從來沒有生效過**。
    #    一律走 `column_footprints`(柱實體的單一出處),它會把 None 解成實際柱位。
    from src.design.column_design import column_footprints
    bodies += column_footprints(spec)
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
            need = (BATH_DOOR_MIN if kinds & (BATH_KINDS | SERVICE_KINDS)
                    else ROOM_DOOR_MIN)
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
            corridors = [Polygon(r.points) for r in spec.rooms
                         if r.kind == "corridor"]
            best = None
            for hinge in ("left", "right"):
                for swing in ("out", "in"):
                    sec = _swing_sector(w, op, type(dp.door)(hinge=hinge,
                                                             swing=swing))
                    hit = sum(sec.intersection(o).area
                              for o in obs if o.intersects(sec))
                    # 門不要往走道開:走道是共用動線,門扇一開就把 1.2m 的走道
                    # 夾成 15cm(walkway 會判整條被擋)。往房間裡開才是常規做法。
                    into_corridor = sum(sec.intersection(c).area
                                        for c in corridors)
                    score = hit + into_corridor
                    if best is None or score < best[0]:
                        best = (score, hinge, swing, hit)
            if best is None:
                continue
            _score, hinge, swing, hit = best
            if (hinge, swing) != (dp.door.hinge, dp.door.swing):
                dp.door.hinge, dp.door.swing = hinge, swing
                changed = True
            if hit > SWING_OVERLAP_TOL:             # 轉遍了還是撞 → 橫拉門
                dp.door.sliding = True
                changed = True

    # ①b 門卡在房間角落 → 沿著同一道牆挪到合法位置。
    #     ⚠️ 兩個底線:①挪完還要連通**同樣的兩間房**(不然臥室的門會挪到通廚房);
    #     ②不能壓柱(兩帶式/集合住宅有柱)。做不到就別挪,留給關卡擋。
    from src.design.layout.narrow_house import (
        DOOR_CLEAR_STEPS, _column_blocks, _door_candidates, _door_front_walkable,
        _door_pos_ok, _stair_room_areas,
    )
    def dp_of(target_op):
        """這個洞口掛的門扇;**開放通道(沒掛門扇)回 None**。

        沒有門扇就沒有開啟弧線 —— 那種洞口不必檢查「會不會打到家具」,
        否則客餐之間 1.8m 的開放通道永遠修不動(周圍本來就擺著沙發餐桌)。"""
        for dp in getattr(spec, "doors", None) or []:
            try:
                if spec.walls[dp.wall_index].openings[dp.opening_index] is target_op:
                    return dp.door
            except (IndexError, AttributeError):
                continue
        return None

    for w in spec.walls:
        (sx, sy), (ex, ey) = w.start, w.end
        vertical = abs(sx - ex) < 1.0
        along = sy if vertical else sx
        lo = min(sy, ey) if vertical else min(sx, ex)
        hi = max(sy, ey) if vertical else max(sx, ex)
        for op in w.openings:
            # ⚠️ **開放通道不是門,不吃門角淨距。** 走道口那種滿寬的洞口兩端就是
            #    結構(導牆/界牆),它「貼著牆角」是對的;照門的規矩把它往裡面挪,
            #    界牆上就會留下一截凸出來的牆頭(使用者 2026-09-02 圈出來的)。
            if getattr(op, "is_passage", False):
                continue
            if op.kind != "door" or _door_pos_ok(spec, w, op.position, op.width):
                continue
            _sides_raw, _is_ext = _door_sides(spec, w, op, polys, env)
            sides0 = [r for r in _sides_raw if r]
            want = {id(r) for r in sides0}
            # 候選位置限制在**兩側房間共有的那一段牆**內:整道牆可能橫跨 12m、
            # 接了五六間房,在別段開門會連到別的房間(那是換設計,不是修門)。
            seg_lo, seg_hi = lo, hi
            for r in sides0:
                rx0, ry0, rx1, ry1 = Polygon(r.points).bounds
                seg_lo = max(seg_lo, ry0 if vertical else rx0)
                seg_hi = min(seg_hi, ry1 if vertical else rx1)
            if seg_hi - seg_lo < op.width:
                seg_lo, seg_hi = lo, hi         # 退回整道牆(至少試試看)
            taken = [(o.position - o.width / 2 - 100.0,
                      o.position + o.width / 2 + 100.0)
                     for o in w.openings if o is not op]
            taken += _column_blocks(spec, w, along)
            keep, keep_w = op.position, op.width
            kinds0 = {r.kind for r in sides0}
            floor_w = (ENTRY_DOOR_MIN if _is_ext          # 對外大門不得 <90cm
                       else BATH_DOOR_MIN if kinds0 & (BATH_KINDS | SERVICE_KINDS)
                       else ROOM_DOOR_MIN)
            # 寬度也可以讓:牆段被柱吃掉之後,900 的門怎麼挪都卡角落,
            # 縮到該房型的法定下限(儲藏/衛浴 750、居室 800)才放得下。
            # 寬度候選:原寬 → 剛好塞進這段牆的寬 → 該房型的法定下限。
            # 中間那個是為了 1.8m 的客餐通道:牆段只有 2m 時原寬放不下,
            # 但也不必一路縮到 0.8m 的房門(通道越寬越好走)。
            fit = (seg_hi - seg_lo) - 2 * DOOR_CLEAR_STEPS[-1]
            widths = [op.width]
            if floor_w < fit < op.width:
                widths.append(fit)
            if op.width > floor_w + 1:
                widths.append(floor_w)
            # ⚠️ **「門前面走得到」排在門寬/牆角淨距前面**(2026-08-28)。
            #    這支挪門時只問「離原位近不近、卡不卡牆角、會不會打到家具」——
            #    不問「挪過去之後人到不到得了那扇門」。實測 4.0m 面寬:
            #    `_ensure_floor_connected` 已經把餐廚的門好好開在走道上,這支為了
            #    閃柱把它搬到樓梯另一側那個繞不過去的死角,整層前後就此走不通。
            #    (門寬讓一級只是「窄一點」,門開在走不到的地方是廢圖。)
            areas = _stair_room_areas(spec)

            def _search(walkable_only):
                for width in widths:
                    for clear in DOOR_CLEAR_STEPS:
                        # 候選位置:①既有的候選(中點/貼齊/三七分)②**各段空牆的
                        #   中點** —— 牆被柱切成好幾段時,只有第②種才找得到位置。
                        from src.design.layout.narrow_house import _free_intervals
                        others = [o for o in w.openings if o is not op]
                        keep_all = list(w.openings)
                        w.openings = others
                        free = _free_intervals(w, seg_lo, seg_hi, along,
                                               _column_blocks(spec, w, along),
                                               clear)
                        w.openings = keep_all
                        extra = [along + (a + b) / 2.0 for a, b in free
                                 if b - a >= width]
                        cands = sorted(
                            _door_candidates(spec, w, seg_lo, seg_hi) + extra,
                            key=lambda m: abs(abs(m - along) - keep))
                        for m in cands:      # 離原位最近的先試(別把門搬到對面)
                            if walkable_only and not _door_front_walkable(
                                    spec, w, m, areas):
                                continue
                            pos = abs(m - along)
                            a, b = pos - width / 2, pos + width / 2
                            if a < 0 or b > w.length:
                                continue
                            if not all(b < t0 or a > t1 for t0, t1 in taken):
                                continue
                            if not _door_pos_ok(spec, w, pos, width, clear):
                                continue
                            leaf = dp_of(op)
                            if leaf is not None and _swing_hits_furniture(
                                    spec, w, pos, width, leaf):
                                continue     # 挪過去門就打到家具,等於沒解決
                            op.position, op.width = pos, width   # 試放
                            same = {id(r) for r
                                    in _door_sides(spec, w, op, polys, env)[0] if r}
                            op.position, op.width = keep, keep_w
                            if same == want:                     # 還是同兩間
                                return pos, width
                return None

            found = (_search(True) if areas else None) or _search(False)
            if found is not None:
                op.position, op.width = found
                changed = True

    # ② 只能穿臥室才進得去的空間 → 補一扇門直通公共動線
    polys = [(r, Polygon(r.points)) for r in spec.rooms]
    for iss in _through_bedroom_issues(spec, polys, env, level, ""):
        room = next((r for r, _p in polys if r.name == iss.room), None)
        if room is None:
            continue
        # ⚠️ `require_walkable`:補一扇**走不到的**門不叫修好(2026-08-28)。
        #    車庫版 1F 的浴廁北邊正好是梯段盡頭那塊死角,這裡照補的話
        #    `through_bedroom` 是消掉了,換來的是 `circulation_blocked` ——
        #    問題只是從左手換到右手(本檔那條老毛病)。
        if (_add_interior_door(spec, room, bx0, by0, bx1, level,
                               only_kinds=PUBLIC_KINDS, require_walkable=True)
                or _add_interior_door(spec, room, bx0, by0, bx1, level,
                                      only_kinds=PUBLIC_KINDS)):
            changed = True
    return changed


def _swing_hits_furniture(spec, wall, pos, width, door) -> bool:
    """門挪到這個位置後,開啟時會不會打到家具。

    用的是 validate_spec 的同一塊方形(門寬 × 門寬,開啟側),判準才一致。"""
    from shapely.geometry import Polygon as _P

    from src.design.collision.geometry import fixture_obstacles
    cx, cy = wall.point_at(pos)
    ux, uy = wall.unit_vector
    nx, ny = wall.normal_vector
    sgn = 1.0 if getattr(door, "swing", "out") == "out" else -1.0
    h = width / 2.0
    square = _P([
        (cx - ux * h, cy - uy * h),
        (cx + ux * h, cy + uy * h),
        (cx + ux * h + sgn * nx * width, cy + uy * h + sgn * ny * width),
        (cx - ux * h + sgn * nx * width, cy - uy * h + sgn * ny * width),
    ])
    return any(square.intersection(o.poly).area > 100.0
               for o in fixture_obstacles(spec))


def _door_neighbors(spec, polys, env, room):
    """這個房間連到的其他空間(對外門的另一側回 None)。

    ⚠️ 「連到」包含**開放通道**:兩間房之間沒畫牆(客餐廳↔走道、玄關↔起居室)
    也是走得過去的,只認門會把開放格局判成到不了。"""
    from src.design.layout.room_circulation import _open_passages

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
    for p in _open_passages(spec, poly):            # 沒有牆的開放邊界
        for r, other in polys:
            if r is not room and other.exterior.distance(p) < EDGE_TOL:
                out.append(r)
    return out


def _through_bedroom_issues(spec, polys, env, level, lb) -> list:
    """從大門(樓上:從樓梯間)出發,不進臥室走得到的空間;走不到的就是要穿臥室。

    例外:套內附屬空間(`ENSUITE_KINDS`)——只跟一間臥室相連的衛浴/更衣室,
    本來就是那間房的一部分。"""
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
        ensuite = (r.kind in ENSUITE_KINDS and len(nbrs) == 1
                   and nbrs[0].kind in PRIVATE_KINDS)
        if ensuite:
            continue                        # 套內衛浴/更衣室:合理,不是缺陷
        issues.append(PlanIssue(
            "error", "through_bedroom", lb, r.name,
            f"只能穿越 {[n.name for n in nbrs]} 才進得去(臥室不可當通道)"))
    return issues


# ── NG09 門撞門(使用者 2026-09-03 給的〈9 種常見 NG 格局〉)────────────────
# 書上的案例是「走道盡頭集中了 5 扇門」,毛病有兩個:①風水上門對門
# ②兩扇門同時開會撞在一起。
#
# ⚠️ 這**不是** `door_swing_blocked` 已經在管的事。那條問的是「這扇門的開啟弧
#    有沒有實際壓到另一扇門的弧」——門對門正對面時,兩道弧常常各自轉得開
#    (中間留得下 1.2m),它一聲不吭;但格局上那就是面面相覷。
#    同理「一小段牆上擠了 N 扇門」也不是任何一條現行規則在問的。
#
# 這兩支是**量表**,不產生 PlanIssue:幾扇門算擠、多近算對門,是設計判斷。
FACE_GAP = 2600.0       # 兩扇門相距多近才算會打架(門扇各約 850,兩人交會)
FACE_OFF = 700.0        # 沿牆方向錯開超過這麼多就不算「正對面」
CLUSTER_R = 2000.0      # 書上的 NG 是「這個半徑內 5 扇門」


def _real_doors(spec) -> list:
    """[(中心點, 寬, 牆的單位向量)]。開放通道(`is_passage`)沒有門扇,不算。"""
    out = []
    for w in spec.walls:
        for op in w.openings:
            if op.kind != "door" or getattr(op, "is_passage", False):
                continue
            out.append((w.point_at(op.position), op.width, w.unit_vector))
    return out


def facing_door_pairs(spec) -> list:
    """門對門的配對 [(門心, 門心, 相距 mm)]。

    判準:兩扇門在**平行**的牆上(垂直的是轉角,不算面面相覷)、沿牆方向幾乎
    不錯開、而且中間近到兩扇門會打架。"""
    from itertools import combinations

    hits = []
    for (p, _wp, up), (q, _wq, uq) in combinations(_real_doors(spec), 2):
        if abs(up[0] * uq[0] + up[1] * uq[1]) < 0.9:
            continue
        dx, dy = q[0] - p[0], q[1] - p[1]
        along = abs(dx * up[0] + dy * up[1])
        across = abs(-dx * up[1] + dy * up[0])
        if along <= FACE_OFF and 1.0 < across <= FACE_GAP:
            hits.append((p, q, across))
    return hits


def max_door_cluster(spec, radius: float = CLUSTER_R) -> int:
    """最擠的一處:半徑內有幾扇門(含自己)。書上的 NG 圖是 5 扇。"""
    ds = _real_doors(spec)
    best = 0
    for p, _w, _u in ds:
        n = sum(1 for q, _w2, _u2 in ds
                if (q[0] - p[0]) ** 2 + (q[1] - p[1]) ** 2 <= radius ** 2)
        best = max(best, n)
    return best
