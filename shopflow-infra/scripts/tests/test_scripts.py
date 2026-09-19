"""Unit tests for the infra scripts' own logic.

These tests never start docker and never contact a service: they cover the parts
of the scripts that can be verified in isolation — the contract constants and
formulas, the seed verifier's catalogue checks, the polling/timeout control flow
of the health gate and of the smoke test, and the migration job order. The
end-to-end behaviour is proven by ``scripts/smoke_test.py`` against a running
stack (see README §7).
"""

from __future__ import annotations

import hashlib
from decimal import Decimal

import pytest

import run_migrations
import seed
import shopflow_compose as compose
import shopflow_http as http
import smoke_test
import wait_for_health


# --------------------------------------------------------------------------- #
# contracts.md §7 / §10 constants shared by the scripts
# --------------------------------------------------------------------------- #
def test_smoke_message_id_matches_the_contract_formula() -> None:
    order = "018f2b6e-0000-4000-8000-000000000000"
    expected = "msg-" + hashlib.sha256(f"order_confirmation:{order}".encode("utf-8")).hexdigest()[:16]

    assert smoke_test.deterministic_message_id("order_confirmation", order) == expected
    assert smoke_test.deterministic_message_id("order_confirmation", order).startswith("msg-")
    assert len(smoke_test.deterministic_message_id("order_cancellation", order)) == 4 + 16


def test_documented_products_match_contract_section_10() -> None:
    assert [(p.product_id, p.sku, p.name, p.price) for p in smoke_test.DOCUMENTED_PRODUCTS] == [
        ("p-1001", "SF-TEE-001", "ShopFlow Classic Tee", Decimal("19.99")),
        ("p-1002", "SF-MUG-002", "ShopFlow Ceramic Mug", Decimal("12.50")),
        ("p-1003", "SF-HOOD-003", "ShopFlow Hoodie", Decimal("49.00")),
        ("p-1004", "SF-STICK-004", "ShopFlow Sticker Pack", Decimal("4.25")),
        ("p-1005", "SF-CAP-005", "ShopFlow Cap", Decimal("22.00")),
    ]
    assert [p.seeded_stock for p in seed.SEEDED_PRODUCTS] == [50, 25, 10, 100, 0]


def test_smoke_and_seed_agree_on_the_seeded_catalogue() -> None:
    smoke_rows = [(p.product_id, p.sku, p.name, p.price) for p in smoke_test.DOCUMENTED_PRODUCTS]
    seed_rows = [(p.product_id, p.sku, p.name, p.price) for p in seed.SEEDED_PRODUCTS]

    assert smoke_rows == seed_rows


# --------------------------------------------------------------------------- #
# smoke test: scenario coverage and polling control flow
# --------------------------------------------------------------------------- #
def test_smoke_scenarios_cover_every_required_case() -> None:
    names = [name for _, name, _ in smoke_test.SCENARIOS]

    assert names == [
        "list-products-through-gateway",
        "create-order-confirms",
        "stock-decremented-after-order",
        "exactly-one-confirmation-message",
        "cancel-order-with-reason",
        "stock-restored-after-cancellation",
        "duplicate-cancellation-is-inert",
        "shipped-order-cannot-be-cancelled",
        "insufficient-stock-blocks-creation",
    ]
    assert [number for number, _, _ in smoke_test.SCENARIOS] == list(range(1, 10))


def test_every_declared_scenario_exists_on_the_runner() -> None:
    runner = smoke_test.SmokeTest(gateway_base_url="http://gateway.invalid", provider_base_url="http://provider.invalid")

    for _, _, method_name in smoke_test.SCENARIOS:
        assert callable(getattr(runner, method_name)), method_name


def _runner(**overrides) -> smoke_test.SmokeTest:
    return smoke_test.SmokeTest(
        gateway_base_url="http://gateway.invalid",
        provider_base_url="http://provider.invalid",
        sleep=lambda _seconds: None,
        **overrides,
    )


