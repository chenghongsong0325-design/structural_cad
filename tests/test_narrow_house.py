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
    kinds = [r.kind for r in generate_narrow_house(W, D).rooms]
    assert kinds == ["living", "bathroom", "storage", "stair_hall",
                     "dining", "kitchen"]


def test_1f_has_front_entry_door():
    """★ 臨路大門:1F 南向外牆有一扇門。"""
    spec = generate_narrow_house(W, D)
    south = [w for w in spec.walls
             if abs(w.start[1] - spec.setback) < 50
             and abs(w.end[1] - spec.setback) < 50
             and abs(w.start[0] - w.end[0]) > 1]
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
    spec = generate_narrow_house(W, D)
    polys = [Polygon(r.points) for r in spec.rooms]
    for i in range(len(polys)):
        for j in range(i + 1, len(polys)):
            assert polys[i].intersection(polys[j]).area < 1e4
    total = sum(p.area for p in polys)
    assert abs(total - W * D) / (W * D) < 0.02
