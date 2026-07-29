"""ML 對照實驗(F1)第 2 步:小模型 —— 學「需求 → 房間矩形」的神經網路。

模型(RoomPainter)刻意選最簡單的形狀,像一個「看過 4000 份考古題的學生」:
  * 輸入 6 維:基地寬深 + 房數 one-hot(dataset.brief_features)。
  * 兩層隱藏層(128)的 MLP(多層感知機——最基本的神經網路,一層層的
    加權平均+開關,沒有摺積沒有注意力,CPU 幾十秒就能訓完)。
  * 輸出每個房間欄位(11 個)的:出現機率(1)+ 矩形(中心x,y,寬,高)(4)。

損失函數 = 出現與否的 BCE + 有出現的欄位矩形 SmoothL1(沒出現的欄位不罰
矩形——不存在的房間畫哪都無所謂)。

重點:模型「只看答案、沒看規則」。它不知道房間不能重疊、不知道柱網、
不知道採光——這些知識在訓練資料裡只是「恰好都成立」。對照實驗
(compare.py)就是要看:沒有規則把關,學出來的直覺會在哪裡露餡。

典型用法::

    python src/ml/train.py          # 讀 output/ml/dataset.npz → 訓練 →
                                    # 存 output/ml/room_painter.pt + 測試指標
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import torch
from torch import nn

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from src.ml.dataset import SLOTS

N_SLOTS = len(SLOTS)          # 11
IN_DIM = 6
HIDDEN = 128
EPOCHS = 300
BATCH = 256
LR = 1e-3
TEST_FRAC = 0.15              # 15% 留當「沒看過的考題」


class RoomPainter(nn.Module):
    """需求 → 每個房間欄位的(出現 logit, 矩形)。"""

    def __init__(self) -> None:
        super().__init__()
        self.body = nn.Sequential(
            nn.Linear(IN_DIM, HIDDEN), nn.ReLU(),
            nn.Linear(HIDDEN, HIDDEN), nn.ReLU(),
        )
        self.head_presence = nn.Linear(HIDDEN, N_SLOTS)
        self.head_boxes = nn.Linear(HIDDEN, N_SLOTS * 4)

    def forward(self, x):
        h = self.body(x)
        return self.head_presence(h), self.head_boxes(h).view(-1, N_SLOTS, 4)


def train() -> dict:
    data = np.load(_PROJECT_ROOT / "output" / "ml" / "dataset.npz")
    X = torch.from_numpy(data["X"])
    P = torch.from_numpy(data["P"])
    B = torch.from_numpy(data["B"])

    # 切訓練/測試(固定種子,可重現)。
    g = torch.Generator().manual_seed(0)
    idx = torch.randperm(len(X), generator=g)
    n_test = int(len(X) * TEST_FRAC)
    test_i, train_i = idx[:n_test], idx[n_test:]

    model = RoomPainter()
    opt = torch.optim.Adam(model.parameters(), lr=LR)
    bce = nn.BCEWithLogitsLoss()
    sl1 = nn.SmoothL1Loss(reduction="none")

    def loss_fn(xb, pb, bb):
        logit, box = model(xb)
        l_p = bce(logit, pb)
        l_b = (sl1(box, bb).mean(dim=2) * pb).sum() / pb.sum().clamp(min=1)
        return l_p + l_b

    model.train()
    for epoch in range(EPOCHS):
        perm = train_i[torch.randperm(len(train_i), generator=g)]
        for s in range(0, len(perm), BATCH):
            b = perm[s:s + BATCH]
            opt.zero_grad()
            loss = loss_fn(X[b], P[b], B[b])
            loss.backward()
            opt.step()
        if (epoch + 1) % 50 == 0:
            model.eval()
            with torch.no_grad():
                lt = loss_fn(X[test_i], P[test_i], B[test_i]).item()
            model.train()
            print(f"  epoch {epoch+1:3d}  測試損失 {lt:.4f}")

    # 測試集指標:出現判斷正確率 + 有出現欄位的矩形 IoU。
    model.eval()
    with torch.no_grad():
        logit, box = model(X[test_i])
        pred_p = (torch.sigmoid(logit) > 0.5).float()
        acc = (pred_p == P[test_i]).float().mean().item()
        iou = _mean_iou(box, B[test_i], P[test_i])
    out = _PROJECT_ROOT / "output" / "ml"
    torch.save(model.state_dict(), out / "room_painter.pt")
    n_params = sum(p.numel() for p in model.parameters())
    print(f"模型存檔 → {out / 'room_painter.pt'}({n_params:,} 參數)")
    print(f"測試集(沒看過的 {n_test} 題):出現判斷正確率 {acc:.1%}、"
          f"矩形平均 IoU {iou:.3f}")
    return {"acc": acc, "iou": iou}


def _mean_iou(pred: torch.Tensor, true: torch.Tensor,
              mask: torch.Tensor) -> float:
    """(中心,寬高) 矩形的平均 IoU(只算真的存在的欄位)。"""
    def corners(b):
        cx, cy, w, h = b.unbind(-1)
        return cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2

    ax0, ay0, ax1, ay1 = corners(pred)
    bx0, by0, bx1, by1 = corners(true)
    iw = (torch.minimum(ax1, bx1) - torch.maximum(ax0, bx0)).clamp(min=0)
    ih = (torch.minimum(ay1, by1) - torch.maximum(ay0, by0)).clamp(min=0)
    inter = iw * ih
    union = ((ax1 - ax0) * (ay1 - ay0)).clamp(min=0) \
        + (bx1 - bx0) * (by1 - by0) - inter
    iou = inter / union.clamp(min=1e-9)
    return (iou * mask).sum().item() / mask.sum().clamp(min=1).item()


if __name__ == "__main__":
    train()
