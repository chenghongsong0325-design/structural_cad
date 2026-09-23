# 本機建築知識檢索（RAG）

目前已接到需求解析、透天選配及房間關係圖的首次設計與修改流程。RAG 將相關文字提供給 Gemini 參考；座標、尺寸、門窗、樓梯與動線仍由既有引擎計算和檢核。

## 啟用

在專案根目錄的 PowerShell 執行：

```powershell
.venv\Scripts\python.exe -m pip install -r requirements-rag.txt
.venv\Scripts\python.exe -m src.knowledge prepare
.venv\Scripts\python.exe -m src.knowledge search "一樓要停車，客廳放二樓"
.venv\Scripts\python.exe -m uvicorn src.web.app:app --reload
```

第一次 `prepare` 需要網路下載公開模型，之後檢索可離線執行；Gemini 生成仍需要原有 API 設定。`prepare` 不呼叫 Gemini。若 Hugging Face 的 Xet 下載長時間無進展，可先設定 `$env:HF_HUB_DISABLE_XET = "1"` 再執行。首次載入模型比後續檢索慢。

重新啟動原本的網頁服務後，生成方案並展開「設計參考資料」，可看到送入模型的段落、用途、來源檔案及行號。歷史方案也會保留當次引用。這份紀錄表示資料已提供給模型，不證明模型採納了資料，更不代表方案已通過所有建築法規。

## 向量與檢索定義

| 項目 | 實作 |
|---|---|
| 向量模型 | `intfloat/multilingual-e5-small`，CPU 本機推論 |
| 固定版本 | `614241f622f53c4eeff9890bdc4f31cfecc418b3` |
| 向量維度 | 384 維浮點數，經 L2 正規化 |
| 文字前綴 | 查詢 `query: `；文件 `passage: `，包含標題、章節、本文 |
| 切段 | 依 Markdown 標題分節，每段最多 320 字元，長段重疊 40 字元 |
| 模型上限 | 512 tokens；查詢先限制到 2,000 字元 |
| 儲存與排序 | SQLite 儲存段落與 float32 向量，NumPy 精確計算餘弦相似度 |
| 篩選 | 先按用途過濾；預設最多 3 段、每份文件同章節取一段，相似度至少 0.84 |
| 提示詞上限 | 引用 JSON 約 3,600 字元，避免整份資料庫塞入模型 |
| 版本追蹤 | 文件內容、來源、用途與向量模型版本共同形成指紋；更動或刪除即更新索引 |

384 個值是模型學到的文字語意表示，並非「面積、房數、採光」各占一維，也不是房間座標或幾何約束向量。相似度用於排序，不是答案正確率。0.84 是依少量中文功能測試調整的初始門檻，尚未用完整標註檢索集校準；低於門檻時不補入參考段落。

## 資料流與責任

1. 讀取 `knowledge/` 中有 metadata 的 Markdown，以及 `output/rag/imports/` 中已完成匯入的文字。原始檔、manifest、暫存上傳檔不當成索引內容，不掃描整個專案。
2. 文件編碼、查詢編碼、按用途檢索。首次設計查詢需求；重新設計優先查詢檢核問題。
3. 將相關段落與出處附加到原有提示詞，保留原有結構化輸出 schema。
4. Gemini 解析需求或提出選項／關係圖，既有程式進行尺寸計算、配置、修復與檢核。
5. API 回傳 `rag`，並隨 `result.json` 保存；前端顯示實際加入提示詞的資料。

`parse` 只讀術語資料，避免將案例的房數與尺寸誤當本案需求；`townhouse` 讀透天選配知識；`graph` 讀關係圖知識。參考資料是背景資料，不能覆蓋使用者需求或改變輸出格式。

## 新增自己的設計資料

格式見 `knowledge/README.md`。先把經核對的建築案例或設計規則整理成 Markdown，標明來源及適用用途，再執行：

```powershell
.venv\Scripts\python.exe -m src.knowledge index
.venv\Scripts\python.exe -m src.knowledge search "房間有門但走不進去" --stage graph
```

執行中也會依內容變更更新索引。不要將金鑰或個人敏感資料放入知識庫。向量計算留在本機，但檢索命中的文字會隨原本需求傳給 Gemini。

