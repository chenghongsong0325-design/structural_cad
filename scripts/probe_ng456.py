"""探針:量 NG05(迷宮動線)與 NG06(街屋暗房)的現況。

使用者 2026-09-03 給了〈9 種常見 NG 格局〉04~06。NG04 夾層本專案沒做,
剩下兩條要先**量現況**再決定加不加規則(本專案一再踩到「規則存在但關卡沒接」
與「報表會說謊」,所以新判準一律先寫探針量一輪)。

    NG05 → 從大門(1F)/樓梯間(樓上)走到每一間房,要**轉幾個彎**、走多遠
    NG06 → 每一層有幾間**暗房**(沒有對外窗、也不貼天井)

用法:python scripts/probe_ng456.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from shapely.geometry import Point, Polygon

from src.design.building_generator import BuildingBrief, generate_building_auto
from src.design.layout.plan_check import EDGE_TOL, VOID_KINDS
from src.design.layout.room_circulation import turns_to_rooms
from src.design.layout_generator import HouseBrief



# ---------------------------------------------------------------- NG06 暗房
def dark_rooms(spec) -> list:
    """回傳這一層的暗房 [(名稱, kind)] —— 牆上沒有對外窗、也不貼天井。"""
    patios = [Polygon(r.points) for r in spec.rooms if r.kind == "patio"]
    wins = []
    for w in spec.walls:
        for op in w.openings:
            if op.kind == "window":
                wins.append(Point(*w.point_at(op.position)))
    out = []
    for r in spec.rooms:
        if r.kind in VOID_KINDS:
            continue
        poly = Polygon(r.points)
        lit = any(poly.distance(p) < EDGE_TOL for p in patios)
        lit = lit or any(poly.exterior.distance(p) < EDGE_TOL for p in wins)
        if not lit:
            out.append((r.name, r.kind))
    return out


def entry_point(spec):
    """1F 大門的內側點;找不到就用最南邊那間房的中心。"""
    from src.design.layout.plan_check import building_env
    env = building_env(spec)
    for w in spec.walls:
        for op in w.openings:
            if op.kind != "door" or op.width < 850:
                continue
            p = w.point_at(op.position)
            if (abs(p[1] - env[1]) < 200 or abs(p[1] - env[3]) < 200
                    or abs(p[0] - env[0]) < 200 or abs(p[0] - env[2]) < 200):
                cy = (env[1] + env[3]) / 2.0
                return (p[0], p[1] + (600 if p[1] < cy else -600))
    r = min((r for r in spec.rooms if r.kind not in VOID_KINDS),
            key=lambda r: Polygon(r.points).centroid.y)
    c = Polygon(r.points).representative_point()
    return (c.x, c.y)


def stair_point(spec):
    for r in spec.rooms:
        if r.kind == "stair_hall":
            c = Polygon(r.points).representative_point()
            return (c.x, c.y)
    return entry_point(spec)


SIZES = [(4.0, 12.0), (4.5, 14.0), (5.5, 15.0), (6.0, 12.5),
         (7.0, 15.5), (8.0, 16.0)]


def main():
    dark_n = 0
    dark_by_kind = {}
    turn_rows = []
    unreach = 0
    for w, d in SIZES:
        brief = BuildingBrief(
            typical=HouseBrief(site_width=w * 1000, site_depth=d * 1000, bedrooms=3,
                               setback=0, seed=0, dimension_basis="building"),
            floors=3, differentiated=True)
        try:
            b = generate_building_auto(brief)
        except Exception as exc:
            print(f"{w}x{d}  生不出來 {type(exc).__name__}: {exc}")
            continue
        for f in b.floors:
            spec = f.spec
            dk = dark_rooms(spec)
            dark_n += len(dk)
            for _n, k in dk:
                dark_by_kind[k] = dark_by_kind.get(k, 0) + 1
            start = entry_point(spec) if f.label == "1F" else stair_point(spec)
            res = turns_to_rooms(spec, start)
            rooms = [r for r in spec.rooms if r.kind not in VOID_KINDS]
            miss = [r.name for r in rooms if r.name not in res]
            unreach += len(miss)
            mx = max(res.values(), default=0)
            turn_rows.append((f"{w}x{d} {f.label}", mx,
                              sum(res.values()) / max(len(res), 1),
                              ",".join(dn for dn, _ in dk), ",".join(miss)))

    print("== NG05 動線轉折(從大門/樓梯間走到每間房) ==")
    for lb, mx, avg, dk, miss in turn_rows:
        note = f"  暗房[{dk}]" if dk else ""
        note += f"  走不到[{miss}]" if miss else ""
        print(f"{lb:<14} 最多轉 {mx} 個彎  平均 {avg:.1f}{note}")
    print(f"\n== NG06 暗房 == 合計 {dark_n} 間")
    for k, n in sorted(dark_by_kind.items(), key=lambda kv: -kv[1]):
        print(f"  {k:<12} {n}")
    print(f"(走不到的房間 {unreach} —— 探針自己的 sanity check,應為 0)")


if __name__ == "__main__":
    main()
