"""Upstream integration of the gateway.

``app.upstream.errors`` holds the error normalizer (no gateway imports), while
``app.upstream.client`` holds the HTTP client and the proxy helper. Modules are
imported directly to keep the import graph acyclic.
"""
