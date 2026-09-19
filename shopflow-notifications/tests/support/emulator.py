"""Read the provider emulator's test affordance (contracts.md §7 ``GET /messages``).

The emulator stores exactly ``messageId``, ``orderId``, ``template``, ``to`` and
``receivedAt`` per accepted message, which is what the ecosystem smoke test counts.
Payload content is asserted separately against a capture provider, because the
emulator deliberately does not expose the payload.
"""

from __future__ import annotations

import httpx


class Emulator:
    """Client of the deterministic read-back route of the provider emulator."""

    def __init__(self, base_url: str, *, timeout_seconds: float = 5.0) -> None:
        self.base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds

    def messages(self, order_id: str | None = None) -> list[dict[str, object]]:
        params = {"orderId": order_id} if order_id else None
        with httpx.Client(timeout=self._timeout) as client:
            response = client.get(f"{self.base_url}/messages", params=params)
        response.raise_for_status()
        payload = response.json()
        return list(payload.get("items") or [])

    def count(self, order_id: str, template: str | None = None) -> int:
        return len(
            [
                message
                for message in self.messages(order_id)
                if template is None or message.get("template") == template
            ]
        )

    def message_ids(self, order_id: str) -> list[str]:
        return [str(message.get("messageId")) for message in self.messages(order_id)]

    def templates(self, order_id: str) -> list[str]:
        return [str(message.get("template")) for message in self.messages(order_id)]


__all__ = ["Emulator"]
