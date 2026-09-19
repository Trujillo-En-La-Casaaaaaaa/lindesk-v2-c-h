"""Shared setup of the public contract suite.

The contract suite verifies the *public* behaviour of the gateway against the
in-process harness upstreams: it drives faults (timeouts, malformed envelopes,
transient statuses) and inspects the upstream request logs, which the real
services deliberately do not expose. It is therefore scoped to harness mode;
the integration suite is the mode-agnostic one that also runs against the real
``shopflow-orders`` and ``shopflow-inventory`` services.
"""

from __future__ import annotations

import pytest

from tests.conftest import UpstreamStack


@pytest.fixture(autouse=True)
def _require_harness_upstreams(upstream_stack: UpstreamStack) -> None:
    if upstream_stack.live:
        pytest.skip(
            "the public contract suite needs the in-process harness upstreams for fault"
            " injection and request logs; unset SHOPFLOW_GATEWAY_LIVE_UPSTREAMS=1 to run it"
        )
