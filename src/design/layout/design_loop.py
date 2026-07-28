"""雙向收斂迴圈(混合式 AI 建築師第 3 步:設計↔評分 來回迭代)。

前兩步是**單向管線**:Gemini 設計拓撲(room_graph)→ 搜尋落實成真圖(graph_layout)。
問題:LLM 看不到自己設計落實後的樣子,一次定生死(客廳太大、某房變內間都要等落實
才知道)。這一步把它接成**迴圈**——真實 AI 建築師就是「畫→檢討→改」來回:

    Gemini 設計 → 落實 → 挑毛病(critique)→ 把毛病餵回 Gemini 重設計 → 再落實評分
    → 留 fitness 最高的那版,迭代數次

fitness = 各層 Global Score 平均 − 2×問題數;每次落實用**固定 rng** → 分數變化來自
「設計改良」而非切法運氣。critique 是純幾何/評分檢查(不呼叫網路):相鄰沒排進去、
房間過大、habitable 房變內間沒採光、動線不通。

⚠️ critique 用引擎已認得的量(AREA_BAND、N/S 採光面、天井旁、room_circulation);
   採光判定:前後外牆或天井旁才算有光(東西是界牆不開窗)。
"""
from __future__ import annotations

import random
import sys
from pathlib import Path
from typing import Optional

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from src.design.layout.graph_layout import realize_graph_building

SETBACK = 2000.0
# 需要對外採光的「居室」(內間沒光要挑出來回饋;浴廁/儲藏/走道/樓梯不算)。
DAYLIGHT_KINDS = {"living", "bedroom", "master_bedroom", "study", "dining",
                  "elder_room"}
OVERSIZE = 1.5          # 面積超過理想上限這麼多倍才算「太大」(只挑誇張的)
# 只挑「設計端改得動」的過大房——居室/廚房(可拆分或增房)。走道/儲藏/陽台等
# 服務房過大是落實時餘量堆積、改拓撲救不了,不回饋(免得浪費一輪迭代)。
OVERSIZE_KINDS = DAYLIGHT_KINDS | {"kitchen"}
FITNESS_PROBLEM_COST = 2.0


def _rect(room):
    xs = [p[0] for p in room.points]
    ys = [p[1] for p in room.points]
    return (min(xs), min(ys), max(xs), max(ys))


def _touches_ns(rect, env) -> bool:
    """貼南面或北面外牆(前後採光面;東西是界牆不開窗)。"""
    return abs(rect[1] - env[1]) < 1.0 or abs(rect[3] - env[3]) < 1.0


# plan_check 的問題代碼 → 給 LLM 的具體修法建議(critique 要「可行動」才有用)。
_FIX_HINT = {
    "room_no_daylight": "減少該層房間數或換位置(靠前後外牆或天井)",
    "room_oversize": "拆成兩間或該層增加房間",
    "room_skinny": "該層房間數減少,或把長邊拆給相鄰房間",
}


def critique_building(floors, env) -> list[str]:
    """挑落實後的毛病 → 給 LLM 的可行動問題清單(純幾何/評分檢查,不呼叫網路)。

    ⚠️ 判準一律走 plan_check(單一來源):硬錯誤(沒門/斷開/穿牆/動線)照理已被
    產線關卡擋掉,萬一漏網也一併回報;設計面警告(內間沒光/過大/細長)加上具體修法。
    另外補一項 plan_check 看不到的:LLM 要求的相鄰沒排進去(那是關係圖 vs 落實的落差)。
    """
    from src.design.layout.plan_check import check_building

    problems: list[str] = []
    for label, spec, sat, tot in floors:
        if sat < tot:                                    # 要求的相鄰沒排進去
            problems.append(
                f"{label}:{tot - sat} 組要求的相鄰沒排進去"
                f"(房間太多擠不下,考慮減少或合併)")

    rep = check_building([(lb, sp) for lb, sp, _s, _t in floors], env)
    for issue in rep.issues:
        if issue.code == "room_oversize" and issue.room:
            kind_ok = True                               # 只挑設計端救得動的居室
            for lb, spec, _s, _t in floors:
                if lb != issue.floor:
                    continue
                r = next((x for x in spec.rooms if x.name == issue.room), None)
                kind_ok = r is not None and r.kind in OVERSIZE_KINDS
            if not kind_ok:
                continue                                 # 走道/儲藏過大是切法餘量,改設計救不動
        who = f"{issue.floor}:{issue.room}" if issue.room else f"{issue.floor}"
        hint = _FIX_HINT.get(issue.code, "")
        problems.append(f"{who} {issue.detail}" + (f",{hint}" if hint else ""))
    return problems


