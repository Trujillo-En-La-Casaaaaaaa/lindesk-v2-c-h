"""Contract tests of the health routes (contracts.md §2/§6, handoff #7)."""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_health_is_liveness_only(ready_client: TestClient) -> None:
    response = ready_client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_ready_reports_the_three_reachability_checks(ready_client: TestClient) -> None:
    response = ready_client.get("/ready")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["upstreams"] == {"store": True, "broker": True, "provider": True}
    # the consumer exists but is not started in this process (the composed stack runs
    # it inside the API process; a dedicated consumer process reports the same value)
    assert body["consumer"] == "separate-process"
    assert response.headers["content-type"].startswith("application/json")


def test_ready_reports_the_consumer_as_disabled_when_it_is_switched_off(repository, provider) -> None:
    from app.main import create_app
    from tests.support.fakes import RecordingProbe

    app = create_app(
        repository=repository,
        provider=provider,
        settings=repository_settings(consumer_enabled=False),
        readiness={
            "store": RecordingProbe(reachable=True),
            "broker": RecordingProbe(reachable=True),
            "provider": RecordingProbe(reachable=True),
        },
        start_consumer=False,
    )
    with TestClient(app) as client:
        body = client.get("/ready").json()

    assert body["consumer"] == "disabled"


def repository_settings(**overrides):
    from tests.support.settings import make_settings

    return make_settings(**overrides)


def test_ready_answers_503_when_an_upstream_is_unreachable(unready_client: TestClient) -> None:
    response = unready_client.get("/ready")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    assert body["upstreams"]["store"] is False
    assert body["upstreams"]["broker"] is True


def test_ready_never_raises_when_a_probe_explodes(repository, provider) -> None:
    from app.main import create_app

    def exploding() -> bool:
        raise RuntimeError("probe blew up")

    app = create_app(
        repository=repository,
        provider=provider,
        readiness={"store": exploding, "broker": exploding, "provider": exploding},
        start_consumer=False,
    )
    with TestClient(app) as client:
        response = client.get("/ready")

    assert response.status_code == 503
    assert response.json()["upstreams"] == {"store": False, "broker": False, "provider": False}
