"""ML 對照實驗(F2)訓練:同一副身體、換一顆輸出頭 —— 學「刀位」不學「矩形」。

跟 F1(train.py)刻意保持公平對照:
  * 同一批資料(dataset.npz 的 4000 筆,同一個 85/15 切分種子)。
  * 同一副 MLP 身體(6→128→128)、同 epochs、同學習率。
  * 唯一差別:輸出頭從「11 個自由矩形」換成「10 個刀位 + 2 個開關」
    (bsp.py 的 θ)——房間由組裝器切出來,重疊/漏地板在結構上不可能。

損失 = 開關 BCE + 刀位 SmoothL1(mask:走道刀位只在有走道時罰、
餐廳刀位只在有餐廳時罰、臥室刀位只罰前 n-1 個)。

典型用法::

    python src/ml/train_bsp.py     # → output/ml/bsp_painter.pt + 測試指標
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

from src.ml.bsp import N_FLAGS, N_THETA, extract_theta
from src.ml.train import BATCH, EPOCHS, HIDDEN, IN_DIM, LR, TEST_FRAC


class BSPPainter(nn.Module):
    """需求 → (刀位 θ[10](sigmoid 到 0~1), 開關 logits[2])。"""

    def __init__(self) -> None:
        super().__init__()
        self.body = nn.Sequential(
            nn.Linear(IN_DIM, HIDDEN), nn.ReLU(),
            nn.Linear(HIDDEN, HIDDEN), nn.ReLU(),
        )
        self.head_theta = nn.Linear(HIDDEN, N_THETA)
        self.head_flags = nn.Linear(HIDDEN, N_FLAGS)

    def forward(self, x):
        h = self.body(x)
        return torch.sigmoid(self.head_theta(h)), self.head_flags(h)


def _theta_mask(flags: torch.Tensor, bedrooms: torch.Tensor) -> torch.Tensor:
    """每筆該罰哪些刀位:走道相關(yc,x_he)看 hall、living_e 看 dining、
    臥室刀位看房數;其餘恆罰。"""
    n = len(flags)
    m = torch.ones(n, N_THETA)
    m[:, 1] = flags[:, 0]                # yc
    m[:, 9] = flags[:, 0]                # x_he
    m[:, 6] = flags[:, 1]                # living_e
    for k in range(3):                   # bed1~3:只有前 n-1 刀存在
        m[:, 2 + k] = (bedrooms > k + 1).float()
    return m


def train() -> None:
    data = np.load(_PROJECT_ROOT / "output" / "ml" / "dataset.npz")
    X = torch.from_numpy(data["X"])
    briefs = data["briefs"]

    theta_all, flags_all = [], []
    for i in range(len(X)):
        w, d, n = briefs[i]
        th, fl = extract_theta(data["P"][i], data["B"][i], w, d, int(n))
        theta_all.append(th)
        flags_all.append(fl)
    T = torch.from_numpy(np.stack(theta_all))
    F = torch.from_numpy(np.stack(flags_all))
    beds = torch.from_numpy(briefs[:, 2])
    M = _theta_mask(F, beds)

    g = torch.Generator().manual_seed(0)          # 與 F1 同切分
    idx = torch.randperm(len(X), generator=g)
    n_test = int(len(X) * TEST_FRAC)
    test_i, train_i = idx[:n_test], idx[n_test:]

    model = BSPPainter()
    opt = torch.optim.Adam(model.parameters(), lr=LR)
    bce = nn.BCEWithLogitsLoss()
    sl1 = nn.SmoothL1Loss(reduction="none")

    def loss_fn(b):
        theta, logit = model(X[b])
        l_f = bce(logit, F[b])
        l_t = (sl1(theta, T[b]) * M[b]).sum() / M[b].sum().clamp(min=1)
        return l_f + l_t * 10          # 刀位是主角,加權讓兩項同量級

    model.train()
    for epoch in range(EPOCHS):
        perm = train_i[torch.randperm(len(train_i), generator=g)]
        for s in range(0, len(perm), BATCH):
            opt.zero_grad()
            loss = loss_fn(perm[s:s + BATCH])
            loss.backward()
            opt.step()
        if (epoch + 1) % 50 == 0:
            model.eval()
            with torch.no_grad():
                lt = loss_fn(test_i).item()
            model.train()
            print(f"  epoch {epoch+1:3d}  測試損失 {lt:.4f}")

    model.eval()
    with torch.no_grad():
        theta, logit = model(X[test_i])
        flag_acc = ((torch.sigmoid(logit) > 0.5).float()
                    == F[test_i]).float().mean().item()
        err = ((theta - T[test_i]).abs() * M[test_i]).sum() \
            / M[test_i].sum()
    out = _PROJECT_ROOT / "output" / "ml"
    torch.save(model.state_dict(), out / "bsp_painter.pt")
    n_params = sum(p.numel() for p in model.parameters())
    print(f"模型存檔 → {out / 'bsp_painter.pt'}({n_params:,} 參數)")
    print(f"測試集:開關正確率 {flag_acc:.1%}、刀位平均誤差 "
          f"{err.item():.4f}(×建築尺寸,0.01≈十幾公分)")


if __name__ == "__main__":
    train()
