"""窄面寬透天產生器測試(v0.7 Phase N1a~N1c)。

驗證「窄深 envelope → 前後串聯 + 中段核(衛浴+樓梯)」的另一套骨架:

  * 7×12 米(舊兩帶式引擎直接拒絕的窄面寬)生得出來、畫得出 DXF、評得動分。
  * 房間前後串聯:1F 客廳/核/廚房;樓上 前臥/核/後臥+浴室。
  * 界牆(東西共用牆)不開窗;**不設天井**、也不切管道柱(5~7m 面寬切不划算)。
  * 多層共用垂直核 → 樓梯每層同位(上下對齊)。
  * 每層動線走得通(room_circulation ok)。
  * 面寬/進深超出定義域會擋。

⚠️ N1a~N1c 不含家具(N1d);柱另議。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from shapely.geometry import Polygon

from src.design.layout import narrow_house as nh
from src.design.layout.global_score import score_report
from src.design.layout.narrow_house import (
    MAX_WIDTH,
    _is_party_wall,
    generate_narrow_building,
    generate_narrow_house,
)
from src.design.layout.room_circulation import analyze_room_circulation
from src.drafting.apartment_plan import FloorPlanSpec, draw_floor_plan
from src.web.render import _new_doc

W, D = 7000.0, 12000.0        # 使用者的例子:建築 7×12 米(窄面寬)


# ── 基本產出:舊引擎拒絕的 7 米寬,這裡生得出來 ──────────────────────────────
def test_narrow_7x12_generates():
    """★ 7 米寬(<兩帶式的 10 米下限)——窄面寬骨架生得出來。"""
    spec = generate_narrow_house(W, D)
    assert isinstance(spec, FloorPlanSpec)
    assert spec.rooms and spec.walls and spec.doors


def test_dims_are_building_not_site():
    """建築物尺寸 → 基地=建築+四周退縮(反推)。"""
    spec = generate_narrow_house(W, D)
    xs = [p[0] for p in spec.site_boundary]
    ys = [p[1] for p in spec.site_boundary]
    assert max(xs) == W + 2 * spec.setback
    assert max(ys) == D + 2 * spec.setback


def test_1f_is_front_to_back_sequence():
    """★ 1F 前後串聯:客廳 → 中段核(浴廁+樓梯間)→ 餐廚。

    ⚠️ 後段從「餐廳|廚房兩間」改成**一間開放餐廚**(使用者 2026-08-26 給的
       參考平面圖上就是「餐廳/廚房」)。後段大到連併合上限都撐不住時才切兩間,
       那條由 `test_open_kitchen_diner_is_one_room` 與寬面寬的測試守。"""
    kinds = [r.kind for r in generate_narrow_house(W, D).rooms]
    assert kinds == ["living", "bathroom", "stair_hall", "dining"]


def test_no_storage_room_at_any_size():
    """★★ 住宅一律不設儲藏室(使用者 2026-07-30 定調):浴廁吃不完的那一小塊
    併進隔壁居室(變 L 形)或併進樓梯間,不隔成一間沒人用的小房。"""
    for bw in (3500.0, 5000.0, 7000.0):
        for bd in (10500.0, 12000.0, 16000.0):
            for lb, spec in generate_narrow_building(bw, bd, floors=3):
                assert not [r for r in spec.rooms if r.kind == "storage"], (
                    bw, bd, lb)


def test_1f_has_front_entry_door():
    """★ 臨路大門:1F 南向外牆有一扇門。

    ⚠️ 南向外牆的位置要由 **spec 自己推**,不是 `y == setback` —— 建築會替外牆柱
    留位置(`STRUCT_MARGIN`)而往內縮,寫死退縮線會一道南牆都找不到、空集合的
    `any()` 是 False,看起來像「大門不見了」其實是量錯地方。"""
    from src.design.layout.plan_check import building_env
    spec = generate_narrow_house(W, D)
    south_y = building_env(spec)[1]
    south = [w for w in spec.walls
             if abs(w.start[1] - south_y) < 50
             and abs(w.end[1] - south_y) < 50
             and abs(w.start[0] - w.end[0]) > 1]
    assert south, "找不到南向外牆"
    assert any(op.kind == "door" for w in south for op in w.openings)


def test_bathroom_has_single_door():
    """★ 浴室只留一扇門(不會兩個鄰室都開門)。"""
    from src.design.layout.room_circulation import _room_openings
    _, spec = generate_narrow_building(W, D, floors=2)[1]      # 2F
    for r in spec.rooms:
        if r.kind == "bathroom":
            assert len(_room_openings(spec, Polygon(r.points))) == 1


def test_no_pipe_shaft_column():
    """★ 窄透天不切管道柱(使用者 2026-07-29 決定)。

    5~7m 面寬多切一根管道柱,只會在西牆邊留一條 80cm×3.2m 的長條收納,礙眼又
    佔地;管道間留給 AI 版的核(見 test_graph_layout)。"""
    for bw in (5000.0, 6000.0, 7000.0):
        for label, spec in generate_narrow_building(bw, 12000.0, floors=3):
            assert not [r for r in spec.rooms if r.kind == "pipe_shaft"],                 (bw, label)


def test_draws_to_dxf_and_scores():
    spec = generate_narrow_house(W, D)
    doc, layers = _new_doc()
    draw_floor_plan(doc.modelspace(), spec, layers)
    assert len(list(doc.modelspace())) > 0
    rep = score_report(spec)
    assert 0.0 <= rep["overall_score"] <= 100.0
    assert len(rep["sub_scores"]) == 13


# ── 界牆無窗 / 不設天井 / 管道間尺寸 ─────────────────────────────────────────
def test_no_windows_on_party_walls():
    """★ 東西兩側是共用界牆,不該開窗。"""
    spec = generate_narrow_house(W, D)
    bx0 = spec.setback
    bx1 = spec.setback + W
    for wp in spec.windows:
        assert not _is_party_wall(spec.walls[wp.wall_index], bx0, bx1)


def test_no_light_well_at_any_size():
    """★ 住宅不設天井(使用者 2026-07-29 定調):任何尺寸都不能生出 patio。"""
    for bw in (5000.0, 6000.0, 7000.0):
        for bd in (11000.0, 14000.0, 18000.0):
            for label, spec in generate_narrow_building(bw, bd, floors=3):
                pat = [r for r in spec.rooms if r.kind == "patio"]
                assert not pat, (bw, bd, label)


# ── 多層 + 樓梯對齊 ─────────────────────────────────────────────────────────
def test_building_has_all_floors_with_stairs():
    floors = generate_narrow_building(W, D, floors=3)
    assert [lb for lb, _ in floors] == ["1F", "2F", "3F"]
    assert all(spec.stairs for _, spec in floors)


def test_stairs_aligned_across_floors():
    """★ 每層共用垂直核 → 樓梯同位(上下對齊,符合柱網原則)。"""
    floors = generate_narrow_building(W, D, floors=3)
    origins = {spec.stairs[0].origin for _, spec in floors}
    assert len(origins) == 1


def test_stair_riser_is_comfortable():
    """★ 樓梯不能太陡:U 形折返梯 → 每階升高在住宅正常範圍(≤190mm)。

    單跑直梯塞進 3.6m 深的中段核每階要升 ~246mm(太陡);折返梯分兩段爬,~178mm。"""
    from src.design.layout.narrow_house import FLOOR_HEIGHT
    _, spec = generate_narrow_building(W, D, floors=2)[0]
    st = spec.stairs[0]
    total = st.steps_per_flight * 2
    assert 150.0 <= FLOOR_HEIGHT / total <= 190.0


def test_stair_flight_is_walled_on_both_sides():
    """★ 梯段兩側都要碰到牆 —— 一側懸空,人走上去會從旁邊掉下去。

    梯段旁留了通道(人不必踩階梯走前後段),那條通道與梯段之間就必須有一道導牆;
    只有起步平台那一端可以開口(平地,而且要走進來)。"""
    from src.design.layout.narrow_house import _flight_sides, _side_is_walled

    for bw in (5000.0, 6000.0, 7000.0):
        for label, spec in generate_narrow_building(bw, 12000.0, floors=3):
            for st in spec.stairs:
                sides, _v = _flight_sides(st)
                assert len(sides) == 2
                for seg in sides:
                    assert _side_is_walled(spec, seg), (bw, label, seg)


def test_stair_guard_wall_does_not_seal_the_passage():
    """導牆補了之後,樓梯旁的通道仍要走得通(不能為了補牆把前後段封死)。"""
    from src.design.layout.narrow_house import GUARD_WALL_T

    for label, spec in generate_narrow_building(W, D, floors=3):
        guards = [w for w in spec.walls if getattr(w, "stair_guard", False)]
        assert guards, label                      # 這個骨架一定有側邊通道 → 一定要補
        assert all(w.thickness == GUARD_WALL_T and not w.openings
                   for w in guards)               # 導牆不開洞(開了就等於沒補)
        rep = analyze_room_circulation(spec)
        assert rep.ok, (label, [(r.name, r.reason) for r in rep.blocked])


def test_top_floor_stair_labeled_down():
    floors = generate_narrow_building(W, D, floors=3)
    assert floors[-1][1].stairs[0].label == "下"
    assert floors[0][1].stairs[0].label == "上"


def test_upper_floors_have_bedrooms_and_bath():
    _, spec = generate_narrow_building(W, D, floors=3)[1]      # 2F
    kinds = {r.kind for r in spec.rooms}
    assert "bedroom" in kinds and "bathroom" in kinds


# ── 每層動線走得通 ──────────────────────────────────────────────────────────
def test_every_floor_circulation_ok():
    """★ 前後串聯 + 樓梯間西側通道 → 每層都走得通。"""
    for label, spec in generate_narrow_building(W, D, floors=3):
        rep = analyze_room_circulation(spec)
        assert rep.ok, (label, [(r.name, r.reason) for r in rep.blocked])


def test_every_floor_draws():
    for _, spec in generate_narrow_building(W, D, floors=3):
        doc, layers = _new_doc()
        draw_floor_plan(doc.modelspace(), spec, layers)
        assert len(list(doc.modelspace())) > 0


# ── 定義域守門 ──────────────────────────────────────────────────────────────
def test_too_wide_is_rejected():
    with pytest.raises(ValueError):
        generate_narrow_house(MAX_WIDTH + 1000, D)             # >7m 該用兩帶式


def test_too_shallow_is_rejected():
    with pytest.raises(ValueError):
        generate_narrow_house(W, 6000)                         # 進深不足


def test_too_narrow_is_rejected():
    """★ 面寬下限:3.0m(<MIN_WIDTH)後段只剩不到 1㎡、前後段動線斷,該擋。"""
    with pytest.raises(ValueError):
        generate_narrow_house(3000, D)


def test_min_depth_depends_on_width():
    """★ 面寬越寬,後段房間越「寬而淺」(像走廊)→ 6m 以上的面寬要多留進深。

    窄的 3.5m 在 9.5m 進深就成立;7m 面寬同樣進深會生出長寬比 3.9 的後臥室。"""
    from src.design.layout.narrow_house import min_depth_for
    assert min_depth_for(3500) == 9500.0
    assert min_depth_for(7000) == 10500.0
    generate_narrow_house(3500, 9500)                      # 窄的可以
    with pytest.raises(ValueError):
        generate_narrow_house(7000, 9500)                  # 寬的不行


def test_deep_lot_caps_building_and_leaves_yards():
    """★★ 基地很深 → **建築封頂 + 前後留院**,不是硬蓋到底。

    硬蓋滿會生出單面採光的深房間,窗開好開滿也達不到 §40 的 1/8(以前 7×16 就是
    這樣默默生出違規圖)。封頂上限依面寬而定(窄面寬南牆短、窗開不大 → 更早封頂)。

    ⚠️ 上限要拿**建築**面寬去查,不是傳進去的那個數字 —— 產生器會先扣掉留給
       外牆柱的 `STRUCT_MARGIN`(5.0m 進來、實際蓋 4.45m 寬)。以前兩個寬度查到
       的上限剛好都是 13500,這個錯就一直藏著。"""
    from src.design.layout.narrow_house import max_depth_for
    from src.design.layout.plan_check import building_env
    spec = generate_narrow_house(5000, 18000)
    x0, y0, x1, y1 = building_env(spec)
    assert y1 - y0 <= max_depth_for(x1 - x0) + 1.0         # 建築封頂
    assert y1 - y0 < 18000 - 1000                          # 而且真的沒蓋滿
    assert y0 > spec.setback + 1000                        # 前面留了院子
    site_d = max(p[1] for p in spec.site_boundary)
    assert site_d == 18000 + 2 * spec.setback              # 基地仍是原尺寸


# ── 定義域全掃描:下限放寬(5×11 → 3.5×9.5)後,整個定義域仍要零錯誤零警告 ──
@pytest.mark.parametrize("bw", [3500.0, 4000.0, 5000.0, 6000.0, 7000.0, 8000.0])
@pytest.mark.parametrize("bd", [11000.0, 13000.0, 15000.0, 18000.0])
@pytest.mark.slow
def test_whole_domain_passes_both_gates(bw, bd):
    """★★ 定義域內每個尺寸都要過**兩道關卡**,而且不得有「太小」類的警告。

    下限是實測出來的、不是估的:再窄(3.0m)或再淺(9.0m)就會出臥室短邊不足、
    房間細長這類警告 —— 這條測試就是那條線的看門人,誰把常數再往下調就會紅。
    (room_oversize「房間過大」是另一個方向的設計警告,與下限無關,不在此列。)"""
    from src.design.layout.code_check import check_code_building
    from src.design.layout.plan_check import check_building
    from src.design.layout.narrow_house import MIN_DEPTH, MIN_WIDTH
    assert (MIN_WIDTH, MIN_DEPTH) == (3500.0, 9500.0)

    floors = generate_narrow_building(bw, bd, floors=3)
    plan = check_building(floors)            # 外框由 spec 自推(深基地封頂留院子)
    code = check_code_building(floors)
    assert plan.ok, [i.code for i in plan.errors]
    assert code.ok, [i.code for i in code.violations]
    too_small = {"bedroom_side", "bedroom_area", "room_skinny",
                 "room_no_daylight", "corridor_width"}
    got = {i.code for i in plan.warnings} | {i.code for i in code.warnings}
    assert not (got & too_small), sorted(got & too_small)


# ── 往真實街屋的參考平面靠(使用者 2026-08-26 給的兩張參考圖)──────────────
def test_open_kitchen_diner_is_one_room():
    """★★ 1F 後段是**一間開放餐廚**,不是隔成廚房+餐廳兩間。

    參考圖上就寫「餐廳/廚房」;4.5m 面寬硬切,廚房只剩 2m 寬(走廊狀),
    中間那道牆在生活上也沒有意義。上限是**兩間相加**(plan_check 早就這樣算)。"""
    from shapely.geometry import Polygon as _P
    spec = generate_narrow_house(4500.0, 12000.0)
    names = [r.name for r in spec.rooms]
    assert "餐廚" in names, names
    assert "廚房" not in names and "餐廳" not in names, names
    area = next(_P(r.points).area for r in spec.rooms if r.name == "餐廚")
    from src.design.layout.narrow_house import _room_area_cap
    assert area <= _room_area_cap("餐廚")


def test_merged_room_gets_both_furniture_programs():
    """★★ 「餐廚」要同時有**流理台**和**餐桌**。

    `plan_check.MERGED_ROOM_PARTS` 早就知道餐廚 = 餐廳+廚房(面積上限相加),
    但擺家具的那支以前只看 kind → 開放餐廚只擺得出餐桌、沒有流理台。
    同一件事不能兩個地方兩把尺。"""
    spec = generate_narrow_house(4500.0, 12000.0)
    assert "餐廚" in [r.name for r in spec.rooms]
    names = {getattr(f, "name", "") for f in spec.fixtures}
    kinds = {type(f).__name__ for f in spec.fixtures}
    assert "Counter" in kinds, "餐廚沒有流理台(只看 kind 就會漏掉)"
    assert "table4" in names, "餐廚沒有餐桌"
    assert "fridge" in names, "餐廚沒有冰箱"


def test_patio_is_off_by_default():
    """★ 天井預設不開 —— 它每層要花掉約 3㎡,是屋主的決定不是引擎的。"""
    spec = generate_narrow_house(4500.0, 12000.0)
    assert not [r for r in spec.rooms if r.kind == "patio"]


def test_patio_sits_in_the_core_and_is_big_enough_to_count():
    """★★ 開了天井:位置在中段核、貼著浴廁(參考圖就是這樣畫的),
    而且要大到 `code_check` 認得(太小的天井採不到光)。"""
    from shapely.geometry import Polygon as _P

    from src.design.layout.code_check import (
        PATIO_MIN_AREA_M2,
        PATIO_MIN_SIDE,
        daylight_patios,
    )
    floors = generate_narrow_building(4500.0, 15000.0, floors=3, seed=0,
                                      patio=True)
    for _label, spec in floors:
        pats = [r for r in spec.rooms if r.kind == "patio"]
        assert len(pats) == 1
        poly = _P(pats[0].points)
        x0, y0, x1, y1 = poly.bounds
        assert min(x1 - x0, y1 - y0) >= PATIO_MIN_SIDE
        assert poly.area / 1e6 >= PATIO_MIN_AREA_M2
        assert daylight_patios(spec), "code_check 不認這個天井 = 白開"
        bath = next(r for r in spec.rooms if r.kind == "bathroom")
        assert poly.distance(_P(bath.points)) < 1.0        # 貼著浴廁


def test_patio_window_counts_as_daylight():
    """★★ §41:開向天井的窗一樣算採光。以前 `_is_exterior` 只認建築外緣,
    等於「天井開了窗也不算」—— 那樣天井就完全沒有意義。"""
    from src.design.layout.code_check import _faces_daylight, daylight_patios
    from src.design.layout.plan_check import building_env
    floors = generate_narrow_building(4500.0, 15000.0, floors=3, seed=0,
                                      patio=True)
    spec = floors[0][1]
    env = building_env(spec)
    pats = daylight_patios(spec)
    inner = [w for w in spec.walls
             if any(op.kind == "window" for op in w.openings)
             and not _faces_daylight(w, env, [])]
    assert all(_faces_daylight(w, env, pats) for w in inner),         "有窗開在內牆上,而且連天井都不算 → 那扇窗是白開的"


@pytest.mark.parametrize("bw", [3500.0, 4450.0, 5450.0])
def test_patio_does_not_buy_extra_depth(bw):
    """★★ **天井不會讓你蓋得更深**(2026-08-26 實測,別再試一次)。

    直覺是「中段補一個採光面 → 前後段各自要服務的進深變短」,但天井只貼著
    服務格的一側,只服務得到前段**或**後段;進深是前後對稱長的,擋住你的
    永遠是「沒貼到天井的那一段」。要靠天井加深得做成貫穿面寬的**天井帶**。

    ⚠️ 第一次量成「一律 +2.0m」是假的:探測跑的是 `generate_narrow_building`,
       它有進深退讓階梯(最多退 4×500=2000mm)—— 量到的是退讓救回來的。
       這條測試直接比 `max_depth_for`,不經那條階梯。"""
    from src.design.layout.plan_check import building_env
    from src.design.layout.narrow_house import max_depth_for
    envs = []
    for patio in (False, True):
        floors = generate_narrow_building(bw + 550.0, 22000.0, floors=3,
                                          seed=0, patio=patio)
        x0, y0, x1, y1 = building_env(floors[0][1])
        envs.append(y1 - y0)
        assert y1 - y0 <= max_depth_for(x1 - x0) + 1.0
    assert abs(envs[0] - envs[1]) < 1.0, ("天井改變了建築進深?", envs)


def test_patio_costs_floor_area_every_level():
    """★ 天井是貫穿到屋頂的洞:**每一層**都少掉那塊樓地板,不是只有一層。"""
    from shapely.geometry import Polygon as _P
    def area(patio):
        floors = generate_narrow_building(4500.0, 15000.0, floors=3, seed=0,
                                          patio=patio)
        return [sum(_P(r.points).area for r in sp.rooms if r.kind != "patio")
                for _l, sp in floors]
    without, with_ = area(False), area(True)
    for a, b in zip(without, with_):
        assert a - b > 2.5e6, (a, b)       # 每層都少掉 ≥2.5㎡


# ── 進深 12~18m(使用者 2026-08-25:「15 米左右是主流」)─────────────────────
def test_max_depth_table_is_the_measured_one():
    """★ 進深上限是**量出來的表**,不是一個寫死的數字。

    舊值(<4m 給 12.0m、其餘一律 13.5m)比實際能力保守 1~2.5m。這條測試釘住表
    本身,誰把它調回單一常數就會紅。"""
    from src.design.layout.narrow_house import _MAX_DEPTH_POINTS, max_depth_for
    for w, d in _MAX_DEPTH_POINTS:
        assert abs(max_depth_for(w) - d) < 1.0, w
    assert max_depth_for(3000.0) == _MAX_DEPTH_POINTS[0][1]     # 表外往下夾
    assert max_depth_for(9000.0) == _MAX_DEPTH_POINTS[-1][1]    # 表外往上夾
    mid = max_depth_for(4200.0)                                 # 中間靠內插
    assert _MAX_DEPTH_POINTS[1][1] < mid < _MAX_DEPTH_POINTS[2][1]


@pytest.mark.parametrize("bw,least", [(5000.0, 14000.0), (6000.0, 15000.0),
                                      (8000.0, 16000.0)])
def test_mainstream_depth_is_actually_built(bw, least):
    """★★ 15m 進深是台灣透天的主流尺寸,不該被上限砍掉。

    改之前一律封頂在 13.5m —— 5m 面寬的基地明明撐得住 14.45m,白白少蓋一整排。"""
    from src.design.layout.plan_check import building_env
    spec = generate_narrow_house(bw, 18000.0)
    x0, y0, x1, y1 = building_env(spec)
    assert y1 - y0 >= least, (bw, y1 - y0)


@pytest.mark.parametrize("bw", [4000.0, 5000.0, 6000.0, 7000.0, 8000.0])
def test_all_floors_share_one_depth(bw):
    """★★ 進深退讓要在**整棟**這層做:各層各退各的,上下樓外牆就對不齊了。"""
    from src.design.layout.plan_check import building_env
    floors = generate_narrow_building(bw, 18000.0, floors=3, seed=1)
    envs = {tuple(round(v, 3) for v in building_env(spec))
            for _label, spec in floors}
    assert len(envs) == 1, envs


@pytest.mark.parametrize("bw", [3500.0, 4500.0, 5500.0, 6500.0, 8000.0])
@pytest.mark.parametrize("bd", [15000.0, 18000.0])
def test_deep_lots_still_pass_daylight(bw, bd):
    """★★ 上限放寬之後,深基地仍要過 §40 —— 表估太樂觀時由 `_fit_depth` 收回來。

    這條是「表 + 退讓階梯」這組機制的看門人:表可以樂觀,但圖不可以違規。"""
    from src.design.layout.code_check import check_code_building
    from src.design.layout.plan_check import check_building
    floors = generate_narrow_building(bw, bd, floors=3, seed=0)
    plan = check_building(floors)
    code = check_code_building(floors)
    assert plan.ok, [str(i) for i in plan.errors]
    assert code.ok, [str(i) for i in code.violations]


def test_service_slot_shrinks_to_unlock_a_band_split():
    """★★ 6m 級面寬:服務格 1.875m 還沒碰到上限夾,但樓梯間也因此不夠往西,
    前段切不成兩間 → 28㎡ 的臥室。把浴廁壓到最小寬(1.5m)就切得開。

    這是拿「浴廁窄 37cm」換「臥室大小正常」,而且只在真的切不動時才做。"""
    from src.design.layout.narrow_house import BATH_MIN_W, _band_split_x, _core_widths
    bw = 5450.0                                     # 6m 基地扣掉留柱位之後
    svc, _sw = _core_widths(bw)
    assert _band_split_x(0.0, bw, svc) is None      # 一般寬度:切不動
    svc_min, _ = _core_widths(bw, min_service=True)
    assert svc_min == BATH_MIN_W
    assert _band_split_x(0.0, bw, svc_min) is not None   # 壓窄之後就切得開


# ── 寬面寬(6~8m):前/後段左右切成兩間 ──────────────────────────────────────
def test_service_slot_never_wider_than_a_bathroom_needs():
    """★ 面寬變寬時,服務格不能跟著無限變寬。

    浴廁用不到那麼寬,多出來的會被 `_core` 當成「空格」塞給隔壁居室,反而把那間
    房撐得更大;多的寬度要給樓梯間(=旁邊的通道變寬),前後段才切得動。"""
    from src.design.layout.narrow_house import BATH_MAX_W, _core_widths
    for bw in (3500.0, 4450.0, 5450.0, 6450.0, 7450.0):
        svc, sw = _core_widths(bw)
        assert abs(svc + sw - bw) < 1e-6, bw          # 兩格要剛好鋪滿面寬
        assert svc <= BATH_MAX_W + 1e-6, (bw, svc)


def test_band_split_leaves_wall_for_the_west_room_door():
    """★★ 切點不能自由選:西半間的門只有北牆能開,而北牆西段貼的是**浴廁**。

    切太靠西 → 西半間跟樓梯間只剩一小段接觸面 → 門補不出來 → 那間房變成「要
    穿越隔壁臥室才進得去」。所以兩邊都要留 BAND_DOOR_ADJ(門寬+兩側牆角淨距)。"""
    from src.design.layout.narrow_house import BAND_DOOR_ADJ, _band_split_x
    x = _band_split_x(0.0, 7450.0, 2400.0)            # 8m 基地的建築寬
    assert x is not None
    assert x - 2400.0 >= BAND_DOOR_ADJ - 1e-6         # 西半間開得出門
    assert 7450.0 - x >= BAND_DOOR_ADJ - 1e-6         # 東半間也是
    assert _band_split_x(0.0, 4450.0, 1875.0) is None  # 5m 基地:切不動


def test_band_split_shares_width_by_how_big_each_room_should_be():
    """★ 切點按兩間房各自的合理上限**按比例**分,不是一律切中間。

    1F 後段是廚房|餐廳,廚房的上限(11㎡)比餐廳(14㎡)小 —— 對半切會讓廚房
    超標。兩邊同 kind(樓上兩間臥室)時退化成切中間。"""
    from src.design.layout.narrow_house import _band_split_x
    mid = _band_split_x(0.0, 7450.0, 0.0, "bedroom", "bedroom")
    assert abs(mid - 3725.0) < 1.0
    kd = _band_split_x(0.0, 7450.0, 0.0, "kitchen", "dining")
    assert kd < mid                                   # 廚房那半要小一點


def test_wide_frontage_splits_bands_into_two_rooms():
    """★★ 8m 面寬:一段當一整間房會生出 43㎡ 的臥室 → 前後段各切成兩間。

    使用者 2026-08-25:「較大的基地會到 6~8 米」。放寬上限本身不難(8m 本來就
    排得下、也不違規),真正要配套的是房間大小。"""
    from src.design.layout.graph_layout import AREA_BAND
    from src.design.layout.plan_check import OVERSIZE_RATIO
    cap = AREA_BAND["bedroom"][1] * OVERSIZE_RATIO
    floors = generate_narrow_building(8000.0, 13500.0, floors=3, seed=0)
    for label, spec in floors[1:]:                    # 2F 以上
        beds = [r for r in spec.rooms if r.kind in ("bedroom", "study")]
        assert len(beds) == 4, (label, [r.name for r in beds])
        for r in beds:
            assert Polygon(r.points).area / 1e6 <= cap, (label, r.name)


@pytest.mark.parametrize("bw", [6000.0, 7000.0, 8000.0])
@pytest.mark.parametrize("seed", [0, 1, 2, 3])
def test_wide_frontage_passes_both_gates(bw, seed):
    """★★ 寬面寬的每個變體都要過兩道關卡,而且**不得有 room_oversize**。

    切成兩間之前,7m 面寬每個 seed 都會冒出 5 個「房間過大」(43㎡ 的臥室);
    8m 則是直接 raise。這條測試同時釘住「生得出來」與「房間大小合理」。"""
    from src.design.layout.code_check import check_code_building
    from src.design.layout.plan_check import check_building
    floors = generate_narrow_building(bw, 13500.0, floors=3, seed=seed)
    plan = check_building(floors)
    code = check_code_building(floors)
    assert plan.ok, [str(i) for i in plan.errors]
    assert code.ok, [str(i) for i in code.violations]
    assert "room_oversize" not in {i.code for i in plan.warnings}


def test_repair_door_tries_other_positions_to_reach_the_named_neighbor():
    """★★ 一道牆可能同時貼著**兩間**鄰室(前段北牆:西半浴廁、東半樓梯間)。

    要求「這間一定要有門直通公共動線」時,第一個合法位置若落在浴廁那半,得
    **換位置再試**,而不是整道牆放棄 —— 放棄的話那間房就一扇門都沒有。
    (修之前:中點正好落在浴廁那半 → 8m 面寬每個 seed 都出 through_bedroom。)"""
    from src.design.layout.bsp_layout import rooms_to_spec
    from src.design.layout.narrow_house import _add_interior_door, _rect
    env = (0.0, 0.0, 6000.0, 6000.0)
    rooms = [("bedroom", "臥室", _rect(0, 0, 6000, 3000)),
             ("bathroom", "浴廁", _rect(0, 3000, 2400, 6000)),
             ("stair_hall", "樓梯間", _rect(2400, 3000, 6000, 6000))]
    spec = rooms_to_spec(rooms, env, 6000.0, 6000.0, setback=0.0)
    for w in spec.walls:                              # 製造「臥室沒有門」的局面
        w.openings = [op for op in w.openings if op.kind != "door"]
    spec.doors = []

    assert _add_interior_door(spec, spec.rooms[0], 0.0, 0.0, 6000.0, 2,
                              only_kinds={"stair_hall"})
    dp = spec.doors[0]
    wall = spec.walls[dp.wall_index]
    px, py = wall.point_at(wall.openings[dp.opening_index].position)
    assert abs(py - 3000.0) < 60.0                    # 開在中間那道牆上
    assert px > 2400.0                                # 而且開在**樓梯間**那半


# ── 房間不重疊、鋪滿建築 ────────────────────────────────────────────────────
# ── N1e:接進網站(建築物尺寸 → 反推基地 → 自動選窄透天骨架)──────────────
def test_building_basis_reverse_derives_site():
    """★「建築物 7×12」→ 基地 = 建築 + 四周退縮(11×16)。"""
    from src.design.nl_parser import building_brief_from_data
    data = {"brief_type": "house", "site_width_m": 7, "site_depth_m": 12,
            "dimension_basis": "building", "floors_above": 3, "bedrooms": 3}
    brief = building_brief_from_data(data)
    assert brief.typical.site_width == 11000      # 7000 + 2*2000
    assert brief.typical.site_depth == 16000      # 12000 + 2*2000


def test_auto_router_picks_narrow_for_narrow_building():
    """★ 建築 7m 寬 → 自動走窄面寬骨架(樓梯間,無天井)。"""
    from src.design.building_generator import BuildingBrief, generate_building_auto
    from src.design.layout_generator import HouseBrief
    brief = BuildingBrief(typical=HouseBrief(site_width=11000, site_depth=16000,
                          bedrooms=3), floors=3, differentiated=True)
    building = generate_building_auto(brief)
    assert len(building.floors) == 3
    kinds = {r.kind for fl in building.floors for r in fl.spec.rooms}
    assert "stair_hall" in kinds
    assert "patio" not in kinds


def test_auto_router_keeps_two_band_for_wide_building():
    """寬基地(建築 ≥10m)仍走既有兩帶式(不是窄透天)。"""
    from src.design.building_generator import BuildingBrief, generate_building_auto
    from src.design.layout_generator import HouseBrief
    brief = BuildingBrief(typical=HouseBrief(site_width=19000, site_depth=13000,
                          bedrooms=3), floors=3, differentiated=True)
    building = generate_building_auto(brief)
    kinds = {r.kind for fl in building.floors for r in fl.spec.rooms}
    assert "stair_hall" not in kinds              # 兩帶式沒有窄透天的樓梯間 kind


def test_rooms_tile_building():
    """★ 房間不重疊、且鋪滿整棟(不留無主的空隙)。

    ⚠️ 外框要由 **spec 自己推**(`building_env`),不能拿輸入的 W×D 當答案 ——
    產生器會替外牆柱留位置(`STRUCT_MARGIN`)、也會封頂深度留院子,建築因此比
    傳進去的尺寸小。拿 W×D 比對是在釘「建築剛好等於基地」這個早就不成立的巧合。"""
    from src.design.layout.plan_check import building_env
    from src.design.layout_generator import STRUCT_MARGIN
    spec = generate_narrow_house(W, D)
    polys = [Polygon(r.points) for r in spec.rooms]
    for i in range(len(polys)):
        for j in range(i + 1, len(polys)):
            assert polys[i].intersection(polys[j]).area < 1e4
    x0, y0, x1, y1 = building_env(spec)
    total = sum(p.area for p in polys)
    assert abs(total - (x1 - x0) * (y1 - y0)) / ((x1 - x0) * (y1 - y0)) < 0.02
    # 建築確實只是「為了留柱位」縮了一點,不是整個垮掉
    assert W - 2 * STRUCT_MARGIN - 1 <= x1 - x0 <= W + 1
    assert D - 2 * STRUCT_MARGIN - 1 <= y1 - y0 <= D + 1


# ── 1F 車庫(使用者 2026-08-26:「做出有車庫的」)──────────────────────────────
GARAGE_W, GARAGE_D = 4500.0, 14400.0     # 參考平面那張街屋的建築尺寸


def _garage_room(spec):
    return next((r for r in spec.rooms if r.kind == "garage"), None)


def test_garage_takes_the_whole_front_band_on_1f():
    """★★ 車庫佔滿 1F 前段(整個面寬),而且深得下一個法定車位。

    ⚠️ 中間不留客廳是**故意的**:那間房南邊車庫、北邊中段核,一面外牆都沒有,
    §40 採光直接違規。"""
    from src.design.layout.narrow_house import garage_min_depth
    floors = generate_narrow_building(GARAGE_W, GARAGE_D, floors=3, seed=3,
                                      garage=True)
    spec = floors[0][1]
    gar = _garage_room(spec)
    assert gar is not None, [r.name for r in spec.rooms]
    x0, y0, x1, y1 = Polygon(gar.points).bounds
    others = [r for r in spec.rooms if r is not gar]
    bx0 = min(p[0] for r in spec.rooms for p in r.points)
    bx1 = max(p[0] for r in spec.rooms for p in r.points)
    assert abs(x0 - bx0) < 1.0 and abs(x1 - bx1) < 1.0     # 整個面寬
    assert y1 - y0 >= garage_min_depth() - 1.0             # 停得進一台車
    assert not [r for r in others if r.kind == "living"]   # 1F 沒有客廳


def test_living_room_moves_upstairs_when_1f_is_a_garage():
    """★★ 1F 讓給車庫 → 客廳挪到 2F 前段(台灣透天最常見的做法)。"""
    floors = generate_narrow_building(GARAGE_W, GARAGE_D, floors=3, seed=3,
                                      garage=True)
    second = floors[1][1]
    assert [r for r in second.rooms if r.kind == "living"], \
        [r.name for r in second.rooms]


def test_garage_keeps_the_stair_core_aligned():
    """★★ 車庫**不另插一帶**,所以每層的核仍在同一個進深位置、樓梯上下對齊。

    (插第四帶就會把 1F 的核往北推,樓上的核卻沒跟著推 → 樓梯對不齊。)"""
    floors = generate_narrow_building(GARAGE_W, GARAGE_D, floors=3, seed=3,
                                      garage=True)
    ys = set()
    for _, spec in floors:
        st = spec.stairs[0]
        ys.add((round(st.origin[0], 1), round(st.origin[1], 1)))
    assert len(ys) == 1, ys


def test_garage_has_a_rolling_shutter_on_the_road_wall():
    """★★ 臨路南牆上要有捲門:夠寬(≥法定車位寬)、畫成不帶開門弧、註明「捲門」。"""
    from src.design.layout.narrow_house import GARAGE_DOOR_MIN_W
    floors = generate_narrow_building(GARAGE_W, GARAGE_D, floors=3, seed=3,
                                      garage=True)
    spec = floors[0][1]
    shutters = [dp for dp in spec.doors if dp.door.label == "捲門"]
    assert len(shutters) == 1
    dp = shutters[0]
    op = spec.walls[dp.wall_index].openings[dp.opening_index]
    assert op.width >= GARAGE_DOOR_MIN_W
    assert dp.door.sliding is True          # 捲門往上捲,平面沒有開門弧
    by0 = min(p[1] for r in spec.rooms for p in r.points)
    assert abs(spec.walls[dp.wall_index].point_at(op.position)[1] - by0) < 100


def test_car_is_parked_in_the_garage():
    """★★ 車庫裡要看得到車(不然圖上就是一間空房)。

    ⚠️ 車走的不是擺位器:它只在房間正中央試 9 個點,4.5m 街屋的車庫正中央必定
    壓到室內門的開啟弧 → 一台都擺不出來(見 auto_furnish._park_car)。"""
    floors = generate_narrow_building(GARAGE_W, GARAGE_D, floors=3, seed=3,
                                      garage=True)
    spec = floors[0][1]
    gar = Polygon(_garage_room(spec).points)
    cars = [f for f in spec.fixtures if getattr(f, "name", "") == "car"]
    assert len(cars) == 1, [getattr(f, "name", f) for f in spec.fixtures]
    assert gar.contains(__import__("shapely.geometry", fromlist=["Point"])
                        .Point(*cars[0].insert))


@pytest.mark.parametrize("bw,bd", [(4000.0, 13450.0), (4500.0, 14400.0),
                                   (5450.0, 15450.0), (7450.0, 16450.0)])
def test_garage_plans_pass_both_gates(bw, bd):
    """★★ 有車庫的圖一樣要過 plan_check + code_check(硬錯誤 0、法規 0)。"""
    from src.design.layout.code_check import check_code_building
    from src.design.layout.plan_check import check_building
    floors = generate_narrow_building(bw, bd, floors=3, seed=3, garage=True)
    plan = check_building(floors)
    code = check_code_building(floors)
    assert plan.ok, [str(i) for i in plan.errors]
    assert code.ok, [str(i) for i in code.violations]


def test_garage_needs_a_deeper_building():
    """★★ 車庫要多一個車位長的進深(≥13.1m);3.5m 面寬的採光上限只有 12.5m,
    放不下 —— 這是**真實的限制**,要擋得明白,不是默默生一張擠壞的圖。"""
    from src.design.layout.narrow_house import max_depth_for, min_depth_for
    assert min_depth_for(4450.0, garage=True) > min_depth_for(4450.0)
    assert max_depth_for(3500.0) < min_depth_for(3500.0, garage=True)
    with pytest.raises(ValueError, match="車庫"):
        generate_narrow_building(3500.0, 12500.0, floors=3, garage=True)


def test_single_floor_house_refuses_a_garage():
    """★★ 一層樓 + 車庫 = 連客廳都沒有(客廳是往上挪的),要擋掉。"""
    with pytest.raises(ValueError, match="車庫"):
        generate_narrow_building(GARAGE_W, GARAGE_D, floors=1, garage=True)


def test_no_garage_layout_is_unchanged():
    """★★ 不要車庫時整棟必須跟以前一模一樣(加分項不得動到既有行為)。"""
    plain = generate_narrow_building(GARAGE_W, GARAGE_D, floors=3, seed=3)
    assert [r.name for r in plain[0][1].rooms][0] == "客廳"
    assert not [r for r in plain[0][1].rooms if r.kind == "garage"]


def test_sliding_door_label_survives_mirroring():
    """★★ 鏡射後「捲門」不能變回「拉門」(門扇註記要跟著翻過去)。"""
    from src.design.layout_generator import _mirror_spec
    floors = generate_narrow_building(GARAGE_W, GARAGE_D, floors=3, seed=3,
                                      garage=True)
    spec = _mirror_spec(floors[0][1], True, False)
    assert [dp for dp in spec.doors if dp.door.label == "捲門"]


@pytest.mark.parametrize("bw,bd,seed", [(4500.0, 14400.0, 3),
                                        (7960.0, 16360.0, 95),
                                        (5450.0, 15450.0, 11)])
def test_no_two_rooms_on_a_floor_share_a_name(bw, bd, seed):
    """★★ 同一層不得有兩間同名房。

    ⚠️ 這不是潔癖:`door_rules.repair_doors` 修 `through_bedroom` 時是**用名字**
    去找那間房的(本專案「房間不能用名稱比對」那條坑)。2F 的前段切開後若也叫
    「書房2F」,就會跟後段那間撞名 → 修到錯的那間、等於沒修
    (實測 7.96×16.36 seed95 就卡在這裡,掃描出現 through_bedroom)。"""
    floors = generate_narrow_building(bw, bd, floors=3, seed=seed, garage=True)
    for label, spec in floors:
        names = [r.name for r in spec.rooms]
        assert len(names) == len(set(names)), (label, names)


# ── 白做的工:重生前那一趟不要跑到最後 ──────────────────────────────────────
def _repair_calls(bw, bd, floors):
    """蓋這棟樓時 `repair_doors` 被呼叫幾次。"""
    from src.design.layout import door_rules as dr
    real, n = dr.repair_doors, {"n": 0}

    def counted(*a, **k):
        n["n"] += 1
        return real(*a, **k)

    dr.repair_doors = counted
    try:
        nh.generate_narrow_building(bw, bd, floors=floors)
    finally:
        dr.repair_doors = real
    return n["n"]


@pytest.mark.parametrize("bw,bd,floors", [(4500.0, 15000.0, 3),
                                          (6000.0, 13000.0, 3),
                                          (7000.0, 12000.0, 1)])
def test_door_repair_not_wasted_on_rebuild(bw, bd, floors):
    """★★ 判斷「空格併進居室健不健康」要在**修門之前**做。

    `_spare_hosts_ok` 不合格時整層會用 force_absorb 重生一次;修門(轉門把、改
    橫拉門)是整條產線最貴的一段,而它做的事情**不影響**那個判斷(它只改門,
    判斷看的是房間形狀與窗寬)。放在判斷後面等於每一層都白修一次門。

    ⚠️ 這條會失敗的話,先確認 `repair_doors` 真的還是不影響判斷
    (見 `test_spare_hosts_verdict_survives_door_repair`),而不是直接調鬆。
    """
    # 一層樓正常會修三次門:①開口收尾後 ②柱定案後 ③擺完家具後(柱是實心的、
    # 車要停得進去,兩次都得讓門去閃)。重生前那一趟若跑到修門,就會變成四次。
    assert _repair_calls(bw, bd, floors) == 3 * floors, (
        "修門次數超過「每層 3 次」→ 重生前那一趟又白修了一次門")


@pytest.mark.parametrize("bw,bd", [(4500.0, 15000.0), (6000.0, 13000.0),
                                   (8000.0, 16000.0)])
def test_spare_hosts_verdict_survives_door_repair(bw, bd):
    """★★ 上一條的前提:修門前後,`_spare_hosts_ok` 的答案必須一樣。

    修門只換門的鉸鏈邊/開啟方向、必要時改橫拉門,不動房間多邊形、也不動窗 ——
    所以判斷可以提前做。哪天修門開始會加窗或改房間,這條會先紅,提醒你把
    判斷搬回去。"""
    from src.design.layout import door_rules as dr

    real, diff = dr.repair_doors, []

    def probe(spec, *a, **k):
        before = nh._spare_hosts_ok(spec)
        r = real(spec, *a, **k)
        if nh._spare_hosts_ok(spec) != before:
            diff.append(getattr(spec, "floor_label", "?"))
        return r

    dr.repair_doors = probe
    try:
        nh.generate_narrow_building(bw, bd, floors=3)
    finally:
        dr.repair_doors = real
    assert not diff, f"修門改變了判斷:{diff}"
