"""衛浴廚具設備 + 家具圖塊 —— ROADMAP 階段 B4。

做法比照 door_window:每種設備定義一次 DXF 圖塊(實際尺寸,mm),之後每個
都是一次插入(位置 + 旋轉);圖塊內部實體掛圖層 "0",插入時繼承 blockref
的圖層(OTHER,細線 0.15mm——競賽規範無家具專層,見 PENDING)。

圖塊座標約定(方便「靠牆擺放」):
  * 原點 = 「貼牆邊」的中點,設備朝局部 +Y 伸出(床頭/沙發背/馬桶水箱
    都在原點那一側)。靠牆時 rotation:
        南牆(朝北伸) 0° / 北牆 180° / 東牆 90° / 西牆 270°
  * 桌椅組(table4)例外:原點 = 桌子中心(不靠牆的家具)。

支援的圖塊(FIXTURE_BUILDERS 的 key):
    toilet(馬桶) basin(洗手台) bathtub(浴缸)
    bed_single(單人床) bed_double(雙人床)
    table4(方桌+四椅) sofa3(三人沙發) wardrobe(衣櫃)
    shoe_cabinet(鞋櫃) desk(書桌+椅) car(汽車,原點=車心)
    coffee_table(茶几,原點=中心) armchair(單人沙發) tv_cabinet(電視櫃)
    nightstand(床頭櫃) fridge(冰箱) bookshelf(書櫃)
    bar_stool(吧檯椅,原點=中心;中島吧台配件)

流理台(Counter):長度隨廚房而變,不做成固定圖塊,改用參數式繪製——
start→end 為靠牆邊,檯面往行進方向「左手側」伸出 depth;L 型 = 兩段
Counter 相接。可選 sink=True 在中點畫水槽圓。

典型用法::

    from src.drafting.fixtures import FixturePlacement, Counter, place_fixture, draw_counter

    place_fixture(msp, FixturePlacement("toilet", (13925, 3600), rotation=90), layers)
    draw_counter(msp, Counter(start=(13925, 4560), end=(13925, 6940), sink=False), layers)

⚠️ 待確認假設見模組結尾 PENDING 區塊(各設備尺寸、圖層歸屬、符號畫法)。
"""
from __future__ import annotations

import math
from dataclasses import dataclass

Point = tuple[float, float]


# ---------------------------------------------------------------------------
# 圖塊定義(單位 mm,實際尺寸;原點=貼牆邊中點,朝 +Y 伸出)
# ---------------------------------------------------------------------------
def _build_toilet(blk) -> None:
    """馬桶 380×700:水箱矩形 + 橢圓馬桶座。"""
    blk.add_lwpolyline([(-190, 0), (190, 0), (190, 170), (-190, 170)],
                       close=True, dxfattribs={"layer": "0"})
    blk.add_ellipse(center=(0, 430), major_axis=(0, 260), ratio=170 / 260,
                    dxfattribs={"layer": "0"})


def _build_basin(blk) -> None:
    """洗手台 500×450:檯面矩形 + 內橢圓面盆。"""
    blk.add_lwpolyline([(-250, 0), (250, 0), (250, 450), (-250, 450)],
                       close=True, dxfattribs={"layer": "0"})
    blk.add_ellipse(center=(0, 225), major_axis=(180, 0), ratio=140 / 180,
                    dxfattribs={"layer": "0"})


def _build_bathtub(blk) -> None:
    """浴缸 1600×750:外框 + 內框(壁厚 75)+ 排水圓。"""
    blk.add_lwpolyline([(-800, 0), (800, 0), (800, 750), (-800, 750)],
                       close=True, dxfattribs={"layer": "0"})
    blk.add_lwpolyline([(-725, 75), (725, 75), (725, 675), (-725, 675)],
                       close=True, dxfattribs={"layer": "0"})
    blk.add_circle((-600, 375), radius=50, dxfattribs={"layer": "0"})


def _build_bed(blk, width: float, pillows: int) -> None:
    """床(床頭貼牆):外框 + 枕頭 + 被摺線。"""
    hw = width / 2
    blk.add_lwpolyline([(-hw, 0), (hw, 0), (hw, 2000), (-hw, 2000)],
                       close=True, dxfattribs={"layer": "0"})
    if pillows == 1:
        blk.add_lwpolyline([(-hw + 100, 80), (hw - 100, 80),
                            (hw - 100, 480), (-hw + 100, 480)],
                           close=True, dxfattribs={"layer": "0"})
    else:
        for cx in (-hw / 2, hw / 2):
            blk.add_lwpolyline([(cx - 300, 80), (cx + 300, 80),
                                (cx + 300, 480), (cx - 300, 480)],
                               close=True, dxfattribs={"layer": "0"})
    blk.add_line((-hw, 560), (hw, 560), dxfattribs={"layer": "0"})   # 被摺線


