"""多樓層骨架 —— 把「單層 FloorPlanSpec」升成「一整棟樓 BuildingSpec」。

這是「多樓層」方向的第一步(M1,見 ROADMAP 多樓層段)。之前的產生器
(layout_generator)一次只做一層;真實建案是「標準層重複疊高幾十層,柱位
上下對齊、垂直核(樓梯/電梯/管道間)貫通到基礎」。本模組:

    BuildingBrief(標準層設計 + 樓層數 + 層高)
        → generate_building → BuildingSpec(逐層 FloorPlanSpec + 標高)

核心保證(使用者 2026-07-12 定調的柱網原則,見 column_grid_principles 記憶):
    * 上下樓層「共用同一套軸網」→ 柱位天生垂直對齊、可連續貫通到基礎。
    * check_column_alignment 把這條原則變成「可驗證的檢核」:逐層比對,
      任何上層柱在下層找不到支承(轉換柱)都會被抓出來。

D2 層別分化(依使用者要求:不做門廳/騎樓,做透天層別+地下室):
    * differentiated=True(僅透天 HouseBrief):B1F 車庫層 / 1F 公共層
      (客廳+餐廳+廚房)/ 2F+ 臥室層——樓梯間與濕區(衛浴/機房)每層同位,
      梯與管道上下貫通(layout_generator 的 _house_frame 骨架)。
    * basements=N:地下層。集合住宅 = 機車停車場+車道坡道+機房/蓄水池
      (逃生核直落);透天 = 車庫+儲藏+機房(需 differentiated,共用骨架)。
    各層格局不同但同軸網 → 柱位對齊由 check_column_alignment 真正把關。

出圖:一層一張 DXF(每層自帶 A3 圖框+標題欄+樓層大字),比照真實建案
「一層一張圖」的施工圖慣例。

尚未做(後續切片):
    * M3 剖面/立面(floor_height/elevation 已存在 BuildingSpec 裡,鋪路給剖面)。
    * 屋突/機房層、退縮樓層;地下層層高獨立設定(現與地上同 floor_height)。

典型用法::

    from src.design.building_generator import BuildingBrief, generate_building
    from src.design.layout_generator import CorridorBrief

    b = generate_building(BuildingBrief(
        typical=CorridorBrief(units_per_row=6), floors=5, basements=1))
    for fl in b.floors:
        print(fl.label, fl.elevation)          # B1F -3200 / 1F 0 / 2F 3200 / ...
"""
from __future__ import annotations

import copy
import sys
from dataclasses import dataclass, field
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from src.drafting.apartment_plan import (
    FloorPlanSpec,
    build_grid,
    resolve_columns,
)
from src.design.column_design import apply_column_design
from src.design.layout_generator import (
    Brief,
    CorridorBrief,
    HouseBrief,
    generate_corridor_basement,
    generate_floor_plan,
    generate_house_basement,
    generate_house_public,
    generate_house_upper,
)

Point = tuple[float, float]

FLOOR_HEIGHT = 3200          # 層高(樓板面到樓板面,mm)——住宅常見 3.0~3.4m
ALIGN_TOL = 50               # 上下柱位對齊容差(mm)——同套軸網下應為 0,留餘裕


# ---------------------------------------------------------------------------
# 需求 / 結果 資料模型
# ---------------------------------------------------------------------------
@dataclass
class BuildingBrief:
    """一整棟樓的需求:標準層設計 + 樓層數 + 層高。

    typical:標準層的設計需求(HouseBrief 透天單戶 / CorridorBrief 集合住宅)。
             會產生一次,再複製成每一層(2F…NF),故各層格局相同、軸網相同
             → 柱上下對齊。
    floors:地上樓層數(含 1F)。
    floor_height:層高(mm),供標高計算與日後剖面/立面用;平面圖不受影響。
    start_level:起始樓層編號(預設 1 = 1F)。
    basements:地下層數(B1F…BnF)。集合住宅 = 機車停車場層;
              透天 = 車庫層(需 differentiated=True,各層才共用同一骨架軸網)。
    differentiated:透天層別分化(僅 HouseBrief)——1F 公共層(客廳/餐廳/
              廚房)、2F+ 臥室層,樓梯間/濕區每層同位。False = 各層同標準層
              (M1 行為)。
    """

    typical: Brief
    floors: int = 5
    floor_height: float = FLOOR_HEIGHT
    start_level: int = 1
    basements: int = 0
    differentiated: bool = False


@dataclass
class FloorLevel:
    """一層樓在整棟裡的定位:樓層號 + 樓板標高 + 該層平面。

    level:地上 1,2,3…;地下 -1,-2(顯示為 B1F,B2F,台灣慣例無 0 樓)。
    """

    level: int
    elevation: float          # 樓板面標高(mm,1F 樓板 = 0;地下為負)
    spec: FloorPlanSpec

    @property
    def label(self) -> str:
        return f"B{-self.level}F" if self.level < 0 else f"{self.level}F"


