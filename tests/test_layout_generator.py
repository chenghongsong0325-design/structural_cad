"""規則式格局產生器(C1)的單元測試。

核心保證:多組不同需求(單戶矩陣 × 集合住宅)都產出「通過 validate_spec
檢核」的完整設計,並端到端畫成圖——大量出圖、張張合格。
"""
from __future__ import annotations

import pytest

from src.design.layout_generator import (
    CorridorBrief,
    HouseBrief,
    generate_floor_plan,
    validate_spec,
)
from src.drafting.apartment_plan import draw_floor_plan
from src.drafting.fixtures import Counter, FixturePlacement
from src.standards.loader import apply_standard, load_standard, new_document

HOUSE_BRIEFS = [
    HouseBrief(site_width=12000, site_depth=11000, bedrooms=1),
    HouseBrief(site_width=12000, site_depth=11000, bedrooms=2),
    HouseBrief(site_width=14000, site_depth=12000, bedrooms=2),
    HouseBrief(site_width=16000, site_depth=14000, bedrooms=3),
    HouseBrief(site_width=18000, site_depth=13000, bedrooms=3),
    HouseBrief(site_width=20000, site_depth=13000, bedrooms=4),
    HouseBrief(site_width=22000, site_depth=15000, bedrooms=4),
]
CORRIDOR_BRIEFS = [
    CorridorBrief(units_per_row=2),
    CorridorBrief(units_per_row=4),
    CorridorBrief(units_per_row=6, corridor_width=2000),
]
ALL_BRIEFS = HOUSE_BRIEFS + CORRIDOR_BRIEFS


def _brief_id(b) -> str:
    if isinstance(b, HouseBrief):
        return f"house-{b.site_width//1000}x{b.site_depth//1000}-{b.bedrooms}房"
    return f"corridor-{b.units_per_row}戶"


# ---------------------------------------------------------------------------
# 1) 核心保證:每組需求 → 合格設計 → 能出圖
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("brief", ALL_BRIEFS, ids=_brief_id)
def test_generate_passes_validation(brief) -> None:
    spec = generate_floor_plan(brief)      # 內部已跑檢核,失敗會 raise
    assert validate_spec(spec) == []


@pytest.mark.parametrize("brief", [HOUSE_BRIEFS[3], CORRIDOR_BRIEFS[1]], ids=_brief_id)
def test_generate_draws_end_to_end(brief) -> None:
    spec = generate_floor_plan(brief)
    std = load_standard()
    doc = new_document()
    layers = apply_standard(doc, std)
    draw_floor_plan(doc.modelspace(), spec, layers)
    msp = doc.modelspace()
    assert len(list(msp)) > 100
    assert len(list(msp.query("DIMENSION"))) > 10   # 尺寸鏈
    assert len(list(msp.query("INSERT"))) > 10      # 門窗+家具+圖面配件


# ---------------------------------------------------------------------------
# 2) 單戶:設計內容隨需求變化
# ---------------------------------------------------------------------------
def test_bedroom_count_matches_brief() -> None:
    for n in (1, 2, 3, 4):
        spec = generate_floor_plan(HouseBrief(site_width=20000, site_depth=14000, bedrooms=n))
        assert len([r for r in spec.rooms if r.kind == "bedroom"]) == n


def test_master_bedroom_is_biggest() -> None:
    spec = generate_floor_plan(HouseBrief(site_width=16000, site_depth=14000, bedrooms=3))
    beds = {r.name: r.area_m2 for r in spec.rooms if r.kind == "bedroom"}
    assert beds["主臥室"] > beds["臥室A"]


def test_small_site_merges_dining() -> None:
    """餐廳分不到足夠的寬度就併成「客餐廳」。

    ⚠️ 2026-08-02 起判準多一條:**餐廳的南外牆開不出 §40 要的窗**(牆太短,或
    柱卡在牆中間把可開窗的段切碎)時也併——以前那種情形會生出一間採光不合格的
    餐廳,整張圖不合格。所以「基地大 → 一定有獨立餐廳」不再成立,要看那面牆。"""
    small = generate_floor_plan(HouseBrief(site_width=12000, site_depth=11000,
                                           bedrooms=2))
    assert "客餐廳" in {r.name for r in small.rooms}
    big = generate_floor_plan(HouseBrief(site_width=20000, site_depth=12000,
                                         bedrooms=3))
    assert "餐廳" in {r.name for r in big.rooms}        # 這個尺寸的南牆夠長

    # 併成客餐廳的那些尺寸,採光一定要合格(併的目的就是這個)
    from src.design.layout.code_check import check_code_building
    merged = generate_floor_plan(HouseBrief(site_width=20000, site_depth=14000,
                                            bedrooms=3))
    assert "客餐廳" in {r.name for r in merged.rooms}
    assert check_code_building([("1F", merged)]).ok


def test_house_fixtures_follow_rooms() -> None:
    """家具數量跟房型走:每臥室 床+衣櫃、兩套衛浴各 馬桶+洗手台、L 型流理台。"""
    spec = generate_floor_plan(HouseBrief(site_width=18000, site_depth=14000, bedrooms=3))
    fx = spec.fixtures
    names = [f.name for f in fx if isinstance(f, FixturePlacement)]
    assert names.count("bed_double") == 1
    assert names.count("bed_single") == 2
    assert names.count("wardrobe") == 3
    # ≥3 房有兩套衛浴(公用 + 主臥套房)。
    assert names.count("toilet") == 2 and names.count("basin") == 2
    assert names.count("sofa3") == 1
    counters = [f for f in fx if isinstance(f, Counter)]
    assert len(counters) == 2                      # L 型 = 兩段
    assert sum(1 for c in counters if c.sink) == 1


