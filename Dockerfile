FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    MINDEX_HOST=0.0.0.0 \
    MINDEX_DATA_DIR=/app/data \
    FASTEMBED_CACHE_PATH=/app/data/.fastembed \
    HF_HOME=/app/data/.hf

WORKDIR /app

COPY pyproject.toml README.md ./
COPY mindex ./mindex
RUN pip install ".[semantic,pdf]"

RUN useradd -m -u 1000 mindex && mkdir -p /app/data && chown -R mindex:mindex /app
USER mindex

VOLUME ["/app/data"]
EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=4s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8765/api/status').status==200 else 1)"

CMD ["python", "-m", "mindex"]
