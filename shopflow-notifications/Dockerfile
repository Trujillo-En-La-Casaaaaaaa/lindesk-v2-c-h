# syntax=docker/dockerfile:1
# ShopFlow notifications service image (contracts.md §6/§7/§8/§9/§11).
#
# The same image runs three ways inside shopflow-infra, all from this repository:
#   * `shopflow-notifications` - the HTTP service and the order-event consumer in
#     one process (the default CMD below; the consumer starts with the app);
#   * `migrate-notifications` - `alembic upgrade head` (this repo owns its schema);
#   * `python -m app.consumer.consumer` - the consumer on its own, should an
#     operator prefer a dedicated consumer process.
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PORT=8000

WORKDIR /app

RUN groupadd --system notifications \
    && useradd --system --gid notifications --create-home --home-dir /home/notifications notifications

# Runtime dependencies only (fully pinned lockfile).
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY alembic.ini ./
COPY app ./app
COPY migrations ./migrations

USER notifications

EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2).status == 200 else 1)"

CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --log-level info"]