def _build_table4(blk) -> None:
    """方桌 800×800 + 四張椅子 380×380(原點=桌心)。"""
    blk.add_lwpolyline([(-400, -400), (400, -400), (400, 400), (-400, 400)],
                       close=True, dxfattribs={"layer": "0"})
    for cx, cy in ((0, 590), (0, -590), (590, 0), (-590, 0)):
        blk.add_lwpolyline(
            [(cx - 190, cy - 190), (cx + 190, cy - 190),
             (cx + 190, cy + 190), (cx - 190, cy + 190)],
            close=True, dxfattribs={"layer": "0"})


def _build_sofa3(blk) -> None:
    """三人沙發 2000×850(背貼牆):背 + 坐墊 + 兩扶手。"""
    blk.add_lwpolyline([(-1000, 0), (1000, 0), (1000, 200), (-1000, 200)],
                       close=True, dxfattribs={"layer": "0"})          # 背
    blk.add_lwpolyline([(-850, 200), (850, 200), (850, 750), (-850, 750)],
                       close=True, dxfattribs={"layer": "0"})          # 坐墊
    for sx in (-1, 1):
        blk.add_lwpolyline(
            [(sx * 850, 0), (sx * 1000, 0), (sx * 1000, 850), (sx * 850, 850)],
            close=True, dxfattribs={"layer": "0"})                     # 扶手


def _build_wardrobe(blk) -> None:
    """衣櫃 1500×600(背貼牆):外框 + 吊桿線 + 斜線記號。"""
    blk.add_lwpolyline([(-750, 0), (750, 0), (750, 600), (-750, 600)],
                       close=True, dxfattribs={"layer": "0"})
    blk.add_line((-750, 300), (750, 300), dxfattribs={"layer": "0"})   # 吊桿
    blk.add_line((-750, 0), (750, 600), dxfattribs={"layer": "0"})     # 斜線記號


def _build_shoe_cabinet(blk) -> None:
    """鞋櫃 1200×350(背貼牆/隔屏):外框 + 兩條層板線(玄關用)。"""
    blk.add_lwpolyline([(-600, 0), (600, 0), (600, 350), (-600, 350)],
                       close=True, dxfattribs={"layer": "0"})
    for y in (117, 233):
        blk.add_line((-600, y), (600, y), dxfattribs={"layer": "0"})


def _build_desk(blk) -> None:
    """書桌 1200×550(桌面貼牆)+ 椅子 400×400(桌前;書房用)。"""
    blk.add_lwpolyline([(-600, 0), (600, 0), (600, 550), (-600, 550)],
                       close=True, dxfattribs={"layer": "0"})
    blk.add_line((-600, 120), (600, 120), dxfattribs={"layer": "0"})   # 桌板前緣
    blk.add_lwpolyline([(-200, 300), (200, 300), (200, 700), (-200, 700)],
                       close=True, dxfattribs={"layer": "0"})           # 椅子(半塞桌下)


def _build_coffee_table(blk) -> None:
    """茶几 1200×600(原點=中心):外框 + 內縮桌面線(客廳沙發前)。"""
    blk.add_lwpolyline([(-600, -300), (600, -300), (600, 300), (-600, 300)],
                       close=True, dxfattribs={"layer": "0"})
    blk.add_lwpolyline([(-520, -220), (520, -220), (520, 220), (-520, 220)],
                       close=True, dxfattribs={"layer": "0"})


def _build_armchair(blk) -> None:
    """單人沙發 900×850(背貼原點側):背 + 坐墊 + 兩扶手(縮小版 sofa3)。"""
    blk.add_lwpolyline([(-450, 0), (450, 0), (450, 200), (-450, 200)],
                       close=True, dxfattribs={"layer": "0"})           # 背
    blk.add_lwpolyline([(-300, 200), (300, 200), (300, 750), (-300, 750)],
                       close=True, dxfattribs={"layer": "0"})           # 坐墊
    for sx in (-1, 1):
        blk.add_lwpolyline(
            [(sx * 300, 0), (sx * 450, 0), (sx * 450, 850), (sx * 300, 850)],
            close=True, dxfattribs={"layer": "0"})                      # 扶手


