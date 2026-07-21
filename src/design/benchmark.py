"""格局基準測試(Layout Benchmark)—— 一次生成幾十組住宅,量化驗證引擎品質。

改造 Layout Engine(F3 自適應房間尺寸)之後,單元測試守的是「不回歸」,但
使用者要的是另一個問題的答案:**這個引擎現在到底穩不穩?** 換一批沒見過的
基地尺寸,能不能都生出合理的圖?哪些會漏氣?本模組把使用者列的五個驗收
面向變成可量測、可彙整的一份報告:

    1. 不同基地是否都能生成合理平面   → 生成成功率(失敗的記原因)
    2. DXF 是否正常                    → 存檔 + 重讀 + 實體/圖層數
    3. 房間比例是否合理                → 面積落在程式範圍內、長寬比不過細
    4. 動線是否合理                    → validate_spec 動線檢核 + 走道/門
    5. 家具是否放得下                  → validate_spec 不重疊/不擋門 + 每室該有的家具

輸出(output/benchmark/):
    * benchmark.json  —— 每個案例的完整量測(可程式再處理)
    * <case>/*.dxf    —— 每層一張 DXF(可用 AutoCAD 開)
    * <case>/*.png    —— 每層一張預覽圖(matplotlib 算圖)
    * report.html     —— 一份自包含的 Layout Report(縮圖內嵌,可直接開)

⚠️ 定位:這是「工程用的巡檢台」,不是產品功能——跑一次看引擎哪裡歪,不是
每次出圖都跑。案例矩陣(CASES)刻意鋪開小/中/大/深/寬基地與 1~4 房、單層/
多樓層/地下室/書房/孝親房/方位約束,專挑會逼出邊界的組合。

    python -m src.design.benchmark            # 跑全部,寫 json+dxf+png+report
    python -m src.design.benchmark --limit 6  # 只跑前 6 組(開發時快速看)
"""
from __future__ import annotations

import argparse
import base64
import json
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from dataclasses import replace

from shapely.geometry import Point as SPoint
from shapely.geometry import Polygon

from src.design.building_generator import (
    BuildingBrief,
    check_column_alignment,
    generate_building,
)
from src.design.layout_generator import (
    HouseBrief,
    generate_floor_plan,
    validate_spec,
)
from src.design.metrics import building_metrics
from src.design.room_program import AREA_TOLERANCE, requirement
from src.drafting.apartment_plan import draw_floor_plan
from src.drafting.fixtures import Counter
from src.web.render import _new_doc

OUT_DIR = _PROJECT_ROOT / "output" / "benchmark"

# 要評估「房間比例」的用途類型(其餘如玄關/走道/天井/樓梯/停車不評——它們
# 本來就不是方正的居室,或尺寸由動線/設備決定而非面積程式)。
_RATED_KINDS = {"bedroom", "living", "dining", "kitchen", "study",
                "bathroom", "storage"}
# 各用途「該有的家具」(家具的插入點落在該房間內才算數)。用來抓「validate
# 過了、但這間房其實空著」的漏放——validate 只查不重疊/不擋門,不查「有沒有」。
_EXPECTED_FIXTURES = {
    "bedroom": ("bed_single", "bed_double"),
    "bathroom": ("toilet",),
    "living": ("sofa3",),
}
# 動線類的檢核訊息關鍵字(從 validate_spec 的問題清單裡挑出動線相關的)。
_CIRCULATION_HINTS = ("門", "走道", "樓梯", "玄關", "採光")


# ---------------------------------------------------------------------------
# 案例矩陣 —— 至少 30 組,鋪開會逼出邊界的組合
# ---------------------------------------------------------------------------
@dataclass
class Case:
    """一個基準案例:人看的名稱 + 產生 brief 需要的參數。"""

    cid: str
    name: str
    kind: str                       # "single"(單層)/ "building"(多樓層)
    w: float                        # 基地寬(m)
    d: float                        # 基地深(m)
    beds: int = 3
    floors: int = 3
    basements: int = 0
    study: bool = False
    elder: bool = False
    car: int = 0
    master_corner: Optional[str] = None
    kitchen_side: Optional[str] = None
    seed: int = 0
    note: str = ""                  # 這組想測什麼

    def house_brief(self) -> HouseBrief:
        return HouseBrief(
            site_width=self.w * 1000, site_depth=self.d * 1000,
            bedrooms=self.beds, want_study=self.study,
            want_elder_room=self.elder, car_spaces=self.car,
            master_corner=self.master_corner, kitchen_side=self.kitchen_side,
            seed=self.seed)

    def build(self):
        """→ ("single", HouseBrief) 或 ("building", BuildingBrief)。"""
        hb = self.house_brief()
        if self.kind == "single":
            return "single", hb
        diff = self.floors > 1 or self.basements > 0
        return "building", BuildingBrief(
            typical=hb, floors=self.floors, basements=self.basements,
            differentiated=diff)


