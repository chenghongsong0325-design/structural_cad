"""多樓層骨架 —— 把「單層 FloorPlanSpec」升成「一整棟樓 BuildingSpec」。

這是「多樓層」方向的第一步(M1,見 ROADMAP 多樓層段)。之前的產生器
(layout_generator)一次只做一層;真實建案是「標準層重複疊高幾十層,柱位
上下對齊、垂直核(樓梯/電梯/管道間)貫通到基礎」。本模組:

    BuildingBrief(標準層設計 + 樓層數 + 層高)
        → generate_building → BuildingSpec(逐層 FloorPlanSpec + 標高)

核心保證(使用者 2026-07-12 定調的柱網原則,見 column_grid_principles 記憶):
    * 上下樓層「共用同一套軸網」→ 柱位天生垂直對齊、可連續貫通到基礎。
    * check_column_alignment 把這條原則變成「可驗證的檢核」:逐層比對,
      任何上層柱在下層找不到支承(轉換柱)都會被抓出來。目前用複製產生
      故必然對齊;等 M2 讓 1F 變化層(門廳)時,這道檢核才真正防守。

出圖:一層一張 DXF(每層自帶 A3 圖框+標題欄+樓層大字),比照真實建案
「一層一張圖」的施工圖慣例。

尚未做(後續切片):
    * M2 地面層變化(1F 門廳/騎樓,不同格局但柱仍對齊)。
    * M3 剖面/立面(layer_height 已存在 BuildingSpec 裡,鋪路給剖面)。
    * 屋突/機房層、地下室、退縮樓層。

典型用法::

    from src.design.building_generator import BuildingBrief, generate_building
    from src.design.layout_generator import CorridorBrief

    b = generate_building(BuildingBrief(
        typical=CorridorBrief(units_per_row=6), floors=5))
    for fl in b.floors:
        print(fl.label, fl.elevation)          # 1F 0 / 2F 3200 / ...
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
    generate_floor_plan,
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
    """

    typical: Brief
    floors: int = 5
    floor_height: float = FLOOR_HEIGHT
    start_level: int = 1


@dataclass
class FloorLevel:
    """一層樓在整棟裡的定位:樓層號 + 樓板標高 + 該層平面。"""

    level: int
    elevation: float          # 樓板面標高(mm,1F 樓板 = 0)
    spec: FloorPlanSpec

    @property
    def label(self) -> str:
        return f"{self.level}F"


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

    (目前各層由複製產生必然對齊;M2 讓 1F 變化後,這道檢核才會實際攔截。)
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

    標準層只設計一次,再深拷貝成每一層(各層是獨立物件,樓層標示各異但
    軸網/柱位相同)。深拷貝是刻意的:各層互不共用可變物件,日後某層要改
    (如 1F 變門廳)不會牽動其他層。
    """
    if brief.floors < 1:
        raise ValueError(f"樓層數需 ≥1,收到 {brief.floors}")

    base = generate_floor_plan(brief.typical)      # 標準層(已通過 validate_spec)

    floors: list[FloorLevel] = []
    for i in range(brief.floors):
        level = brief.start_level + i
        spec = copy.deepcopy(base)
        spec.floor_label = f"{level}F"
        floors.append(FloorLevel(level=level,
                                 elevation=i * brief.floor_height,
                                 spec=spec))

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
    # 集合住宅塔樓:每排 6 戶標準層,疊 5 層。
    ("bldg_corridor", BuildingBrief(typical=CorridorBrief(units_per_row=6),
                                    floors=5)),
    # 透天單戶疊 3 層(先示範骨架;各層同格局,M2 再讓 1F 公共層/樓上臥室層分化)。
    ("bldg_house", BuildingBrief(typical=HouseBrief(site_width=18000,
                                                    site_depth=14000,
                                                    bedrooms=3),
                                 floors=3)),
]


def main() -> None:
    from src.drafting.apartment_plan import draw_floor_plan
    from src.standards.loader import apply_standard, load_standard, new_document

    out_dir = _PROJECT_ROOT / "output" / "building"
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, brief in DEMO_BUILDINGS:
        building = generate_building(brief)
        for fl in building.floors:
            std = load_standard()
            doc = new_document()
            layers = apply_standard(doc, std)
            draw_floor_plan(doc.modelspace(), fl.spec, layers)
            doc.saveas(out_dir / f"{name}_{fl.label}.dxf")
        print(f"[OK] {name}: {len(building.floors)} 層 "
              f"(標高 0~{building.floors[-1].elevation/1000:.1f}m,"
              f"全高 {building.total_height/1000:.1f}m),柱網上下對齊")


if __name__ == "__main__":
    main()