def _build_tv_cabinet(blk) -> None:
    """電視櫃 1600×450(背貼牆):櫃體 + 前緣電視(薄矩形+中心點)。"""
    blk.add_lwpolyline([(-800, 0), (800, 0), (800, 450), (-800, 450)],
                       close=True, dxfattribs={"layer": "0"})
    blk.add_lwpolyline([(-550, 300), (550, 300), (550, 430), (-550, 430)],
                       close=True, dxfattribs={"layer": "0"})           # 電視
    blk.add_circle((0, 365), radius=25, dxfattribs={"layer": "0"})


def _build_nightstand(blk) -> None:
    """床頭櫃 450×400(背貼牆)+ 檯燈圓(臥室床邊)。"""
    blk.add_lwpolyline([(-225, 0), (225, 0), (225, 400), (-225, 400)],
                       close=True, dxfattribs={"layer": "0"})
    blk.add_circle((0, 200), radius=90, dxfattribs={"layer": "0"})      # 檯燈


def _build_fridge(blk) -> None:
    """冰箱 700×700(背貼牆):外框 + 對角線 + 前緣門縫線(平面慣用符號)。"""
    blk.add_lwpolyline([(-350, 0), (350, 0), (350, 700), (-350, 700)],
                       close=True, dxfattribs={"layer": "0"})
    blk.add_line((-350, 0), (350, 700), dxfattribs={"layer": "0"})      # 對角線記號
    blk.add_line((0, 560), (0, 700), dxfattribs={"layer": "0"})         # 門縫


def _build_bar_stool(blk) -> None:
    """吧檯椅 400×400(原點=中心):簡化圓形座位符號(中島吧台配件)。"""
    blk.add_circle((0, 0), radius=200, dxfattribs={"layer": "0"})
    blk.add_circle((0, 0), radius=90, dxfattribs={"layer": "0"})   # 椅面內圈


def _build_bookshelf(blk) -> None:
    """書櫃 1200×350(背貼牆):外框 + 兩道隔板(書房用)。"""
    blk.add_lwpolyline([(-600, 0), (600, 0), (600, 350), (-600, 350)],
                       close=True, dxfattribs={"layer": "0"})
    for x in (-200, 200):
        blk.add_line((x, 0), (x, 350), dxfattribs={"layer": "0"})


def _build_car(blk) -> None:
    """汽車 1800×4600(原點=車心):車身外框(切角)+ 車艙 + 前擋線。

    停車位圖示用:小客車常見尺寸,車位淨尺寸建議 ≥2.5×5.5m。
    """
    blk.add_lwpolyline(
        [(-900, -1900), (-500, -2300), (500, -2300), (900, -1900),
         (900, 1900), (500, 2300), (-500, 2300), (-900, 1900)],
        close=True, dxfattribs={"layer": "0"})                          # 車身
    blk.add_lwpolyline([(-750, -800), (750, -800), (750, 900), (-750, 900)],
                       close=True, dxfattribs={"layer": "0"})           # 車艙
    blk.add_line((-750, 900), (750, 900), dxfattribs={"layer": "0"})    # 前擋風


FIXTURE_BUILDERS = {
    "toilet": _build_toilet,
    "basin": _build_basin,
    "bathtub": _build_bathtub,
    "bed_single": lambda blk: _build_bed(blk, 1000, pillows=1),
    "bed_double": lambda blk: _build_bed(blk, 1600, pillows=2),
    "table4": _build_table4,
    "sofa3": _build_sofa3,
    "wardrobe": _build_wardrobe,
    "shoe_cabinet": _build_shoe_cabinet,
    "desk": _build_desk,
    "car": _build_car,
    "coffee_table": _build_coffee_table,
    "armchair": _build_armchair,
    "tv_cabinet": _build_tv_cabinet,
    "nightstand": _build_nightstand,
    "fridge": _build_fridge,
    "bookshelf": _build_bookshelf,
    "bar_stool": _build_bar_stool,
}

# 原點在圖塊中心(不靠牆)的家具:方桌、汽車、茶几、吧檯椅。其餘原點在貼牆邊
# 中點、朝 +Y。
_CENTER_ORIGIN = {"table4", "car", "coffee_table", "bar_stool"}

