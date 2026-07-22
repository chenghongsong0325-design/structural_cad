"""ML 對照實驗(F1)第 1 步:資料工廠 —— 規則引擎量產「標準答案」。

「訓練生成模型直接畫房間」的對照實驗:學術界主流(FloorplanGAN、
ChatHouseDiffusion…)是用真實建案資料集訓練生成模型,讓模型直接輸出房間。
我們沒有那種資料集,但有更好的東西——自己的規則引擎,每張圖都通過
validate_spec(不重疊/有門窗/採光/柱網),是「保證合格」的標準答案。

做法(像出考古題):
  * 隨機抽幾千組(基地寬, 基地深, 房數),餵給 generate_floor_plan,
    生得出來的就是一筆訓練資料(生不出來的組合直接跳過——太小的基地
    塞不下 4 房,引擎會拒絕,這正是規則的價值)。
  * 每張圖轉成「向量表示」:固定 11 個房間欄位(見 SLOTS),每欄記
    (有沒有這間, 中心x, 中心y, 寬, 高),座標都除以基地尺寸歸一化到 0~1
    ——像把平面圖填進一張固定格式的表格,模型學的就是「需求 → 表格」。
  * L 形房間(客廳挖玄關角、主臥挖套衛角)取外接矩形(bounding box)
    ——小模型只學「房間大概在哪、多大」,不學精確形狀。這是刻意的簡化,
    對照實驗的重點是「模型畫的房間會不會重疊/漏地板」,不是形狀細節。

輸出 output/ml/dataset.npz(output/ 不進版本控制,隨時可重新量產)。

典型用法::

    python src/ml/dataset.py            # 量產 4000 筆 → output/ml/dataset.npz

⚠️ 待確認假設見模組結尾 PENDING 區塊。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from src.design.layout_generator import HouseBrief, generate_floor_plan

# 房間欄位表(固定順序):單層透天會出現的 11 種房間。
# 名稱 → 欄位索引;「客餐廳」跟「客廳」都算 living(餐廳併入時名稱不同)。
SLOTS = ["living", "foyer", "dining", "corridor", "bath",
         "kitchen", "master", "ensuite", "bedA", "bedB", "bedC"]
SLOT_LABELS = ["客廳", "玄關", "餐廳", "走道", "浴廁",
               "廚房", "主臥室", "主臥浴", "臥室A", "臥室B", "臥室C"]
_NAME_TO_SLOT = {
    "客廳": 0, "客餐廳": 0, "玄關": 1, "餐廳": 2, "走道": 3, "浴廁": 4,
    "廚房": 5, "主臥室": 6, "主臥浴": 7, "臥室A": 8, "臥室B": 9, "臥室C": 10,
}

# 抽樣範圍(米):單層兩帶式的可行區間(基地再小引擎會拒絕,取樣時跳過)。
SITE_W_RANGE = (12.0, 26.0)
SITE_D_RANGE = (11.0, 18.0)
BEDROOM_RANGE = (1, 4)


def spec_to_row(spec, site_w: float, site_d: float):
    """一張 FloorPlanSpec → (presence[11], boxes[11,4]);座標歸一化 0~1。

    boxes 每列 = (中心x, 中心y, 寬, 高)/基地尺寸;沒出現的欄位全 0。
    """
    presence = np.zeros(len(SLOTS), dtype=np.float32)
    boxes = np.zeros((len(SLOTS), 4), dtype=np.float32)
    for room in spec.rooms:
        idx = _NAME_TO_SLOT.get(room.name)
        if idx is None:            # 書房/孝親房等不在基本盤,資料集不含
            continue
        xs = [p[0] for p in room.points]
        ys = [p[1] for p in room.points]
        x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
        presence[idx] = 1.0
        boxes[idx] = ((x0 + x1) / 2 / (site_w * 1000),
                      (y0 + y1) / 2 / (site_d * 1000),
                      (x1 - x0) / (site_w * 1000),
                      (y1 - y0) / (site_d * 1000))
    return presence, boxes


def brief_features(site_w: float, site_d: float, bedrooms: int) -> np.ndarray:
    """需求 → 模型輸入向量(6 維):基地寬深(除以 30 歸一化)+ 房數 one-hot。"""
    feat = np.zeros(6, dtype=np.float32)
    feat[0] = site_w / 30.0
    feat[1] = site_d / 30.0
    feat[1 + bedrooms] = 1.0          # bedrooms 1~4 → 索引 2~5
    return feat


def build_dataset(n_target: int = 4000, seed: int = 42):
    """量產資料集:隨機抽需求 → 引擎出圖 → 向量化;回傳 (X, P, B, briefs)。

    briefs 是 (site_w, site_d, bedrooms) 清單,對照實驗抽考題用。
    去重:同一組需求只收一次(引擎是確定性的,重複組合是同一張圖)。
    """
    rng = np.random.default_rng(seed)
    seen: set = set()
    X, P, B, briefs = [], [], [], []
    tried = 0
    while len(X) < n_target and tried < n_target * 6:
        tried += 1
        w = round(float(rng.uniform(*SITE_W_RANGE)), 2)
        d = round(float(rng.uniform(*SITE_D_RANGE)), 2)
        n = int(rng.integers(BEDROOM_RANGE[0], BEDROOM_RANGE[1] + 1))
        key = (w, d, n)
        if key in seen:
            continue
        seen.add(key)
        try:
            spec = generate_floor_plan(
                HouseBrief(site_width=w * 1000, site_depth=d * 1000, bedrooms=n))
        except ValueError:
            continue                   # 基地塞不下 → 引擎拒絕,跳過
        p, b = spec_to_row(spec, w, d)
        X.append(brief_features(w, d, n))
        P.append(p)
        B.append(b)
        briefs.append(key)
    return (np.stack(X), np.stack(P), np.stack(B),
            np.array(briefs, dtype=np.float32))


def main() -> None:
    out = _PROJECT_ROOT / "output" / "ml"
    out.mkdir(parents=True, exist_ok=True)
    X, P, B, briefs = build_dataset()
    np.savez(out / "dataset.npz", X=X, P=P, B=B, briefs=briefs)
    n_by_bed = {k: int((briefs[:, 2] == k).sum()) for k in (1, 2, 3, 4)}
    print(f"資料集 {len(X)} 筆 → {out / 'dataset.npz'}")
    print(f"  房數分布:{n_by_bed}")
    print(f"  輸入 {X.shape} / 出現 {P.shape} / 矩形 {B.shape}")


if __name__ == "__main__":
    main()


# =============================================================================
# PENDING(待確認假設彙整)
# =============================================================================
# 1. 資料只含「基本盤」單層透天(無書房/孝親房/方位約束)——對照實驗聚焦
#    「需求→房間位置」的核心映射;要教模型更多旋鈕,加輸入維度重訓即可。
# 2. L 形房間取外接矩形:客廳 bbox 會跟玄關 bbox 重疊(玄關本來就是從客廳
#    挖出來的角)。評估重疊率時以「規則引擎自己的 bbox 重疊率」當基準線,
#    比模型的絕對重疊率公平。
# 3. 抽樣範圍是單層兩帶式的合理區間;範圍外(超大基地/超深基地)模型沒看
#    過就會亂畫——這正是對照實驗要展示的「ML 外插失效」。
# =============================================================================
