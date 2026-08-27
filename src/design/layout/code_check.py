"""法規檢查器(建築技術規則)—— 圖「畫得對」之外,還要「合法」。

`plan_check` 管的是**圖面正確性**(有沒有門、動線通不通、梯段兩側有沒有牆);
這一層管的是**法規尺寸**(級高級深、採光開口比、走道淨寬…)。兩層分開的理由:

  * 圖面錯誤 = 一定是 bug,換個切法就該解決 → 擋圖重生。
  * 法規不符 = 尺寸/開口不夠,要**改設計參數**(窗開大一點、樓梯間加深)。

⚠️ **條號誠實原則**:有把握的才寫條號(建築技術規則建築設計施工編),沒把握的
   只寫判準、標成「設計慣例」(warning),不編造條文。專題引用前請自行核對法規原文。

⚠️ 平面圖沒有的資訊一律**明說是估算**:窗戶只有寬度(高度不在平面圖上),採光面積
   用「窗寬 × 假設窗高 1.2m」估;層高由呼叫端給(預設 3.2m)。

分級::

    violation  違反法規尺寸(要改設計參數才救得動)
    warning    設計慣例/下限(不是法規,但真實圖面不會這樣做)

典型用法::

    rep = check_code_building(floors, env)     # floors = [(label, spec), ...]
    print(rep.summary())
    rep.to_json()
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field

from shapely.geometry import Point, Polygon

# ── 法規門檻(建築技術規則建築設計施工編)────────────────────────────────────
# §33 樓梯各部尺寸(供住宅使用之專用樓梯):級高 ≤20cm、級深 ≥21cm、
#     梯段(樓梯)淨寬 ≥75cm;平臺深度不得小於樓梯寬度。
MAX_RISER = 200.0
MIN_TREAD = 210.0
MIN_FLIGHT_W = 750.0
# §40 居室採光:採光用窗或開口面積 ≥ 該居室樓地板面積 1/8。
DAYLIGHT_RATIO = 1 / 8
# §43 通風:有效通風面積 ≥ 該居室樓地板面積 1/20(有窗通常就過)。
VENT_RATIO = 1 / 20
WINDOW_H_ASSUMED = 1200.0       # 平面圖沒有窗高 → 用住宅常見窗高估算(mm)

# 以下是**設計慣例**,不是法規條文(住宅非供公眾使用建築物,走廊寬度不受 §92 拘束)。
MIN_CORRIDOR_W = 900.0          # 走道淨寬(輪椅不過,但人走得舒服的下限)
MIN_ROOM_DOOR_W = 750.0         # 房門淨寬
MIN_ENTRY_DOOR_W = 900.0        # 大門淨寬
MIN_BEDROOM_AREA = 3.6          # 臥室最小面積(㎡)
MIN_BEDROOM_SIDE = 1800.0       # 臥室最短邊(擺得下床)

# 需要採光/通風的「居室」(廚房、浴廁、走道、儲藏、豎井不在此列)。
HABITABLE_KINDS = {"living", "dining", "bedroom", "master_bedroom", "study",
                   "elder_room"}

# §41:居室的採光開口不一定要開在建築外緣 —— 開向**天井/內庭**的窗一樣算採光,
# 這正是連棟街屋中段唯一的採光來源(左右是共同壁,只有前後兩端能對外)。以前
# `_is_exterior` 只認建築外緣,等於「天井開了窗也不算」,天井就完全沒有意義。
# ⚠️ 但天井不能無限小 —— 一條 30cm 的縫採不到光。真正的 §41 有一套隨建築高度
#    變化的最小尺寸公式(還牽涉遮蔽角),**本專案未實作**,這裡用一組保守的
#    下限代替:短邊 ≥1.5m 且面積 ≥3㎡ 才算數。這是**簡化**,不是法規檢討。
PATIO_MIN_SIDE = 1500.0
PATIO_MIN_AREA_M2 = 3.0
EDGE_TOL = 60.0


@dataclass(frozen=True)
class CodeIssue:
    """一條法規檢查結果。severity: "violation"(不合法規)/ "warning"(慣例)。"""

    severity: str
    code: str
    article: str            # 條號;沒把握就留空字串(表示這是設計慣例)
    floor: str
    room: str
    detail: str

    def __str__(self) -> str:
        mark = "⛔" if self.severity == "violation" else "⚠️"
        who = f"{self.floor}/{self.room}" if self.room else self.floor
        art = f"[{self.article}] " if self.article else "[慣例] "
        return f"{mark} {art}{who}:{self.detail}"


@dataclass
class CodeCheckReport:
    """整棟(或單層)的法規檢查報表。"""

    issues: list[CodeIssue] = field(default_factory=list)

    @property
    def violations(self) -> list[CodeIssue]:
        return [i for i in self.issues if i.severity == "violation"]

    @property
    def warnings(self) -> list[CodeIssue]:
        return [i for i in self.issues if i.severity == "warning"]

    @property
    def ok(self) -> bool:
        """沒有違反法規 = 這張圖送得出去(慣例警告不擋)。"""
        return not self.violations

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "n_violations": len(self.violations),
            "n_warnings": len(self.warnings),
            "issues": [asdict(i) for i in self.issues],
        }

    def to_json(self, **kw) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, **kw)

    def summary(self) -> str:
        if not self.issues:
            return "✅ 法規檢查全過(無違規、無慣例警告)"
        head = (f"{'✅' if self.ok else '⛔'} 違規 {len(self.violations)} 項、"
                f"慣例警告 {len(self.warnings)} 項")
        return "\n".join([head, *(str(i) for i in self.issues)])


# ── 幾何小工具 ──────────────────────────────────────────────────────────────
def _openings_on_room(spec, poly: Polygon, kind: str) -> list:
    """這間房邊界上的洞口 → [(wall, opening)]。"""
    out = []
    for w in spec.walls:
        for op in w.openings:
            if op.kind != kind:
                continue
            if poly.exterior.distance(Point(*w.point_at(op.position))) < EDGE_TOL:
                out.append((w, op))
    return out


def _is_exterior(wall, env) -> bool:
    (sx, sy), (ex, ey) = wall.start, wall.end
    if abs(sx - ex) < 1.0:
        return abs(sx - env[0]) < EDGE_TOL or abs(sx - env[2]) < EDGE_TOL
    return abs(sy - env[1]) < EDGE_TOL or abs(sy - env[3]) < EDGE_TOL


def daylight_patios(spec) -> list:
    """這一層裡「大到可以當採光來源」的天井(→ Polygon 清單)。

    太小的天井不算(見 PATIO_MIN_SIDE / PATIO_MIN_AREA_M2 的說明)。"""
    out = []
    for room in spec.rooms:
        if room.kind != "patio":
            continue
        poly = Polygon(room.points)
        x0, y0, x1, y1 = poly.bounds
        if min(x1 - x0, y1 - y0) < PATIO_MIN_SIDE:
            continue
        if poly.area / 1e6 < PATIO_MIN_AREA_M2:
            continue
        out.append(poly)
    return out


def _faces_daylight(wall, env, patios) -> bool:
    """這道牆是不是採光面:建築外緣,**或**貼著夠大的天井(§41)。"""
    if _is_exterior(wall, env):
        return True
    if not patios:
        return False
    (sx, sy), (ex, ey) = wall.start, wall.end
    mx, my = (sx + ex) / 2.0, (sy + ey) / 2.0
    probes = [Point(mx + dx, my + dy)
              for dx, dy in ((250, 0), (-250, 0), (0, 250), (0, -250))]
    return any(p.contains(q) for p in patios for q in probes)


def _stair_dims(stair, floor_height: float):
    """→ (級高, 級深, 梯段淨寬, 平臺深);拿不到的回 None。"""
    tread = getattr(stair, "tread", None)
    spf = getattr(stair, "steps_per_flight", None)
    steps = getattr(stair, "steps", None)
    if spf:                                     # UStair:兩段折返
        total = spf * 2
        width = getattr(stair, "flight_width", None)
        landing = getattr(stair, "landing_depth", None)
    elif steps:                                 # Stair:單跑直梯
        total = steps
        width = getattr(stair, "width", None)
        # ⚠️ 單跑直梯**沒有折返平臺**:頂端那段只是梯段與牆之間的空隙,不是 §33
        #    講的「平臺」(平臺深 ≥梯段寬 是針對轉向/折返處)。拿空隙去比梯段寬,
        #    會把每一座直梯都誤判成違規(實測兩帶式透天全中)。故回 None = 不檢查。
        landing = None
    else:
        return None
    if not total or not tread or not width:
        return None
    return floor_height / total, tread, width, landing


# ── 單層檢查 ────────────────────────────────────────────────────────────────
def check_code_floor(spec, env=None, level: int = 1, label: str = "",
                     floor_height: float = 3200.0) -> list[CodeIssue]:
    """檢查一層是否合乎法規尺寸。

    env=(x0,y0,x1,y1) 建築外框;**給 None 就由 spec 自己推**(plan_check.building_env)
    ——深基地會封頂建築、留院子,拿「基地−退縮」當外框會把窗判成不在外牆上。"""
    from src.design.layout.plan_check import building_env
    env = building_env(spec) if env is None else env
    lb = label or getattr(spec, "floor_label", "") or f"{level}F"
    issues: list[CodeIssue] = []

    # ① §33 樓梯:級高 ≤20cm、級深 ≥21cm、梯段淨寬 ≥75cm、平臺深 ≥梯段寬
    for st in getattr(spec, "stairs", None) or []:
        dims = _stair_dims(st, floor_height)
        if dims is None:
            continue
        riser, tread, fw, landing = dims
        if riser > MAX_RISER + 1e-6:
            issues.append(CodeIssue(
                "violation", "stair_riser", "施工編§33", lb, "樓梯",
                f"級高 {riser:.0f}mm 超過 {MAX_RISER:.0f}mm(層高 "
                f"{floor_height:.0f} ÷ 級數)"))
        if tread < MIN_TREAD - 1e-6:
            issues.append(CodeIssue(
                "violation", "stair_tread", "施工編§33", lb, "樓梯",
                f"級深 {tread:.0f}mm 不足 {MIN_TREAD:.0f}mm"))
        if fw < MIN_FLIGHT_W - 1e-6:
            issues.append(CodeIssue(
                "violation", "stair_width", "施工編§33", lb, "樓梯",
                f"梯段淨寬 {fw:.0f}mm 不足 {MIN_FLIGHT_W:.0f}mm"))
        if landing is not None and landing < fw - 1e-6:
            issues.append(CodeIssue(
                "violation", "stair_landing", "施工編§33", lb, "樓梯",
                f"平臺深 {landing:.0f}mm 小於梯段寬 {fw:.0f}mm"))

    # ② §40/§43 居室採光與通風(窗高用 1.2m 估算)
    patios = daylight_patios(spec)          # §41:開向天井的窗一樣算採光
    for room in spec.rooms:
        if room.kind not in HABITABLE_KINDS:
            continue
        poly = Polygon(room.points)
        win_w = sum(op.width for w, op in _openings_on_room(spec, poly, "window")
                    if _faces_daylight(w, env, patios))
        # §40 講的是「採光用窗**或開口**」:通往陽台的落地玻璃門就是實務上最主要的
        # 採光開口(客廳落地窗)。不算的話,4m 面寬的套房北牆要同時塞 1.9m 的窗與
        # 0.9m 的門,只剩 10cm 的牆垛 —— 那不是設計問題,是判準漏了一項。
        # ⚠️ §41 對「開口外側有陽台」另有採光面積折減,本專案**未實作**;陽台進深
        #    一律壓在 2m 以內(見 layout/balcony.py)以避開深陽台的折減爭議。
        from src.design.layout.balcony import door_opens_to_balcony
        win_w += sum(op.width for w, op in _openings_on_room(spec, poly, "door")
                     if door_opens_to_balcony(spec, w, op))
        area = poly.area / 1e6                              # ㎡
        win_area = win_w * WINDOW_H_ASSUMED / 1e6
        if win_area < area * DAYLIGHT_RATIO - 1e-9:
            issues.append(CodeIssue(
                "violation", "daylight_area", "施工編§40", lb, room.name,
                f"採光開口 {win_area:.2f}㎡ < 樓地板 {area:.1f}㎡ 的 1/8"
                f"({area * DAYLIGHT_RATIO:.2f}㎡);窗高以 1.2m 估"))
        elif win_area < area * VENT_RATIO - 1e-9:
            issues.append(CodeIssue(
                "violation", "vent_area", "施工編§43", lb, room.name,
                f"有效通風 {win_area:.2f}㎡ < 樓地板的 1/20"))

    # ③ 設計慣例(非法規):走道淨寬、門寬、臥室尺寸
    for room in spec.rooms:
        poly = Polygon(room.points)
        x0, y0, x1, y1 = poly.bounds
        short = min(x1 - x0, y1 - y0)
        if room.kind == "corridor" and short < MIN_CORRIDOR_W:
            issues.append(CodeIssue(
                "warning", "corridor_width", "", lb, room.name,
                f"走道寬 {short:.0f}mm 小於 {MIN_CORRIDOR_W:.0f}mm"))
        if room.kind in ("bedroom", "master_bedroom", "elder_room"):
            if poly.area / 1e6 < MIN_BEDROOM_AREA:
                issues.append(CodeIssue(
                    "warning", "bedroom_area", "", lb, room.name,
                    f"臥室 {poly.area / 1e6:.1f}㎡ 小於 {MIN_BEDROOM_AREA}㎡"))
            elif short < MIN_BEDROOM_SIDE:
                issues.append(CodeIssue(
                    "warning", "bedroom_side", "", lb, room.name,
                    f"臥室最短邊 {short:.0f}mm 小於 {MIN_BEDROOM_SIDE:.0f}mm"))

    for w in spec.walls:
        for op in w.openings:
            if op.kind != "door":
                continue
            outside = _is_exterior(w, env)
            need = MIN_ENTRY_DOOR_W if outside else MIN_ROOM_DOOR_W
            if op.width < need - 1e-6:
                px, py = w.point_at(op.position)
                issues.append(CodeIssue(
                    "warning", "door_width", "", lb, "",
                    f"{'大門' if outside else '房門'}({px:.0f},{py:.0f})"
                    f"淨寬 {op.width:.0f}mm 小於 {need:.0f}mm"))
    return issues


def check_code_building(floors, env=None,
                        floor_height: float = 3200.0) -> CodeCheckReport:
    """檢查整棟。floors = [(label, spec, ...)](多的欄位忽略)。"""
    issues: list[CodeIssue] = []
    for item in floors:
        label, spec = item[0], item[1]
        level = int(label[:-1]) if label[:-1].isdigit() else 1
        issues.extend(check_code_floor(spec, env, level, label, floor_height))
    return CodeCheckReport(issues)
