"""Constraint Engine(v0.7 Phase 5-5)—— 把「設計常規」寫成可檢查的規則。

⚠️ **唯讀**:只檢查,不改 spec、不 Auto Fix、不碰 Generator。

與其他層的分工:
  * `validate_spec`      —— 生成流程的硬性守門(法規/採光/家具),會擋圖。
  * `layout_validation`  —— 格局拓樸健檢(封閉/重疊/連通)。
  * `scoring`            —— 品質量化成分數。
  * **本模組**            —— 「這樣配不合常規」的具名規則,逐條回報違反了哪一條。

規則以登錄表(RULES)管理,每條規則是一個 `ConstraintRule`;新增規則 =
加一個函式 + 登錄一筆,引擎本身不動。

嚴重度:
  * error —— 明確的機能錯誤(臥室直通廚房、衛浴直開餐廳)。
  * warn  —— 設計品質提醒,不一定是錯(臥室貼公共空間、廚房遠離餐廳)。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from shapely.geometry import LineString
from shapely.geometry import Polygon

from src.design.connectivity import build_graphs, room_polys, wall_cover
from src.design.report import JsonReport

# 訪客會進入的公共空間(與 scoring 同一套語意:家庭廳/書房屬半私密,不算)。
PUBLIC_KINDS = {"living", "dining", "kitchen", "foyer"}
# 臥室「貼著」時要提醒的公共房型(噪音/氣味/管線)。
# ⚠️ 刻意**不含 living**:實測 100 層,臥室與客廳共牆 42 次、與廚房共牆 12 次。
# 客廳是臥室的天然鄰居(小宅尤其無可避免),把它算成違反會讓這條規則觸發率
# 衝到 88% 而失去意義;廚房才是真正該避開的(油煙/水路/噪音)。
NOISY_PUBLIC_KINDS = {"kitchen", "dining"}

SEVERITY_ERROR = "error"
SEVERITY_WARN = "warn"


@dataclass
class ConstraintViolation(JsonReport):
    """一筆違反紀錄。"""

    rule: str
    severity: str
    message: str
    rooms: list = field(default_factory=list)

    def __str__(self) -> str:
        return f"[{self.severity}] {self.rule}:{self.message}"

    def to_dict(self) -> dict:
        return {"rule": self.rule, "severity": self.severity,
                "message": self.message, "rooms": list(self.rooms)}


@dataclass
class ConstraintReport(JsonReport):
    """一次規則檢查的結果(唯讀產物)。"""

    violations: list = field(default_factory=list)
    checked: list = field(default_factory=list)      # 實際跑過的 rule id
    skipped: list = field(default_factory=list)      # 條件不成立而略過的規則

    @property
    def errors(self) -> list:
        return [v for v in self.violations if v.severity == SEVERITY_ERROR]

    @property
    def warnings(self) -> list:
        return [v for v in self.violations if v.severity == SEVERITY_WARN]

    @property
    def ok(self) -> bool:
        """沒有 error 就算通過(warn 只是提醒)。"""
        return not self.errors

    def by_rule(self, rule: str) -> list:
        return [v for v in self.violations if v.rule == rule]

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "error_count": len(self.errors),
            "warning_count": len(self.warnings),
            "checked": list(self.checked),
            "skipped": list(self.skipped),
            "violations": [v.to_dict() for v in self.violations],
        }

    def summary(self) -> str:
        head = (f"ConstraintReport:檢查 {len(self.checked)} 條規則 → "
                f"{'PASS' if self.ok else 'FAIL'}"
                f"(error {len(self.errors)} · warn {len(self.warnings)})")
        return "\n".join([head] + [f"  {v}" for v in self.violations])


# ---------------------------------------------------------------------------
# 規則執行的共用脈絡(算一次,給所有規則共用)
# ---------------------------------------------------------------------------
@dataclass
class ConstraintContext:
    spec: object
    graphs: object
    polys: list
    cover: object = None

    @classmethod
    def build(cls, spec) -> "ConstraintContext":
        return cls(spec=spec, graphs=build_graphs(spec),
                   polys=room_polys(spec), cover=wall_cover(spec))

    def indices(self, *kinds) -> list:
        want = set(kinds)
        return [i for i, k in enumerate(self.graphs.kinds) if k in want]

    def name(self, i: int) -> str:
        return self.graphs.names[i]

    def connected(self, i: int, j: int) -> bool:
        """兩房是否有門或開口直接相通(Room Graph 上的邊)。"""
        return j in self.graphs.room_graph[i]

    def adjacent(self, i: int, j: int) -> bool:
        """兩房是否實體相鄰(共用夠長的牆),不管通不通。"""
        return j in self.graphs.adjacency[i]


# ---------------------------------------------------------------------------
# 規則
# ---------------------------------------------------------------------------
def _pair_rule(ctx, kind_a, kind_b, rule, severity, phrase) -> list:
    """通則:kind_a 的房間不得與 kind_b 的房間「直接相通」。"""
    out = []
    for i in ctx.indices(kind_a):
        for j in ctx.indices(kind_b):
            if ctx.connected(i, j):
                out.append(ConstraintViolation(
                    rule=rule, severity=severity,
                    message=f"{ctx.name(i)} {phrase} {ctx.name(j)}",
                    rooms=[ctx.name(i), ctx.name(j)]))
    return out


def bedroom_not_facing_kitchen(ctx) -> list:
    """臥室不可直接面廚房(油煙/噪音直入寢區)。"""
    return _pair_rule(ctx, "bedroom", "kitchen",
                      "bedroom_not_facing_kitchen", SEVERITY_ERROR, "直接通向")


def bathroom_not_facing_dining(ctx) -> list:
    """衛浴不可直接面餐廳(衛生觀感)。"""
    return _pair_rule(ctx, "bathroom", "dining",
                      "bathroom_not_facing_dining", SEVERITY_ERROR, "直接開向")


def entrance_not_facing_toilet(ctx) -> list:
    """入口不可直視馬桶(開門見廁)。

    判準:從大門洞口中心到馬桶之間拉一條直線,若**完全沒被實牆擋住**就算
    直視。門洞已從牆體扣除,故「穿過敞開的門看見」也算——這是最壞情況。
    沒有對外門的樓層(如臥室層)不適用,由引擎列入 skipped。"""
    entrance = next((d for d in ctx.graphs.doors if d.is_exterior), None)
    if entrance is None:
        return []
    toilets = [fx for fx in ctx.spec.fixtures
               if getattr(fx, "name", None) == "toilet"]
    out = []
    for fx in toilets:
        line = LineString([entrance.point, fx.insert])
        if ctx.cover is None or not line.intersects(ctx.cover):
            out.append(ConstraintViolation(
                rule="entrance_not_facing_toilet", severity=SEVERITY_ERROR,
                message=f"大門(牆 {entrance.wall_index})可直視馬桶 "
                        f"@({fx.insert[0]:.0f},{fx.insert[1]:.0f})",
                rooms=[]))
    return out


def bedroom_avoids_public_adjacency(ctx) -> list:
    """臥室避免緊鄰公共空間(共牆傳噪)。

    這是「避免」等級的提醒:小宅本來就常見臥室貼客廳(動線融入客廳),
    故列為 warn 而非 error。"""
    out = []
    for i in ctx.indices("bedroom"):
        hit = [ctx.name(j) for j in sorted(ctx.graphs.adjacency[i])
               if ctx.graphs.kinds[j] in NOISY_PUBLIC_KINDS]
        if hit:
            out.append(ConstraintViolation(
                rule="bedroom_avoids_public_adjacency", severity=SEVERITY_WARN,
                message=f"{ctx.name(i)} 與公共空間共牆({', '.join(hit)})",
                rooms=[ctx.name(i)] + hit))
    return out


def kitchen_near_dining(ctx) -> list:
    """廚房應接近餐廳(上菜動線)。

    「接近」= 直接相通**或**實體相鄰(共牆)。實測有獨立餐廳的 13 個案例中,
    3 個直接相通、5 個雖無門但緊鄰(端菜幾步就到,合理)、5 個兩者皆非(真的
    隔開)。只把最後這種列為違反,才符合「接近」而非「必須開門相通」。

    餐廚合一(沒有獨立餐廳)時本規則不適用,由引擎列入 skipped。"""
    kitchens, dinings = ctx.indices("kitchen"), ctx.indices("dining")
    if not kitchens or not dinings:
        return []
    out = []
    for k in kitchens:
        near = any(ctx.connected(k, d) or ctx.adjacent(k, d) for d in dinings)
        if not near:
            out.append(ConstraintViolation(
                rule="kitchen_near_dining", severity=SEVERITY_WARN,
                message=f"{ctx.name(k)} 與餐廳既不相通也不相鄰(上菜要繞路)",
                rooms=[ctx.name(k)] + [ctx.name(d) for d in dinings]))
    return out


@dataclass
class ConstraintRule:
    """一條可登錄的規則。`applies` 回 False 時整條略過(記進 skipped)。"""

    rule_id: str
    description: str
    severity: str
    check: Callable
    applies: Callable = None

    def is_applicable(self, ctx) -> bool:
        return self.applies is None or bool(self.applies(ctx))


RULES: list[ConstraintRule] = [
    ConstraintRule(
        "bedroom_not_facing_kitchen", "臥室不可直接面廚房", SEVERITY_ERROR,
        bedroom_not_facing_kitchen,
        lambda c: c.indices("bedroom") and c.indices("kitchen")),
    ConstraintRule(
        "bathroom_not_facing_dining", "衛浴不可直接面餐廳", SEVERITY_ERROR,
        bathroom_not_facing_dining,
        lambda c: c.indices("bathroom") and c.indices("dining")),
    ConstraintRule(
        "entrance_not_facing_toilet", "入口不可直視馬桶", SEVERITY_ERROR,
        entrance_not_facing_toilet,
        lambda c: any(d.is_exterior for d in c.graphs.doors)),
    ConstraintRule(
        "bedroom_avoids_public_adjacency", "臥室避免緊鄰公共空間",
        SEVERITY_WARN, bedroom_avoids_public_adjacency,
        lambda c: c.indices("bedroom")),
    ConstraintRule(
        "kitchen_near_dining", "廚房應接近餐廳", SEVERITY_WARN,
        kitchen_near_dining,
        lambda c: c.indices("kitchen") and c.indices("dining")),
]


def check_constraints(spec, rules: list | None = None) -> ConstraintReport:
    """跑過所有規則,回一份 ConstraintReport。**唯讀**,不改 spec。"""
    ctx = ConstraintContext.build(spec)
    report = ConstraintReport()
    for rule in (RULES if rules is None else rules):
        if not rule.is_applicable(ctx):
            report.skipped.append(rule.rule_id)
            continue
        report.checked.append(rule.rule_id)
        report.violations.extend(rule.check(ctx))
    return report
