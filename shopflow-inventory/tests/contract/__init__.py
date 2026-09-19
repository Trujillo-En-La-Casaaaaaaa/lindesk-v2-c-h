"""Contract suite of shopflow-inventory: the HTTP surface of contracts.md §4.

The suite drives the real application (real routers, real error envelopes, real
use cases) over ASGI. Only the two ports are replaced by doubles so the contract
can be asserted without a database; the same shapes are re-asserted against the
real PostgreSQL store in ``tests/integration``.
"""

from __future__ import annotations