# 各圖塊的佔地外框(寬w × 深d,局部座標;與 builder 幾何一致)。
# table4 原點在中心(±780),其餘原點在貼牆邊中點、朝 +Y 伸出 d。
FIXTURE_SIZES = {
    "toilet": (380, 700),
    "basin": (500, 450),
    "bathtub": (1600, 750),
    "bed_single": (1000, 2000),
    "bed_double": (1600, 2000),
    "table4": (1560, 1560),      # 桌 800 + 兩側椅子(590+190)×2
    "sofa3": (2000, 850),
    "wardrobe": (1500, 600),
    "shoe_cabinet": (1200, 350),
    "desk": (1200, 700),         # 桌 550 + 椅子外緣(部分塞桌下)
    "car": (1800, 4600),         # 小客車;原點=車心(_CENTER_ORIGIN)
    "coffee_table": (1200, 600),  # 茶几;原點=中心(_CENTER_ORIGIN)
    "armchair": (900, 850),
    "tv_cabinet": (1600, 450),
    "nightstand": (450, 400),
    "fridge": (700, 700),
    "bookshelf": (1200, 350),
    "bar_stool": (400, 400),      # 吧檯椅;原點=中心(_CENTER_ORIGIN)
}

# 碰撞判定用的佔地尺寸(v0.6 Phase 2)——多數同 FIXTURE_SIZES;桌椅組(table4)
# 的「四面拉開椅子」不作為「穿牆」依據:椅子可推進桌下、靠牆那側本就不拉開,
# 故碰撞用「桌體 + 少量餘裕」(900×900),避免正常靠牆餐桌的椅子區被誤判穿牆。
# 這是 fixture 資料修正,與 wall collision 演算法分離(FIXTURE_SIZES 不動,畫圖與
# 家具×家具碰撞仍用完整 footprint)。
COLLISION_SIZES = {**FIXTURE_SIZES, "table4": (900, 900)}


def _block_name(name: str) -> str:
    return f"FX_{name.upper()}"


def create_fixture_block(doc, name: str) -> str:
    """建立(或取得)一種設備圖塊,回傳圖塊名。未知種類報錯。"""
    if name not in FIXTURE_BUILDERS:
        raise ValueError(
            f"未知設備 {name!r},支援:{sorted(FIXTURE_BUILDERS)}"
        )
    block_name = _block_name(name)
    if block_name not in doc.blocks:
        FIXTURE_BUILDERS[name](doc.blocks.new(block_name))
    return block_name


# ---------------------------------------------------------------------------
# 放置
# ---------------------------------------------------------------------------
@dataclass
class FixturePlacement:
    """一件設備/家具的放置:種類 + 插入點(貼牆邊中點)+ 旋轉(度,逆時針)。"""

    name: str
    insert: Point
    rotation: float = 0.0


def _footprint_points(name: str, insert: Point, rotation: float,
                      sizes: dict) -> list[Point]:
    """佔地矩形四角(世界座標,含旋轉);sizes 決定用畫圖尺寸或碰撞尺寸。"""
    w, d = sizes[name]
    if name in _CENTER_ORIGIN:                    # 原點=圖塊中心(方桌/汽車)
        local = [(-w / 2, -d / 2), (w / 2, -d / 2), (w / 2, d / 2), (-w / 2, d / 2)]
    else:                                          # 原點=貼牆邊中點,朝 +Y
        local = [(-w / 2, 0), (w / 2, 0), (w / 2, d), (-w / 2, d)]
    a = math.radians(rotation)
    ca, sa = math.cos(a), math.sin(a)
    ix, iy = insert
    return [(ix + x * ca - y * sa, iy + x * sa + y * ca) for x, y in local]


def fixture_footprint(placement: FixturePlacement) -> list[Point]:
    """設備的佔地矩形(世界座標四角點,含旋轉)——畫圖用完整尺寸 FIXTURE_SIZES,
    家具×家具碰撞與 validate 沿用此函式(行為不變)。"""
    return _footprint_points(placement.name, placement.insert,
                             placement.rotation, FIXTURE_SIZES)


def fixture_collision_footprint(placement: FixturePlacement) -> list[Point]:
    """碰撞判定用的佔地矩形——同 fixture_footprint,但用 COLLISION_SIZES
    (桌椅組收緊)。v0.6 Phase 2 的牆碰撞(穿牆判定)用這個。"""
    return _footprint_points(placement.name, placement.insert,
                             placement.rotation, COLLISION_SIZES)


