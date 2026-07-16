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
