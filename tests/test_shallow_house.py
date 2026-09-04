"""淺進深透天(樓梯轉 90°)測試 —— 5×5 米也要生得出合格圖。

窄面寬透天的樓梯是南北向的,光樓梯就吃掉 4.4m 進深 → 進深下限 9.5m。這套骨架把
梯段轉成東西向(梯跑改吃面寬),進深下限降到 5m。驗四件事:

  1. 定義域(5~9m × 5m 以上)每個尺寸都過**兩道關卡**,連「太小/太細長」的警告都沒有。
  2. 骨架該有的樣子:梯向東西、樓梯上下對齊、界牆不開窗、1F 有臨路大門。
  3. 面寬決定分間:窄 → 開放式廚房、一間臥室;寬 → 客廳+廚房、兩間臥室。
  4. 自動選路:淺基地走這套、深基地仍走 narrow_house(不搶別人的活)。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from shapely.geometry import Polygon

from src.design.layout.code_check import check_code_building
from src.design.layout.plan_check import check_building
from src.design.layout.shallow_house import (
    MAX_WIDTH,
    MIN_DEPTH,
    MIN_WIDTH,
    generate_shallow_building,
    generate_shallow_house,
)

# 「房間太小/太細長」類的警告——淺骨架最容易犯的錯,一個都不准有。
TOO_SMALL = {"bedroom_side", "bedroom_area", "room_skinny", "room_no_daylight",
             "corridor_width", "room_oversize"}


# ── 定義域全掃描 ────────────────────────────────────────────────────────────
@pytest.mark.parametrize("bw", [5000.0, 6000.0, 7000.0, 9000.0])
@pytest.mark.parametrize("bd", [5000.0, 6000.0, 8000.0])
@pytest.mark.slow
def test_domain_passes_both_gates(bw, bd):
    """★★ 5~9m × 5~8m 全部要過 plan_check + code_check,且無「太小」類警告。"""
    floors = generate_shallow_building(bw, bd, floors=3)
    plan = check_building(floors)
    code = check_code_building(floors)
    assert plan.ok, [(i.code, i.floor, i.room) for i in plan.errors]
    assert code.ok, [(i.code, i.floor, i.room) for i in code.violations]
    got = {i.code for i in plan.warnings} | {i.code for i in code.warnings}
    assert not (got & TOO_SMALL), sorted(got & TOO_SMALL)


def test_five_by_five_works():
    """★★ 使用者要的下限:建築 5×5 米(一層 25㎡)生得出合格圖。"""
    floors = generate_shallow_building(5000, 5000, floors=3)
    assert len(floors) == 3
    assert check_building(floors).ok and check_code_building(floors).ok
    kinds = {r.kind for _lb, sp in floors for r in sp.rooms}
    assert {"stair_hall", "bathroom"} <= kinds          # 該有的服務空間都在


# ── 骨架該有的樣子 ──────────────────────────────────────────────────────────
def test_stair_runs_east_west_and_aligns():
    """★ 梯段東西向(這正是進深能縮到 5m 的原因),且每層同位置。"""
    floors = generate_shallow_building(6000, 5500, floors=3)
    origins = set()
    for _lb, spec in floors:
        st = spec.stairs[0]
        assert st.direction in ("east", "west")
        origins.add((round(st.origin[0]), round(st.origin[1])))
    assert len(origins) == 1, f"樓梯沒有上下對齊:{origins}"


def test_stair_band_is_shallower_than_narrow_core():
    """★ 梯帶進深 ≤2.1m —— 對照 narrow_house 的核要 4.4m,差距就是這套骨架的意義。"""
    spec = generate_shallow_house(6000, 5000)
    hall = next(Polygon(r.points) for r in spec.rooms if r.kind == "stair_hall")
    x0, y0, x1, y1 = hall.bounds
    assert (y1 - y0) <= 2100.0
    assert (x1 - x0) >= 4000.0                          # 梯跑沿面寬跑


def test_party_walls_have_no_windows_and_1f_has_entry():
    """★ 共壁透天:東西界牆不開窗;1F 南向外牆有臨路大門。"""
    spec = generate_shallow_house(6000, 5500)
    bx0, bx1 = spec.setback, spec.setback + 6000
    for w in spec.walls:
        vertical = abs(w.start[0] - w.end[0]) < 1
        on_party = min(abs(w.start[0] - bx0), abs(w.start[0] - bx1)) < 50
        if vertical and on_party:
            assert not [op for op in w.openings if op.kind == "window"]
    south = [w for w in spec.walls
             if abs(w.start[1] - spec.setback) < 50
             and abs(w.end[1] - spec.setback) < 50
             and abs(w.start[0] - w.end[0]) > 1]
    assert any(op.kind == "door" for w in south for op in w.openings)


def test_width_decides_how_many_rooms():
    """★ 窄 → 開放式廚房 / 一間臥室;寬 → 客廳+廚房 / 兩間臥室。

    5m 面寬硬切廚房只會生出 2m 寬的走廊狀廚房,小宅本來就是開放式廚房。"""
    narrow = generate_shallow_building(5000, 5000, floors=2)
    wide = generate_shallow_building(8000, 6000, floors=2)
    assert not [r for r in narrow[0][1].rooms if r.kind == "kitchen"]
    assert [r for r in wide[0][1].rooms if r.kind == "kitchen"]
    assert len([r for r in narrow[1][1].rooms if r.kind == "bedroom"]) == 1
    assert len([r for r in wide[1][1].rooms if r.kind == "bedroom"]) == 2


def test_deep_lot_caps_building_and_leaves_backyard():
    """★★ 基地深過上限 → 建築封頂、多的留成**後院**(前緣貼建築線)。

    前段只有南面採光,硬加深就是暗房;這條與其他產線同一個做法。"""
    spec = generate_shallow_house(6000, 12000)
    ys = [p[1] for r in spec.rooms for p in r.points]
    assert min(ys) == spec.setback                      # 前緣貼建築線
    assert max(ys) - min(ys) < 12000 - 1000             # 後面留了院子
    assert max(p[1] for p in spec.site_boundary) == 12000 + 2 * spec.setback


# ── 定義域守門 + 自動選路 ───────────────────────────────────────────────────
def test_out_of_domain_is_rejected():
    with pytest.raises(ValueError):
        generate_shallow_house(MIN_WIDTH - 500, 5000)   # 太窄:梯跑放不下
    with pytest.raises(ValueError):
        generate_shallow_house(MAX_WIDTH + 500, 5000)   # 太寬:該用別套骨架
    with pytest.raises(ValueError):
        generate_shallow_house(6000, MIN_DEPTH - 500)   # 太淺:梯帶都放不下


def test_auto_router_picks_shallow_then_narrow():
    """★★ 淺基地走淺骨架(梯向東西)、深基地仍走窄透天(梯向南北)。"""
    from src.design.building_generator import BuildingBrief, generate_building_auto
    from src.design.layout_generator import HouseBrief

    # ⚠️ 拿「樓梯朝向」當骨架的替身要**把核的款式釘住**:窄透天的核自動挑之後
    #    預設是參考圖版(樓梯**橫置**,朝向也是東西向)—— 不釘的話這個替身會把
    #    走對骨架的案子誤判成走錯。
    def _dir(bw, bd):
        brief = BuildingBrief(
            typical=HouseBrief(site_width=bw + 4000, site_depth=bd + 4000,
                               bedrooms=3, core_style="default"),
            floors=3, differentiated=True)
        return generate_building_auto(brief).floors[0].spec.stairs[0].direction

    assert _dir(5000, 5000) == "east"
    assert _dir(5000, 12000) == "north"


# ── 樓梯把梯帶切開:剩料要留在起步端 ────────────────────────────────────────
@pytest.mark.parametrize("bw,bd", [(5100.0, 6900.0), (5800.0, 9300.0),
                                   (6800.0, 7900.0), (7200.0, 9000.0),
                                   (8600.0, 8600.0)])
def test_no_room_is_stranded_behind_the_stairs(bw, bd):
    """★★★ 每一間房都走得到,而且**不准踩過樓梯**(使用者 2026-08-28)。

    梯帶只有 1.9m 深,梯段兩側各只剩 75mm —— 人繞不過去。梯跑吃不完整條梯帶,
    剩下那一截落在哪一端,就決定那一端是「樓層地板」還是「死角」:

      舊做法:起步平台只留 900,多的全堆在**折返端**(半層高,不是地板)
              → 而浴廁正好在前段東側,唯一開得了門的鄰居就是那塊死角
              → **上廁所要踩過樓梯**(實測 70 個樓層中 26 個)。
      現在:  梯段貼東牆、剩料全歸西端的起步平台,浴廁跟著搬到前段西側。

    ⚠️ 兩道關卡原本都問錯問題:`floor_split` 一間房算一個節點、
       `room_circulation` 的障礙只有家具(看不見樓梯)。
    """
    from tests.test_narrow_house import _walk_islands
    for _lb, spec in generate_shallow_building(bw, bd, floors=3):
        _home, lost = _walk_islands(spec)
        assert lost == [], (bw, bd, _lb, lost)


# ── 2026-09-04:使用者「全部尺寸的住宅都幫我用這些規則去排」量出來的三條 ──────
@pytest.mark.parametrize("bw", [7000.0, 8000.0, 9000.0])
@pytest.mark.parametrize("bd", [6000.0, 8000.0])
def test_every_floor_has_the_same_footprint(bw, bd):
    """★★ 各層的外牆要對齊 —— 樓上不能有房間懸在樓下的屋頂外面。

    ⚠️ 這條在 2026-09-04 之前是**紅的**(25 個尺寸有 12 個)。根因是採光收進深的
    退讓寫在 `_build_floor` 裡、**每層自己判**:1F 有客廳+廚房、窗要開得比較多,
    收到 4500 才過;樓上只有臥室、不必收,停在 6500 —— 2F 的臥室就有 2m 懸空。
    退讓一律要在**整棟**那一層做(`_fit_depth`),與 `_fit_service` / `_fit_margin`
    / `_fit_bath_side` 同一條規矩。
    """
    from shapely.geometry import Polygon

    from src.design.layout.plan_check import VOID_KINDS
    boxes = []
    for _lb, spec in generate_shallow_building(bw, bd, floors=3):
        ps = [Polygon(r.points) for r in spec.rooms if r.kind not in VOID_KINDS]
        boxes.append((round(min(p.bounds[0] for p in ps)),
                      round(min(p.bounds[1] for p in ps)),
                      round(max(p.bounds[2] for p in ps)),
                      round(max(p.bounds[3] for p in ps))))
    assert len(set(boxes)) == 1, f"各層外框不一樣:{boxes}"


@pytest.mark.parametrize("bw,bd", [(5000.0, 5000.0), (7000.0, 6000.0),
                                   (8000.0, 7000.0), (9000.0, 9000.0)])
def test_front_door_never_opens_into_a_bathroom(bw, bd):
    """★★ 大門不能一開就是廁所(台灣室內設計的經典 NG)。

    ⚠️ 這條在 2026-09-04 之前是**紅的**,而且 7 個尺寸全中 —— `_add_front_door`
    只問「撞不撞洞口、離不離牆角夠遠」,**從來沒問過門後面是哪間房**,而預設的
    `entry_frac=0.22` 偏西、淺基地的浴廁正好排在前段西端。
    """
    from shapely.geometry import Point, Polygon

    from src.design.layout.plan_check import building_env
    spec = generate_shallow_building(bw, bd, floors=3)[0][1]
    env = building_env(spec)
    for w in spec.walls:
        for op in w.openings:
            if op.kind != "door" or op.width < 900:
                continue
            p = w.point_at(op.position)
            if not (abs(p[1] - env[1]) < 200 or abs(p[1] - env[3]) < 200
                    or abs(p[0] - env[0]) < 200 or abs(p[0] - env[2]) < 200):
                continue                            # 不是外牆上的門
            for r in spec.rooms:
                if Polygon(r.points).exterior.distance(Point(*p)) < 60:
                    assert r.kind not in ("bathroom", "toilet", "bedroom"), \
                        f"{bw}x{bd} 大門開進 {r.name}({r.kind})"


@pytest.mark.parametrize("bw,bd", [(7000.0, 8000.0), (9000.0, 9000.0),
                                   (8000.0, 7000.0)])
def test_bathroom_depth_is_capped(bw, bd):
    """★ 浴室深度要有上限 —— 居室帶一深,浴室不該跟著長到 9~11㎡。

    書上〈空間最適尺寸〉Space 6 的全套浴室只要 220 寬;窄透天早就有 `BATH_MAX_D`,
    這條產線漏了(實測 2F/3F 的浴室 10.3㎡,`room_oversize` 每個尺寸 2~3 件)。
    多出來的面積**還給隔壁居室**,不是丟給樓梯間(NG03:純走道不該比房間還大)。
    """
    from shapely.geometry import Polygon

    from src.design.layout.narrow_house import BATH_MAX_D
    for _lb, spec in generate_shallow_building(bw, bd, floors=3):
        for r in spec.rooms:
            if r.kind != "bathroom":
                continue
            b = Polygon(r.points).bounds
            assert min(b[2] - b[0], b[3] - b[1]) <= BATH_MAX_D + 1.0
            assert Polygon(r.points).area / 1e6 <= 7.0, f"{r.name} 太大"
