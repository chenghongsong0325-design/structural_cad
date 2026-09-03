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
    opening_on_column   門窗開口壓在柱上(柱穿過窗框/門洞,蓋不出來)
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
    no_cross_ventilation 這一層找不到一對「拉直線不被牆擋」的對外窗 → 風進不來
    bath_no_window      浴廁沒有對外窗(濕氣淤積、發霉)

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
OPENING_COLUMN_BITE = 10.0  # 柱吃掉開口沿牆這麼多 mm 以上算「開口壓柱」
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


def opening_body(wall, op) -> Polygon:
    """一個門窗開口在圖上佔的實體(沿牆 op.width × 牆厚)。

    量「開口跟誰打架」一律用這塊,判準才跟畫出來的圖一致。"""
    ux, uy = wall.unit_vector
    nx, ny = wall.normal_vector
    cx, cy = wall.point_at(op.position)
    h, t = op.width / 2.0, wall.thickness / 2.0
    return Polygon([
        (cx - ux * h - nx * t, cy - uy * h - ny * t),
        (cx + ux * h - nx * t, cy + uy * h - ny * t),
        (cx + ux * h + nx * t, cy + uy * h + ny * t),
        (cx - ux * h + nx * t, cy - uy * h + ny * t),
    ])


def column_bite(wall, op, cols) -> float:
    """柱吃掉這個開口沿牆多少 mm(0 = 沒壓到)。

    回「沿牆的 mm」而不是面積:使用者看圖問的是「這扇窗被柱吃掉多寬」,
    面積得再心算除以牆厚才看得懂。"""
    if not cols:
        return 0.0
    body = opening_body(wall, op)
    area = sum(body.intersection(c).area for c in cols)
    return area / wall.thickness if wall.thickness else 0.0


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

    # ④c 門窗開口不得壓在柱上(使用者 2026-08-20:「柱子會在窗戶裡面」)
    #     ⚠️ 這條跟 ④b 家具壓柱**不同級**,是 error:柱穿過窗框/門洞根本蓋不出來,
    #     而且開口是**沿著牆挪就能解**的(四條產線都有躲柱機制,
    #     narrow_house._column_blocks / layout_generator._blocked),換個切法必定救得動。
    #     ⚠️ 判準只認「真的重疊」,不是 COLUMN_CLEARANCE(300mm 淨距)——那是產生端
    #     排洞口時要留的餘裕,拿來當關卡會把只差幾十 mm、其實蓋得出來的圖也擋掉。
    for w in spec.walls:
        for op in w.openings:
            bite = column_bite(w, op, cols)
            if bite > OPENING_COLUMN_BITE:
                px, py = w.point_at(op.position)
                what = "窗" if op.kind == "window" else "門"
                issues.append(PlanIssue(
                    "error", "opening_on_column", lb, "",
                    f"{what}洞({px:.0f},{py:.0f})壓在柱上,柱吃掉洞口 {bite:.0f}mm"
                    f"(柱穿過{what}框,蓋不出來)"))

    # ⑤ 門不得卡在房間角落(人走不進那個角 → 動線判不通,看圖卻不明顯)
    from src.design.layout.narrow_house import DOOR_CORNER_MIN, _door_pos_ok
    for w in spec.walls:
        for op in w.openings:
            if op.kind != "door":
                continue
            # ⚠️ **開放通道不吃這條。** 這條問的是「門扇旁邊站不站得下人」;走道口
            #    那種滿寬的洞口沒有門扇,兩端就是結構(導牆/界牆),它貼著牆角是
            #    對的 —— 真實透天的走道口就是從界牆直接開到底(使用者 2026-09-02:
            #    「走道出入口都不用設置門,也不用用牆隔起來」)。
            if getattr(op, "is_passage", False):
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
    issues.extend(_ventilation_issues(spec, env, lb))
    return issues


#: 從窗往室內縮多少再拉直線(起點不要落在牆體上)。
VENT_PROBE = 200.0


def _wall_solid_union(spec):
    """牆的**實體聯集**(已扣掉門窗洞口)—— 風的直線碰到它才叫被擋住。

    ⚠️ 名字不要叫 `_wall_bodies` —— 本檔已經有一支同名的(每道牆一塊、**不扣
    洞口**),而且 `door_rules` 直接 import 它。重名會把它整支換掉,症狀是
    「'MultiPolygon' object is not iterable」出現在完全不相干的修門程式裡。

    洞口一律當「開著」:書上的 OK 圖寫的就是「只要保持房門開啟,前後窗就能
    通風對流」(使用者 2026-09-03 給的〈9 種常見 NG 格局〉NG01)。
    """
    from shapely.ops import unary_union

    from src.drafting.wall import solid_segments

    segs = []
    for w in spec.walls:
        ux, uy = w.unit_vector
        sx, sy = w.start
        for a, b in solid_segments(w.length, w.openings):
            if b - a < 1.0:
                continue
            segs.append(LineString([(sx + ux * a, sy + uy * a),
                                    (sx + ux * b, sy + uy * b)])
                        .buffer(w.thickness / 2.0, cap_style=2, join_style=2))
    return unary_union(segs) if segs else None


#: 每扇窗沿寬度取幾個取樣點(風從洞口的哪一段過都算)。
VENT_SAMPLES = 5


