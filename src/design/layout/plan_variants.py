"""設計變體 → 多方案(讓同一組需求生出**真的不一樣**的幾張圖)。

問題:規則產生器同一個尺寸永遠輸出同一張圖;搜尋式產生器雖然有亂數,但評分挑
「最高分」那張,結果每次都長得差不多——使用者看到的是「大同小異」。

做法(不是亂加亂數):

  1. **變體** = 同一套規則裡「設計師本來就可以選」的決定(核在左還在右、浴廁在
     南還在北、大門偏西還偏東…),不是把牆隨機亂移。
  2. 每個變體都要通過**兩道關卡**:`plan_check`(圖面正確)+ `code_check`(法規尺寸)。
     不合格的直接淘汰——自由度放大不能犧牲正確性。
  3. 從合格的變體裡挑「**彼此差最多**」的 N 個(不是分數最高的 N 個——那幾張通常
     長得一樣)。差異用「平面取樣格」比對:把樓地板切成 8×8 格,看每格落在哪種
     房間,兩張圖不同格數越多就越不像。

典型用法::

    rep = narrow_options(7000, 12000, floors=3, n=3)
    for opt in rep.options:
        print(opt.variant, opt.plan.ok, opt.code.ok)
        draw_floor_plan(msp, opt.floors[0][1], layers)
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from shapely.geometry import Point, Polygon

from src.design.layout.code_check import CodeCheckReport, check_code_building
from src.design.layout.narrow_house import (
    all_variants,
    generate_narrow_building,
)
from src.design.layout.plan_check import PlanCheckReport, check_building

GRID_N = 8          # 差異比對的取樣格數(8×8=64 點)


def _signature(spec, env, n: int = GRID_N) -> tuple:
    """一層的「平面指紋」:n×n 取樣格各落在哪種房間。"""
    polys = [(r.kind, Polygon(r.points)) for r in spec.rooms]
    out = []
    for j in range(n):
        for i in range(n):
            x = env[0] + (i + 0.5) * (env[2] - env[0]) / n
            y = env[1] + (j + 0.5) * (env[3] - env[1]) / n
            p = Point(x, y)
            out.append(next((k for k, poly in polys if poly.contains(p)), ""))
    return tuple(out)


def _distance(a: tuple, b: tuple) -> float:
    """兩張圖有多不一樣(0=完全相同,1=每一格都不同)。"""
    if not a or not b:
        return 0.0
    return sum(1 for x, y in zip(a, b) if x != y) / len(a)


@dataclass
class PlanOption:
    """一個合格方案(變體 + 各層圖 + 兩份檢查報表)。"""

    variant: object
    floors: list
    plan: PlanCheckReport
    code: CodeCheckReport
    signature: tuple = ()

    @property
    def n_issues(self) -> int:
        """設計面待改項數(硬錯誤已在篩選階段淘汰,這裡看警告)。"""
        return len(self.plan.warnings) + len(self.code.warnings)

    def to_dict(self) -> dict:
        v = self.variant
        return {
            "variant": {"mirror": v.mirror, "bath_north": v.bath_north,
                        "open_kitchen": v.open_kitchen,
                        "entry_frac": v.entry_frac},
            "labels": [lb for lb, _sp in self.floors],
            "n_issues": self.n_issues,
            "plan_check": self.plan.to_dict(),
            "code_check": self.code.to_dict(),
        }

    def to_json(self, **kw) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, **kw)


@dataclass
class VariantReport:
    """多方案結果:挑出來的 options + 這次試了幾個、過了幾個。"""

    options: list = field(default_factory=list)
    n_tried: int = 0
    n_passed: int = 0

    @property
    def diversity(self) -> float:
        """挑出來這幾個方案彼此的平均差異(0~1;越大越不像)。"""
        sigs = [o.signature for o in self.options]
        pairs = [(a, b) for i, a in enumerate(sigs) for b in sigs[i + 1:]]
        return sum(_distance(a, b) for a, b in pairs) / len(pairs) if pairs else 0.0

    def to_dict(self) -> dict:
        return {
            "n_tried": self.n_tried,
            "n_passed": self.n_passed,
            "diversity": round(self.diversity, 3),
            "options": [o.to_dict() for o in self.options],
        }

    def to_json(self, **kw) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, **kw)

    def summary(self) -> str:
        head = (f"試 {self.n_tried} 種變體、{self.n_passed} 種合格,"
                f"挑出 {len(self.options)} 個方案(平均差異 {self.diversity:.0%})")
        body = [f"  方案{i + 1}:鏡射{'✓' if o.variant.mirror else '✗'} "
                f"浴廁{'北' if o.variant.bath_north else '南'} "
                f"後段{'開放餐廚' if o.variant.open_kitchen else '餐廳|廚房'} "
                f"大門{o.variant.entry_frac:.0%} → 待改 {o.n_issues} 項"
                for i, o in enumerate(self.options)]
        return "\n".join([head, *body])


def _pick_diverse(cands: list, n: int) -> list:
    """從合格方案裡挑 n 個**彼此差最多**的(最遠點取樣)。

    先放「待改項數最少」那個當起點,之後每次加入「離已選集合最遠」的那個——
    挑分數前 n 名會拿到三張幾乎一樣的圖,這裡要的是看得出差別的選擇。"""
    if len(cands) <= n:
        return cands
    chosen = [min(cands, key=lambda o: o.n_issues)]
    rest = [o for o in cands if o is not chosen[0]]
    while len(chosen) < n and rest:
        far = max(rest, key=lambda o: min(_distance(o.signature, c.signature)
                                          for c in chosen))
        chosen.append(far)
        rest.remove(far)
    return chosen


def narrow_options(building_w_mm: float, building_d_mm: float, *,
                   floors: int = 3, n: int = 3, furnish: bool = True,
                   setback: float = 2000.0) -> VariantReport:
    """窄面寬透天 → n 個彼此差最多、且**兩道關卡都過**的方案。

    關卡:plan_check 無硬錯誤(圖面正確)+ code_check 無違規(法規尺寸)。
    全部變體都不合格時,退而回傳「錯誤最少」的那一個(韌性:寧可出圖並回報問題,
    也不要什麼都生不出來)。"""
    W, D = float(building_w_mm), float(building_d_mm)
    env = (setback, setback, setback + W, setback + D)
    passed, fallback = [], None
    variants = all_variants()
    for v in variants:
        try:
            fl = generate_narrow_building(W, D, floors=floors, furnish=furnish,
                                          variant=v)
        except ValueError:
            continue
        plan = check_building(fl)           # 外框由 spec 自推(深基地會封頂留院子)
        code = check_code_building(fl)
        opt = PlanOption(v, fl, plan, code, _signature(fl[0][1], env))
        if plan.ok and code.ok:
            passed.append(opt)
        elif fallback is None or (len(opt.plan.errors) + len(opt.code.violations)
                                  < len(fallback.plan.errors)
                                  + len(fallback.code.violations)):
            fallback = opt
    if not passed:
        return VariantReport([fallback] if fallback else [], len(variants), 0)
    return VariantReport(_pick_diverse(passed, n), len(variants), len(passed))