def _single(cid, name, w, d, beds, **kw) -> Case:
    return Case(cid, name, "single", w, d, beds=beds, **kw)


def _bldg(cid, name, w, d, beds, floors=3, basements=1, **kw) -> Case:
    return Case(cid, name, "building", w, d, beds=beds,
                floors=floors, basements=basements, **kw)


# 深度刻意多半 ≤17m(維持兩帶式,即目前要穩定的核心);少數 19~22m 進天井版,
# 讓報告也照得到那條路(使用者要先穩定兩帶式,再回頭做天井版)。
CASES: list[Case] = [
    # ── 單層住宅(_generate_house 路徑)────────────────────────────────
    _single("S01", "小宅一房", 12, 11, 1, note="最小可行基地"),
    _single("S02", "小宅兩房", 14, 12, 2, note="餐廳應併入客廳"),
    _single("S03", "標準兩房", 16, 13, 2),
    _single("S04", "標準三房", 16, 14, 3, note="經典戶型"),
    _single("S05", "三房加寬", 18, 13, 3),
    _single("S06", "三房方正", 20, 14, 3),
    _single("S07", "四房", 22, 15, 4),
    _single("S08", "四房大宅", 24, 16, 4),
    _single("S09", "三房加書房", 22, 15, 3, study=True, note="+1格需較寬基地"),
    _single("S10", "三房加孝親房", 20, 15, 3, elder=True),
    _single("S11", "主臥西南/廚房北", 18, 14, 3,
            master_corner="SW", kitchen_side="N", note="方位約束"),
    _single("S12", "寬基地三房", 30, 16, 3, note="房間到頂→留側院"),
    # ── 多樓層透天(generate_building 路徑)────────────────────────────
    _bldg("B01", "透天三層三房+車庫", 19, 13, 3),
    _bldg("B02", "透天三層三房", 20, 13, 3),
    _bldg("B03", "透天兩層兩房", 18, 13, 2, floors=2),
    _bldg("B04", "透天三層三房", 22, 14, 3),
    _bldg("B05", "透天三層四房", 24, 16, 4),
    _bldg("B06", "透天四層三房", 20, 13, 3, floors=4),
    _bldg("B07", "透天三層大宅", 26, 16, 3, note="大基地兩帶界線"),
    _bldg("B08", "透天三層加書房", 22, 14, 3, study=True),
    _bldg("B09", "透天三層加孝親房", 20, 13, 3, elder=True),
    _bldg("B10", "透天三層雙車位", 20, 20, 3, car=2, note="車位需深基地(車長4.8m)"),
    _bldg("B11", "透天四房加寬", 30, 13, 4, note="寬→柱網跨數"),
    _bldg("B12", "透天主臥西南", 19, 13, 3, master_corner="SW"),
    _bldg("B13", "透天三層 seed7", 21, 14, 3, seed=7, note="設計變體"),
    _bldg("B14", "透天三層 seed42", 19, 13, 3, seed=42, note="設計變體"),
    _bldg("B15", "透天三層四房", 22, 16, 4),
    _bldg("B16", "透天三層三房", 24, 15, 3),
    _bldg("B17", "透天三層三房", 21, 15, 3, seed=3),
    _bldg("B18", "透天三層三房", 23, 14, 3, seed=11),
    # ── 深基地(天井版,少量覆蓋)──────────────────────────────────────
    _bldg("P01", "深基地天井三房", 19, 19, 3, note="天井版"),
    _bldg("P02", "深基地天井三房", 20, 20, 3, note="天井版"),
    _bldg("P03", "深大基地中庭", 26, 22, 3, note="中庭版"),
    _bldg("P04", "深基地天井四房", 22, 20, 4, note="天井版"),
]


