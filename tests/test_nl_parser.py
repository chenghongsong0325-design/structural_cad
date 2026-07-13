"""自然語言介面(nl_parser)的單元測試。

驗證重點:
  1. _brief_from_data:純資料轉換——米→mm、預設值、缺欄位/未知類型報錯。
  2. parse_brief:注入假 client(不需網路/API key),驗證 prompt 組裝
     (強制 JSON schema、系統提示)與回應解析。
  3. 端到端:假 client 的解析結果 → generate_floor_plan 通過檢核。
"""
from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from src.design.layout_generator import CorridorBrief, HouseBrief, generate_floor_plan
from src.design.nl_parser import (
    BRIEF_SCHEMA,
    _brief_from_data,
    parse_brief,
)


# ---------------------------------------------------------------------------
# 假 client(模仿 google-genai SDK 的最小表面)
# ---------------------------------------------------------------------------
@dataclass
class _FakeResponse:
    text: str


class _FakeModels:
    def __init__(self, payload: dict):
        self.payload = payload
        self.last_kwargs: dict = {}

    def generate_content(self, **kwargs):
        self.last_kwargs = kwargs
        return _FakeResponse(text=json.dumps(self.payload))


class _FakeClient:
    def __init__(self, payload: dict):
        self.models = _FakeModels(payload)


def _payload(**over) -> dict:
    """齊全欄位的解析結果(schema 的 required 全帶),再覆寫。"""
    base = {"brief_type": "house", "site_width_m": 16, "site_depth_m": 14,
            "bedrooms": 3, "units_per_row": None, "corridor_width_m": None,
            "floor_label": None, "master_corner": None, "kitchen_side": None}
    base.update(over)
    return base


# ---------------------------------------------------------------------------
# 1) _brief_from_data:資料轉換
# ---------------------------------------------------------------------------
def test_house_meters_to_mm() -> None:
    brief = _brief_from_data(_payload())
    assert isinstance(brief, HouseBrief)
    assert brief.site_width == 16000
    assert brief.site_depth == 14000
    assert brief.bedrooms == 3


def test_house_defaults_when_null() -> None:
    brief = _brief_from_data(_payload(bedrooms=None, floor_label=None))
    assert brief.bedrooms == 3          # dataclass 預設
    assert brief.floor_label == "1F"


def test_house_missing_site_raises() -> None:
    with pytest.raises(ValueError, match="基地"):
        _brief_from_data(_payload(site_width_m=None))


def test_corridor_mapping_and_defaults() -> None:
    brief = _brief_from_data(_payload(
        brief_type="corridor", units_per_row=6, corridor_width_m=2.0,
        floor_label="3F"))
    assert isinstance(brief, CorridorBrief)
    assert brief.units_per_row == 6
    assert brief.corridor_width == 2000
    assert brief.floor_label == "3F"

    bare = _brief_from_data(_payload(brief_type="corridor"))
    assert bare.units_per_row == 4      # dataclass 預設
    assert bare.corridor_width == 1800


def test_unknown_brief_type_raises() -> None:
    with pytest.raises(ValueError, match="未知建築類型"):
        _brief_from_data(_payload(brief_type="宮殿"))


# ---------------------------------------------------------------------------
# 2) parse_brief:prompt 組裝與回應解析(假 client)
# ---------------------------------------------------------------------------
def test_parse_brief_house() -> None:
    client = _FakeClient(_payload())
    brief = parse_brief("基地16×14米,三房", client=client)
    assert isinstance(brief, HouseBrief)
    assert brief.site_width == 16000

    sent = client.models.last_kwargs
    assert sent["config"]["response_schema"] is BRIEF_SCHEMA   # 強制 schema
    assert sent["config"]["response_mime_type"] == "application/json"
    assert sent["contents"] == "基地16×14米,三房"
    assert "解析" in sent["config"]["system_instruction"]


def test_parse_brief_empty_text_raises() -> None:
    with pytest.raises(ValueError, match="空"):
        parse_brief("   ", client=_FakeClient(_payload()))


def test_schema_covers_all_required() -> None:
    """schema 的 required 必須列齊所有欄位(結構化輸出的規定)。"""
    assert set(BRIEF_SCHEMA["required"]) == set(BRIEF_SCHEMA["properties"])


# ---------------------------------------------------------------------------
# 3) 端到端:解析結果餵產生器要能通過檢核
# ---------------------------------------------------------------------------
def test_parsed_house_generates_valid_plan() -> None:
    brief = parse_brief("基地16×14米,三房", client=_FakeClient(_payload()))
    spec = generate_floor_plan(brief)
    assert any(r.kind == "foyer" for r in spec.rooms)


def test_parsed_corridor_generates_valid_plan() -> None:
    client = _FakeClient(_payload(brief_type="corridor", units_per_row=4))
    brief = parse_brief("集合住宅每排4戶", client=client)
    spec = generate_floor_plan(brief)
    assert len([r for r in spec.rooms if r.name == "1房型"]) == 8


def test_llm_nonsense_caught_by_generator() -> None:
    """LLM 給離譜數值(基地 3×3m)→ 產生器既有檢核要擋下。"""
    client = _FakeClient(_payload(site_width_m=3, site_depth_m=3))
    brief = parse_brief("基地3×3米", client=client)
    with pytest.raises(ValueError):
        generate_floor_plan(brief)


# ---------------------------------------------------------------------------
# 4) 方位約束(C2:「主臥要在西南角,廚房靠北」)
# ---------------------------------------------------------------------------
def test_position_constraints_passed_through() -> None:
    client = _FakeClient(_payload(master_corner="SW", kitchen_side="N"))
    brief = parse_brief("主臥要在西南角,廚房靠北", client=client)
    assert brief.master_corner == "SW"
    assert brief.kitchen_side == "N"


def test_parsed_constraints_generate_valid_plan() -> None:
    """ROADMAP 原句組合:主臥西南角+廚房靠北 → 主臥真的在西南。"""
    from shapely.geometry import Polygon

    client = _FakeClient(_payload(master_corner="SW", kitchen_side="N"))
    brief = parse_brief("基地16×14米,三房,主臥西南角,廚房靠北", client=client)
    spec = generate_floor_plan(brief)
    bx0, by0 = spec.grid_origin
    mx = bx0 + sum(spec.x_spacings) / 2
    my = by0 + sum(spec.y_spacings) / 2
    m = Polygon(next(r for r in spec.rooms if r.name == "主臥室").points).centroid
    k = Polygon(next(r for r in spec.rooms if r.kind == "kitchen").points).centroid
    assert m.x < mx and m.y < my          # 主臥在西南
    assert k.y > my                       # 廚房在北
