# 使用官方 Python 运行时作为基础镜像
FROM python:3.11-slim

WORKDIR /app

# 先复制依赖清单，便于利用 Docker 构建缓存
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

EXPOSE 8000

ENV API_KEY="" \
    LOG_LEVEL="INFO" \
    REQUEST_TIMEOUT="30" \
    LISTEN_HOST="0.0.0.0" \
    LISTEN_PORT="8000"

# 使用 Python 标准库进行健康检查，无需额外安装 curl
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).read()" || exit 1

CMD ["python", "app.py"]
