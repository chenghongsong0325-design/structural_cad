"""圖面正確性檢查器(硬性關卡)—— 保證輸出的平面圖「一定能用」。

前面各階段是「盡量畫好」;這一層是**把關**:一張圖只要違反下列任何一條硬規則,
就不算合格圖,產線會換個切法重生,不會輸出給使用者。這樣才不是修一個 bug 補一個
補丁,而是「以後的圖都不會有這些問題」。

硬錯誤(error,落實端一定救得動 → 不合格就重生)::

    room_no_door        可進入的房間沒有門(進不去)
    floor_split         同一層室內斷成好幾塊(要繞到室外才能過去)
    no_entry            1F 沒有對外大門(進不了建築)
    entry_upstairs      樓上外牆開門(門通往空中)
    furniture_in_wall   家具嵌進牆體(畫出來是穿牆)
    door_in_corner      門洞卡在房間角落(人走不進那個角)
    stair_blocks_door   門直接開在階梯上(缺起步平台,門扇會掃到踏step)
    stair_side_open     梯段有一側沒牆(人走上去會從旁邊掉下去)
    balcony_no_door     陽台沒有門通到(畫了一塊到不了的地)
    entry_door_narrow   對外大門淨寬 <90cm(門與動線規範)
    room_door_narrow    居室門 <80cm / 衛浴門 <75cm(同上)
    door_swing_blocked  門的開啟弧線撞到牆/柱/家具/另一扇門(同上)
    bath_door_to_kitchen 衛浴門直接開向廚房或神明廳(同上)
    through_bedroom     要穿越別人的臥室才進得去(套內衛浴除外,同上)
    stair_wrapped       樓梯間被私人房間包住,沒接到該層公共動線(同上)
    circulation_blocked 房間走不進去/家具擋死動線

設計警告(warning,要改「房間怎麼配」才救得動,由收斂迴圈回饋給 LLM)::

    room_no_daylight    居室是內間(四周無外牆也無天井)→ 沒窗
    room_oversize       居室大到不合理
    room_skinny         居室細長得像走廊

⚠️ 分界原則:**error = 同一份房間關係圖、換個切法就能解決**;warning = 非改設計
   (房間數/相鄰關係)不可。所以只有 error 拿來擋圖,warning 只回報。
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field

from shapely.geometry import LineString, Point, Polygon

# 這些「房間」是封閉服務豎井,本來就不設門窗,所有規則都跳過。
VOID_KINDS = {"pipe_shaft", "patio"}
# 需要對外採光的居室(內間沒光要回報)。
DAYLIGHT_KINDS = {"living", "dining", "bedroom", "master_bedroom", "study",
                  "elder_room"}
# 這些房間本來就細長,不檢查長寬比。
# stair_hall:樓梯間裝的是一整段梯跑(4m 長 × 一梯段多寬),本來就是長條;
# 那是垂直動線不是居室,不該用居室的長寬比去挑毛病。
SKINNY_OK_KINDS = {"corridor", "pipe_shaft", "patio", "balcony", "storage",
                   "utility", "stair_hall"}

EDGE_TOL = 60.0             # 貼邊/貼牆的容差(mm)
WALL_OVERLAP_TOL = 1000.0   # 家具與牆重疊超過這個面積(mm²)算穿牆
OVERSIZE_RATIO = 1.5        # 面積超過理想上限這麼多倍算過大

# 併合/開放式的房名 → 它其實是哪幾間併起來的。「餐廚」= 餐廳+廚房、
# 「客餐廳」= 客廳+餐廳:面積本來就是兩間相加,拿單間的上限去量,正常的
# 開放式格局會被判成過大。名字用 in 比對(「客餐廳」帶後綴)。
MERGED_ROOM_PARTS = {"客餐": ("living", "dining"), "餐廚": ("dining", "kitchen")}
ASPECT_LIMIT = 2.8          # 居室長寬比超過這個算細長
STAIR_LANDING_MIN = 600.0   # 門與第一階之間至少要有這麼深的平地(起步平台)


def oversize_band(room, table: dict) -> tuple | None:
    """量這間房「會不會太大」該用哪一段面積範圍;查不到回 None(=不查過大)。

    ⚠️ 兩個坑,少一個就會**量錯尺**,把正常的設計判成過大:

      ① **主臥的 kind 也是 `"bedroom"`**(規則版兩帶式這樣存,只有名字帶「主臥」)。
         直接查表會拿**次臥**的上限去量主臥 —— 主臥本來就該比次臥大。
      ② **「餐廚」「客餐廳」是兩間併成一間**,上限要相加。開放式餐廚被拿去跟
         單獨一間餐廳比,一定超標,但那正是我們自己選的開放式格局。

    實測 19×13 三層:10 個「房間過大」裡有 5 個是這兩個坑,不是設計問題。
    ⚠️ `benchmark.check_rooms` 有一份同樣意圖的舊實作(`_req_for` /
    `_MERGED_HINTS`),那支是報告用的,尚未併過來 —— 改判準時兩邊要一起改。
    """
    for hint, parts in MERGED_ROOM_PARTS.items():
        if hint in room.name:
            bands = [table.get(p) for p in parts]
            if all(b is not None for b in bands):
                return (sum(b[0] for b in bands), sum(b[1] for b in bands))
    if room.kind == "bedroom" and "主臥" in room.name:
        return table.get("master_bedroom", table.get(room.kind))
    return table.get(room.kind)


def _stair_footprint(stair):
    """樓梯踏step 佔的矩形(世界座標)。拿不到幾何回 None。"""
    from shapely.geometry import box
    try:
        ox, oy = stair.origin
        w, ln = stair.width, stair.length
    except Exception:
        return None
    if getattr(stair, "direction", "north") in ("north", "south"):
        return box(ox, oy, ox + w, oy + ln)
    return box(ox, oy, ox + ln, oy + w)


@dataclass(frozen=True)
class PlanIssue:
    """一條檢查結果。severity: "error"(擋圖)/ "warning"(只回報)。"""

    severity: str
    code: str
    floor: str
    room: str
    detail: str

    def __str__(self) -> str:
        mark = "❌" if self.severity == "error" else "⚠️"
        who = f"{self.floor}/{self.room}" if self.room else self.floor
        return f"{mark} {who}:{self.detail}"


@dataclass
class PlanCheckReport:
    """整棟(或單層)的檢查報表。"""

    issues: list[PlanIssue] = field(default_factory=list)

    @property
    def errors(self) -> list[PlanIssue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self) -> list[PlanIssue]:
        return [i for i in self.issues if i.severity == "warning"]

    @property
    def ok(self) -> bool:
        """沒有硬錯誤 = 這張圖可以出。"""
        return not self.errors

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "n_errors": len(self.errors),
            "n_warnings": len(self.warnings),
            "issues": [asdict(i) for i in self.issues],
        }

    def to_json(self, **kw) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, **kw)

    def summary(self) -> str:
        if not self.issues:
            return "✅ 圖面檢查全過(無錯誤、無警告)"
        head = (f"{'✅' if self.ok else '❌'} 錯誤 {len(self.errors)} 項、"
                f"警告 {len(self.warnings)} 項")
        return "\n".join([head, *(str(i) for i in self.issues)])


# ── 幾何小工具 ──────────────────────────────────────────────────────────────
def _wall_bodies(spec) -> list:
    """每道牆的實體多邊形(中心線往兩側各推半個牆厚)。"""
    return [LineString([w.start, w.end]).buffer(w.thickness / 2.0,
                                                cap_style=2, join_style=2)
            for w in spec.walls]


def _openings_of(spec, poly: Polygon, kind: str) -> list:
    """房間邊界上的門洞/窗洞中心點。"""
    out = []
    for w in spec.walls:
        for op in w.openings:
            if op.kind != kind:
                continue
            p = Point(*w.point_at(op.position))
            if poly.exterior.distance(p) < EDGE_TOL:
                out.append(p)
    return out


def _on_envelope(x: float, y: float, env) -> bool:
    """這個點是不是落在建築外框上(= 對外開口)。"""
    return (abs(y - env[1]) < EDGE_TOL or abs(y - env[3]) < EDGE_TOL
            or abs(x - env[0]) < EDGE_TOL or abs(x - env[2]) < EDGE_TOL)


def _room_graph_components(spec) -> list[set]:
    """靠門/開放通道互通的房間分群(豎井/天井不算)。斷成多塊 = 室內走不通。

    ⚠️ 兩間房之間**沒畫牆**也是通的(集合住宅的玄關↔起居室就是這樣做),
    只認門會把開放格局誤判成走不通。"""
    import itertools

    from src.design.layout.room_circulation import _open_passages

    polys = [(r, Polygon(r.points)) for r in spec.rooms]
    live = [i for i, (r, _p) in enumerate(polys) if r.kind not in VOID_KINDS]
    adj = {i: set() for i in live}
    pts = [Point(*w.point_at(op.position)) for w in spec.walls
           for op in w.openings if op.kind == "door"]
    for i in live:                                  # 開放通道:邊界上沒牆的缺口
        pts += _open_passages(spec, polys[i][1])
    for p in pts:
        touch = [i for i in live
                 if polys[i][1].exterior.distance(p) < EDGE_TOL]
        for a, b in itertools.combinations(touch, 2):
            adj[a].add(b)
            adj[b].add(a)
    comps, seen = [], set()
    for i in live:
        if i in seen:
            continue
        grp, stack = {i}, [i]
        seen.add(i)
        while stack:
            k = stack.pop()
            for n in adj[k]:
                if n not in grp:
                    grp.add(n)
                    seen.add(n)
                    stack.append(n)
        comps.append(grp)
    return comps


# ── 單層檢查 ────────────────────────────────────────────────────────────────
def building_env(spec) -> tuple:
    """從 spec 自己推建築外框(所有房間的外接矩形)。

    為什麼需要:呼叫端習慣用「基地 − 退縮」當外框,但產生器會**封頂建築深度、
    多的地留成院子**(窄透天深基地、兩帶式皆然)。那時基地框比建築框大,窗與大門
    就會被誤判成「不在外牆上」→ 冒出假的 no_entry / 採光 0。"""
    xs = [p[0] for r in spec.rooms for p in r.points]
    ys = [p[1] for r in spec.rooms for p in r.points]
    return (min(xs), min(ys), max(xs), max(ys)) if xs else (0.0, 0.0, 0.0, 0.0)


def check_floor(spec, env=None, level: int = 1, label: str = "") -> list[PlanIssue]:
    """檢查一層,回問題清單(error + warning)。

    env=(x0,y0,x1,y1) 建築外框;**給 None 就由 spec 自己推**(建議),免得傳進
    比建築大的基地框而誤判(見 building_env)。"""
    env = building_env(spec) if env is None else env
    from src.design.layout.graph_layout import AREA_BAND
    from src.design.layout.room_circulation import analyze_room_circulation

    lb = label or getattr(spec, "floor_label", "") or f"{level}F"
    issues: list[PlanIssue] = []
    polys = [(r, Polygon(r.points)) for r in spec.rooms]
    patios = [p for r, p in polys if r.kind == "patio"]

    # ① 每間可進入的房間都要有門(或**開放通道**:兩間房之間根本沒畫牆也算通)
    from src.design.layout.room_circulation import _room_openings
    for r, poly in polys:
        if r.kind in VOID_KINDS:
            continue
        if not _room_openings(spec, poly):
            issues.append(PlanIssue("error", "room_no_door", lb, r.name,
                                    "沒有門也沒有開放通道,進不去"))

    # ② 同層室內要連通(不能要繞到室外)
    comps = _room_graph_components(spec)
    if len(comps) > 1:
        groups = [[spec.rooms[i].name for i in c] for c in comps]
        issues.append(PlanIssue("error", "floor_split", lb, "",
                                f"室內斷成 {len(comps)} 塊,彼此走不通:{groups}"))

    # ③ 對外門:1F 恰一扇、樓上不得有
    #    ⚠️ 樓上外牆的門**通往陽台就不算**——那正是陽台存在的意義(落地門出去有地
    #    可站);沒有陽台接著的樓上外門才是「門通往空中」。
    from src.design.layout.balcony import balcony_doors, door_opens_to_balcony
    ext = [(w, op) for w in spec.walls for op in w.openings
           if op.kind == "door" and _on_envelope(*w.point_at(op.position), env)
           and not door_opens_to_balcony(spec, w, op)]
    if level == 1 and not ext:
        issues.append(PlanIssue("error", "no_entry", lb, "",
                                "沒有對外大門,進不了建築"))
    if level != 1 and ext:
        issues.append(PlanIssue("error", "entry_upstairs", lb, "",
                                f"樓上外牆開了 {len(ext)} 扇門(門會通往空中)"))

    # ③b 每座陽台都要有門進得去(畫了一塊到不了的地,是真實圖面不會有的錯誤)
    for bal in getattr(spec, "balconies", None) or []:
        if not balcony_doors(spec, bal):
            x0, y0 = bal.origin
            issues.append(PlanIssue(
                "error", "balcony_no_door", lb, "陽台",
                f"陽台({x0:.0f},{y0:.0f})沒有門通到,進不去"))

    # ④ 家具不得嵌進牆體
    from src.design.collision.geometry import fixture_obstacles
    bodies = _wall_bodies(spec)
    obstacles = list(fixture_obstacles(spec))
    for o in obstacles:
        overlap = sum(o.poly.intersection(b).area for b in bodies)
        if overlap > WALL_OVERLAP_TOL:
            issues.append(PlanIssue(
                "error", "furniture_in_wall", lb, getattr(o, "tag", "家具"),
                f"家具嵌進牆體 {overlap/1e6:.3f}㎡(畫出來是穿牆)"))

    # ④b 家具壓在柱上 → **warning,不是 error**。
    #     ⚠️ 分類理由(很重要,不要好心改成 error):柱是結構物、位置由軸網決定,
    #     擺位器只能「盡力挪開」(fixture_fix.clear_fixtures_off_columns),挪開
    #     會撞到牆/門迴轉時寧可留著壓柱 —— 那是刻意的取捨。列成 error 會讓產線
    #     為了一件修不掉的家具無限重生,也會讓網站對一張其他都合格的圖回 422。
    #     與 ④ 分開列:穿牆是「貼牆樣板算錯半個牆厚」,壓柱是「柱比牆胖」,
    #     成因與解法都不同,混在一起看不出是哪一種。
    from src.design.column_design import column_footprints
    cols = column_footprints(spec)
    for o in obstacles:
        overlap = sum(o.poly.intersection(c).area for c in cols)
        if overlap > WALL_OVERLAP_TOL:
            issues.append(PlanIssue(
                "warning", "furniture_in_column", lb, getattr(o, "tag", "家具"),
                f"家具壓在柱上 {overlap/1e6:.3f}㎡(柱角凸出牆面,擺不進去)"))

    # ⑤ 門不得卡在房間角落(人走不進那個角 → 動線判不通,看圖卻不明顯)
    from src.design.layout.narrow_house import DOOR_CORNER_MIN, _door_pos_ok
    for w in spec.walls:
        for op in w.openings:
            if op.kind != "door":
                continue
            if not _door_pos_ok(spec, w, op.position, op.width,
                                DOOR_CORNER_MIN):        # 動線走得通的物理下限
                px, py = w.point_at(op.position)
                issues.append(PlanIssue(
                    "error", "door_in_corner", lb, "",
                    f"門洞({px:.0f},{py:.0f})貼著房間角落,人走不進去"))

    # ⑥ 門不得直接開在階梯上(門與第一階之間要有起步平台可站)
    from src.design.layout.narrow_house import _door_clear_of_stairs
    if getattr(spec, "stairs", None):
        for w in spec.walls:
            for op in w.openings:
                if op.kind != "door":
                    continue
                if not _door_clear_of_stairs(spec, w, op.position, op.width):
                    p = Point(*w.point_at(op.position))
                    issues.append(PlanIssue(
                        "error", "stair_blocks_door", lb, "",
                        f"門洞({p.x:.0f},{p.y:.0f})正對階梯,開門就要踩上踏step"
                        f"(門前需有可站的平地)"))

    # ⑥b 梯段兩側都要有牆:走在階梯上,旁邊是空的就會掉下去(只有起步/折返平台
    #     那種平地才可以開口)。
    from src.design.layout.narrow_house import _flight_sides, _side_is_walled
    for st in getattr(spec, "stairs", None) or []:
        sides, _vertical = _flight_sides(st)
        for (p0, p1) in sides:
            if not _side_is_walled(spec, (p0, p1)):
                issues.append(PlanIssue(
                    "error", "stair_side_open", lb, "",
                    f"梯段側邊({p0[0]:.0f},{p0[1]:.0f})→({p1[0]:.0f},{p1[1]:.0f})"
                    f"沒有牆,人走在階梯上會掉下去"))

    # ⑥c 門與動線規範(使用者 2026-07-30 定調):門淨寬、開啟弧線、衛浴門朝向、
    #     不得穿越臥室、樓梯不得被房間包住。判準集中在 door_rules,這裡只接進來。
    from src.design.layout.door_rules import check_door_rules
    issues += check_door_rules(spec, env, level, lb)

    # ⑦ 動線:每間房走得進去、家具沒擋死
    rep = analyze_room_circulation(spec)
    if not rep.ok:
        for rc in rep.blocked:
            issues.append(PlanIssue("error", "circulation_blocked", lb, rc.name,
                                    f"動線不通({rc.reason})"))

    # ⑧ 設計面警告(落實端救不動,回饋給 LLM 重設計)
    for r, poly in polys:
        if r.kind in VOID_KINDS:
            continue
        x0, y0, x1, y1 = poly.bounds
        if r.kind in DAYLIGHT_KINDS:
            lit = (abs(y0 - env[1]) < EDGE_TOL or abs(y1 - env[3]) < EDGE_TOL
                   or any(poly.distance(p) < EDGE_TOL for p in patios))
            if not lit:
                issues.append(PlanIssue("warning", "room_no_daylight", lb,
                                        r.name, "是內間,沒有對外採光面"))
        band = oversize_band(r, AREA_BAND)
        if band and r.area_m2 > band[1] * OVERSIZE_RATIO:
            issues.append(PlanIssue("warning", "room_oversize", lb, r.name,
                                    f"{r.area_m2:.0f}㎡ 過大(理想 ≤{band[1]:.0f}㎡)"))
        if r.kind not in SKINNY_OK_KINDS:
            side = max(x1 - x0, y1 - y0), max(min(x1 - x0, y1 - y0), 1.0)
            ar = side[0] / side[1]
            if ar > ASPECT_LIMIT:
                issues.append(PlanIssue("warning", "room_skinny", lb, r.name,
                                        f"長寬比 {ar:.1f},細長得像走廊"))
    return issues


def check_building(floors, env=None) -> PlanCheckReport:
    """檢查整棟。floors = [(label, spec, ...)](多的欄位忽略)。

    env=None → 每層各自由 spec 推建築外框(深基地封頂留院子時才不會誤判)。"""
    issues: list[PlanIssue] = []
    for item in floors:
        label, spec = item[0], item[1]
        level = int(label[:-1]) if label[:-1].isdigit() else 1
        issues.extend(check_floor(spec, env, level, label))
    return PlanCheckReport(issues)
