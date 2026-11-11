# 網頁版部署容器(E1)—— 雲端主機(Render / Railway / Fly.io)吃這個檔案
# 就能把整個生產線跑起來。本機開發不需要 Docker,直接 uvicorn 即可。
FROM python:3.12-slim

WORKDIR /app

# SVG/PDF render Chinese labels to vector paths; install a real CJK font.
RUN apt-get update && apt-get install -y --no-install-recommends fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*
ENV CAD_PREVIEW_FONT=NotoSansCJK-Regular.ttc

# 先只複製套件清單再安裝——之後改程式碼不用重新下載套件(Docker 快取層)
COPY requirements.txt requirements-web.txt requirements-rag-onnx.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-web.txt -r requirements-rag-onnx.txt
RUN python -c "from ezdxf.fonts import fonts; assert fonts.font_manager.has_font('NotoSansCJK-Regular.ttc'); cmap=fonts.font_manager.get_ttf_font('NotoSansCJK-Regular.ttc').getBestCmap(); assert all(ord(c) in cmap for c in '樓梯間主臥室浴廁尺寸')"
# 建置時直接確認搜尋、網頁與文字 PDF 擷取可用，避免部署後才發現漏裝。
RUN python -c "from ddgs import DDGS; from lxml import html; import urllib3, certifi, pymupdf"

COPY config ./config
COPY src ./src
COPY knowledge ./knowledge

# Download only at image build time. The fixed model is verified before indexing.
ENV RAG_BACKEND=onnx RAG_MODEL_DIR=/opt/rag/model TOKENIZERS_PARALLELISM=false
RUN python -m src.knowledge prepare-onnx

# 雲端平台會用 PORT 環境變數告訴我們該聽哪個埠;本機預設 8000
ENV PORT=8000
CMD ["sh", "-c", "uvicorn src.web.app:app --host 0.0.0.0 --port ${PORT}"]