# ---------------------------------------------------------------------------
# 2b) C1.5a:法規/安全
# ---------------------------------------------------------------------------
def test_three_bedrooms_get_second_bathroom() -> None:
    """≥3 房自動加主臥套房衛浴;<3 房維持一套。"""
    spec3 = generate_floor_plan(HouseBrief(site_width=16000, site_depth=14000, bedrooms=3))
    baths3 = [r for r in spec3.rooms if r.kind == "bathroom"]
    assert len(baths3) == 2
    assert "主臥浴" in {r.name for r in baths3}

    spec2 = generate_floor_plan(HouseBrief(site_width=14000, site_depth=12000, bedrooms=2))
    assert len([r for r in spec2.rooms if r.kind == "bathroom"]) == 1


def test_master_is_L_shape_with_ensuite() -> None:
    spec = generate_floor_plan(HouseBrief(site_width=16000, site_depth=14000, bedrooms=3))
    master = next(r for r in spec.rooms if r.name == "主臥室")
    ensuite = next(r for r in spec.rooms if r.name == "主臥浴")
    assert len(master.points) == 6                 # L 形
    assert ensuite.area_m2 == pytest.approx(1.8 * 2.0)


def test_corridor_has_two_stairs_and_elevator() -> None:
    """集合住宅:兩端樓梯間(兩個逃生方向)+ 電梯,走廊縱貫全樓。"""
    spec = generate_floor_plan(CorridorBrief(units_per_row=4))
    stairs = [r for r in spec.rooms if r.kind == "stair"]
    assert len(stairs) == 2
    assert len(spec.stairs) == 2                   # 折返梯實體 ×2
    assert len(spec.elevators) == 1
    # 樓梯間分在建築兩端。
    xs = sorted(min(p[0] for p in r.points) for r in stairs)
    assert xs[0] < 6000 and xs[1] > 10000


def test_corridor_each_unit_has_outward_balcony() -> None:
    """每戶對外側各一座陽台:2n 座,且都落在建築南北外牆之外(採光/工作側)。"""
    spec = generate_floor_plan(CorridorBrief(units_per_row=4))
    assert len(spec.balconies) == 2 * 4
    y0 = spec.grid_origin[1]
    by1 = y0 + sum(spec.y_spacings)          # 建築北緣
    for b in spec.balconies:
        top = b.origin[1] + b.depth
        # 陽台不落在建築內部:整體在南牆(y0)以南,或北牆(by1)以北。
        assert top <= y0 + 1 or b.origin[1] >= by1 - 1
        # 且不超出地界線(仍在退縮帶內)。
        assert b.origin[1] >= 0 and top <= by1 + spec.setback


def test_unit_bathroom_marked_mech_vent() -> None:
    """單元浴廁無對外窗 → 標示機械排風(通過檢核的依據)。"""
    spec = generate_floor_plan(CorridorBrief(units_per_row=2))
    unit_baths = [r for r in spec.rooms if r.name == "浴廁"]
    assert len(unit_baths) == 4
    assert all("排風" in r.note for r in unit_baths)


def test_validate_flags_unvented_bathroom() -> None:
    """把排風標示拿掉 → 檢核要抓到「無窗且未標排風」。"""
    spec = generate_floor_plan(CorridorBrief(units_per_row=2))
    for r in spec.rooms:
        if r.name == "浴廁":
            r.note = ""
    problems = validate_spec(spec)
    assert any("排風" in p for p in problems)


def test_validate_flags_missing_escape_stairs() -> None:
    """把樓梯間改名成儲藏 → 檢核要抓到「逃生需 ≥2 樓梯」。"""
    spec = generate_floor_plan(CorridorBrief(units_per_row=2))
    for r in spec.rooms:
        if r.kind == "stair":
            r.kind = "storage"
    problems = validate_spec(spec)
    assert any("逃生" in p for p in problems)


# ---------------------------------------------------------------------------
# 2c) C1.5b:格局動線 — 走道
# ---------------------------------------------------------------------------
def test_house_has_hallway_between_bands() -> None:
    """≥2 房的單戶有走道,每間臥室與走道共享 ≥ 門寬的邊。"""
    from shapely.geometry import Polygon

    spec = generate_floor_plan(HouseBrief(site_width=16000, site_depth=14000, bedrooms=3))
    hall = next(r for r in spec.rooms if r.kind == "corridor")
    assert hall.name == "走道"
    hp = Polygon(hall.points)
    beds = [r for r in spec.rooms if r.kind == "bedroom"]
    assert len(beds) == 3
    for b in beds:
        assert Polygon(b.points).intersection(hp).length >= 900


