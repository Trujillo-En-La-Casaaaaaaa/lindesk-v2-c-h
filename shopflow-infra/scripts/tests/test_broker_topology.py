"""Contract tests for the broker topology (``contracts.md`` §8).

The durable topology is declared twice on purpose — once declaratively in
``broker/definitions.json`` (loaded by the broker at boot where the host allows
it) and once imperatively by ``broker/bootstrap.py`` (the ``broker-init`` job that
re-declares and verifies it, which the services are gated on). These tests make
sure both mechanisms still describe the *same* contract, so they cannot drift
apart: exchange ``shopflow.orders`` (topic, durable), queue
``shopflow.notifications.order-events`` (durable), bindings
``order.confirmed`` and ``order.cancelled``.

``bootstrap.py`` is parsed with :mod:`ast` rather than imported, so the test needs
neither ``pika`` nor a broker.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BOOTSTRAP = REPO_ROOT / "broker" / "bootstrap.py"
DEFINITIONS = REPO_ROOT / "broker" / "definitions.json"
RABBITMQ_CONF = REPO_ROOT / "broker" / "rabbitmq.conf"
COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"

EXPECTED_EXCHANGE = "shopflow.orders"
EXPECTED_EXCHANGE_TYPE = "topic"
EXPECTED_QUEUE = "shopflow.notifications.order-events"
EXPECTED_ROUTING_KEYS = ("order.confirmed", "order.cancelled")


def bootstrap_tree() -> ast.Module:
    return ast.parse(BOOTSTRAP.read_text(encoding="utf-8"))


def bootstrap_constants() -> dict[str, object]:
    """Module-level literal constants of ``bootstrap.py`` (no import, no pika)."""

    constants: dict[str, object] = {}
    for node in bootstrap_tree().body:
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
            try:
                constants[node.targets[0].id] = ast.literal_eval(node.value)
            except ValueError:  # not a literal (for example a computed value)
                continue
    return constants


def declared_calls() -> list[tuple[str, dict[str, str]]]:
    """``(method_name, {keyword: source})`` for every declare/bind call."""

    calls = []
    for node in ast.walk(bootstrap_tree()):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in {"exchange_declare", "queue_declare", "queue_bind"}:
                calls.append(
                    (
                        node.func.attr,
                        {keyword.arg: ast.unparse(keyword.value) for keyword in node.keywords if keyword.arg},
                    )
                )
    return calls


def definitions() -> dict:
    return json.loads(DEFINITIONS.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# bootstrap.py (the mechanism the services are gated on)
# --------------------------------------------------------------------------- #
def test_bootstrap_constants_match_contract_section_8() -> None:
    constants = bootstrap_constants()

    assert constants["EXCHANGE_NAME"] == EXPECTED_EXCHANGE
    assert constants["EXCHANGE_TYPE"] == EXPECTED_EXCHANGE_TYPE
    assert constants["QUEUE_NAME"] == EXPECTED_QUEUE
    assert tuple(constants["ROUTING_KEYS"]) == EXPECTED_ROUTING_KEYS


def test_bootstrap_declares_both_objects_durable_and_not_auto_deleted() -> None:
    calls = declared_calls()
    declares = {
        method: [keywords for candidate, keywords in calls if candidate == method]
        for method in ("exchange_declare", "queue_declare")
    }

    assert declares["exchange_declare"], "bootstrap.py never declares the exchange"
    assert declares["queue_declare"], "bootstrap.py never declares the queue"

    for keywords in declares["exchange_declare"] + declares["queue_declare"]:
        assert keywords["durable"] == "True", f"durable must be a literal True: {keywords}"

        if keywords.get("passive") == "True":
            continue  # the verification pass only asserts existence
        assert keywords.get("auto_delete") == "False", keywords
        assert "passive" not in keywords, keywords


def test_bootstrap_verifies_with_passive_declares() -> None:
    calls = declared_calls()
    passive = [keywords for _, keywords in calls if keywords.get("passive") == "True"]

    assert passive, "the bootstrap must verify the topology, not only declare it"
    assert all(keywords["durable"] == "True" for keywords in passive), passive


def test_bootstrap_binds_every_routing_key_from_the_exchange_to_the_queue() -> None:
    calls = declared_calls()
    binds = [keywords for method, keywords in calls if method == "queue_bind"]

    assert {"exchange": "EXCHANGE_NAME", "queue": "QUEUE_NAME"} in [
        {key: value for key, value in keywords.items() if key in {"exchange", "queue"}} for keywords in binds
    ], binds
    assert all(keywords.get("routing_key") == "routing_key" for keywords in binds), binds


def test_bootstrap_verifies_the_topology_before_reporting_success() -> None:
    source = BOOTSTRAP.read_text(encoding="utf-8")

    assert "def verify_topology" in source
    assert "passive=True" in source, "verification must assert the objects exist"
    assert "broker-bootstrap: OK" in source


# --------------------------------------------------------------------------- #
# definitions.json (the declarative twin)
# --------------------------------------------------------------------------- #
def test_definitions_file_declares_the_same_exchange_and_queue() -> None:
    document = definitions()

    exchanges = [entry for entry in document["exchanges"] if entry["name"] == EXPECTED_EXCHANGE]
    assert len(exchanges) == 1
    assert exchanges[0]["type"] == EXPECTED_EXCHANGE_TYPE
    assert exchanges[0]["durable"] is True
    assert exchanges[0]["auto_delete"] is False
    assert exchanges[0]["internal"] is False

    queues = [entry for entry in document["queues"] if entry["name"] == EXPECTED_QUEUE]
    assert len(queues) == 1
    assert queues[0]["durable"] is True
    assert queues[0]["auto_delete"] is False


def test_definitions_file_binds_both_routing_keys() -> None:
    document = definitions()

    bindings = {
        (entry["source"], entry["destination"], entry["routing_key"]) for entry in document["bindings"]
    }

    for routing_key in EXPECTED_ROUTING_KEYS:
        assert (EXPECTED_EXCHANGE, EXPECTED_QUEUE, routing_key) in bindings, bindings
    assert not [
        entry for entry in document["bindings"] if entry["source"] == EXPECTED_EXCHANGE and entry["routing_key"] not in EXPECTED_ROUTING_KEYS
    ], "the exchange must not be bound to anything else in scope"


def test_both_mechanisms_agree() -> None:
    constants = bootstrap_constants()
    document = definitions()

    assert constants["EXCHANGE_NAME"] == document["exchanges"][0]["name"]
    assert constants["EXCHANGE_TYPE"] == document["exchanges"][0]["type"]
    assert constants["QUEUE_NAME"] == document["queues"][0]["name"]
    assert sorted(constants["ROUTING_KEYS"]) == sorted(entry["routing_key"] for entry in document["bindings"])


def test_the_broker_is_wired_to_load_the_definitions_file() -> None:
    assert "load_definitions = /etc/rabbitmq/definitions.json" in RABBITMQ_CONF.read_text(encoding="utf-8")

    compose = COMPOSE_FILE.read_text(encoding="utf-8")
    assert "./broker/definitions.json:/etc/rabbitmq/definitions.json:ro" in compose
    assert "./broker/rabbitmq.conf:/etc/rabbitmq/rabbitmq.conf:ro" in compose


def test_the_broker_init_job_gates_both_event_producers() -> None:
    compose = COMPOSE_FILE.read_text(encoding="utf-8")

    # `broker-init` must be a completion dependency of the two services that use the broker
    assert compose.count("broker-init:\n        condition: service_completed_successfully") == 2, (
        "shopflow-orders and shopflow-notifications must both wait for broker-init"
    )
