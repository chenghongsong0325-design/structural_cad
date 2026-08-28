"""Auto Furnish(v0.7 Phase 7.1b)—— 給「只有房間與牆」的平面自動配家具。

BSP 產生的平面(Phase 7.1a)只有房間/牆/門/窗,沒有家具,Phase 6 的家具類子分數
全都評不出東西。本模組補上這段:**依房間用途決定該擺什麼,再用 Phase 6 既有的
FurniturePlacementOptimizer 挑合法且分數最好的位置**。

⚠️ **不自己寫擺放邏輯**:位置一律由 Phase 6 的 optimizer 決定(內含 collision 硬
閘門 + 走道/人體淨空/擺放偏好/家具關聯/房間語意軟分數),本模組只負責「這種房
該有哪些家具」與「逐件依序放」。

⚠️ 逐件重建 optimizer:前一件擺好後會成為下一件看到的障礙,故同房家具不會重疊
(貪婪、逐件更新;與 MultiRoomOptimizer 同策略)。

⚠️ 流理台(Counter)是參數式、非固定圖塊,optimizer 無法挑位,改用「沿廚房最長
邊貼牆」的幾何擺法,並**先於冰箱**擺(讓冰箱看得到它、不會撞上)。

典型用法::

    spec = bsp_to_spec(theta, flags, 20000, 14000, 3)   # 7.1a:只有牆
    furnish_spec(spec)                                   # 7.1b:補家具
    score_report(spec)                                   # Phase 6 就評得出東西了
"""
from __future__ import annotations

from shapely.geometry import Point, Polygon

from src.design.collision.furniture_engine import FurnitureCollisionEngine
from src.design.collision.placement_optimizer import (
    FurniturePlacementOptimizer,
    PlacementWeights,
)
from src.design.layout.multi_room_optimizer import ROOM_ORDER
from src.design.semantic.room_semantic import canonical_room
from src.drafting.fixtures import (
    Counter, counter_footprint, fixture_footprint,
)

# 每種房間該有的家具(依序擺)。tuple = 依序嘗試,第一個放得下的就用
# (例如主臥先試雙人床,放不下退而求其次單人床)。
FURNITURE_PROGRAM: dict[str, list] = {
    # ⚠️ 床頭櫃不在這張表裡:它的位置是**被床決定的**(床頭兩側各一),
    #    交給擺位器會讓兩個並排擠在同一面牆上。見 `_add_nightstands`。
    "bedroom": [("bed_double", "bed_single"), "wardrobe"],
    "living": ["sofa3", "tv_cabinet", "coffee_table", "armchair"],
    # ⚠️ 餐桌也要有小一號的:`table4` 的原點是桌心,而擺位器對那種家具只在
    #    房間**正中央**試 9 個點 —— 走道型餐廳(4.5m 面寬街屋的 1.8×4.3m)
    #    正中央就是通道,9 個點全被打回 = 一間沒有餐桌的餐廳。`table2` 是
    #    **靠牆**餐桌,走「沿牆找位置」那條路(這個坑車子踩過、床踩過)。
    "dining": [("table4", "table2")],
    "kitchen": ["fridge"],                       # 流理台另由 _add_counter 處理
    # ⚠️ 浴缸 1600×750 放不下 2~4㎡ 的小衛浴,而擺不下的家具是**直接略過**的 ——
    #    改之前 18 間浴室有 14 間一件洗澡設備都沒有。放不下浴缸就擺淋浴間
    #    (台灣這種尺寸的衛浴幾乎都是淋浴)。
    # ⚠️ 洗澡設備要**先擺**:它是這間房裡最大又最不能將就的東西(900×900),
    #    馬桶洗手台先佔位的話它就永遠擠不進去(實測 1.35×2.45m 的浴廁,9 個
    #    候選位置全被門弧與既有家具打回)。馬桶小、後面再排得進去。
    # ⚠️ 洗澡設備不在這張表裡:它由 `_place_bathing` **先擺**(見那支的說明)。
    "bathroom": ["toilet", "basin"],
    "foyer": ["shoe_cabinet"],
    # 更衣室(narrow_house 主臥切出來的那間,kind=storage):吊衣櫃,兩側各一排。
    "storage": [("wardrobe", "closet_rail"), "closet_rail"],
    "study": ["desk", "bookshelf"],
    # ⚠️ 車庫**不在這張表裡**:車不是「找個好位置擺」的家具,是停進車位,
    #    位置由 _park_car 自己列候選(原因見那支的說明)。
}

