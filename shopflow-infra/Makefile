# ShopFlow local stack - thin wrappers around docker compose and the infra scripts.
#
#   make up       build images and bring the whole stack up in dependency order
#   make health   wait until every /health endpoint answers
#   make migrate  apply each service's own migrations (inventory -> orders -> notifications)
#   make seed     run the inventory-owned deterministic seed and verify idempotency
#   make smoke    black-box end-to-end verification through the public gateway
#   make test     emulator contract tests + infra script unit tests
#   make reset    tear everything down including volumes (back to the deterministic seed)

SHELL := /bin/sh
COMPOSE ?= docker compose
PYTHON ?= python3
COMPOSE_FILE ?= docker-compose.yml

.PHONY: help build up down stop reset restart migrate seed smoke smoke-in-stack health test logs ps

help:
	@echo "ShopFlow local stack targets:"
	@echo "  make help           show this message"
	@echo "  make build          build every image (services, emulator, infra scripts)"
	@echo "  make up             docker compose up -d --build"
	@echo "  make health         wait for every /health endpoint (scripts/wait_for_health.py)"
	@echo "  make migrate        run each service's own migrations (scripts/run_migrations.py)"
	@echo "  make seed           run + verify the inventory seed (scripts/seed.py)"
	@echo "  make smoke          end-to-end smoke test through the gateway (scripts/smoke_test.py)"
	@echo "  make smoke-in-stack run the smoke test as a one-shot container inside the network"
	@echo "  make test           emulator contract + script unit tests (pytest)"
	@echo "  make stop/down      stop / remove containers (volumes kept)"
	@echo "  make restart        restart the running containers"
	@echo "  make reset          docker compose down -v (removes all state)"
	@echo "  make logs/ps        tail logs / show container status"

build:
	$(COMPOSE) -f $(COMPOSE_FILE) build

up:
	$(COMPOSE) -f $(COMPOSE_FILE) up -d --build

down:
	$(COMPOSE) -f $(COMPOSE_FILE) down

stop:
	$(COMPOSE) -f $(COMPOSE_FILE) stop

# Removes containers and volumes: the next run starts from the deterministic seed.
reset:
	$(COMPOSE) -f $(COMPOSE_FILE) down -v

restart:
	$(COMPOSE) -f $(COMPOSE_FILE) restart

migrate:
	$(PYTHON) scripts/run_migrations.py --compose-file $(COMPOSE_FILE)

seed:
	$(PYTHON) scripts/seed.py --compose-file $(COMPOSE_FILE)

smoke:
	$(PYTHON) scripts/smoke_test.py

smoke-in-stack:
	$(COMPOSE) -f $(COMPOSE_FILE) run --rm smoke-test

health:
	$(PYTHON) scripts/wait_for_health.py --timeout 180

test:
	$(PYTHON) -m pytest emulator/tests scripts/tests -q

logs:
	$(COMPOSE) -f $(COMPOSE_FILE) logs -f --tail=100

ps:
	$(COMPOSE) -f $(COMPOSE_FILE) ps
