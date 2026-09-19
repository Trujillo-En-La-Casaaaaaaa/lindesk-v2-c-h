# syntax=docker/dockerfile:1
#
# ShopFlow broker bootstrap job (contracts.md §8).
# Declares and verifies the durable exchange/queue/bindings, then exits.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /broker

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY bootstrap.py ./

RUN useradd --system --create-home --home-dir /home/broker broker \
    && chown -R broker:broker /broker
USER broker

CMD ["python", "bootstrap.py"]