@pytest.mark.parametrize("w,d", [(18000, 13000), (16000, 14000), (19000, 13000)])
def test_bedroom_doors_open_to_public_circulation(w, d) -> None:
    """★★ 每間臥室都有一扇門直接開向**公共動線**。

    ⚠️ 舊版釘的是「門落在**走道**邊界上」——那是把載具當成目的。走道現在改成
    功能性判準(東臥進得了客餐廳就不設,見 `has_hall`),18×13 這種尺寸本來就
    沒有走道,測試就 StopIteration 掛掉,看起來像臥室沒門,其實是量錯東西。
    真正要守的是「臥室不必穿過別人的房間才出得來」,公共動線是走道**或**客餐廳。
    """
    from shapely.geometry import Point, Polygon

    PUBLIC = {"corridor", "living", "dining", "foyer"}
    spec = generate_floor_plan(HouseBrief(site_width=w, site_depth=d, bedrooms=3))
    pubs = [Polygon(r.points) for r in spec.rooms if r.kind in PUBLIC]
    assert pubs, "這層沒有任何公共空間"
    bed_polys = [Polygon(r.points) for r in spec.rooms if r.kind == "bedroom"]
    served = set()
    for wall in spec.walls:
        for op in wall.openings:
            if op.kind != "door":
                continue
            pt = Point(wall.point_at(op.position))
            if not any(p.boundary.distance(pt) < 1.0 for p in pubs):
                continue
            for i, bp in enumerate(bed_polys):
                if bp.boundary.distance(pt) < 1.0:
                    served.add(i)
    assert served == set(range(len(bed_polys))), (
        f"這幾間臥室的門沒開向公共動線:"
        f"{sorted(set(range(len(bed_polys))) - served)}")


def test_hallway_open_passage_to_living() -> None:
    """走道南牆要留通道口(≥1.4m 缺口)連通客廳,不是整條封死。"""
    spec = generate_floor_plan(HouseBrief(site_width=16000, site_depth=14000, bedrooms=3))
    hall = next(r for r in spec.rooms if r.kind == "corridor")
    y_lo = min(p[1] for p in hall.points)
    x_lo = min(p[0] for p in hall.points)
    x_hi = max(p[0] for p in hall.points)
    covered = sum(
        abs(w.end[0] - w.start[0]) for w in spec.walls
        if w.start[1] == w.end[1] == y_lo)
    assert covered > 0                              # 牆存在(不是整條開放)
    assert (x_hi - x_lo) - covered >= 1400          # 通道口


def test_one_bedroom_house_has_no_hallway() -> None:
    """1 房戶不加走道(只服務一扇門太浪費),臥室門維持直開。"""
    spec = generate_floor_plan(HouseBrief(site_width=12000, site_depth=11000, bedrooms=1))
    assert not any(r.kind == "corridor" for r in spec.rooms)


def test_two_bedroom_hallway_only_when_needed() -> None:
    """2 房動線融入客餐廳、預設不設走道;小基地上東臥門被服務核+柱位
    擠到開不進客餐廳時,才退回設走道。

    ⚠️ 2026-08-10 起外牆要替柱留位置(STRUCT_MARGIN),建築每邊縮 27.5cm,
       「不必設走道」的門檻跟著往上平移約 0.5m(14.0m → 14.5m)。這裡原本釘
       14×12 剛好卡在舊門檻上,改成 15×12 讓它明確落在門檻的另一邊 ——
       釘的是「大基地不設走道、小基地才設」這個意圖,不是那個特定尺寸。
    """
    big = generate_floor_plan(HouseBrief(site_width=15000, site_depth=12000, bedrooms=2))
    assert not any(r.kind == "corridor" for r in big.rooms)
    small = generate_floor_plan(HouseBrief(site_width=12000, site_depth=11000, bedrooms=2))
    assert any(r.kind == "corridor" for r in small.rooms)


def test_validate_flags_bedroom_door_into_kitchen() -> None:
    """把 2 房東臥的門移到廚房正上方 → 檢核要抓到「門開進廚房」。"""
    spec = generate_floor_plan(HouseBrief(site_width=14000, site_depth=12000, bedrooms=2))
    kitchen = next(r for r in spec.rooms if r.kind == "kitchen")
    kx = min(p[0] for p in kitchen.points)          # 服務核西緣
    band_wall = spec.walls[4]                        # 帶分界牆
    band_wall.openings[-1].position = kx - band_wall.start[0] + 800
    problems = validate_spec(spec)
    assert any("開進廚房" in p for p in problems)


def test_validate_flags_bedroom_door_not_to_hallway() -> None:
    """把走道往西縮一半 → 檢核要抓到東側臥室「門未通走道」。"""
    spec = generate_floor_plan(HouseBrief(site_width=16000, site_depth=14000, bedrooms=3))
    hall = next(r for r in spec.rooms if r.kind == "corridor")
    x_mid = (min(p[0] for p in hall.points) + max(p[0] for p in hall.points)) / 2
    hall.points = [(min(p[0], x_mid), p[1]) for p in hall.points]
    problems = validate_spec(spec)
    assert any("未通走道" in p for p in problems)


def test_house_has_foyer_at_entry() -> None:
    """每個單戶都有玄關落塵區:大門開進玄關、玄關開放連通客廳。"""
    from shapely.geometry import Point, Polygon

    for brief in HOUSE_BRIEFS:
        spec = generate_floor_plan(brief)
        foyer = next(r for r in spec.rooms if r.kind == "foyer")
        fp = Polygon(foyer.points)
        # 大門(南牆第一個門洞)落在玄關邊界上。
        south = spec.walls[0]
        entry = next(op for op in south.openings if op.kind == "door")
        assert fp.boundary.distance(Point(south.point_at(entry.position))) < 1.0, \
            _brief_id(brief)
        # 玄關與客廳共享一段開放邊(≥0.7m 通行)。
        living = next(r for r in spec.rooms if r.kind == "living")
        assert Polygon(living.points).intersection(fp).length >= 700, _brief_id(brief)


def test_validate_flags_foyer_without_door() -> None:
    """把玄關搬離大門 → 檢核要抓到「玄關沒貼門」。"""
    spec = generate_floor_plan(HouseBrief(site_width=16000, site_depth=14000, bedrooms=3))
    foyer = next(r for r in spec.rooms if r.kind == "foyer")
    foyer.points = [(x, y + 800) for x, y in foyer.points]   # 往北平移離開南牆
    problems = validate_spec(spec)
    assert any("玄關" in p for p in problems)


