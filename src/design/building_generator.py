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
    路徑才能連續貫通到基礎,避免轉換樑/轉換柱。逐一相鄰樓層比對:每根上層
    柱都要在下層找到 tol 內的柱心當支承,否則列為問題。

    (D2 起各層格局可以不同——B1F 車庫/1F 公共層/2F+ 臥室層——靠「同一副
    骨架軸網」保證對齊,這裡驗證。)
    """
    problems: list[str] = []
    floors = building.floors
    for lower, upper in zip(floors[:-1], floors[1:]):
        below = _column_centers(lower.spec)
        for cx, cy in _column_centers(upper.spec):
            nearest = min(
                (((cx - bx) ** 2 + (cy - by) ** 2) ** 0.5 for bx, by in below),
                default=float("inf"),
            )
            if nearest > tol:
                problems.append(
                    f"{upper.label} 柱 ({cx/1000:.2f},{cy/1000:.2f})m "
                    f"下方 {lower.label} 無柱支承(最近 {nearest/1000:.2f}m)"
                    f"——需轉換樑,違反柱位上下對齊")
    return problems


# ---------------------------------------------------------------------------
# 產生器:標準層 → 疊成一整棟
# ---------------------------------------------------------------------------
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
