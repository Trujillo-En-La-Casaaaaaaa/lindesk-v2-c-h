"""ShopFlow Gateway: the single public HTTP surface for the ShopFlow storefront.

The gateway is a layered edge adapter: it validates client requests, propagates
correlation ids, routes every request to the owning service, normalizes errors
into one envelope, guards the administrative route and exposes health and
readiness. It owns no durable business data.
"""

__all__ = ["__version__"]

__version__ = "1.0.0"