# ---------------------------------------------------------------------------
# 2d) C1.5c:機能/舒適 — 採光、家具碰撞、隔間坐樑
# ---------------------------------------------------------------------------
def test_grid_aligns_with_partition_wall() -> None:
    """中間軸線距主要隔牆 ≤0.9m 時挪到牆位——柱站在隔牆交點、被牆包住。

    14×12 兩房:W=10m 等分 2 跨(中線 x=7000),主臥/臥A 隔牆在 x=7745,
    差 745 → 軸線挪到 7745;跨距 5745/4255(max/min=1.35,仍規則)。
    """
    spec = generate_floor_plan(HouseBrief(site_width=14000, site_depth=12000, bedrooms=2))
    beds = {r.name: r for r in spec.rooms if r.kind == "bedroom"}
    wall_x = min(p[0] for p in beds["臥室A"].points)
    grid_line = spec.grid_origin[0] + spec.x_spacings[0]
    assert grid_line == pytest.approx(wall_x)              # 軸線 = 隔牆
    assert max(spec.x_spacings) / min(spec.x_spacings) <= 1.6


def test_no_orphan_column_at_bedroom_door() -> None:
    """16×14 三房:中間軸線挪到主臥隔牆(4.8m+7.2m)——修使用者截圖抓到的
    「孤柱凸在走道牆上、貼著臥室門」問題;柱藏進隔牆交點。"""
    spec = generate_floor_plan(HouseBrief(site_width=16000, site_depth=14000, bedrooms=3))
    master = next(r for r in spec.rooms if r.name == "主臥室")
    wall_x = max(p[0] for p in master.points)          # 主臥/臥A 隔牆
    assert spec.grid_origin[0] + spec.x_spacings[0] == pytest.approx(wall_x)


def test_grid_regular_and_every_axis_on_a_wall() -> None:
    """22×15 四房:柱網要「同時」規則且零孤柱——每條中間軸線都坐在隔牆上。

    這裡本來只能二選一:等分起手法碰到「牆每 4.1m 一道、跨距想要 5.5m」時
    對不上,只能留一根孤柱列(柱凸在房間邊)換取跨距規則。_plan_x_grid 加了
    「反過來從隔牆裡挑軸線」之後兩者可以兼得(F3),故本測試改成兩項都驗。
    """
    spec = generate_floor_plan(HouseBrief(site_width=22000, site_depth=15000, bedrooms=4))
    xs = spec.x_spacings
    assert 3000 <= min(xs) and max(xs) <= 9000
    assert max(xs) / min(xs) <= 1.6                    # 跨距規則
    axes, wall_xs = [spec.grid_origin[0]], {
        w.start[0] for w in spec.walls if w.start[0] == w.end[0]}
    for s in xs:
        axes.append(axes[-1] + s)
    for a in axes[1:-1]:                               # 零孤柱:柱都藏進豎牆
        assert any(abs(a - wx) < 1 for wx in wall_xs), f"軸線 x={a} 上沒有豎牆"


def test_bay_spans_economic() -> None:
    """所有單戶案例:X 向跨距 3~9m、max/min ≤1.6(柱網規則、跨度經濟)。"""
    for brief in HOUSE_BRIEFS:
        spec = generate_floor_plan(brief)
        xs = spec.x_spacings
        assert 3000 <= min(xs) and max(xs) <= 9000, _brief_id(brief)
        assert max(xs) / min(xs) <= 1.6, _brief_id(brief)


def test_deep_living_gets_south_window() -> None:
    """客廳寬 >6m(只靠西窗採光深度超標)→ 南牆自動補窗。"""
    spec = generate_floor_plan(HouseBrief(site_width=22000, site_depth=15000, bedrooms=4))
    living = next(r for r in spec.rooms if r.kind == "living")
    lx0 = min(p[0] for p in living.points)
    lx1 = max(p[0] for p in living.points)
    assert lx1 - lx0 > 6000                        # 前提:客廳真的很寬
    south = spec.walls[0]
    wins_in_living = [
        op for op in south.openings
        if op.kind == "window" and lx0 <= south.start[0] + op.position <= lx1]
    assert len(wins_in_living) >= 1


def test_validate_flags_daylight_too_deep() -> None:
    """拿掉寬客廳的南窗 → 檢核要抓到「採光深度超過上限」。"""
    spec = generate_floor_plan(HouseBrief(site_width=22000, site_depth=15000, bedrooms=4))
    spec.windows = [wp for wp in spec.windows if wp.wall_index != 0]
    spec.walls[0].openings = [op for op in spec.walls[0].openings
                              if op.kind != "window"]
    problems = validate_spec(spec)
    assert any("採光深度" in p for p in problems)


def test_validate_flags_furniture_blocking_door() -> None:
    """在臥室門的迴轉方塊裡塞一個衣櫃 → 檢核要抓到「擋住門」。"""
    spec = generate_floor_plan(HouseBrief(site_width=16000, site_depth=14000, bedrooms=3))
    dp = next(d for d in spec.doors if d.wall_index == 4)
    w = spec.walls[4]
    x, y = w.point_at(w.openings[dp.opening_index].position)
    spec.fixtures.append(FixturePlacement("wardrobe", (x, y + 75), 0))
    problems = validate_spec(spec)
    assert any("擋住門" in p for p in problems)