# ---------------------------------------------------------------------------
# 出圖:一份 ezdxf 文件 → PNG(matplotlib 算圖,白底黑線)
# ---------------------------------------------------------------------------
def render_png(doc, path: Path, dpi: int = 100) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from ezdxf.addons.drawing import Frontend, RenderContext
    from ezdxf.addons.drawing.config import (
        BackgroundPolicy,
        ColorPolicy,
        Configuration,
    )
    from ezdxf.addons.drawing.matplotlib import MatplotlibBackend

    cfg = Configuration(lineweight_scaling=20,
                        background_policy=BackgroundPolicy.WHITE,
                        color_policy=ColorPolicy.BLACK)
    fig = plt.figure(figsize=(8, 8))
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_axis_off()
    Frontend(RenderContext(doc), MatplotlibBackend(ax), config=cfg).draw_layout(
        doc.modelspace(), finalize=True)
    fig.savefig(path, dpi=dpi, facecolor="white")
    plt.close(fig)


def _floor_docs(label: str, spec):
    """一層樓的兩份 ezdxf 文件:下載版(含圖框/表格)+ 預覽版(去圖框)。"""
    doc, layers = _new_doc()
    draw_floor_plan(doc.modelspace(), replace(spec, schedules=True), layers)
    pdoc, players = _new_doc()
    draw_floor_plan(pdoc.modelspace(),
                    replace(spec, sheet=False, title_block=None), players)
    return doc, pdoc


# ---------------------------------------------------------------------------
# 五個面向的量測
# ---------------------------------------------------------------------------
def _bbox_aspect(points) -> float:
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    w, h = max(xs) - min(xs), max(ys) - min(ys)
    lo = min(w, h)
    return (max(w, h) / lo) if lo > 0 else float("inf")


def _req_for(room) -> "object":
    """房間 → 面積程式需求。所有臥室的 kind 都是 "bedroom",但主臥(名字含
    「主臥」)的合理範圍比次臥大,要另取 master_bedroom;否則會拿次臥的
    max(20m²)去量主臥,把正常的大主臥誤判成過大。"""
    if room.kind == "bedroom" and "主臥" in room.name:
        return requirement("master_bedroom")
    return requirement(room.kind)


# 只有這幾種「有意義上限」的房間才查過大——它們是使用者明確給了 max_area、
# F3 想擋住「變成剩餘空間垃圾桶」的主角(主臥/次臥、客廳/起居、獨立餐廳)。
# 儲藏室/機房/書房是刻意的餘量吸納空間,本來就該能大;廚房大一點是奢侈不是
# 缺陷——都不查過大,免得報告被無意義的黃燈淹沒。
_OVERSIZE_KINDS = {"bedroom", "living", "dining"}
# 併合/開放式的房名(客餐廳 = 客+餐、餐廚 = 餐+廚):面積本來就是兩間相加,
# 拿單間的上限去量不公平,不查過大。
_MERGED_HINTS = ("客餐", "餐廚")
# 客廳/餐廳可以是開放式長條(長寬比較大是常態);超過這個才算真的過細。
# 其餘居室(臥室/衛浴/廚房)用各自 aspect_max 的 1.1 倍(它們實測都 ≤3.3)。
_LONG_ROOM_ASPECT = 4.5


def check_rooms(spec) -> dict:
    """房間比例:每間居室的面積 vs 程式範圍、長寬比;彙整超標清單。

    嚴重度:面積偏離或長寬比過細一律「warn」——設計品質提醒,不是壞圖
    (validate_spec 已擋掉真正不能用的)。只有小到不足下限一半(排壞了)
    才「fail」。查什麼、不查什麼見上面常數的說明(刻意避開餘量吸納空間,
    否則報告會被儲藏室/機房的「過大」黃燈灌爆,看不出真正的問題)。"""
    rooms = []
    undersized, oversized, narrow, broken = [], [], [], []
    for r in spec.rooms:
        if r.kind not in _RATED_KINDS:
            continue
        req = _req_for(r)
        area = r.area_m2
        aspect = _bbox_aspect(r.points)
        merged = any(h in r.name for h in _MERGED_HINTS)
        lo_ok = area >= req.min_area * (1 - AREA_TOLERANCE)
        check_hi = (r.kind in _OVERSIZE_KINDS and not merged
                    and req.max_area is not None)
        hi_ok = (not check_hi) or area <= req.max_area * (1 + AREA_TOLERANCE)
        asp_cap = (_LONG_ROOM_ASPECT if r.kind in ("living", "dining")
                   else req.aspect_max * 1.10)
        asp_ok = aspect <= asp_cap
        rooms.append({
            "name": r.name, "kind": r.kind, "area_m2": round(area, 1),
            "aspect": round(aspect, 2),
            "area_ok": lo_ok and hi_ok, "aspect_ok": asp_ok,
        })
        if area < req.min_area * 0.5:
            broken.append(f"{r.name} {area:.1f}m²<<{req.min_area}")
        elif not lo_ok:
            undersized.append(f"{r.name} {area:.1f}m²<{req.min_area}")
        if not hi_ok:
            oversized.append(f"{r.name} {area:.1f}m²>{req.max_area}")
        if not asp_ok:
            narrow.append(f"{r.name} 長寬比{aspect:.1f}")
    if broken:
        status = "fail"                            # 小到不足下限一半 = 排壞了
    elif undersized or oversized or narrow:
        status = "warn"                            # 偏離理想範圍 = 品質提醒
    else:
        status = "pass"
    return {"status": status, "rooms": rooms, "broken": broken,
            "undersized": undersized, "oversized": oversized, "narrow": narrow}


