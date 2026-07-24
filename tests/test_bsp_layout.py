"""BSP Layout → FloorPlanSpec 測試(v0.7 Phase 7.1a)。

驗證「刀位 θ → 能畫 DXF 的合法平面(牆/門/窗)」:

  * θ → FloorPlanSpec:房間/牆/門/窗齊全,kind 正確。
  * 牆由房間邊界推得:外框=外牆厚、內部=內牆厚;房間不重疊(剛好鋪滿建築)。
  * 門/窗的 wall_index/opening_index 真的指到牆上的洞口,且洞口落在牆長內。
  * 能畫 DXF、能被 Phase 6 Global Score 評分。
  * 任意隨機 θ 都不崩(含 MultiPolygon 房間);同 θ → 同結果。

⚠️ 7.1a 不含家具/柱(那是 7.1b);本層只保證幾何合法、畫得出、評得動。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
from shapely.geometry import Polygon

from src.design.layout.bsp_layout import (
    DOOR_WIDTH,
    WINDOW_WIDTH,
    bsp_to_spec,
    building_rect,
    random_theta,
)
from src.design.layout.global_score import score_report
from src.drafting.apartment_plan import FloorPlanSpec, draw_floor_plan
from src.drafting.wall import EXTERIOR_WALL_THICKNESS, INTERIOR_WALL_THICKNESS
from src.web.render import _new_doc

# 一組手挑的、像樣的刀位(有走道 + 獨立餐廳)。
THETA = np.array([0.62, 0.50, 0.33, 0.66, 0.0, 0.72, 0.55, 0.42, 0.20, 0.30])
FLAGS = np.array([1.0, 1.0])
SITE_W, SITE_D, BEDS = 20000.0, 14000.0, 3


def _spec(theta=THETA, flags=FLAGS):
    return bsp_to_spec(theta, flags, SITE_W, SITE_D, BEDS)


# ── 基本產出 ────────────────────────────────────────────────────────────────
def test_returns_floorplanspec_with_all_parts():
    spec = _spec()
    assert isinstance(spec, FloorPlanSpec)
    assert spec.rooms and spec.walls and spec.doors and spec.windows


def test_room_kinds_are_valid():
    spec = _spec()
    kinds = {r.kind for r in spec.rooms}
    assert kinds <= {"bedroom", "bathroom", "kitchen", "living", "dining",
                     "foyer", "corridor"}
    assert sum(r.kind == "bedroom" for r in spec.rooms) >= BEDS  # n 房都在


def test_walls_have_exterior_and_interior_thickness():
    spec = _spec()
    ths = {w.thickness for w in spec.walls}
    assert EXTERIOR_WALL_THICKNESS in ths and INTERIOR_WALL_THICKNESS in ths


def test_rooms_do_not_overlap_and_tile_building():
    """★ BSP 天生不重疊:房間兩兩不相交,且面積和 ≈ 建築範圍。"""
    spec = _spec()
    polys = [Polygon(r.points) for r in spec.rooms]
    for i in range(len(polys)):
        for j in range(i + 1, len(polys)):
            assert polys[i].intersection(polys[j]).area < 1e4  # <0.01 m² 視為只共邊
    bx0, by0, bx1, by1 = (v * 1000 for v in building_rect(SITE_W / 1000, SITE_D / 1000))
    build_area = (bx1 - bx0) * (by1 - by0)
    assert abs(sum(p.area for p in polys) - build_area) / build_area < 0.02


# ── 門 / 窗 指標正確 ─────────────────────────────────────────────────────────
def test_doors_reference_real_openings_within_wall():
    """★ 每扇門的 wall_index/opening_index 指到真實洞口,且洞口落在牆長內。"""
    spec = _spec()
    for dp in spec.doors:
        wall = spec.walls[dp.wall_index]
        op = wall.openings[dp.opening_index]              # 不越界即通過
        assert op.kind == "door" and abs(op.width - DOOR_WIDTH) < 1e-6
        assert op.position - op.width / 2 >= -1.0
        assert op.position + op.width / 2 <= wall.length + 1.0


def test_windows_reference_real_openings_within_wall():
    spec = _spec()
    for wp in spec.windows:
        wall = spec.walls[wp.wall_index]
        op = wall.openings[wp.opening_index]
        assert op.kind == "window" and abs(op.width - WINDOW_WIDTH) < 1e-6
        assert 0.0 <= op.position - op.width / 2
        assert op.position + op.width / 2 <= wall.length + 1.0


def test_private_rooms_get_a_door():
    """★ 私密房(臥室/浴廁/廚房)至少開得出門(門數 ≥ 私密房數的一半以上)。"""
    spec = _spec()
    private = sum(r.kind in ("bedroom", "bathroom", "kitchen") for r in spec.rooms)
    assert len(spec.doors) >= private * 0.5


# ── 能畫 / 能評分 ───────────────────────────────────────────────────────────
def test_draws_to_dxf():
    spec = _spec()
    doc, layers = _new_doc()
    draw_floor_plan(doc.modelspace(), spec, layers)
    assert len(list(doc.modelspace())) > 0


def test_phase6_can_score_it():
    """★ Phase 6 Global Score 吃得下 BSP 產生的 spec(0~100,不崩)。"""
    rep = score_report(_spec())
    assert 0.0 <= rep["overall_score"] <= 100.0
    assert len(rep["sub_scores"]) == 13


# ── 隨機 θ 穩健性 / 決定性 ──────────────────────────────────────────────────
def test_random_theta_always_produces_drawable_valid_spec():
    """★ 任意隨機刀位都不崩、都畫得出、都評得動(含 MultiPolygon 房間)。"""
    rng = np.random.default_rng(0)
    for _ in range(40):
        th, fl = random_theta(rng)
        spec = bsp_to_spec(th, fl, SITE_W, SITE_D, BEDS)
        doc, layers = _new_doc()
        draw_floor_plan(doc.modelspace(), spec, layers)
        assert len(list(doc.modelspace())) > 0
        assert 0.0 <= score_report(spec)["overall_score"] <= 100.0


def test_deterministic_same_theta_same_spec():
    a, b = _spec(), _spec()
    assert len(a.rooms) == len(b.rooms) and len(a.walls) == len(b.walls)
    assert len(a.doors) == len(b.doors) and len(a.windows) == len(b.windows)
    assert [r.points for r in a.rooms] == [r.points for r in b.rooms]


def test_random_theta_shapes():
    th, fl = random_theta(np.random.default_rng(1))
    assert th.shape == (10,) and fl.shape == (2,)
    assert set(np.unique(fl)) <= {0.0, 1.0}


def test_site_boundary_matches_input():
    spec = _spec()
    xs = [p[0] for p in spec.site_boundary]
    ys = [p[1] for p in spec.site_boundary]
    assert max(xs) == SITE_W and max(ys) == SITE_D


def test_different_theta_gives_different_layout():
    """★ 不同刀位 → 不同佈局(房間面積分佈不同)。"""
    a = _spec(np.array([0.62, 0.50, 0.33, 0.66, 0.0, 0.72, 0.55, 0.42, 0.20, 0.30]))
    b = _spec(np.array([0.45, 0.40, 0.25, 0.50, 0.0, 0.60, 0.45, 0.30, 0.15, 0.25]))
    areas_a = sorted(round(Polygon(r.points).area) for r in a.rooms)
    areas_b = sorted(round(Polygon(r.points).area) for r in b.rooms)
    assert areas_a != areas_b
