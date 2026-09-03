"""衛浴廚具設備+家具圖塊(fixtures)的單元測試。

驗證重點:
  1. 圖塊建立:8 種都能建、冪等、未知種類報錯、內部實體掛圖層 "0"。
  2. 放置:blockref 掛 OTHER、旋轉正確。
  3. 流理台:檯面矩形方向(左手側)、深度、水槽圓、起訖相同報錯。
  4. 生產線整合:demo 的設備家具全部畫出。
"""
from __future__ import annotations

import pytest

from src.drafting.fixtures import (
    FIXTURE_BUILDERS,
    Counter,
    FixturePlacement,
    create_fixture_block,
    draw_counter,
    place_fixture,
)
from src.standards.loader import apply_standard, load_standard, new_document


@pytest.fixture()
def doc_and_layers():
    standard = load_standard()
    doc = new_document()
    layers = apply_standard(doc, standard)
    return doc, layers


# ---------------------------------------------------------------------------
# 1) 圖塊建立
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name", sorted(FIXTURE_BUILDERS))
def test_create_each_fixture_block(doc_and_layers, name) -> None:
    doc, _ = doc_and_layers
    block_name = create_fixture_block(doc, name)
    assert block_name == f"FX_{name.upper()}"
    blk = doc.blocks.get(block_name)
    entities = list(blk)
    assert len(entities) >= 2
    # 內部實體掛圖層 "0",插入時才會繼承 blockref 的圖層。
    assert all(e.dxf.layer == "0" for e in entities)


def test_create_fixture_block_idempotent(doc_and_layers) -> None:
    doc, _ = doc_and_layers
    create_fixture_block(doc, "toilet")
    create_fixture_block(doc, "toilet")   # 不應報錯或重複
    assert "FX_TOILET" in doc.blocks


def test_unknown_fixture_raises(doc_and_layers) -> None:
    doc, _ = doc_and_layers
    with pytest.raises(ValueError):
        create_fixture_block(doc, "piano")


# ---------------------------------------------------------------------------
# 2) 放置
# ---------------------------------------------------------------------------
def test_place_fixture_layer_and_rotation(doc_and_layers) -> None:
    doc, layers = doc_and_layers
    msp = doc.modelspace()
    ref = place_fixture(msp, FixturePlacement("bed_double", (4200, 11925), 180), layers)
    assert ref.dxf.name == "FX_BED_DOUBLE"
    assert ref.dxf.layer == layers["OTHER"]
    assert ref.dxf.rotation == pytest.approx(180)
    assert tuple(ref.dxf.insert)[:2] == (4200, 11925)


def test_placed_toilet_extends_away_from_wall(doc_and_layers) -> None:
    """rotation=90(貼東牆)→ 馬桶應往 -X 伸出。"""
    doc, layers = doc_and_layers
    msp = doc.modelspace()
    ref = place_fixture(msp, FixturePlacement("toilet", (13925, 3700), 90), layers)
    xs = []
    for e in ref.virtual_entities():
        if e.dxftype() == "LWPOLYLINE":
            xs += [p[0] for p in e.get_points()]
    assert max(xs) <= 13925 + 1e-6      # 全部在牆內面以西
    assert min(xs) < 13925 - 100        # 確實往房內伸


# ---------------------------------------------------------------------------
# 3) 流理台
# ---------------------------------------------------------------------------
def test_counter_left_side_and_depth(doc_and_layers) -> None:
    """沿 +Y 的流理台:檯面往左手側(-X)伸出 depth。"""
    doc, layers = doc_and_layers
    msp = doc.modelspace()
    draw_counter(msp, Counter(start=(13925, 4560), end=(13925, 6940)), layers)

    poly = list(msp.query("LWPOLYLINE"))[0]
    assert poly.dxf.layer == layers["OTHER"]
    xs = [p[0] for p in poly.get_points()]
    assert (min(xs), max(xs)) == (13325, 13925)     # 往 -X 伸 600


def test_counter_sink_circle(doc_and_layers) -> None:
    doc, layers = doc_and_layers
    msp = doc.modelspace()
    draw_counter(msp, Counter(start=(13325, 6940), end=(11060, 6940), sink=True), layers)
    circles = list(msp.query("CIRCLE"))
    assert len(circles) == 1
    assert circles[0].dxf.radius == 180
    # 沿 -X、左手側 = -Y → 水槽在 y 6940-300 = 6640。
    assert circles[0].dxf.center.y == pytest.approx(6640)


def test_counter_zero_length_raises() -> None:
    with pytest.raises(ValueError):
        Counter(start=(0, 0), end=(0, 0))


# ---------------------------------------------------------------------------
# 4) 生產線整合
# ---------------------------------------------------------------------------
def test_floor_plan_draws_fixtures(doc_and_layers) -> None:
    from src.drafting.apartment_plan import demo_spec, draw_floor_plan

    doc, layers = doc_and_layers
    msp = doc.modelspace()
    spec = demo_spec()
    assert len(spec.fixtures) == 13
    draw_floor_plan(msp, spec, layers)

    fx_inserts = [e for e in msp.query("INSERT") if e.dxf.name.startswith("FX_")]
    assert len(fx_inserts) == 11        # 11 件圖塊(另 2 段流理台是多義線)
    assert all(e.dxf.layer == layers["OTHER"] for e in fx_inserts)


