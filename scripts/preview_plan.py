"""生一棟樓 → 每張圖存成 PNG + 跑 plan_check,一支指令看到結果。

用途:使用者驗收的方式是**看圖**,不是看測試數字。改完格局相關的程式碼,
拿這支生預覽,人和 AI 都直接看得到畫出來長什麼樣。
(`preview.py` 是寫死 hello_cad.dxf 的一次性腳本,不要拿它當通用工具。)

用法::

    python scripts/preview_plan.py --width 6 --depth 12 --bedrooms 3 --floors 3
    python scripts/preview_plan.py --width 15 --depth 15 --out output/preview/big
    python scripts/preview_plan.py --width 6 --depth 12 --sheet   # 連 A3 圖框一起畫

尺寸單位是**公尺**,一律當**建築物**尺寸(setback=0,site=building),跟
scan_plans.py 同一套慣例 —— 這樣寬度就直接對應各骨架的 MIN/MAX_WIDTH,
不會被退縮換算糊掉。

退出碼:plan_check 全合格 = 0,有硬錯誤 = 1。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Windows 主控台預設 cp950,印 ✅/❌ 會整支炸掉 → 強制 UTF-8。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from src.design.benchmark import render_png
from src.design.building_generator import BuildingBrief, generate_building_auto
from src.design.layout.plan_check import check_building
from src.design.layout_generator import HouseBrief
from src.drafting.preview_font import preview_font
from src.web.render import build_sheets


def main() -> int:
    ap = argparse.ArgumentParser(description="生成一棟樓並輸出 PNG 預覽")
    ap.add_argument("--width", type=float, required=True, help="建築寬(公尺)")
    ap.add_argument("--depth", type=float, required=True, help="建築深(公尺)")
    ap.add_argument("--bedrooms", type=int, default=3, help="房間數(預設 3)")
    ap.add_argument("--floors", type=int, default=2, help="樓層數(預設 2)")
    ap.add_argument("--seed", type=int, default=0, help="亂數種子(可重現)")
    ap.add_argument("--out", default="output/preview", help="輸出資料夾")
    ap.add_argument("--sheet", action="store_true",
                    help="畫含 A3 圖框的版本(預設用去框版,平面本身看得更清楚)")
    ap.add_argument("--dpi", type=int, default=110, help="PNG 解析度")
    ap.add_argument("--lineweight", type=float, default=6,
                    help="線粗倍率(預設 6:牆不糊成一團,標註文字看得清楚)")
    args = ap.parse_args()

    brief = BuildingBrief(
        typical=HouseBrief(site_width=args.width * 1000,
                           site_depth=args.depth * 1000,
                           bedrooms=args.bedrooms, setback=0, seed=args.seed),
        floors=args.floors,
        # 跟 nl_parser / scan_plans 一致:透天只要多層就走層別分化
        # (1F 公共層 / 2F+ 臥室層)。不開的話 2F 會整層複製 1F、連大門一起。
        differentiated=args.floors > 1,
    )

    building = generate_building_auto(brief)
    print(f"{args.width:g}×{args.depth:g}m、{args.bedrooms}房、{args.floors}層 "
          f"(seed={args.seed})")

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    for sheet in build_sheets(building):
        doc = sheet.doc if args.sheet else (sheet.preview_doc or sheet.doc)
        path = out_dir / f"{sheet.label}.png"
        # ⚠️ 一定要包 preview_font:STRUCT 樣式是標楷體,轉成向量路徑時中文會
        #    破碎(原因見 src/drafting/preview_font.py)。網頁的 SVG/PDF 早就包了,
        #    這支 PNG 以前漏掉 → 使用者看到的預覽圖中文全是裂的。
        with preview_font(doc):
            render_png(doc, path, dpi=args.dpi,
                       lineweight=args.lineweight, size=10)
        print(f"  [OK] {path}")

    report = check_building([(f.label, f.spec) for f in building.floors])
    print()
    print(report.summary())
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