@dataclass
class BuildingSpec:
    """一整棟樓 = 由下而上的樓層清單 + 層高。"""

    floors: list[FloorLevel] = field(default_factory=list)
    floor_height: float = FLOOR_HEIGHT

    @property
    def levels(self) -> list[int]:
        return [f.level for f in self.floors]

    @property
    def total_height(self) -> float:
        """全棟高度(mm)= 樓層數 × 層高。"""
        return len(self.floors) * self.floor_height


# ---------------------------------------------------------------------------
# 柱網對齊檢核(結構原則:柱位上下對齊、連續貫通到基礎)
# ---------------------------------------------------------------------------
def _column_centers(spec: FloorPlanSpec) -> list[Point]:
    """一層樓的所有柱心世界座標(沿用 apartment_plan 的軸網/柱解算)。"""
    grid = build_grid(spec)
    return [c.center for c in resolve_columns(spec, grid)]


def check_column_alignment(building: BuildingSpec,
                           tol: float = ALIGN_TOL) -> list[str]:
    """逐層檢核柱位是否上下對齊,回傳問題清單(空 = 全對齊)。

    結構原則(見 column_grid_principles):上層柱必須落在下層柱正上方,力
    路徑才能連續貫通到基礎,避免轉換樑/轉換柱。

    判準是「**上層柱的斷面完全落在下層柱的斷面裡**」,不是「柱心距離 ≤ tol」。
    為什麼改:柱斷面概算(column_design)會讓上層的柱比下層細,而真實建築的
    柱往上縮時常常是**對齊某一面**(貼分間牆那一面)、不是同心縮 —— 柱心因此
    差了幾公分,但上層柱整根坐在下層柱頭上,力路徑完全連續,這是合格的做法。
    用柱心距離判會把這種正確的做法誤判成錯誤。

    兩層柱同尺寸時,此判準退化成原本的「柱心距離 ≤ tol」,行為不變。

    (D2 起各層格局可以不同——B1F 車庫/1F 公共層/2F+ 臥室層——靠「同一副
    骨架軸網」保證對齊,這裡驗證。)
    """
    problems: list[str] = []
    floors = building.floors
    for lower, upper in zip(floors[:-1], floors[1:]):
        ls = float(lower.spec.column_size)
        us = float(upper.spec.column_size)
        slack = (ls - us) / 2.0 + tol      # 上層柱心可以偏離下層柱心多遠
        below = _column_centers(lower.spec)
        for cx, cy in _column_centers(upper.spec):
            if any(abs(cx - bx) <= slack and abs(cy - by) <= slack
                   for bx, by in below):
                continue
            nearest = min(
                (max(abs(cx - bx), abs(cy - by)) for bx, by in below),
                default=float("inf"),
            )
            problems.append(
                f"{upper.label} 柱 ({cx/1000:.2f},{cy/1000:.2f})m "
                f"斷面 {us:.0f} 未完全落在下方 {lower.label} 柱 {ls:.0f} 內"
                f"(最近偏移 {nearest:.0f}mm,可容許 {slack:.0f}mm)"
                f"——需轉換樑,違反柱位上下對齊")
    return problems


# ---------------------------------------------------------------------------
# 產生器:標準層 → 疊成一整棟
# ---------------------------------------------------------------------------
def _narrow_to_building(named_floors, floor_height: float) -> BuildingSpec:
    """窄透天各層 [(標示, spec)] → BuildingSpec(1F 樓板標高 0,往上每層 +層高)。

    ⚠️ AI 關係圖版(graph_layout)也走這條路,不經過 generate_building ——
    柱斷面概算要在這裡也套一次,四條產線才一致。窄透天/淺透天沒有柱,
    apply_column_design 會自動跳過。
    """
    levels = [FloorLevel(level=i, elevation=(i - 1) * floor_height, spec=spec)
              for i, (_, spec) in enumerate(named_floors, 1)]
    building = BuildingSpec(floors=levels, floor_height=floor_height)
    apply_column_design(building)
    return building


def _townhouse_lot(house, min_w: float, max_w: float):
    """這個需求是不是「連棟街屋基地」?是的話回 `zoning.TownhouseLot`,否則 None。

    判準是**面寬**:4~8m 的基地退完側院就不成立(5m 退掉左右各 2m 只剩 1m),
    在台灣只可能是與鄰戶共壁的連棟街屋。再寬的基地兩種都可能,維持原本的獨棟
    規則(四面退縮),免得既有尺寸的行為整批改變。

    ⚠️ 只有使用者講的是**基地**尺寸(`dimension_basis == "site"`)才適用。
       講「建築物 7×12」的人已經自己扣好了,再套建蔽率會扣兩次。
    """
    if getattr(house, "dimension_basis", "site") != "site":
        return None
    if not min_w <= house.site_width <= max_w:
        return None
    from src.design.zoning import townhouse_envelope

    return townhouse_envelope(house.site_width, house.site_depth,
                              zone=house.zone, coverage=house.coverage)