GARAGE_KINDS = ("garage", "parking")
# 這間房裡的東西是**固定設備**(吊衣桿),位置由房間形狀決定,不是「挑個好位置」;
# 而房間又小到**門一開就掃掉大半個空間** —— 1.35×1.5m 的更衣室,門弧把 12 個
# 候選位置全部打回,結果是一間空房。該讓**門去閃設備**:`_build_floor` 擺完家具
# 之後本來就還會再跑一次 `repair_doors`(轉門把 → 改橫拉門),真實圖上的更衣室
# 就是拉門。(與 `_park_car` 同一個道理,那支是車庫。)
# ⚠️ **浴廁不能放進來**:實測整間一起忽略門弧會讓淋浴間先佔走位置,反而有 4 間
#    連馬桶都擺不下(18→14)。浴廁改用下面的 `_ensure_bathing` 最後補救 ——
#    馬桶洗手台照常先擺,擺完真的沒有洗澡設備才讓門去閃一次。
DOOR_DODGES_KINDS = {"storage"}

BATHING = ("bathtub", "shower")          # 洗澡設備,由大到小
BEDS = ("bed_double", "bed_single")      # 床,由大到小
CLOSET_FIXTURES = ("wardrobe", "closet_rail")   # 更衣室該有的東西
# 浴室小於這個樓地板面積(㎡)就**直接畫淋浴間,不試浴缸**。
# ⚠️ 這不是「放不放得下」的問題:1600×750 的浴缸塞得進 3.3㎡ 的浴廁,但塞進去
#    之後**動線就不通了**,`_declutter_for_circulation` 會把它整個移掉,留下一間
#    只有馬桶的浴室(實測六棟樓被移掉 8 個浴缸)。台灣 3~4㎡ 的浴廁本來就是
#    乾濕分離的淋浴間,不是浴缸。
BATHTUB_MIN_ROOM_M2 = 4.5
CAR_WALL_CLEAR = 100.0       # 車身與牆內面之間留的縫(真實車庫就是這麼緊)

COUNTER_DEPTH = 600.0        # 流理台檯面深(mm)
COUNTER_INSET = 300.0        # 兩端離牆角留的距離(mm)
COUNTER_MIN_LEN = 1500.0     # 太短就不擺流理台
COUNTER_TRIM_STEP = 300.0    # 截短的級距(mm);見 _counter_candidates

DEFAULT_HALF_WALL = 75.0     # 找不到對應牆時的預設半牆厚(mm)
# 內緣縮完至少要留這麼寬才採用。設很小是刻意的:窄到放不下家具的房間本來就不會
# 擺東西,但若因此「不縮」而沿用牆中心線,家具會貼著中心線畫出去 = 穿牆。
MIN_INNER_SIDE = 300.0


def _inner_room(spec, room):
    """把房間縮到**牆的內面**再交給擺位器 → 家具不會陷進牆體。

    房間多邊形走的是牆**中心線**,家具貼齊邊界就等於嵌進半個牆厚(≈75~100mm),
    畫出來就是「家具穿牆」。這裡依每一側實際覆蓋的牆厚往內縮,回一個**臨時 Room**
    (不動 spec 內的房間,面積標註/評分仍用原多邊形)。"""
    from src.drafting.room import Room

    poly = Polygon(room.points)
    x0, y0, x1, y1 = poly.bounds
    ins = {"S": DEFAULT_HALF_WALL, "N": DEFAULT_HALF_WALL,
           "W": DEFAULT_HALF_WALL, "E": DEFAULT_HALF_WALL}
    for w in spec.walls:
        (sx, sy), (ex, ey) = w.start, w.end
        half = w.thickness / 2.0
        if abs(sx - ex) < 1.0:                       # 垂直牆 → 東西側
            if min(sy, ey) < y1 - 1 and max(sy, ey) > y0 + 1:
                if abs(sx - x0) < 60:
                    ins["W"] = max(ins["W"], half)
                elif abs(sx - x1) < 60:
                    ins["E"] = max(ins["E"], half)
        elif abs(sy - ey) < 1.0:                     # 水平牆 → 南北側
            if min(sx, ex) < x1 - 1 and max(sx, ex) > x0 + 1:
                if abs(sy - y0) < 60:
                    ins["S"] = max(ins["S"], half)
                elif abs(sy - y1) < 60:
                    ins["N"] = max(ins["N"], half)
    nx0, ny0 = x0 + ins["W"], y0 + ins["S"]
    nx1, ny1 = x1 - ins["E"], y1 - ins["N"]
    if nx1 - nx0 < MIN_INNER_SIDE or ny1 - ny0 < MIN_INNER_SIDE:
        return room                                  # 太小就不縮(免得擺不下任何東西)
    return Room(name=room.name, kind=room.kind,
                points=[(nx0, ny0), (nx1, ny0), (nx1, ny1), (nx0, ny1)])


