FROM python:3.12.10-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    MEMORY_GATEWAY_REPOSITORY_ROOT=/app

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
COPY schema ./schema
RUN chmod -R a+rX /app/schema \
    && python -m pip install --no-cache-dir ".[postgres]"

COPY deploy/fn/entrypoint.sh /usr/local/bin/memory-gateway-entrypoint
RUN chmod 0555 /usr/local/bin/memory-gateway-entrypoint

ENTRYPOINT ["/usr/local/bin/memory-gateway-entrypoint"]
