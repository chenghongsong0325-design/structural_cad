"""關鍵數字(E4)—— 建蔽率 / 容積率 / 總樓地板面積 / 粗估造價。

老闆看圖三秒鐘、看數字三分鐘:一張平面圖漂不漂亮是其次,業主真正要的
是「這塊地用了幾成(建蔽率)、總共蓋了多少(容積率/樓地板)、大概要花
多少錢(粗估造價)」。本模組從 BuildingSpec 算出這些數字,給網頁顯示。

名詞解釋(給非本行的人):
  * 建蔽率 = 建築物「影子」佔基地的比例——從天上往下看,房子蓋住基地
    幾成。台灣住宅區法定上限常見 50~60%。
  * 容積率 = 地上各層樓地板面積加總 ÷ 基地面積——「這塊地總共疊了幾層
    地板」。住宅區常見上限 120~300%。
  * 粗估造價 = 總樓地板坪數 × 每坪營造單價。只是量體級的粗估(結構、
    裝修等級都會大幅影響),給業主一個數量級概念。

⚠️ 這些是「量體粗估」不是法規檢討:天井/中庭不計入建築面積、地下室
不計入容積(台灣停車空間多可免計),陽台/梯間免計等細節一律從簡。
待確認假設見模組結尾 PENDING。
"""
from __future__ import annotations

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from src.design.building_generator import BuildingSpec

M2_PER_PING = 3.305785          # 1 坪 = 400/121 m²

# 粗估營造單價(新台幣/坪)。2026 年行情 RC 住宅約 15~22 萬/坪,取中間偏保守
# 的 18 萬;地下室要開挖、擋土、防水,單價以地上層的 1.6 倍粗估。預設值,待確認。
COST_PER_PING = 180_000
BASEMENT_COST_FACTOR = 1.6


def _polygon_area_m2(points: list[tuple[float, float]]) -> float:
    """鞋帶公式(座標 mm → 面積 m²)。"""
    n = len(points)
    s = 0.0
    for i in range(n):
        x1, y1 = points[i]
        x2, y2 = points[(i + 1) % n]
        s += x1 * y2 - x2 * y1
    return abs(s) / 2 / 1_000_000


def _floor_area_m2(spec) -> float:
    """一層樓的樓地板面積 = 建築外殼(軸網範圍)- 該層天井/中庭開口。"""
    shell = sum(spec.x_spacings) * sum(spec.y_spacings) / 1_000_000
    patio = sum(_polygon_area_m2(r.points)
                for r in spec.rooms if r.kind == "patio")
    return shell - patio


def building_metrics(building: BuildingSpec) -> dict:
    """整棟樓 → 關鍵數字 dict(數值都取到小數一位,百分比取整數)。

    回傳鍵:
      site_area_m2     基地面積
      footprint_m2     建築面積(1F 投影,扣天井)
      coverage_pct     建蔽率 %
      floors_area_m2   地上總樓地板面積(各層加總,扣天井)
      basement_m2      地下樓地板面積
      far_pct          容積率 %(地上樓地板 ÷ 基地;地下不計)
      total_ping       總樓地板坪數(含地下,造價用)
      est_cost_wan     粗估造價(萬元;地下層單價 ×1.6)
    """
    above = [f for f in building.floors if f.level > 0]
    below = [f for f in building.floors if f.level < 0]
    if not above:
        raise ValueError("整棟樓沒有地上層,算不出建蔽/容積")

    ground = min(above, key=lambda f: f.level)            # 1F(最低地上層)
    site_area = _polygon_area_m2(ground.spec.site_boundary)
    footprint = _floor_area_m2(ground.spec)
    floors_area = sum(_floor_area_m2(f.spec) for f in above)
    basement = sum(_floor_area_m2(f.spec) for f in below)

    above_ping = floors_area / M2_PER_PING
    below_ping = basement / M2_PER_PING
    cost = above_ping * COST_PER_PING + below_ping * COST_PER_PING * BASEMENT_COST_FACTOR

    return {
        "site_area_m2": round(site_area, 1),
        "footprint_m2": round(footprint, 1),
        "coverage_pct": round(footprint / site_area * 100),
        "floors_area_m2": round(floors_area, 1),
        "basement_m2": round(basement, 1),
        "far_pct": round(floors_area / site_area * 100),
        "total_ping": round(above_ping + below_ping, 1),
        "est_cost_wan": round(cost / 10_000),
    }


# =============================================================================
# PENDING(待確認假設彙整)
# =============================================================================
# 1. 樓地板面積用「軸網外殼矩形 - 天井」粗算,非逐室淨面積、也未扣牆體;
#    與法規的「建築面積/樓地板面積」定義(以外牆中心線計、陽台雨遮免計等)
#    有出入。量體級粗估,待確認。
# 2. 容積率:地下室全額不計(台灣停車空間多可免計容積,但儲藏/機房其實要計);
#    梯間/陽台免計等細節未做。待確認。
# 3. 造價:每坪 18 萬、地下 ×1.6 為 2026 年 RC 住宅粗估行情,未分結構形式/
#    裝修等級/區域,COST_PER_PING 常數可調。待確認。
# =============================================================================