def _exterior_window_points(spec, env) -> list:
    """對外窗 → [([沿寬度的取樣點], 牆法線)];風要從這裡進、從另一個出。

    ⚠️ **一扇窗不是一個點。** 只拿窗心拉線的話,明明已經對齊走道、重疊 500mm
    的一對前後窗會被判成不通(實測 4.5~7m 面寬全中)——因為兩個窗心連成的那條
    線剛好落在走道外面。窗有寬度,風從洞口的哪一段過都算,所以要沿寬度取樣。

    ⚠️ **開向天井的窗也算對外窗。** 天井貫穿到屋頂、是通到外面的空氣,連棟街屋
    開天井本來就是為了通風採光。只認建築外緣的話,開了天井反而會判成「通風變差」
    —— 這跟 2026-08-26 `code_check` 的 §41 是**同一個 bug**(當時 `_is_exterior`
    只認建築外緣,天井開了窗也不算採光,等於天井完全沒有意義),只是這次出現在
    通風這條。實測 7×15.5m 開天井後浴廁採光 3 件 → 0,卻冒出 1 件
    `no_cross_ventilation`,`_fit_patio_auto` 因此把天井整個退掉。
    """
    out = []
    patios = [Polygon(r.points).buffer(EDGE_TOL) for r in spec.rooms
              if r.kind == "patio"]
    # ⚠️ **直接走牆上的洞口,不要繞 `spec.windows` 的索引。** 那份清單存的是
    #    (wall_index, opening_index),別的地方一動洞口就可能過期 —— 檢查器拿
    #    過期的索引去查會 IndexError 整支炸掉(`test_detects_removed_doors`
    #    故意拆光門洞就撞到)。**關卡要能吃畸形的圖**,那正是它要回報的東西。
    for w in spec.walls:
        for op in w.openings:
            if op.kind != "window":
                continue
            px, py = w.point_at(op.position)
            if not (_on_envelope(px, py, env)
                    or any(p.contains(Point(px, py)) for p in patios)):
                continue
            ux, uy = w.unit_vector
            half = op.width / 2.0 * 0.9          # 留一點邊,不要取在窗框上
            pts = []
            for k in range(VENT_SAMPLES):
                t = -half + (2 * half) * k / max(1, VENT_SAMPLES - 1)
                pts.append((px + ux * t, py + uy * t))
            src = next((k for k, pp in enumerate(patios)
                        if pp.contains(Point(px, py))), -1)   # -1 = 建築外緣
            out.append((pts, (-uy, ux), src))
    return out


def has_cross_ventilation(spec, env=None) -> bool:
    """這一層有沒有**對流**:兩個對外窗之間拉一條直線,沒有被牆擋住。

    ⚠️ 判準就是書上教的量法(使用者 2026-09-03):「風走最短直線,沒辦法像人
    一樣轉身,用同一個開窗不能同時進與出」——所以要**兩個**對外窗,而且
    「用尺在窗與窗之間拉直線,觀察直線有沒有被阻斷」。

    ⚠️ **同一個天井上的兩扇窗不算一對。** 書上那句「同一個開窗不能同時進與出」
    講的是同一個空氣來源 —— 一座天井就是一個來源,兩扇窗都開向它等於進出同一口
    井。不擋這件事的話,「開了天井就一定通風」會變成免費通過的假合格。
    """
    env = env if env is not None else building_env(spec)
    wins = _exterior_window_points(spec, env)
    if len(wins) < 2:
        return False
    bodies = _wall_solid_union(spec)
    if bodies is None:
        return True
    for i in range(len(wins)):
        for j in range(i + 1, len(wins)):
            (ps, np_, si), (qs, nq, sj) = wins[i], wins[j]
            if si >= 0 and si == sj:
                continue                        # 同一座天井:同一個空氣來源
            for s1 in (1.0, -1.0):
                for s2 in (1.0, -1.0):
                    for p in ps:
                        a = (p[0] + np_[0] * VENT_PROBE * s1,
                             p[1] + np_[1] * VENT_PROBE * s1)
                        for q in qs:
                            b = (q[0] + nq[0] * VENT_PROBE * s2,
                                 q[1] + nq[1] * VENT_PROBE * s2)
                            if not LineString([a, b]).intersects(bodies):
                                return True
    return False


def _ventilation_issues(spec, env, lb) -> list:
    """NG01:通風要對流 + 用水空間要有對外窗(使用者 2026-09-03 給的判準)。

    ⚠️ 兩條都是 **warning**:要多開一扇對外窗、或把核搬開,那是**改設計**,
    不是「換個切法」—— 照本檔的分界原則就該歸 warning(判成 error 會讓產線
    為了救不動的東西無限重生)。
    """
    env = env if env is not None else building_env(spec)
    out = []
    if not has_cross_ventilation(spec, env):
        out.append(PlanIssue("warning", "no_cross_ventilation", lb, "",
                             "找不到一對拉直線不被牆擋的對外窗,風進得來出不去"))
    patios = [Polygon(r.points) for r in spec.rooms if r.kind == "patio"]
    for r in spec.rooms:
        if r.kind not in ("bathroom", "toilet"):
            continue
        poly = Polygon(r.points)
        lit = any(poly.distance(p) < EDGE_TOL for p in patios)
        for w in spec.walls:                    # 同上:不繞 spec.windows 的索引
            for op in w.openings:
                if op.kind != "window":
                    continue
                if (poly.exterior.distance(Point(*w.point_at(op.position)))
                        < EDGE_TOL):
                    lit = True
        if not lit:
            out.append(PlanIssue("warning", "bath_no_window", lb, r.name,
                                 "沒有對外窗,濕氣排不掉(容易發霉)"))
    return out


def check_building(floors, env=None) -> PlanCheckReport:
    """檢查整棟。floors = [(label, spec, ...)](多的欄位忽略)。

    env=None → 每層各自由 spec 推建築外框(深基地封頂留院子時才不會誤判)。"""
    issues: list[PlanIssue] = []
    for item in floors:
        label, spec = item[0], item[1]
        level = int(label[:-1]) if label[:-1].isdigit() else 1
        issues.extend(check_floor(spec, env, level, label))
    return PlanCheckReport(issues)