ENTRY_PROBE = 500.0          # 從大門往室內探這麼遠,看落在哪間房(mm)
# 進門就有的東西。⚠️ 「玄關」在這種房子裡**不是一間房**:4~8m 面寬的透天擠不出
# 獨立玄關,真實街屋也是進門就是客廳 —— 所以 FURNITURE_PROGRAM["foyer"] 從來沒有
# 觸發過,全棟一個鞋櫃都沒有。改成**認門不認房名**:大門開在哪間房的牆上,鞋櫃就
# 擺那間。(車庫不算:那扇是捲門,鞋櫃不會擺在車頭前面。)
ENTRY_FURNITURE = "shoe_cabinet"
ENTRY_SKIP_KINDS = set(GARAGE_KINDS) | {"stair_hall", "corridor", "bathroom",
                                        "toilet", "storage"}


def _entry_doors(spec) -> dict:
    """{房間 index: 大門座標} —— 哪幾間房直接連著**對外大門**(= 這棟房子的玄關)。

    判準與 `plan_check` 的 `no_entry` 同一把尺:門洞在建築外框上、而且不是通往
    陽台的落地門(那是陽台的門,不是大門)。"""
    from src.design.layout.balcony import door_opens_to_balcony
    from src.design.layout.plan_check import building_env, _on_envelope

    env = building_env(spec)
    polys = [Polygon(r.points) for r in spec.rooms]
    out: dict = {}
    for wall in spec.walls:
        ux, uy = wall.unit_vector
        nx, ny = -uy, ux                                  # 牆的法線
        for op in wall.openings:
            if op.kind != "door":
                continue
            px, py = wall.point_at(op.position)
            if not _on_envelope(px, py, env):
                continue
            if door_opens_to_balcony(spec, wall, op):
                continue
            for sgn in (1, -1):                           # 室內在哪一側不一定
                probe = (px + nx * ENTRY_PROBE * sgn, py + ny * ENTRY_PROBE * sgn)
                for i, poly in enumerate(polys):
                    if poly.contains(Point(*probe)):
                        out.setdefault(i, (px, py))
    return out


def _place_near_door(spec, room, name, door_pt) -> bool:
    """把 name 擺在**離這扇門最近**的合法位置(鞋櫃用)。

    ⚠️ 鞋櫃的位置是被**大門**決定的,不是「哪面牆分數高」。交給擺位器挑,
    4.5m 街屋的鞋櫃會落在離大門 3.9m 的客廳角落 —— 圖上就是一個莫名其妙的矮櫃。
    (與 `_add_nightstands` 跟著床、`_park_car` 停進車位同一個道理:位置被別的
    東西決定的家具,不要交給「挑好位置」的擺位器。)

    合法與否仍走既有的碰撞引擎(不撞牆/門迴轉/既有家具),只是**改用距離排序**
    取代分數排序。"""
    opt = FurniturePlacementOptimizer(spec)
    inner = _inner_room(spec, room)
    poly = Polygon(inner.points)
    door = Point(*door_pt)
    best, best_d = None, None
    for pl in opt.candidates(name, inner):
        fp = Polygon(fixture_footprint(pl))
        if not poly.buffer(1.0).contains(fp):
            continue
        if not opt.engine.check(pl).valid:
            continue
        d = fp.centroid.distance(door)
        if best_d is None or d < best_d:
            best, best_d = pl, d
    if best is None:
        return False
    spec.fixtures.append(best)
    return True


def _room_priority(room) -> int:
    kind = canonical_room(room.kind)
    return ROOM_ORDER.index(kind) if kind in ROOM_ORDER else len(ROOM_ORDER)


