"""Integration tests of ``/ready`` against the real store and the real broker."""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from app.adapters.rabbitmq_publisher import RabbitMqPublisher
from app.config import BROKER_EXCHANGE, Settings
from app.main import create_app
from tests.support.fakes import FakeBroker, FakeInventory
from tests.support.servers import unused_port

UNREACHABLE_BROKER = "amqp://shopflow:shopflow@127.0.0.1:1/"


def test_ready_reports_the_real_store_and_the_real_broker(
    app_factory, settings: Settings, broker_url: str
) -> None:
    broker = RabbitMqPublisher(broker_url, exchange=BROKER_EXCHANGE, connect_timeout_seconds=5.0)
    try:
        client = TestClient(app_factory(broker_override=broker))
        response = client.get("/ready")
    finally:
        broker.close()

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "ready"
    assert body["upstreams"] == {"store": True, "broker": True, "inventory": True}
    assert client.get("/health").json() == {"status": "ok"}


def test_ready_is_503_when_the_broker_is_unreachable(app_factory) -> None:
    broker = RabbitMqPublisher(UNREACHABLE_BROKER, exchange=BROKER_EXCHANGE, connect_timeout_seconds=2.0)
    try:
        client = TestClient(app_factory(broker_override=broker))
        response = client.get("/ready")
    finally:
        broker.close()

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    assert body["upstreams"]["broker"] is False
    assert body["upstreams"]["store"] is True


def test_ready_is_503_when_the_store_does_not_answer(settings: Settings, inventory_url: str) -> None:
    broken_engine = create_engine(
        f"postgresql://shopflow:shopflow@127.0.0.1:{unused_port()}/shopflow_orders",
        pool_pre_ping=True,
        connect_args={"connect_timeout": 1},
    )
    app = create_app(
        settings=settings,
        engine=broken_engine,
        inventory=FakeInventory(),
        broker=FakeBroker(),
        start_workers=False,
    )
    try:
        client = TestClient(app)
        response = client.get("/ready")
        assert response.status_code == 503
        body = response.json()
        assert body["status"] == "not_ready"
        assert body["upstreams"]["store"] is False
    finally:
        broken_engine.dispose()


def test_ready_reports_an_unreachable_inventory_without_gating(app_factory) -> None:
    client = TestClient(app_factory(inventory_base_url=f"http://127.0.0.1:{unused_port()}"))

    response = client.get("/ready")

    assert response.status_code == 200
    assert response.json()["upstreams"]["inventory"] is False


def test_health_never_contacts_a_dependency(app_factory) -> None:
    broker = RabbitMqPublisher(UNREACHABLE_BROKER, exchange=BROKER_EXCHANGE, connect_timeout_seconds=2.0)
    try:
        client = TestClient(app_factory(broker_override=broker, inventory_base_url="http://127.0.0.1:1"))
        assert client.get("/health").json() == {"status": "ok"}
    finally:
        broker.close()


def test_the_ready_payload_shape_is_stable(app_factory, settings: Settings) -> None:
    """``/ready`` always answers ``{status, upstreams}`` with the three named upstreams."""

    client = TestClient(app_factory())
    body = client.get("/ready").json()

    assert set(body) == {"status", "upstreams"}
    assert set(body["upstreams"]) == {"store", "broker", "inventory"}
    assert all(isinstance(value, bool) for value in body["upstreams"].values())
    assert body["status"] in {"ready", "not_ready"}
    assert settings.database_url  # the settings of the suite point at a real store