def _fixture_in(fx, poly: Polygon) -> bool:
    pt = ((fx.start[0] + fx.end[0]) / 2, (fx.start[1] + fx.end[1]) / 2) \
        if isinstance(fx, Counter) else fx.insert
    return poly.contains(SPoint(pt)) or poly.boundary.distance(SPoint(pt)) < 200


def check_furniture(spec) -> dict:
    """家具:每間該有家具的房間是否真的擺了(插入點落在房內);+ 總數。"""
    missing = []
    count = len(spec.fixtures)
    for r in spec.rooms:
        want = _EXPECTED_FIXTURES.get(r.kind)
        if not want:
            continue
        poly = Polygon(r.points)
        has = any(
            (not isinstance(fx, Counter) and fx.name in want and _fixture_in(fx, poly))
            for fx in spec.fixtures)
        # 浴室的洗手台也要在(toilet 已在 want,再單獨確認 basin)。
        if r.kind == "bathroom":
            has = has and any(
                not isinstance(fx, Counter) and fx.name == "basin"
                and _fixture_in(fx, poly) for fx in spec.fixtures)
        if not has:
            missing.append(f"{r.name} 缺{'/'.join(want)}")
    return {"status": "fail" if missing else "pass",
            "fixture_count": count, "missing": missing}


def check_circulation(spec) -> dict:
    """動線:重跑 validate_spec,挑出動線相關的問題(生成成功者理應為空,
    這裡做獨立複驗——引擎的守門與基準的判準各自算一次,對得起來才安心)。"""
    problems = validate_spec(spec)
    circ = [p for p in problems if any(h in p for h in _CIRCULATION_HINTS)]
    other = [p for p in problems if p not in circ]
    has_corridor = any(r.kind == "corridor" for r in spec.rooms)
    return {"status": "fail" if circ else "pass",
            "problems": circ, "other_problems": other,
            "has_corridor": has_corridor}


def check_overflow(spec) -> dict:
    """Living Overflow 產物:客廳實際長寬比 + 這層切出的溢位房間(家庭廳/多功能/
    書房/儲藏,由 Program Selector 決定)。給修改前/後比較「客廳是否還過細長、
    多的空間變成什麼」。"""
    living = [r for r in spec.rooms if r.kind == "living"]
    aspects = []
    for r in living:
        xs = [p[0] for p in r.points]
        ys = [p[1] for p in r.points]
        w, d = max(xs) - min(xs), max(ys) - min(ys)
        if min(w, d) > 0:
            aspects.append(round(max(w, d) / min(w, d), 2))
    # 溢位房間:kind=family 一定是溢位;study/storage 可能是溢位或既有,列出供人看。
    overflow_rooms = [
        {"name": r.name, "kind": r.kind, "area_m2": round(r.area_m2, 1)}
        for r in spec.rooms if r.kind == "family"]
    return {"living_aspect": max(aspects) if aspects else None,
            "overflow_rooms": overflow_rooms}


def check_dxf(path: Path) -> dict:
    """DXF 是否正常:存在、非空、ezdxf 重讀得回來、有實體與圖層。"""
    import ezdxf
    if not path.is_file() or path.stat().st_size == 0:
        return {"status": "fail", "reason": "檔案不存在或為空"}
    try:
        doc = ezdxf.readfile(path)
        n_ent = len(list(doc.modelspace()))
        n_layer = len(doc.layers)
    except Exception as exc:                       # noqa: BLE001
        return {"status": "fail", "reason": f"ezdxf 讀取失敗:{exc}"}
    ok = n_ent > 0 and n_layer > 0
    return {"status": "pass" if ok else "fail",
            "size_kb": path.stat().st_size // 1024,
            "entities": n_ent, "layers": n_layer}