def _counter_candidates(room):
    """沿四面牆的流理台候選,**由長到短**(檯面往室內側伸)。

    ⚠️ 不是只有「整面牆那麼長」一種。窄面寬街屋的廚房是 1.85×4.3m 的**走道型**,
    唯一夠長的那面牆上就開著門 —— 整條檯面必定撞到門迴轉,於是**一件都不擺**
    (實測 4.5/5.45/6m 面寬的廚房全部沒有流理台,而那正是台灣最常見的面寬)。
    真實師傅遇到擋路的東西是把檯面**截短**,不是整條不做 —— 本專案的
    `trim_counters_at_columns` 遇到柱早就是這樣處理了,這裡只是把同一件事
    提前到「排候選」這一步。

    候選 = 四面牆 × 兩個錨定端(靠這頭 / 靠那頭)× 由長到短每級 300mm。
    最後**依長度排序**,所以 `_add_counter` 取到的一定是「放得下的最長那條」——
    不會為了湊一面長牆而擺出一條 1.5m 的短檯面。"""
    x0, y0, x1, y1 = Polygon(room.points).bounds
    w, d = x1 - x0, y1 - y0
    ins = COUNTER_INSET
    # (可用長度, start, end):方向決定「左手側=室內」,見 draw_counter。
    walls = [
        (w, (x0 + ins, y0), (x1 - ins, y0)),     # 南牆 → +y 進室內
        (w, (x1 - ins, y1), (x0 + ins, y1)),     # 北牆 → -y 進室內
        (d, (x1, y0 + ins), (x1, y1 - ins)),     # 東牆 → -x 進室內
        (d, (x0, y1 - ins), (x0, y0 + ins)),     # 西牆 → +x 進室內
    ]
    walls.sort(key=lambda t: -t[0])              # 長邊優先(同長度時的順序)
    out: list = []
    for order, (span, start, end) in enumerate(walls):
        full = span - 2 * ins
        if full < COUNTER_MIN_LEN:
            continue
        (sx, sy), (ex, ey) = start, end
        ux, uy = (ex - sx) / full, (ey - sy) / full          # 單位向量
        length = full
        while length >= COUNTER_MIN_LEN:
            ends = [(start, (sx + ux * length, sy + uy * length))]   # 錨在起點
            if length < full:                                        # 錨在終點
                ends.append(((ex - ux * length, ey - uy * length), end))
            for s, e in ends:
                out.append((length, order, Counter(
                    start=s, end=e, depth=COUNTER_DEPTH,
                    sink=True, stove=True)))
            length -= COUNTER_TRIM_STEP
    out.sort(key=lambda t: (-t[0], t[1]))        # 長的優先,同長度時長牆優先
    return [c for _len, _o, c in out]


def _add_counter(spec, room) -> bool:
    """沿牆擺一段流理台;由 Phase 6 碰撞引擎把關(不撞牆/門迴轉/既有家具)。

    逐面牆試,取第一個「檯面落在房內 + 通過碰撞查詢」的候選。都不行就不擺。"""
    inner = _inner_room(spec, room)                  # 貼牆內面,不嵌進牆體
    poly = Polygon(inner.points)
    engine = FurnitureCollisionEngine(spec)
    for counter in _counter_candidates(inner):
        if not poly.buffer(1.0).contains(Polygon(counter_footprint(counter))):
            continue
        if engine.check(counter).valid:          # 不撞門迴轉/牆/既有家具
            spec.fixtures.append(counter)
            return True
    return False


def _place_bathing(spec, room, weights=None, names=BATHING) -> bool:
    """浴廁的洗澡設備(浴缸 → 放不下就淋浴間)。**在馬桶洗手台之前擺。**

    ⚠️ 兩件事都是量出來的:
    ① **先擺**:它是這間房裡最大又最不能將就的東西。馬桶洗手台先佔位的話它就
       永遠擠不進去 —— 實測 18 間浴廁,先擺馬桶只補得到 6 間、先擺它補到 10 間,
       而且馬桶洗手台一間都沒少。
    ② 第一輪照常閃門;**真的擺不下才讓門去閃設備**再試一輪。1.35×2.45m 的浴廁
       (4.5~6m 面寬街屋的標配)門弧會把候選位置全打回,而一間沒地方洗澡的浴室
       是不完整的圖。真實圖上這種小浴室本來就用橫拉門/外開門,`repair_doors`
       在擺完家具之後會把門收掉。
       ⚠️ 不能一開始就忽略門弧:整間浴廁都那樣做,淋浴間會挑到擋門的位置,
       反而有 4 間連馬桶都擺不下(18→14)。
    """
    inner = _inner_room(spec, room)
    if Polygon(inner.points).area / 1.0e6 < BATHTUB_MIN_ROOM_M2:
        names = tuple(n for n in names if n != "bathtub") or names
    for dodge in (False, True):                      # 先讓設備閃門,不行才反過來
        for name in names:
            opt = FurniturePlacementOptimizer(spec)
            if dodge:
                opt.engine.doors = []
            res = opt.place(name, inner, weights=weights)
            if res.found:
                spec.fixtures.append(res.best.placement())
                return True
    return False


