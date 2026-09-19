#!/usr/bin/env sh
#
# ShopFlow local stack bootstrap (POSIX shell wrapper).
#
#   ./up.sh              build, start, gate on health, seed, smoke test
#   ./up.sh --reset      also drop all volumes first (deterministic restart)
#   ./up.sh --no-smoke   skip the end-to-end smoke test
#
# Equivalent manual commands are documented in README.md and docs/runbook.md.
set -eu

cd "$(dirname "$0")"

PYTHON="${PYTHON:-python3}"
TIMEOUT="${HEALTH_GATE_TIMEOUT:-180}"
RESET="no"
SMOKE="yes"

usage() {
  cat <<'EOF'
ShopFlow local stack bootstrap (POSIX shell wrapper).

  ./up.sh              build, start, gate on health, seed, then smoke test
  ./up.sh --reset      also drop all volumes first (deterministic restart)
  ./up.sh --no-smoke   stop after the seed, skipping the smoke test
  ./up.sh --help       show this message

Environment: PYTHON (default python3), HEALTH_GATE_TIMEOUT (default 180).
Equivalent manual commands are documented in README.md and docs/runbook.md.
EOF
}

for argument in "$@"; do
  case "$argument" in
    --reset) RESET="yes" ;;
    --no-smoke) SMOKE="no" ;;
    -h|--help) usage; exit 0 ;;
    *)
      echo "up.sh: unknown argument '$argument'" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [ "$RESET" = "yes" ]; then
  echo "==> docker compose down -v (dropping all state)"
  docker compose down -v
fi

echo "==> docker compose up -d --build"
docker compose up -d --build

echo "==> waiting for every /health endpoint (timeout ${TIMEOUT}s)"
"$PYTHON" scripts/wait_for_health.py --timeout "$TIMEOUT"

echo "==> inventory-owned deterministic seed"
"$PYTHON" scripts/seed.py

if [ "$SMOKE" = "yes" ]; then
  echo "==> end-to-end smoke test through the gateway"
  "$PYTHON" scripts/smoke_test.py
fi

echo "==> stack is ready"
echo "    web      http://localhost:${WEB_PORT:-3000}"
echo "    gateway  http://localhost:${GATEWAY_PORT:-8080}/api/products"
