# ─────────────────────────────────────────────
# 阶段 1：用 Node 构建前端（Vue3 + Vite）
# ─────────────────────────────────────────────
FROM node:20-slim AS frontend-builder
WORKDIR /frontend

# 安装前端依赖（利用缓存层）
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm install -i https://registry.npmmirror.com || npm install

# 构建前端到 dist/
COPY frontend/ ./
RUN npm run build

# ─────────────────────────────────────────────
# 阶段 2：Python 运行后端（FastAPI）+ 托管前端 dist
# ─────────────────────────────────────────────
FROM python:3.11-slim AS base

# 使用国内镜像源加速
RUN sed -i 's|deb.debian.org|mirrors.aliyun.com|g' /etc/apt/sources.list.d/debian.sources 2>/dev/null || \
    sed -i 's|deb.debian.org|mirrors.aliyun.com|g' /etc/apt/sources.list 2>/dev/null || true

# 系统依赖：Chromium（Selenium 无头浏览器）+ 中文字体
RUN apt-get update && apt-get install -y --no-install-recommends \
    chromium \
    chromium-driver \
    fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

# 告诉 selenium / webdriver 使用系统 Chromium
ENV CHROME_BIN=/usr/bin/chromium
ENV CHROMEDRIVER_PATH=/usr/bin/chromedriver
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# 先拷贝依赖文件，利用 Docker 缓存层
COPY requirements.txt .
RUN pip install --no-cache-dir -i https://mirrors.aliyun.com/pypi/simple/ --trusted-host mirrors.aliyun.com -r requirements.txt

# 拷贝后端代码
COPY app.py ./
COPY api/ ./api/
COPY core/ ./core/
COPY db/ ./db/

# 拷贝前端构建产物（由阶段 1 生成）
COPY --from=frontend-builder /frontend/dist ./frontend/dist

# 拷贝浏览器插件目录（供 /api/files/extension-zip 打包下载）
COPY extension/ ./extension/

# 创建下载 / 数据目录
RUN mkdir -p /app/downloads /app/data

# 数据目录环境变量（SQLite 持久化 + 分区下载根）
ENV DATA_DIR=/app/data
ENV DOWNLOAD_DIR=/app/downloads

EXPOSE 6500

# 数据持久化：下载文件 + 配置数据库
VOLUME ["/app/downloads", "/app/data"]

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "6500"]
