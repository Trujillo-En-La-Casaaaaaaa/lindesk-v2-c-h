"""Wait-until helpers and the consumer thread used by the integration/failure suites."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

from app.consumer.consumer import OrderEventConsumer

DEFAULT_TIMEOUT = 60.0
DEFAULT_INTERVAL = 0.05


class WaitTimeout(AssertionError):
    """A condition did not become true within its budget."""


def wait_until(
    predicate: Callable[[], Any],
    *,
    timeout: float = DEFAULT_TIMEOUT,
    interval: float = DEFAULT_INTERVAL,
    description: str = "condition",
) -> Any:
    """Poll ``predicate`` until it returns something truthy, or fail loudly."""

    deadline = time.monotonic() + timeout
    last: Any = None
    while True:
        last = predicate()
        if last:
            return last
        if time.monotonic() >= deadline:
            raise WaitTimeout(f"{description} did not happen within {timeout:g}s (last value: {last!r})")
        time.sleep(interval)


class ConsumerThread:
    """Runs an :class:`OrderEventConsumer` in a background thread for one test."""

    def __init__(self, consumer: OrderEventConsumer, *, name: str = "notifications-test-consumer") -> None:
        self.consumer = consumer
        self._thread: threading.Thread | None = None
        self._name = name

    def start(self) -> ConsumerThread:
        self._thread = self.consumer.run_in_thread(name=self._name)
        return self

    def stop(self, timeout: float = 30.0) -> None:
        self.consumer.stop()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout)

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def __enter__(self) -> ConsumerThread:
        return self.start()

    def __exit__(self, *_exc: object) -> None:
        self.stop()


__all__ = ["ConsumerThread", "WaitTimeout", "wait_until"]
