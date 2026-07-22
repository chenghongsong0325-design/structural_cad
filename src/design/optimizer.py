"""Layout Optimizer(v0.7 Phase 5-6)—— 單步、可驗證的格局微調。

⚠️ **不接進生成流程**:本模組**會**改動 spec(前幾層都是唯讀,這層不是),
所以它是**呼叫端主動叫用**的工具,不掛在 generate_* 上。生成流程不呼叫它 →
benchmark 輸出逐字不變 → Regression = 0。這是刻意的架構決定。

限制(依 Phase 5-6 需求):
  * **不重新 Generate** —— 只在既有 spec 上微調,不重跑任何 generator。
  * **只動三樣**:Room Position(推移共用分界)、Room Rotation、Door Position。
  * **Single Step** —— 一次呼叫最多套用**一個**改動;要多步請自行重複呼叫。
  * **不影響 Collision Engine** —— 不碰家具、不呼叫 resolver;候選若讓碰撞
    數變多會被直接否決。

流程(propose → verify → accept / revert):

    1. 先評估現況(constraint errors + LayoutScore)。
    2. 列出候選微調(每個候選都是「一個」小改動)。
    3. 每個候選都套在 **deepcopy** 上試算,原始 spec 在此階段完全不動。
    4. 安全閘門:validate_spec 全過、格局健檢過、連通過、碰撞數不增加。
    5. 改善判準:constraint error 變少;或 error 持平且分數提高。
    6. 取最佳的**一個**寫回原 spec;沒有任何候選過關就完全不動。

因為「先試算再寫回」,失敗的候選永遠不會污染原始 spec。
"""
from __future__ import annotations

import copy
import math
from dataclasses import dataclass, field

from src.design.collision.detector import find_collisions
from src.design.collision.geometry import collect_active
from src.design.connectivity import analyze_connectivity
from src.design.constraints import check_constraints
from src.design.layout_validation import validate_layout
from src.design.report import JsonReport
from src.design.scoring import ScoreWeights, score_layout

# 微調步長(mm)。刻意小——這是「微調」不是重排。
STEP_MM = 100.0
# 門洞距離牆端的最小淨距(mm),避免門被推到牆角。
DOOR_EDGE_MARGIN = 200.0
# 分數要進步超過這個量才算改善(避免浮點雜訊造成無意義的抖動)。
SCORE_EPS = 1e-6
# 共用分界推移的最大幅度(mm),超過就不算「微調」。
MAX_SHIFT_MM = 300.0
# 房間推移後的最小邊長(mm),防止把房間壓扁。
MIN_ROOM_SIDE = 1500.0


@dataclass
class OptimizeStep(JsonReport):
    """一次單步最佳化的結果。"""

    applied: bool = False
    strategy: str = ""
    target: str = ""
    detail: str = ""
    before_score: float = 0.0
    after_score: float = 0.0
    before_errors: int = 0
    after_errors: int = 0
    candidates: int = 0
    accepted: int = 0

    @property
    def improvement(self) -> float:
        return self.after_score - self.before_score

    def to_dict(self) -> dict:
        return {
            "applied": self.applied,
            "strategy": self.strategy,
            "target": self.target,
            "detail": self.detail,
            "before_score": round(self.before_score, 1),
            "after_score": round(self.after_score, 1),
            "improvement": round(self.improvement, 1),
            "before_errors": self.before_errors,
            "after_errors": self.after_errors,
            "candidates": self.candidates,
            "accepted": self.accepted,
        }

    def summary(self) -> str:
        if not self.applied:
            return (f"OptimizeStep:無可套用的改善"
                    f"(試了 {self.candidates} 個候選,分數 "
                    f"{self.before_score:.1f})")
        return (f"OptimizeStep:{self.strategy} → {self.target}\n"
                f"  {self.detail}\n"
                f"  分數 {self.before_score:.1f} → {self.after_score:.1f}"
                f"(+{self.improvement:.1f})· "
                f"error {self.before_errors} → {self.after_errors}"
                f"(候選 {self.candidates},可行 {self.accepted})")


# ---------------------------------------------------------------------------
# 評估
# ---------------------------------------------------------------------------
@dataclass
class _Eval:
    errors: int
    score: float
    collisions: int
    min_side: float = 0.0


