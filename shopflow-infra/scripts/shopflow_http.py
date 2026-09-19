"""Small HTTP helper shared by the shopflow-infra scripts.

The infra scripts run both on the operator's host and inside the infra one-shot
containers, so they are written to work with the Python **standard library
alone**. When ``httpx`` (pinned in ``scripts/requirements.txt``) happens to be
importable it is used instead, purely as a faster transport — no script ever
requires third-party code to be installed.

Everything here is intentionally free of business rules: it only speaks HTTP and
normalises the error envelope of contracts.md §1 for assertions.
"""

from __future__ import annotations

import json as _json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Mapping

try:  # optional transport, pinned in scripts/requirements.txt
    import httpx as _httpx  # type: ignore[import-not-found]
except Exception:  # pragma: no cover - exercised on hosts without httpx
    _httpx = None

#: Default per-request timeout for infra scripts.
DEFAULT_TIMEOUT_SECONDS = 5.0


class TransportError(RuntimeError):
    """The endpoint could not be reached at all (DNS, refused connection, timeout)."""


@dataclass(frozen=True)
class Response:
    """A minimal, transport-independent HTTP response."""

    status_code: int
    text: str
    headers: Mapping[str, str]
    url: str

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300

    def json(self) -> Any:
        """Parse the body as JSON. Raises :class:`ValueError` on a non-JSON body."""

        if not self.text:
            return None
        return _json.loads(self.text)

    def error_code(self) -> str | None:
        """Return ``error.code`` of the contracts.md §1 envelope, if present."""

        try:
            payload = self.json()
        except ValueError:
            return None
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict) and isinstance(error.get("code"), str):
                return error["code"]
        return None

    def error_message(self) -> str | None:
        """Return ``error.message`` of the contracts.md §1 envelope, if present."""

        try:
            payload = self.json()
        except ValueError:
            return None
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict) and isinstance(error.get("message"), str):
                return error["message"]
        return None

    def correlation_id(self) -> str | None:
        """Return ``error.correlationId`` of the contracts.md §1 envelope, if present."""

        try:
            payload = self.json()
        except ValueError:
            return None
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict) and isinstance(error.get("correlationId"), str):
                return error["correlationId"]
        return None

    def describe(self) -> str:
        """One-line description used in failure messages."""

        detail = f"HTTP {self.status_code} from {self.url}"
        code = self.error_code()
        if code:
            detail += f" [{code}]"
            message = self.error_message()
            if message:
                detail += f" {message}"
        elif self.text:
            detail += f" body={self.text[:200]}"
        correlation_id = self.correlation_id()
        if correlation_id:
            detail += f" (correlationId={correlation_id})"
        return detail


def backend() -> str:
    """Return the transport actually in use (``httpx`` or ``urllib``)."""

    return "httpx" if _httpx is not None else "urllib"


def with_query(url: str, params: Mapping[str, Any] | None) -> str:
    """Append non-``None`` query parameters to ``url``."""

    if not params:
        return url
    cleaned = {key: str(value) for key, value in params.items() if value is not None}
    if not cleaned:
        return url
    separator = "&" if urllib.parse.urlparse(url).query else "?"
    return f"{url}{separator}{urllib.parse.urlencode(cleaned)}"


def request(
    method: str,
    url: str,
    *,
    json_body: Any = None,
    headers: Mapping[str, str] | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> Response:
    """Perform one HTTP request and return a :class:`Response`.

    Raises :class:`TransportError` when the endpoint cannot be reached.
    """

    if _httpx is not None:
        return _request_with_httpx(method, url, json_body=json_body, headers=headers, timeout=timeout)
    return _request_with_urllib(method, url, json_body=json_body, headers=headers, timeout=timeout)


def get(url: str, *, params: Mapping[str, Any] | None = None, headers: Mapping[str, str] | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS) -> Response:
    return request("GET", with_query(url, params), headers=headers, timeout=timeout)


def post(url: str, *, json_body: Any = None, headers: Mapping[str, str] | None = None,
         timeout: float = DEFAULT_TIMEOUT_SECONDS) -> Response:
    return request("POST", url, json_body=json_body, headers=headers, timeout=timeout)


# --------------------------------------------------------------------------- #
# Transport implementations
# --------------------------------------------------------------------------- #
def _request_with_httpx(
    method: str,
    url: str,
    *,
    json_body: Any,
    headers: Mapping[str, str] | None,
    timeout: float,
) -> Response:
    try:
        with _httpx.Client(timeout=timeout, follow_redirects=False) as client:
            raw = client.request(method.upper(), url, json=json_body, headers=dict(headers or {}))
    except Exception as exc:  # httpx.TransportError and friends
        raise TransportError(f"{method.upper()} {url} failed: {exc}") from exc
    return Response(raw.status_code, raw.text, dict(raw.headers), url)


def _request_with_urllib(
    method: str,
    url: str,
    *,
    json_body: Any,
    headers: Mapping[str, str] | None,
    timeout: float,
) -> Response:
    data = None
    request_headers: dict[str, str] = {"Accept": "application/json"}
    request_headers.update(headers or {})
    if json_body is not None:
        data = _json.dumps(json_body).encode("utf-8")
        request_headers["Content-Type"] = "application/json; charset=utf-8"

    raw_request = urllib.request.Request(url, data=data, headers=request_headers, method=method.upper())
    try:
        with urllib.request.urlopen(raw_request, timeout=timeout) as raw:
            body = raw.read().decode("utf-8", "replace")
            return Response(int(raw.status), body, dict(raw.headers.items()), url)
    except urllib.error.HTTPError as exc:  # 4xx/5xx still carry a contract envelope
        body = exc.read().decode("utf-8", "replace")
        return Response(int(exc.code), body, dict(exc.headers.items()) if exc.headers else {}, url)
    except urllib.error.URLError as exc:
        raise TransportError(f"{method.upper()} {url} failed: {exc.reason}") from exc
    except OSError as exc:
        raise TransportError(f"{method.upper()} {url} failed: {exc}") from exc


__all__ = ["DEFAULT_TIMEOUT_SECONDS", "Response", "TransportError", "backend", "get", "post", "request", "with_query"]
