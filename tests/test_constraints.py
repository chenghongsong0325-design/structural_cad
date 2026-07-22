"""Constraint Engine 測試(v0.7 Phase 5-5)。

三類:
  * **零誤報**——實際生成的圖不該違反那些本來就守得住的規則。
  * **抓得到**——每一條規則都要能在注入對應缺陷時觸發(否則「永遠回空」的
    壞偵測器也會通過零誤報測試)。
  * **唯讀**——check_constraints() 不得改動 spec。
"""
import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shapely.geometry import Polygon

from src.design.constraints import (
    NOISY_PUBLIC_KINDS,
    RULES,
    SEVERITY_ERROR,
    SEVERITY_WARN,
    ConstraintContext,
    ConstraintReport,
    ConstraintViolation,
    check_constraints,
)
from src.design.layout_generator import (
    HouseBrief,
    generate_floor_plan,
    generate_house_public,
    generate_house_upper,
)
from src.drafting.fixtures import FixturePlacement

RULE_IDS = ("bedroom_not_facing_kitchen", "bathroom_not_facing_dining",
            "entrance_not_facing_toilet", "bedroom_avoids_public_adjacency",
            "kitchen_near_dining")

_SPECS = [
    lambda: generate_floor_plan(
        HouseBrief(site_width=16000, site_depth=14000, bedrooms=3)),
    lambda: generate_floor_plan(
        HouseBrief(site_width=20000, site_depth=14000, bedrooms=3)),
    lambda: generate_house_upper(
        HouseBrief(site_width=26000, site_depth=16000, bedrooms=3, seed=1)),
    lambda: generate_house_public(
        HouseBrief(site_width=22000, site_depth=14000, bedrooms=3, seed=1)),
]


def _spec():
    return _SPECS[0]()


def _relabel_neighbour(spec, of_kind: str, to_kind: str, connected=True):
    """把某個 of_kind 房間的鄰居改標成 to_kind,用來構造違規情境。

    connected=True 取「有門相通」的鄰居;False 取「只共牆」的鄰居。
    回傳 (來源房名, 被改標的房名),找不到回 (None, None)。"""
    ctx = ConstraintContext.build(spec)
    g = ctx.graphs
    for i in ctx.indices(of_kind):
        pool = g.room_graph[i] if connected else (g.adjacency[i] - set(g.room_graph[i]))
        for j in pool:
            if g.kinds[j] in (of_kind, to_kind):
                continue
            spec.rooms[j].kind = to_kind
            return g.names[i], g.names[j]
    return None, None


# ── 引擎結構 ──────────────────────────────────────────────────────────────
def test_rule_registry_is_complete():
    """五條規則都在登錄表裡,且欄位齊全。"""
    assert tuple(r.rule_id for r in RULES) == RULE_IDS
    for r in RULES:
        assert r.description and callable(r.check)
        assert r.severity in (SEVERITY_ERROR, SEVERITY_WARN)


def test_report_records_checked_and_skipped():
    """每條規則不是被檢查就是被略過,兩者互斥且涵蓋全部。"""
    for make in _SPECS:
        rep = check_constraints(make())
        assert isinstance(rep, ConstraintReport)
        assert set(rep.checked) | set(rep.skipped) == set(RULE_IDS)
        assert not (set(rep.checked) & set(rep.skipped))


def test_inapplicable_rules_are_skipped_not_failed():
    """沒有獨立餐廳時,餐廳相關規則應列入 skipped 而不是誤判成通過/違反。"""
    spec = _spec()
    if not any(r.kind == "dining" for r in spec.rooms):
        rep = check_constraints(spec)
        assert "bathroom_not_facing_dining" in rep.skipped
        assert "kitchen_near_dining" in rep.skipped
        assert rep.by_rule("kitchen_near_dining") == []


def test_check_does_not_mutate_spec():
    """★ 需求:只建立 Constraint,不得修改 Layout。"""
    for make in _SPECS:
        spec = make()
        before = copy.deepcopy((
            [(r.points, r.kind, r.name) for r in spec.rooms],
            [(w.start, w.end, [(o.position, o.width, o.kind) for o in w.openings])
             for w in spec.walls],
            [(getattr(f, "name", "counter"), getattr(f, "insert", None))
             for f in spec.fixtures],
        ))
        check_constraints(spec)
        after = (
            [(r.points, r.kind, r.name) for r in spec.rooms],
            [(w.start, w.end, [(o.position, o.width, o.kind) for o in w.openings])
             for w in spec.walls],
            [(getattr(f, "name", "counter"), getattr(f, "insert", None))
             for f in spec.fixtures],
        )
        assert before == after


def test_rules_can_be_run_selectively():
    """可只跑指定規則(給日後分階段檢查用)。"""
    only = [r for r in RULES if r.rule_id == "bedroom_not_facing_kitchen"]
    rep = check_constraints(_spec(), rules=only)
    assert set(rep.checked) | set(rep.skipped) == {"bedroom_not_facing_kitchen"}


# ── 零誤報 ────────────────────────────────────────────────────────────────
def test_generated_layouts_do_not_violate_hard_rules():
    """★ 實際生成的圖:臥室不會直通廚房、大門不會直視馬桶。"""
    for make in _SPECS:
        rep = check_constraints(make())
        assert rep.by_rule("bedroom_not_facing_kitchen") == []
        assert rep.by_rule("entrance_not_facing_toilet") == []


