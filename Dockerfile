# 網頁版部署容器(E1)—— 雲端主機(Render / Railway / Fly.io)吃這個檔案
# 就能把整個生產線跑起來。本機開發不需要 Docker,直接 uvicorn 即可。
FROM python:3.12-slim

WORKDIR /app

# 先只複製套件清單再安裝——之後改程式碼不用重新下載套件(Docker 快取層)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY config ./config
COPY src ./src

# 雲端平台會用 PORT 環境變數告訴我們該聽哪個埠;本機預設 8000
ENV PORT=8000
CMD ["sh", "-c", "uvicorn src.web.app:app --host 0.0.0.0 --port ${PORT}"]
