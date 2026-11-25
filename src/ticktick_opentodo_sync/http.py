"""Thin httpx wrapper: bearer auth, request spacing, retry on 429/5xx/transport errors."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from typing import Any

import httpx

from . import __version__

log = logging.getLogger(__name__)

RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "PUT", "DELETE", "PATCH"})
"""Safe to repeat (the sync's PATCHes all set absolute values)."""
NOT_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)
"""Transport errors raised before the request left: any request can be retried."""


class ApiError(Exception):
    def __init__(self, service: str, method: str, url: str, status: int, body: str) -> None:
        super().__init__(f"{service}: {method} {url} -> HTTP {status}: {body[:300]}")
        self.status = status


class Http:
    def __init__(
        self,
        service: str,
        base_url: str,
        token: str,
        *,
        min_interval: float = 0.3,
        timeout: float = 20.0,
        retries: int = 3,
        sleep: Callable[[float], None] = time.sleep,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.service = service
        self.min_interval = min_interval
        self.retries = retries
        self._sleep = sleep
        self._last = 0.0
        self.client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "User-Agent": f"ticktick-opentodo-sync/{__version__}",
            },
            timeout=timeout,
            transport=transport,
        )

    def close(self) -> None:
        self.client.close()

    def _throttle(self) -> None:
        wait = self._last + self.min_interval - time.monotonic()
        if wait > 0:
            self._sleep(wait)
        self._last = time.monotonic()

    def request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        not_found_ok: bool = False,
        idempotent: bool | None = None,
    ) -> Any:
        """Return the decoded JSON body (``None`` if empty, or on 404 with ``not_found_ok``).

        ``idempotent``: safe to send twice. By default true for GET, PUT, PATCH and DELETE,
        and for a POST with an ``Idempotency-Key`` (the server dedupes it). Anything else is
        retried only when the answer says it wasn't acted on: repeating a create whose answer
        was lost makes a duplicate, and repeating a TickTick completion moves a repeating
        task on twice.
        """
        if idempotent is None:
            idempotent = method.upper() in IDEMPOTENT_METHODS or "Idempotency-Key" in (
                headers or {}
            )
        attempt = 0
        while True:
            self._throttle()
            try:
                resp = self.client.request(method, path, json=json, params=params, headers=headers)
            except httpx.TransportError as exc:
                if not idempotent and not isinstance(exc, NOT_SENT):
                    log.warning(
                        "%s %s %s: no answer (%s); not repeated, as it may have gone through",
                        self.service,
                        method,
                        path,
                        exc,
                    )
                    raise
                if attempt >= self.retries:
                    raise
                delay = 2.0**attempt
                log.warning(
                    "%s %s %s failed (%s); retry in %.0fs", self.service, method, path, exc, delay
                )
            else:
                if resp.status_code == 404 and not_found_ok:
                    return None
                if resp.status_code < 300:
                    return resp.json() if resp.content.strip() else None
                if resp.status_code < 400:
                    # Redirects aren't followed: an empty 3xx body must never read as "no data".
                    where = resp.headers.get("Location", "")
                    raise ApiError(
                        self.service, method, path, resp.status_code, f"redirect to {where!r}"
                    )
                retryable = _retryable(resp, idempotent)
                if not retryable and resp.status_code in RETRY_STATUSES:
                    log.warning(
                        "%s %s %s -> %d; not repeated, as it may have gone through",
                        self.service,
                        method,
                        path,
                        resp.status_code,
                    )
                if not retryable or attempt >= self.retries:
                    raise ApiError(self.service, method, path, resp.status_code, resp.text)
                delay = _retry_after(resp) or 2.0**attempt
                log.warning(
                    "%s %s %s -> %d; retry in %.0fs",
                    self.service,
                    method,
                    path,
                    resp.status_code,
                    delay,
                )
            attempt += 1
            self._sleep(min(delay, 60.0))


def _retryable(resp: httpx.Response, idempotent: bool) -> bool:
    if resp.status_code not in RETRY_STATUSES:
        return False
    # A 500, 502 or 504 can come after the server acted (a gateway timing out on a slow
    # backend); a 429, or a 503 asking to come back later, says it wasn't acted on.
    return (
        idempotent
        or resp.status_code == 429
        or (resp.status_code == 503 and "Retry-After" in resp.headers)
    )


def _retry_after(resp: httpx.Response) -> float | None:
    value = resp.headers.get("Retry-After")
    try:
        return float(value) if value else None
    except ValueError:
        return None