目前附六份小型種子文件：尺寸與需求術語、透天骨架、採光與車庫、可達性、房間關係圖，以及一份標明出處的建築案例摘要。可再匯入自己的 PDF／DWG／圖片；資料庫並非完整建築法規庫。

## 自動匯入 PDF、DWG 與圖片

```powershell
.venv\Scripts\python.exe -m pip install -r requirements-import.txt
.venv\Scripts\python.exe -m src.knowledge prepare-dwg
.venv\Scripts\python.exe -m src.knowledge import "C:\案例\住宅.pdf" "C:\案例\平面圖.dwg"
.venv\Scripts\python.exe -m src.knowledge import "C:\案例資料夾"
```

網頁：啟動或重啟伺服器後，展開左側「匯入參考資料」，選擇一個或多個檔案，按「匯入並建立索引」。處理後展開各筆結果核對文字；顯示「已可檢索」才代表向量索引也成功。資料夾 CLI 會遞迴處理支援格式，一個檔案失敗不影響其餘檔案。

| 輸入 | 擷取方法 | 保留的來源 |
|---|---|---|
| 文字 PDF | PyMuPDF 逐頁讀文字 | 檔名、第幾頁 |
| 掃描／混合 PDF | 頁面渲染後以本機 RapidOCR 辨識；去除和原生文字完全相同的行 | 頁碼、辨識框、分數、渲染像素尺寸 |
| PNG、JPG、TIFF、WebP、BMP | 處理 EXIF 方向後進行本機 OCR；多頁 TIFF 逐頁處理 | 圖片頁次、辨識框、分數 |
| DWG | 固定版本 LibreDWG 轉 DXF，再以 ezdxf 讀取 | 檔名、配置空間、圖層、圖元 handle、圖塊路徑 |
| DXF | 讀 TEXT、MTEXT、ATTRIB、常數 ATTDEF、DIMENSION 與巢狀圖塊 | 同 DWG，並保留圖面單位及尺寸量測種類 |

限制：每檔 50 MB；PDF／TIFF 最多 100 頁；圖片 2,500 萬像素；每檔擷取文字 150,000 字；CAD 最多遍歷 100,000 個圖元；匯入庫最多 4,000 段，含種子庫總計最多 5,000 段。加密 PDF 請先另存未加密副本。空白檔不入庫，部分頁面 OCR 失敗會明示「部分匯入」。純向量輪廓文字、外部參照、特殊 CAD 物件可能無法擷取；不讀取 DWG 外部參照檔案。

OCR 分數只反映辨識器的文字信心，並非建築正確率。匯入只取得文字及標註，不會還原房間幾何、辨認牆線拓撲或認證尺寸／法規。CAD 尺寸保留原始量測值與單位，不套用標註樣式倍率；圖塊尺寸註明使用圖塊定義座標。原始案例需求不會自動變成本案需求，所有匯入文件限定 `townhouse`／`graph`。

原檔保持不變。副本、擷取文字、逐頁／圖元證據及 SHA-256 存在 `output/rag/imports/import-<hash>/`，此目錄不進 Git。相同內容與格式重複匯入會沿用第一次的來源名稱，不新增副本；檔案內容變動會成為另一份來源。若需要移除資料或重新辨識部分失敗的檔案，先移出對應的 `import-<hash>` 資料夾，再執行 `index` 或重新匯入。原生 PDF 文字、OCR 與向量計算在本機執行；檢索命中的文字仍會依原生成流程送給 Gemini。