def test_bedroom_living_adjacency_is_not_flagged():
    """★ 臥室與客廳共牆是小宅的必然,不該被列為違反。

    實測 100 層:臥室—客廳共牆 42 次、臥室—廚房共牆 12 次。把客廳算進去會讓
    這條規則觸發率衝到 88% 而失去意義。"""
    assert "living" not in NOISY_PUBLIC_KINDS
    assert "kitchen" in NOISY_PUBLIC_KINDS
    for make in _SPECS:
        for v in check_constraints(make()).by_rule(
                "bedroom_avoids_public_adjacency"):
            assert "客廳" not in v.message and "客餐廳" not in v.message


# ── 每條規則都要抓得到(注入缺陷)────────────────────────────────────────
def test_bedroom_facing_kitchen_is_detected():
    spec = _spec()
    bed, other = _relabel_neighbour(spec, "bedroom", "kitchen", connected=True)
    assert bed is not None                       # 前提:構造得出來
    hits = check_constraints(spec).by_rule("bedroom_not_facing_kitchen")
    assert hits and hits[0].severity == SEVERITY_ERROR
    assert bed in hits[0].rooms and other in hits[0].rooms


def test_bathroom_facing_dining_is_detected():
    spec = _spec()
    bath, other = _relabel_neighbour(spec, "bathroom", "dining", connected=True)
    assert bath is not None
    hits = check_constraints(spec).by_rule("bathroom_not_facing_dining")
    assert hits and hits[0].severity == SEVERITY_ERROR
    assert not check_constraints(spec).ok       # error → 整份不通過


def test_entrance_facing_toilet_is_detected():
    """把馬桶擺到大門正前方(中間無牆)→ 觸發「開門見廁」。"""
    spec = _spec()
    ctx = ConstraintContext.build(spec)
    entrance = next(d for d in ctx.graphs.doors if d.is_exterior)
    room = ctx.polys[entrance.rooms[0]]          # 大門貼著的房間
    c = room.centroid
    ex, ey = entrance.point
    inside = (ex + (c.x - ex) * 0.3, ey + (c.y - ey) * 0.3)   # 門內一點
    spec.fixtures.append(FixturePlacement("toilet", inside, 0))
    hits = check_constraints(spec).by_rule("entrance_not_facing_toilet")
    assert hits and hits[0].severity == SEVERITY_ERROR


def test_bedroom_public_adjacency_is_detected():
    """把臥室的共牆鄰居改標成廚房 → 觸發共牆提醒(warn)。"""
    spec = _spec()
    bed, other = _relabel_neighbour(spec, "bedroom", "kitchen", connected=False)
    if bed is None:                              # 找不到「只共牆不相通」的鄰居
        bed, other = _relabel_neighbour(spec, "bedroom", "kitchen",
                                        connected=True)
    assert bed is not None
    hits = check_constraints(spec).by_rule("bedroom_avoids_public_adjacency")
    assert hits and hits[0].severity == SEVERITY_WARN


def test_kitchen_far_from_dining_is_detected():
    """把一個既不相通也不相鄰的房間改標成餐廳 → 觸發上菜繞路提醒。"""
    spec = _spec()
    ctx = ConstraintContext.build(spec)
    k = ctx.indices("kitchen")
    assert k
    k = k[0]
    far = next((j for j in range(len(ctx.graphs.kinds))
                if j != k and not ctx.connected(k, j) and not ctx.adjacent(k, j)
                and ctx.graphs.kinds[j] not in ("kitchen", "dining")), None)
    assert far is not None
    spec.rooms[far].kind = "dining"
    hits = check_constraints(spec).by_rule("kitchen_near_dining")
    assert hits and hits[0].severity == SEVERITY_WARN


def test_kitchen_adjacent_to_dining_is_not_flagged():
    """★ 廚房與餐廳「只共牆、沒開門」仍算接近,不該報違反。

    實測有獨立餐廳的 13 案中,5 案屬此類——把它當違反會讓規則失準。"""
    spec = _spec()
    ctx = ConstraintContext.build(spec)
    k = ctx.indices("kitchen")[0]
    nb = next((j for j in ctx.graphs.adjacency[k]
               if ctx.graphs.kinds[j] not in ("kitchen", "dining")), None)
    assert nb is not None
    spec.rooms[nb].kind = "dining"
    assert check_constraints(spec).by_rule("kitchen_near_dining") == []


# ── 報表與序列化 ──────────────────────────────────────────────────────────
def test_report_separates_errors_and_warnings():
    rep = ConstraintReport(violations=[
        ConstraintViolation("r1", SEVERITY_ERROR, "壞", ["A"]),
        ConstraintViolation("r2", SEVERITY_WARN, "提醒", ["B"]),
    ], checked=["r1", "r2"])
    assert len(rep.errors) == 1 and len(rep.warnings) == 1
    assert rep.ok is False and "FAIL" in rep.summary()


def test_report_follows_json_convention():
    import json
    rep = check_constraints(_spec())
    d = rep.to_dict()
    assert json.loads(rep.to_json()) == d
    assert set(d) >= {"ok", "error_count", "warning_count",
                      "checked", "skipped", "violations"}
    assert "\\u" not in rep.to_json()            # 中文保持可讀
    v = ConstraintViolation("r", SEVERITY_WARN, "訊息", ["房"])
    assert json.loads(v.to_json())["rooms"] == ["房"]
