# mindex — runs the knowledge-base server in a container.
# Your conversation data is NOT copied in; it is mounted read-only at runtime
# (see docker-compose.yml). Only the derived index lives in the data volume.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    MINDEX_HOST=0.0.0.0 \
    MINDEX_DATA_DIR=/app/data \
    FASTEMBED_CACHE_PATH=/app/data/.fastembed \
    HF_HOME=/app/data/.hf

WORKDIR /app

# Install the package (with transformer embeddings) — all deps ship as wheels.
COPY pyproject.toml README.md ./
COPY mindex ./mindex
RUN pip install ".[semantic,pdf]"

# Run as a non-root user.
RUN useradd -m -u 1000 mindex && mkdir -p /app/data && chown -R mindex:mindex /app
USER mindex

VOLUME ["/app/data"]
EXPOSE 8765

# Lightweight healthcheck against the stats API.
HEALTHCHECK --interval=30s --timeout=4s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8765/api/status').status==200 else 1)"

CMD ["python", "-m", "mindex"]
