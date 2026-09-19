# syntax=docker/dockerfile:1
#
# One-shot container image for the shopflow-infra scripts (health gate, smoke test).
# The scripts themselves only need the standard library; httpx is installed from
# the pinned lockfile so the container path matches a workstation that has it.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /infra-scripts

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN useradd --system --create-home --home-dir /home/infra infra \
    && chown -R infra:infra /infra-scripts
USER infra

CMD ["python", "wait_for_health.py", "--help"]
