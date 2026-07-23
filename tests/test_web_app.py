"""網頁後端(src/web/app.py)的單元測試。

驗證重點:
  1. /api/generate:假 Gemini client 注入 → 回傳每張圖的 SVG + DXF 連結,
     單層一張圖、多層含剖面/立面。
  2. 下載端點:DXF/zip 抓得到;亂七八糟的檔名(路徑跳脫)一律 404。
  3. 錯誤處理:空描述/基地太小 → 422 帶中文訊息;沒 API key → 503;
     通行碼(ACCESS_CODE)錯誤 → 403。

全程不碰網路:client_factory 注入假物件(同 test_nl_parser 的做法)。
"""
from __future__ import annotations

import json
from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient

from src.web.app import create_app


# ---------------------------------------------------------------------------
# 假 Gemini client(同 test_nl_parser)
# ---------------------------------------------------------------------------
@dataclass
class _FakeResponse:
    text: str


class _FakeModels:
    def __init__(self, payload: dict):
        self.payload = payload

    def generate_content(self, **kwargs):
        return _FakeResponse(text=json.dumps(self.payload))


class _FakeClient:
    def __init__(self, payload: dict):
        self.models = _FakeModels(payload)


def _payload(**over) -> dict:
    base = {"brief_type": "house", "site_width_m": 16, "site_depth_m": 14,
            "bedrooms": 3, "units_per_row": None, "corridor_width_m": None,
            "floor_label": None, "master_corner": None, "kitchen_side": None,
            "floors_above": None, "basements": None}
    base.update(over)
    return base


def _client(payload: dict) -> TestClient:
    app = create_app(client_factory=lambda: _FakeClient(payload))
    return TestClient(app)


@pytest.fixture(autouse=True)
def _no_access_code(monkeypatch):
    """預設不設通行碼(要測通行碼的測試自己 setenv)。"""
    monkeypatch.delenv("ACCESS_CODE", raising=False)


# ---------------------------------------------------------------------------
# 1) 生成:單層 / 多層
# ---------------------------------------------------------------------------
def test_generate_single_floor_house() -> None:
    c = _client(_payload())
    r = c.post("/api/generate", json={"text": "基地16×14米,三房"})
    assert r.status_code == 200
    data = r.json()
    assert [s["label"] for s in data["sheets"]] == ["1F"]   # 單層無剖面/立面
    svg = data["sheets"][0]["svg"]
    assert "<svg" in svg and "</svg>" in svg
    assert "單戶住宅 3 房" in data["summary"]

    # DXF 與 zip 都要抓得到
    dxf = c.get(data["sheets"][0]["dxf"])
    assert dxf.status_code == 200 and b"SECTION" in dxf.content
    assert c.get(data["zip"]).status_code == 200


def test_generate_multifloor_house_with_basement() -> None:
    c = _client(_payload(site_width_m=19, site_depth_m=13,
                         floors_above=3, basements=1))
    r = c.post("/api/generate", json={"text": "透天三層,地下一層", "seed": 5})
    assert r.status_code == 200
    data = r.json()
    assert ([s["label"] for s in data["sheets"]]
            == ["B1F", "1F", "2F", "3F", "剖面", "立面"])
    assert {s["kind"] for s in data["sheets"]} == {"floor", "section",
                                                   "elevation"}
    assert "地上 3 層 + 地下 1 層" in data["summary"]
    assert data["seed"] == 5
    assert "樓梯" in data["design_note"] and "廚房" in data["design_note"]


def test_suggestions_offer_site_upgrades() -> None:
    """設計建議:告訴使用者基地還放得下什麼,每則附完整需求句(可點擊
    重新生成)。19×13 無地下室 → 至少建議「加地下車庫」;文字要能直接
    當需求送(含基地尺寸)。"""
    c = _client(_payload(site_width_m=19, site_depth_m=13, floors_above=2))
    r = c.post("/api/generate", json={"text": "透天二層,基地19×13米,三房"})
    assert r.status_code == 200
    sugg = r.json()["suggestions"]
    labels = [s["label"] for s in sugg]
    assert "加地下車庫" in labels
    for s in sugg:
        assert "基地19×13米" in s["text"]     # 完整需求句,點了能直接重生成
        assert s["note"]