NIGHTSTAND_GAP = 50.0        # 床與床頭櫃之間留的縫(mm)


def _add_nightstands(spec, room, weights=None) -> int:
    """床頭**兩側各一個**床頭櫃 —— 位置是被床決定的,不是「挑個好位置」。

    ⚠️ 交給擺位器的話,兩個床頭櫃會並排擠在同一面牆上(實測 2F 主臥就是這樣),
    那不是真實圖面的畫法。這裡直接算床頭兩側的位置,再交碰撞引擎把關;某一側
    放不下(碰牆/撞門/撞別的家具)就只擺另一側。回擺了幾個。
    """
    import math

    from shapely.geometry import Point

    from src.drafting.fixtures import FIXTURE_SIZES, FixturePlacement
    poly = Polygon(room.points).buffer(1.0)
    bed = next((f for f in spec.fixtures
                if getattr(f, "name", "") in BEDS
                and poly.contains(Point(*f.insert))), None)
    if bed is None:
        return 0
    half = FIXTURE_SIZES[bed.name][0] / 2.0 + NIGHTSTAND_GAP         + FIXTURE_SIZES["nightstand"][0] / 2.0
    th = math.radians(bed.rotation)
    ux, uy = math.cos(th), math.sin(th)              # 沿牆方向(床頭那道牆)
    added = 0
    for sgn in (-1.0, 1.0):
        ns = FixturePlacement("nightstand",
                              (bed.insert[0] + ux * half * sgn,
                               bed.insert[1] + uy * half * sgn),
                              bed.rotation)
        if FurnitureCollisionEngine(spec).check(ns).valid:
            spec.fixtures.append(ns)
            added += 1
    return added


def _has_any(spec, room, names) -> bool:
    """這間房裡有沒有這幾種家具的任何一件。"""
    from shapely.geometry import Point
    poly = Polygon(room.points).buffer(1.0)
    return any(getattr(f, "name", "") in names and poly.contains(Point(*f.insert))
               for f in spec.fixtures)


def _restore_one(spec, room, names, weights=None, dodge_doors=False) -> bool:
    """在 room 裡補一件 names(由小到大試),補到就回 True。"""
    inner = _inner_room(spec, room)
    for name in names:
        opt = FurniturePlacementOptimizer(spec)
        if dodge_doors:
            opt.engine.doors = []
        res = opt.place(name, inner, weights=weights)
        if res.found:
            spec.fixtures.append(res.best.placement())
            return True
    return False


