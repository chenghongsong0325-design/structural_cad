"""Reproducible furnished-layout scan for exact total bedroom allocation."""
from __future__ import annotations
import argparse
from collections import Counter
import json
from pathlib import Path
import random
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.design.bedroom_program import BEDROOMS, BedroomCapacityError
from src.design.building_generator import _narrow_to_building, FLOOR_HEIGHT
from src.design.layout.narrow_house import generate_narrow_building
from src.design.validation import validate_building


def run(case):
    try:
        floors = generate_narrow_building(case['width_mm'], case['depth_mm'],
            floors=case['floors'], garage=case['garage'],
            bedroom_target=case['requested'], seed=case['seed'], furnish=case.get('furnish', True),
            core_style=case.get('core_style'))
        actual = sum(r.kind in BEDROOMS for _, spec in floors for r in spec.rooms)
        validation = validate_building(_narrow_to_building(floors, FLOOR_HEIGHT))
        issues = [i for key in ('plan_check', 'code_check')
                  for i in validation.get(key, {}).get('issues', [])]
        passed = actual == case['requested'] and validation['validation']['status'] == 'passed'
        return {**case, 'status': 'passed' if passed else 'failed', 'actual': actual,
                'validation': validation['validation'], 'issues': issues}
    except BedroomCapacityError as exc:
        return {**case, 'status': 'capacity_rejected', 'conflict': exc.conflict}
    except Exception as exc:
        return {**case, 'status': 'raised', 'error': type(exc).__name__, 'detail': str(exc)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--n', type=int, default=80)
    parser.add_argument('--seed', type=int, default=0)
    parser.add_argument('--no-furnish', action='store_true', help='Geometry-only scan; does not test furniture placement')
    parser.add_argument('--core-style', choices=('auto', 'ref', 'mid', 'default'), default='auto')
    parser.add_argument('--json', type=Path, required=True)
    args = parser.parse_args()
    rng = random.Random(args.seed)
    results = []
    for i in range(args.n):
        case = {'width_mm': round(rng.uniform(4500, 7500), -2),
                'depth_mm': round(rng.uniform(14000, 18000), -2),
                'floors': rng.choice([3, 4]), 'garage': bool(rng.randrange(2)),
                'requested': rng.randint(1, 4), 'seed': rng.randint(0, 9999), 'furnish': not args.no_furnish,
                'core_style': None if args.core_style == 'auto' else args.core_style}
        print(f"START {i+1}/{args.n}: {case}", flush=True)
        started = time.monotonic()
        result = run(case)
        result['elapsed_seconds'] = round(time.monotonic() - started, 3)
        results.append(result)
        args.json.write_text(json.dumps({'seed': args.seed, 'requested_cases': args.n,
            'counts': dict(Counter(r['status'] for r in results)), 'cases': results},
            ensure_ascii=False, indent=2), encoding='utf-8')
        print(f"{i+1}/{args.n}: {result['status']} {case}", flush=True)
    counts = Counter(r['status'] for r in results)
    print(json.dumps(counts), flush=True)
    # Capacity rejections remain explicit and are never counted as passed plans.
    return int(bool(counts['failed'] or counts['raised']))


if __name__ == '__main__':
    raise SystemExit(main())
