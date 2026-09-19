# syntax=docker/dockerfile:1
# ShopFlow orders service image (contracts.md §5/§8/§9/§11).
#
# shopflow-infra uses this one image two ways, both from this repository:
#   * `migrate-orders`   - `alembic upgrade head` (this repo owns its schema);
#   * `shopflow-orders`  - the HTTP service plus the two background relays
#     (outbox publisher and release reconciler) inside the same process.
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PORT=8000

WORKDIR /app

RUN groupadd --system orders \
    && useradd --system --gid orders --create-home --home-dir /home/orders orders

# Runtime dependencies only (fully pinned lockfile).
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY alembic.ini ./
COPY app ./app
COPY migrations ./migrations

USER orders

EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2).status == 200 else 1)"

CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --log-level info"]
