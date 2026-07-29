"""ML 對照實驗(F1+F2)考試 —— 沒看過的考題上,三方同台:
規則引擎 vs 自由矩形模型(F1)vs BSP 切割模型(F2)。

拿訓練時沒見過的需求(含一題「超出訓練範圍」的外插題)同時考:
  * 規則引擎:每張都通過 validate_spec,牆對齊、面積鋪滿、有門有窗。
  * RoomPainter(F1):自由畫 11 個矩形——沒有任何機制保證不重疊。
  * BSPPainter(F2):只預測「刀位」,房間由組裝器切出來——重疊/漏地板
    在結構上不可能(by construction),看它 IoU 追不追得上。

每題量三個數字(都以基地面積為分母):
  * IoU:模型的房間跟正確答案平均重疊多少(1.0 = 完全一樣)。
  * 重疊率:模型畫的房間彼此壓在一起的面積比。規則引擎的 bbox 也有
    「天生重疊」(客廳 bbox 蓋到玄關——玄關本來就是從客廳挖出來的角),
    所以並列顯示當基準線,看模型「多出來」的重疊才公平。
  * 覆蓋率:房間聯集佔建築範圍多少(規則引擎 = 100%,模型會漏縫)。

輸出 output/ml/compare.json(給視覺化頁面畫並排圖)+ 終端摘要表。

典型用法::

    python src/ml/compare.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import torch
from shapely.geometry import box as shp_box
from shapely.ops import unary_union

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from src.design.layout_generator import HouseBrief, generate_floor_plan
from src.ml.bsp import assemble
from src.ml.dataset import SLOTS, SLOT_LABELS, brief_features, spec_to_row
from src.ml.train import RoomPainter
from src.ml.train_bsp import BSPPainter

# 考題:前四題在訓練分布內(但組合沒出現過),最後一題刻意超出訓練範圍
# (基地 28×19m > 抽樣上限 26×18)——看模型外插時會發生什麼事。
EXAM = [
    (12.4, 11.2, 1, "小基地 1 房"),
    (15.85, 12.55, 2, "中基地 2 房"),
    (19.37, 13.61, 3, "中大基地 3 房"),
    (23.13, 15.77, 4, "大基地 4 房"),
    (28.0, 19.0, 3, "外插題:28×19m(超出訓練範圍)"),
]


def _boxes_mm(presence, boxes, site_w, site_d, thr=0.5):
    """歸一化 (出現, 中心寬高) → [(slot, x0, y0, x1, y1) mm]。"""
    out = []
    for i in range(len(SLOTS)):
        if presence[i] < thr:
            continue
        cx, cy, w, h = boxes[i]
        out.append((i,
                    (cx - w / 2) * site_w * 1000, (cy - h / 2) * site_d * 1000,
                    (cx + w / 2) * site_w * 1000, (cy + h / 2) * site_d * 1000))
    return out


def _geometry_stats(rects, building_area):
    """矩形清單 → (兩兩重疊面積比, 覆蓋率)——分母都是建築面積。"""
    polys = [shp_box(x0, y0, x1, y1) for _, x0, y0, x1, y1 in rects
             if x1 > x0 and y1 > y0]
    overlap = sum(polys[i].intersection(polys[j]).area
                  for i in range(len(polys)) for j in range(i + 1, len(polys)))
    coverage = unary_union(polys).area if polys else 0.0
    return overlap / building_area, coverage / building_area


def _pair_iou(rule_rects, model_rects):
    """同欄位矩形的平均 IoU(以規則引擎有的欄位為準)。"""
    model_by = {r[0]: r for r in model_rects}
    ious = []
    for slot, ax0, ay0, ax1, ay1 in rule_rects:
        m = model_by.get(slot)
        if m is None:
            ious.append(0.0)
            continue
        _, bx0, by0, bx1, by1 = m
        iw = max(0.0, min(ax1, bx1) - max(ax0, bx0))
        ih = max(0.0, min(ay1, by1) - max(ay0, by0))
        inter = iw * ih
        union = (ax1 - ax0) * (ay1 - ay0) + (bx1 - bx0) * (by1 - by0) - inter
        ious.append(inter / union if union > 0 else 0.0)
    return float(np.mean(ious)) if ious else 0.0


def _bsp_stats(polys_by_slot, rule_rects, building_area):
    """BSP 多邊形 → (IoU vs 規則 bbox, 重疊率, 覆蓋率)。

    IoU 用多邊形的 bbox 對規則 bbox(跟 F1 同一把尺);重疊/覆蓋用
    精確多邊形——表示法保證應為 0% / 100%,量出來就是驗證。
    """
    rects = []
    for slot, poly in polys_by_slot:
        x0, y0, x1, y1 = poly.bounds
        rects.append((SLOTS.index(slot), x0, y0, x1, y1))
    iou = _pair_iou(rule_rects, rects)
    polys = [p for _, p in polys_by_slot]
    overlap = sum(polys[i].intersection(polys[j]).area
                  for i in range(len(polys)) for j in range(i + 1, len(polys)))
    coverage = unary_union(polys).area
    return iou, overlap / building_area, coverage / building_area


def main() -> None:
    model = RoomPainter()
    model.load_state_dict(
        torch.load(_PROJECT_ROOT / "output" / "ml" / "room_painter.pt",
                   weights_only=True))
    model.eval()
    bsp_model = BSPPainter()
    bsp_model.load_state_dict(
        torch.load(_PROJECT_ROOT / "output" / "ml" / "bsp_painter.pt",
                   weights_only=True))
    bsp_model.eval()

    # 確認考題不在訓練資料裡。
    data = np.load(_PROJECT_ROOT / "output" / "ml" / "dataset.npz")
    trained = {tuple(r) for r in data["briefs"].round(2).tolist()}

    results = []
    print(f"{'考題':<28}{'IoU 自由/BSP':>13}{'重疊 自由/BSP':>15}"
          f"{'覆蓋 自由/BSP':>17}")
    for w, d, n, label in EXAM:
        assert (round(w, 2), round(d, 2), float(n)) not in trained, \
            f"考題 {label} 出現在訓練資料裡"
        spec = generate_floor_plan(
            HouseBrief(site_width=w * 1000, site_depth=d * 1000, bedrooms=n))
        p_true, b_true = spec_to_row(spec, w, d)
        with torch.no_grad():
            logit, box = model(torch.from_numpy(brief_features(w, d, n))[None])
        p_pred = torch.sigmoid(logit)[0].numpy()
        b_pred = box[0].numpy()

        rule_rects = _boxes_mm(p_true, b_true, w, d)
        model_rects = _boxes_mm(p_pred, b_pred, w, d)

        bx0, by0 = spec.grid_origin
        bw, bd = sum(spec.x_spacings), sum(spec.y_spacings)
        area = bw * bd
        r_ov, r_cov = _geometry_stats(rule_rects, area)
        m_ov, m_cov = _geometry_stats(model_rects, area)
        iou = _pair_iou(rule_rects, model_rects)

        # BSP 模型:刀位 → 組裝(m 座標,配合 assemble 的單位)→ 換算 mm。
        with torch.no_grad():
            theta, flogit = bsp_model(
                torch.from_numpy(brief_features(w, d, n))[None])
        bsp_flags = (torch.sigmoid(flogit)[0] > 0.5).float().numpy()
        bsp_rooms_m = assemble(theta[0].numpy(), bsp_flags, w, d, n)
        from shapely.affinity import scale as shp_scale
        bsp_rooms = [(s, shp_scale(p, 1000, 1000, origin=(0, 0)))
                     for s, p in bsp_rooms_m]
        b_iou, b_ov, b_cov = _bsp_stats(bsp_rooms, rule_rects, area)

        print(f"{label:<28}{iou:>6.3f}{b_iou:>7.3f}"
              f"{m_ov:>8.1%}{b_ov:>7.1%}"
              f"{m_cov:>9.1%}{b_cov:>8.1%}")
        results.append(dict(
            brief=dict(site_w=w, site_d=d, bedrooms=n, label=label),
            building=dict(x0=bx0, y0=by0, w=bw, d=bd),
            rule=[dict(slot=SLOTS[s], label=SLOT_LABELS[s],
                       x0=float(x0), y0=float(y0), x1=float(x1), y1=float(y1))
                  for s, x0, y0, x1, y1 in rule_rects],
            model=[dict(slot=SLOTS[s], label=SLOT_LABELS[s],
                        x0=float(x0), y0=float(y0), x1=float(x1), y1=float(y1))
                   for s, x0, y0, x1, y1 in model_rects],
            bsp=[dict(slot=s, label=SLOT_LABELS[SLOTS.index(s)],
                      poly=[[float(x), float(y)]
                            for x, y in p.exterior.coords])
                 for s, p in bsp_rooms],
            metrics=dict(iou=float(iou), model_overlap=float(m_ov),
                         rule_overlap=float(r_ov),
                         model_coverage=float(m_cov),
                         rule_coverage=float(r_cov),
                         bsp_iou=float(b_iou), bsp_overlap=float(b_ov),
                         bsp_coverage=float(b_cov)),
        ))

    out = _PROJECT_ROOT / "output" / "ml" / "compare.json"
    out.write_text(json.dumps(results, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    print(f"\n對照結果 → {out}")


if __name__ == "__main__":
    main()