def _min_room_side(spec) -> float:
    """所有房間中最窄的那一邊(mm)。"""
    best = math.inf
    for room in spec.rooms:
        xs = [p[0] for p in room.points]
        ys = [p[1] for p in room.points]
        best = min(best, max(xs) - min(xs), max(ys) - min(ys))
    return 0.0 if best is math.inf else best


def _evaluate(spec, weights) -> _Eval:
    return _Eval(
        errors=len(check_constraints(spec).errors),
        score=score_layout(spec, weights).total,
        collisions=len(find_collisions(collect_active(spec))),
        min_side=_min_room_side(spec))


def _is_safe(spec, base: _Eval) -> bool:
    """安全閘門:改完仍必須是一張合法、連通、碰撞不增加的圖。"""
    from src.design.layout_generator import validate_spec
    try:
        if validate_spec(spec):
            return False
        if len(find_collisions(collect_active(spec))) > base.collisions:
            return False                              # 不得讓碰撞變多
        # 不得把房間壓得比「原本最窄的房間」還窄——用相對門檻而非絕對值,
        # 否則原圖若已有窄房(如小衛浴),連無關的移門候選都會被一併否決。
        floor = min(MIN_ROOM_SIDE, base.min_side)
        if _min_room_side(spec) < floor - 1e-6:
            return False
        if not validate_layout(spec).ok:
            return False
        return analyze_connectivity(spec).ok
    except Exception:                                 # noqa: BLE001
        return False                                  # 幾何壞掉一律否決


def _is_better(new: _Eval, base: _Eval) -> bool:
    """改善判準:先看 constraint error,再看分數。"""
    if new.errors != base.errors:
        return new.errors < base.errors
    return new.score > base.score + SCORE_EPS


# ---------------------------------------------------------------------------
# 候選:三種微調
# ---------------------------------------------------------------------------
@dataclass
class _Candidate:
    strategy: str
    target: str
    detail: str
    apply: object                                     # Callable[[spec], None]


def _rect(points):
    """房間若是軸對齊矩形,回 (x0, y0, x1, y1),否則 None。"""
    if len(points) != 4:
        return None
    xs = sorted({round(p[0], 6) for p in points})
    ys = sorted({round(p[1], 6) for p in points})
    if len(xs) != 2 or len(ys) != 2:
        return None
    return (xs[0], ys[0], xs[1], ys[1])


def _door_candidates(spec) -> list:
    """Door Position:把門沿著自己那道牆前後推 STEP_MM。"""
    out = []
    for di, dp in enumerate(spec.doors):
        wall = spec.walls[dp.wall_index]
        op = wall.openings[dp.opening_index]
        length = wall.length
        lo = op.width / 2 + DOOR_EDGE_MARGIN
        hi = length - op.width / 2 - DOOR_EDGE_MARGIN
        for sign in (+1, -1):
            newpos = op.position + sign * STEP_MM
            if not (lo <= newpos <= hi):
                continue

            def _apply(s, wi=dp.wall_index, oi=dp.opening_index, p=newpos):
                s.walls[wi].openings[oi].position = p

            out.append(_Candidate(
                "door_position", f"門 #{di}(牆 {dp.wall_index})",
                f"沿牆移動 {sign * STEP_MM:+.0f}mm"
                f"({op.position:.0f} → {newpos:.0f})",
                _apply))
    return out


