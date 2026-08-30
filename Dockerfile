FROM python:3.12-slim

LABEL org.opencontainers.image.title="mini-kb-agent" \
      org.opencontainers.image.description="Lightweight multimodal knowledge QA without embeddings or a vector database" \
      org.opencontainers.image.source="https://github.com/saitomikuya/mini-kb-agent" \
      org.opencontainers.image.version="0.8.2"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    DATA_DIR=/app/data \
    SOURCE_DIR=/app/sources \
    TZ=UTC \
    SESSION_MAX_AGE=604800

WORKDIR /app

COPY pyproject.toml README.md ./
COPY app ./app
COPY prompts ./prompts
COPY migrations ./migrations
COPY alembic.ini supervisord.conf entrypoint.sh ./

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        fonts-noto-cjk \
        libreoffice-writer \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir --timeout 120 --retries 10 . \
    && chmod +x /app/entrypoint.sh \
    && mkdir -p \
        /app/sources \
        /app/data/md \
        /app/data/index \
        /app/data/tmp \
        /app/data/logs

VOLUME ["/app/sources", "/app/data"]

EXPOSE 8080

STOPSIGNAL SIGTERM

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import json,sys,urllib.request; response=urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=3); payload=json.load(response); sys.exit(0 if response.status == 200 and payload == {'status': 'ok'} else 1)"

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["supervisord", "-c", "/app/supervisord.conf"]