# ---------------------------------------------------------------------------
# 跑一個案例
# ---------------------------------------------------------------------------
def _worst(*statuses) -> str:
    order = {"pass": 0, "warn": 1, "fail": 2}
    return max(statuses, key=lambda s: order.get(s, 0))


def run_case(case: Case, out_dir: Path, render: bool = True) -> dict:
    """生成 → 逐層存 DXF/PNG → 五面向量測 → 回一份結果 dict。"""
    kind, brief = case.build()
    res: dict = {
        "cid": case.cid, "name": case.name, "kind": case.kind,
        "site": f"{case.w:g}×{case.d:g}m", "beds": case.beds,
        "floors": case.floors if case.kind == "building" else 1,
        "basements": case.basements if case.kind == "building" else 0,
        "note": case.note, "seed": case.seed,
    }
    case_dir = out_dir / case.cid
    t0 = time.time()
    try:
        if kind == "single":
            spec = generate_floor_plan(brief)
            floor_specs = [("1F", spec)]
            res["metrics"] = None
            res["column_aligned"] = None
        else:
            building = generate_building(brief)
            floor_specs = [(fl.label, fl.spec) for fl in building.floors]
            res["metrics"] = building_metrics(building)
            res["column_aligned"] = not check_column_alignment(building)
    except Exception as exc:                        # noqa: BLE001
        res["generated"] = False
        res["error"] = str(exc)
        res["error_type"] = type(exc).__name__
        res["criteria"] = {"generation": "fail"}
        res["status"] = "fail"
        res["seconds"] = round(time.time() - t0, 2)
        return res

    res["generated"] = True
    case_dir.mkdir(parents=True, exist_ok=True)

    floors_out, dxf_status, room_status, circ_status, furn_status = [], [], [], [], []
    thumb_b64 = None
    for label, spec in floor_specs:
        dxf_path = case_dir / f"{label}.dxf"
        png_path = case_dir / f"{label}.png"
        doc, pdoc = _floor_docs(label, spec)
        doc.saveas(dxf_path)
        if render:
            render_png(pdoc, png_path)

        dxf = check_dxf(dxf_path)
        rooms = check_rooms(spec)
        circ = check_circulation(spec)
        furn = check_furniture(spec)
        overflow = check_overflow(spec)
        dxf_status.append(dxf["status"])
        room_status.append(rooms["status"])
        circ_status.append(circ["status"])
        furn_status.append(furn["status"])
        # 代表縮圖:多樓層取 1F(生活主層),單層取該層。
        if thumb_b64 is None and render and (label == "1F" or case.kind == "single"):
            thumb_b64 = base64.b64encode(png_path.read_bytes()).decode()
        floors_out.append({
            "label": label,
            "dxf": f"{case.cid}/{label}.dxf",
            "png": f"{case.cid}/{label}.png",
            "dxf_check": dxf, "rooms": rooms,
            "circulation": circ, "furniture": furn, "overflow": overflow,
        })
    if thumb_b64 is None and render and floors_out:      # 沒有 1F(理論上不會)
        first_png = case_dir / f"{floor_specs[0][0]}.png"
        if first_png.is_file():
            thumb_b64 = base64.b64encode(first_png.read_bytes()).decode()

    res["floors_detail"] = floors_out
    res["thumb"] = thumb_b64
    res["criteria"] = {
        "generation": "pass",
        "dxf": _worst(*dxf_status),
        "rooms": _worst(*room_status),
        "circulation": _worst(*circ_status),
        "furniture": _worst(*furn_status),
    }
    if res["column_aligned"] is False:
        res["criteria"]["circulation"] = "fail"     # 柱位對不齊 = 結構問題,併入報告
    res["status"] = _worst(*res["criteria"].values())
    res["seconds"] = round(time.time() - t0, 2)
    return res


# ---------------------------------------------------------------------------
# 跑全部 + 產報告
# ---------------------------------------------------------------------------
def run_benchmark(cases: list[Case], out_dir: Path = OUT_DIR,
                  render: bool = True) -> dict:
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    results = []
    for i, case in enumerate(cases, 1):
        print(f"[{i:>2}/{len(cases)}] {case.cid} {case.name} "
              f"({case.w:g}×{case.d:g}m) …", end="", flush=True)
        r = run_case(case, out_dir, render=render)
        print(f" {r['status'].upper()} ({r['seconds']}s)")
        results.append(r)

    summary = _summarize(results)
    payload = {"summary": summary, "cases": results}
    (out_dir / "benchmark.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "report.html").write_text(
        _render_report(payload), encoding="utf-8")
    return payload


