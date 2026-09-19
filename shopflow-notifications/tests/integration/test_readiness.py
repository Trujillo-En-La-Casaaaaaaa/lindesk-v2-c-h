"""Integration tests of the readiness report against the real dependencies.

``/health`` is liveness only; ``/ready`` must answer ``200`` exactly when the store,
the broker and the provider answer, and ``503`` as soon as one of them does not
(handoff #7).
"""

from __future__ import annotations

from sqlalchemy import create_engine

from app.config import Settings
from app.db.session import database_is_reachable
from tests.support.servers import free_port


def test_ready_is_green_against_the_real_dependencies(client_factory) -> None:
    with client_factory() as api:
        response = api.get("/ready")

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ready"
        assert body["upstreams"] == {"store": True, "broker": True, "provider": True}
        assert body["consumer"] == "running"  # the lifespan started the consumer


def test_health_is_liveness_only_even_without_dependencies(client_factory) -> None:
    with client_factory() as api:
        assert api.get("/health").json() == {"status": "ok"}


def test_ready_is_red_when_the_provider_is_unreachable(client_factory, case_id: str) -> None:
    """A provider outage must be visible on ``/ready`` - and must not be reported as green."""

    with client_factory(provider_base_url=f"http://127.0.0.1:{free_port()}", start_consumer=False) as api:
        response = api.get("/ready")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    assert body["upstreams"]["provider"] is False
    assert body["upstreams"]["store"] is True
    assert body["upstreams"]["broker"] is True


def test_the_store_probe_really_queries_the_store(settings: Settings, engine) -> None:
    """The probe must report what the store answers, not what the configuration says."""

    broken = create_engine(f"postgresql://shopflow:shopflow@127.0.0.1:{free_port()}/shopflow_notifications")
    try:
        assert database_is_reachable(broken) is False
    finally:
        broken.dispose()

    assert database_is_reachable(engine) is True