def generate_building_auto(brief: BuildingBrief) -> BuildingSpec:
    """依建築面寬自動選骨架:**窄面寬單戶透天**(建築寬 4~8m)走 narrow_house
    的前後串聯+中段核+單樓梯骨架;其餘走既有兩帶式 generate_building。

    ⚠️ 只有單戶透天(HouseBrief)才有窄面寬版;窄透天暫不含地下室(basements 忽略)。
    這是「建築物 7×12」這類窄基地能生得出來的入口。

    ⚠️ **基地窄到只可能是連棟街屋時,用的是另一套基地→建築規則**(見下方
       `_townhouse_lot` 與 `design/zoning.py`):街屋左右與鄰戶共壁、**側邊不
       退縮**,建築進深由**建蔽率**決定。四面各退 2m 是**獨棟**的規則,拿它去
       算 5m 寬的街屋基地會得到「建築 1m 寬」然後直接 raise —— 那正是使用者
       2026-08-25 拿真實透天基地(5×20m)進來時撞到的。
    """
    if isinstance(brief.typical, HouseBrief):
        setback = brief.typical.setback
        bw = brief.typical.site_width - 2 * setback
        bd = brief.typical.site_depth - 2 * setback
        from src.design.layout.narrow_house import (
            MAX_WIDTH,
            MIN_DEPTH,
            MIN_WIDTH,
            generate_narrow_building,
        )
        lot = _townhouse_lot(brief.typical, MIN_WIDTH, MAX_WIDTH)
        # 窄透天的 `car_spaces` 是**1F 車庫**(前段整段停車、捲門臨路),不是
        # 兩帶式那種地下車庫 —— 4~8m 面寬的透天挖地下室不合成本,真實街屋一律
        # 把車停在一樓前段。
        want_garage = brief.typical.car_spaces > 0
        if lot is not None:
            floors = generate_narrow_building(
                lot.building_w, brief.typical.site_depth,
                floors=max(1, brief.floors),
                bedrooms=brief.typical.bedrooms, seed=brief.typical.seed,
                lot=lot, patio=brief.typical.patio, garage=want_garage,
                core_style=brief.typical.core_style)
            return _narrow_to_building(floors, brief.floor_height)
        from src.design.layout.narrow_house import min_depth_for
        from src.design.layout.shallow_house import (
            MAX_WIDTH as SH_MAX_W,
            MIN_DEPTH as SH_MIN_D,
            MIN_WIDTH as SH_MIN_W,
            generate_shallow_building,
        )
        # 淺基地(進深放不下南北向折返梯)→ 樓梯轉 90 度的另一套骨架。
        # 5×5 米這種小基地只有它生得出來;深一點的仍走正常透天骨架。
        if (SH_MIN_W <= bw <= SH_MAX_W and SH_MIN_D <= bd < min_depth_for(bw)):
            return _narrow_to_building(
                generate_shallow_building(bw, bd, floors=max(1, brief.floors)),
                brief.floor_height)
        if MIN_WIDTH <= bw <= MAX_WIDTH and bd >= MIN_DEPTH:
            # seed → 設計變體(核在左/右、浴廁在南/北、後段配置、大門位置):
            # 同一個需求換個 seed 就是**另一種格局**,不會每次都長一樣。
            floors = generate_narrow_building(
                bw, bd, floors=max(1, brief.floors),
                bedrooms=brief.typical.bedrooms, seed=brief.typical.seed,
                patio=brief.typical.patio, garage=want_garage,
                core_style=brief.typical.core_style)
            return _narrow_to_building(floors, brief.floor_height)
    return generate_building(brief)