def _summarize(results: list[dict]) -> dict:
    crit_keys = ["generation", "dxf", "rooms", "circulation", "furniture"]
    tally = {k: {"pass": 0, "warn": 0, "fail": 0} for k in crit_keys}
    for r in results:
        for k in crit_keys:
            v = r.get("criteria", {}).get(k)
            if v in tally[k]:
                tally[k][v] += 1
    status_tally = {"pass": 0, "warn": 0, "fail": 0}
    for r in results:
        status_tally[r["status"]] = status_tally.get(r["status"], 0) + 1

    # 系統性發現:同一種品質問題橫跨幾個案例 → 當「重點結論」放報告最上面,
    # 才不會被 30 張卡片各自的小黃燈埋掉(哪些要優先改,一眼看得出來)。
    # living_long 專指「客廳/起居室本身過細長」(benchmark 首要問題,Living
    # Overflow 要修的);跟一般 narrow(可能是衛浴細長條等)分開,才看得出成效。
    flag_cases = {"living_long": [], "broken": [], "undersized": [],
                  "oversized": [], "narrow": []}
    for r in results:
        if not r.get("generated"):
            continue
        for k in ("broken", "undersized", "oversized", "narrow"):
            if any(fl["rooms"].get(k) for fl in r.get("floors_detail", [])):
                flag_cases[k].append(r["cid"])
        if any((fl.get("overflow", {}).get("living_aspect") or 0) > 2.5
               for fl in r.get("floors_detail", [])):
            flag_cases["living_long"].append(r["cid"])
    return {
        "total": len(results),
        "generated": sum(1 for r in results if r.get("generated")),
        "status": status_tally,
        "criteria": tally,
        "flag_cases": flag_cases,
        "failures": [{"cid": r["cid"], "name": r["name"],
                      "error": r.get("error")}
                     for r in results if not r.get("generated")],
    }


# ---------------------------------------------------------------------------
# HTML 報告(自包含:縮圖以 base64 內嵌,單一檔案可攜)
# ---------------------------------------------------------------------------
_BADGE = {"pass": ("✓", "#1a7f37", "#dafbe1"),
          "warn": ("!", "#9a6700", "#fff8c5"),
          "fail": ("✕", "#cf222e", "#ffebe9")}
_CRIT_LABEL = {"generation": "生成", "dxf": "DXF",
               "rooms": "房間比例", "circulation": "動線", "furniture": "家具"}


def _badge(status: str) -> str:
    sym, fg, bg = _BADGE.get(status, ("?", "#57606a", "#eee"))
    return (f'<span class="badge" style="color:{fg};background:{bg}">'
            f'{sym} {status}</span>')


def _bar(tally: dict) -> str:
    total = sum(tally.values()) or 1
    seg = []
    for st in ("pass", "warn", "fail"):
        n = tally.get(st, 0)
        if n:
            _, fg, _ = _BADGE[st]
            seg.append(f'<span style="width:{n/total*100:.1f}%;background:{fg}" '
                       f'title="{st} {n}"></span>')
    return f'<div class="bar">{"".join(seg)}</div>'