def test_wait_for_stock_returns_once_the_release_lands(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = _runner()
    observed = iter([47, 47, 50])
    monkeypatch.setattr(runner, "available", lambda product_id: next(observed))

    assert runner.wait_for_stock("p-1001", 50) == 50


def test_wait_for_stock_times_out_with_the_expected_and_observed_values(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = _runner(event_timeout=0)
    monkeypatch.setattr(runner, "available", lambda product_id: 47)

    with pytest.raises(smoke_test.SmokeFailure) as failure:
        runner.wait_for_stock("p-1001", 50)

    assert "47" in str(failure.value) and "50" in str(failure.value)


def test_wait_for_messages_recounts_after_the_settle_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """A duplicate that lands inside the settle window must be visible."""

    runner = _runner(settle_seconds=1.0, event_timeout=10)
    calls: list[int] = []
    screens = [
        [],  # first look: nothing yet
        [{"template": "order_confirmation", "messageId": "msg-1"}],  # delivery arrived
        [  # the settle recount observes a wrongly duplicated delivery
            {"template": "order_confirmation", "messageId": "msg-1"},
            {"template": "order_confirmation", "messageId": "msg-1"},
        ],
    ]

    def fake_provider_messages(order_id: str) -> list[dict]:
        calls.append(1)
        return screens[min(len(calls) - 1, len(screens) - 1)]

    monkeypatch.setattr(runner, "provider_messages", fake_provider_messages)

    assert len(runner.wait_for_messages("order-1", "order_confirmation")) == 2


def test_wait_for_messages_times_out_when_nothing_is_delivered(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = _runner(event_timeout=0)
    monkeypatch.setattr(runner, "provider_messages", lambda order_id: [])

    with pytest.raises(smoke_test.SmokeFailure) as failure:
        runner.wait_for_messages("order-1", "order_cancellation")

    assert "order_cancellation" in str(failure.value)


# --------------------------------------------------------------------------- #
# seed verifier (acceptance criterion 3)
# --------------------------------------------------------------------------- #
def _catalogue(**stock: int) -> dict[str, dict]:
    catalogue = {}
    for product in seed.SEEDED_PRODUCTS:
        quantity = stock.get(product.product_id, product.seeded_stock)
        catalogue[product.product_id] = {
            "productId": product.product_id,
            "sku": product.sku,
            "name": product.name,
            "price": float(product.price),
            "availableQuantity": quantity,
        }
    return catalogue


def test_check_documented_products_accepts_the_documented_catalogue() -> None:
    seed.check_documented_products(_catalogue())


def test_check_documented_products_accepts_stock_below_the_seed() -> None:
    seed.check_documented_products(_catalogue(**{"p-1001": 3}))


@pytest.mark.parametrize("product_id", ["p-1001", "p-1005"])
def test_check_documented_products_rejects_a_missing_product(product_id: str) -> None:
    catalogue = _catalogue()
    del catalogue[product_id]

    with pytest.raises(seed.VerificationError) as failure:
        seed.check_documented_products(catalogue)

    assert product_id in str(failure.value)


def test_check_documented_products_rejects_stock_above_the_seed() -> None:
    with pytest.raises(seed.VerificationError) as failure:
        seed.check_documented_products(_catalogue(**{"p-1005": 1}))

    assert "p-1005" in str(failure.value)


def test_check_documented_products_rejects_a_wrong_price_and_sku() -> None:
    catalogue = _catalogue()
    catalogue["p-1003"]["price"] = 48.0
    with pytest.raises(seed.VerificationError):
        seed.check_documented_products(catalogue)

    catalogue = _catalogue()
    catalogue["p-1002"]["sku"] = "SF-WRONG-002"
    with pytest.raises(seed.VerificationError):
        seed.check_documented_products(catalogue)


def test_check_documented_products_tolerates_extra_products() -> None:
    """Only the five documented rows are constrained (§10); extra rows are not."""

    catalogue = _catalogue()
    catalogue["p-9999"] = {"productId": "p-9999", "sku": "X", "name": "X", "price": 1.0, "availableQuantity": 1}

    seed.check_documented_products(catalogue)
    assert "p-9999" in seed.snapshot(catalogue)


# --------------------------------------------------------------------------- #
# health gate
# --------------------------------------------------------------------------- #
def test_parse_target_accepts_a_bare_url_and_a_name_form() -> None:
    bare = wait_for_health.parse_target("http://localhost:8081/health")
    named = wait_for_health.parse_target("gateway=http://shopflow-gateway:8000/health")

    assert bare.url == "http://localhost:8081/health"
    assert bare.name == "localhost:8081/health"
    assert (named.name, named.url) == ("gateway", "http://shopflow-gateway:8000/health")


@pytest.mark.parametrize("spec", ["", "   ", "no-url-here", "=http://x/health", "name="])
def test_parse_target_rejects_malformed_specs(spec: str) -> None:
    with pytest.raises(ValueError):
        wait_for_health.parse_target(spec)


def test_default_targets_follow_the_documented_dependency_order() -> None:
    names = [target.name for target in wait_for_health.default_targets()]

    assert names == [
        "shopflow-inventory",
        "shopflow-orders",
        "shopflow-notifications",
        "notification-provider-emulator",
        "shopflow-gateway",
    ]


def test_wait_returns_zero_once_every_endpoint_is_healthy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wait_for_health, "probe", lambda target, request_timeout: (True, "HTTP 200 " + target.url))
    targets = [wait_for_health.Target("a", "http://a/health"), wait_for_health.Target("b", "http://b/health")]

    assert wait_for_health.wait(targets, timeout=5, interval=1, request_timeout=1, quiet=True) == 0


def test_wait_fails_and_names_the_unhealthy_endpoints(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        wait_for_health,
        "probe",
        lambda target, request_timeout: (target.name == "a", "unreachable"),
    )
    targets = [wait_for_health.Target("a", "http://a/health"), wait_for_health.Target("b", "http://b/health")]

    ticks = iter(range(1000))
    exit_code = wait_for_health.wait(
        targets,
        timeout=3,
        interval=1,
        request_timeout=1,
        quiet=True,
        sleep=lambda _seconds: None,
        clock=lambda: float(next(ticks)),
    )

    assert exit_code == 1


def test_wait_rejects_an_empty_target_list(capsys: pytest.CaptureFixture[str]) -> None:
    assert wait_for_health.wait([], timeout=1, interval=1, request_timeout=1) == 2
    assert "no endpoints" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# migration runner and compose wrapper
# --------------------------------------------------------------------------- #
def test_migrations_run_in_the_documented_dependency_order() -> None:
    assert [job.name for job in run_migrations.JOBS] == ["inventory", "orders", "notifications"]
    assert [job.service for job in run_migrations.JOBS] == [
        "migrate-inventory",
        "migrate-orders",
        "migrate-notifications",
    ]
    assert {job.default_command for job in run_migrations.JOBS} == {"alembic upgrade head"}


def test_selected_jobs_supports_a_subset_and_rejects_unknown_names() -> None:
    assert [job.name for job in run_migrations.selected_jobs("orders,inventory")] == ["orders", "inventory"]
    with pytest.raises(ValueError):
        run_migrations.selected_jobs("payments")


def test_effective_command_prefers_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INVENTORY_MIGRATION_COMMAND", "python -m app.migrate")
    assert compose.describe_effective_command("INVENTORY_MIGRATION_COMMAND", "alembic upgrade head") == (
        "python -m app.migrate"
    )
    monkeypatch.delenv("INVENTORY_MIGRATION_COMMAND")
    assert compose.describe_effective_command("INVENTORY_MIGRATION_COMMAND", "alembic upgrade head") == (
        "alembic upgrade head"
    )


def test_compose_file_resolution_defaults_to_this_repository() -> None:
    assert compose.compose_file_from(None) == compose.DEFAULT_COMPOSE_FILE
    assert compose.compose_file_from(None).is_file()

    with pytest.raises(compose.ComposeError):
        compose.compose_file_from("does-not-exist.yml")


def test_http_helper_exposes_the_documented_response_contract() -> None:
    response = http.Response(
        status_code=409,
        text='{"error": {"code": "insufficient_stock", "message": "nope", "correlationId": "corr-1", "details": {}}}',
        headers={},
        url="http://gateway.invalid/api/orders",
    )

    assert response.error_code() == "insufficient_stock"
    assert response.error_message() == "nope"
    assert response.correlation_id() == "corr-1"
    assert not response.ok
    assert "insufficient_stock" in response.describe()

    plain = http.Response(status_code=500, text="<html>boom</html>", headers={}, url="http://gateway.invalid/")
    assert plain.error_code() is None
    assert "HTTP 500" in plain.describe()


def test_http_helper_builds_query_strings() -> None:
    assert http.with_query("http://x/api/orders", None) == "http://x/api/orders"
    assert http.with_query("http://x/api/orders", {"customerId": "abc"}) == "http://x/api/orders?customerId=abc"
    assert http.with_query("http://x/api/orders?limit=5", {"offset": 0}) == "http://x/api/orders?limit=5&offset=0"
    assert http.with_query("http://x/api/orders", {"skip": None}) == "http://x/api/orders"
