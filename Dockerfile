# Instagram 翻譯播放 — 容器部署（需外層平台注入 PORT 時仍綁定 0.0.0.0）
FROM python:3.12-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    ca-certificates \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir yt-dlp

COPY translate_instagram_video.py dub_video_chinese.py web_server.py ./
COPY static ./static/

# 雲端平台常見：只設 PORT；對外監聽需 0.0.0.0
ENV HOST=0.0.0.0
ENV PORT=8765
EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
    CMD sh -c 'curl -fsS "http://127.0.0.1:$${PORT:-8765}/health" || exit 1'

CMD ["python", "web_server.py"]
