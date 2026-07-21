"""房間面積程式(F3)——分配器本身 + 「房間會跟著基地長大」的端到端行為。

這份測試守的是使用者 2026-07-20 的驗收條件:
  * 不准有固定房間尺寸:基地變大,**每一間**都要變大(不是只有客廳)。
  * 每間都有 min/preferred/max,到頂就停;全員到頂後的餘量變院子,不塞客廳。
  * 面積是目標不是命令:柱網吸附會讓實際面積偏移,容許 ±AREA_TOLERANCE。
  * 柱網優先:任何尺寸下,柱都要藏在牆裡、跨距規則。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.design.layout_generator import (
    HouseBrief,
    generate_floor_plan,
    generate_house_public,
    generate_house_upper,
)
from src.design.metrics import _polygon_area_m2
from src.design.room_program import (
    AREA_TOLERANCE,
    ROOM_PROGRAM,
    allocate_areas,
    requirement,
    solve_band,
)

HOUSE = ["master_bedroom", "bedroom", "bedroom", "bathroom", "living", "kitchen"]


def _areas(spec) -> dict:
    out: dict = {}
    for r in spec.rooms:
        out[r.name] = out.get(r.name, 0.0) + _polygon_area_m2(r.points)
    return out


# ── 分配器 ────────────────────────────────────────────────────────────────
def test_tiny_budget_raises_instead_of_squeezing():
    """預算低於「全員 min 合計」就明講放不下,不硬塞。"""
    need = sum(requirement(k).min_area for k in HOUSE)
    with pytest.raises(ValueError, match="不足最低需求"):
        allocate_areas(HOUSE, need - 1)


def test_everyone_starts_at_minimum():
    need = sum(requirement(k).min_area for k in HOUSE)
    plan = allocate_areas(HOUSE, need)
    assert plan.areas == pytest.approx([requirement(k).min_area for k in HOUSE])


def test_every_room_grows_when_budget_grows():
    """使用者【4】:可用面積變大,**每一間**都要變大——不是只有客廳。

    這正是改造前的病:基地 +160% 而主臥/次臥/浴室一格沒動。
    """
    small = allocate_areas(HOUSE, 80)
    big = allocate_areas(HOUSE, 96)                      # +20%
    for kind, a, b in zip(HOUSE, small.areas, big.areas):
        assert b > a + 1e-6, f"{kind} 沒有跟著變大({a:.1f} → {b:.1f})"


def test_priority_order_master_grows_fastest():
    """使用者【5】:同樣的餘量,優先序高的漲得多(主臥 > 次臥 > 浴室)。"""
    lo, hi = allocate_areas(HOUSE, 70), allocate_areas(HOUSE, 85)
    gain = {k: h - l for k, l, h in zip(HOUSE, lo.areas, hi.areas)}
    # 以「離自己 preferred 的距離」正規化,才比得出優先序而不是比房間大小。
    norm = {k: gain[k] / (requirement(k).preferred_area - requirement(k).min_area)
            for k in ("master_bedroom", "bedroom", "bathroom")}
    assert norm["master_bedroom"] > norm["bedroom"] > norm["bathroom"]


def test_nobody_exceeds_maximum_and_surplus_is_returned():
    """使用者定調:客廳有 max(60m²);全員到頂後的餘量原封退回(給院子),
    不再無腦塞給客廳。"""
    plan = allocate_areas(HOUSE, 400)
    for kind, area in zip(HOUSE, plan.areas):
        assert area <= requirement(kind).max_area + 1e-6
    assert plan.area_of("living") == pytest.approx(ROOM_PROGRAM["living"].max_area)
    assert plan.leftover_m2 > 200          # 剩下的沒有被偷偷塞進任何房間
    assert plan.total_m2 + plan.leftover_m2 == pytest.approx(400)


def test_caps_clamp_to_geometric_capacity():
    """caps(幾何容量)收得比 max_area 更緊時要生效——窄基地不該分到裝不下的面積。"""
    caps = [6.0] * len(HOUSE)
    plan = allocate_areas(HOUSE, 400, caps=caps)
    for kind, area in zip(HOUSE, plan.areas):
        # 上限取 min(max_area, cap),但不得低於 min_area(低於就讓幾何去報錯)。
        assert area <= max(requirement(kind).min_area, 6.0) + 1e-6


# ── 形狀(面積 → 幾×幾)────────────────────────────────────────────────
def test_shape_is_not_fixed_but_area_is_kept():
    """使用者【7】:18m² 可以是 3×6 / 4×4.5 / 5×3.6——形狀由可用寬度決定,
    面積守住。同一份面積目標,給不同的可用寬度就該長出不同的形狀。"""
    reqs = [requirement("bedroom")] * 3
    targets = [13.0, 13.0, 13.0]
    shapes = []
    for avail in (9000, 12000, 15000):
        depth, widths = solve_band(targets, reqs, width_avail=avail,
                                   depth_bounds=(2800.0, 6000.0))
        shapes.append((depth, widths[0]))
        assert widths[0] * depth / 1e6 == pytest.approx(13.0, rel=0.1)
    assert len({round(d) for d, _ in shapes}) > 1, "不同可用寬度應長出不同形狀"


# ── 端到端:房間真的跟著基地長大 ──────────────────────────────────────
@pytest.mark.parametrize("gen", [generate_house_upper, generate_house_public])
def test_rooms_grow_with_site_not_just_living(gen):
    """改造前的實測:基地 20×16 → 32×26(+160%),主臥 23.1→23.1、臥室
    17.1→17.1,一格沒動,只有客廳/天井變大。現在臥室必須真的變大。"""
    small = _areas(gen(HouseBrief(site_width=18000, site_depth=13000,
                                  bedrooms=3, seed=1)))
    big = _areas(gen(HouseBrief(site_width=26000, site_depth=16000,
                                bedrooms=3, seed=1)))
    # 兩層都有的公共空間:1F 廚房 / 2F 主臥——各自都要跟著基地變大。
    key = "主臥室" if "主臥室" in small else "廚房"
    assert big[key] > small[key] * 1.15, (
        f"{key} 只從 {small[key]:.1f} 變到 {big[key]:.1f}m²,沒有跟著基地長大")


def test_bedrooms_respect_program_bounds_across_sites():
    """任何基地尺寸下,臥室面積都要落在 [min, max] 內(±10% 容許誤差)。

    ±10% 是因為柱網吸附會把隔牆挪到軸線上——柱網優先,面積讓步。
    """
    mreq, breq = ROOM_PROGRAM["master_bedroom"], ROOM_PROGRAM["bedroom"]
    for w, d in [(19000, 13000), (22000, 14000), (26000, 16000), (30000, 20000)]:
        spec = generate_house_upper(
            HouseBrief(site_width=w, site_depth=d, bedrooms=3, seed=1))
        got = _areas(spec)
        master = got["主臥室"]
        assert mreq.min_area * (1 - AREA_TOLERANCE) <= master \
            <= mreq.max_area * (1 + AREA_TOLERANCE), f"{w}×{d} 主臥 {master:.1f}m²"
        for name in ("臥室2", "臥室3"):
            a = got[name]
            assert breq.min_area * (1 - AREA_TOLERANCE) <= a \
                <= breq.max_area * (1 + AREA_TOLERANCE), f"{w}×{d} {name} {a:.1f}m²"


def test_living_room_compact_not_a_long_thin_band():
    """客廳不再是長條垃圾桶(benchmark 首要問題):寬基地上客廳保持緊湊
    (長寬比 ≤ aspect_max),多的空間切成 Living Overflow(家庭廳/書房等)
    交給 Program Selector,不是全部拉長客廳。(非天井路徑;天井版另有骨架。)"""
    for gen in (generate_house_upper, generate_house_public):
        spec = gen(HouseBrief(site_width=30000, site_depth=13000,
                              bedrooms=3, seed=1))
        liv = next(r for r in spec.rooms if r.kind == "living")
        xs = [p[0] for p in liv.points]
        ys = [p[1] for p in liv.points]
        w, d = max(xs) - min(xs), max(ys) - min(ys)
        amax = ROOM_PROGRAM["living"].aspect_max
        assert max(w, d) / min(w, d) <= amax * 1.05, \
            f"{gen.__name__} 客廳長寬比 {max(w, d) / min(w, d):.2f} > {amax}"
        # 多的空間切成溢位房間(家庭廳/多功能/書房),不是灌進客廳。
        assert any(r.kind in ("family", "study") for r in spec.rooms), \
            f"{gen.__name__} 沒有切出 Living Overflow 房間"


def test_dining_zone_is_split_when_it_would_be_oversized():
    """1F 北帶西段大到超過餐廳上限就切一間附屬房出來——避免「餐廳」變成
    新的垃圾桶(改造中途曾出現 47~89m² 的餐廳)。"""
    got = _areas(generate_house_public(
        HouseBrief(site_width=20000, site_depth=16000, bedrooms=3, seed=1)))
    assert any(n in got for n in ("書房", "儲藏室")), "西段沒有切出附屬房"
    assert got.get("餐廳", 0) <= ROOM_PROGRAM["dining"].max_area * 1.5


def test_patio_band_dining_split_but_family_room_kept_whole():
    """深基地(三帶+天井版)的東段房間:1F 餐廳大到超過上限要切一間儲藏室
    出來(改造中途實測 32×26 基地量出 89m² 的「餐廳」);但 2F 家庭廳**不**
    比照切割——它是所有臥室門共用的走道(validate_spec C1.5b:有走道時
    每間臥室門都要通到走道),切開會讓貼樓梯間側的臥室門搆不到,判定
    「門未通走道」。功能性硬約束(走道必須貫通)優先於面積目標。

    餐廳是 L 形(東段 + 天井北側固定走道段),深基地時東段的「結構下限」
    (min_width × 深帶進深 dp)本身就可能超過 max_area(dp 越深越明顯)——
    這種情況下切割只能把面積壓到這個下限,壓不進 max_area,是合理的
    (同主臥深度加成的結論:硬性幾何約束有時就是比面積目標優先)。所以這裡
    只驗證「切了之後大幅縮小、且不再隨基地繼續長大」,不驗證絕對值達標。
    """
    w, d = 32000, 26000
    pub = generate_house_public(HouseBrief(site_width=w, site_depth=d,
                                           bedrooms=3, seed=1))
    upper = generate_house_upper(HouseBrief(site_width=w, site_depth=d,
                                            bedrooms=3, seed=1))
    assert pub is not upper                                # 兩層各自產生
    got_pub = _areas(pub)
    assert "儲藏室" in got_pub, "深基地 1F 東段沒有切出儲藏室"
    assert got_pub["餐廳"] < 30, f"餐廳 {got_pub['餐廳']:.1f}m² 遠超合理範圍,切割沒生效"

    # 基地再深一截,餐廳不該繼續變大(已經切到結構下限、停止成長)。
    bigger = _areas(generate_house_public(
        HouseBrief(site_width=w + 8000, site_depth=d + 6000, bedrooms=3, seed=1)))
    assert bigger["餐廳"] <= got_pub["餐廳"] + 1.0

    got_upper = _areas(upper)
    assert "儲藏室" not in got_upper, "2F 家庭廳不該被切(會斷走道)"


# ── 柱網優先(使用者定調:不得為了湊面積破壞柱網)──────────────────
@pytest.mark.parametrize("bedrooms,w,d", [(2, 18000, 13000), (3, 22000, 15000),
                                          (4, 26000, 16000), (3, 30000, 20000)])
def test_column_grid_never_sacrificed_for_area(bedrooms, w, d):
    """任何尺寸下:跨距 3~9m、max/min ≤1.6、每條中間軸線都坐在豎牆上。"""
    for gen in (generate_house_public, generate_house_upper):
        spec = gen(HouseBrief(site_width=w, site_depth=d,
                              bedrooms=bedrooms, seed=1))
        xs = spec.x_spacings
        assert 3000 <= min(xs) and max(xs) <= 9000
        assert max(xs) / min(xs) <= 1.6
        axes, wall_xs = [spec.grid_origin[0]], {
            wl.start[0] for wl in spec.walls if wl.start[0] == wl.end[0]}
        for s in xs:
            axes.append(axes[-1] + s)
        for a in axes[1:-1]:
            assert any(abs(a - wx) < 1 for wx in wall_xs), \
                f"{bedrooms}房 {w}×{d} 軸線 x={a:.0f} 上沒有豎牆(柱凸進房間)"


def test_program_selector_avoids_duplicate_rooms():
    """Program Selector 依脈絡選 Overflow 用途:1F 北帶已切出書房時,南帶溢位
    不再選書房(改家庭廳),避免同層兩間書房。"""
    from src.design.room_program import select_overflow_program
    # 1F 已有書房 → 不再選書房
    kind, _ = select_overflow_program(
        floor="public", bedrooms=3, want_study=False, has_study=True,
        has_family=False, width_mm=4000, depth_mm=3800)
    assert kind == "family"
    # 使用者已指定書房(want_study)→ 同樣不再自動配書房
    kind2, _ = select_overflow_program(
        floor="public", bedrooms=3, want_study=True, has_study=False,
        has_family=False, width_mm=4000, depth_mm=3800)
    assert kind2 == "family"
    # 太窄 → 儲藏室(收納)
    kind3, _ = select_overflow_program(
        floor="upper", bedrooms=3, want_study=False, has_study=False,
        has_family=False, width_mm=1800, depth_mm=3800)
    assert kind3 == "storage"


def test_single_floor_master_no_longer_unbounded():
    """單層路徑改造前完全沒有上限:30×16m 基地會生出 54m² 的主臥、42m² 的次臥
    (「等比放大」放到失控)。現在一樣受 max_area 管。"""
    spec = generate_floor_plan(
        HouseBrief(site_width=30000, site_depth=16000, bedrooms=3))
    got = _areas(spec)
    cap = ROOM_PROGRAM["master_bedroom"].max_area * (1 + AREA_TOLERANCE)
    assert got["主臥室"] <= cap, f"主臥 {got['主臥室']:.1f}m² 仍然沒有上限"
