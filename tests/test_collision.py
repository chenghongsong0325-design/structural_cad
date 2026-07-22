"""Collision Engine 測試(v0.6 Phase 1)。

Step 1 重點:detector 與 validate_spec 的家具檢核**逐字一致**——這是「接進
流程零 regression」的保證(detector 在合格圖上必須也回空,才不會誤判去動家具)。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.design.collision import collision_problems, find_collisions
from src.design.collision.geometry import (
    collect_active,
    door_swing_obstacles,
    fixture_obstacles,
)
from src.design.layout_generator import (
    HouseBrief,
    generate_floor_plan,
    generate_house_public,
    generate_house_upper,
    validate_spec,
)
from src.drafting.fixtures import FixturePlacement

_SPECS = [
    lambda: generate_floor_plan(
        HouseBrief(site_width=16000, site_depth=14000, bedrooms=3)),
    lambda: generate_house_upper(
        HouseBrief(site_width=26000, site_depth=16000, bedrooms=3, seed=1)),
    lambda: generate_house_public(
        HouseBrief(site_width=22000, site_depth=14000, bedrooms=3, seed=1)),
]


def test_detector_empty_on_passing_specs():
    """合格圖:detector 無碰撞,且與 validate 的家具問題一致(皆空)。"""
    for make in _SPECS:
        spec = make()
        assert collision_problems(spec) == []
        val_fixture_problems = [p for p in validate_spec(spec)
                                if p.startswith("家具")]
        assert val_fixture_problems == []
        assert find_collisions(collect_active(spec)) == []


def test_detector_matches_validate_on_injected_overlap():
    """注入一件重疊家具:detector 與 validate 都要抓到「家具重疊」。"""
    spec = generate_floor_plan(
        HouseBrief(site_width=16000, site_depth=14000, bedrooms=3))
    fx = next(f for f in spec.fixtures if isinstance(f, FixturePlacement))
    spec.fixtures.append(FixturePlacement(fx.name, fx.insert, fx.rotation))
    probs = collision_problems(spec)
    assert any("家具重疊" in p for p in probs)
    assert any("家具重疊" in p for p in validate_spec(spec))


def test_obstacle_movable_flags():
    """家具是 movable、門迴轉是 static——detector 靠這區分誰能被修復。"""
    spec = _SPECS[0]()
    assert fixture_obstacles(spec) and all(o.movable for o in fixture_obstacles(spec))
    assert door_swing_obstacles(spec) and all(
        not o.movable for o in door_swing_obstacles(spec))


# ── Step 2:priority + resolver ────────────────────────────────────────────
def test_priority_and_droppable():
    from src.design.collision.priority import is_droppable, priority_of
    assert priority_of("bed_double") == 3          # 必要
    assert priority_of("coffee_table") == 1        # 裝飾
    assert is_droppable("coffee_table") and not is_droppable("sofa3")


def test_try_drop_only_decorative():
    from shapely.geometry import Polygon
    from src.design.collision.obstacle import FURNITURE, Obstacle
    from src.design.collision.resolver import try_drop
    from src.drafting.fixtures import FixturePlacement as FP
    from src.drafting.fixtures import fixture_footprint

    def ob(name):
        fx = FP(name, (0, 0), 0)
        return Obstacle(poly=Polygon(fixture_footprint(fx)), kind=FURNITURE,
                        movable=True, ref=fx, tag=name)
    assert try_drop(ob("coffee_table")) is True
    assert try_drop(ob("sofa3")) is False          # 必要不丟


def test_try_move_slides_along_wall_to_clear():
    from shapely.geometry import Polygon
    from src.design.collision.obstacle import FURNITURE, Obstacle
    from src.design.collision.resolver import try_move
    from src.drafting.fixtures import FixturePlacement as FP
    from src.drafting.fixtures import fixture_footprint

    room = Polygon([(0, 0), (5000, 0), (5000, 4000), (0, 4000)])
    fx = FP("sofa3", (1000, 75), 0)                # 貼南牆,朝北伸
    ob = Obstacle(poly=Polygon(fixture_footprint(fx)), kind=FURNITURE,
                  movable=True, ref=fx, tag="sofa3", room=room)
    blocker = Polygon([(775, 200), (1225, 200), (1225, 600), (775, 600)])
    assert try_move(ob, [blocker]) is True
    assert ob.poly.intersection(blocker).area <= 100    # 已閃開
    assert room.contains(ob.poly.centroid)              # 仍在房內
    assert fx.insert != (1000, 75)                      # 真的有移動


# ── Step 3:engine（no-op 保證 + 修復注入的碰撞）──────────────────────────
def test_engine_is_noop_on_passing_spec():
    """核心 regression 保證:合格圖上 resolve 完全不動 spec.fixtures。"""
    from src.design.collision import CollisionEngine
    for make in _SPECS:
        spec = make()
        before = [(getattr(f, "name", "counter"), getattr(f, "insert", None),
                   getattr(f, "start", None)) for f in spec.fixtures]
        report = CollisionEngine(spec).resolve()
        after = [(getattr(f, "name", "counter"), getattr(f, "insert", None),
                  getattr(f, "start", None)) for f in spec.fixtures]
        assert report.changed is False
        assert before == after


def test_engine_resolves_injected_decorative_overlap():
    """注入一件疊在沙發上的裝飾家具(茶几)→ engine 應移走或丟掉,碰撞消失。"""
    from src.design.collision import CollisionEngine, collision_problems
    spec = generate_floor_plan(
        HouseBrief(site_width=20000, site_depth=14000, bedrooms=3))
    sofa = next(f for f in spec.fixtures
                if isinstance(f, FixturePlacement) and f.name == "sofa3")
    spec.fixtures.append(FixturePlacement("coffee_table", sofa.insert, 0))
    assert any("家具重疊" in p for p in collision_problems(spec))   # 先確定有撞
    report = CollisionEngine(spec).resolve()
    assert report.changed                                          # 有處理
    assert collision_problems(spec) == []                          # 撞解掉了


# ── Phase 2:Wall Collision(Room Polygon barrier)──────────────────────────
def test_wall_obstacles_are_rooms():
    """牆障礙 = 各房間多邊形(kind=wall、static),天井除外。"""
    from src.design.collision.geometry import wall_obstacles
    from src.design.collision.obstacle import WALL
    spec = _SPECS[0]()
    walls = wall_obstacles(spec)
    assert walls and all(w.kind == WALL and not w.movable for w in walls)
    assert not any(w.tag == "天井" or w.tag == "中庭" for w in walls)


def test_flush_furniture_not_flagged_as_wall_crossing():
    """核心 regression 保證:貼牆家具(footprint 壓到牆內半邊)不算穿牆。"""
    from src.design.collision.detector import find_collisions
    from src.design.collision.geometry import collect_active
    from src.design.collision.obstacle import WALL
    for make in _SPECS:
        spec = make()
        wall_cols = [c for c in find_collisions(collect_active(spec))
                     if c.b.kind == WALL]
        assert wall_cols == []


def test_wall_crossing_detected_and_resolved():
    """注入一件穿牆家具(把沙發往牆外推 1m)→ detector 抓到、engine 修回房內。"""
    from src.design.collision import CollisionEngine
    from src.design.collision.detector import find_collisions
    from src.design.collision.geometry import collect_active
    from src.design.collision.obstacle import WALL
    spec = generate_floor_plan(
        HouseBrief(site_width=20000, site_depth=14000, bedrooms=3))
    sofa = next(f for f in spec.fixtures
                if isinstance(f, FixturePlacement) and f.name == "sofa3")
    ox, oy = sofa.insert
    sofa.insert = (ox - 300, oy)                       # 往西推 300:形心仍在房內、
    wall_cols = [c for c in find_collisions(collect_active(spec))  # 但背面穿西牆
                 if c.b.kind == WALL]
    assert wall_cols                                   # 抓到穿牆
    CollisionEngine(spec).resolve()
    after = [c for c in find_collisions(collect_active(spec)) if c.b.kind == WALL]
    assert after == []                                 # 修回房內,不再穿牆


def test_table4_uses_tightened_collision_footprint():
    """table4 的碰撞 footprint 收緊(900),椅子區(1560)不作為穿牆依據——
    這是 fixture 資料修正,與牆演算法分離。"""
    from src.design.collision.geometry import fixture_obstacles
    spec = generate_floor_plan(
        HouseBrief(site_width=16000, site_depth=14000, bedrooms=3))
    t = next((o for o in fixture_obstacles(spec) if o.tag == "table4"), None)
    if t is not None:                                  # 該案例有方桌時才驗
        dw = t.poly.bounds[2] - t.poly.bounds[0]       # 畫圖 footprint 寬
        cw = t.collision_poly.bounds[2] - t.collision_poly.bounds[0]
        assert cw < dw                                 # 碰撞用的較小


# ── Phase 3-1:Void Collision(天井/挑空為硬障礙)────────────────────────────
def _patio_specs():
    """含天井/中庭的樓層(深基地 → 天井版)。"""
    return [
        generate_house_upper(
            HouseBrief(site_width=20000, site_depth=20000, bedrooms=3, seed=1)),
        generate_house_public(
            HouseBrief(site_width=26000, site_depth=22000, bedrooms=3, seed=1)),
    ]


def test_void_obstacles_are_patios():
    """天井障礙 = kind=patio 的房間,static(不可移動)。"""
    from src.design.collision.geometry import void_obstacles
    from src.design.collision.obstacle import VOID
    for spec in _patio_specs():
        voids = void_obstacles(spec)
        assert voids                                   # 天井版一定有天井
        assert all(v.kind == VOID and not v.movable for v in voids)
        patio_names = {r.name for r in spec.rooms if r.kind == "patio"}
        assert {v.tag for v in voids} == patio_names


def test_void_is_in_collect_active():
    """天井已納入作用中偵測集合(Phase 3-1 的接線點)。"""
    from src.design.collision.geometry import collect_active
    from src.design.collision.obstacle import VOID
    for spec in _patio_specs():
        assert any(o.kind == VOID for o in collect_active(spec))


def test_no_furniture_in_void_on_passing_specs():
    """核心 regression 保證:合格的天井版圖面,沒有任何家具掉進天井。"""
    from src.design.collision.detector import find_collisions
    from src.design.collision.geometry import collect_active
    from src.design.collision.obstacle import VOID
    for spec in _patio_specs():
        void_cols = [c for c in find_collisions(collect_active(spec))
                     if c.b.kind == VOID]
        assert void_cols == []


def test_furniture_in_void_detected_and_resolved():
    """注入一件掉進天井的家具 → detector 抓到,engine 修掉(移開或丟棄)。

    同時證明:**不需要新的 void resolver**——天井是硬障礙,已被 engine 放進
    blockers,現有 try_move / try_drop 直接處理。"""
    from shapely.geometry import Polygon
    from src.design.collision import CollisionEngine
    from src.design.collision.detector import find_collisions
    from src.design.collision.geometry import collect_active
    from src.design.collision.obstacle import VOID
    spec = _patio_specs()[0]
    patio = next(r for r in spec.rooms if r.kind == "patio")
    c = Polygon(patio.points).centroid
    spec.fixtures.append(FixturePlacement("coffee_table", (c.x, c.y), 0))

    before = [col for col in find_collisions(collect_active(spec))
              if col.b.kind == VOID]
    assert before                                      # 抓到「掉進天井」

    CollisionEngine(spec).resolve()
    after = [col for col in find_collisions(collect_active(spec))
             if col.b.kind == VOID]
    assert after == []                                 # 已修掉,天井淨空


# ── Phase 3-2:Stair Collision(梯段為硬障礙)────────────────────────────────
def _stair_specs():
    """含樓梯的樓層(透天各層;單層住宅沒有樓梯)。"""
    return [
        generate_house_upper(
            HouseBrief(site_width=26000, site_depth=16000, bedrooms=3, seed=1)),
        generate_house_public(
            HouseBrief(site_width=22000, site_depth=14000, bedrooms=3, seed=1)),
    ]


def test_stair_obstacles_are_bboxes():
    """樓梯障礙 = 依行進方向組出的軸對齊 bbox,static(不可移動)。"""
    from src.design.collision.geometry import stair_obstacles
    from src.design.collision.obstacle import STAIR
    for spec in _stair_specs():
        obs = stair_obstacles(spec)
        assert obs and len(obs) == len(spec.stairs)
        assert all(o.kind == STAIR and not o.movable for o in obs)
        for st, o in zip(spec.stairs, obs):
            ox, oy = st.origin
            w, d = ((st.width, st.length) if st.direction in ("north", "south")
                    else (st.length, st.width))
            for got, want in zip(o.poly.bounds, (ox, oy, ox + w, oy + d)):
                assert abs(got - want) < 1e-6


def test_stair_is_in_collect_active():
    """樓梯已納入作用中偵測集合(Phase 3-2 的接線點)。"""
    from src.design.collision.geometry import collect_active
    from src.design.collision.obstacle import STAIR
    for spec in _stair_specs():
        assert any(o.kind == STAIR for o in collect_active(spec))


def test_no_furniture_on_stair_on_passing_specs():
    """核心 regression 保證:合格圖面沒有任何家具壓在梯段上。"""
    from src.design.collision.detector import find_collisions
    from src.design.collision.geometry import collect_active
    from src.design.collision.obstacle import STAIR
    for spec in _stair_specs():
        stair_cols = [c for c in find_collisions(collect_active(spec))
                      if c.b.kind == STAIR]
        assert stair_cols == []


def test_furniture_on_stair_detected_and_resolved():
    """注入一件壓在梯段上的家具 → detector 抓到,engine 修掉(移開或丟棄)。

    與天井同樣證明:**不需要新的 Stair Resolver**——樓梯是硬障礙,已被 engine
    放進 blockers,現有 try_move / try_drop 直接處理。"""
    from src.design.collision import CollisionEngine
    from src.design.collision.detector import find_collisions
    from src.design.collision.geometry import collect_active, stair_obstacles
    from src.design.collision.obstacle import STAIR
    spec = _stair_specs()[0]
    c = stair_obstacles(spec)[0].poly.centroid
    spec.fixtures.append(FixturePlacement("coffee_table", (c.x, c.y), 0))

    before = [col for col in find_collisions(collect_active(spec))
              if col.b.kind == STAIR]
    assert before                                      # 抓到「壓住梯段」

    CollisionEngine(spec).resolve()
    after = [col for col in find_collisions(collect_active(spec))
             if col.b.kind == STAIR]
    assert after == []                                 # 已修掉,梯段淨空


# ── Phase 3-3:Column Detection Only(只偵測、不修復)──────────────────────
def test_column_obstacles_are_grid_columns():
    """柱障礙 = 軸網柱斷面,static;數量與 resolve_columns 一致。"""
    from src.design.collision.geometry import column_obstacles
    from src.design.collision.obstacle import COLUMN
    from src.drafting.apartment_plan import build_grid, resolve_columns
    for make in _SPECS:
        spec = make()
        obs = column_obstacles(spec)
        assert obs and len(obs) == len(resolve_columns(spec, build_grid(spec)))
        assert all(o.kind == COLUMN and not o.movable for o in obs)


def test_column_is_in_collect_active():
    """柱已納入作用中偵測集合(Phase 3-3 的接線點)。"""
    from src.design.collision.geometry import collect_active
    from src.design.collision.obstacle import COLUMN
    for make in _SPECS:
        assert any(o.kind == COLUMN for o in collect_active(make()))


def test_no_column_hits_on_passing_specs():
    """核心 regression 保證:合格圖面在 COLUMN_TOLERANCE_MM=300 下零壓柱——
    30% 家具貼牆壓到柱的室內半邊(≤190mm)全部合法,不得被判成碰撞。"""
    from src.design.collision.detector import find_collisions
    from src.design.collision.geometry import collect_active
    from src.design.collision.obstacle import COLUMN
    for make in _SPECS:
        hits = [c for c in find_collisions(collect_active(make()))
                if c.b.kind == COLUMN]
        assert hits == []


def test_column_contacts_reports_every_contact():
    """Collision Report:column_contacts 列出**所有**家具×柱接觸(含合法的),
    每筆有穿入深度/面積/是否超標;合格圖上應全部 over_tolerance=False。"""
    from src.design.collision import column_contacts
    seen = 0
    for make in _SPECS:
        for c in column_contacts(make()):
            seen += 1
            assert c["penetration_mm"] > 0
            assert c["area_mm2"] > 0
            assert len(c["column"]) == 2               # 柱心座標
            assert c["over_tolerance"] is False        # 貼牆合法接觸
            assert c["penetration_mm"] <= 190          # 柱半250 − 內牆半厚60
    assert seen > 0                                    # 確實有貼牆壓柱的家具


def test_column_detection_fires_when_tolerance_lowered():
    """把容差降到合法穿入之下,偵測就會抓到 → 證明判定確實在運作,
    而 COLUMN_TOLERANCE_MM=300 正是「讓合法貼牆穿入通過」的那條線。"""
    from src.design.collision.detector import find_collisions
    from src.design.collision.geometry import collect_active
    from src.design.collision.obstacle import COLUMN
    obs = collect_active(_SPECS[0]())
    assert [c for c in find_collisions(obs) if c.b.kind == COLUMN] == []
    loud = [c for c in find_collisions(obs, col_tol=50) if c.b.kind == COLUMN]
    assert loud                                        # 容差 50 → 抓到貼牆接觸


def test_engine_never_acts_on_column_contacts():
    """★ 需求 5:柱接觸真實存在(貼牆家具壓到柱的室內半邊),但 engine
    **一件都不動**——不 try_move、不 try_drop,純回報。"""
    from src.design.collision import CollisionEngine, column_contacts
    for make in _SPECS:
        spec = make()
        assert column_contacts(spec)                   # 確實有家具貼到柱
        before = [(f.name, f.insert) for f in spec.fixtures
                  if isinstance(f, FixturePlacement)]
        report = CollisionEngine(spec).resolve()
        after = [(f.name, f.insert) for f in spec.fixtures
                 if isinstance(f, FixturePlacement)]
        assert report.changed is False                 # 沒有移動/丟棄
        assert before == after                         # 逐字不變


# ── Phase 4:Column Resolver(超過容差才修,修不動就保留並標記)──────────────
def _lone_column_spec(centers_fn):
    """把客廳清空、換成指定的柱配置,並在客廳中心擺一件家具(正壓柱心)。

    用 column_centers 指定「獨立柱」——真實圖面的柱都藏在牆內(合法貼柱),
    唯有獨立柱才可能出現超過容差的壓柱,這裡用它建構可重現的情境。"""
    from shapely.geometry import Polygon
    spec = _SPECS[0]()
    room = next(r for r in spec.rooms if r.kind == "living")
    poly = Polygon(room.points)
    c = poly.centroid
    spec.column_centers = centers_fn(poly)
    spec.fixtures.clear()                              # 隔離:只留待測家具
    fx = FixturePlacement("coffee_table", (c.x, c.y), 0)
    spec.fixtures.append(fx)
    return spec, fx, (c.x, c.y)


def test_column_over_tolerance_is_moved_away():
    """超過容差的壓柱 → resolver 介入把家具移開,且不製造新的牆/天井/樓梯碰撞。"""
    from src.design.collision import CollisionEngine
    from src.design.collision.detector import find_collisions
    from src.design.collision.geometry import collect_active
    from src.design.collision.obstacle import COLUMN, STAIR, VOID, WALL
    spec, fx, origin = _lone_column_spec(lambda p: [(p.centroid.x, p.centroid.y)])

    before = [c for c in find_collisions(collect_active(spec))
              if c.b.kind == COLUMN]
    assert before                                      # 確實超標壓柱

    report = CollisionEngine(spec).resolve()
    after = find_collisions(collect_active(spec))
    assert [c for c in after if c.b.kind == COLUMN] == []     # 壓柱解除
    assert report.moved                                        # 是「移開」解決的
    assert fx in spec.fixtures                                 # 家具沒被丟掉
    assert fx.insert != origin                                 # 真的移動了
    # 需求 4:不得造成新的 Wall / Void / Stair 碰撞
    assert [c for c in after if c.b.kind in (WALL, VOID, STAIR)] == []


def test_unresolved_column_keeps_furniture_and_is_reported():
    """★ 需求 5:找不到合法位置時,家具**保留原位**(即使是可丟的裝飾),
    並在 Collision Report 標記 unresolved_column。"""
    from src.design.collision import CollisionEngine
    from src.design.collision.priority import is_droppable

    def dense(poly):                                   # 密集柱網鋪滿全房 → 無處可去
        minx, miny, maxx, maxy = poly.bounds
        return [(x, y)
                for x in range(int(minx), int(maxx), 400)
                for y in range(int(miny), int(maxy), 400)]

    spec, fx, origin = _lone_column_spec(dense)
    assert is_droppable("coffee_table")                # 前提:它「可丟」

    report = CollisionEngine(spec).resolve()
    assert report.unresolved_column                    # 有標記
    assert fx in spec.fixtures                         # ★ 仍保留,沒被丟掉
    assert fx.insert == origin                         # ★ 原位不動
    assert report.dropped == []                        # 柱碰撞不走 try_drop


def test_try_move_respects_column_guard():
    """try_move 直接支援避柱(不新增 Resolver 類別):壓柱的落點會被拒絕。"""
    from shapely.geometry import Polygon
    from src.design.collision.detector import penetration
    from src.design.collision.obstacle import FURNITURE, Obstacle
    from src.design.collision.resolver import try_move
    from src.drafting.fixtures import FixturePlacement as FP
    from src.drafting.fixtures import fixture_footprint

    room = Polygon([(0, 0), (6000, 0), (6000, 5000), (0, 5000)])
    col = Polygon([(2750, 2250), (3250, 2250), (3250, 2750), (2750, 2750)])
    fx = FP("coffee_table", (3000, 2500), 0)           # 正壓在柱心
    ob = Obstacle(poly=Polygon(fixture_footprint(fx)), kind=FURNITURE,
                  movable=True, ref=fx, tag="coffee_table", room=room)
    assert penetration(ob.poly, col) > 300             # 先確認真的超標壓柱
    assert try_move(ob, [], columns=[col]) is True
    assert penetration(ob.poly, col) <= 300            # 移完不再超標
    assert room.contains(ob.poly.centroid)             # 仍在房內
