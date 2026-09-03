"""法規檢查器(建築技術規則)+ 產線合規測試。

`plan_check` 驗「圖畫得對不對」,這一組驗「合不合法規尺寸」:

  * 檢查器抓得到人為破壞(把樓梯改陡、把窗封小)。
  * violation / warning 分界正確(法規 vs 設計慣例)。
  * Report 可序列化(to_dict/to_json),照專案慣例。
  * **產線掃描**:規則版窄透天在定義域內不得有法規違規。
"""
import json
import pytest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.design.layout.code_check import (
    CodeCheckReport,
    check_code_building,
    check_code_floor,
)
from src.design.layout.narrow_house import generate_narrow_building

SB = 2000.0
W, D = 7000.0, 12000.0
ENV = (SB, SB, SB + W, SB + D)


def _floors(bw=W, bd=D, n=3, core_style=None):
    return generate_narrow_building(bw, bd, floors=n, core_style=core_style)


# ── 產線合規 ────────────────────────────────────────────────────────────────
@pytest.mark.slow
def test_rule_pipeline_is_code_compliant():
    """★ 規則版窄透天(面寬 5~7m × 進深 11~14m)不得有法規違規。

    ⚠️ 外框讓 spec 自推(不傳 env):基地深過上限時建築會封頂留院子,拿基地框
    去檢查會把窗誤判成不在外牆上。"""
    bad = []
    for bw in (5000.0, 6000.0, 7000.0):
        for bd in (11000.0, 12000.0, 14000.0):
            rep = check_code_building(generate_narrow_building(bw, bd, floors=3))
            if not rep.ok:
                bad.append((bw, bd, [i.code for i in rep.violations]))
    assert not bad, f"這些尺寸不合法規:{bad}"


def test_stair_meets_code_dimensions():
    """★ §33:級高 ≤20cm、級深 ≥21cm、梯段淨寬 ≥75cm、**平臺深 ≥梯段寬**。

    平臺那條最容易漏:梯段變寬(樓梯間變寬)時平臺要跟著加深,否則轉身空間不足。

    ⚠️ 三款核都要問(2026-08-31):它們的梯型不一樣(預設核順著進深跑、參考圖版
    橫置、窄面寬會換單跑直梯),而 §33 對哪一種都一樣要守。單跑直梯沒有
    `steps_per_flight`,總級數要分開取 —— 只問折返梯的話,橫置直梯那一半的案子
    等於沒人檢查。"""
    from src.design.layout.narrow_house import FLOOR_HEIGHT
    for style in ("default", "mid", "ref"):
        for _lb, spec in _floors(core_style=style):
            st = spec.stairs[0]
            total = getattr(st, "steps_per_flight", 0) * 2 or st.steps
            assert FLOOR_HEIGHT / total <= 200.0, style
            assert st.tread >= 210.0, style
            # 單跑直梯只有一個梯段 → 梯段寬就是 `width`;折返梯的 `width` 是
            # 兩段加中間的梯井,單段寬要問 `flight_width`。
            fw = getattr(st, "flight_width", st.width)
            assert fw >= 750.0, style
            if hasattr(st, "steps_per_flight"):      # 折返端平台才有這條
                assert st.landing_depth >= fw - 1e-6, style


def test_daylight_area_meets_one_eighth():
    """★ §40:居室採光開口 ≥ 樓地板面積 1/8(窗高以 1.2m 估)。

    以前每間房只補一扇 1.2m 寬的窗(1.44㎡),27㎡ 的客廳要 3.4㎡ —— 差一倍多。"""
    from src.design.layout.code_check import (
        DAYLIGHT_RATIO, HABITABLE_KINDS, WINDOW_H_ASSUMED,
    )
    from shapely.geometry import Point, Polygon
    for _lb, spec in _floors():
        for room in spec.rooms:
            if room.kind not in HABITABLE_KINDS:
                continue
            poly = Polygon(room.points)
            got = sum(op.width for w in spec.walls for op in w.openings
                      if op.kind == "window"
                      and poly.exterior.distance(
                          Point(*w.point_at(op.position))) < 60)
            need = poly.area * DAYLIGHT_RATIO / WINDOW_H_ASSUMED
            assert got >= need - 1.0, (room.name, got, need)


# ── 檢查器本身抓不抓得到 ────────────────────────────────────────────────────
def test_detects_steep_stair():
    """★ 人為把樓梯改陡(級數砍半)→ 檢查器必須抓到級高超標。

    ⚠️ 明寫預設核:`steps_per_flight` 是**折返梯**才有的欄位,自動挑到橫置直梯
    的話這個破壞根本做不出來(測試會變成 AttributeError,不是「抓不抓得到」)。"""
    floors = _floors(n=2, core_style="default")
    spec = floors[0][1]
    spec.stairs[0].steps_per_flight = 4              # 8 級爬 3.2m = 每階 400mm
    codes = {i.code for i in check_code_floor(spec, ENV, 1, "1F")}
    assert "stair_riser" in codes


def test_detects_small_window():
    """★ 人為把窗封到剩一點點 → 檢查器必須抓到採光不足。"""
    floors = _floors(n=2)
    spec = floors[0][1]
    for w in spec.walls:
        for op in w.openings:
            if op.kind == "window":
                op.width = 600.0
    codes = {i.code for i in check_code_floor(spec, ENV, 1, "1F")}
    assert "daylight_area" in codes


def test_violation_vs_warning_split():
    """★ 有條號的是法規(violation);沒條號的是設計慣例(warning)。"""
    rep = check_code_building(_floors(), ENV)
    for i in rep.issues:
        if i.severity == "violation":
            assert i.article, f"{i.code} 是法規就要有條號"
        else:
            assert not i.article, f"{i.code} 沒條號就不該算違規"


def test_report_serialisable():
    """★ Report 要能 to_dict/to_json(專案慣例)。"""
    rep = check_code_building(_floors(), ENV)
    d = rep.to_dict()
    assert set(d) == {"ok", "n_violations", "n_warnings", "issues"}
    assert json.loads(rep.to_json())["ok"] == rep.ok
    assert isinstance(CodeCheckReport().summary(), str)
