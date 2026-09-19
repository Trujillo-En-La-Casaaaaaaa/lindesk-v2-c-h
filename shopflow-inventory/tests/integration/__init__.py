"""Integration suite of shopflow-inventory: the real store, no doubles.

The suite runs against the inventory database of ``shopflow-infra``
(``docker compose -f ../shopflow-infra/docker-compose.yml up -d inventory-db``).
It applies this repository's own migration exactly the way infrastructure does
(``alembic upgrade head`` with ``DATABASE_URL``), then exercises the service over
HTTP - including concurrency, restart and failure paths - against PostgreSQL.

Every test starts from the deterministic seed of contracts.md §10 with no
recorded movements, so the assertions are reproducible.
"""