DWG 安裝命令只支援 Windows x64：從 [GNU LibreDWG 官方發行頁](https://github.com/LibreDWG/libredwg/releases/tag/0.14) 下載 0.14，驗證固定 SHA-256 後保留完整工具包與授權檔於 `output/rag/tools/`；匯入時不下載工具。其他平台可安裝 `dwg2dxf` 並設定 `RAG_DWG2DXF` 完整路徑。亦可指定 `RAG_ACCORECONSOLE` 使用已授權的 AutoCAD Core Console；轉檔超時或失敗時會回報原因，不把 DWG 當成文字硬讀。LibreDWG 對特殊物件的限制見[官方說明](https://github.com/LibreDWG/libredwg)。

`POST /api/rag/import?filename=...` 接受原始檔案位元組（`application/octet-stream`），設定通行碼時透過 `X-Access-Code` 傳入。伺服器先驗證通行碼，再串流限制檔案大小。回應分開列出 `file.status`（擷取）和 `index.status`（向量索引）；索引不可用時仍保存已完成的擷取，可在模型修復後執行 `index`。

## 沒有檔案時，自動搜尋網路資料

```powershell
.venv\Scripts\python.exe -m pip install -r requirements-web.txt
.venv\Scripts\python.exe -m src.knowledge research "透天住宅 採光 平面圖" --limit 3
```

網頁：展開「匯入參考資料」，在「沒有檔案？讓系統上網找」輸入主題，按「自動找資料並加入 RAG」。預設最多取三個不同網站；展開各筆結果可看原始網址、讀到的正文及索引狀態。搜尋使用 DDGS 的公開搜尋服務，不需要額外搜尋 API 金鑰，但服務可能限流或暫時不可用。

流程是「搜尋網址 → 讀取來源正文 → 主題關鍵詞初步篩選 → 保存來源及文字 → 本機向量索引」。搜尋摘要不入庫，不使用 LLM 補寫讀不到的內容。搜尋結果中的政府／學校網站優先，但排序不代表內容已核實；新增資料均標示尚待人工核對。每次只送出搜尋欄位的主題；不會上傳本機檔案，也不會在生成平面圖時自動上網。

目前支援公開 HTML 網頁與有文字層的 PDF，每份最多下載 8 MB、保留 12,000 字元，網路 PDF 最多 30 頁。網路圖片、DWG、掃描 PDF 請下載後走本機檔案匯入。需要登入、機器驗證、禁止擷取、無法讀到足夠正文的來源會跳過並顯示原因；只取得一兩份可用資料時仍能入庫。遵守 robots.txt，並略過標示 noindex／noarchive／noai 的來源。

擷取結果存於 `output/rag/imports/web-<hash>/`：`document.md` 保存來源網址與文字；`manifest.json` 保存搜尋主題、擷取日期、原文作者／發布日期／授權資訊（來源有提供時）、逐頁證據。URL 與內容相同不重複新增。移除資料時，先移出該份 `web-<hash>` 目錄，再執行 `index`。這些參考只用於 `townhouse`／`graph`，不進需求解析；幾何、尺寸與法規仍須原引擎檢核。

`POST /api/rag/research` 接受 JSON `{"query":"住宅採光", "limit":3, "code":""}`，`limit` 為 1～5。先驗證通行碼，再連網。`ready` 表示索引完成；`stored` 表示文字已保存但索引尚未就緒；`no_match`／`unavailable` 會說明原因。連線限制公開 HTTP／HTTPS 位址，每次重新導向重新驗證，拒絕內網／本機位址並驗證 TLS 憑證。

## 設定與診斷

| 設定或端點 | 用途 |
|---|---|
| `RAG_ENABLED=0` | 關閉檢索（預設開啟） |
| `RAG_WEB_ENABLED=0` | 關閉網路搜尋與網路入庫；不影響既有本機資料檢索 |
| `RAG_KNOWLEDGE_DIR` | 覆寫知識資料夾，預設專案 `knowledge/` |
| `RAG_DATA_DIR` | 覆寫模型與索引目錄，預設 `output/rag/`；不進 Git |
| `GET /api/rag/status` | 檢查模型／索引狀態與文件數 |
| `POST /api/rag/search` | 傳入 `query`、`stage`、`top_k`；設定 `ACCESS_CODE` 時須帶 `code` |

沒有命中為 `no_match`；缺模型、依賴或資料錯誤為 `unavailable`，畫面會說明已沿用原有生成流程。幾何檢核仍然執行。`DEMO_MODE=1` 使用錄好的模型回覆，即使真實檢索成功，也不能用來證明 RAG 改善了設計品質。

自動測試預設關閉外部向量模型，RAG 專用測試注入向量器驗證索引和呼叫契約；另可用上述 CLI 進行真實模型檢索驗證。

實作入口：`src/knowledge/rag.py`；網路搜尋：`src/knowledge/web_research.py`；管理命令：`src/knowledge/__main__.py`。網路測試見 `tests/test_web_research.py`，測試使用假搜尋結果，不依賴外網。
