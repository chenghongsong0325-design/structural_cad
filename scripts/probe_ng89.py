"""探針:量 NG08(用不到的房間)與 NG09(門撞門)的現況。

使用者 2026-09-03 給的〈9 種常見 NG 格局〉第三批。照本專案的規矩:**先量現況、
再決定加不加規則**(「規則存在但關卡沒接」與「報表會說謊」都踩過很多次)。

    NG08 → 有沒有生出「用不到的房間」:多功能室/家庭廳這種用途模糊的溢位房
    NG09 → 門對門(正對面且開啟弧會打架)、以及一小段牆上擠了幾扇門

用法:python scripts/probe_ng89.py
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

from src.design.building_generator import BuildingBrief, generate_building_auto
from src.design.layout_generator import HouseBrief

from src.design.layout.door_rules import (
    CLUSTER_R,
    facing_door_pairs as face_to_face,
    max_door_cluster,
    _real_doors as _doors,
)


def door_cluster(spec):
    return max_door_cluster(spec)


VAGUE_NAMES = ("多功能室", "和室")


def vague_rooms(spec) -> list:
    """NG08:用途模糊的房間(書上點名的和室/多功能室這一類)。"""
    return [r.name for r in spec.rooms if r.name in VAGUE_NAMES]


CASES = [   # (寬 m, 深 m, 層數, 標籤)
    (4.0, 12.0, 3, "一般透天"), (5.5, 15.0, 3, "一般透天"),
    (7.0, 15.5, 3, "一般透天"), (8.0, 16.0, 3, "一般透天"),
    (12.0, 11.0, 3, "兩帶式"), (15.0, 12.0, 3, "兩帶式"),
    (19.0, 13.0, 3, "兩帶式"), (24.0, 16.0, 3, "兩帶式"),
]


def main():
    tot_face = tot_vague = 0
    worst = 0
    for w, d, fl, tag in CASES:
        brief = BuildingBrief(
            typical=HouseBrief(site_width=w * 1000, site_depth=d * 1000,
                               bedrooms=3, setback=0, seed=0,
                               dimension_basis="building"),
            floors=fl, differentiated=True)
        try:
            b = generate_building_auto(brief)
        except Exception as exc:
            print(f"{w}x{d} {tag}  生不出來 {type(exc).__name__}: {exc}")
            continue
        for f in b.floors:
            face = face_to_face(f.spec)
            clus = door_cluster(f.spec)
            vag = vague_rooms(f.spec)
            tot_face += len(face)
            tot_vague += len(vag)
            worst = max(worst, clus)
            note = ""
            if face:
                note += f"  門對門 {len(face)}(最近 {min(h[2] for h in face):.0f}mm)"
            if clus >= 4:
                note += f"  ⚠️{CLUSTER_R/1000:.0f}m 內擠了 {clus} 扇門"
            if vag:
                note += f"  用不到的房 {vag}"
            print(f"{w:>5}x{d:<6}{tag} {f.label}  門 {len(_doors(f.spec)):>2} "
                  f"最擠 {clus}{note}")

    print(f"\n門對門合計 {tot_face} 對 · 最擠的一處 {worst} 扇門 · "
          f"用途模糊的房 {tot_vague} 間")


if __name__ == "__main__":
    main()
