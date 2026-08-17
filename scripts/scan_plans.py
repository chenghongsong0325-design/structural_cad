"""隨機掃描產線:亂數抽 N 組需求 → 出圖 → plan_check 把關 → 統計硬錯誤。

用途:每次動骨架(narrow_house / shallow_house / graph_layout / layout_generator)
之後,拿它掃一輪看有沒有「系統性錯誤」。單元測試守的是已知案例,這支守的是
「沒人想到的尺寸組合」。目標永遠是 **硬錯誤 0**。

用法::

    python scripts/scan_plans.py                    # 預設 80 案
    python scripts/scan_plans.py --n 200 --seed 7   # 換樣本數/亂數種子
    python scripts/scan_plans.py --width 5,9        # 只掃建築寬 5~9m(找回歸)
    python scripts/scan_plans.py --json out.json    # 存明細供比對

尺寸一律當**建築物**尺寸掃(setback=0,site=building),這樣掃描區間就直接對應
各骨架的 MIN/MAX_WIDTH,不會被退縮換算糊掉。
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import traceback
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Windows 主控台預設 cp950,印不出 ✅/❌ 會整支炸掉 → 強制 UTF-8。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from src.design.building_generator import BuildingBrief, generate_building_auto
from src.design.layout.plan_check import check_building
from src.design.layout_generator import HouseBrief


def run_case(case: dict) -> dict:
    """跑一案:出圖 + 檢查。回傳這案的結果(含錯誤代碼清單)。"""
    brief = BuildingBrief(
        typical=HouseBrief(
            site_width=case["w"], site_depth=case["d"],
            bedrooms=case["bedrooms"], setback=0, seed=case["seed"]),
        floors=case["floors"],
        # 跟 nl_parser 一致:透天只要多層就走層別分化(1F 公共層/2F+ 臥室層)。
        # 不開的話 2F 會整層複製 1F、連大門一起 → 掃出來全是假的 entry_upstairs。
        differentiated=case["floors"] > 1,
    )
    try:
        building = generate_building_auto(brief)
    except Exception as exc:                      # 生不出來也是一種結果
        return {**case, "status": "raised",
                "detail": f"{type(exc).__name__}: {exc}",
                "trace": traceback.format_exc(limit=3),
                "errors": [], "warnings": []}
    report = check_building([(f.label, f.spec) for f in building.floors])
    return {**case,
            "status": "ok" if report.ok else "failed",
            "errors": [i.code for i in report.errors],
            "warnings": [i.code for i in report.warnings],
            "detail": "" if report.ok else report.summary()}


def sample(n: int, seed: int, w_range, d_range, floors_range) -> list[dict]:
    rng = random.Random(seed)
    return [{"w": round(rng.uniform(*w_range), -2),
             "d": round(rng.uniform(*d_range), -2),
             "bedrooms": rng.randint(1, 4),
             "floors": rng.randint(1, floors_range),
             "seed": rng.randint(0, 9999)}
            for _ in range(n)]


def _mm_range(text: str, fallback) -> tuple:
    """'5,9'(公尺)→ (5000.0, 9000.0)。"""
    if not text:
        return fallback
    lo, hi = (float(x) * 1000 for x in text.split(","))
    return (lo, hi)


def main() -> int:
    ap = argparse.ArgumentParser(description="隨機掃描平面圖產線")
    ap.add_argument("--n", type=int, default=80, help="案數(預設 80)")
    ap.add_argument("--seed", type=int, default=0, help="亂數種子(可重現)")
    ap.add_argument("--width", default="", help="建築寬區間(公尺),例 5,9")
    ap.add_argument("--depth", default="", help="建築深區間(公尺),例 8,20")
    ap.add_argument("--floors", type=int, default=3, help="最大樓層數")
    ap.add_argument("--json", default="", help="把明細寫到這個檔")
    args = ap.parse_args()

    cases = sample(args.n, args.seed,
                   _mm_range(args.width, (5000.0, 30000.0)),
                   _mm_range(args.depth, (5000.0, 20000.0)),
                   args.floors)

    results, rule_hits = [], Counter()
    for k, case in enumerate(cases, 1):
        res = run_case(case)
        results.append(res)
        rule_hits.update(res["errors"])
        if res["status"] != "ok":
            rule_hits.update(["<生不出來>"] if res["status"] == "raised" else [])
        mark = {"ok": ".", "failed": "X", "raised": "E"}[res["status"]]
        print(mark, end="", flush=True)
        if k % 50 == 0:
            print(f" {k}")
    print()

    bad = [r for r in results if r["status"] != "ok"]
    n_ok = len(results) - len(bad)
    print(f"\n{'='*60}\n掃描 {len(results)} 案:✅ 合格 {n_ok}、"
          f"❌ 不合格 {sum(r['status'] == 'failed' for r in results)}、"
          f"💥 生不出來 {sum(r['status'] == 'raised' for r in results)}")
    if rule_hits:
        print("\n硬錯誤排行(規則 → 次數):")
        for rule, cnt in rule_hits.most_common(10):
            print(f"  {cnt:>4}  {rule}")
    warn_hits = Counter(w for r in results for w in r["warnings"])
    if warn_hits:
        print("\n設計警告(不擋圖,供收斂迴圈參考):")
        for rule, cnt in warn_hits.most_common(5):
            print(f"  {cnt:>4}  {rule}")

    for r in bad[:5]:
        print(f"\n--- {r['w']/1000:g}×{r['d']/1000:g}m, {r['bedrooms']}房 "
              f"{r['floors']}層 seed={r['seed']} [{r['status']}]\n{r['detail']}")
    if len(bad) > 5:
        print(f"\n(還有 {len(bad)-5} 案,用 --json 看完整明細)")

    if args.json:
        Path(args.json).write_text(
            json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n明細已寫入 {args.json}")

    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
