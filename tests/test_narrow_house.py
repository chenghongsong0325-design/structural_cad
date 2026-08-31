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
from shapely.geometry import Point, Polygon

from src.design.layout import narrow_house as nh
from src.design.layout.auto_furnish import (
    COUNTER_INSET,
    COUNTER_MIN_LEN,
    _counter_candidates,
    _merged_kinds,
)
from src.design.semantic.room_semantic import canonical_room
from src.drafting.fixtures import counter_footprint
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
    併進隔壁居室(變 L 形)或併進樓梯間,不隔成一間沒人用的小房。

    ⚠️ **主臥的更衣室不在此列**(2026-08-27 的 2F 參考平面上就有一間):它是
    刻意切出來、有人用、而且是主臥的附屬空間 —— 這條規則擋的是「吃剩的空格
    被隔成一間沒人用的小房」,判準因此改成「storage 只能是更衣室」,不是放寬。"""
    for bw in (3500.0, 5000.0, 7000.0):
        for bd in (10500.0, 12000.0, 16000.0):
            for lb, spec in generate_narrow_building(bw, bd, floors=3):
                junk = [r for r in spec.rooms if r.kind == "storage"
                        and not r.name.startswith("更衣室")]
                assert not junk, (bw, bd, lb, [r.name for r in junk])


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
    而且要大到 `code_check` 認得(太小的天井採不到光)。

    ⚠️ 尺寸從 15000/seed0 換成 14450/seed7,不是為了讓測試好過:原本那組**開了
    天井就是一張壞圖**(2F 的更衣室與浴室只能穿過主臥才進得去),而這條測試只
    量幾何、從來沒送 plan_check,所以釘了一個壞掉的設定很久都沒人發現。
    `_fit_patio` 上線之後那組會自動退掉天井 —— 退得對。下面補上 plan_check,
    這條測試就不可能再釘到壞圖(本檔「報表會說謊」那一族)。"""
    from shapely.geometry import Polygon as _P

    from src.design.layout.code_check import (
        PATIO_MIN_AREA_M2,
        PATIO_MIN_SIDE,
        daylight_patios,
    )
    from src.design.layout.plan_check import check_building
    floors = generate_narrow_building(4500.0, 14450.0, floors=3, seed=7,
                                      patio=True)
    assert check_building(floors).ok, [str(i)
                                       for i in check_building(floors).errors]
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
    """★ 天井是貫穿到屋頂的洞:**每一層**都少掉那塊樓地板,不是只有一層。

    ⚠️ 尺寸換成天井真的留得住的那一組(理由同上一條測試)。"""
    from shapely.geometry import Polygon as _P
    def area(patio):
        floors = generate_narrow_building(4500.0, 14450.0, floors=3, seed=7,
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
    floors = generate_narrow_building(8000.0, 13500.0, floors=3, seed=0)
    for label, spec in floors[1:]:                    # 2F 以上
        # ⚠️ 2F 前段的西半間現在 kind 是 `master_bedroom`(主臥),要一起數 ——
        #    漏掉它會以為那一段沒切。
        beds = [r for r in spec.rooms
                if r.kind in ("bedroom", "master_bedroom", "study")]
        assert len(beds) == 4, (label, [r.name for r in beds])
        for r in beds:                                # 各用**自己那種房**的上限
            cap = AREA_BAND[r.kind][1] * OVERSIZE_RATIO
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
def _spare_verdict_vs_door_repair(bw, bd, floors):
    """回 (問了幾次 `_spare_hosts_ok`, 其中幾次是**修過門之後**才問的)。

    ⚠️ 2026-08-28 從「數修門總次數」改成這樣。原本釘的是「整棟正好 3×層數 次」,
    但 `_fit_core_reach` 量到「有門走不到」時會換一種核的排法**重蓋整棟**,
    次數自然會變 —— 那是合理的重蓋,不是白修門。真正要釘的是**順序**:
    `_spare_hosts_ok` 這個判斷必須在這一層修門**之前**問完,
    數字怎麼變都不影響這件事。"""
    from src.design.layout import door_rules as dr
    st = {"since": 0, "asked": 0, "late": 0}
    real_spec, real_rep, real_ok = (nh.rooms_to_spec, dr.repair_doors,
                                    nh._spare_hosts_ok)

    def spec_(*a, **k):
        st["since"] = 0                      # 新的一層,重新計數
        return real_spec(*a, **k)

    def rep_(*a, **k):
        st["since"] += 1
        return real_rep(*a, **k)

    def ok_(*a, **k):
        st["asked"] += 1
        st["late"] += bool(st["since"])
        return real_ok(*a, **k)

    nh.rooms_to_spec, dr.repair_doors, nh._spare_hosts_ok = spec_, rep_, ok_
    try:
        nh.generate_narrow_building(bw, bd, floors=floors)
    finally:
        nh.rooms_to_spec, dr.repair_doors, nh._spare_hosts_ok = (
            real_spec, real_rep, real_ok)
    return st["asked"], st["late"]


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
    # 車要停得進去,兩次都得讓門去閃)。這條測的不是「總共幾次」——`_fit_core_reach`
    # 會為了換一種核的排法重蓋整棟,次數本來就會變 —— 而是**順序**:每一次問
    # `_spare_hosts_ok` 的時候,那一層都還沒修過門。
    asked, late = _spare_verdict_vs_door_repair(bw, bd, floors)
    assert asked, "`_spare_hosts_ok` 根本沒被問到 —— 這條測試沒在測東西"
    assert late == 0, (
        f"{late}/{asked} 次是修過門之後才問的 —— 那一趟的門白修了")


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


# ── 主臥 + 更衣室(使用者 2026-08-27 給的 2F 參考平面)────────────────────────
def _rooms_by_name(spec):
    return {r.name: Polygon(r.points) for r in spec.rooms}


def test_second_floor_front_is_the_master_bedroom():
    """★★ 主臥在 2F 前段(參考平面就是這樣),而且用**主臥自己的面積尺**。

    ⚠️ 拿次臥的上限(18㎡×1.5)去量主臥,24㎡ 的主臥會被判過大 → 切成兩間小房。
    主臥的 band 是 12~24㎡。"""
    floors = generate_narrow_building(4500.0, 14400.0, floors=3, seed=3)
    second = floors[1][1]
    master = [r for r in second.rooms if r.kind == "master_bedroom"]
    assert len(master) == 1 and master[0].name == "主臥室", \
        [(r.name, r.kind) for r in second.rooms]
    assert [r for r in second.rooms if r.name.startswith("次臥")]


def test_master_bedroom_has_a_walk_in_closet():
    """★★ 主臥要切得出更衣室(參考平面上的「更衣」)。

    切在**貼著浴廁那一段北牆**:那段本來就開不了主臥的門(門要開向樓梯間),
    拿來當更衣室剛好,主臥的門仍然開在剩下那段牆上。
    ⚠️ 用 6m 面寬:4.5m 的主臥切了更衣室就擺不下床,會照鐵則退掉(見下一條)。"""
    floors = generate_narrow_building(6000.0, 15000.0, floors=3, seed=5)
    second = floors[1][1]
    closet = [r for r in second.rooms if r.name.startswith("更衣室")]
    assert len(closet) == 1, [r.name for r in second.rooms]
    from src.design.layout.narrow_house import CLOSET_MIN_D, CLOSET_MIN_W
    x0, y0, x1, y1 = Polygon(closet[0].points).bounds
    assert x1 - x0 >= CLOSET_MIN_W - 1.0
    assert y1 - y0 >= CLOSET_MIN_D - 1.0
    # 更衣室是主臥的附屬空間,不是居室:不必採光、門只從主臥開
    assert closet[0].kind == "storage"


def test_walk_in_closet_is_not_a_through_bedroom_defect():
    """★★ 只跟主臥相連的更衣室**不算**「要穿越別人的臥室」。

    跟套內衛浴是同一件事 —— 規則本來只豁免衛浴,更衣室一加進來每一層都冒
    `through_bedroom`。⚠️ 豁免的條件是「只有**一個**鄰室而且那間是臥室」:
    有兩個以上鄰室的儲藏是公共儲藏,藏在臥室後面才真的是缺陷。"""
    from src.design.layout.door_rules import ENSUITE_KINDS
    from src.design.layout.plan_check import check_building
    assert "storage" in ENSUITE_KINDS and "bathroom" in ENSUITE_KINDS
    floors = generate_narrow_building(4500.0, 14400.0, floors=3, seed=3)
    plan = check_building(floors)
    assert plan.ok, [str(i) for i in plan.errors]


def test_master_bedroom_actually_gets_furniture():
    """★★ 主臥要擺得出床。

    ⚠️ `canonical_room("master_bedroom")` 若沒對映回 "bedroom",家具表就查不到 →
    主臥會是一間**空房**(擺位器不報錯,只是什麼都不擺)。"""
    from src.design.semantic.room_semantic import canonical_room
    assert canonical_room("master_bedroom") == "bedroom"
    floors = generate_narrow_building(4500.0, 14400.0, floors=3, seed=3)
    second = floors[1][1]
    master = Polygon(next(r for r in second.rooms
                          if r.kind == "master_bedroom").points)
    from shapely.geometry import Point
    beds = [f for f in second.fixtures
            if getattr(f, "name", "").startswith("bed")
            and master.contains(Point(*f.insert))]
    assert beds, [getattr(f, "name", f) for f in second.fixtures]


def test_garage_pushes_the_master_up_to_3f():
    """★★ 1F 讓給車庫時 2F 前段是客廳 → 主臥往上挪到 3F(不能兩個都要)。"""
    from src.design.layout.narrow_house import _master_level
    assert _master_level(False) == 2 and _master_level(True) == 3
    floors = generate_narrow_building(4500.0, 14400.0, floors=3, seed=3,
                                      garage=True)
    assert not [r for r in floors[1][1].rooms if r.kind == "master_bedroom"]
    assert [r for r in floors[2][1].rooms if r.kind == "master_bedroom"]


_FUR_CASES = [(4500.0, 14400.0, False, 3), (6000.0, 15000.0, False, 5),
              (7450.0, 16450.0, False, 2), (4500.0, 14400.0, True, 3)]


def _fixtures_in(spec, room):
    from shapely.geometry import Point
    poly = Polygon(room.points).buffer(1.0)
    return [getattr(f, "name", "counter") for f in spec.fixtures
            if getattr(f, "name", None) and poly.contains(Point(*f.insert))]


@pytest.mark.parametrize("bw,bd,garage,seed", _FUR_CASES)
def test_closet_never_costs_the_master_its_bed(bw, bd, garage, seed):
    """★★ 更衣室是加分項,**不得讓主臥擺不下床**(那比沒有更衣室糟得多)。

    主臥切出更衣室之後就多一個要走得到的目的地,`_declutter_for_circulation`
    為了保住那條通道會把床搬走。兩道防線接力擋這件事,這條測的是**結果**:
      ① `restore_essentials` 換小一號的床補回來(雙人 → 單人);
      ② 還是補不回來就 `_beds_ok` 退掉更衣室重生
         (同 `_fit_service` / `_fit_margin` 的鐵則)。
    所以判準不是「有沒有更衣室」,是「**有更衣室的那層,主臥一定有床**」。"""
    from shapely.geometry import Point
    from src.design.layout.auto_furnish import BEDS
    for label, spec in generate_narrow_building(bw, bd, floors=3, seed=seed,
                                                garage=garage):
        if not [r for r in spec.rooms if r.name.startswith("更衣室")]:
            continue
        master = Polygon(next(r for r in spec.rooms
                              if r.kind == "master_bedroom").points).buffer(1.0)
        assert [f for f in spec.fixtures
                if getattr(f, "name", "") in BEDS
                and master.contains(Point(*f.insert))], label


@pytest.mark.parametrize("bw,bd,seed", [(4500.0, 14400.0, 3),
                                        (6000.0, 15000.0, 5),
                                        (7450.0, 16450.0, 2),
                                        (5450.0, 15450.0, 11)])
def test_core_is_identical_on_every_floor(bw, bd, seed):
    """★★ 核(浴廁 + 樓梯間)每層同構 —— 樓梯才對得齊、牆才上下對得上。

    ⚠️ 「服務格要不要壓到最窄」是**整棟**的決定,不是各層各自決定的。主臥層改用
    主臥的面積尺(36㎡)之後就不必切房了 → 那層不壓 → 2F 的浴廁比 1F/3F 寬 1㎡,
    牆對不上。判準要拿「最需要壓的那種樓層」(臥室,上限最小)去問。"""
    floors = generate_narrow_building(bw, bd, floors=3, seed=seed)
    cores, stairs = set(), set()
    for _label, spec in floors:
        for r in spec.rooms:
            if r.kind in ("bathroom", "stair_hall"):
                cores.add((r.kind, tuple(round(v, 1)
                                         for v in Polygon(r.points).bounds)))
        stairs.add(tuple(round(v, 1) for v in spec.stairs[0].origin))
    assert len(stairs) == 1, stairs
    assert len(cores) == 2, sorted(cores)      # 剛好一間浴廁 + 一個樓梯間的框


# ── 家具:每間房都要配得完整(使用者 2026-08-27「家具也都幫我設計進去」)──────
@pytest.mark.parametrize("bw,bd,garage,seed", _FUR_CASES)
def test_every_bedroom_has_a_bed(bw, bd, garage, seed):
    """★★ 每一間臥室都要有床 —— 一間空臥室是不完整的圖。

    ⚠️ 床會被 `_declutter_for_circulation` 移掉(它只看動線,不知道床是臥室的
    重點):實測六棟樓被拿掉 6 張雙人床。`restore_essentials` 換**小一號**的
    補回來(雙人 → 單人),原尺寸再放一次只會再被移掉一次。"""
    from src.design.layout.auto_furnish import BEDS
    for label, spec in generate_narrow_building(bw, bd, floors=3, seed=seed,
                                                garage=garage):
        for r in spec.rooms:
            if r.kind not in ("bedroom", "master_bedroom"):
                continue
            got = _fixtures_in(spec, r)
            assert set(got) & set(BEDS), (label, r.name, got)


@pytest.mark.parametrize("bw,bd,garage,seed", _FUR_CASES)
def test_walk_in_closet_is_not_an_empty_room(bw, bd, garage, seed):
    """★★ 更衣室要有吊衣桿。

    ⚠️ 兩個原因會讓它變空房,兩個都修過:①衣櫃 1500 寬,而更衣室淨寬只有
    1350 → 加了窄版的 `closet_rail`(1000×600);②房間小到**門一開就掃掉
    大半個空間**,12 個候選位置全被 door_swing 打回 → 讓門去閃設備
    (`DOOR_DODGES_KINDS`),擺完 `repair_doors` 會把門收成拉門。"""
    for label, spec in generate_narrow_building(bw, bd, floors=3, seed=seed,
                                                garage=garage):
        for r in spec.rooms:
            if not r.name.startswith("更衣室"):
                continue
            got = _fixtures_in(spec, r)
            assert {"wardrobe", "closet_rail"} & set(got), (label, r.name, got)


@pytest.mark.parametrize("bw,bd,garage,seed", _FUR_CASES)
def test_small_bathrooms_get_a_shower_not_a_bathtub(bw, bd, garage, seed):
    """★★ 浴室要有得洗澡;小浴室畫**淋浴間**不畫浴缸。

    ⚠️ 這不是「放不放得下」:1600×750 的浴缸塞得進 3.3㎡ 的浴廁,但塞進去動線
    就不通了,`_declutter_for_circulation` 會整個移掉 → 留下一間只有馬桶的浴室
    (實測六棟樓被移掉 8 個浴缸)。台灣 3~4㎡ 的浴廁本來就是乾濕分離淋浴間。"""
    from src.design.layout.auto_furnish import BATHTUB_MIN_ROOM_M2
    for label, spec in generate_narrow_building(bw, bd, floors=3, seed=seed,
                                                garage=garage):
        for r in spec.rooms:
            if r.kind != "bathroom":
                continue
            got = _fixtures_in(spec, r)
            assert "toilet" in got, (label, r.name, got)
            if Polygon(r.points).area / 1e6 < BATHTUB_MIN_ROOM_M2:
                assert "bathtub" not in got, (label, r.name, got)


def test_nightstands_flank_the_bed_head():
    """★★ 床頭櫃在床的**兩側**,不是兩個並排擠在同一面牆上。

    ⚠️ 交給擺位器就會並排(它只知道「靠牆、分數高」),那不是真實圖面的畫法。
    位置是被床決定的 → `_add_nightstands` 直接算床頭兩側。"""
    import math
    from src.drafting.fixtures import FIXTURE_SIZES
    spec = generate_narrow_building(6000.0, 15000.0, floors=3, seed=5)[1][1]
    room = next(r for r in spec.rooms if r.kind == "master_bedroom")
    poly = Polygon(room.points).buffer(1.0)
    from shapely.geometry import Point
    here = [f for f in spec.fixtures if poly.contains(Point(*f.insert))]
    bed = next(f for f in here if getattr(f, "name", "") .startswith("bed"))
    ns = [f for f in here if getattr(f, "name", "") == "nightstand"]
    assert len(ns) == 2, [getattr(f, "name", f) for f in here]
    th = math.radians(bed.rotation)
    ux, uy = math.cos(th), math.sin(th)
    # 兩個床頭櫃要落在床的**相反兩側**(沿牆方向的投影一正一負)
    proj = sorted((f.insert[0] - bed.insert[0]) * ux
                  + (f.insert[1] - bed.insert[1]) * uy for f in ns)
    assert proj[0] < 0 < proj[1], proj
    half = FIXTURE_SIZES[bed.name][0] / 2.0
    for d in proj:                                # 就貼在床邊,不是房間另一頭
        assert half < abs(d) < half + 800.0, proj


def test_no_nightstand_without_a_bed():
    """★★ 沒有床的房間不留床頭櫃(那是床的配件,單獨畫像漏畫)。

    會發生是因為順序:床頭櫃跟著床擺,但後面的動線修復器可能把床移掉。"""
    from src.design.layout.auto_furnish import BEDS
    for bw, bd, garage, seed in _FUR_CASES + [(8000.0, 13500.0, False, 0)]:
        for label, spec in generate_narrow_building(bw, bd, floors=3, seed=seed,
                                                    garage=garage):
            for r in spec.rooms:
                got = _fixtures_in(spec, r)
                if "nightstand" in got:
                    assert set(got) & set(BEDS), (bw, label, r.name, got)


def test_new_fixture_blocks_are_registered():
    """★★ 新家具要能真的畫出來:圖塊 builder / 尺寸 / 碰撞尺寸都要有。

    ⚠️ 漏一項不會在擺位時報錯,是**畫圖那一刻**才炸(或畫成空白)。"""
    from src.drafting.fixtures import (
        COLLISION_SIZES, FIXTURE_BUILDERS, FIXTURE_SIZES, create_fixture_block,
    )
    from src.web.render import _new_doc
    doc, _layers = _new_doc()
    for name in ("shower", "closet_rail", "table2"):
        assert name in FIXTURE_BUILDERS and name in FIXTURE_SIZES
        assert name in COLLISION_SIZES
        blk = create_fixture_block(doc, name)
        assert len(list(doc.blocks.get(blk))) > 0, name


# ---------------------------------------------------------------------------
# 1F 的家具(使用者 2026-08-27:「1樓的家具也要擺好」)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("bw,bd,garage,seed", _FUR_CASES)
def test_every_kitchen_has_a_counter(bw, bd, garage, seed):
    """★★ 廚房一定要有流理台 —— 沒有流理台的廚房不是廚房。

    ⚠️ 以前流理台的候選**只有「整面牆那麼長」一種**,而窄面寬街屋的廚房是
    1.85×4.3m 的走道型,唯一夠長的那面牆上就開著門 → 整條檯面必撞門迴轉 →
    一件都不擺。實測 4.5/5.45/6m(台灣最常見的三個面寬)的廚房全部沒有流理台。
    現在照真實師傅的做法**截短**(`_counter_candidates` 每級 300mm 往內收)。"""
    from src.drafting.fixtures import Counter
    for label, spec in generate_narrow_building(bw, bd, floors=3, seed=seed,
                                                garage=garage):
        for r in spec.rooms:
            if "kitchen" not in _merged_kinds(r, canonical_room(r.kind)):
                continue
            poly = Polygon(r.points).buffer(1.0)
            got = [c for c in spec.fixtures if isinstance(c, Counter)
                   and poly.contains(Polygon(counter_footprint(c)).centroid)]
            assert got, (label, r.name)


def test_counter_candidates_step_down_instead_of_all_or_nothing():
    """★★ 流理台候選要從整面牆一路**截短**到下限,而且由長到短排序。

    ⚠️ 舊寫法只有「整面牆那麼長」一種:走道型廚房(1.85×4.3m)唯一夠長的那面牆上
    就開著門,整條檯面必撞門迴轉 → 一件都不擺(4.5/5.45/6m 面寬的廚房全部沒有
    流理台)。真實師傅遇到擋路的東西是把檯面**截短** —— `trim_counters_at_columns`
    遇到柱早就這樣做了,這裡只是把同一件事提前到「排候選」那一步。

    ⚠️ 這條釘的是**機制**,不是某個尺寸的圖。格局會變(4.5m 的後段後來改成開放
    餐廚,整面牆的檯面就放得下了),但「放不下要截短、而且取放得下的最長那條」
    這件事不該變 —— 釘表象的測試會在格局一動就失去意義。"""
    from src.drafting.room import Room
    room = Room(name="廚房", points=[(0.0, 0.0), (1850.0, 0.0),
                                    (1850.0, 4325.0), (0.0, 4325.0)],
                kind="kitchen")
    lens = [round(c.length) for c in _counter_candidates(room)]
    assert lens, "走道型廚房至少要有一個候選"
    assert lens == sorted(lens, reverse=True)            # 由長到短 → 取到最長那條
    assert max(lens) == round(4325.0 - 2 * COUNTER_INSET)  # 最長 = 整面長牆
    assert min(lens) >= COUNTER_MIN_LEN                  # 不會短到不像流理台
    assert len(set(lens)) > 1, "只有一種長度 = 沒有截短的候選"


@pytest.mark.parametrize("bw,bd,garage,seed", _FUR_CASES)
def test_every_dining_room_has_a_table(bw, bd, garage, seed):
    """★★ 餐廳一定要有餐桌。

    ⚠️ `table4` 的原點是桌心,而擺位器對那種家具**只在房間正中央試 9 個點** ——
    走道型餐廳(4.5m 面寬街屋的 1.8×4.3m)的正中央就是通道,9 個點全被打回,
    結果是一間沒有餐桌的餐廳。補了**靠牆**的 `table2`,走「沿四面牆找位置」
    那條路。(車子、床都踩過同一個坑。)"""
    for label, spec in generate_narrow_building(bw, bd, floors=3, seed=seed,
                                                garage=garage):
        for r in spec.rooms:
            if "dining" not in _merged_kinds(r, canonical_room(r.kind)):
                continue
            got = _fixtures_in(spec, r)
            assert {"table4", "table2"} & set(got), (label, r.name, got)


def test_dining_table_has_a_wall_anchored_fallback():
    """★★ 餐桌要有**靠牆**的小一號替代品(`table2`)。

    ⚠️ 關鍵不是「小一號」,是**貼牆**。`table4` 的原點是桌心,而擺位器對那種
    家具**只在房間正中央試 9 個點** —— 走道型餐廳(1.8×4.3m,4.5m 面寬街屋硬切
    後段時的樣子)的正中央就是通道,9 個點全被打回 = 一間沒有餐桌的餐廳。
    車(`_park_car`)、床頭櫃(`_add_nightstands`)都踩過同一個坑。所以替代品
    必須走「沿四面牆找位置」那條路,不能又是中心原點的家具。"""
    from src.design.layout.auto_furnish import FURNITURE_PROGRAM
    from src.drafting.fixtures import _CENTER_ORIGIN, FIXTURE_SIZES
    assert ("table4", "table2") in FURNITURE_PROGRAM["dining"]
    assert "table4" in _CENTER_ORIGIN                 # 原本那張是中心原點
    assert "table2" not in _CENTER_ORIGIN             # 替代品必須是貼牆的
    assert FIXTURE_SIZES["table2"][1] < FIXTURE_SIZES["table4"][1]   # 而且更淺


@pytest.mark.parametrize("bw,bd,seed", [(4500.0, 14450.0, 7),
                                        (5450.0, 15450.0, 7),
                                        (6000.0, 15000.0, 5),
                                        (7450.0, 16450.0, 2)])
def test_shoe_cabinet_sits_by_the_front_door(bw, bd, seed):
    """★★ 進門就要有鞋櫃,而且要**在門邊**。

    ⚠️ 兩件事各修過一次:
    ① 這種房子沒有「玄關」這間房(4~8m 面寬擠不出來,真實街屋進門就是客廳),
       所以 `FURNITURE_PROGRAM["foyer"]` 從來沒有觸發過 = 全棟一個鞋櫃都沒有。
       改成**認門不認房名**(`_entry_doors`)。
    ② 位置交給擺位器挑,鞋櫃會落在離大門 3.9m 的客廳角落 —— 位置是被**門**
       決定的,要按距離排序(`_place_near_door`),而且要**先擺**(沙發電視
       先佔位的話門邊就滿了)。"""
    import math
    from src.design.layout.auto_furnish import ENTRY_FURNITURE, _entry_doors
    _lbl, spec = generate_narrow_building(bw, bd, floors=3, seed=seed)[0]
    doors = _entry_doors(spec)
    assert doors, "1F 應該找得到對外大門"
    got = [f for f in spec.fixtures
           if getattr(f, "name", "") == ENTRY_FURNITURE]
    assert len(got) == 1, got
    near = min(math.dist(got[0].insert, pt) for pt in doors.values())
    assert near <= 2500.0, near


def test_no_shoe_cabinet_in_the_garage():
    """★★ 車庫版 1F 不放鞋櫃 —— 那扇是**捲門**,鞋櫃不會擺在車頭前面。"""
    from src.design.layout.auto_furnish import ENTRY_FURNITURE
    _lbl, spec = generate_narrow_building(4500.0, 14450.0, floors=3, seed=7,
                                          garage=True)[0]
    garage = next(r for r in spec.rooms if r.kind == "garage")
    assert ENTRY_FURNITURE not in _fixtures_in(spec, garage)


@pytest.mark.parametrize("bw,bd,garage,seed", _FUR_CASES + [
    (5450.0, 15450.0, False, 7), (6000.0, 15000.0, False, 7),
    (7000.0, 16000.0, False, 7), (8000.0, 16450.0, False, 7),
])
def test_closet_and_bed_survive_the_declutter(bw, bd, garage, seed):
    """★★ 更衣室不得是空房、臥室不得沒有床 —— 補回來的機制要**每一種尺寸都成立**。

    ⚠️ 這條是拿更多尺寸釘住既有的兩條(`test_walk_in_closet_is_not_an_empty_room` /
    `test_every_bedroom_has_a_bed`):同樣的規則,只是原本那批案子剛好沒中,
    seed=7 的 5.45/6/7/8m 就會漏(3 間空更衣室、2 間沒床)。兩個根因:
      ① `restore_essentials` 只救浴室與臥室,**沒救更衣室**;
      ② **順序**:孤兒床頭櫃要先清掉床才補得回去 —— 床頭櫃是跟著床擺的,床被
         移走後那兩個小方塊還停在床頭原位,正好卡住床要回去的地方。"""
    from src.design.layout.auto_furnish import BEDS, CLOSET_FIXTURES
    for label, spec in generate_narrow_building(bw, bd, floors=3, seed=seed,
                                                garage=garage):
        for r in spec.rooms:
            got = set(_fixtures_in(spec, r))
            if r.name.startswith("更衣室"):
                assert got & set(CLOSET_FIXTURES), (label, r.name, got)
            if canonical_room(r.kind) == "bedroom":
                assert got & set(BEDS), (label, r.name, got)


@pytest.mark.parametrize("bw,bd,seed", [(6000.0, 15000.0, 7),
                                        (7000.0, 16000.0, 7)])
def test_restored_bed_gets_its_nightstands(bw, bd, seed):
    """★★ 補回來的床旁邊也要有床頭櫃(它是**新位置**,舊床頭櫃已經清掉了)。

    ⚠️ 補床頭櫃要跑在最後,而且跑完仍要再過閃柱/動線/穿牆三道修復器,
    再依「有沒有床不見了」決定整批回退 —— 不補後面那段,淺透天掃描會冒出
    `furniture_in_wall` / `circulation_blocked`(實測 2 案);補了不回退,
    動線修復器會把床搬走(實測 2 張)。**加分項不得讓原本好好的東西壞掉。**"""
    from src.design.layout.auto_furnish import BEDS
    for label, spec in generate_narrow_building(bw, bd, floors=3, seed=seed):
        for r in spec.rooms:
            if canonical_room(r.kind) != "bedroom":
                continue
            got = _fixtures_in(spec, r)
            assert set(got) & set(BEDS), (label, r.name, got)
            assert "nightstand" in got, (label, r.name, got)


# ---------------------------------------------------------------------------
# 樓梯與走道(使用者 2026-08-27:「這個樓梯做得不太對…沒有路可以到廚房」)
# ---------------------------------------------------------------------------
_PASSAGE_CASES = [(3600.0, 12500.0), (4000.0, 13450.0), (4500.0, 14450.0),
                  (5450.0, 15450.0), (6000.0, 15000.0), (8000.0, 16450.0)]


def _front_to_rear_walkable(spec) -> bool:
    """這層的前段到後段,在**樓層地板高度**走不走得通(整座樓梯都算障礙)。"""
    from shapely.geometry import box
    from shapely.ops import unary_union
    hall = [r for r in spec.rooms if r.kind == "stair_hall"]
    if not hall:
        return True
    hp = Polygon(hall[0].points)
    x0, y0, x1, y1 = hp.bounds
    solid = [Polygon(r.points) for r in spec.rooms if r is not hall[0]]
    for st in getattr(spec, "stairs", None) or []:
        ox, oy = st.origin
        solid.append(box(ox, oy, ox + st.width, oy + st.length))
    free = hp.difference(unary_union(solid)).buffer(-50).buffer(50)
    parts = [free] if free.geom_type == "Polygon" else list(free.geoms)
    parts = [p for p in parts if p.area > 1e5]
    south, north = box(x0, y0 - 10, x1, y0 + 10), box(x0, y1 - 10, x1, y1 + 10)
    if not (any(p.intersects(south) for p in parts)
            and any(p.intersects(north) for p in parts)):
        return True                      # 這層本來就不必穿過(單邊)
    return any(p.intersects(south) and p.intersects(north) for p in parts)


@pytest.mark.parametrize("bw,bd", _PASSAGE_CASES)
def test_front_and_rear_connect_without_walking_on_the_stair(bw, bd):
    """★★ 每一層的前段到後段,都要在**樓層地板高度**走得通。

    ⚠️ 折返梯中間那塊平台在**半層高**(9 階 × 188 ≈ 1.69m),不是地板 —— 門開在
    那一段等於開進一個 1.1m 深、頭頂 1.69m 的凹洞。以前 `_stair_boxes` 把它當成
    「站得住人的平地」(那句話只對**起步**平台成立),60 案掃描有 45 案的門這樣
    開、16 案前後段完全走不通。這條不看門,直接把整座樓梯當障礙問「走不走得通」。"""
    for label, spec in generate_narrow_building(bw, bd, floors=3, seed=7):
        assert _front_to_rear_walkable(spec), label


@pytest.mark.parametrize("bw,bd", [(3600.0, 12500.0), (3800.0, 12500.0),
                                   (4000.0, 13450.0), (4300.0, 13450.0),
                                   (4500.0, 14450.0), (5000.0, 14450.0)])
def test_narrow_frontage_switches_to_a_straight_flight(bw, bd):
    """★★ 3.6~5.0m 面寬改用**單跑直梯**(使用者的參考平面圖畫的就是這種)。

    折返梯要兩個梯段並排,這個面寬連法定下限(2×750+100=1600)配上最窄的浴廁
    (1200)都留不下一條**開得出門**的走道 —— 幾何上兜不攏,不是擺法的問題。
    單跑直梯只有一個梯段,省下的 0.9m 正好是那條走道。代價是踏面從 25cm 縮到
    21~22cm。

    ⚠️ 上界從 4.3m 抬到 5.0m,是走道判準改嚴的連帶結果:走道要**開得出一扇門**
    (`PASSAGE_DOOR_NEED` + 餘裕),不是「走得過去」就好。剛好夠走(750~1000)的
    走道,後段那扇門的合法窗口只剩幾十 mm,補門機制只好改走浴廁 → 動線變成
    「穿過廁所才到得了餐廚」。"""
    from src.design.layout.narrow_house import MIN_TREAD
    from src.drafting.stair import Stair, UStair
    for label, spec in generate_narrow_building(bw, bd, floors=3, seed=7):
        st = spec.stairs[0]
        assert isinstance(st, Stair) and not isinstance(st, UStair), label
        assert st.tread >= MIN_TREAD - 1e-6, (label, st.tread)   # 仍守法定下限


@pytest.mark.parametrize("bw,bd", [(5450.0, 14450.0), (6000.0, 15000.0),
                                   (8000.0, 16450.0)])
def test_wider_frontage_keeps_the_two_flight_stair(bw, bd):
    """★★ 面寬夠的時候**不要**換成直梯 —— 折返梯比較好走(踏面 25cm),而且省進深。

    釘住「換梯型只是窄面寬的備案」,不是全面改掉。分界線在 5.45m:那是第一個
    「梯段縮到 1800 之後,旁邊還留得下一條開得出門的走道」的面寬。"""
    from src.drafting.stair import UStair
    for label, spec in generate_narrow_building(bw, bd, floors=3, seed=7):
        assert isinstance(spec.stairs[0], UStair), label


def test_stair_landing_is_only_as_deep_as_it_needs_to_be():
    """★★ 折返平台**該多深就多深**,不要把樓梯間剩下的長度全吃掉。

    ⚠️ 多吃的那一截會被畫成**半層高**的平台,而不是地板 —— 實測淺透天有一案
    平台深 3.3m(真正需要 0.75m),等於白白把 3.3m 的地板變成走不上去的東西。"""
    from src.design.layout.narrow_house import STAIR_WELL_GAP, TURN_LANDING_MIN
    _lb, spec = generate_narrow_building(6000.0, 15000.0, floors=3, seed=7)[0]
    st = spec.stairs[0]
    landing = st.length - st.flight_run
    need = max(TURN_LANDING_MIN, (st.width - STAIR_WELL_GAP) / 2.0)
    assert landing == pytest.approx(need, abs=1.0), (landing, need)


def _passage_strip(spec):
    """樓梯間裡真正連通前後段的那條走道(沒有回 None)。

    做法與 `_front_to_rear_walkable` 相同:樓梯間扣掉別的房間與整座樓梯,
    剩下同時碰到南北兩端的那塊就是走道。"""
    from shapely.geometry import box
    from shapely.ops import unary_union
    hall = [r for r in spec.rooms if r.kind == "stair_hall"]
    if not hall:
        return None
    hp = Polygon(hall[0].points)
    x0, y0, x1, y1 = hp.bounds
    solid = [Polygon(r.points) for r in spec.rooms if r is not hall[0]]
    for st in getattr(spec, "stairs", None) or []:
        ox, oy = st.origin
        solid.append(box(ox, oy, ox + st.width, oy + st.length))
    free = hp.difference(unary_union(solid)).buffer(-50).buffer(50)
    parts = [free] if free.geom_type == "Polygon" else list(free.geoms)
    south, north = box(x0, y0 - 10, x1, y0 + 10), box(x0, y1 - 10, x1, y1 + 10)
    for p in parts:
        if p.area > 1e5 and p.intersects(south) and p.intersects(north):
            return p
    return None


@pytest.mark.parametrize("bw,bd", [(4500.0, 14450.0), (5450.0, 14450.0),
                                   (6000.0, 15000.0), (8000.0, 16450.0)])
def test_passage_hugs_the_party_wall(bw, bd):
    """★★ 樓梯旁那條走道要**貼著界牆**,不能夾在樓梯與浴廁中間。

    使用者 2026-08-27 指著參考平面圖:「正常的走道應該靠在旁邊的牆上,不會在
    樓梯跟廁所的中間」——參考圖畫的就是「廁所|樓梯|走道」三條並排、走道貼牆。
    做法是梯段改貼服務格那一側(`_stair(hug="west")`),走道自然落到另一側的
    界牆邊。

    另外釘住走道的**寬度判準是「開得出一扇門」**(`PASSAGE_DOOR_NEED`),
    不是「人擠得過去」——見 `test_narrow_frontage_switches_to_a_straight_flight`。
    """
    from src.design.layout.narrow_house import PASSAGE_DOOR_NEED
    from src.design.layout.plan_check import building_env
    for label, spec in generate_narrow_building(bw, bd, floors=3, seed=7):
        strip = _passage_strip(spec)
        assert strip is not None, label            # 沒走道 = 前後段走不通
        sx0, _sy0, sx1, _sy1 = strip.bounds
        ex0, _ey0, ex1, _ey1 = building_env(spec)
        # 貼牆:走道的某一側就是建築側界(容差給一個牆厚,牆中心線的關係)
        assert min(abs(sx0 - ex0), abs(sx1 - ex1)) <= 200.0, (
            label, (sx0, sx1), (ex0, ex1))
        assert sx1 - sx0 >= PASSAGE_DOOR_NEED, (label, sx1 - sx0)


@pytest.mark.parametrize("bw,bd,fl,bd_n,seed", [(4300.0, 13400.0, 2, 2, 9851),
                                                (4600.0, 15200.0, 2, 4, 6360)])
def test_doors_added_after_the_grid_still_dodge_the_columns(bw, bd, fl, bd_n, seed):
    """★★ **修門補出來的新門也要躲柱**(車庫版實測 30 案有 2 案沒躲到)。

    躲柱那一支跑在第一次修門之前,而修門會**補新的門**(接通用門、浴廁門)——
    新補的那幾扇從來沒有人問過它壓不壓柱。這是本檔「規則存在,但關卡沒接」
    的又一則:規則有、修復器有,就是沒接到補門之後那一段。"""
    from src.design.layout.plan_check import check_building
    floors = generate_narrow_building(bw, bd, floors=fl, bedrooms=bd_n,
                                      seed=seed, garage=True)
    plan = check_building(floors)
    assert "opening_on_column" not in {i.code for i in plan.errors}, (
        [str(i) for i in plan.errors])


def test_a_crowded_wall_repacks_all_its_doors_together():
    """★★ 挪不動一扇門的時候,擋住它的常常**不是柱,是旁邊那扇門**。

    一道牆上排了三扇 850 的門,最西那扇被角柱吃掉,而它東邊只有 220mm 就是下一扇
    —— 逐扇試永遠找不到位置。整排一起往東推就排得下(`_repack_openings_on_wall`)。

    ⚠️ 重排要保住三件事,少一件就整批還原:左右順序、每扇門的**鄰室**、離牆角的
    淨距。門一挪過界,那間房就變成要穿過別人家才進得去。

    ⚠️ 2026-08-28 改寫。原本是拿一組寫死的尺寸去撈「牆上剛好有三扇門」當樣本,
    幾何一動樣本就沒了(120 組隨機尺寸一個都撈不到)—— 那時這條規則等於沒人守。
    改成**在產線跑的時候攔截**:確認這件事真的還會發生,而且發生的當下每一項
    不變量都成立。第一個 assert 就是守門的:撈不到樣本要換尺寸,不是刪掉測試。
    """
    from src.design.layout import narrow_house as nh
    seen = []
    orig = nh._repack_openings_on_wall

    def spy(spec, wall, lo, hi, along):
        keys = [id(o) for o in sorted(wall.openings, key=lambda o: o.position)]
        was = [(id(o), nh._rooms_across(spec, wall, o.position), o.position)
               for o in wall.openings]
        ok = orig(spec, wall, lo, hi, along)
        if ok:
            after = sorted(wall.openings, key=lambda o: o.position)
            blocks = nh._column_blocks(spec, wall, along, 0.0)
            seen.append({
                "n": len(after),
                "order_kept": [id(o) for o in after] == keys,
                "rooms_kept": all(
                    nh._rooms_across(spec, wall, o.position) == pair
                    for o in wall.openings
                    for i, pair, _p in was if i == id(o)),
                "moved": any(o.position != p for o in wall.openings
                             for i, _pair, p in was if i == id(o)),
                "on_column": [
                    any(t0 < o.position + o.width / 2.0
                        and o.position - o.width / 2.0 < t1 for t0, t1 in blocks)
                    for o in after],
                "corner_ok": [
                    o.kind != "door" or any(
                        nh._door_pos_ok(spec, wall, o.position, o.width, c)
                        for c in (*nh.DOOR_CLEAR_STEPS, nh.DOOR_CORNER_MIN))
                    for o in after],
            })
        return ok

    nh._repack_openings_on_wall = spy
    try:
        nh.generate_narrow_building(5200.0, 14500.0, floors=3, seed=33,
                                    garage=True)
    finally:
        nh._repack_openings_on_wall = orig

    assert seen, "產線裡撈不到「整排洞口一起推」的樣本了 —— 換一組尺寸,不要刪測試"
    assert any(r["n"] >= 3 for r in seen), [r["n"] for r in seen]
    for r in seen:
        assert r["moved"], r            # 真的推了(不是原地不動也回 True)
        assert r["order_kept"], r       # 左右順序沒亂
        assert r["rooms_kept"], r       # 每個洞口的鄰室沒換
        assert not any(r["on_column"]), r       # 沒有柱再壓著
        assert all(r["corner_ok"]), r           # 門也沒變成卡在牆角


# ── 參考平面「方案 B」的中段核(使用者 2026-08-28)────────────────────────────
_REF_CASES = [(4500.0, 14450.0), (5450.0, 15450.0), (6000.0, 15000.0),
              (7000.0, 16000.0)]


@pytest.mark.parametrize("bw,bd", _REF_CASES)
def test_reference_core_passes_both_gates(bw, bd):
    """★★ 參考圖版的核也要過兩道關卡 —— 多一種排法不是多一種爛圖。"""
    from src.design.layout.code_check import check_code_building
    from src.design.layout.plan_check import check_building
    floors = generate_narrow_building(bw, bd, floors=3, seed=7,
                                      core_style="ref")
    plan, code = check_building(floors), check_code_building(floors)
    assert plan.ok, [str(i) for i in plan.errors]
    assert code.ok, [str(i) for i in code.violations]


@pytest.mark.parametrize("bw,bd", _REF_CASES)
def test_reference_core_puts_the_toilet_door_on_the_passage(bw, bd):
    """★★ 參考圖版的重點:**廁所的門開在走道上**,不是開向餐廚/車庫。

    使用者 2026-08-28 給的方案 B 就是「樓梯|天井+廁所|走道」,廁所貼著走道。
    ⚠️ 「貼著」不等於「門開在那裡」—— 光把廁所搬到走道旁邊,實測 101 個樓層
    仍有 38 個把門開向餐廚(補門機制先挑到哪一面就開哪一面)。所以
    `_bath_door_to_hall` 明講,這條測試釘的就是那一步。"""
    from src.design.layout.narrow_house import _door_kinds
    for label, spec in generate_narrow_building(bw, bd, floors=3, seed=7,
                                                core_style="ref"):
        bath = next((r for r in spec.rooms if r.kind == "bathroom"), None)
        if bath is None:
            continue
        bp = Polygon(bath.points)
        kinds = set()
        for dp in spec.doors:
            w = spec.walls[dp.wall_index]
            op = w.openings[dp.opening_index]
            if bp.exterior.distance(Point(*w.point_at(op.position))) < 50.0:
                kinds |= set(_door_kinds(spec, dp))
        assert "stair_hall" in kinds, (label, bath.name, kinds)


@pytest.mark.parametrize("bw,bd", _REF_CASES)
def test_reference_core_is_stair_then_patio_and_toilet(bw, bd):
    """★★ 參考圖版的三件事:樓梯**橫置**、天井回來、走道貼界牆跑滿核。

    橫置的意思是梯跑沿著**面寬**跑(`direction` 是 east/west),只吃掉核的一小段
    進深 —— 預設核的直梯要吃掉 3.65m,橫置只要 1.0~2.1m,省下來的給前後居室。"""
    for label, spec in generate_narrow_building(bw, bd, floors=3, seed=7,
                                                core_style="ref"):
        st = spec.stairs[0]
        assert st.direction in ("east", "west"), (label, st.direction)
        assert any(r.kind == "patio" for r in spec.rooms), label
        strip = _passage_strip(spec)
        assert strip is not None, label
        ex0, _ey0, ex1, _ey1 = building_env_of(spec)
        sx0, _sy0, sx1, _sy1 = strip.bounds
        assert min(abs(sx0 - ex0), abs(sx1 - ex1)) <= 200.0, label


def building_env_of(spec):
    from src.design.layout.plan_check import building_env
    return building_env(spec)


def test_reference_core_falls_back_when_it_does_not_fit():
    """★★ 加一種排法**不得**讓原本生得出來的案子生不出來。

    參考圖版要「橫置樓梯 + 天井 + 廁所 + 走道」四樣東西並排,最窄的面寬排不下 ——
    那時要靜靜退回預設核,不是 raise(本檔那條鐵則,這已經是第五次登場)。"""
    from src.design.layout.plan_check import check_building
    floors = generate_narrow_building(3600.0, 12500.0, floors=3, seed=7,
                                      core_style="ref")
    assert check_building(floors).ok


# ── 服務格在中間的核(使用者 2026-08-28:「還想做一款廁所門是開向走道的」)────
def _bath_door_neighbors(spec):
    """浴廁**自己那幾道牆**上的門,各通到哪一間(kind)。

    ⚠️ 不能用「離浴廁多近」去抓 —— 後段那扇開在走道上的門就貼著浴廁的轉角,
    50mm 的容差就會把它算成浴廁的門(第一版的量法就是這樣,報出來的數字沒有
    意義)。這裡只認**門洞落在浴廁邊界線上**的那幾扇。"""
    from shapely.geometry import LineString
    from src.design.layout.narrow_house import _door_kinds
    out = []
    for room in [r for r in spec.rooms if r.kind == "bathroom"]:
        bp = Polygon(room.points)
        kinds = set()
        for dp in spec.doors:
            w = spec.walls[dp.wall_index]
            op = w.openings[dp.opening_index]
            seg = LineString([w.point_at(max(0.0, op.position - op.width / 2)),
                              w.point_at(op.position + op.width / 2)])
            if bp.exterior.distance(seg.centroid) < 5.0:
                kinds |= set(_door_kinds(spec, dp)) - {"bathroom"}
        out.append((room.name, kinds))
    return out


@pytest.mark.parametrize("bw,bd", _REF_CASES)
def test_mid_core_passes_both_gates(bw, bd):
    """★★ 服務格在中間的核也要過兩道關卡。"""
    from src.design.layout.code_check import check_code_building
    from src.design.layout.plan_check import check_building
    floors = generate_narrow_building(bw, bd, floors=3, seed=7,
                                      core_style="mid")
    plan, code = check_building(floors), check_code_building(floors)
    assert plan.ok, [str(i) for i in plan.errors]
    assert code.ok, [str(i) for i in code.violations]


@pytest.mark.parametrize("bw,bd", _REF_CASES)
def test_mid_core_puts_the_toilet_door_on_the_passage(bw, bd):
    """★★ 這一版的**唯一目的**:廁所的門開在走道上,不是開向餐廚/臥室。

    預設核是「浴廁|樓梯|走道」,廁所被樓梯隔開,只剩南北兩面開得了門;把服務格
    搬到樓梯與走道**中間**,廁所的東牆就直接貼著走道。"""
    for label, spec in generate_narrow_building(bw, bd, floors=3, seed=7,
                                                core_style="mid"):
        for name, kinds in _bath_door_neighbors(spec):
            assert "stair_hall" in kinds, (label, name, kinds)


@pytest.mark.parametrize("bw,bd", _REF_CASES)
def test_mid_core_keeps_the_stair_running_along_the_depth(bw, bd):
    """★★ 這一版**不動樓梯的方向** —— 差別只有服務格搬到中間。

    (橫置樓梯是另一版 `core_style="ref"` 的事;兩版要分得開,否則使用者說
    「我要那一版」時會拿到另一版。)"""
    for label, spec in generate_narrow_building(bw, bd, floors=3, seed=7,
                                                core_style="mid"):
        assert spec.stairs[0].direction in ("north", "south"), label
        strip = _passage_strip(spec)
        assert strip is not None, label            # 前後段仍走得通


def test_mid_core_falls_back_when_it_does_not_fit():
    """★★ 加一種排法不得讓原本生得出來的案子生不出來(本檔鐵則,第六次)。"""
    from src.design.layout.plan_check import check_building
    floors = generate_narrow_building(3600.0, 12500.0, floors=3, seed=7,
                                      core_style="mid")
    assert check_building(floors).ok


def test_opening_a_patio_never_breaks_the_floor_apart():
    """★★ 開天井是**加分項**:開了出硬錯誤就不開(本檔鐵則,第八次登場)。

    天井會讓 `_core_widths` **跳過浴廁退讓**(服務格一窄,天井就小到 code_check
    不認)—— 但那個退讓正是窄面寬唯一擠得出走道的手段。3.6m 面寬開天井因此讓
    1F 斷成兩塊(客廳|浴廁|樓梯間 / 餐廚,餐廚進不去)。

    ⚠️ 本檔原本把這件事寫成「拿走道換採光」的設計取捨 —— 那對 4.5m 以上成立,
       對 3.6m 不成立:沒了走道那一層根本走不通,是廢圖不是取捨。
    ⚠️ 這個案子**蓋得出來**、只是圖不合格,所以退讓的判準是 plan_check 有沒有
       硬錯誤,不是有沒有 raise(只看例外的話這道退讓永遠不會啟動)。
    """
    from src.design.layout.plan_check import check_building
    from src.design.layout.narrow_house import NarrowVariant
    v = NarrowVariant(mirror=False, bath_north=False, open_kitchen=True,
                      entry_frac=0.22)
    floors = generate_narrow_building(3600.0, 12500.0, floors=3, bedrooms=3,
                                      variant=v, patio=True)
    plan = check_building(floors)
    assert plan.ok, [str(i) for i in plan.errors]
    # 退掉的是天井本身,不是整張圖
    assert not any(r.kind == "patio" for _lb, sp in floors for r in sp.rooms)


@pytest.mark.parametrize("bw,bd", [(4500.0, 14450.0), (5450.0, 15450.0)])
def test_a_patio_that_fits_is_kept(bw, bd):
    """★★ 退讓只在「真的壞掉」時啟動 —— 放得下的天井不准被順手退掉。

    (少了這條,`_fit_patio` 可以靠「一律不開天井」通過上面那條測試。)"""
    from src.design.layout.plan_check import check_building
    floors = generate_narrow_building(bw, bd, floors=3, seed=7, patio=True,
                                      core_style="ref")
    assert check_building(floors).ok
    assert any(r.kind == "patio" for _lb, sp in floors for r in sp.rooms)


# ── 樓梯是障礙:走不走得到,要繞過梯段來問 ──────────────────────────────────
def _walk_islands(spec, width=600.0):
    """把**梯段**當障礙,回 (從最大那塊地出發走得到的房名, 走不到的房名)。

    ⚠️ 這是使用者 2026-08-28 指著 7×12 的圖問的那件事:「這樣設計一定要走過廁所
    才能到廚房」。專案原本**沒有任何一支這樣問**:

      * `plan_check.floor_split` 一間房算**一個節點** —— 一間房被自己的樓梯切成
        兩半,它照樣算「同一塊」;
      * `room_circulation` 的障礙**只有家具** —— 它看不見樓梯。

    兩條規則都在,樓梯剛好從中間漏掉。實測窄透天預設核 96 個樓層有 38 個、
    淺基地 70 個有 26 個前後根本走不通,而 plan_check 全部給過。
    """
    from shapely.geometry import Point as _Pt
    from shapely.geometry import Polygon as _Pg
    from shapely.ops import unary_union

    from src.design.layout import room_circulation as rc
    from src.design.layout.narrow_house import _stair_boxes

    boxes = _stair_boxes(spec)
    nodes = []                                  # (房名, 一塊可走區)
    for room in spec.rooms:
        if room.kind in ("pipe_shaft", "patio"):
            continue
        poly = _Pg(room.points)
        obs = [b for b in boxes if b.intersection(poly).area > rc.INTRUDE_TOL]
        free = poly.difference(unary_union(obs)) if obs else poly
        comps = rc._components_of(free.buffer(-width / 2)) or [poly]
        nodes += [(room.name, c) for c in comps]

    pts = [_Pt(*w.point_at(op.position)) for w in spec.walls
           for op in w.openings if op.kind == "door"]
    for room in spec.rooms:
        if room.kind not in ("pipe_shaft", "patio"):
            pts += rc._open_passages(spec, _Pg(room.points))

    reach = width / 2 + 120.0
    adj = {i: set() for i in range(len(nodes))}
    for p in pts:
        touch = [i for i, (_n, c) in enumerate(nodes) if c.distance(p) <= reach]
        for a in touch:
            for b in touch:
                if a != b:
                    adj[a].add(b)
                    adj[b].add(a)

    groups, seen = [], set()
    for i in range(len(nodes)):
        if i in seen:
            continue
        grp, stack = {i}, [i]
        seen.add(i)
        while stack:
            u = stack.pop()
            for v in adj[u]:
                if v not in grp:
                    grp.add(v)
                    seen.add(v)
                    stack.append(v)
        groups.append(grp)
    groups.sort(key=lambda g: -sum(nodes[i][1].area for i in g))
    home = {nodes[i][0] for i in groups[0]}
    lost = sorted({nodes[i][0] for g in groups[1:] for i in g} - home)
    return sorted(home), lost


@pytest.mark.parametrize("bw,bd,style", [
    (7000.0, 12000.0, "default"),      # ← 使用者指出的那一張(1F 到不了餐廚)
    (6300.0, 13900.0, "default"),
    (4000.0, 11400.0, "default"),
    (7500.0, 13000.0, "default"),
    (5450.0, 15450.0, "mid"),
    (5450.0, 15450.0, "ref"),
])
def test_every_room_is_reachable_without_walking_over_the_stairs(bw, bd, style):
    """★★★ 每一間房都走得到 —— 而且**不准踩過樓梯**。

    使用者 2026-08-28:「這樣設計一定要走過廁所才能到廚房,是不對的。」
    實測 7×12 的 1F:客廳的門開在樓梯東側的走道上,餐廚的門卻開在樓梯**西側**
    浴廁北邊那塊死角(三面是浴廁/梯段/外牆),兩邊只隔著梯段旁 75mm 的縫 ——
    人根本過不去,而 plan_check 給過。
    """
    for _lb, spec in generate_narrow_building(bw, bd, floors=3, seed=0,
                                              core_style=style):
        _home, lost = _walk_islands(spec)
        assert lost == [], (_lb, lost)


def test_the_stair_itself_counts_as_an_obstacle():
    """★★ 關卡真的看得見樓梯 —— 梯段撐滿樓梯間時,動線檢查要抓得到。

    ⚠️ 少了這條,上面那條測試可以靠「產生器剛好沒犯錯」通過,而關卡其實還是瞎的。
    `room_circulation` 原本的障礙只有家具,把梯段加寬到擋住所有門也一聲不吭。"""
    spec = generate_narrow_building(7000.0, 12000.0, floors=2, seed=0)[0][1]
    assert analyze_room_circulation(spec).ok         # 修好之後本來就該過
    hall = next(r for r in spec.rooms if r.kind == "stair_hall")
    x0, _y0, x1, _y1 = Polygon(hall.points).bounds
    spec.stairs[0].width = (x1 - x0) - 200.0         # 梯段撐滿樓梯間
    rep = analyze_room_circulation(spec)
    assert not rep.ok
    assert any(r.kind == "stair_hall" for r in rep.blocked), rep.summary()


def test_the_core_never_traps_the_bathroom():
    """★★ 空格併進樓梯間之後,浴室不准被關在梯段盡頭那塊死角裡。

    浴廁在南時空格落在服務格**北**端,而那一端是梯段的盡頭(折返平台在半層高、
    直梯只剩一道 75mm 的牆縫)。預設核的浴室又被梯段隔開、只剩南北兩面開得了門
    → 門一定落進死角。退讓分兩級:先把浴廁翻到北邊(空格改落在起步平台那一側,
    走得到),翻不動才退回「空格併進居室」。

    ⚠️ 判準是 `room_circulation` **真的量出有門走不到**,不是「核裡有沒有死角」——
    一塊沒困住任何人的空地只是浪費坪效(與 `room_circulation` 同一條分界)。
    拿死角當觸發條件的那一版,把沒事的 6×15 也退掉了,連帶弄丟廚房的流理台、
    浴室的淋浴間與床頭櫃 —— **退讓有代價,不該白付**。"""
    for bw, bd in [(4500.0, 14500.0), (5100.0, 14600.0), (5200.0, 14100.0),
                   (6000.0, 15000.0)]:
        for _lb, spec in generate_narrow_building(bw, bd, floors=3, seed=0):
            rep = analyze_room_circulation(spec)
            assert rep.ok, (bw, bd, _lb, rep.summary())
            _home, lost = _walk_islands(spec)
            assert lost == [], (bw, bd, _lb, lost)


def test_a_garage_floor_bathroom_is_reachable():
    """★★ 車庫版也一樣:1F 的浴廁不能被關在梯段盡頭那塊死角裡。

    這一版原本是靠 `repair_doors` 為了 `through_bedroom` 補一扇門進去「修好」的
    —— 補一扇**走不到**的門不叫修好,只是把問題從左手換到右手。"""
    for bw, bd in [(4400.0, 13800.0), (4300.0, 14000.0), (5300.0, 15200.0)]:
        for _lb, spec in generate_narrow_building(bw, bd, floors=3, seed=0,
                                                  garage=True):
            _home, lost = _walk_islands(spec)
            assert lost == [], (bw, bd, _lb, lost)
