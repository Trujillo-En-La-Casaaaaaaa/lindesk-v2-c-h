"""Unit tests of the provider HTTP adapter (contracts.md §7, handoff #5).

The adapter is exercised through ``httpx.MockTransport``: the real request object
is built by httpx, so the method, URL, headers and body asserted below are the ones
a provider would receive. The integration suite repeats the same conversation
against the real emulator over a socket.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from app.adapters.provider_http_client import (
    CHANNEL_EMAIL,
    HttpNotificationProvider,
    message_body,
    provider_is_reachable,
)
from app.domain.notification import OutboundMessage, parse_order_event
from app.ports.provider import ProviderRejected, ProviderUnavailable
from tests.support.events import event_id, order_cancelled, order_confirmed, order_id

ORDER = order_id("provider")

ACCEPTED = {"messageId": "msg-0123456789abcdef", "status": "accepted", "acceptedAt": "2026-09-18T12:00:00Z"}


def confirmed_message() -> OutboundMessage:
    from app.domain.templates import build_outbound_message

    return build_outbound_message(parse_order_event(order_confirmed(order=ORDER, event=event_id("provider-c"))))


def cancelled_message() -> OutboundMessage:
    from app.domain.templates import build_outbound_message

    return build_outbound_message(
        parse_order_event(order_cancelled(order=ORDER, event=event_id("provider-x"), reason="out of stock"))
    )


class Recorder:
    """A ``httpx.MockTransport`` handler replaying scripted outcomes."""

    def __init__(self, *outcomes: Any) -> None:
        self.outcomes = list(outcomes)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        index = min(len(self.requests) - 1, len(self.outcomes) - 1)
        outcome = self.outcomes[index]
        if isinstance(outcome, Exception):
            raise outcome
        if callable(outcome):
            return outcome(request)
        return outcome


def build_provider(*outcomes: Any, **kwargs: Any) -> tuple[HttpNotificationProvider, Recorder, list[float]]:
    recorder = Recorder(*outcomes)
    client = httpx.Client(transport=httpx.MockTransport(recorder), timeout=10.0, follow_redirects=False)
    sleeps: list[float] = []
    adapter = HttpNotificationProvider(
        "http://provider.test",
        client=client,
        sleep=sleeps.append,
        **kwargs,
    )
    return adapter, recorder, sleeps


def test_the_request_matches_the_provider_contract() -> None:
    adapter, recorder, _ = build_provider(httpx.Response(200, json=ACCEPTED))
    message = confirmed_message()

    receipt = adapter.send(message)

    request = recorder.requests[0]
    assert request.method == "POST"
    assert str(request.url) == "http://provider.test/messages"
    assert request.headers["Idempotency-Key"] == f"order:{ORDER}:order_confirmation"
    assert request.headers["X-Correlation-Id"] == "corr-smoke"
    assert request.headers["Content-Type"].startswith("application/json")
    body = json.loads(request.content)
    assert body == message_body(message)
    assert set(body) == {"channel", "to", "template", "orderId", "payload"}
    assert body["channel"] == CHANNEL_EMAIL
    assert body["template"] == "order_confirmation"
    assert body["orderId"] == ORDER
    assert body["payload"]["total"] == 72.47
    assert receipt.message_id == ACCEPTED["messageId"]
    assert receipt.status == "accepted"
    assert receipt.accepted_at == ACCEPTED["acceptedAt"]


def test_a_cancellation_carries_its_template_and_reason() -> None:
    adapter, recorder, _ = build_provider(httpx.Response(200, json=ACCEPTED))

    adapter.send(cancelled_message())

    body = json.loads(recorder.requests[0].content)
    assert body["template"] == "order_cancellation"
    assert body["payload"]["reason"] == "out of stock"
    assert recorder.requests[0].headers["Idempotency-Key"] == f"order:{ORDER}:order_cancellation"


def test_a_5xx_is_retried_with_backoff_until_it_succeeds() -> None:
    adapter, recorder, sleeps = build_provider(
        httpx.Response(503, json={"error": {"code": "internal_error"}}),
        httpx.Response(502, text="bad gateway"),
        httpx.Response(200, json=ACCEPTED),
        max_attempts=3,
        backoff_seconds=0.5,
        backoff_max_seconds=5,
    )

    receipt = adapter.send(confirmed_message())

    assert receipt.message_id == ACCEPTED["messageId"]
    assert len(recorder.requests) == 3
    assert sleeps == [0.5, 1.0]


def test_a_connection_error_is_retried_and_then_reported_as_unavailable() -> None:
    adapter, recorder, sleeps = build_provider(
        httpx.ConnectError("connection refused"),
        httpx.ConnectError("connection refused"),
        max_attempts=2,
        backoff_seconds=0.25,
        backoff_max_seconds=1,
    )

    with pytest.raises(ProviderUnavailable) as failure:
        adapter.send(confirmed_message())

    assert len(recorder.requests) == 2
    assert sleeps == [0.25]
    assert "2 attempt" in str(failure.value)
    assert isinstance(failure.value, ProviderUnavailable)


def test_a_failing_5xx_is_bounded_and_never_retries_forever() -> None:
    adapter, recorder, sleeps = build_provider(
        httpx.Response(500, text="boom"),
        max_attempts=3,
        backoff_seconds=0.1,
        backoff_max_seconds=0.2,
    )

    with pytest.raises(ProviderUnavailable):
        adapter.send(confirmed_message())

    assert len(recorder.requests) == 3
    assert sleeps == [0.1, 0.2]  # exponential, capped


def test_a_4xx_is_permanent_and_not_retried() -> None:
    adapter, recorder, sleeps = build_provider(
        httpx.Response(400, json={"error": {"code": "validation_error"}}),
        max_attempts=3,
    )

    with pytest.raises(ProviderRejected) as failure:
        adapter.send(confirmed_message())

    assert len(recorder.requests) == 1
    assert sleeps == []
    assert failure.value.status_code == 400
    assert failure.value.retryable is False


def test_a_409_is_an_idempotent_replay_and_reports_the_deterministic_id() -> None:
    from app.adapters.provider_http_client import deterministic_message_id

    adapter, recorder, _ = build_provider(
        httpx.Response(409, json={"error": {"code": "idempotency_conflict"}}),
    )

    receipt = adapter.send(confirmed_message())

    assert len(recorder.requests) == 1
    assert receipt.message_id == deterministic_message_id("order_confirmation", ORDER)


def test_a_success_without_a_message_id_is_treated_as_retryable() -> None:
    adapter, recorder, _ = build_provider(
        httpx.Response(200, json={"status": "accepted"}),
        max_attempts=2,
        backoff_seconds=0,
        backoff_max_seconds=0,
    )

    with pytest.raises(ProviderUnavailable):
        adapter.send(confirmed_message())

    assert len(recorder.requests) == 2


def test_the_configured_timeout_is_applied_to_every_attempt() -> None:
    adapter, recorder, _ = build_provider(httpx.Response(200, json=ACCEPTED), timeout_seconds=2.5)

    adapter.send(confirmed_message())

    timeouts = recorder.requests[0].extensions.get("timeout") or {}
    assert set(timeouts.values()) == {2.5}


def test_readiness_probe_reads_the_ready_route() -> None:
    recorder = Recorder(httpx.Response(200, json={"status": "ready"}))
    client = httpx.Client(transport=httpx.MockTransport(recorder), timeout=5.0)

    assert provider_is_reachable("http://provider.test", client=client) is True
    assert str(recorder.requests[0].url) == "http://provider.test/ready"
    assert recorder.requests[0].method == "GET"


@pytest.mark.parametrize(
    "outcome",
    [httpx.Response(503, text="down"), httpx.ConnectError("refused")],
)
def test_readiness_probe_reports_an_unreachable_provider(outcome: Any) -> None:
    recorder = Recorder(outcome)
    client = httpx.Client(transport=httpx.MockTransport(recorder), timeout=5.0)

    assert provider_is_reachable("http://provider.test", client=client) is False


def test_the_adapter_can_probe_its_own_provider() -> None:
    adapter, recorder, _ = build_provider(httpx.Response(200, json={"status": "ready"}))

    assert adapter.is_reachable() is True
    assert str(recorder.requests[0].url) == "http://provider.test/ready"