def restore_essentials(spec, weights=None) -> int:
    """動線修復器把「這間房非有不可的東西」移掉之後,換**小一號**的補回來。

    ⚠️ `_declutter_for_circulation` 只看動線,它不知道那個浴缸是這間浴室**唯一**
    的洗澡設備、也不知道床是臥室的重點 —— 實測六棟樓被它拿掉 8 個浴缸與 3 張
    雙人床,留下「只有馬桶的浴室」與「沒有床的臥室」,那是不完整的圖。

    補回來的一律是**更小的替代品**(浴缸 → 淋浴間、雙人床 → 單人床):原尺寸
    再放一次只會再被移掉一次。補完呼叫端要再跑一次動線修復;還是擋路就維持
    原狀,不硬塞。回補了幾件。
    """
    added = 0
    for room in spec.rooms:
        kind = canonical_room(room.kind)
        if kind == "bathroom":
            # ⚠️ **馬桶要先救**:一間沒有馬桶的廁所比沒有淋浴間更離譜,而動線
            #    修復器不知道這件事。兩扇門的小浴室(1.3×2.6m:南邊套內、北邊
            #    對走道)兩個門弧一夾,通道只剩中間一條,它就把馬桶整個移掉了。
            #    馬桶沒有「小一號」的替代品,所以改**讓門去閃設備**(與更衣室
            #    同一招)—— 後面的 `repair_doors` 會把這種小浴室的門收成拉門,
            #    真實圖上的小浴室本來就是拉門。
            if not _has_any(spec, room, ("toilet",)):
                added += bool(_restore_one(spec, room, ("toilet",), weights,
                                           dodge_doors=True))
            if not _has_any(spec, room, BATHING):
                added += bool(_place_bathing(spec, room, weights,
                                             names=("shower",)))
        elif kind == "bedroom" and not _has_any(spec, room, BEDS):
            added += bool(_restore_one(spec, room, ("bed_single", "bed_double"),
                                       weights))
        elif kind == "storage" and not _has_any(spec, room, CLOSET_FIXTURES):
            # 更衣室被清空的話,一樣要補 —— 一間空的更衣室在圖上就是漏畫。
            # ⚠️ 這間房**讓門去閃設備**(見 DOOR_DODGES_KINDS),補的時候
            #    也要照做,否則門弧會把候選位置全部打回、補了等於沒補。
            added += bool(_restore_one(spec, room, ("closet_rail",), weights,
                                       dodge_doors=True))
    return added


def _rooms_with_beds(spec) -> set:
    """目前哪幾間臥室有床(拿來比對「加分項有沒有把床弄不見」)。"""
    return {id(r) for r in spec.rooms
            if canonical_room(r.kind) == "bedroom" and _has_any(spec, r, BEDS)}


def refit_nightstands(spec, weights=None) -> int:
    """床旁邊沒有床頭櫃就補上 —— 補回來的床是**新位置**,舊床頭櫃早被清掉了。

    ⚠️ 補完**仍然要再過一次動線與穿牆兩道修復器** —— 不過的話淺透天掃描會冒出
    `furniture_in_wall` / `circulation_blocked`(實測 2 案不合格)。但那兩支可能
    反過來把床搬走(床頭櫃讓那間房剛好擠不下通道),所以呼叫端會**整批回退**:
    一張床比兩個床頭櫃重要得多,這是本檔那條鐵則 ——
    **加分項不得讓原本好好的東西壞掉**。回擺了幾個。"""
    added = 0
    for room in spec.rooms:
        if canonical_room(room.kind) != "bedroom":
            continue
        if not _has_any(spec, room, BEDS) or _has_any(spec, room, ("nightstand",)):
            continue
        added += _add_nightstands(spec, room, weights)
    return added


def drop_orphan_nightstands(spec) -> int:
    """沒有床的房間裡不該留著床頭櫃(那是床的配件)。回移除件數。

    ⚠️ 會發生是因為順序:床頭櫃是**跟著床**擺的,但後面的動線修復器可能把床
    移掉(房間有兩扇門、通道非過不可時)。留下兩個貼著牆的小方塊、房間裡沒有
    床,圖上看起來像漏畫。"""
    from shapely.geometry import Point
    victims = []
    for room in spec.rooms:
        if canonical_room(room.kind) != "bedroom" or _has_any(spec, room, BEDS):
            continue
        poly = Polygon(room.points).buffer(1.0)
        victims += [f for f in spec.fixtures
                    if getattr(f, "name", "") == "nightstand"
                    and poly.contains(Point(*f.insert))]
    for f in victims:
        spec.fixtures.remove(f)
    return len(victims)


