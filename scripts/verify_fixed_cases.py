"""Run: python scripts/verify_fixed_cases.py --output output/verification"""
import argparse
import html
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.design.fixed_verification import run_fixed_cases


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, default=Path('output/verification'))
    args = parser.parse_args()
    report = run_fixed_cases()
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / 'fixed_cases.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    cards = ''.join(f"<article><h2>{html.escape(r['title'])}：{'通過' if r['passed'] else '失敗'}</h2>"
                    f"<p>預期：{html.escape(str(r['expected']))}</p><p>{html.escape(r['scope'])}</p>"
                    f"<details><summary>實際結果與證據</summary><pre>{html.escape(json.dumps(r, ensure_ascii=False, indent=2))}</pre></details></article>" for r in report['cases'])
    (args.output / 'fixed_cases.html').write_text("<!doctype html><meta charset='utf-8'><title>固定案例驗證</title><style>body{font:16px/1.7 system-ui;max-width:960px;margin:40px auto;padding:20px}pre{white-space:pre-wrap;overflow-wrap:anywhere}article{border-top:1px solid #ccc}</style>"
         + f"<h1>固定案例 {report['passed']}/{report['total']} 通過</h1><p>{html.escape(report['scope'])}</p>" + cards, encoding='utf-8')
    print(f"固定案例 {report['passed']}/{report['total']} 通過；報告：{args.output / 'fixed_cases.html'}")
    if report['passed'] != report['total']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
