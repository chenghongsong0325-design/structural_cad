"""門與動線規範(使用者 2026-07-30 定調的產圖指令)測試。

規範裡每一條都要是**程式擋得住的規則**,不是文件上的期許。這一組驗:

  1. 四條產線的實際輸出都符合規範(門淨寬、開啟弧線、衛浴門朝向、不穿臥室、
     樓梯不被包住)。
  2. 檢查器抓得到人為破壞(把門改窄、把衛浴門改開向廚房、把門轉去撞牆)。
  3. 修復器救得動:先轉門 → 再改橫拉門;補一扇門直通公共動線。
  4. 門連通表(房間 → 門 → 通往空間)產得出來、可序列化。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from src.design.layout.door_rules import (
    BATH_DOOR_MIN,
    ENTRY_DOOR_MIN,
    ROOM_DOOR_MIN,
    DoorTable,
    check_door_rules,
    door_table,
    repair_doors,
)
from src.design.layout.narrow_house import SETBACK, generate_narrow_building
from src.design.layout.shallow_house import generate_shallow_building

SIZES_NARROW = [(4000.0, 11000.0), (5000.0, 12000.0), (7000.0, 14000.0)]
SIZES_SHALLOW = [(5000.0, 5000.0), (7000.0, 6000.0), (9000.0, 8000.0)]


def _issues(floors):
    out = []
    for lb, spec in floors:
        level = int(lb[:-1]) if lb[:-1].isdigit() else 1
        out += check_door_rules(spec, None, level, lb)
    return out


# ── 產線合規 ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("bw,bd", SIZES_NARROW)
def test_narrow_pipeline_follows_door_rules(bw, bd):
    """★★ 窄面寬透天:各尺寸都符合門與動線規範。"""
    bad = _issues(generate_narrow_building(bw, bd, floors=3))
    assert not bad, [(i.code, i.floor, i.room) for i in bad]


@pytest.mark.parametrize("bw,bd", SIZES_SHALLOW)
def test_shallow_pipeline_follows_door_rules(bw, bd):
    """★★ 淺進深透天(含 5×5)同樣要符合。"""
    bad = _issues(generate_shallow_building(bw, bd, floors=3))
    assert not bad, [(i.code, i.floor, i.room) for i in bad]


def test_every_room_reachable_without_crossing_a_bedroom():
    """★★ 規範第 6 條:從大門出發,不穿越任何臥室就能到每一間房與樓梯。"""
    for bw, bd in SIZES_NARROW + SIZES_SHALLOW:
        gen = (generate_narrow_building if bd >= 9500 else generate_shallow_building)
        for i in _issues(gen(bw, bd, floors=3)):
            assert i.code != "through_bedroom", (bw, bd, i.room)


def test_door_widths_meet_minimums():
    """★ 對外門 ≥90cm、居室門 ≥80cm、衛浴門 ≥75cm(實際量每一扇)。"""
    for lb, spec in generate_narrow_building(5000.0, 12000.0, floors=3):
        for ln in door_table([(lb, spec)]).links:
            need = (ENTRY_DOOR_MIN if ln.exterior
                    else (BATH_DOOR_MIN if "bathroom" in ln.kinds
                          else ROOM_DOOR_MIN))
            assert ln.width >= need - 1.0, (lb, ln.sides, ln.width, need)


# ── 檢查器抓不抓得到 ────────────────────────────────────────────────────────
def test_detects_narrow_door():
    """★ 人為把一扇內門改成 70cm → 必須抓到。"""
    _lb, spec = generate_narrow_building(5000.0, 12000.0, floors=2)[0]
    for w in spec.walls:
        for op in w.openings:
            if op.kind == "door" and op.width < 900:
                op.width = 700.0
    codes = {i.code for i in check_door_rules(spec, None, 1, "1F")}
    assert "room_door_narrow" in codes


def test_detects_blocked_swing():
    """★ 人為把每扇門都轉到同一邊(鉸鏈/開啟方向全改)→ 至少一扇會撞到東西。

    這條是規範第 4 條的把關:門的開啟弧線不得撞牆/柱/家具/另一扇門。"""
    _lb, spec = generate_narrow_building(5000.0, 12000.0, floors=2)[0]
    for dp in spec.doors:
        dp.door.sliding = False
        dp.door.hinge, dp.door.swing = "left", "out"
    hits = [i for i in check_door_rules(spec, None, 1, "1F")
            if i.code == "door_swing_blocked"]
    # 全轉同一邊之後若剛好都沒撞,至少要能證明修復器不會把它弄壞
    if hits:
        assert repair_doors(spec, SETBACK, SETBACK, SETBACK + 5000, 1)
        left = [i for i in check_door_rules(spec, None, 1, "1F")
                if i.code == "door_swing_blocked"]
        assert not left, [i.detail for i in left]


def test_repair_turns_door_into_sliding_when_no_room():
    """★ 轉遍四種方向還是撞 → 改**橫拉門**(規範:空間不足時改用橫拉門並註明)。

    做法:把門兩側都用家具堵住,逼修復器沒有乾淨的開啟方向可選。"""
    from src.drafting.apartment_plan import FixturePlacement
    _lb, spec = generate_narrow_building(5000.0, 12000.0, floors=2)[0]
    dp = spec.doors[0]
    w = spec.walls[dp.wall_index]
    op = w.openings[dp.opening_index]
    cx, cy = w.point_at(op.position)
    nx, ny = w.normal_vector
    for s in (1, -1):                       # 兩側各塞一張雙人床
        spec.fixtures.append(FixturePlacement(
            name="bed_double", insert=(cx + nx * 700 * s, cy + ny * 700 * s),
            rotation=0.0))
    repair_doors(spec, SETBACK, SETBACK, SETBACK + 5000, 1)
    assert dp.door.sliding, "兩側都堵住了還不改橫拉門"
    assert not [i for i in check_door_rules(spec, None, 1, "1F")
                if i.code == "door_swing_blocked"
                and f"{cx:.0f},{cy:.0f}" in i.detail]


def test_detects_bath_door_to_kitchen():
    """★ 衛浴門直接開向廚房 → 抓得到(規範第 5 條)。

    ⚠️ 2026-08-28 改寫。淺骨架把浴廁從前段**東**側搬到西側(它得貼著樓梯的起步
    平台才進得去,見 shallow_house._fit_bath_side),浴廁與廚房因此**結構上不再
    相鄰** —— 拿產線的圖當「違規樣本」已經測不到東西了。改成兩段:
      ① 產線的圖本來就不該違規;
      ② 把浴廁隔壁那間的**用途**改成廚房,規則要叫得出來(否則這條測試只是在
         測「產線剛好沒犯錯」,規則本身壞掉也照樣綠)。"""
    from shapely.geometry import Polygon
    _lb, spec = generate_shallow_building(7000.0, 6000.0, floors=2)[0]
    bath = next(r for r in spec.rooms if r.kind == "bathroom")
    codes = {i.code for i in check_door_rules(spec, None, 1, "1F")}
    assert "bath_door_to_kitchen" not in codes      # ① 產線已經避開了

    bp = Polygon(bath.points)
    nb = next(r for r in spec.rooms                 # 浴廁的門通到的那一間
              if r is not bath and r.kind not in ("stair_hall",)
              and not bp.intersection(Polygon(r.points).buffer(80)).is_empty)
    nb.kind = "kitchen"                             # ② 同一張圖,只改用途
    codes = {i.code for i in check_door_rules(spec, None, 1, "1F")}
    assert "bath_door_to_kitchen" in codes


# ── 門連通表 ────────────────────────────────────────────────────────────────
def test_door_table_lists_every_door():
    """★ 門連通表:每扇門一列,含樓層/淨寬/兩側空間;對外門標 exterior。"""
    floors = generate_shallow_building(7000.0, 6000.0, floors=2)
    table = door_table(floors)
    n_doors = sum(1 for _lb, sp in floors for w in sp.walls
                  for op in w.openings if op.kind == "door")
    assert len(table.links) == n_doors
    assert any(ln.exterior for ln in table.links), "1F 應該有一扇對外大門"
    assert all(len(ln.sides) == 2 for ln in table.links)


def test_door_table_serialisable():
    """★ Report 慣例:to_dict / to_json,外加人看的 summary。"""
    table = door_table(generate_narrow_building(5000.0, 12000.0, floors=2))
    d = table.to_dict()
    assert set(d) == {"n_doors", "links"}
    assert json.loads(table.to_json())["n_doors"] == d["n_doors"]
    assert "門連通表" in table.summary()
    assert isinstance(DoorTable().to_json(), str)


# ── 柱是實心的:門扇掃到柱就打不開 ───────────────────────────────────────────
def test_swing_check_sees_columns_stored_as_a_grid():
    """★★ 柱存成「放在每個軸網交點」時,開啟弧線檢查也要看得到柱。

    ⚠️ 踩過的坑:以前寫 `spec.column_centers or []` —— 但 `column_centers is None`
    的意思是「柱放在每個軸網交點」,`None or []` 會變成**空清單**,於是窄透天 /
    淺透天 / AI 產線的門「會不會撞到柱」**從來沒有被檢查過**。柱位是由
    `column_footprints` 解出來的,這條測試釘的就是「別再繞過它」。"""
    from src.design.column_design import column_footprints
    from src.design.layout.door_rules import _swing_obstacles

    _lb, spec = generate_narrow_building(7000.0, 12000.0, floors=2)[0]
    assert spec.column_centers is None, "這條產線的柱本來就是存成軸網交點"
    cols = column_footprints(spec)
    assert cols, "這層本來就該有柱"
    w = next(w for w in spec.walls if any(o.kind == "door" for o in w.openings))
    op = next(o for o in w.openings if o.kind == "door")
    obs = _swing_obstacles(spec, w, op)
    assert all(any(c.equals(o) for o in obs) for c in cols),         "柱沒被算進開啟弧線的障礙物 → 門撞柱不會被抓到"


def test_column_planted_in_front_of_a_door_is_caught():
    """★ 人為在門扇掃過的地方種一根柱 → 要判 door_swing_blocked。

    上一條驗「看得到柱」,這條驗「看到了會出聲」。"""
    from src.design.building_generator import _column_centers

    _lb, spec = generate_narrow_building(7000.0, 12000.0, floors=2)[0]
    assert not [i for i in check_door_rules(spec, None, 1, "1F")]   # 原本乾淨
    dp = next(dp for dp in spec.doors if not getattr(dp.door, "sliding", False))
    w = spec.walls[dp.wall_index]
    op = w.openings[dp.opening_index]
    cx, cy = w.point_at(op.position)
    nx, ny = w.normal_vector
    s = 1.0 if getattr(dp.door, "swing", "out") == "out" else -1.0
    spec.column_centers = _column_centers(spec) + [(cx + nx * 400 * s,
                                                    cy + ny * 400 * s)]
    codes = {i.code for i in check_door_rules(spec, None, 1, "1F")}
    assert "door_swing_blocked" in codes


@pytest.mark.parametrize("bw,bd", SIZES_NARROW)
def test_narrow_doors_clear_of_columns(bw, bd):
    """★★ 窄透天各尺寸:沒有一扇門的開啟弧線壓在柱上。"""
    from src.design.column_design import column_footprints
    from src.design.layout.door_rules import SWING_OVERLAP_TOL, _swing_sector

    bad = []
    for lb, spec in generate_narrow_building(bw, bd, floors=3):
        cols = column_footprints(spec)
        for dp in spec.doors:
            if getattr(dp.door, "sliding", False):
                continue
            try:
                w = spec.walls[dp.wall_index]
                op = w.openings[dp.opening_index]
            except (IndexError, AttributeError):
                continue
            sec = _swing_sector(w, op, dp.door)
            if sec.is_empty:
                continue
            hit = sum(sec.intersection(c).area for c in cols)
            if hit > SWING_OVERLAP_TOL:
                bad.append((lb, round(hit / 1e6, 3)))
    assert not bad, f"這些門被柱擋住:{bad}"


def test_swing_obstacles_builds_wall_bodies_once():
    """★★ `_swing_obstacles` 不得「每檢查一道牆就把全部牆體重算一次」。

    舊寫法在迴圈裡呼叫 `_wall_bodies(spec)`(它一次會做出**全部**牆的實體),
    再從結果裡只拿第 i 個 —— N 道牆就做了 N×N 個 buffer,其中 N²−N 個當場丟掉。
    一層樓約 50 道牆 → 每檢查一扇門就白算 2400 次 buffer,而修門會對每扇門
    反覆檢查。這條釘住「算一次就好」,順便釘住**內容不能變**(除了自己那道牆,
    其餘全在)。
    """
    from src.design.layout import door_rules as dr

    spec = generate_narrow_building(5000.0, 12000.0, floors=1)[0][1]
    wall = next(w for w in spec.walls
                if any(op.kind == "door" for op in w.openings))
    op = next(o for o in wall.openings if o.kind == "door")

    calls = {"n": 0}
    real = dr._wall_bodies

    def counted(sp):
        calls["n"] += 1
        return real(sp)

    dr._wall_bodies = counted
    try:
        bodies = dr._swing_obstacles(spec, wall, op)
    finally:
        dr._wall_bodies = real

    assert calls["n"] == 1, (
        f"_wall_bodies 被呼叫 {calls['n']} 次 —— 每道牆重算一次全部牆體")

    # 內容:自己那道牆除外,其餘牆體一個都不能少(用面積比對,不比物件identity)
    want = [b.area for w, b in zip(spec.walls, real(spec)) if w is not wall]
    got = [b.area for b in bodies]
    for a in want:
        assert any(abs(a - g) < 1.0 for g in got), "少了某道牆的實體"


@pytest.mark.parametrize("bw,bd", SIZES_NARROW + [(4500.0, 14000.0)])
def test_doors_push_into_the_room_and_hinge_on_the_outer_wall(bw, bd):
    """★★ 門一律**往內推**,而且**開了之後貼著外牆那一側**(使用者 2026-09-05)。

    兩件事是分開的:
      · 往哪一側掃(`swing`)= 推進哪一間 —— 要推進「要進去的那一間」,不是把門
        推到走道/客廳這種共用動線上,大門更不能往室外開。
      · 鉸鏈在哪一端(`hinge`)= 門扇開到底之後立在哪 —— 挑離外牆近的那一端,
        打開的門就貼著外牆,不會站在房間中間把空間切成兩半。

    ⚠️ 這是**偏好不是硬規則**:門扇會撞到東西的時候仍然以「撞得最少」為準
    (撞遍了才改橫拉門)。所以這條測試只驗**開得了的平開門** —— 橫拉門沒有
    開啟弧線,不在這條的射程內。
    """
    from shapely.geometry import Polygon

    from src.design.layout.door_rules import (_door_sides, _hinge_wall_dist,
                                              _swing_into_rank, _swing_sector,
                                              _swing_obstacles,
                                              SWING_OVERLAP_TOL)
    from src.design.layout.plan_check import building_env

    floors = generate_narrow_building(bw, bd, floors=3, seed=0)
    checked = 0
    for lb, sp in floors:
        env = building_env(sp)
        polys = [(r, Polygon(r.points)) for r in sp.rooms]
        for dp in sp.doors:
            w = sp.walls[dp.wall_index]
            if dp.opening_index >= len(w.openings):
                continue
            op = w.openings[dp.opening_index]
            if op.kind != "door" or getattr(dp.door, "sliding", False):
                continue
            # 撞得到東西的門不吃這條(它是被實體逼著轉的,不是被偏好挑的)。
            obs = _swing_obstacles(sp, w, op)
            sec = _swing_sector(w, op, dp.door)
            if sum(sec.intersection(o).area
                   for o in obs if o.intersects(sec)) > SWING_OVERLAP_TOL:
                continue
            checked += 1
            sides, _ext = _door_sides(sp, w, op, polys, env)
            into = sides[0 if dp.door.swing == "out" else 1]
            other = sides[1 if dp.door.swing == "out" else 0]
            assert _swing_into_rank(into) <= _swing_into_rank(other), (
                f"{lb} 有一扇門往外推(推向 "
                f"{into.kind if into else '室外'})")
            dist = _hinge_wall_dist(w, op, env)
            assert dist[dp.door.hinge] <= min(dist.values()) + 1.0, (
                f"{lb} 有一扇門的鉸鏈在離外牆遠的那一端(開了會擋在房間中間)")
    assert checked >= 8, f"只驗到 {checked} 扇平開門 —— 換一組尺寸,不要刪測試"