def test_validate_flags_overlapping_furniture() -> None:
    """把一件家具原地複製一份 → 檢核要抓到「家具重疊」。"""
    spec = generate_floor_plan(HouseBrief(site_width=16000, site_depth=14000, bedrooms=3))
    fx = next(f for f in spec.fixtures if isinstance(f, FixturePlacement))
    spec.fixtures.append(FixturePlacement(fx.name, fx.insert, fx.rotation))
    problems = validate_spec(spec)
    assert any("家具重疊" in p for p in problems)


def test_house_rooms_have_codes() -> None:
    spec = generate_floor_plan(HouseBrief(site_width=16000, site_depth=14000, bedrooms=3))
    assert all(r.code for r in spec.rooms)
    assert {r.code for r in spec.rooms if r.kind == "bedroom"} == {"X05"}


# ---------------------------------------------------------------------------
# 2e) C2:方位約束(主臥角落/廚房方位,整張圖鏡射)
# ---------------------------------------------------------------------------
def _quadrant(spec, room) -> str:
    """房間形心落在建築的哪個象限,回傳如 "SW"。"""
    from shapely.geometry import Polygon

    bx0, by0 = spec.grid_origin
    mx = bx0 + sum(spec.x_spacings) / 2
    my = by0 + sum(spec.y_spacings) / 2
    c = Polygon(room.points).centroid
    return ("N" if c.y > my else "S") + ("E" if c.x > mx else "W")


@pytest.mark.parametrize("corner", ["NW", "NE", "SW", "SE"])
def test_master_corner_all_four(corner) -> None:
    """四個角落都要:主臥真的落在指定象限,且整張圖通過全部檢核。"""
    spec = generate_floor_plan(HouseBrief(
        site_width=16000, site_depth=14000, bedrooms=3, master_corner=corner))
    master = next(r for r in spec.rooms if r.name == "主臥室")
    assert _quadrant(spec, master) == corner
    assert validate_spec(spec) == []


def test_kitchen_side_north_and_west() -> None:
    spec = generate_floor_plan(HouseBrief(
        site_width=16000, site_depth=14000, bedrooms=3, kitchen_side="N"))
    kitchen = next(r for r in spec.rooms if r.kind == "kitchen")
    assert "N" in _quadrant(spec, kitchen)

    spec = generate_floor_plan(HouseBrief(
        site_width=16000, site_depth=14000, bedrooms=3, kitchen_side="W"))
    kitchen = next(r for r in spec.rooms if r.kind == "kitchen")
    assert "W" in _quadrant(spec, kitchen)


def test_roadmap_sentence_combo() -> None:
    """「主臥要在西南角,廚房靠北」——兩個約束同時滿足(同一次上下翻)。"""
    spec = generate_floor_plan(HouseBrief(
        site_width=16000, site_depth=14000, bedrooms=3,
        master_corner="SW", kitchen_side="N"))
    master = next(r for r in spec.rooms if r.name == "主臥室")
    kitchen = next(r for r in spec.rooms if r.kind == "kitchen")
    assert _quadrant(spec, master) == "SW"
    assert "N" in _quadrant(spec, kitchen)


def test_conflicting_constraints_raise() -> None:
    """主臥 NW(臥室帶佔北)+ 廚房靠北 → 不可能,要報「衝突」。"""
    with pytest.raises(ValueError, match="衝突"):
        generate_floor_plan(HouseBrief(
            site_width=16000, site_depth=14000,
            master_corner="NW", kitchen_side="N"))


def test_invalid_corner_value_raises() -> None:
    with pytest.raises(ValueError, match="master_corner"):
        generate_floor_plan(HouseBrief(
            site_width=16000, site_depth=14000, master_corner="中間"))


def test_mirrored_spec_draws_end_to_end() -> None:
    """鏡射後的圖照樣能端到端畫出來(門弧/家具圖塊/尺寸鏈)。"""
    spec = generate_floor_plan(HouseBrief(
        site_width=16000, site_depth=14000, bedrooms=3,
        master_corner="SE"))
    doc = new_document()
    layers = apply_standard(doc, load_standard())
    draw_floor_plan(doc.modelspace(), spec, layers)
    assert len(list(doc.modelspace())) > 100


# ---------------------------------------------------------------------------
# 3) 集合住宅
# ---------------------------------------------------------------------------
def test_corridor_unit_count_scales() -> None:
    for n in (2, 4, 6):
        spec = generate_floor_plan(CorridorBrief(units_per_row=n))
        assert len([r for r in spec.rooms if r.name == "起居室"]) == 2 * n
        # 每戶 入口+陽台落地門+浴廁門 ×2n,加上核區 4 扇(兩梯間+兩儲藏)。
        assert len(spec.doors) == 6 * n + 4


def test_corridor_area_closure() -> None:
    spec = generate_floor_plan(CorridorBrief(units_per_row=4))
    building = (2 * 3.1 + 4 * 4.0) * 13.8          # 含兩端核開間
    assert sum(r.area_m2 for r in spec.rooms) == pytest.approx(building)


# ---------------------------------------------------------------------------
# 3b) C2/A2:同排混合房型(套房 + 一房一廳,寬窄戶並存,柱藏分戶牆)
# ---------------------------------------------------------------------------
def test_corridor_mixed_unit_types() -> None:
    from src.drafting.unit import one_bed_unit, one_room_unit

    spec = generate_floor_plan(CorridorBrief(
        units=[one_room_unit(), one_bed_unit(), one_room_unit()]))
    assert validate_spec(spec) == []
    # 兩排各 [套房, 一房, 套房] → 套房 4 戶(起居室×4)、一房 2 戶(客廳/臥室×2)。
    assert len([r for r in spec.rooms if r.name == "起居室"]) == 4
    assert len([r for r in spec.rooms if r.name == "客廳"]) == 2
    assert len([r for r in spec.rooms if r.name == "臥室"]) == 2
    # 柱跨寬窄並存;一房是單一 6m 柱跨(中間不切柱 → 無孤柱)。
    spans = sorted({round(x) for x in spec.x_spacings})
    assert spans == [3100, 4000, 6000]
    assert 6000 in [round(x) for x in spec.x_spacings]


