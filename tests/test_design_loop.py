"""雙向收斂迴圈(混合式 AI 建築師第 3 步)測試。

  * critique_building:純幾何/評分檢查,回可行動問題清單(不呼叫網路)。
  * _touches_ns:前後外牆才算採光面。
  * refine_room_graph:問題清單有進到送模型的 contents。
  * design_building:用**假 client**(不呼叫 Gemini)跑數次迭代,回 fitness 最高那版。
"""
import json
import random
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from src.design.layout.design_loop import (
    SETBACK,
    _touches_ns,
    critique_building,
    design_building,
)
from src.design.layout.graph_layout import realize_graph_building
from src.design.layout.room_graph import refine_room_graph

W, D = 7000.0, 12000.0
ENV = (SETBACK, SETBACK, SETBACK + W, SETBACK + D)

GRAPH = {
    "rooms": [
        {"id": "entry", "kind": "corridor", "floor": 1, "wants_daylight": False},
        {"id": "living", "kind": "living", "floor": 1, "wants_daylight": True},
        {"id": "dining", "kind": "dining", "floor": 1, "wants_daylight": True},
        {"id": "kitchen", "kind": "kitchen", "floor": 1, "wants_daylight": False},
        {"id": "toilet1", "kind": "toilet", "floor": 1, "wants_daylight": False},
        {"id": "stair1", "kind": "stair", "floor": 1, "wants_daylight": False},
        {"id": "corr2", "kind": "corridor", "floor": 2, "wants_daylight": False},
        {"id": "bedA", "kind": "bedroom", "floor": 2, "wants_daylight": True},
        {"id": "bedB", "kind": "bedroom", "floor": 2, "wants_daylight": True},
        {"id": "bath2", "kind": "bathroom", "floor": 2, "wants_daylight": False},
        {"id": "stair2", "kind": "stair", "floor": 2, "wants_daylight": False},
        {"id": "corr3", "kind": "corridor", "floor": 3, "wants_daylight": False},
        {"id": "master", "kind": "master_bedroom", "floor": 3,
         "wants_daylight": True},
        {"id": "mbath", "kind": "bathroom", "floor": 3, "wants_daylight": False},
        {"id": "study", "kind": "study", "floor": 3, "wants_daylight": True},
        {"id": "stair3", "kind": "stair", "floor": 3, "wants_daylight": False},
    ],
    "adjacencies": [
        {"a": "entry", "b": "living", "connection": "open"},
        {"a": "living", "b": "dining", "connection": "open"},
        {"a": "dining", "b": "kitchen", "connection": "open"},
        {"a": "entry", "b": "toilet1", "connection": "door"},
        {"a": "stair2", "b": "corr2", "connection": "open"},
        {"a": "corr2", "b": "bedA", "connection": "door"},
        {"a": "corr2", "b": "bedB", "connection": "door"},
        {"a": "corr2", "b": "bath2", "connection": "door"},
        {"a": "stair3", "b": "corr3", "connection": "open"},
        {"a": "corr3", "b": "master", "connection": "door"},
        {"a": "master", "b": "mbath", "connection": "door"},
        {"a": "corr3", "b": "study", "connection": "door"},
    ],
    "entry": "entry",
    "rationale": "測試用。",
}


class _SeqClient:
    """假 client:依序回傳 payloads(propose 收第一個,之後 refine 各收下一個)。"""

    def __init__(self, payloads):
        self._payloads = list(payloads)
        self._i = 0
        self.models = self
        self.calls = []

    def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        p = self._payloads[min(self._i, len(self._payloads) - 1)]
        self._i += 1
        return types.SimpleNamespace(text=json.dumps(p))


def test_touches_ns_front_back_only():
    env = (0.0, 0.0, 7000.0, 12000.0)
    assert _touches_ns((0.0, 0.0, 3000.0, 2000.0), env)          # 貼南
    assert _touches_ns((0.0, 10000.0, 3000.0, 12000.0), env)     # 貼北
    assert not _touches_ns((3000.0, 4000.0, 5000.0, 8000.0), env)  # 內間


def test_critique_returns_actionable_list():
    floors = realize_graph_building(GRAPH, W, D, rng=random.Random(1))
    probs = critique_building(floors, ENV)
    assert isinstance(probs, list)
    assert all(isinstance(p, str) for p in probs)


def test_refine_passes_problems_into_contents():
    spy = _SeqClient([GRAPH])
    refine_room_graph(GRAPH, ["1F:客廳 55㎡ 太大", "2F:臥室 是內間"],
                      client=spy, floor_area_m2=84.0)
    assert "客廳 55㎡ 太大" in spy.calls[0]["contents"]
    assert "84" in spy.calls[0]["contents"]


def test_design_building_keeps_best_across_iterations():
    """★ 迴圈回傳 fitness 最高那版(用假 client,不呼叫 Gemini)。"""
    client = _SeqClient([GRAPH, GRAPH, GRAPH])
    best, history = design_building("三房", W, D, iterations=2, client=client)
    assert best["fitness"] == max(h["fitness"] for h in history)   # 挑最佳
    assert 1 <= len(history) <= 2
    assert best["floors"] and best["graph"]
    assert client.calls                                            # 有透過假 client


def test_design_building_fitness_penalizes_problems():
    """fitness = 平均分 − 2×問題數(問題越多分越低)。"""
    client = _SeqClient([GRAPH])
    best, history = design_building("三房", W, D, iterations=1, client=client)
    h = history[0]
    assert h["fitness"] == pytest.approx(h["mean_score"] - 2.0 * h["n_problems"])
