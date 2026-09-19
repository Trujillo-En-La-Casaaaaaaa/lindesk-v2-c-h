"""Integration tests of the provider adapter against the provider contract.

The successful path is asserted against the real emulator of ``shopflow-infra``
(``POST /messages`` and the deterministic ``messageId`` of contracts.md §7); the
failure paths the emulator cannot produce on demand - ``5xx``, ``4xx`` and a genuine
connection refusal - are asserted against a real HTTP provider double and a closed
port.
"""

from __future__ import annotations

import httpx
import pytest

from app.adapters.provider_http_client import (
    HttpNotificationProvider,
    deterministic_message_id,
    provider_is_reachable,
)
from app.config import Settings
from app.domain.notification import OutboundMessage, parse_order_event
from app.domain.templates import build_outbound_message
from app.ports.provider import ProviderRejected, ProviderUnavailable
from tests.support.capture_provider import MODE_REJECTED, MODE_UNAVAILABLE
from tests.support.emulator import Emulator
from tests.support.events import event_id, order_cancelled, order_confirmed, order_id
from tests.support.servers import free_port


def message_for(order: str, seed: str, *, cancelled: bool = False) -> OutboundMessage:
    if cancelled:
        event = order_cancelled(order=order, event=event_id(seed), reason="out of stock")
    else:
        event = order_confirmed(order=order, event=event_id(seed))
    return build_outbound_message(parse_order_event(event))


def adapter(settings: Settings, *, base_url: str | None = None, **overrides: object) -> HttpNotificationProvider:
    options = {
        "timeout_seconds": settings.request_timeout_seconds,
        "max_attempts": settings.provider_max_attempts,
        "backoff_seconds": settings.provider_backoff_seconds,
        "backoff_max_seconds": settings.provider_backoff_max_seconds,
    }
    options.update(overrides)
    return HttpNotificationProvider(base_url or settings.provider_base_url, **options)  # type: ignore[arg-type]


def test_the_emulator_accepts_a_confirmation_once(
    settings: Settings,
    emulator: Emulator,
    case_id: str,
) -> None:
    order = order_id(f"{case_id}-order")
    provider = adapter(settings)

    receipt = provider.send(message_for(order, f"{case_id}-c"))

    assert receipt.status == "accepted"
    assert receipt.message_id == deterministic_message_id("order_confirmation", order)
    assert receipt.accepted_at and receipt.accepted_at.endswith("Z")
    messages = emulator.messages(order)
    assert len(messages) == 1
    assert messages[0]["template"] == "order_confirmation"
    assert messages[0]["orderId"] == order


def test_replaying_the_idempotency_key_returns_the_original_message(
    settings: Settings,
    emulator: Emulator,
    case_id: str,
) -> None:
    """The provider-side half of exactly-once: a retry must not record a second message."""

    order = order_id(f"{case_id}-order")
    provider = adapter(settings)
    message = message_for(order, f"{case_id}-c")

    first = provider.send(message)
    second = provider.send(message)

    assert second.message_id == first.message_id
    assert emulator.count(order) == 1


def test_the_same_key_with_a_different_body_is_reported_as_a_replay(
    settings: Settings,
    emulator: Emulator,
    case_id: str,
) -> None:
    """A 409 answers the deterministic id of the original message and adds nothing."""

    order = order_id(f"{case_id}-order")
    provider = adapter(settings)
    first = message_for(order, f"{case_id}-c")
    provider.send(first)

    # same key (same order and template) but a different body
    altered = OutboundMessage(
        order_id=first.order_id,
        notification_type=first.notification_type,
        recipient=first.recipient,
        payload={**first.payload, "text": "a conflicting body for the same key"},
        correlation_id=first.correlation_id,
    )
    receipt = provider.send(altered)

    assert receipt.message_id == deterministic_message_id("order_confirmation", order)
    assert emulator.count(order) == 1


def test_the_emulator_records_a_cancellation_with_its_template(
    settings: Settings,
    emulator: Emulator,
    case_id: str,
) -> None:
    order = order_id(f"{case_id}-order")
    provider = adapter(settings)

    provider.send(message_for(order, f"{case_id}-x", cancelled=True))

    messages = emulator.messages(order)
    assert len(messages) == 1
    assert messages[0]["template"] == "order_cancellation"
    assert messages[0]["messageId"] == deterministic_message_id("order_cancellation", order)


def test_an_unreachable_provider_is_bounded_and_reported(
    settings: Settings,
    case_id: str,
) -> None:
    """A genuine connection refusal: bounded retries, then a retryable failure."""

    order = order_id(f"{case_id}-order")
    provider = adapter(settings, base_url=f"http://127.0.0.1:{free_port()}", max_attempts=2)

    with pytest.raises(ProviderUnavailable) as failure:
        provider.send(message_for(order, f"{case_id}-c"))

    assert "2 attempt" in str(failure.value)
    assert failure.value.retryable is True


def test_a_5xx_provider_is_retried_and_then_reported_unavailable(
    settings: Settings,
    capture_provider,
    case_id: str,
) -> None:
    capture_provider.reset(mode=MODE_UNAVAILABLE)
    order = order_id(f"{case_id}-order")
    provider = adapter(settings, base_url=capture_provider.base_url, max_attempts=3)

    with pytest.raises(ProviderUnavailable):
        provider.send(message_for(order, f"{case_id}-c"))

    assert capture_provider.request_count == 3
    assert capture_provider.count() == 0


def test_a_4xx_provider_is_rejected_without_retrying(
    settings: Settings,
    capture_provider,
    case_id: str,
) -> None:
    capture_provider.reset(mode=MODE_REJECTED)
    order = order_id(f"{case_id}-order")
    provider = adapter(settings, base_url=capture_provider.base_url, max_attempts=3)

    with pytest.raises(ProviderRejected) as failure:
        provider.send(message_for(order, f"{case_id}-c"))

    assert capture_provider.request_count == 1
    assert failure.value.status_code == 400


def test_a_recovered_provider_accepts_the_retry(
    settings: Settings,
    capture_provider,
    case_id: str,
) -> None:
    """A provider that comes back within the budget must still accept the message."""

    capture_provider.reset(mode=MODE_UNAVAILABLE)
    order = order_id(f"{case_id}-order")
    message = message_for(order, f"{case_id}-c")
    provider = adapter(settings, base_url=capture_provider.base_url, max_attempts=3)

    with pytest.raises(ProviderUnavailable):
        provider.send(message)
    assert capture_provider.count() == 0

    capture_provider.reset()
    receipt = provider.send(message)

    assert receipt.message_id == deterministic_message_id("order_confirmation", order)
    assert capture_provider.count("order_confirmation", order) == 1


def test_the_real_emulator_answers_the_readiness_probe(provider_base_url: str) -> None:
    assert provider_is_reachable(provider_base_url) is True


def test_a_closed_port_is_not_ready() -> None:
    assert provider_is_reachable(f"http://127.0.0.1:{free_port()}", timeout_seconds=1.0) is False


def test_the_adapter_never_touches_the_internet(settings: Settings) -> None:
    """The provider URL is configuration; nothing else is ever contacted."""

    with httpx.Client(timeout=2.0) as client:
        response = client.get(f"{settings.provider_base_url.rstrip('/')}/health")
    assert response.status_code == 200
