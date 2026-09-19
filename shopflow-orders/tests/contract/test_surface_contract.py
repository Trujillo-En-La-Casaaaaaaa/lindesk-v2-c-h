"""Contract test of the HTTP surface itself (contracts.md §5)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.contract.conftest import route_surface

#: Every route this service owns, from the table of contracts.md §5.
EXPECTED_SURFACE = sorted(
    [
        ("POST", "/orders"),
        ("GET", "/orders"),
        ("GET", "/orders/{orderId}"),
        ("POST", "/orders/{orderId}/cancel"),
        ("POST", "/orders/{orderId}/ship"),
        ("GET", "/health"),
        ("GET", "/ready"),
    ]
)


def test_the_surface_is_exactly_the_documented_one(app) -> None:
    assert route_surface(app) == EXPECTED_SURFACE


def test_the_surface_exposes_no_administrative_role_check() -> None:
    """``X-ShopFlow-Role`` is enforced by the gateway; this service only documents §5."""

    assert ("POST", "/orders/{orderId}/ship") in EXPECTED_SURFACE


def test_an_unknown_route_is_not_found_in_the_shared_envelope(client: TestClient) -> None:
    response = client.get("/nope", headers={"X-Correlation-Id": "corr-404"})
    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "not_found"
    assert body["error"]["correlationId"] == "corr-404"


def test_a_wrong_method_is_reported_as_a_client_error(client: TestClient) -> None:
    response = client.put("/orders", json={})
    assert response.status_code in {400, 405}
    assert "error" in response.json()


def test_the_health_routes_are_available_without_a_store(app, client: TestClient) -> None:
    paths = {path for _method, path in route_surface(app)}
    assert {"/health", "/ready"}.issubset(paths)
    assert client.get("/health").status_code == 200