def settle_after_declutter(spec) -> None:
    """動線修復器跑完之後的收尾:補回被移掉的必需品、清掉孤兒床頭櫃。

    ⚠️ **四條產線都要接。** 本專案一再踩到的坑是「規則寫好了,但只接了一條
    產線」—— `column_centers is None`、單跑直梯、開口壓柱都是這樣漏掉的。
    這支包成一個呼叫,就是為了讓每個 `_declutter_for_circulation` 後面都能
    只加一行。"""
    from src.design.layout.fixture_fix import clear_fixtures_off_columns
    from src.design.layout.graph_layout import _declutter_for_circulation
    # ⚠️ **順序**:孤兒床頭櫃要先清掉,床才補得回去 —— 床頭櫃是跟著床擺的,
    #    床被移走後那兩個小方塊還停在床頭原位,正好卡住床要回去的地方
    #    (實測 7×16 的前臥室、8×16.45 的小孩房都是這樣補不回來的)。
    drop_orphan_nightstands(spec)
    if restore_essentials(spec):
        _declutter_for_circulation(spec)
        drop_orphan_nightstands(spec)     # 補完再清一次(可能又被移掉)
    # 床頭櫃最後才補(補回來的床是新位置,舊床頭櫃早被清掉了)。補完要再過
    # 一次修復器,而修復器可能反過來把床搬走 → 有床不見就**整批回退**。
    before = list(spec.fixtures)
    had_beds = _rooms_with_beds(spec)
    if refit_nightstands(spec):
        # ⚠️ 新床頭櫃也要閃柱(產線的 clear_fixtures_off_columns 早就跑過了,
        #    輪不到它們)—— 不補這行,車庫掃描的 furniture_in_column 3→8。
        #    順序照舊:閃柱在動線修復**之前**,動線要有最後決定權。
        clear_fixtures_off_columns(spec)
        _declutter_for_circulation(spec)
        _drop_furniture_in_walls(spec)
        drop_orphan_nightstands(spec)
        if not _rooms_with_beds(spec) >= had_beds:
            spec.fixtures[:] = before


def _park_car(spec, room) -> bool:
    """車庫的車:**停進車位**,不是「找最佳位置」。

    先靠捲門那一端停,另一端才留得下人繞過車頭走到室內門;同一端再試置中/靠西/
    靠東三條車道。合法性一樣交給碰撞引擎把關(同 `_add_counter`)。

    ⚠️ 為什麼不走擺位器:車是 CENTER_ORIGIN 家具,擺位器只在房間**正中央**試
    9 個點(中心 ±step)。3.8m 寬 × 5.5m 深的車庫(4.5m 面寬街屋)正中央必定壓到
    室內門的開啟弧 —— 實測 9 個候選全被 door_swing 打回,一輛車都擺不出來,
    圖上就是一間空房。這不是擺位器的錯:它的前提是「家具可以挑位置」,而車位
    的位置是被車庫形狀決定的。
    """
    from src.drafting.fixtures import (
        FIXTURE_SIZES, FixturePlacement, fixture_footprint,
    )

    inner = _inner_room(spec, room)                  # 貼牆內面,不嵌進牆體
    poly = Polygon(inner.points)
    x0, y0, x1, y1 = poly.bounds
    w, d = FIXTURE_SIZES["car"]
    if x1 - x0 < w + 2 * CAR_WALL_CLEAR or y1 - y0 < d + 2 * CAR_WALL_CLEAR:
        return False                                 # 這間車庫塞不下一台車
    engine = FurnitureCollisionEngine(spec)
    # ⚠️ **車不閃門,門閃車。** 4.5m 面寬街屋的車庫內深 5.53m、車長 4.6m,只剩
    #    925mm 餘裕;而一扇內門的開啟弧就要 850mm —— 要車讓門的話車永遠停不進去
    #    (實測 6 個候選全被 door_swing 打回),圖上就是一間空房。真實車庫的門
    #    本來就不會往車頭上開。`_build_floor` 在擺完家具後還會再跑一次
    #    `repair_doors`(轉門把 → 改橫拉門),讓門去閃車才是對的順序。
    engine.doors = []
    lanes = [(x0 + x1) / 2.0,                        # 置中 → 靠西 → 靠東
             x0 + w / 2.0 + CAR_WALL_CLEAR,
             x1 - w / 2.0 - CAR_WALL_CLEAR]
    ends = [y0 + d / 2.0 + CAR_WALL_CLEAR,           # 靠捲門(南)→ 靠內側(北)
            y1 - d / 2.0 - CAR_WALL_CLEAR]
    for cy in ends:
        for cx in lanes:
            car = FixturePlacement("car", (cx, cy), 0.0)
            if not poly.buffer(1.0).contains(Polygon(fixture_footprint(car))):
                continue
            if engine.check(car).valid:              # 不撞門迴轉/牆/柱/既有家具
                spec.fixtures.append(car)
                return True
    return False