def generate_building(brief: BuildingBrief) -> BuildingSpec:
    """需求 → BuildingSpec(已通過柱網對齊檢核)。

    重複的樓層只設計一次,再深拷貝(各層是獨立物件,樓層標示各異但軸網/
    柱位相同);differentiated/basements 的變化層各自產生(內建 validate),
    因共用同一副骨架軸網,柱位對齊由 check_column_alignment 真正把關。
    頂層樓梯標示改「下」(往上沒有樓層了)。
    """
    if brief.floors < 1:
        raise ValueError(f"樓層數需 ≥1,收到 {brief.floors}")
    if brief.basements < 0:
        raise ValueError(f"地下層數需 ≥0,收到 {brief.basements}")
    is_house = isinstance(brief.typical, HouseBrief)
    if brief.differentiated and not is_house:
        raise ValueError("differentiated(層別分化)僅支援透天 HouseBrief")
    if brief.basements and is_house and not brief.differentiated:
        raise ValueError(
            "透天要配地下室請開 differentiated=True(各層需共用同一套骨架軸網,"
            "否則地下室柱位對不上標準層)")

    floors: list[FloorLevel] = []

    # ── 地下層(由深到淺:BnF → B1F;各層相同,產一次再拷貝)──────────
    if brief.basements:
        base_b = (generate_house_basement(brief.typical) if is_house
                  else generate_corridor_basement(brief.typical))
        for k in range(brief.basements, 0, -1):
            spec = copy.deepcopy(base_b)
            spec.floor_label = f"B{k}F"
            floors.append(FloorLevel(level=-k,
                                     elevation=-k * brief.floor_height,
                                     spec=spec))

    # ── 地上層 ────────────────────────────────────────────────────────
    upper_base: FloorPlanSpec | None = None
    for i in range(brief.floors):
        level = brief.start_level + i
        if brief.differentiated and i == 0:
            spec = generate_house_public(brief.typical)      # 1F 公共層
        else:
            if upper_base is None:
                upper_base = (generate_house_upper(brief.typical)
                              if brief.differentiated
                              else generate_floor_plan(brief.typical))
            spec = copy.deepcopy(upper_base)
        spec.floor_label = f"{level}F"
        floors.append(FloorLevel(level=level,
                                 elevation=i * brief.floor_height,
                                 spec=spec))

    # 頂層樓梯只能往下(有下層才有梯;中間層僅標「上」是簡化,PENDING)。
    if len(floors) > 1:
        for st in floors[-1].spec.stairs:
            st.label = "下"

    building = BuildingSpec(floors=floors, floor_height=brief.floor_height)

    # 柱斷面概算:各層依「上方壓了幾層」給不同斷面(一樓比頂樓粗),取代原本
    # 全棟寫死 500。⚠️ 概算不是結構計算,細節與免責見 column_design 模組說明。
    # 只縮不放 → 不可能新撞到門窗家具,所以放在格局定案之後、對齊檢核之前。
    apply_column_design(building)

    problems = check_column_alignment(building)
    if problems:
        raise ValueError("產生的樓棟柱網未上下對齊:\n  - "
                         + "\n  - ".join(problems))
    return building


# ---------------------------------------------------------------------------
# 示範:兩種樓棟 → 逐層出圖
# ---------------------------------------------------------------------------
DEMO_BUILDINGS: list[tuple[str, BuildingBrief]] = [
    # 集合住宅塔樓:B1F 機車停車場 + 每排 6 戶標準層 ×5。
    ("bldg_corridor", BuildingBrief(typical=CorridorBrief(units_per_row=6),
                                    floors=5, basements=1)),
    # 透天層別分化:B1F 車庫 / 1F 公共層(客餐廚)/ 2F・3F 臥室層。
    ("bldg_house", BuildingBrief(typical=HouseBrief(site_width=19000,
                                                    site_depth=13000,
                                                    bedrooms=3),
                                 floors=3, basements=1, differentiated=True)),
]


def main() -> None:
    from src.drafting.apartment_plan import draw_floor_plan
    from src.drafting.section import draw_elevation, draw_section
    from src.standards.loader import apply_standard, load_standard, new_document

    out_dir = _PROJECT_ROOT / "output" / "building"
    out_dir.mkdir(parents=True, exist_ok=True)

    def _new():
        doc = new_document()
        return doc, apply_standard(doc, load_standard())

    for name, brief in DEMO_BUILDINGS:
        building = generate_building(brief)
        for fl in building.floors:
            doc, layers = _new()
            draw_floor_plan(doc.modelspace(), fl.spec, layers)
            doc.saveas(out_dir / f"{name}_{fl.label}.dxf")

        # D3:剖面圖(沿長向 X 剖,含地下層)+ 南向立面圖(僅地上層)。
        doc, layers = _new()
        draw_section(doc.modelspace(), building, layers, axis="x",
                     title=f"{name} 剖面圖 A-A")
        doc.saveas(out_dir / f"{name}_section.dxf")

        doc, layers = _new()
        draw_elevation(doc.modelspace(), building, layers, side="south",
                       title=f"{name} 南向立面圖")
        doc.saveas(out_dir / f"{name}_elevation.dxf")

        print(f"[OK] {name}: {building.floors[0].label}~{building.floors[-1].label} "
              f"共 {len(building.floors)} 層(標高 "
              f"{building.floors[0].elevation/1000:.1f}~"
              f"{building.floors[-1].elevation/1000:.1f}m)"
              f"+剖面+立面,柱網上下對齊")


if __name__ == "__main__":
    main()