def counter_footprint(counter: Counter) -> list[Point]:
    """流理台的佔地矩形(與 draw_counter 畫的檯面一致)——碰撞檢核用。"""
    (x1, y1), (x2, y2) = counter.start, counter.end
    length = counter.length
    ux, uy = (x2 - x1) / length, (y2 - y1) / length
    nx, ny = -uy, ux                               # 左手側
    d = counter.depth
    return [(x1, y1), (x2, y2), (x2 + nx * d, y2 + ny * d), (x1 + nx * d, y1 + ny * d)]


def place_fixture(msp, placement: FixturePlacement, layers: dict[str, str]):
    """插入一件設備圖塊(掛 OTHER 層),回傳 blockref。"""
    block_name = create_fixture_block(msp.doc, placement.name)
    return msp.add_blockref(
        block_name, placement.insert,
        dxfattribs={"layer": layers["OTHER"], "rotation": placement.rotation},
    )


# ---------------------------------------------------------------------------
# 流理台(參數式:長度隨廚房而變)
# ---------------------------------------------------------------------------
@dataclass
class Counter:
    """一段流理台:start→end 為靠牆邊,檯面往行進方向「左手側」伸出 depth。

    L 型 = 兩段 Counter 相接(共用轉角點)。sink=True 在段中點畫水槽圓;
    stove=True 在段 30% 處畫四口爐(2×2 圓圈,平面慣用符號)。
    """

    start: Point
    end: Point
    depth: float = 600
    sink: bool = False
    stove: bool = False

    @property
    def length(self) -> float:
        return math.hypot(self.end[0] - self.start[0], self.end[1] - self.start[1])

    def __post_init__(self) -> None:
        if self.length <= 0:
            raise ValueError("Counter 的起訖點不能相同")


def draw_counter(msp, counter: Counter, layers: dict[str, str]) -> None:
    """畫一段流理台:檯面矩形(+ 可選水槽圓),掛 OTHER 層。"""
    (x1, y1), (x2, y2) = counter.start, counter.end
    length = counter.length
    ux, uy = (x2 - x1) / length, (y2 - y1) / length
    nx, ny = -uy, ux                                   # 左手側
    d = counter.depth
    msp.add_lwpolyline(
        [(x1, y1), (x2, y2), (x2 + nx * d, y2 + ny * d), (x1 + nx * d, y1 + ny * d)],
        close=True, dxfattribs={"layer": layers["OTHER"]},
    )
    if counter.sink:
        mx, my = (x1 + x2) / 2 + nx * d / 2, (y1 + y2) / 2 + ny * d / 2
        msp.add_circle((mx, my), radius=180, dxfattribs={"layer": layers["OTHER"]})
    if counter.stove:
        # 四口爐:2×2 圓圈,中心在段長 30% 處(避開中點的水槽)。
        cx = x1 + ux * length * 0.3 + nx * d / 2
        cy = y1 + uy * length * 0.3 + ny * d / 2
        for da in (-190, 190):
            for db in (-140, 140):
                msp.add_circle((cx + ux * da + nx * db, cy + uy * da + ny * db),
                               radius=110, dxfattribs={"layer": layers["OTHER"]})


# =============================================================================
# PENDING(待確認假設彙整)
# =============================================================================
# 1. 設備尺寸(mm)皆為市面常見值,非任何規範:馬桶 380×700、洗手台 500×450、
#    浴缸 1600×750、單人床 1000×2000、雙人床 1600×2000、方桌 800×800+椅 380、
#    三人沙發 2000×850、衣櫃 1500×600、流理台深 600、水槽圓 r180。待確認。
# 2. 圖層:全掛 OTHER(其他,0.15mm 細線)——競賽圖層規定沒有家具/設備專層。
#    真實事務所常設 FURN/P-FIXT 等專層,要拆layer時在 default.yaml 加層+改此處。
# 3. 符號畫法(枕頭/被摺線/衣櫃斜線/沙發三件式)為常見簡化畫法,各事務所
#    圖例略異。馬桶/面盆用橢圓,床/沙發用直角矩形(未做圓角)。待確認。
# 4. 靠牆擺放由呼叫端負責算插入點(牆內面座標)與旋轉;本模組不自動吸附牆面。
#    之後 C1 自動設計時可加「貼某道牆的中點/偏移」的輔助函式。
# =============================================================================
