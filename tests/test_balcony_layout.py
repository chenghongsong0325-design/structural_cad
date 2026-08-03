"""陽台配置測試(Phase 11)—— 「圖上有陽台」而且「陽台是對的」。

畫陽台的零件(矮牆/欄杆/文字)早就有測試(test_balcony_elevator);這一組驗的是
**規則**:誰該有陽台、多大、門開在哪、算不算居室,以及檢查器抓不抓得到壞陽台。

  1. 只有二樓以上有;一樓臨路面是大門/前院。
  2. 尺寸照規範:進深 1.2~2.0m、寬度 ≥ 服務房間外牆的一半、院子還留得下退讓。
  3. 每座陽台都有一扇**落地橫拉門**,而且門在陽台範圍內(推開門踩得到地板)。
  4. 陽台不是居室:不進 spec.rooms、不改建築外框、不吃室內樓地板。
  5. 加了陽台之後**兩道關卡仍全過**(這是最重要的一條:陽台不能是採光的代價)。
  6. 檢查器抓得到人為破壞:拆掉門 → balcony_no_door;搬走陽台 → entry_upstairs。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from shapely.geometry import Polygon

from src.design.layout.balcony import (
    MAX_DEPTH,
    MIN_DEPTH,
    WIDTH_RATIO,
    BalconyReport,
    balcony_doors,
    balcony_polygon,
    balcony_report,
)
from src.design.layout.code_check import check_code_building
from src.design.layout.narrow_house import all_variants, generate_narrow_building
from src.design.layout.plan_check import building_env, check_building, check_floor
from src.design.layout.shallow_house import generate_shallow_building


def _narrow(w=7000.0, d=12000.0, n=3):
    return generate_narrow_building(w, d, floors=n)


# ── 誰該有陽台 ──────────────────────────────────────────────────────────────
def test_first_floor_has_none_and_upper_floors_have_them():
    """★ 一樓臨路面是大門/車庫/前院,不做陽台;二樓以上前後各一座。"""
    floors = _narrow()
    assert floors[0][1].balconies == []
    for _lb, spec in floors[1:]:
        assert len(spec.balconies) == 2          # 前(南)+ 後(北)
        assert {b.attach for b in spec.balconies} == {"north", "south"}


def test_shallow_house_gets_a_front_balcony():
    """★ 淺進深透天:北側是梯帶(樓梯間不配陽台)→ 只有南向前陽台。"""
    floors = generate_shallow_building(6000, 5500, floors=3)
    assert floors[0][1].balconies == []
    for _lb, spec in floors[1:]:
        assert len(spec.balconies) == 1
        assert spec.balconies[0].attach == "north"   # 北邊貼建築 = 往南挑出


def test_balconies_stack_between_floors():
    """★ 各層陽台上下對齊(懸挑要落在同一組結構上,不能一層一個位置)。"""
    floors = _narrow(n=4)
    shapes = {tuple(sorted((b.origin, b.width, b.depth, b.attach)
                           for b in spec.balconies))
              for _lb, spec in floors[1:]}
    assert len(shapes) == 1, f"陽台沒有上下對齊:{shapes}"


# ── 尺寸規範 ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("bw,bd", [(3500.0, 10000.0), (5000.0, 11000.0),
                                   (7000.0, 12000.0), (6000.0, 14000.0)])
def test_size_follows_the_rule(bw, bd):
    """★★ 進深 1.2~2.0m、寬度 ≥ 服務房間外牆的一半、且不壓到地界線。"""
    floors = generate_narrow_building(bw, bd, floors=2)
    spec = floors[1][1]
    env = building_env(spec)
    ys = [p[1] for p in spec.site_boundary]
    for bal in spec.balconies:
        assert MIN_DEPTH - 1e-6 <= bal.depth <= MAX_DEPTH + 1e-6
        poly = balcony_polygon(bal)
        x0, y0, x1, y1 = poly.bounds
        assert min(ys) <= y0 and y1 <= max(ys)       # 沒有超出地界線
        # 服務的那間房:貼著這座陽台的居室
        host = next(r for r in spec.rooms
                    if Polygon(r.points).distance(poly) < 60
                    and r.kind in ("bedroom", "master_bedroom", "living"))
        hx0, _hy0, hx1, _hy1 = Polygon(host.points).bounds
        assert bal.width >= (hx1 - hx0) * WIDTH_RATIO - 1e-6


def test_balcony_does_not_eat_the_rooms():
    """★★ 挑出式:陽台在建築外牆之外 → 房間、建築外框都不受影響。

    這是選「懸挑」而不是「內凹」最重要的理由:居室不會被切小,§40 的採光需求
    也不會跟著變,所以補陽台不必動任何一條既有的尺寸下限。"""
    with_bal = _narrow()[1][1]
    env = building_env(with_bal)
    assert with_bal.balconies                       # 真的有陽台
    for bal in with_bal.balconies:                  # 每座都完全落在建築外框之外
        x0, y0, x1, y1 = balcony_polygon(bal).bounds
        assert y1 <= env[1] + 1 or y0 >= env[3] - 1
    # 陽台不是房間:不進 spec.rooms,面積也不算進樓地板
    assert not [r for r in with_bal.rooms if r.kind == "balcony"]
    assert sum(r.area_m2 for r in with_bal.rooms) == pytest.approx(
        (env[2] - env[0]) * (env[3] - env[1]) / 1e6)


# ── 門 ──────────────────────────────────────────────────────────────────────
def test_every_balcony_has_a_sliding_door_within_its_span():
    """★★ 門要在陽台上(推開門踩得到地板),而且是橫拉門。

    平開門的門扇會掃掉 1.2m 深陽台的一半,真實圖遇到這種情形就是畫拉門。"""
    for _lb, spec in _narrow()[1:]:
        for bal in spec.balconies:
            doors = balcony_doors(spec, bal)
            assert doors, "陽台沒有門"
            for w, op in doors:
                px, _py = w.point_at(op.position)
                x0, _y0, x1, _y1 = balcony_polygon(bal).bounds
                assert x0 - 60 <= px <= x1 + 60
                assert op.width >= 900              # 對外門淨寬下限
            dp = next(d for d in spec.doors
                      if spec.walls[d.wall_index].openings[d.opening_index]
                      is doors[0][1])
            assert dp.door.sliding, "陽台門要畫成橫拉門"


def test_apartment_units_can_reach_their_balcony():
    """★★ 集合住宅每戶的工作陽台也要有門。

    這是本階段抓出來的舊缺陷:陽台早就畫在圖上,但北牆只有一扇窗——住戶只能
    從窗戶爬出去曬衣服。"""
    from src.design.layout_generator import CorridorBrief, generate_floor_plan

    spec = generate_floor_plan(CorridorBrief(units_per_row=4))
    assert len(spec.balconies) == 8
    for bal in spec.balconies:
        assert balcony_doors(spec, bal), "集合住宅的陽台沒有門"
    assert check_building([("3F", spec)]).ok
    assert check_code_building([("3F", spec)]).ok


def test_balcony_door_counts_as_daylight_opening():
    """★ §40 講「採光用窗**或開口**」:落地玻璃門是採光開口,要算進去。

    不算的話,4m 面寬的套房北牆得同時塞 1.9m 的窗和 0.9m 的門,只剩 10cm 牆垛
    ——那不是設計問題,是判準漏了一項。"""
    from src.drafting.unit import one_room_unit

    unit = one_room_unit()
    north = unit.walls[1]
    win = sum(op.width for op in north.openings if op.kind == "window")
    door = sum(op.width for op in north.openings if op.kind == "door")
    assert door > 0 and win > 0
    living = next(r for r in unit.rooms if r.kind == "living")
    need = Polygon(living.points).area / 8 / 1200.0      # §40,窗高以 1.2m 估
    assert win < need <= win + door                      # 只有窗不夠、加上門才夠


# ── 加了陽台之後,兩道關卡仍全過 ────────────────────────────────────────────
@pytest.mark.parametrize("bw", [3500.0, 5000.0, 7000.0])
@pytest.mark.parametrize("bd", [10000.0, 12000.0, 15000.0])
@pytest.mark.slow
def test_narrow_domain_still_passes_both_gates(bw, bd):
    """★★ 窄透天定義域:配了陽台之後 plan_check + code_check 仍零錯誤。"""
    if bd < (10500.0 if bw > 6000.0 else 9500.0):
        pytest.skip("低於該面寬的進深下限")
    floors = generate_narrow_building(bw, bd, floors=3)
    plan, code = check_building(floors), check_code_building(floors)
    assert plan.ok, [(i.code, i.floor, i.room) for i in plan.errors]
    assert code.ok, [(i.code, i.floor, i.room) for i in code.violations]
    assert sum(len(s.balconies) for _l, s in floors) > 0


@pytest.mark.slow
def test_all_design_variants_keep_the_balcony_under_its_door():
    """★★ 24 種設計變體(含東西鏡射)每一種都合格、且每層都配得出陽台。

    鏡射是最容易出錯的一步:整層翻面時陽台若沒跟著翻,門就會開到空中。"""
    bad = []
    for v in all_variants():
        floors = generate_narrow_building(6000, 12000, floors=3, variant=v)
        rep = check_building(floors)
        if not rep.ok or sum(len(s.balconies) for _l, s in floors) != 4:
            bad.append((v, [i.code for i in rep.errors]))
    assert not bad, bad


# ── 檢查器抓不抓得到壞陽台 ──────────────────────────────────────────────────
def test_detects_balcony_without_door():
    """★ 人為拆掉陽台門 → 必須抓到「陽台進不去」。"""
    spec = _narrow()[1][1]
    bal = spec.balconies[0]
    w, op = balcony_doors(spec, bal)[0]
    assert not [i for i in check_floor(spec, None, 2, "2F")
                if i.code == "balcony_no_door"]          # 原本是乾淨的
    w.openings.remove(op)
    codes = [i.code for i in check_floor(spec, None, 2, "2F")]
    assert "balcony_no_door" in codes


def test_upstairs_door_without_balcony_is_still_an_error():
    """★★ 豁免只給「門外真的有陽台」的情形:陽台搬走 → 回到 entry_upstairs。

    這條確認 balcony 的豁免沒有把「樓上外牆開門」這個硬規則整個打掉。"""
    spec = _narrow()[1][1]
    assert not [i for i in check_floor(spec, None, 2, "2F")
                if i.code == "entry_upstairs"]
    spec.balconies = []
    codes = [i.code for i in check_floor(spec, None, 2, "2F")]
    assert "entry_upstairs" in codes
    assert "balcony_no_door" not in codes                # 陽台都沒了就不該再報這條


# ── 報表 ────────────────────────────────────────────────────────────────────
def test_report_serialisable():
    """★ Report 要能 to_dict/to_json(專案慣例)。"""
    rep = balcony_report(_narrow())
    d = rep.to_dict()
    assert set(d) == {"n_balconies", "n_skipped", "items", "skipped"}
    assert d["n_balconies"] == 4
    assert json.loads(rep.to_json())["n_balconies"] == 4
    assert isinstance(BalconyReport().summary(), str)
    assert all(i["door_width"] >= 900 for i in d["items"])
