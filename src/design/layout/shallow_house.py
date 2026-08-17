"""淺進深透天(樓梯轉 90°)—— 進深小到 5 米也生得出來的另一套骨架。

為什麼要另做一套:窄面寬透天(narrow_house)的樓梯是**南北向**的折返梯,光是樓梯
自己就吃掉 4.4 米進深(起步平台 0.9 + 梯跑 9×0.26 + 折返平台 ≥梯段寬),再加前後
兩段居室,進深至少 9.5 米。基地只有 5 米深時,那套骨架連一間房都放不下。

這裡把**梯段轉成東西向**:樓梯改吃「面寬」不吃「進深」——

    梯帶只佔進深 1.75~2.1 米(兩梯段並排 + 梯井縫),梯跑 4.2 米沿著面寬跑。

於是一層變成前後兩帶:

    北(後):樓梯帶 —— 東西向折返梯 [+ 面寬有剩的話放浴廁]
    南(前):居室帶 —— 1F 客廳(+浴廁)、樓上 臥室(+浴室);臨路大門開在南外牆

定義域(建築尺寸,mm):面寬 5~9 米 × 進深 5 米 ~ narrow_house 的下限。更深的基地
交給 narrow_house(前/核/後三段),那才是正常透天的樣子;這裡只補「淺」的那一塊。

⚠️ 共壁透天:東西外牆是界牆不開窗,採光只能靠南(臨路)、北(後院)兩面。所以
   前段太深就會變暗房 → 進深超過上限時**建築封頂、多的地留成後院**(與其他產線同一
   個做法),不硬蓋。
"""
from __future__ import annotations

from src.design.layout.bsp_layout import rooms_to_spec
from src.design.layout.narrow_house import (
    BATH_MAX_D,
    BATH_MAX_W,
    BATH_MIN_W,
    ENTRY_LANDING,
    FLOOR_HEIGHT,
    MAX_RISER,
    MIN_TREAD,
    SETBACK,
    STAIR_TREAD,
    STAIR_WELL_GAP,
    TURN_LANDING_MIN,
    WALL_GAP,
    _add_stair_guard_walls,
    _ensure_floor_connected,
    _ensure_room_doors,
    _ensure_room_windows,
    _fix_openings,
    _rect,
    _set_structural_grid,
)
from src.drafting.stair import UStair

# 定義域(建築尺寸 mm)。
MIN_WIDTH, MAX_WIDTH = 5000.0, 9000.0
MIN_DEPTH = 5000.0
# 進深上限 = 梯帶 + 前段;前段是單面(南)採光,太深就是暗房(§40 開不出 1/8 的窗)。
FRONT_MAX_D = 4600.0

# 梯帶進深:兩梯段並排 + 梯井縫 + 兩側牆縫。1750 時梯段淨寬剛好 750(法規下限),
# 這裡多給一點(1900 → 梯段 825)走起來才不擠;面寬吃緊時再降回 1750。
STAIR_BAND_D = 1900.0
STAIR_BAND_D_MIN = 1750.0
BATH_MIN_D = 1600.0             # 浴廁最小進深(淺骨架比 narrow_house 再緊一點)
# 前段分間的最小開間:切得比這窄就變走廊狀的房間,寧可不切(小宅=開放式廚房)。
MIN_LIVING_W, MIN_KITCHEN_W, MIN_BEDROOM_W = 2500.0, 2000.0, 2600.0
MAX_KITCHEN_W = 3500.0          # 廚房再寬就過大(多的寬度給客廳)
# 每間臥室都要**自己**有一段牆面對著樓梯間(門開在那裡),否則只能穿隔壁房進去。
# 850 門 + 兩側牆角淨距(DOOR_CLEAR_STEPS 的第二級 250)+ 餘裕。
MIN_HALL_FRONTAGE = 1450.0


