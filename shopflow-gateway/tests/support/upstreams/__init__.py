"""Contract-faithful upstream services and their test affordances.

``tests/support/upstreams/inventory.py`` and ``orders.py`` implement the HTTP
interfaces of contracts.md §4 and §5 (including the owned SQL schemas and the
seed data) so the gateway suite can run a real end-to-end flow over HTTP even
when the sibling repositories ``shopflow-inventory`` and ``shopflow-orders``
are not present in this workspace. Set ``SHOPFLOW_GATEWAY_LIVE_UPSTREAMS=1``
with ``ORDERS_BASE_URL``/``INVENTORY_BASE_URL`` to run the same suite against
the real services instead.
"""
