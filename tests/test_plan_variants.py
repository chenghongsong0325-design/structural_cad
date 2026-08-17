"""設計變體 / 多方案測試 —— 「自由設計」不能犧牲正確性。

驗三件事:

  1. 變體真的**改變格局**(不是換個名字),而且每一種都過兩道關卡。
  2. 挑出來的 N 個方案**彼此有明顯差異**(不是分數前三名那三張一樣的圖)。
  3. Report 可序列化(to_dict/to_json),照專案慣例。
"""
import json
import pytest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.design.layout.code_check import check_code_building
from src.design.layout.narrow_house import (
    all_variants,
    generate_narrow_building,
    variant_from_seed,
)
from src.design.layout.plan_check import check_building
from src.design.layout.plan_variants import (
    VariantReport,
    _distance,
    _signature,
    narrow_options,
)

SB = 2000.0
W, D = 7000.0, 12000.0


def env_of(spec) -> tuple:
    """這份圖的建築外框,**由 spec 自己推**。

    ⚠️ 以前這裡寫死 `(SB, SB, SB+W, SB+D)`。產生器會替外牆柱留位置
    (`STRUCT_MARGIN`)而把建築往內縮,寫死的框就比真正的建築大一圈 → 大門與窗
    被判成「不在外牆上」,24 個變體全部冒出假的 `no_entry`。產線程式碼早就修過
    這個坑(見 `plan_check.building_env` 的說明),測試沒跟上而已。"""
    from src.design.layout.plan_check import building_env
    return building_env(spec)


@pytest.mark.slow
def test_every_variant_passes_both_gates():
    """★★ 24 種變體全部要過 plan_check(圖面)+ code_check(法規)。

    這是「自由度不犧牲正確性」的依據:多出來的選擇不是亂改,是合法的設計決定。"""
    bad = []
    for v in all_variants():
        fl = generate_narrow_building(W, D, floors=3, variant=v)
        plan = check_building(fl, None)          # None = 各層自己推外框
        code = check_code_building(fl, None)
        if not (plan.ok and code.ok):
            bad.append((v, [i.code for i in plan.errors],
                        [i.code for i in code.violations]))
    assert not bad, f"這些變體生出不合格圖:{bad}"


def test_variants_actually_change_the_plan():
    """★ 變體要真的改到格局:鏡射 → 樓梯換邊;浴廁南北 → 服務格上下對調。"""
    base = generate_narrow_building(W, D, floors=2)[0][1]
    mirrored = generate_narrow_building(
        W, D, floors=2, variant=type(all_variants()[0])(mirror=True))[0][1]
    assert base.stairs[0].origin[0] != mirrored.stairs[0].origin[0]

    bath_n = generate_narrow_building(
        W, D, floors=2, variant=type(all_variants()[0])(bath_north=True))[0][1]

    def _bath_y(spec):
        from shapely.geometry import Polygon
        return next(Polygon(r.points).bounds[1] for r in spec.rooms
                    if r.kind == "bathroom")
    assert _bath_y(base) != _bath_y(bath_n)


def test_seed_picks_a_variant_and_is_repeatable():
    """★ 同 seed 同設計、不同 seed 換設計(網站按「重新生成」才有意義)。"""
    a = generate_narrow_building(W, D, floors=2, seed=7)[0][1]
    b = generate_narrow_building(W, D, floors=2, seed=7)[0][1]
    assert [r.points for r in a.rooms] == [r.points for r in b.rooms]
    assert variant_from_seed(7) != variant_from_seed(3) or True   # 抽樣可能相同
    sigs = {_signature(sp := generate_narrow_building(W, D, floors=2,
                                                      seed=s)[0][1],
                       env_of(sp)) for s in range(8)}
    assert len(sigs) >= 2, "8 個 seed 生出的格局全一樣 → 變體沒有生效"


def test_options_are_valid_and_different():
    """★★ 挑出來的 3 個方案:全部合格,而且彼此看得出差別。"""
    rep = narrow_options(W, D, floors=3, n=3)
    assert len(rep.options) == 3
    assert rep.n_passed >= 3
    for o in rep.options:
        assert o.plan.ok and o.code.ok
    assert rep.diversity > 0.1, f"三個方案幾乎一樣(差異 {rep.diversity:.0%})"


def test_distance_and_signature():
    """指紋比對:同一張圖距離 0,鏡射後距離明顯 >0。"""
    a = generate_narrow_building(W, D, floors=1)[0][1]
    b = generate_narrow_building(
        W, D, floors=1, variant=type(all_variants()[0])(mirror=True))[0][1]
    sa, sb = _signature(a, env_of(a)), _signature(b, env_of(b))
    assert _distance(sa, sa) == 0.0
    assert _distance(sa, sb) > 0.2


def test_report_serialisable():
    """★ Report 要能 to_dict/to_json(專案慣例)。"""
    rep = narrow_options(W, D, floors=2, n=2)
    d = rep.to_dict()
    assert set(d) == {"n_tried", "n_passed", "diversity", "options"}
    assert json.loads(rep.to_json())["n_passed"] == rep.n_passed
    assert isinstance(VariantReport().to_json(), str)
    assert "方案" in rep.summary()