def _render_report(payload: dict) -> str:
    s = payload["summary"]
    cases = payload["cases"]
    st = s["status"]

    crit_rows = "".join(
        f'<tr><td>{_CRIT_LABEL[k]}</td>'
        f'<td class="num ok">{v["pass"]}</td>'
        f'<td class="num wa">{v["warn"]}</td>'
        f'<td class="num fa">{v["fail"]}</td>'
        f'<td style="width:180px">{_bar(v)}</td></tr>'
        for k, v in s["criteria"].items())

    _FIND_LABEL = {
        "living_long": "客廳/起居室過細長(長寬比 >2.5;天井/孝親版不在 Overflow 範圍)",
        "broken": "房間排壞(不足下限一半)",
        "undersized": "房間偏小(低於程式下限)",
        "oversized": "主要房間過大(超過上限)",
        "narrow": "房間細長(含衛浴等長條;長寬比 >上限)",
    }
    find_items = []
    for k in ("living_long", "broken", "undersized", "oversized", "narrow"):
        cids = s.get("flag_cases", {}).get(k, [])
        if cids:
            find_items.append(
                f'<li><b>{_FIND_LABEL[k]}</b>:{len(cids)} 案 '
                f'<span class="mono">{", ".join(cids)}</span></li>')
    findings = (f'<div class="findings"><h3>重點結論</h3><ul>{"".join(find_items)}'
                f'</ul><p class="note">上列為橫跨多案的系統性品質提醒;'
                f'皆通過 validate_spec(不是壞圖),是「下一步該優化什麼」的指路牌。'
                f'</p></div>') if find_items else ''

    cards = []
    for r in cases:
        crits = "".join(
            f'<span class="chip">{_CRIT_LABEL[k]} {_badge(v)}</span>'
            for k, v in r.get("criteria", {}).items())
        if r.get("generated"):
            m = r.get("metrics")
            meta = (f'{r["site"]} · {r["beds"]}房 · '
                    f'{"單層" if r["kind"]=="single" else f"{r["floors"]}層"}'
                    + (f'+地下{r["basements"]}' if r["basements"] else ''))
            if m:
                meta += (f' · 建蔽{m["coverage_pct"]}% 容積{m["far_pct"]}%'
                         f' · {m["total_ping"]}坪')
            img = (f'<img src="data:image/png;base64,{r["thumb"]}" alt="{r["cid"]}">'
                   if r.get("thumb") else '<div class="noimg">(未算圖)</div>')
            flags = []
            for fl in r.get("floors_detail", []):
                for key in ("broken", "oversized", "undersized", "narrow"):
                    for it in fl["rooms"].get(key, []):
                        flags.append(f'{fl["label"]} {it}')
                for it in fl["furniture"].get("missing", []):
                    flags.append(f'{fl["label"]} {it}')
                for it in fl["circulation"].get("problems", []):
                    flags.append(f'{fl["label"]} ⚠ {it}')
            flag_html = ('<ul class="flags">'
                         + "".join(f'<li>{f}</li>' for f in flags[:8])
                         + ('<li>…</li>' if len(flags) > 8 else '')
                         + '</ul>') if flags else '<div class="clean">無異常</div>'
            # 客廳最大長寬比 + Living Overflow 切出的房間(給「首要問題」追蹤)。
            asp = max((fl["overflow"]["living_aspect"] or 0
                       for fl in r.get("floors_detail", [])), default=0)
            ovs = []
            for fl in r.get("floors_detail", []):
                for o in fl["overflow"].get("overflow_rooms", []):
                    ovs.append(f'{fl["label"]} {o["name"]}({o["area_m2"]:.0f}㎡)')
            ov_html = (f'<div class="ovf">客廳長寬比 {asp:.2f}'
                       + (' · 溢位:' + '、'.join(dict.fromkeys(ovs)) if ovs else '')
                       + '</div>')
            body = f'{img}<div class="crits">{crits}</div>{ov_html}{flag_html}'
        else:
            meta = f'{r["site"]} · {r["beds"]}房 — 生成失敗'
            body = (f'<div class="err">{r.get("error","")}</div>'
                    f'<div class="crits">{crits}</div>')
        cards.append(
            f'<div class="card {r["status"]}">'
            f'<div class="chead">{_badge(r["status"])} '
            f'<b>{r["cid"]}</b> {r["name"]}'
            + (f' <span class="tag">{r["note"]}</span>' if r["note"] else '')
            + f'</div><div class="cmeta">{meta}</div>{body}</div>')

    return _HTML_TEMPLATE.format(
        total=s["total"], generated=s["generated"],
        n_pass=st.get("pass", 0), n_warn=st.get("warn", 0), n_fail=st.get("fail", 0),
        crit_rows=crit_rows, findings=findings, cards="\n".join(cards),
        ts=time.strftime("%Y-%m-%d %H:%M"))