def _stair_run_needed(band_d: float, tread: float = STAIR_TREAD) -> float:
    """這個梯帶深度、這個踏面,梯跑要吃掉多少**面寬**(含兩側牆縫)。"""
    flight_w = (band_d - 2 * WALL_GAP - STAIR_WELL_GAP) / 2.0
    spf = _steps_per_flight()
    # §33:折返端平臺深不得小於梯段寬。
    return (ENTRY_LANDING + spf * tread + max(TURN_LANDING_MIN, flight_w)
            + 2 * WALL_GAP)


def _steps_per_flight() -> int:
    """每梯段級數(層高 / 每階升高上限,再折半;向上取整)。"""
    total = max(4, -(-int(FLOOR_HEIGHT) // int(MAX_RISER)))
    return max(2, -(-total // 2))


def _band_depth(W: float) -> float:
    """這個面寬撐得起多深的梯帶(越深梯段越寬,但梯跑也越長)。"""
    for band in (STAIR_BAND_D, STAIR_BAND_D_MIN):
        if _stair_run_needed(band) <= W:
            return band
    return STAIR_BAND_D_MIN


def _stair(bx0, bx1, y0, y1, label):
    """梯帶內的東西向折返梯(往東上),**西端留起步平台**。

    與 narrow_house 的樓梯同一組規則(級高 ≤190、踏面 ≥210、平臺深 ≥梯段寬、
    門進來先站平地再上階),只是行進方向轉了 90 度:梯跑沿面寬跑。"""
    span = (y1 - y0) - 2 * WALL_GAP                 # 兩梯段並排的總寬
    flight_w = span
    spf = _steps_per_flight()
    need_turn = max(TURN_LANDING_MIN, (flight_w - STAIR_WELL_GAP) / 2.0)
    usable = (bx1 - bx0) - 2 * WALL_GAP
    landing = min(ENTRY_LANDING,
                  max(0.0, usable - spf * MIN_TREAD - need_turn))
    run_len = usable - landing
    tread = STAIR_TREAD
    if spf * tread + need_turn > run_len:           # 太擠 → 縮踏面(仍 ≥法定 210)
        tread = max(MIN_TREAD, (run_len - need_turn) / spf)
    return UStair(origin=(bx0 + WALL_GAP + landing, y0 + WALL_GAP),
                  width=flight_w, length=run_len, direction="east",
                  steps_per_flight=spf, tread=tread,
                  well_gap=STAIR_WELL_GAP, label=label)


def _floor_rooms(level, top, bx0, by0, bx1, by1, no_split=False):
    """一層的房間矩形 + 樓梯。北=梯帶(樓梯[+浴廁]),南=居室帶。

    no_split:樓上不分兩間臥室(由 _build_floor 量過發現第二間的門開不到動線上時
    退回一大間——與其讓人穿別間臥室,不如少切一刀)。"""
    W = bx1 - bx0
    band_d = _band_depth(W)
    yb = by1 - band_d                               # 梯帶南緣
    label = "下" if level == top else "上"

    # 梯帶:梯跑吃掉的面寬之外若還有 ≥浴廁最小寬,浴廁就放這裡(東端),
    # 前段整片留給居室;不夠就整條都是樓梯間,浴廁改放前段東側。
    run_w = _stair_run_needed(band_d)
    spare_w = W - run_w
    bath_in_band = spare_w >= BATH_MIN_W and band_d >= BATH_MIN_D
    xs = bx1 - min(max(spare_w, BATH_MIN_W), BATH_MAX_W) if bath_in_band else bx1

    bath_name = "浴廁" if level == 1 else f"浴室{level}F"
    rooms = [("stair_hall", "樓梯間", _rect(bx0, yb, xs, by1))]
    stair = _stair(bx0, xs, yb, by1, label)
    if bath_in_band:
        rooms.append(("bathroom", bath_name, _rect(xs, yb, bx1, by1)))
        xf = bx1                                    # 前段整片給居室
    else:
        # 浴廁放前段東側(面寬窄時的做法):兩間都貼南外牆,採光才夠。
        bath_w = min(max(BATH_MIN_W, (W - MIN_LIVING_W) / 2.0), BATH_MAX_W)
        xf = bx1 - bath_w
        rooms.append(("bathroom", bath_name, _rect(xf, by0, bx1, by1 - band_d)))
    rooms += _front_rooms(level, bx0, by0, xf, yb, xs, no_split)
    return rooms, stair


def _front_rooms(level, x0, y0, x1, y1, hall_x1, no_split=False):
    """前段(居室帶)的分間:寬得下就分兩間,不夠寬就一間(廚房開放在客廳裡)。

    5 米面寬的小透天硬切廚房只會生出 2 米寬的走廊狀廚房;真實的小宅就是開放式
    廚房。寬到放得下(客廳 2.5m + 廚房 2.0m)才切。

    hall_x1:樓梯間東緣。分間線要往西讓,讓**東邊那間也有一段牆面對樓梯間**,
    門才開得在動線上——切在幾何中線時東房只剩 1.1m 貼著樓梯間,門塞不下,結果
    只能從隔壁房穿過去(門與動線規範明文禁止)。"""
    w = x1 - x0
    if level == 1:
        if w < MIN_LIVING_W + MIN_KITCHEN_W:
            return [("living", "客廳", _rect(x0, y0, x1, y1))]
        xm = x1 - min(max(MIN_KITCHEN_W, w * 0.4), MAX_KITCHEN_W)
        return [("living", "客廳", _rect(x0, y0, xm, y1)),
                ("kitchen", "廚房", _rect(xm, y0, x1, y1))]
    if no_split or w < 2 * MIN_BEDROOM_W:
        return [("bedroom", f"臥室{level}F", _rect(x0, y0, x1, y1))]
    xm = min(x0 + w / 2.0, hall_x1 - MIN_HALL_FRONTAGE)
    if xm - x0 < MIN_BEDROOM_W:                     # 讓不出來就不分間(一大間)
        return [("bedroom", f"臥室{level}F", _rect(x0, y0, x1, y1))]
    return [("bedroom", f"主臥室{level}F", _rect(x0, y0, xm, y1)),
            ("bedroom", f"次臥室{level}F", _rect(xm, y0, x1, y1))]


def _daylight_ok(spec) -> bool:
    """每間居室的窗都開夠了嗎(§40 樓地板 1/8)。

    **實際量過才算數**:前段能開窗的只有南面那道牆(東西是界牆、北面被梯帶擋住),
    牆就那麼長,房間一深就補不滿。不夠就由呼叫端把建築進深再收一點、留成後院。"""
    from src.design.layout.narrow_house import WINDOW_KINDS, _need_window_width
    from shapely.geometry import Point, Polygon
    for room in spec.rooms:
        if room.kind not in WINDOW_KINDS:
            continue
        poly = Polygon(room.points)
        have = sum(op.width for w in spec.walls for op in w.openings
                   if op.kind == "window"
                   and poly.exterior.distance(
                       Point(*w.point_at(op.position))) < 60)
        if have < _need_window_width(room) - 1.0:
            return False
    return True


def _build_floor(level, top, W, D, floor_label, furnish=True, cap=None,
                 no_split=False):
    """組一層 spec(房間 → 牆/門/窗 + 樓梯 + 開口收尾 + 家具)。

    進深超過上限時**建築封頂、多的留成後院**(前緣貼建築線、院子在後)——淺骨架
    的前段只有南面採光,硬加深只會生出暗房。上限先用 FRONT_MAX_D 估,再由
    _daylight_ok 實際量;量不過就每次收 250mm 重生,直到窗開得夠。"""
    build_d = min(D, cap if cap is not None else _band_depth(W) + FRONT_MAX_D)
    bx0 = by0 = SETBACK                             # 前緣貼建築線,院子留在後面
    bx1, by1 = SETBACK + W, by0 + build_d
    site_w, site_d = W + 2 * SETBACK, D + 2 * SETBACK

    rooms, stair = _floor_rooms(level, top, bx0, by0, bx1, by1, no_split)
    spec = rooms_to_spec(rooms, (bx0, by0, bx1, by1), site_w, site_d,
                         setback=SETBACK)
    spec.stairs = [stair]                           # 先掛樓梯:開口收尾才避得開梯段
    _add_stair_guard_walls(spec)                    # 梯段兩側都要有牆(不能一側懸空)
    _fix_openings(spec, bx0, by0, bx1, level)       # 去重複門/界牆窗;1F 補臨路大門
    _ensure_floor_connected(spec)                   # 從大門走得到每一間房
    _ensure_room_doors(spec, bx0, by0, bx1, level)  # 每房都有門
    from src.design.layout.balcony import add_balconies
    # 2F 以上挑陽台;北側是梯帶(樓梯間不配陽台),實際上只會生出南向前陽台。
    add_balconies(spec, level, env=(bx0, by0, bx1, by1))
    _ensure_room_windows(spec, bx0, by0, bx1, by1)  # 居室補窗(§40 依面積補到 1/8)
    from src.design.layout.door_rules import check_door_rules, repair_doors
    repair_doors(spec, bx0, by0, bx1, level)        # 門與動線規範:改門(不改切法)
    if not no_split and any(i.code == "through_bedroom"
                            for i in check_door_rules(spec, None, level, floor_label)):
        # 第二間臥室的門開不到樓梯間(門前那段牆被梯段佔住)→ 退回一大間。
        return _build_floor(level, top, W, D, floor_label, furnish, cap,
                            no_split=True)
    if not _daylight_ok(spec) and build_d - 250.0 >= _band_depth(W) + 2500.0:
        return _build_floor(level, top, W, D, floor_label, furnish,
                            cap=build_d - 250.0,     # 收一點進深、多留後院
                            no_split=no_split)
    spec.floor_label = floor_label
    _set_structural_grid(spec, bx0, by0, W, build_d)   # 柱放軸網交點,與窄透天同一套
    repair_doors(spec, bx0, by0, bx1, level)     # 柱是實心的,門扇掃到柱就打不開(見窄透天同段)
    if furnish:
        from src.design.layout.auto_furnish import furnish_spec
        from src.design.layout.fixture_fix import (
            clear_fixtures_off_columns, trim_counters_at_columns)
        from src.design.layout.graph_layout import _declutter_for_circulation
        furnish_spec(spec)
        trim_counters_at_columns(spec)              # 有柱之後才需要,見窄透天同段說明
        clear_fixtures_off_columns(spec)
        _declutter_for_circulation(spec)            # 擋動線的家具移掉(四條產線同一套)
        repair_doors(spec, bx0, by0, bx1, level)    # 家具擺完再修一次門(弧線會不會撞家具)
    return spec


def _check_dims(W, D):
    if not MIN_WIDTH <= W <= MAX_WIDTH:
        raise ValueError(
            f"淺進深透天面寬需 {MIN_WIDTH/1000:.1f}~{MAX_WIDTH/1000:.1f}m,"
            f"收到 {W/1000:.1f}m(樓梯轉 90 度後,梯跑要沿面寬跑)")
    if D < MIN_DEPTH:
        raise ValueError(
            f"淺進深透天進深需 ≥{MIN_DEPTH/1000:.1f}m,收到 {D/1000:.1f}m")


def generate_shallow_house(building_w_mm: float, building_d_mm: float, *,
                           furnish: bool = True):
    """淺進深透天單層 → FloorPlanSpec(建築尺寸;基地由退縮反推)。"""
    W, D = float(building_w_mm), float(building_d_mm)
    _check_dims(W, D)
    return _build_floor(1, 1, W, D, "1F", furnish)


def generate_shallow_building(building_w_mm: float, building_d_mm: float, *,
                              floors: int = 3, furnish: bool = True):
    """淺進深透天多層 → [(樓層標示, FloorPlanSpec)]。

    每層共用同一梯帶(樓梯上下對齊);頂層樓梯標「下」,其餘標「上」。"""
    W, D = float(building_w_mm), float(building_d_mm)
    _check_dims(W, D)
    floors = max(1, int(floors))
    return [(f"{lv}F", _build_floor(lv, floors, W, D, f"{lv}F", furnish))
            for lv in range(1, floors + 1)]