def test_mixed_wide_unit_has_no_interior_column() -> None:
    """一房一廳(6m)整跨無內部軸線 → 柱只落在其東西分戶牆,不生孤柱。"""
    from src.design.layout_generator import build_grid, resolve_columns
    from src.drafting.unit import one_bed_unit, one_room_unit

    spec = generate_floor_plan(CorridorBrief(
        units=[one_room_unit(), one_bed_unit(), one_room_unit()]))
    grid = build_grid(spec)
    xs = sorted(a.position for a in grid.x_axes)
    # 一房一廳跨:第 2 條軸線(核+套房後)到第 3 條,寬 6m,中間無軸線。
    gaps = [xs[i + 1] - xs[i] for i in range(len(xs) - 1)]
    assert 6000 in [round(g) for g in gaps]           # 存在 6m 整跨
    # 該 6m 跨區間內不得有其他軸線(否則柱會切進房內)。
    lo = xs[[round(g) for g in gaps].index(6000)]
    assert not any(lo + 1 < x < lo + 5999 for x in xs)


def test_corridor_mixed_depth_mismatch_raises() -> None:
    """同一排各房型深度須相同(等深帶),不同就報錯。"""
    from src.drafting.unit import UnitSpec, one_room_unit

    shallow = UnitSpec(name="淺戶", width=4000, depth=5000)   # 深度與套房 6000 不同
    with pytest.raises(ValueError, match="深度"):
        generate_floor_plan(CorridorBrief(units=[one_room_unit(), shallow]))


# ---------------------------------------------------------------------------
# 4) 不合理需求要報清楚的錯
# ---------------------------------------------------------------------------
def test_too_small_site_raises() -> None:
    with pytest.raises(ValueError):
        generate_floor_plan(HouseBrief(site_width=9000, site_depth=8000, bedrooms=2))


def test_too_many_bedrooms_raises() -> None:
    with pytest.raises(ValueError):
        generate_floor_plan(HouseBrief(site_width=13000, site_depth=12000, bedrooms=4))


def test_invalid_bedroom_count_raises() -> None:
    with pytest.raises(ValueError):
        generate_floor_plan(HouseBrief(site_width=16000, site_depth=14000, bedrooms=5))


def test_invalid_units_per_row_raises() -> None:
    with pytest.raises(ValueError):
        generate_floor_plan(CorridorBrief(units_per_row=1))


def test_unknown_brief_type_raises() -> None:
    with pytest.raises(TypeError):
        generate_floor_plan("三房兩廳")   # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 5) 確定性:同需求 → 同設計
# ---------------------------------------------------------------------------
def test_same_brief_same_design() -> None:
    brief = HouseBrief(site_width=16000, site_depth=14000, bedrooms=3)
    a = generate_floor_plan(brief)
    b = generate_floor_plan(brief)
    assert [r.points for r in a.rooms] == [r.points for r in b.rooms]
    assert [(w.start, w.end, w.thickness) for w in a.walls] == \
           [(w.start, w.end, w.thickness) for w in b.walls]


# ---------------------------------------------------------------------------
# 6) 樓梯:梯段兩側一定要有牆(所有產線共用的硬規則)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("brief", ALL_BRIEFS, ids=_brief_id)
def test_stair_flight_is_walled_on_both_sides(brief) -> None:
    """★ 梯段兩側都要碰到牆 —— 一側懸空,人走上去會從旁邊掉下去。

    集合住宅的折返梯原本只有 2500 寬、擺在 3100 的梯間正中,兩側各留 225mm 的空
    ——那不是走道,是空的。判準與窄透天/AI 產線共用同一支 _side_is_walled。"""
    from src.design.layout.narrow_house import _flight_sides, _side_is_walled

    spec = generate_floor_plan(brief)
    for st in getattr(spec, "stairs", None) or []:
        sides, _v = _flight_sides(st)
        assert len(sides) == 2
        for seg in sides:
            assert _side_is_walled(spec, seg), (_brief_id(brief), seg)


# ── 房間數要跟著面積長(不是把同樣幾間一起撐大)────────────────────────────
def _floor_rooms(w_m, d_m, bedrooms=3, floors=3):
    from src.design.building_generator import BuildingBrief, generate_building_auto
    b = generate_building_auto(BuildingBrief(
        typical=HouseBrief(site_width=w_m * 1000, site_depth=d_m * 1000,
                           bedrooms=bedrooms, setback=0, seed=0),
        floors=floors, differentiated=floors > 1))
    return [(f.spec.floor_label, f.spec) for f in b.floors]


def test_big_north_band_splits_into_several_rooms():
    """★★ 大基地的 1F 北帶西段要切成**好幾間**,不是切一刀變兩間大房。

    ⚠️ 舊版 `_west_zone_cut` 只切一刀:19×13m 實測切完是書房 32.9㎡ + 餐廚 34.1㎡,
    兩邊都還是大得離譜——「不讓一間無限長大」變成「讓兩間一起無限長大」。"""
    from src.design.room_program import requirement
    _lb, spec = _floor_rooms(19, 13)[0]
    west = [r for r in spec.rooms if r.kind in ("study", "family", "storage")
            and max(p[1] for p in r.points) > spec.rooms[0].points[0][1]]
    assert len(west) >= 2, f"北帶西段只切出 {len(west)} 間:{[r.name for r in west]}"
    for r in spec.rooms:
        if r.kind != "study":
            continue
        cap = requirement("study").max_area
        assert r.area_m2 <= cap * 1.5, f"{r.name} {r.area_m2:.1f}㎡ 仍然過大"