def design_building(brief: str, building_w: float, building_d: float, *,
                    iterations: int = 3, client: Optional[object] = None,
                    floor_area_m2: Optional[float] = None, verbose: bool = True):
    """雙向收斂:設計→落實→挑毛病→重設計,留 fitness 最高的那版。

    回 (best, history)。best = {iter, fitness, mean_score, graph, floors, problems}。
    每次落實用固定 rng → 分數變化來自設計改良,不是切法運氣。
    """
    from src.design.layout.global_score import score_report
    from src.design.layout.room_graph import propose_room_graph, refine_room_graph

    if floor_area_m2 is None:
        floor_area_m2 = building_w * building_d / 1e6
    env = (SETBACK, SETBACK, SETBACK + building_w, SETBACK + building_d)

    graph = propose_room_graph(brief, client=client, floor_area_m2=floor_area_m2)
    best = None
    history: list[dict] = []
    for it in range(iterations):
        try:
            floors = realize_graph_building(graph, building_w, building_d,
                                            rng=random.Random(7))
        except Exception:                                # 這版落實失敗
            if best is not None:
                break                                    # 已有較早的最佳 → 用它
            raise                                        # 第一版就失敗,沒得回
        scores = [score_report(sp)["overall_score"] for _, sp, _, _ in floors]
        mean_score = sum(scores) / len(scores)
        problems = critique_building(floors, env)
        fitness = mean_score - FITNESS_PROBLEM_COST * len(problems)
        history.append({"iter": it, "mean_score": mean_score,
                        "n_problems": len(problems), "fitness": fitness})
        if best is None or fitness > best["fitness"]:
            best = {"iter": it, "fitness": fitness, "mean_score": mean_score,
                    "graph": graph, "floors": floors, "problems": problems}
        if verbose:
            print(f"  迭代 {it}: 平均分 {mean_score:.0f}  問題 {len(problems)} 個  "
                  f"fitness {fitness:.0f}")
            for p in problems:
                print(f"       · {p}")
        if not problems or it == iterations - 1:         # 沒毛病了就收工
            break
        try:
            graph = refine_room_graph(graph, problems, client=client,
                                      floor_area_m2=floor_area_m2)
        except Exception:                                # 重設計失敗(額度/網路)
            break                                        # → 用目前最佳,不讓整棟白做
    return best, history


def main(argv: Optional[list[str]] = None) -> None:
    import os

    from src.drafting.apartment_plan import draw_floor_plan
    from src.standards.loader import apply_standard, load_standard, new_document

    args = argv if argv is not None else sys.argv[1:]
    if not (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")):
        print("需要設定 GEMINI_API_KEY 環境變數")
        raise SystemExit(1)

    brief = " ".join(args) if args else "透天三層,建築物7×12米,三房"
    W, D = 7000.0, 12000.0
    print(f"需求:「{brief}」  建築 {W/1000:g}×{D/1000:g}m")
    print("雙向收斂迴圈(Gemini 設計 → 落實評分 → 挑毛病回饋 → 重設計)…\n")

    best, history = design_building(brief, W, D, iterations=3)

    traj = " → ".join(f"[{h['iter']}]分{h['mean_score']:.0f}/題{h['n_problems']}"
                      for h in history)
    print(f"\n軌跡:{traj}")
    print(f"最佳:迭代 {best['iter']}  平均分 {best['mean_score']:.0f}  "
          f"剩 {len(best['problems'])} 個問題")

    outdir = _PROJECT_ROOT / "output"
    outdir.mkdir(exist_ok=True)
    for label, spec, _sat, _tot in best["floors"]:
        doc = new_document()
        layers = apply_standard(doc, load_standard())
        draw_floor_plan(doc.modelspace(), spec, layers)
        doc.saveas(outdir / f"loop_{label}.dxf")
    print("最佳方案已出圖:output/loop_1F.dxf / loop_2F.dxf / loop_3F.dxf")


if __name__ == "__main__":
    main()
