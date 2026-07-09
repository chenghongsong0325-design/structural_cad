"""把 DXF 渲染成 PNG 預覽圖(給沒有 AutoCAD 時檢查用)。
用法:python scripts/preview.py
"""
from pathlib import Path
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import ezdxf
from ezdxf.addons.drawing import RenderContext, Frontend
from ezdxf.addons.drawing.matplotlib import MatplotlibBackend

root = Path(__file__).resolve().parents[1]
doc = ezdxf.readfile(root / "output" / "hello_cad.dxf")
msp = doc.modelspace()

fig = plt.figure(figsize=(10, 4))
ax = fig.add_axes([0, 0, 1, 1])
ax.set_facecolor("black")           # 用黑底,模擬 CAD 深色畫面
fig.patch.set_facecolor("black")
Frontend(RenderContext(doc), MatplotlibBackend(ax)).draw_layout(msp, finalize=True)

out = root / "output" / "hello_cad_preview.png"
fig.savefig(out, dpi=120, facecolor="black")
print("[OK] 預覽圖:", out)