def test_west_zone_split_never_costs_a_generatable_plan():
    """★★ 多切幾刀不得讓原本生得出來的案子生不出來(能力不可倒退)。

    切點會被柱擋住(柱剛好站在那一格的北牆正中,窗與門都塞不進去)——
    這時要**少切一刀**,不是整份設計失敗。"""
    ok = 0
    for w, d, n in [(12, 11, 1), (14, 12, 2), (16, 14, 3), (18, 13, 3),
                    (20, 13, 4), (22, 15, 4), (26, 16, 4), (24, 16, 4)]:
        spec = generate_floor_plan(HouseBrief(site_width=w * 1000,
                                              site_depth=d * 1000, bedrooms=n))
        validate_spec(spec)
        ok += 1
    assert ok == 8


# ── 走道:功能性判準,不是「房數 ≥3 就給」──────────────────────────────
def test_corridor_only_when_east_bedroom_cannot_reach_public():
    """★★ 走道是**東側臥室進不了客餐廳**時才設,不是房間數多就設。

    使用者 2026-08-12:「走道是真的很大的房子才會需要走道」;
    2026-07-12:「小宅動線融入客廳,不要硬塞走廊」。
    19×13m 三房的東臥門開得進客餐廳 → 不該有走道。"""
    spec = generate_floor_plan(HouseBrief(site_width=19000, site_depth=13000,
                                          bedrooms=3))
    assert not [r for r in spec.rooms if r.kind == "corridor"],         "東臥門進得了客餐廳,不該再切一條走道"
    validate_spec(spec)


def test_corridor_still_appears_when_it_is_actually_needed():
    """★ 反面:東臥被服務核擠住、門開不進客餐廳時,走道要留著。

    拿掉走道不是目標,「沒必要就不要」才是——這條守住不能矯枉過正。"""
    spec = generate_floor_plan(HouseBrief(site_width=15000, site_depth=12000,
                                          bedrooms=3))
    assert [r for r in spec.rooms if r.kind == "corridor"],         "這個尺寸的東臥門開不進客餐廳,走道不能拿掉"


def test_every_bedroom_still_reaches_public_space_without_corridor():
    """★★ 拿掉走道之後,每間臥室仍然走得到公共空間(動線不能斷)。"""
    from src.design.layout.plan_check import check_floor
    for w, d, n in [(19, 13, 3), (20, 13, 4), (22, 15, 4), (18, 13, 3)]:
        spec = generate_floor_plan(HouseBrief(site_width=w * 1000,
                                              site_depth=d * 1000, bedrooms=n))
        bad = [i for i in check_floor(spec) if i.severity == "error"]
        assert not bad, f"{w}×{d} {n}房:{[(i.code, i.room) for i in bad]}"


# ── 地下車庫:替外牆柱留位置不得害車停不進去 ───────────────────────────────
def test_basement_garage_keeps_room_for_a_car():
    """★★ 有汽車位時,南帶(車庫就在它正下方)一定要有一個車位的長度。

    ⚠️ 踩過的坑:替外牆柱留位置(`STRUCT_MARGIN`)把建築縮小之後,20×20m 基地的
    南帶掉到 3.9m,B1F 直接報「車庫進深太淺」整份設計失敗 —— 而旁邊還有 6m 的
    院子沒用到。專案鐵則是「留柱位不得讓原本生得出來的案子生不出來」,當時只
    驗了地上層,漏了地下車庫。院子少 0.9m 換車停得進去,不用選。"""
    from src.design.building_generator import BuildingBrief, generate_building_auto
    from src.design.layout.code_check import check_code_building
    from src.design.layout.plan_check import check_building
    from src.design.layout_generator import GARAGE_MIN_DEPTH, _house_frame

    for w, d, cars in [(20, 20, 2), (20, 18, 1), (22, 20, 2)]:
        brief = HouseBrief(site_width=w * 1000, site_depth=d * 1000,
                           bedrooms=3, car_spaces=cars)
        f = _house_frame(brief)
        assert f.yn - f.by0 >= GARAGE_MIN_DEPTH - 1, (
            f"{w}×{d} {cars}車位:車庫只有 {(f.yn-f.by0)/1000:.1f}m")
        bld = generate_building_auto(BuildingBrief(
            typical=brief, floors=3, basements=1, differentiated=True))
        floors = [(fl.label, fl.spec) for fl in bld.floors]
        assert "B1F" in [lb for lb, _ in floors]
        assert check_building(floors).ok
        assert check_code_building(floors).ok


def test_garage_depth_never_eats_the_bedroom_band():
    """★ 讓給車庫的只能是**院子**的餘量,不能吃臥室帶(那會讓臥室變窄)。"""
    from src.design.layout_generator import _house_frame

    for w, d in [(20, 20), (22, 20), (20, 18)]:
        plain = _house_frame(HouseBrief(site_width=w * 1000, site_depth=d * 1000,
                                        bedrooms=3))
        car = _house_frame(HouseBrief(site_width=w * 1000, site_depth=d * 1000,
                                      bedrooms=3, car_spaces=2))
        assert car.dn == pytest.approx(plain.dn), f"{w}×{d} 臥室帶被吃掉了"
        assert car.W == pytest.approx(plain.W)


