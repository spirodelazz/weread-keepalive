# weread-keepalive 容器镜像
# 基于 python:3.12-slim 在构建期安装 Playwright + Chromium（版本始终与 pip 包一致，
# 避免官方预装镜像与 requirements 版本漂移导致的浏览器找不到问题）。
# 支持 amd64 / arm64（NAS 常见 arm64）。
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# 依赖层单独缓存：requirements 不变时浏览器不用重新下载
COPY requirements.txt ./
RUN pip install -r requirements.txt \
    && playwright install --with-deps chromium

COPY . .

# wx.lic / Chrome profile / 日志都放这里，与 we-mp-rss 容器挂载同一目录
VOLUME ["/app/data"]

CMD ["python", "start_daemon.py"]
