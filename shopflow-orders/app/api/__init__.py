"""HTTP surface of shopflow-orders (contracts.md §5)."""

from fastapi import Request


def correlation_id_of(request: Request) -> str:
    """Echo ``X-Correlation-Id`` unchanged, or the empty string when absent (§1).

    The gateway generates the id when the browser does not send one and forwards it
    unchanged; this service therefore never invents one, it only echoes what it got
    in the error envelope and in the event envelopes it publishes.
    """

    return request.headers.get("X-Correlation-Id", "")


__all__ = ["correlation_id_of"]
