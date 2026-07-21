"""格局基準測試(benchmark.py)的煙霧測試——確保巡檢台本身不會壞。

不算 PNG(render=False,測試要快),只驗證:案例能跑、五面向量測都在、
生成失敗的案例被好好接住、報告 HTML 產得出來。實際的「引擎好不好」由
跑 benchmark 產生的報告人看,不在這裡斷言(那是會變動的品質指標,不是
不變的契約)。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.design import benchmark as bm
from src.design.layout_generator import HouseBrief, generate_floor_plan


def test_case_matrix_has_at_least_30():
    """使用者要求:一次至少 30 組。"""
    assert len(bm.CASES) >= 30
    assert len({c.cid for c in bm.CASES}) == len(bm.CASES)   # cid 不重複


def test_run_single_case_produces_all_criteria(tmp_path):
    case = next(c for c in bm.CASES if c.cid == "S04")       # 16×14 三房
    r = bm.run_case(case, tmp_path, render=False)
    assert r["generated"] is True
    assert set(r["criteria"]) == {
        "generation", "dxf", "rooms", "circulation", "furniture"}
    assert r["status"] in ("pass", "warn", "fail")
    # DXF 真的存了、重讀得回來、有實體。
    fl = r["floors_detail"][0]
    assert (tmp_path / fl["dxf"]).is_file()
    assert fl["dxf_check"]["status"] == "pass"
    assert fl["dxf_check"]["entities"] > 0


def test_building_case_reports_metrics_and_alignment(tmp_path):
    case = next(c for c in bm.CASES if c.cid == "B01")       # 透天三層+車庫
    r = bm.run_case(case, tmp_path, render=False)
    assert r["generated"] is True
    assert r["metrics"] is not None and r["metrics"]["far_pct"] > 0
    assert r["column_aligned"] is True                       # 柱位上下對齊
    labels = [fl["label"] for fl in r["floors_detail"]]
    assert "B1F" in labels and "1F" in labels                # 有地下層與地上層


def test_generation_failure_is_captured_not_raised(tmp_path):
    """基地太小的多樓層透天:應被接住成 generated=False,不是拋例外。"""
    bad = bm.Case("X99", "太小透天", "building", w=12, d=10, beds=3)
    r = bm.run_case(bad, tmp_path, render=False)
    assert r["generated"] is False
    assert r["error"] and r["criteria"]["generation"] == "fail"
    assert r["status"] == "fail"


def test_room_and_furniture_checks_on_valid_spec():
    spec = generate_floor_plan(HouseBrief(site_width=16000, site_depth=14000,
                                          bedrooms=3))
    rooms = bm.check_rooms(spec)
    assert rooms["status"] in ("pass", "warn")
    assert not rooms["broken"]                               # 不該有排壞的房間
    assert any(rm["name"] == "主臥室" for rm in rooms["rooms"])
    # 主臥用 master_bedroom 的範圍量(不會被次臥 20m² 上限誤判)。
    master = next(rm for rm in rooms["rooms"] if rm["name"] == "主臥室")
    assert master["area_ok"]

    furn = bm.check_furniture(spec)
    assert furn["status"] == "pass"                          # 每間該有的家具都在
    assert furn["fixture_count"] > 0

    circ = bm.check_circulation(spec)
    assert circ["status"] == "pass"                          # 生成成功者動線應無問題


def test_report_html_renders(tmp_path):
    payload = bm.run_benchmark(bm.CASES[:2], out_dir=tmp_path, render=False)
    assert (tmp_path / "benchmark.json").is_file()
    html = (tmp_path / "report.html").read_text(encoding="utf-8")
    assert "<!doctype html>" in html.lower()
    assert "Layout Benchmark" in html
    assert payload["summary"]["total"] == 2
