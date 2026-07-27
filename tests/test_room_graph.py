"""room_graph(混合式 AI 建築師第 1 步:LLM 當設計師 → 房間關係圖)測試。

  * sanity_check:抓得出明顯不合理的拓撲(一進門就臥室、孤立房間、廚房沒挨餐廳…)。
  * propose_room_graph:用**假 client**(不呼叫 Gemini)驗證組 prompt/收 JSON 的流程。
  * _topology_signature:同拓撲同指紋、異拓撲異指紋。
"""
import json
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.design.layout.room_graph import (
    _topology_signature,
    propose_room_graph,
    sanity_check,
)

GOOD = {
    "rooms": [
        {"id": "entry", "kind": "corridor", "floor": 1, "wants_daylight": False},
        {"id": "living", "kind": "living", "floor": 1, "wants_daylight": True},
        {"id": "dining", "kind": "dining", "floor": 1, "wants_daylight": True},
        {"id": "kitchen", "kind": "kitchen", "floor": 1, "wants_daylight": False},
        {"id": "bath", "kind": "bathroom", "floor": 1, "wants_daylight": False},
        {"id": "bed", "kind": "bedroom", "floor": 1, "wants_daylight": True},
    ],
    "adjacencies": [
        {"a": "entry", "b": "living", "connection": "open"},
        {"a": "living", "b": "dining", "connection": "open"},
        {"a": "dining", "b": "kitchen", "connection": "near"},
        {"a": "entry", "b": "bed", "connection": "door"},
        {"a": "entry", "b": "bath", "connection": "door"},
    ],
    "entry": "entry",
    "rationale": "公共空間串聯,臥室浴室由玄關進。",
}


def test_sanity_passes_reasonable_graph():
    assert sanity_check(GOOD) == []


def test_sanity_flags_entry_into_bedroom():
    bad = json.loads(json.dumps(GOOD))
    bad["entry"] = "bed"                       # 一進門就臥室
    assert any("bedroom" in p or "臥" in p for p in sanity_check(bad))


def test_sanity_flags_orphan_room():
    bad = json.loads(json.dumps(GOOD))
    bad["rooms"].append({"id": "ghost", "kind": "storage", "floor": 1,
                         "wants_daylight": False})   # 沒有任何相鄰邊
    assert any("孤立" in p for p in sanity_check(bad))


def test_sanity_flags_kitchen_not_near_food():
    bad = json.loads(json.dumps(GOOD))
    bad["adjacencies"] = [e for e in bad["adjacencies"]
                          if "kitchen" not in (e["a"], e["b"])]
    assert any("廚房" in p for p in sanity_check(bad))


def test_sanity_flags_no_bathroom():
    bad = json.loads(json.dumps(GOOD))
    bad["rooms"] = [r for r in bad["rooms"] if r["kind"] != "bathroom"]
    bad["adjacencies"] = [e for e in bad["adjacencies"]
                          if "bath" not in (e["a"], e["b"])]
    assert any("浴廁" in p for p in sanity_check(bad))


class _FakeClient:
    """模仿 google-genai 的最小表面:client.models.generate_content(...).text。"""

    def __init__(self, payload):
        self._payload = payload
        self.models = self

    def generate_content(self, **kwargs):
        return types.SimpleNamespace(text=json.dumps(self._payload))


def test_propose_with_fake_client_returns_graph():
    graph = propose_room_graph("三房兩廳", client=_FakeClient(GOOD))
    assert graph["rooms"] and graph["adjacencies"]
    assert graph["entry"] == "entry"


def test_propose_passes_area_hint_in_contents():
    """給了 floor_area_m2 → 面積提示要進到送模型的 contents(閉迴圈的關鍵)。"""
    captured = {}

    class _Spy(_FakeClient):
        def generate_content(self, **kwargs):
            captured["contents"] = kwargs.get("contents", "")
            return super().generate_content(**kwargs)

    propose_room_graph("三房", client=_Spy(GOOD), floor_area_m2=84.0)
    assert "84" in captured["contents"]


def test_topology_signature_distinguishes():
    other = json.loads(json.dumps(GOOD))
    other["adjacencies"] = other["adjacencies"][:2]     # 改拓撲
    assert _topology_signature(GOOD) != _topology_signature(other)
    assert _topology_signature(GOOD) == _topology_signature(
        json.loads(json.dumps(GOOD)))                    # 同圖同指紋
