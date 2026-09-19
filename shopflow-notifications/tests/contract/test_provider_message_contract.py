"""Contract tests of the provider call (contracts.md §7).

The body and headers are taken from the adapter itself, so the contract is asserted
against the code that really performs the call.
"""

from __future__ import annotations

import json

from app.adapters.provider_http_client import (
    CHANNEL_EMAIL,
    IDEMPOTENCY_HEADER,
    PROVIDER_MESSAGES_PATH,
    deterministic_message_id,
    message_body,
)
from app.domain.notification import TEMPLATE_ORDER_CANCELLATION, TEMPLATE_ORDER_CONFIRMATION, parse_order_event
from app.domain.templates import build_outbound_message
from tests.support.events import event_id, order_cancelled, order_confirmed, order_id

ORDER = order_id("provider-contract")

SUPPORTED_TEMPLATES = {TEMPLATE_ORDER_CONFIRMATION, TEMPLATE_ORDER_CANCELLATION}


def confirmation():
    event = order_confirmed(order=ORDER, event=event_id("provider-contract-c"))
    return build_outbound_message(parse_order_event(event))


def cancellation():
    return build_outbound_message(
        parse_order_event(order_cancelled(order=ORDER, event=event_id("provider-contract-x"), reason="late"))
    )


def test_the_message_path_is_the_documented_one() -> None:
    assert PROVIDER_MESSAGES_PATH == "/messages"


def test_the_body_has_exactly_the_contracted_fields() -> None:
    body = message_body(confirmation())

    assert set(body) == {"channel", "to", "template", "orderId", "payload"}
    assert body["channel"] == CHANNEL_EMAIL
    assert body["template"] in SUPPORTED_TEMPLATES
    assert isinstance(body["to"], str) and body["to"]
    assert isinstance(body["payload"], dict)
    json.dumps(body)  # the body must be JSON serialisable


def test_the_idempotency_key_is_order_colon_order_id_colon_template() -> None:
    message = confirmation()

    assert message.idempotency_key == f"order:{ORDER}:order_confirmation"
    assert IDEMPOTENCY_HEADER == "Idempotency-Key"
    # contracts.md §7 requires the key the adapter sends to be that exact value.
    assert message.template in message.idempotency_key
    assert message.order_id in message.idempotency_key


def test_each_notification_type_uses_its_own_template_and_key() -> None:
    confirmed = confirmation()
    cancelled = cancellation()

    assert confirmed.template == "order_confirmation"
    assert cancelled.template == "order_cancellation"
    assert confirmed.idempotency_key != cancelled.idempotency_key
    assert cancelled.payload["reason"] == "late"


def test_the_message_id_is_the_documented_deterministic_value() -> None:
    import hashlib

    for template, message in (("order_confirmation", confirmation()), ("order_cancellation", cancellation())):
        expected = "msg-" + hashlib.sha256(f"{template}:{ORDER}".encode()).hexdigest()[:16]
        assert deterministic_message_id(template, message.order_id) == expected