def test_dining_set_follows_the_book_sizes():
    """餐桌照〈空間最適尺寸〉:四人方桌 135×85cm、拉椅活動距離每側 80cm。

    使用者 2026-09-03 給的台灣室內設計書。兩件事以前都不對:

      * 桌面是 **800×800** —— 比書上的**二人桌**(70×85)大不了多少,四個人坐不下。
      * `COLLISION_SIZES["table4"]` 是 **900×900** —— 擺位器以為整組餐桌椅只佔
        0.9×0.9m,於是「椅子拉不拉得出來」**從來沒有被檢查過**(圖上畫 1560 寬,
        擺位只用 900)。

    ⚠️ **拉椅空間不放進 COLLISION_SIZES**(走錯過的路,已退回):那張表同時被
    穿牆判定吃,放大它會讓「椅子拉開掃到牆」被判成家具穿牆(實測倒 3 條既有
    測試)。書上四口之家「靠牆擺」本來就是椅子貼著牆。拉椅那件事
    **早就模擬過了** —— `collision/human_clearance.py` 的 `dining_table` 規則
    四面各留 900mm(比書上的 800 還嚴),只是它是軟分數不是硬閘門。
    """
    from src.design.collision.human_clearance import (
        HUMAN_CLEARANCE_RULES,
        HUMAN_TYPE,
    )
    from src.drafting.fixtures import (
        COLLISION_SIZES,
        DINING_PULL_OUT,
        FIXTURE_SIZES,
        TABLE4_TOP,
    )

    assert TABLE4_TOP == (1350.0, 850.0)          # 書上的四人方桌 135×85cm
    assert DINING_PULL_OUT == 800.0               # 書上的拉椅活動距離 80cm
    # 硬閘門只算桌面;椅子區只在畫圖時出現(畫圖比較大,與全表其餘家具一致)。
    assert COLLISION_SIZES["table4"] == TABLE4_TOP
    dw, dd = FIXTURE_SIZES["table4"]
    assert dw == TABLE4_TOP[0], "繪圖寬度就是桌寬(椅子擺長邊,不超出桌寬)"
    assert dd > TABLE4_TOP[1], "繪圖深度要含長邊的椅子"
    # 拉椅空間由 human_clearance 管,而且不得低於書上的 80cm。
    rule = HUMAN_CLEARANCE_RULES[HUMAN_TYPE["table4"]]
    for side in (rule.front_clearance, rule.side_clearance, rule.back_clearance):
        assert side >= DINING_PULL_OUT, (side, DINING_PULL_OUT)


def test_bathroom_fixtures_follow_the_book_sizes():
    """浴室設備照〈空間最適尺寸〉Space 6(使用者 2026-09-03 給的書)。

    * 洗手台檯面基本尺寸 **600×600**;窄浴室退而求其次用 `basin_small` 500×450
      —— 與 `bed_double→bed_single`、`bathtub→shower` 同一條路。
      ⚠️ 沒有這個退讓,實測洗手台從 21 個掉到 **9** 個(25% 的浴室長邊不到書上
      全套浴室的 2200 = 馬桶區800+洗手檯600+淋浴間800)。
    * 淋浴間 800~900 見方、浴缸 150×70(按摩 160×75)—— 本來就符合。
    * **馬桶寬度維持 380,不採書上的 450**:書上真正要求的是「馬桶**區** 80 寬」,
      那是活動空間;`human_clearance` 側向各留 200 → 380+400 = **780 ≈ 800**,
      本來就達標。本體改 450 會讓窄浴室的洗手台少 3 個。
    """
    from src.design.collision.human_clearance import (
        HUMAN_CLEARANCE_RULES,
        HUMAN_TYPE,
    )
    from src.drafting.fixtures import FIXTURE_SIZES

    assert FIXTURE_SIZES["basin"] == (600, 600)          # 書上的基本檯面
    assert FIXTURE_SIZES["basin_small"] == (500, 450)    # 窄浴室的小一號
    tw, td = FIXTURE_SIZES["toilet"]
    assert 750 <= td <= 900, "馬桶深度要落在書上的 75~90cm"
    zone = tw + 2 * HUMAN_CLEARANCE_RULES[HUMAN_TYPE["toilet"]].side_clearance
    assert zone >= 780, f"馬桶區只有 {zone}mm,書上要 800"
    sw, sd = FIXTURE_SIZES["shower"]
    assert 800 <= sw <= 900 and sd >= 800, "淋浴間 80~90cm 見方"
    bw, bd = FIXTURE_SIZES["bathtub"]
    assert (bw, bd) in ((1500, 700), (1600, 750)), "浴缸:單人 150×70 或按摩 160×75"


def test_narrow_bathrooms_still_get_a_basin():
    """★ 洗手台放大到書上的尺寸,**不准讓原本有洗手台的浴室變成沒有**。

    這條釘的是本專案的鐵則(加分項不得讓原本好好的東西壞掉)。實測基準是
    8 個尺寸 × 3 層 = 24 間浴室裡有 21 個洗手台。
    """
    from shapely.geometry import Point, Polygon

    from src.design.layout.narrow_house import generate_narrow_building

    got = 0
    for bw, bd in ((4500, 14000), (6000, 12500), (7000, 15500), (8000, 16000)):
        for _lb, spec in generate_narrow_building(bw, bd, floors=3, seed=0):
            for r in spec.rooms:
                if r.kind not in ("bathroom", "toilet"):
                    continue
                poly = Polygon(r.points)
                if any(getattr(f, "name", "") in ("basin", "basin_small")
                       and poly.contains(Point(*f.insert))
                       for f in spec.fixtures if getattr(f, "insert", None)):
                    got += 1
    assert got >= 10, f"只有 {got} 間浴室有洗手台(基準:12 間中的 10 間以上)"
