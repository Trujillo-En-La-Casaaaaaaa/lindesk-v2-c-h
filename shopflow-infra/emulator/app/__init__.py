"""ShopFlow notification-provider emulator (contracts.md §7).

Deterministic, offline test double for the production notification provider.
The package exposes a FastAPI application in :mod:`app.main`;
:mod:`app.store` holds the in-memory idempotent message store.
"""

__all__ = ["__version__"]

__version__ = "1.0.0"