def test_seed_reproducible_and_random(monkeypatch) -> None:
    """同 seed → 同方案(同設計說明);不帶 seed → 伺服器隨機抽,會給回 seed。"""
    payload = _payload(site_width_m=19, site_depth_m=13,
                       floors_above=3, basements=1)
    c = _client(payload)
    a = c.post("/api/generate", json={"text": "透天三層", "seed": 3}).json()
    b = c.post("/api/generate", json={"text": "透天三層", "seed": 3}).json()
    assert a["design_note"] == b["design_note"]         # 同 seed 同方案

    rnd = c.post("/api/generate", json={"text": "透天三層"}).json()
    assert isinstance(rnd["seed"], int)                 # 隨機抽的 seed 有回傳


# ---------------------------------------------------------------------------
# 2) 下載端點的白名單
# ---------------------------------------------------------------------------
def test_download_rejects_bad_names() -> None:
    c = _client(_payload())
    assert c.get("/api/jobs/abcdef123456/..%2Fsecret.dxf").status_code == 404
    assert c.get("/api/jobs/not-a-job-id/1F.dxf").status_code == 404
    assert c.get("/api/jobs/abcdef123456/nothere.dxf").status_code == 404


# ---------------------------------------------------------------------------
# 3) 錯誤處理:422 / 503 / 403
# ---------------------------------------------------------------------------
def test_empty_text_is_422() -> None:
    c = _client(_payload())
    r = c.post("/api/generate", json={"text": "   "})
    assert r.status_code == 422
    assert "空" in r.json()["detail"]


def test_generator_validation_error_is_422() -> None:
    """基地 3×3 米 → 產生器檢核訊息要原封不動帶給使用者。"""
    c = _client(_payload(site_width_m=3, site_depth_m=3))
    r = c.post("/api/generate", json={"text": "基地3×3米"})
    assert r.status_code == 422
    assert r.json()["detail"]          # 有中文說明,不是空白 500


def test_missing_api_key_is_503(monkeypatch) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    c = TestClient(create_app())       # 沒注入假 client、也沒 key
    r = c.post("/api/generate", json={"text": "基地16×14米"})
    assert r.status_code == 503
    assert c.get("/api/config").json()["has_api_key"] is False


def test_access_code_protects_generate(monkeypatch) -> None:
    monkeypatch.setenv("ACCESS_CODE", "s3cret")
    c = _client(_payload())
    assert c.get("/api/config").json()["needs_code"] is True

    wrong = c.post("/api/generate", json={"text": "基地16×14米", "code": "x"})
    assert wrong.status_code == 403

    ok = c.post("/api/generate",
                json={"text": "基地16×14米", "code": "s3cret"})
    assert ok.status_code == 200


# ---------------------------------------------------------------------------
# 4) 前端頁面有掛上
# ---------------------------------------------------------------------------
def test_index_page_served() -> None:
    c = _client(_payload())
    r = c.get("/")
    assert r.status_code == 200
    assert "自動建築平面圖生成器" in r.text


# ---------------------------------------------------------------------------
# 5) E4:關鍵數字 / PDF 圖冊 / 歷史方案 / 多輪修改
# ---------------------------------------------------------------------------
def test_generate_returns_metrics_and_brief_data() -> None:
    """回應要帶關鍵數字(建蔽/容積/造價)與 brief_data(多輪修改的底)。"""
    c = _client(_payload(site_width_m=19, site_depth_m=13,
                         floors_above=3, basements=1))
    data = c.post("/api/generate",
                  json={"text": "透天三層,地下一層", "seed": 5}).json()
    m = data["metrics"]
    assert m["site_area_m2"] == pytest.approx(247, abs=0.5)
    assert 0 < m["coverage_pct"] <= 100
    assert m["far_pct"] > m["coverage_pct"]      # 三層 → 容積率 > 建蔽率
    assert m["est_cost_wan"] > 0
    assert data["brief_data"]["brief_type"] == "house"


def test_pdf_booklet_lazy_generated() -> None:
    """PDF 圖冊:第一次 GET 才渲染(從已存 DXF),回傳真 PDF。"""
    c = _client(_payload())
    data = c.post("/api/generate", json={"text": "基地16×14米,三房"}).json()
    r = c.get(data["pdf"])
    assert r.status_code == 200
    assert r.content[:5] == b"%PDF-"
    # 第二次直接用快取檔,一樣拿得到。
    assert c.get(data["pdf"]).status_code == 200


