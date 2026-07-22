"""Report 序列化契約測試(v0.7)。

慣例:每個 Report 都要有 `to_dict()`(純原生型別)與 `to_json()`(建在
to_dict 之上)。這裡對**所有**分析層的 Report 一起驗,避免日後新增模組漏做。

重點不是「方法存在」,而是**真的 json.dumps 得出來**——這個 repo 的三個陷阱
(shapely 物件、set、int-key dict)只有實際序列化才抓得到。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.design.collision import CollisionEngine
from src.design.connectivity import analyze_connectivity, build_graphs
from src.design.corridor import analyze_corridors
from src.design.layout_generator import (
    HouseBrief,
    generate_floor_plan,
    generate_house_upper,
)
from src.design.layout_validation import LayoutIssue, LayoutReport, validate_layout
from src.design.report import JsonReport


def _spec():
    return generate_floor_plan(
        HouseBrief(site_width=16000, site_depth=14000, bedrooms=3))


def _all_reports():
    """四個分析層的 Report + 兩個巢狀結構。"""
    spec = _spec()
    return [
        ("LayoutReport", validate_layout(spec)),
        ("ConnectivityReport", analyze_connectivity(spec)),
        ("ConnectivityGraphs", build_graphs(spec)),
        ("CorridorReport", analyze_corridors(spec)),
        ("ResolveReport", CollisionEngine(spec).resolve()),
    ]


def _assert_json_native(obj, where=""):
    """遞迴確認只有 JSON 原生型別(抓 shapely / set / tuple / int-key)。"""
    if isinstance(obj, dict):
        for k, v in obj.items():
            assert isinstance(k, str), f"{where}: dict key 不是 str → {k!r}"
            _assert_json_native(v, f"{where}.{k}")
    elif isinstance(obj, list):
        for n, v in enumerate(obj):
            _assert_json_native(v, f"{where}[{n}]")
    else:
        assert obj is None or isinstance(obj, (str, int, float, bool)), \
            f"{where}: 非原生型別 {type(obj).__name__}"


def test_every_report_implements_the_contract():
    """★ 慣例:每個 Report 都是 JsonReport,且有 to_dict / to_json。"""
    for name, rep in _all_reports():
        assert isinstance(rep, JsonReport), f"{name} 未繼承 JsonReport"
        assert callable(rep.to_dict) and callable(rep.to_json)
        assert isinstance(rep.to_dict(), dict), f"{name}.to_dict() 不是 dict"


def test_every_report_is_json_serialisable():
    """★ 真的 json.dumps 得出來(不是只有方法存在)。"""
    for name, rep in _all_reports():
        text = rep.to_json()
        assert isinstance(text, str) and text.strip()
        restored = json.loads(text)                 # 能還原 = 真的合法 JSON
        assert restored == rep.to_dict(), f"{name} round-trip 不一致"


def test_report_dicts_contain_only_native_types():
    """遞迴檢查:沒有 shapely 物件、沒有 set、沒有 int 當 key。"""
    for name, rep in _all_reports():
        _assert_json_native(rep.to_dict(), name)


def test_to_json_keeps_chinese_readable():
    """中文房名不可被轉成 \\uXXXX(ensure_ascii=False)。"""
    rep = validate_layout(_spec())
    assert "\\u" not in rep.to_json()
    conn = analyze_connectivity(_spec())
    assert any("一" <= ch <= "鿿" for ch in conn.to_json())


def test_to_json_indent_is_configurable():
    rep = validate_layout(_spec())
    assert "\n" in rep.to_json()                    # 預設 indent=2
    assert "\n" not in rep.to_json(indent=None)     # 壓成單行


# ── 各 Report 的內容正確性 ────────────────────────────────────────────────
def test_layout_report_dict_shape():
    spec = _spec()
    rep = validate_layout(spec)
    d = rep.to_dict()
    assert d["ok"] is True and d["issues"] == []
    assert d["rooms"] == len(spec.rooms) and d["doors"] == len(spec.doors)
    assert d["error_count"] == 0 and d["warning_count"] == 0


def test_layout_issue_serialises():
    d = LayoutIssue("polygon", "error", "壞掉").to_dict()
    assert d == {"check": "polygon", "severity": "error", "message": "壞掉"}
    assert json.loads(LayoutReport(issues=[LayoutIssue(
        "overlap", "warn", "x")]).to_json())["issues"][0]["severity"] == "warn"


def test_connectivity_graphs_use_edge_lists():
    """圖用 edge list 表示,不是 int-key dict(避開序列化陷阱)。"""
    g = build_graphs(_spec())
    d = g.to_dict()
    assert isinstance(d["adjacency"], list) and isinstance(d["room_edges"], list)
    assert all(len(e) == 2 for e in d["adjacency"])
    assert all(set(e) >= {"a", "b", "link"} for e in d["room_edges"])
    assert all(e["a"] < e["b"] for e in d["room_edges"])      # 無向邊只列一次
    assert len(d["rooms"]) == len(g.names)
    assert all(dr["role"] in ("interior", "exterior", "orphan")
               for dr in d["doors"])


def test_connectivity_report_embeds_graphs():
    d = analyze_connectivity(_spec()).to_dict()
    assert d["ok"] is True and d["entrance"]
    assert "graphs" in d and d["graphs"]["rooms"]


def test_corridor_report_dict_shape():
    spec = generate_house_upper(
        HouseBrief(site_width=26000, site_depth=16000, bedrooms=3, seed=1))
    d = analyze_corridors(spec).to_dict()
    assert d["ok"] is True
    assert d["longest_room"] and d["longest_distance"] > 0
    assert d["longest_path"][0] == d["entrance"]
    assert isinstance(d["walking_distance"], dict)
    assert all(isinstance(k, str) for k in d["walking_distance"])


def test_resolve_report_dict_shape():
    """合格圖上 resolve 是 no-op → changed False、各清單為空。"""
    d = CollisionEngine(_spec()).resolve().to_dict()
    assert d["changed"] is False
    assert d["moved"] == [] and d["dropped"] == []
    assert d["unresolved_column"] == []


def test_base_class_requires_to_dict():
    """沒實作 to_dict() 的子類別要明確報錯,不能默默回怪東西。"""
    class Bad(JsonReport):
        pass

    try:
        Bad().to_dict()
    except NotImplementedError as exc:
        assert "to_dict" in str(exc)
    else:                                            # pragma: no cover
        raise AssertionError("應該要丟 NotImplementedError")
