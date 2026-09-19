"""HTTP middleware of the gateway.

Modules are imported directly (``from app.middleware.correlation import ...``);
this package deliberately re-exports nothing so that the import graph between
the middleware, routing and upstream packages stays acyclic.
"""