def _drop_furniture_in_walls(spec, tol: float = 1000.0) -> int:
    """移掉「畫出來會嵌進牆體」的家具(最後防線)。

    擺位器判斷放不放得下時用的是**碰撞尺寸**,比**繪圖尺寸**小一點;房間剛好卡在
    中間時,它會以為 2.0m 的沙發塞得進 1.85m 的內緣,畫出來就凸進牆裡。這裡直接
    以繪圖佔地驗一次,凸出去的就不擺——**畫面正確優先於家具齊全**。回移除件數。"""
    from shapely.geometry import LineString

    from src.design.collision.geometry import fixture_obstacles

    bodies = [LineString([w.start, w.end]).buffer(w.thickness / 2.0,
                                                 cap_style=2, join_style=2)
              for w in spec.walls]
    victims = [o.ref for o in fixture_obstacles(spec)
               if sum(o.poly.intersection(b).area for b in bodies) > tol]
    for v in victims:
        try:
            spec.fixtures.remove(v)
        except ValueError:
            pass
    return len(victims)


def _merged_kinds(room, kind: str) -> tuple[str, ...]:
    """這間房實際上是哪幾種空間併成的(「餐廚」= 廚房+餐廳),沒併就回自己。

    ⚠️ 判準的單一出處是 `plan_check.MERGED_ROOM_PARTS` —— 那邊早就知道「餐廚」
       要拿兩間的面積上限相加,擺家具的這邊卻不知道,於是開放式餐廚只擺得出
       餐桌、沒有流理台(或反過來)。**同一件事不能兩個地方兩把尺。**
    """
    from src.design.layout.plan_check import MERGED_ROOM_PARTS

    for hint, parts in MERGED_ROOM_PARTS.items():
        if hint in getattr(room, "name", ""):
            return parts
    return (kind,)


def _program_for(room, kind: str) -> list:
    """這間房該擺哪些家具;併合房(餐廚/客餐廳)拿兩份清單接起來。"""
    kinds = _merged_kinds(room, kind)
    if len(kinds) == 1:
        return FURNITURE_PROGRAM.get(kind, [])
    plan: list = []
    for k in kinds:
        for item in FURNITURE_PROGRAM.get(k, []):
            if item not in plan:
                plan.append(item)
    return plan


def furnish_spec(spec, *, weights: PlacementWeights | None = None):
    """就地把 spec 的每個房間配上家具(回傳同一個 spec,方便串接)。

    位置一律由 Phase 6 的 FurniturePlacementOptimizer 決定;擺不下的家具直接略過
    (不硬塞、不產生非法佈局)。"""
    rooms = sorted(spec.rooms, key=_room_priority)
    entry = {id(spec.rooms[i]): pt                            # 大門在哪間房
             for i, pt in _entry_doors(spec).items()}
    for room in rooms:
        kind = canonical_room(room.kind)
        if kind in GARAGE_KINDS:                     # 車位不是「擺家具」,見 _park_car
            _park_car(spec, room)
            continue
        plan = _program_for(room, kind)
        if "kitchen" in _merged_kinds(room, kind):   # 流理台先擺,冰箱才看得到它
            _add_counter(spec, room)
        if kind in ("bathroom", "toilet"):           # 洗澡設備先擺,見 _place_bathing
            _place_bathing(spec, room, weights)
        if id(room) in entry and kind not in ENTRY_SKIP_KINDS:
            # 進門就要有鞋櫃,而且要在**門邊**。⚠️ 跟洗澡設備一樣要**先擺**:
            #    沙發/電視櫃先佔位的話,門邊全滿,鞋櫃會被擠到 4.5m 外的北牆。
            _place_near_door(spec, room, ENTRY_FURNITURE, entry[id(room)])
        inner = _inner_room(spec, room)              # 可擺範圍=牆內面(家具不穿牆)
        for item in plan:
            names = item if isinstance(item, tuple) else (item,)
            for name in names:
                opt = FurniturePlacementOptimizer(spec)
                if kind in DOOR_DODGES_KINDS:       # 門閃設備,見上面那段
                    opt.engine.doors = []
                res = opt.place(name, inner, weights=weights)
                if res.found:
                    spec.fixtures.append(res.best.placement())
                    break                        # 這個位置的家具已定,換下一項
        if kind == "bedroom":                    # 床定位之後才擺得了床頭櫃
            _add_nightstands(spec, room, weights)
    _drop_furniture_in_walls(spec)               # 最後防線:畫出來不能穿牆
    return spec
