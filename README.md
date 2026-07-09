# structural_cad — 結構 CAD 繪圖專案

用 Python + [ezdxf](https://ezdxf.mozman.at/) 產生結構工程 DXF 圖檔的專案。
這是路線圖 **Phase 0 / P0** 的成果:一支「Hello CAD」程式,建立 3 個圖層、畫線、加文字、存成 DXF。

---

## 資料夾結構

```
structural_cad/
├── README.md              # 本說明
├── requirements.txt       # 套件清單
├── .gitignore
├── config/                # (未來)繪圖標準設定檔:圖層/文字/標註/圖框
├── src/
│   ├── standards/         # (未來)讀取並套用繪圖標準
│   ├── model/             # (未來)語意建築模型:柱/梁/版/配筋
│   ├── drafting/          # 製圖引擎
│   │   └── hello_cad.py   # ← 本階段的主程式
│   └── tools/             # (未來)給 AI 呼叫的工具層 / MCP
├── scripts/
│   └── preview.py         # 把 DXF 渲染成 PNG 預覽
├── tests/                 # (未來)單元測試
└── output/                # 產出的 DXF 與預覽圖
```

> 空的 `standards / model / tools / tests` 資料夾是**刻意先留位**的,對應路線圖後面的階段。這種「一開始就把關注點分開」的結構,能讓專案長大時不會亂。

---

## 環境建置(在你自己的電腦上執行)

需要先安裝 Python 3.10 以上。以下指令請在專案根目錄 `structural_cad/` 下執行。

### Windows (PowerShell)

```powershell
# 1) 建立虛擬環境(把這個專案的套件跟系統隔離,不會互相污染)
python -m venv .venv

# 2) 啟用虛擬環境(啟用後,命令列前面會出現 (.venv))
.venv\Scripts\Activate.ps1

# 3) 安裝套件
pip install -r requirements.txt

# 4) 初始化 Git 版本控管
git init
git add .
git commit -m "Phase 0: Hello CAD"
```

### macOS / Linux

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
git init
git add .
git commit -m "Phase 0: Hello CAD"
```

---

## 執行

```bash
# 產生 DXF(輸出到 output/hello_cad.dxf)
python src/drafting/hello_cad.py

# (選用)渲染成 PNG 預覽,不用開 AutoCAD 也能先看
python scripts/preview.py
```

---

## 如何確認 DXF 能被 AutoCAD 正常開啟

1. 直接在 AutoCAD(或 DWG TrueView、BricsCAD、免費的 LibreCAD)開啟 `output/hello_cad.dxf`。
2. 打開「圖層」面板,應看到 `軸線 / 梁 / 文字` 三個圖層,顏色與線型各不相同。
3. 若中文顯示為問號或方框,把「文字」圖層用到的文字型指定為支援中文的字型(如「標楷體」「新細明體」或 `.shx` 中文字型)即可。
4. 若虛線/中心線看起來像實線,在命令列輸入 `LTSCALE`,調整全域線型比例。