_HTML_TEMPLATE = """<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Layout Benchmark 報告</title><style>
:root{{--bg:#fff;--fg:#1f2328;--mut:#57606a;--line:#d0d7de;--card:#f6f8fa}}
@media(prefers-color-scheme:dark){{:root{{--bg:#0d1117;--fg:#e6edf3;--mut:#8b949e;--line:#30363d;--card:#161b22}}}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--fg);
font-family:-apple-system,"Segoe UI","Microsoft JhengHei",sans-serif;line-height:1.5}}
.wrap{{max-width:1200px;margin:0 auto;padding:24px}}
h1{{font-size:22px;margin:0 0 4px}}.sub{{color:var(--mut);font-size:13px;margin-bottom:20px}}
.dash{{display:flex;gap:16px;flex-wrap:wrap;margin-bottom:20px}}
.stat{{background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:14px 20px;min-width:120px}}
.stat b{{font-size:28px;display:block}}.stat span{{color:var(--mut);font-size:12px}}
table{{border-collapse:collapse;width:100%;margin-bottom:24px;font-size:14px}}
th,td{{text-align:left;padding:7px 10px;border-bottom:1px solid var(--line)}}
td.num{{text-align:right;font-variant-numeric:tabular-nums;width:52px}}
.ok{{color:#1a7f37}}.wa{{color:#9a6700}}.fa{{color:#cf222e}}
.bar{{display:flex;height:8px;border-radius:4px;overflow:hidden;background:var(--line)}}
.bar span{{display:block}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:16px}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:12px;overflow:hidden}}
.card.fail{{border-color:#cf222e}}.card.warn{{border-color:#d4a72c}}
.chead{{font-size:14px;margin-bottom:2px}}.chead b{{font-family:monospace}}
.cmeta{{color:var(--mut);font-size:12px;margin-bottom:8px}}
.card img{{width:100%;border-radius:6px;background:#fff;border:1px solid var(--line)}}
.noimg{{padding:40px;text-align:center;color:var(--mut);background:var(--bg);border-radius:6px}}
.crits{{display:flex;flex-wrap:wrap;gap:4px;margin:8px 0}}
.chip{{font-size:11px;color:var(--mut)}}
.badge{{display:inline-block;padding:1px 6px;border-radius:20px;font-size:11px;font-weight:600}}
.tag{{font-size:11px;background:var(--line);padding:1px 6px;border-radius:4px;color:var(--mut)}}
.flags{{margin:4px 0 0;padding-left:18px;font-size:12px;color:#9a6700}}
.ovf{{font-size:12px;color:var(--mut);margin:4px 0}}
.clean{{font-size:12px;color:#1a7f37}}
.err{{font-size:12px;color:#cf222e;background:var(--bg);padding:8px;border-radius:6px;
white-space:pre-wrap}}
.findings{{background:var(--card);border:1px solid var(--line);border-left:4px solid #9a6700;
border-radius:8px;padding:4px 18px 12px;margin-bottom:24px}}
.findings h3{{margin:12px 0 6px;font-size:15px}}
.findings ul{{margin:0;padding-left:20px;font-size:14px}}.findings li{{margin:3px 0}}
.findings .note{{color:var(--mut);font-size:12px;margin:8px 0 0}}
.mono{{font-family:monospace;color:var(--mut);font-size:12px}}
</style></head><body><div class="wrap">
<h1>Layout Benchmark 報告</h1>
<div class="sub">自動建築平面圖生成器 · 格局引擎巡檢 · {ts}</div>
<div class="dash">
<div class="stat"><b>{total}</b><span>總案例</span></div>
<div class="stat"><b class="ok">{generated}</b><span>成功生成</span></div>
<div class="stat"><b class="ok">{n_pass}</b><span>全數通過</span></div>
<div class="stat"><b class="wa">{n_warn}</b><span>有警告</span></div>
<div class="stat"><b class="fa">{n_fail}</b><span>有失敗</span></div>
</div>
<table><thead><tr><th>驗收面向</th><th class="num ok">通過</th>
<th class="num wa">警告</th><th class="num fa">失敗</th><th>分布</th></tr></thead>
<tbody>{crit_rows}</tbody></table>
{findings}
<div class="grid">{cards}</div>
</div></body></html>"""


def main() -> None:
    ap = argparse.ArgumentParser(description="Layout Benchmark")
    ap.add_argument("--limit", type=int, default=0,
                    help="只跑前 N 組(0=全部)")
    ap.add_argument("--no-render", action="store_true",
                    help="不算 PNG(只出 DXF/JSON,快)")
    args = ap.parse_args()

    cases = CASES[:args.limit] if args.limit else CASES
    payload = run_benchmark(cases, render=not args.no_render)
    s = payload["summary"]
    print(f"\n=== 完成:{s['generated']}/{s['total']} 生成成功 · "
          f"通過 {s['status'].get('pass',0)} / 警告 {s['status'].get('warn',0)} / "
          f"失敗 {s['status'].get('fail',0)} ===")
    print(f"報告:{(OUT_DIR / 'report.html')}")
    for f in s["failures"]:
        print(f"  ✕ {f['cid']} {f['name']}: {f['error']}")


if __name__ == "__main__":
    main()