def test_history_lists_and_reloads() -> None:
    """歷史列表包含剛生成的 job;result 端點能整包載回(含 SVG)。"""
    c = _client(_payload())
    data = c.post("/api/generate",
                  json={"text": "基地16×14米,三房"}).json()
    hist = c.get("/api/history").json()
    assert any(h["job_id"] == data["job_id"] for h in hist)
    mine = next(h for h in hist if h["job_id"] == data["job_id"])
    assert mine["text"] == "基地16×14米,三房"

    r = c.get(f"/api/jobs/{data['job_id']}/result")
    assert r.status_code == 200
    loaded = r.json()
    assert loaded["summary"] == data["summary"]
    assert "<svg" in loaded["sheets"][0]["svg"]


def test_history_reload_missing_job_404() -> None:
    c = _client(_payload())
    assert c.get("/api/jobs/aaaabbbbcccc/result").status_code == 404
    assert c.get("/api/jobs/aaaabbbbcccc/pdf").status_code == 404


class _RecordingModels:
    """假 Gemini:記下收到的 contents/system,回固定 payload(修改模式驗證用)。"""

    def __init__(self, payload: dict, log: list):
        self.payload = payload
        self.log = log

    def generate_content(self, **kwargs):
        self.log.append(kwargs)
        return _FakeResponse(text=json.dumps(self.payload))


def test_modify_uses_base_and_keeps_seed() -> None:
    """多輪修改:帶 base → LLM 收到「目前需求 JSON+修改指令」;seed 沿用。"""
    log: list = []
    modified = _payload(bedrooms=2)          # LLM 合併後:三房 → 二房
    app = create_app(client_factory=lambda: type(
        "C", (), {"models": _RecordingModels(modified, log)})())
    c = TestClient(app)

    base = _payload()                        # 上一輪的 brief_data(三房)
    r = c.post("/api/generate", json={
        "text": "改二房", "base": base, "seed": 7})
    assert r.status_code == 200
    data = r.json()
    assert data["seed"] == 7                             # seed 沿用不重骰
    assert "2 房" in data["summary"]                     # 用了修改後的需求
    assert data["brief_data"]["bedrooms"] == 2           # 新的底給下一輪

    sent = log[-1]
    assert "修改指令:改二房" in sent["contents"]         # 指令有送到
    assert "site_width_m" in sent["contents"]            # 原需求 JSON 也在
    assert "修改模式" in sent["config"]["system_instruction"]


# ---------------------------------------------------------------------------
# 5) 家具自動配置 + 評分(Phase 6-9)
# ---------------------------------------------------------------------------
def test_optimize_returns_score_and_optimized_sheets() -> None:
    """先生成一個方案,再對它跑家具最佳化 → 回評分 + 最佳化後 SVG/DXF。"""
    c = _client(_payload())
    gen = c.post("/api/generate", json={"text": "基地16×14米,三房"}).json()
    r = c.post("/api/optimize", json={"job_id": gen["job_id"]})
    assert r.status_code == 200
    d = r.json()
    assert d["grade"] in {"A+", "A", "B", "C", "D"}
    assert 0.0 <= d["overall_score"] <= 100.0
    assert len(d["sub_scores"]) == 12                 # 12 個子分數
    assert d["rooms"]                                  # 各房重擺概況
    # 最佳化後的圖能顯示、DXF/zip 抓得到,且檔名與原始不同(opt_ 前綴)
    assert d["sheets"] and "<svg" in d["sheets"][0]["svg"]
    assert "opt_" in d["sheets"][0]["dxf"]
    assert c.get(d["sheets"][0]["dxf"]).status_code == 200
    assert c.get(d["zip"]).status_code == 200


def test_optimize_unknown_job_is_404() -> None:
    c = _client(_payload())
    r = c.post("/api/optimize", json={"job_id": "deadbeefcafe"})
    assert r.status_code == 404


def test_optimize_bad_job_id_is_404() -> None:
    """job_id 白名單:亂七八糟的字串(路徑跳脫)一律 404。"""
    c = _client(_payload())
    r = c.post("/api/optimize", json={"job_id": "../secrets"})
    assert r.status_code == 404


def test_optimize_wrong_access_code_is_403(monkeypatch) -> None:
    monkeypatch.setenv("ACCESS_CODE", "1234")
    c = _client(_payload())
    r = c.post("/api/optimize", json={"job_id": "deadbeefcafe", "code": "x"})
    assert r.status_code == 403