def _shared_boundary_candidates(spec) -> list:
    """Room Position:推移一整條「內部分界線」(同時調整兩側房間)。

    做法是列舉房間頂點用到的內部座標線,把落在該線上的**所有**房間頂點與牆
    端點一起平移——一邊變大、一邊變小,房間仍完美鋪滿建築,不會破壞
    validate_spec 的「面積合計 = 建築面積」,T 形接頭也會跟著走。

    ⚠️ 用座標線而非「兩間矩形房」來列舉,是因為本引擎有不少 L 形房間
    (走道、客餐廳),用矩形配對會把它們整批漏掉。"""
    xs: set = set()
    ys: set = set()
    for room in spec.rooms:
        for x, y in room.points:
            xs.add(round(x, 6))
            ys.add(round(y, 6))
    bx0, by0 = spec.grid_origin
    bx1, by1 = bx0 + sum(spec.x_spacings), by0 + sum(spec.y_spacings)

    out = []
    for axis, coords, lo, hi in ((0, sorted(xs), bx0, bx1),
                                 (1, sorted(ys), by0, by1)):
        for c in coords:
            if abs(c - lo) < 1.0 or abs(c - hi) < 1.0:
                continue                              # 建築外緣不動
            for sign in (+1, -1):
                d = sign * STEP_MM
                if abs(d) > MAX_SHIFT_MM:
                    continue

                def _apply(s, ax=axis, coord=c, delta=d):
                    _shift_boundary(s, ax, coord, delta)

                out.append(_Candidate(
                    "room_position",
                    f"{'垂直' if axis == 0 else '水平'}分界 @{c:.0f}",
                    f"整條分界推移 {d:+.0f}mm", _apply))
    return out


def _shift_boundary(spec, axis: int, coord: float, delta: float) -> None:
    """把座落在 coord 的整條分界(房間頂點 + 牆端點)沿 axis 推移 delta。"""
    def moved(pt):
        vals = list(pt)
        if abs(vals[axis] - coord) < 1e-6:
            vals[axis] += delta
        return tuple(vals)

    for room in spec.rooms:
        room.points = [moved(p) for p in room.points]
    for wall in spec.walls:
        wall.start = moved(wall.start)
        wall.end = moved(wall.end)


def _rotation_candidates(spec) -> list:
    """Room Rotation:把房間繞形心轉 90°。

    ⚠️ 本引擎的平面是「軸對齊且完全鋪滿」的,單獨旋轉一間房必然破壞鋪滿,
    因此在目前的格局上這類候選**一定會被安全閘門否決**(實測 0 次通過)。
    保留它是為了日後非鋪滿/自由平面;它不會造成任何改動,只會被試算後丟棄。"""
    out = []
    for idx, room in enumerate(spec.rooms):
        rect = _rect(room.points)
        if rect is None:
            continue
        x0, y0, x1, y1 = rect
        if abs((x1 - x0) - (y1 - y0)) < 1e-6:         # 正方形轉了等於沒轉
            continue

        def _apply(s, k=idx):
            pts = s.rooms[k].points
            cx = sum(p[0] for p in pts) / len(pts)
            cy = sum(p[1] for p in pts) / len(pts)
            s.rooms[k].points = [
                (cx + (p[1] - cy), cy - (p[0] - cx)) for p in pts]

        out.append(_Candidate(
            "room_rotation", spec.rooms[idx].name, "繞形心旋轉 90°", _apply))
    return out


def candidates(spec) -> list:
    """所有單步候選(每個都只改一樣東西)。"""
    return (_door_candidates(spec) + _shared_boundary_candidates(spec)
            + _rotation_candidates(spec))


# ---------------------------------------------------------------------------
# 單步最佳化
# ---------------------------------------------------------------------------
def optimize_step(spec, weights: ScoreWeights | None = None) -> OptimizeStep:
    """套用**至多一個**微調。沒有候選能改善就完全不動 spec。

    要多步請自行重複呼叫(需求明訂不得一次大量修改)。"""
    w = weights or ScoreWeights()
    base = _evaluate(spec, w)
    cands = candidates(spec)

    best = None
    accepted = 0
    for cand in cands:
        trial = copy.deepcopy(spec)                   # 原 spec 全程不動
        try:
            cand.apply(trial)
        except Exception:                             # noqa: BLE001
            continue
        if not _is_safe(trial, base):
            continue
        ev = _evaluate(trial, w)
        if not _is_better(ev, base):
            continue
        accepted += 1
        if best is None or _is_better(ev, best[2]):
            best = (cand, trial, ev)

    step = OptimizeStep(
        before_score=base.score, after_score=base.score,
        before_errors=base.errors, after_errors=base.errors,
        candidates=len(cands), accepted=accepted)
    if best is None:
        return step

    cand, trial, ev = best
    spec.rooms, spec.walls, spec.doors = trial.rooms, trial.walls, trial.doors
    step.applied = True
    step.strategy, step.target, step.detail = cand.strategy, cand.target, cand.detail
    step.after_score, step.after_errors = ev.score, ev.errors
    return step