# ── 北帶切完,通道還要開得出來 ────────────────────────────────────────────
def test_dining_keeps_room_for_the_passage_to_the_living():
    """★★ 北帶切完,餐廳/餐廚對客廳的**寬通道**還要開得出來。

    ⚠️ 踩過的坑:切完只剩 1.186m 的乾淨牆,房門(0.75)過得了、通道(1.2)差
    **14mm** 過不了 → 整份設計 raise。驗切點時只驗門寬會漏掉這一格。
    13.9×15.3m/2房/seed203 是實際掃到的那一案。"""
    from src.design.building_generator import BuildingBrief, generate_building_auto

    for w, d, n, seed in [(13.9, 15.3, 2, 203), (15, 12, 3, 0), (19, 13, 3, 0)]:
        generate_building_auto(BuildingBrief(
            typical=HouseBrief(site_width=w * 1000, site_depth=d * 1000,
                               bedrooms=n, setback=0, seed=seed),
            floors=3, differentiated=True))


# ── 樓梯:門一開要站得住人(單跑直梯的盲點)────────────────────────────────
def test_stair_boxes_sees_a_straight_flight():
    """★★ 取「踏step 佔的範圍」時,單跑直梯(Stair)也要看得見。

    ⚠️ 踩過的坑:`_stair_boxes` 只讀 `flight_run` —— 那是**折返梯**(UStair)才有的
    欄位。兩帶式/集合住宅用的是單跑直梯,沒有這個欄位 → AttributeError → 整座
    樓梯被當成不存在 → 「門不得開在階梯上」「梯段不得一側沒牆」這兩條硬規則
    **從來沒對那兩條產線生效過**。"""
    from types import SimpleNamespace

    from src.design.layout.narrow_house import _stair_boxes
    from src.drafting.stair import Stair

    st = Stair(origin=(1000.0, 2000.0), width=2500.0, length=4000.0,
               direction="north", steps=15, tread=250.0)
    assert not hasattr(st, "flight_run"), "直梯本來就沒有 flight_run(這正是坑)"
    boxes = _stair_boxes(SimpleNamespace(stairs=[st]))
    assert len(boxes) == 1, "單跑直梯被當成不存在了"
    x0, y0, x1, y1 = boxes[0].bounds
    assert (x0, y0) == (1000.0, 2000.0)
    assert y1 - y0 == pytest.approx(15 * 250.0)      # 踏step 從 origin 起算


@pytest.mark.parametrize("w,d,n,floors", [(19, 13, 3, 3), (15, 12, 3, 2),
                                          (22, 14, 3, 3), (26, 16, 4, 3)])
def test_stairwell_door_has_a_place_to_stand(w, d, n, floors):
    """★★ 每一層的樓梯間門與第一階之間都要有起步平台(門一開站得住人)。

    ⚠️ 實測改版前**每一層**都只有 150mm(一個牆縫)——「一開門就踩上踏step」。"""
    from shapely.geometry import Point

    from src.design.building_generator import BuildingBrief, generate_building_auto
    from src.design.layout.narrow_house import STAIR_DOOR_CLEAR, _stair_boxes

    bld = generate_building_auto(BuildingBrief(
        typical=HouseBrief(site_width=w * 1000, site_depth=d * 1000,
                           bedrooms=n, setback=0, seed=0),
        floors=floors, differentiated=True))
    seen = 0
    for fl in bld.floors:
        spec = fl.spec
        boxes = _stair_boxes(spec)
        assert boxes, f"{fl.label} 看不到樓梯(又踩到 flight_run 的坑?)"
        for wall in spec.walls:
            for op in wall.openings:
                if op.kind != "door":
                    continue
                p = Point(*wall.point_at(op.position))
                for bx in boxes:
                    if bx.distance(p) > 3000:
                        continue                     # 不是樓梯間那扇門
                    seen += 1
                    assert bx.distance(p) >= STAIR_DOOR_CLEAR - wall.thickness / 2, (
                        f"{fl.label} 門({p.x:.0f},{p.y:.0f}) 離第一階只有 "
                        f"{bx.distance(p):.0f}mm")
    assert seen > 0, "沒量到任何樓梯間的門(測試自己失效了)"


@pytest.mark.parametrize("w,d,n", [(19, 13, 3), (15, 12, 3), (26, 16, 4)])
def test_stair_riser_and_tread_stay_legal_after_the_landing(w, d, n):
    """★★ 讓出起步平台之後,每階升高/踏面仍要合法。

    ⚠️ 這是讓平台時最容易錯的地方:梯跑變短,若還照舊「用長度回推級數」,
    級數會變少 → **每階升高超過法定上限**。級數要由**層高**決定。"""
    from src.design.layout.narrow_house import FLOOR_HEIGHT, MAX_RISER, MIN_TREAD
    from src.design.layout_generator import _house_frame, _house_stair

    st = _house_stair(_house_frame(HouseBrief(
        site_width=w * 1000, site_depth=d * 1000, bedrooms=n, setback=0)))
    assert FLOOR_HEIGHT / st.steps <= MAX_RISER + 1e-6, (
        f"每階升高 {FLOOR_HEIGHT / st.steps:.0f} > {MAX_RISER:.0f}")
    assert st.tread >= MIN_TREAD - 1e-6, f"踏面 {st.tread:.0f} < {MIN_TREAD:.0f}"
    assert st.steps * st.tread <= st.length + 1e-6   # 梯跑放得進樓梯間
