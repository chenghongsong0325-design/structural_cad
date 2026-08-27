"""陽台配置(Phase 11)—— 把「哪裡該有陽台」寫成規則,不是畫圖技巧。

畫陽台的零件早就有了(`src/drafting/balcony_elevator.py`:三面矮牆 + 欄杆折線 +
「陽台」文字),集合住宅每戶也各配一座工作陽台;缺的是**透天產線**——規則版窄面寬
透天與淺進深透天生出來的圖,一樓到頂樓一座陽台都沒有,對照真實審定圖一眼就看得出來。

規則(台灣透天慣例;尺寸下限依使用者 2026-08-03 指定的 1.2~2.0m):

  * **只有二樓以上**設陽台。一樓臨路那一面是大門/車庫/前院,真實圖不會在那裡做陽台。
  * **挑出式(懸挑)**:陽台落在建築外牆之外、蓋在院子上方,**不佔室內樓地板**。
    這樣居室不會被切小,§40 的採光需求也不會跟著變 —— 這是選這個做法最重要的理由。
  * 進深 1.2~2.0m(站得住人、曬得了衣服);院子還要留 ≥0.5m,陽台不會壓到地界線。
  * 寬度 ≥ 服務那間房外牆長的**一半**,置中在該房的外牆上。
  * 出入口是**落地橫拉門**(淨寬 1.2m):平開門的門扇會掃掉陽台一半的地。
  * 陽台**不是居室**:不進 `spec.rooms`,所以不佔面積、不要求採光、不進動線分析
    ——這與真實圖面把陽台算成「附屬建物」是一致的。

⚠️ **採光讓路**:落地門會吃掉外牆長度,而共壁透天只有前後兩面能開窗。所以放陽台
   之前先**實際量**這間房剩下的牆還開不開得出 §40 要求的窗(`_daylight_capacity`),
   開不出來就不放這座陽台 —— 採光是法規,陽台不是。同理,門位要通過
   `_door_pos_ok`(不卡牆角、門前站得住人),擺不下就不放。

⚠️ 配套的硬規則寫在 `plan_check`(不在這裡自己判,免得判準漂):
     balcony_no_door   陽台沒有門進得去(等於畫了一塊到不了的地)
     entry_upstairs    樓上外牆開門 → **門外是陽台就不算**(由 `door_opens_to_balcony`
                       判定;沒有陽台的樓上外門仍是「門通往空中」的硬錯誤)

⚠️ 未做:陽台面積不列入面積計算表(真實圖會列成附屬建物);§41「外側有陽台時採光
   面積折減」未實作 —— 本模組把陽台進深壓在 2m 以內,避開深陽台的折減爭議。

典型用法::

    rep = add_balconies(spec, level=2)      # 直接改 spec.balconies / spec.doors
    print(rep.summary())
    rep.to_json()
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field

from shapely.geometry import Point, Polygon

from src.drafting.apartment_plan import DoorPlacement
from src.drafting.balcony_elevator import Balcony
from src.drafting.door_window import Door
from src.drafting.wall import Opening

# ── 尺寸規範(mm)────────────────────────────────────────────────────────────
MIN_DEPTH, MAX_DEPTH = 1200.0, 2000.0
# 陽台外緣離地界線至少留這麼多(排水溝/植栽/施工空間;也讓圖上看得出不是壓線蓋)。
YARD_CLEAR = 500.0
DOOR_WIDTH = 1200.0             # 落地橫拉門淨寬(≥ 對外門下限 900)
# 窄面寬(3.5~4m)透天的退讓寬度:1.2m 的門把南牆吃掉太多,§40 的窗就補不滿了。
# 900 是對外門的法定下限,再窄就不合格。
DOOR_MIN_WIDTH = 900.0
# 門洞離陽台自己的側矮牆至少留這麼多(矮牆厚 100,半厚 50 + 站人的餘裕)。
SIDE_CLEAR = 250.0
# 寬度 ≥ 服務房間外牆長的一半(使用者規範)。實作上直接給**滿寬**(貼齊該房的整段
# 外牆)——真實透天的前/後陽台就是這樣通長一條,而且滿寬時落地門不管開在哪一段都
# 一定落在陽台上。這個常數留著當規範下限,由測試把關。
WIDTH_RATIO = 0.5
# 外牆短於這個就不做陽台(落地門 1200 + 兩側各 250 淨距 + 餘裕)。
MIN_WIDTH = 1800.0
EDGE_TOL = 60.0                 # 貼邊容差

# 會配陽台的房間(居室);廚房/浴廁/樓梯間不配。
HOST_KINDS = {"bedroom", "master_bedroom", "living", "dining", "study",
              "elder_room"}
# 可以挑出陽台的兩個方向:透天東西是共壁界牆,只有前(南)後(北)挑得出去。
SIDES = ("south", "north")


# ── 報表(專案慣例:Report 一律可 to_dict / to_json)────────────────────────
@dataclass(frozen=True)
class BalconyItem:
    """一座配好的陽台:服務哪間房、多大、門開在哪。"""

    floor: str
    room: str
    side: str               # "south"(前陽台)/ "north"(後陽台)
    width: float
    depth: float
    door_x: float
    door_y: float
    door_width: float

    def __str__(self) -> str:
        where = "前" if self.side == "south" else "後"
        return (f"{self.floor} {self.room} {where}陽台 "
                f"{self.width/1000:.1f}×{self.depth/1000:.1f}m"
                f"(落地拉門 {self.door_width/1000:.1f}m)")


@dataclass
class BalconyReport:
    """一層(或整棟)配了哪些陽台、哪些沒配成與原因。"""

    items: list = field(default_factory=list)
    skipped: list = field(default_factory=list)     # [(樓層, 房間, 原因)]

    def to_dict(self) -> dict:
        return {
            "n_balconies": len(self.items),
            "n_skipped": len(self.skipped),
            "items": [asdict(i) for i in self.items],
            "skipped": [{"floor": f, "room": r, "reason": why}
                        for f, r, why in self.skipped],
        }

    def to_json(self, **kw) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, **kw)

    def summary(self) -> str:
        if not self.items and not self.skipped:
            return "陽台:這層不配(一樓臨路面是大門/前院)"
        lines = [f"陽台 {len(self.items)} 座"]
        lines += [f"  ✅ {i}" for i in self.items]
        lines += [f"  ➖ {f} {r}:{why}" for f, r, why in self.skipped]
        return "\n".join(lines)

    def extend(self, other: "BalconyReport") -> "BalconyReport":
        self.items.extend(other.items)
        self.skipped.extend(other.skipped)
        return self


# ── 幾何小工具 ──────────────────────────────────────────────────────────────
def balcony_polygon(bal: Balcony) -> Polygon:
    """陽台外圍(矮牆中心線)多邊形。"""
    x0, y0 = bal.origin
    return Polygon([(x0, y0), (x0 + bal.width, y0),
                    (x0 + bal.width, y0 + bal.depth), (x0, y0 + bal.depth)])


def door_opens_to_balcony(spec, wall, op, tol: float = EDGE_TOL) -> bool:
    """這個門洞的門外是不是一座陽台(而不是空中)。

    判準:洞口中心落在某座陽台的**貼建築那一邊**上,且在陽台的寬度範圍內。
    `plan_check` 的 entry_upstairs 用它豁免陽台門;`balcony_no_door` 反過來用它
    確認每座陽台都進得去。"""
    px, py = wall.point_at(op.position)
    for bal in getattr(spec, "balconies", None) or []:
        x0, y0 = bal.origin
        x1, y1 = x0 + bal.width, y0 + bal.depth
        if bal.attach in ("north", "south"):
            line = y1 if bal.attach == "north" else y0
            if abs(py - line) <= tol and x0 - tol <= px <= x1 + tol:
                return True
        else:
            line = x1 if bal.attach == "east" else x0
            if abs(px - line) <= tol and y0 - tol <= py <= y1 + tol:
                return True
    return False


def balcony_doors(spec, bal: Balcony, tol: float = EDGE_TOL) -> list:
    """通往這座陽台的門洞 → [(wall, opening)]。"""
    out = []
    for w in spec.walls:
        for op in w.openings:
            if op.kind == "door" and door_opens_to_balcony(spec, w, op, tol):
                x0, y0 = bal.origin
                px, py = w.point_at(op.position)
                inside = (x0 - tol <= px <= x0 + bal.width + tol
                          and y0 - tol <= py <= y0 + bal.depth + tol)
                if inside:
                    out.append((w, op))
    return out


def _room_edge_on(room_poly: Polygon, line: float, horizontal: bool = True):
    """房間貼在 y=line(或 x=line)上的**最長**那一段 → (lo, hi);沒貼到回 None。"""
    from src.design.layout.bsp_layout import _axis_edges

    want = "H" if horizontal else "V"
    best = None
    for kind, c, a, b in _axis_edges(room_poly):
        if kind != want or abs(c - line) > EDGE_TOL:
            continue
        if best is None or (b - a) > (best[1] - best[0]):
            best = (a, b)
    return best


def _wall_covering(spec, line: float, lo: float, hi: float, horizontal: bool = True):
    """覆蓋這一段外牆的牆 → (wall_index, 沿牆起點座標);找不到回 None。"""
    mid = (lo + hi) / 2.0
    for wi, w in enumerate(spec.walls):
        (sx, sy), (ex, ey) = w.start, w.end
        if horizontal:
            if abs(sy - ey) > 1.0 or abs(sy - line) > EDGE_TOL:
                continue
            a, b, along = min(sx, ex), max(sx, ex), sx
        else:
            if abs(sx - ex) > 1.0 or abs(sx - line) > EDGE_TOL:
                continue
            a, b, along = min(sy, ey), max(sy, ey), sy
        if a - 1.0 <= mid <= b + 1.0:
            return wi, along
    return None


# ── 採光容量:放了落地門之後,這間房還開不開得出 §40 要求的窗 ────────────────
def _daylight_capacity(spec, room, env) -> float:
    """這間房在所有採光面上「還開得出多寬的窗」(mm)。

    **實際量過才算數**:用補窗器同一組函式(`_window_segments` / `_free_intervals`),
    把這間房既有的窗先當作可以加寬/挪位而拿掉,算出剩餘牆面的可用區間總長(單扇窗
    上限 3.6m)。判準與 `_ensure_room_windows` 一致,才不會這裡說放得下、那裡開不出來。"""
    from src.design.layout.narrow_house import (
        WINDOW_MAX_W,
        WINDOW_MIN_W,
        _column_blocks,
        _free_intervals,
        _window_segments,
    )

    poly = Polygon(room.points)
    total = 0.0
    for rank, wi, lo, hi, along in _window_segments(spec, room, *env, True):
        if rank != 0:                       # 只算真正的對外採光面
            continue
        w = spec.walls[wi]
        keep = list(w.openings)
        w.openings = [op for op in keep
                      if not (op.kind == "window"
                              and poly.exterior.distance(
                                  Point(*w.point_at(op.position))) < 60)]
        free = _free_intervals(w, lo, hi, along, _column_blocks(spec, w, along))
        w.openings = keep
        total += sum(min(b - a, WINDOW_MAX_W) for a, b in free
                     if b - a >= WINDOW_MIN_W)
    return total


# ── 開門位置 ────────────────────────────────────────────────────────────────
def _door_position(spec, wall, along, lo, hi, door_w=DOOR_WIDTH):
    """在外牆的 [lo,hi] 世界座標段內找一個開得下落地門的位置(沿牆座標)。

    要同時滿足:不撞既有洞口、不卡房間角落、門前站得住人(`_door_pos_ok` 一次驗完,
    與其他所有開門路徑同一套判準)。找不到回 None。"""
    from src.design.layout.narrow_house import DOOR_CLEAR_STEPS, _door_pos_ok

    a0, b0 = abs(lo - along), abs(hi - along)
    a0, b0 = min(a0, b0), max(a0, b0)
    if b0 - a0 < door_w:
        return None
    taken = [(op.position - op.width / 2.0 - 100.0,
              op.position + op.width / 2.0 + 100.0) for op in wall.openings]
    half = door_w / 2.0
    mid = (a0 + b0) / 2.0
    step = 100.0
    n = int((b0 - a0 - door_w) / step)
    cands = [mid] + [a0 + half + i * step for i in range(n + 1)]
    for clear in DOOR_CLEAR_STEPS:              # 先求舒適淨距,擺不下再放寬
        for pos in cands:
            a, b = pos - half, pos + half
            if a < a0 - 1e-6 or b > b0 + 1e-6:
                continue
            if a < 0 or b > wall.length:
                continue
            if not all(b < t0 or a > t1 for t0, t1 in taken):
                continue
            if not _door_pos_ok(spec, wall, pos, door_w, clear):
                continue
            return pos
    return None


# ── 主流程 ──────────────────────────────────────────────────────────────────
def _yard_space(spec, env, side: str) -> float:
    """建築某一側到地界線之間的空地深度(mm)。"""
    ys = [p[1] for p in spec.site_boundary]
    return env[1] - min(ys) if side == "south" else max(ys) - env[3]


def add_balconies(spec, level: int, *, sides=SIDES, env=None) -> BalconyReport:
    """替這一層的前/後居室各挑出一座陽台(直接改 spec)。回 BalconyReport。

    ⚠️ 呼叫時機:要在**門補齊之後、補窗之前**——補窗器才會把落地門當成既有洞口
    繞開;補門器則已經替每間房安排好正式的室內門(陽台門不能拿來充當房間的門)。"""
    from src.design.layout.narrow_house import _need_window_width
    from src.design.layout.plan_check import building_env

    rep = BalconyReport()
    lb = getattr(spec, "floor_label", "") or f"{level}F"
    if level < 2:                                   # 一樓臨路面是大門/車庫/前院
        return rep
    env = building_env(spec) if env is None else env

    for side in sides:
        line = env[1] if side == "south" else env[3]
        space = _yard_space(spec, env, side)
        depth = min(MAX_DEPTH, space - YARD_CLEAR)
        # 這一側挑最長外牆的那間居室(最像「主要陽台」該掛的房間)
        best = None
        for room in spec.rooms:
            if room.kind not in HOST_KINDS:
                continue
            seg = _room_edge_on(Polygon(room.points), line)
            if seg is None:
                continue
            if best is None or (seg[1] - seg[0]) > (best[1][1] - best[1][0]):
                best = (room, seg)
        if best is None:
            continue
        room, (lo, hi) = best
        if depth < MIN_DEPTH:
            rep.skipped.append((lb, room.name,
                                f"院子只剩 {space/1000:.1f}m,挑不出 1.2m 深的陽台"))
            continue
        width = hi - lo                             # 滿寬(通長一條,見 WIDTH_RATIO)
        if width < MIN_WIDTH:
            rep.skipped.append((lb, room.name,
                                f"外牆只有 {width/1000:.1f}m,放不下落地門"))
            continue
        cover = _wall_covering(spec, line, lo, hi)
        if cover is None:
            continue
        wi, along = cover
        wall = spec.walls[wi]
        # ⚠️ 先把這間房開在這道牆上的窗**收回來**再找門位:那些窗是 rooms_to_spec 的
        #    預設配置(擺在最長外牆的正中),不讓開就正好卡在落地門該在的位置。窗等
        #    一下由 _ensure_room_windows 依 §40 重開(它才是採光的保證),所以收掉是
        #    安全的;能不能開得回來,下面 _daylight_capacity 會先量過。
        _release_windows(spec, room, wi)
        # 門寬:先試舒服的 1.2m,窄面寬吃不消就退到法定下限 0.9m(採光優先)。
        chosen = None
        for door_w in (DOOR_WIDTH, DOOR_MIN_WIDTH):
            pos = _door_position(spec, wall, along, lo + SIDE_CLEAR,
                                 hi - SIDE_CLEAR, door_w)
            if pos is None:
                continue
            wall.openings.append(Opening(pos, door_w, "door"))
            # 採光讓路:門吃掉牆之後,這間房還開得出 §40 差額的窗嗎?
            # ⚠️ 要**扣掉落地門自己**:§40 講的是「採光用窗**或開口**」,通往陽台
            #    的落地玻璃門就是開口,`code_check` 就是這樣算的。這裡以前只算窗,
            #    等於用比關卡更嚴的尺擋掉自己的陽台 —— 同一件事兩個地方兩把尺,
            #    實測 7×15m 的臥室差 30mm 就被判「放了門就開不滿窗」,其實合格。
            need = _need_window_width(room) - door_w
            if _daylight_capacity(spec, room, env) + 1.0 >= need:
                chosen = (pos, door_w)
                break
            wall.openings.pop()
        if chosen is None:
            rep.skipped.append((lb, room.name,
                                "外牆放不下落地門(或放了就開不滿 §40 的窗)"))
            continue
        pos, door_w = chosen
        spec.doors.append(DoorPlacement(wi, len(wall.openings) - 1,
                                        Door(sliding=True)))
        oy = line - depth if side == "south" else line
        spec.balconies.append(Balcony(
            origin=(lo, oy), width=width, depth=depth,
            attach=("north" if side == "south" else "south")))
        px, py = wall.point_at(pos)
        rep.items.append(BalconyItem(lb, room.name, side, width, depth,
                                     px, py, door_w))
    return rep


def _release_windows(spec, room, wall_index: int) -> None:
    """把某間房開在某道牆上的窗全部收回(索引重映射交給 _remove_openings)。"""
    from src.design.layout.narrow_house import _remove_openings

    poly = Polygon(room.points)
    wall = spec.walls[wall_index]
    targets = {(wall_index, oi) for oi, op in enumerate(wall.openings)
               if op.kind == "window"
               and poly.exterior.distance(Point(*wall.point_at(op.position))) < 60}
    _remove_openings(spec, targets)


def balcony_report(floors) -> BalconyReport:
    """整棟已配好的陽台 → BalconyReport(唯讀,給網站/報告用)。

    floors = [(label, spec), ...]。不動 spec,只把 spec.balconies 讀成報表。"""
    rep = BalconyReport()
    for item in floors:
        label, spec = item[0], item[1]
        for bal in getattr(spec, "balconies", None) or []:
            side = "south" if bal.attach == "north" else "north"
            doors = balcony_doors(spec, bal)
            px, py = (doors[0][0].point_at(doors[0][1].position)
                      if doors else (float("nan"), float("nan")))
            dw = doors[0][1].width if doors else 0.0
            poly = balcony_polygon(bal)
            host = next((r.name for r in spec.rooms
                         if Polygon(r.points).distance(poly) < EDGE_TOL
                         and r.kind in HOST_KINDS), "")
            rep.items.append(BalconyItem(label, host, side, bal.width,
                                         bal.depth, px, py, dw))
    return rep
