"""使用分區與建蔽率 → 這塊地能蓋多大(**連棟街屋**的基地→建築推導)。

以前專案只在 `metrics.py` **事後回報**建蔽率,沒有任何地方**用它決定要蓋多深** ——
使用者給真實的透天基地(5×20m)時,程式拿「四面各退 2m」去算,得到「建築 1×16m」
然後直接 raise。四面退縮是**獨棟**的規則,連棟街屋不是那樣蓋的。

連棟街屋(透天厝)的三個特徵(使用者 2026-08-25 指著平面圖歸納,圖上就看得出來):

  1. 左右兩道牆是**共同壁** —— 貫穿到底、整段沒有開口(有的話至少一側會畫窗)
  2. 對外開口只在**前後兩端**(前面捲門/大門、後面後門),中間完全封閉
  3. 中段常開**天井** —— 側面沒窗,中間才需要另外找採光

所以基地→建築的推導很簡單:

    建築面寬 = 基地面寬            ← 共壁,**側邊不退縮**
    建築面積 ≤ 基地面積 × 建蔽率
    ⇒ 建築進深 = 基地進深 × 建蔽率  ← 面寬約掉了
    剩下的進深 = 前院 + 後院(法定空地,不計建築面積)

⚠️ **建蔽率一定要當參數,不能寫死。** 使用者拿一張舊市區街屋的平面圖來算,建蔽率
   約 93% —— 那在都市計畫住宅區(60%)不可能過,只可能是商業區(80% 上下)或
   法規收緊前就蓋好的既存老屋。同一塊地在不同分區能蓋的深度差很多:

       基地 4.5×15m  住宅區 60% → 建築 9.0m 深(低於骨架下限,生不出來)
                     商業區 80% → 建築 12.0m 深 ✅

⚠️ 表裡的數字是**常見值,不是法源**:建蔽率由各縣市的都市計畫書逐區規定,同樣叫
   住宅區也可能是 50% 或 60%。要精確就直接給 `coverage`,別依賴這張表。
⚠️ 本模組只做**量體推導**,不是法規檢討:未計入騎樓、法定空地的最小尺寸規定
   (§建築技術規則對前院/後院深度另有下限)、角地放寬、免計建築面積的項目。
"""
from __future__ import annotations

import json
from dataclasses import dataclass

# 都市計畫使用分區 → 常見法定建蔽率上限。⚠️ 常見值,不是法源(見模組說明)。
COVERAGE_BY_ZONE = {
    "住宅區": 0.60,
    "商業區": 0.80,
    "工業區": 0.70,
}
DEFAULT_ZONE = "住宅區"


def coverage_for(zone: str | None = None,
                 coverage: float | None = None) -> float:
    """要用的建蔽率:直接給 `coverage` 最優先,否則查分區表。

    ⚠️ 給了 `coverage` 就以它為準 —— 使用者手上有都市計畫書的實際數字時,
       那份數字永遠比這張常見值表可信。"""
    if coverage is not None:
        if not 0.0 < coverage <= 1.0:
            raise ValueError(f"建蔽率要在 0~1 之間,收到 {coverage}")
        return float(coverage)
    z = zone or DEFAULT_ZONE
    if z not in COVERAGE_BY_ZONE:
        raise ValueError(f"沒有「{z}」這個分區,已知:{sorted(COVERAGE_BY_ZONE)}")
    return COVERAGE_BY_ZONE[z]


@dataclass
class TownhouseLot:
    """一塊連棟街屋基地能蓋出來的量體(長度單位 mm)。"""

    site_w: float
    site_d: float
    zone: str
    coverage: float                 # 採用的建蔽率(0~1)
    building_w: float               # = site_w(共壁,側邊不退縮)
    building_d: float
    front_yard: float
    rear_yard: float

    @property
    def site_area_m2(self) -> float:
        return self.site_w * self.site_d / 1.0e6

    @property
    def building_area_m2(self) -> float:
        return self.building_w * self.building_d / 1.0e6

    @property
    def coverage_used(self) -> float:
        """實際用掉的建蔽率(可能比上限小 —— 骨架有自己的進深上限)。"""
        return self.building_area_m2 / self.site_area_m2 if self.site_area_m2 else 0.0

    def summary(self) -> str:
        return (f"基地 {self.site_w/1000:.2f}×{self.site_d/1000:.2f}m"
                f"({self.site_area_m2:.1f}㎡)· {self.zone} 建蔽上限 "
                f"{self.coverage:.0%}\n"
                f"  → 建築 {self.building_w/1000:.2f}×{self.building_d/1000:.2f}m"
                f"({self.building_area_m2:.1f}㎡),實際建蔽 "
                f"{self.coverage_used:.0%}\n"
                f"  → 前院 {self.front_yard/1000:.2f}m、"
                f"後院 {self.rear_yard/1000:.2f}m(法定空地,不計建築面積)")

    def to_dict(self) -> dict:
        return {
            "site_w": round(self.site_w, 1),
            "site_d": round(self.site_d, 1),
            "zone": self.zone,
            "coverage_limit": round(self.coverage, 3),
            "coverage_used": round(self.coverage_used, 3),
            "building_w": round(self.building_w, 1),
            "building_d": round(self.building_d, 1),
            "front_yard": round(self.front_yard, 1),
            "rear_yard": round(self.rear_yard, 1),
            "site_area_m2": round(self.site_area_m2, 1),
            "building_area_m2": round(self.building_area_m2, 1),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)


def townhouse_envelope(site_w: float, site_d: float, *,
                       zone: str | None = None,
                       coverage: float | None = None,
                       building_d: float | None = None) -> TownhouseLot:
    """連棟街屋:基地尺寸 → 能蓋的建築量體 + 前後院(單位 mm)。

    building_d:骨架**實際**蓋出來的進深。骨架有自己的上限(採光、樓梯間),
    常常比建蔽率允許的還淺 —— 那就以實際的為準,剩下的都是院子。不給的話
    先用建蔽率算出來的上限當估計值。

    ⚠️ 院子**前後均分**(=建築置中),與 `narrow_house._build_floor` 的做法一致。
       真實案子的前後院深度常不同(前面留騎樓、後面留法定空地),而且技術規則對
       後院深度另有下限 —— 那些還沒做,見模組說明。
    """
    if site_w <= 0 or site_d <= 0:
        raise ValueError(f"基地尺寸要是正數,收到 {site_w}×{site_d}")
    cov = coverage_for(zone, coverage)
    bw = site_w                                     # 共壁:面寬整塊用滿
    bd = site_d * cov if building_d is None else min(building_d, site_d)
    yard = max(0.0, site_d - bd) / 2.0
    return TownhouseLot(site_w=site_w, site_d=site_d,
                        zone=zone or DEFAULT_ZONE, coverage=cov,
                        building_w=bw, building_d=bd,
                        front_yard=yard, rear_yard=yard)


def max_building_depth(site_d: float, *, zone: str | None = None,
                       coverage: float | None = None) -> float:
    """這塊地的建築進深上限(mm)= 基地進深 × 建蔽率。

    連棟街屋的面寬整塊用滿,所以建蔽率限制的**只有進深** —— 面寬在
    「建築面積 ÷ 基地面積」裡約掉了。"""
    return site_d * coverage_for(zone, coverage)
