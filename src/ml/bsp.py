"""ML 對照實驗(F2):BSP 切割表示法 —— 換一種「不可能畫錯」的輸出方式。

F1(train.py)的 RoomPainter 自由畫 11 個矩形,指望它們自己對齊——結果
是漏地板、互相壓、外插時畫出建築外。F2 換表示法,借 HypergraphFormer
(arXiv 2605.18932)的核心洞察:讓模型改預測「在哪裡下刀」(切割線位置),
房間由切割結果組出來——只要是切出來的,天生就互不重疊、剛好鋪滿
(by construction),不必靠模型自律。

切割模板(照單層兩帶式的骨架形狀,像一份「填空的裁切圖」):

    建築矩形
    ├─ 水平刀 yd:北帶(臥室)/ 南側
    │    北帶:垂直刀 ×(n-1) → 臥室格;主臥西南角挖固定尺寸套衛(n≥3)
    ├─ 水平刀 yc(可選):走道帶 [bx0, x_he]
    └─ 南帶:垂直刀 living_e(可選,餐廳)、垂直刀 sx(服務核)
         服務核:水平刀 yb → 浴廁(南)/ 廚房(北,讓開走道端)
         客廳:南牆挖玄關落塵區(寬固定,位置可學)

模型只學 10 個「刀位」+ 2 個開關(走道/獨立餐廳);組裝器負責把刀位
變成房間多邊形——排序、夾範圍、挖角都是表示法的一部分,不是設計規則
(沒有最小房寬、沒有採光檢核——那些仍然只有規則引擎有)。

本模組提供:
  * extract_theta(row):既有資料集的 bbox → 刀位參數(訓練標籤)。
  * assemble(theta, site_w, site_d, bedrooms):刀位 → 房間多邊形
    (shapely),保證精確鋪滿建築矩形。
  * python src/ml/bsp.py:抽取正確性驗證(θ→組裝→跟原 bbox 對 IoU)。

⚠️ 組裝器內嵌了兩條「基地→建築範圍」的簡單規則(退縮 2m、深度封頂
11.5m 置中)——這是刻意的:BSP 路線的本質就是把一部分領域知識搬進
表示法,經驗上該講清楚。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
from shapely.geometry import box as shp_box

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from src.ml.dataset import SLOTS

# θ 佈局(10 個連續刀位,全部歸一化到 0~1)+ 2 個開關。
# 連續值的歸一化基準:x 類除以建築寬、y 類除以建築深(不是基地——
# 建築範圍由組裝器從基地算出,見 building_rect)。
THETA_NAMES = ["yd", "yc", "bed1", "bed2", "bed3",
               "sx", "living_e", "yb", "foyer_cx", "x_he"]
N_THETA = len(THETA_NAMES)
FLAG_NAMES = ["hall", "dining"]
N_FLAGS = len(FLAG_NAMES)

FOYER_HALF_W = 1.1          # 玄關半寬(m;引擎 FOYER_W=2.2m)
FOYER_D = 1.5               # 玄關深(m)
ENSUITE_W, ENSUITE_D = 1.8, 2.0   # 主臥套衛(m;n≥3 固定尺寸)
SETBACK = 2.0               # 退縮(m)
MAX_DEPTH = 11.5            # 建築深度封頂(m;引擎 MAX_HOUSE_DEPTH)


def building_rect(site_w: float, site_d: float):
    """基地(m)→ 建築範圍 (bx0, by0, bx1, by1)(m):退縮+深度封頂置中。"""
    bx0 = by0 = SETBACK
    bx1, by1 = site_w - SETBACK, site_d - SETBACK
    depth = by1 - by0
    if depth > MAX_DEPTH:
        side = (depth - MAX_DEPTH) / 2
        by0, by1 = by0 + side, by1 - side
    return bx0, by0, bx1, by1


def extract_theta(presence, boxes, site_w, site_d, bedrooms):
    """資料集一列(歸一化 bbox)→ (theta[10], flags[2])。

    所有刀位都能從房間 bbox 反推:浴廁 bbox 給 sx/yb、走道 bbox 給
    yc/x_he、餐廳 bbox 給 living_e、臥室 bbox 給 bed1~3、玄關 bbox 給
    foyer_cx。沒出現的欄位填中性值(訓練時用 mask 不罰)。
    """
    bx0, by0, bx1, by1 = building_rect(site_w, site_d)
    W, D = bx1 - bx0, by1 - by0

    def mm(slot, k):            # bbox 欄位(歸一化 → m)
        i = SLOTS.index(slot)
        cx, cy, w, h = boxes[i]
        x0 = (cx - w / 2) * site_w
        y0 = (cy - h / 2) * site_d
        return {"x0": x0, "y0": y0, "x1": x0 + w * site_w,
                "y1": y0 + h * site_d}[k]

    has = {s: presence[SLOTS.index(s)] > 0.5 for s in SLOTS}
    theta = np.zeros(N_THETA, dtype=np.float32)
    flags = np.zeros(N_FLAGS, dtype=np.float32)

    yd = mm("master", "y0")                       # 帶分界 = 主臥下緣
    theta[0] = (yd - by0) / D
    if has["corridor"]:
        flags[0] = 1.0
        theta[1] = (mm("corridor", "y0") - by0) / D
        theta[9] = (mm("corridor", "x1") - bx0) / W
    else:
        theta[1] = theta[0]                       # 無走道:yc = yd
        theta[9] = 0.0
    # 臥室隔牆(由西往東:主臥右緣、臥A右緣、臥B右緣)。
    order = ["master", "bedA", "bedB"]
    for k in range(3):
        if k < bedrooms - 1:
            theta[2 + k] = (mm(order[k], "x1") - bx0) / W
    theta[5] = (mm("bath", "x0") - bx0) / W       # 服務核西牆
    if has["dining"]:
        flags[1] = 1.0
        theta[6] = (mm("dining", "x0") - bx0) / W
    else:
        theta[6] = theta[5]                       # 併餐:客廳直到服務核
    theta[7] = (mm("bath", "y1") - by0) / D       # 浴廁/廚房分界
    theta[8] = ((mm("foyer", "x0") + mm("foyer", "x1")) / 2 - bx0) / W
    return theta, flags


def assemble(theta, flags, site_w, site_d, bedrooms):
    """刀位 → 房間多邊形 [(slot, shapely 多邊形)];保證精確鋪滿建築矩形。

    組裝器只做「表示法的整理」:夾 0~1、切割線排序、挖角不出界。
    沒有任何設計規則——刀位亂給照樣組(組出來可能房寬 30cm,但絕不重疊)。
    """
    bx0, by0, bx1, by1 = building_rect(site_w, site_d)
    W, D = bx1 - bx0, by1 - by0
    t = np.clip(np.asarray(theta, dtype=np.float64), 0.02, 0.98)
    hall = flags[0] > 0.5
    dining = flags[1] > 0.5

    yd = by0 + t[0] * D
    yc = by0 + min(t[1], t[0]) * D if hall else yd
    cuts = sorted(bx0 + t[2 + k] * W for k in range(bedrooms - 1))
    bed_x = [bx0] + cuts + [None]                 # 東端下面補
    sx = bx0 + t[5] * W
    living_e = bx0 + min(t[6], t[5]) * W if dining else sx
    yb = by0 + min(t[7], t[0]) * D
    x_he = max(sx, bx0 + t[9] * W) if hall else sx

    rooms = []

    # 北帶:臥室格(西→東);主臥挖套衛(n≥3,固定尺寸夾進主臥格內)。
    edges = [bx0] + cuts + [bx1]
    for i in range(bedrooms):
        cell = shp_box(edges[i], yd, edges[i + 1], by1)
        slot = ["master", "bedA", "bedB", "bedC"][i]
        if i == 0 and bedrooms >= 3:
            ew = min(ENSUITE_W, (edges[1] - edges[0]) * 0.8)
            ed = min(ENSUITE_D, (by1 - yd) * 0.8)
            ens = shp_box(edges[0], yd, edges[0] + ew, yd + ed)
            rooms.append(("ensuite", ens))
            cell = cell.difference(ens)
        rooms.append((slot, cell))

    # 走道帶(有走道才有)。
    if hall and yd - yc > 1e-9:
        rooms.append(("corridor", shp_box(bx0, yc, x_he, yd)))

    # 南帶:客廳(挖玄關)| 餐廳 | 服務核(浴廁/廚房;廚房讓開走道端)。
    fcx = bx0 + t[8] * W
    fx0 = max(bx0, min(fcx - FOYER_HALF_W, living_e - 2 * FOYER_HALF_W))
    fx1 = min(living_e, fx0 + 2 * FOYER_HALF_W)
    fy1 = by0 + min(FOYER_D, max(0.3, (yc - by0) - 0.7))
    foyer = shp_box(fx0, by0, fx1, fy1)
    rooms.append(("foyer", foyer))
    rooms.append(("living", shp_box(bx0, by0, living_e, yc).difference(foyer)))
    if dining and sx - living_e > 1e-9:
        rooms.append(("dining", shp_box(living_e, by0, sx, yc)))
    rooms.append(("bath", shp_box(sx, by0, bx1, yb)))
    kitchen = shp_box(sx, yb, bx1, yd)
    if hall and x_he > sx:                        # 走道吃進廚房角
        kitchen = kitchen.difference(shp_box(sx, yc, x_he, yd))
    rooms.append(("kitchen", kitchen))
    return [(s, p) for s, p in rooms if not p.is_empty and p.area > 1e-9]


def main() -> None:
    """抽取正確性驗證:θ_true → 組裝 → 跟資料集原 bbox 對平均 IoU。"""
    data = np.load(_PROJECT_ROOT / "output" / "ml" / "dataset.npz")
    X, P, B, briefs = data["X"], data["P"], data["B"], data["briefs"]
    rng = np.random.default_rng(0)
    ious = []
    for i in rng.choice(len(X), 300, replace=False):
        w, d, n = briefs[i]
        theta, flags = extract_theta(P[i], B[i], w, d, int(n))
        rooms = assemble(theta, flags, w, d, int(n))
        by_slot = {s: p for s, p in rooms}
        for j, slot in enumerate(SLOTS):
            if P[i][j] < 0.5 or slot not in by_slot:
                continue
            cx, cy, bw, bh = B[i][j]
            true_box = shp_box((cx - bw / 2) * w, (cy - bh / 2) * d,
                               (cx + bw / 2) * w, (cy + bh / 2) * d)
            gx0, gy0, gx1, gy1 = by_slot[slot].bounds
            got = shp_box(gx0, gy0, gx1, gy1)
            u = true_box.union(got).area
            ious.append(true_box.intersection(got).area / u if u else 0.0)
    print(f"θ 抽取+組裝還原驗證:300 張圖、{len(ious)} 個房間,"
          f"bbox 平均 IoU = {np.mean(ious):.3f}(≈1 代表表示法無損)")


if __name__ == "__main__":
    main()
