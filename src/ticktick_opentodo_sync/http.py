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
    ) -> Any:
        """Return the decoded JSON body (``None`` if empty, or on 404 with ``not_found_ok``)."""
        attempt = 0
        while True:
            self._throttle()
            try:
                resp = self.client.request(method, path, json=json, params=params, headers=headers)
            except httpx.TransportError as exc:
                if attempt >= self.retries:
                    raise
                delay = 2.0**attempt
                log.warning(
                    "%s %s %s failed (%s); retry in %.0fs", self.service, method, path, exc, delay
                )
            else:
                if resp.status_code == 404 and not_found_ok:
                    return None
                if resp.status_code < 400:
                    return resp.json() if resp.content.strip() else None
                if resp.status_code not in RETRY_STATUSES or attempt >= self.retries:
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


def _retry_after(resp: httpx.Response) -> float | None:
    value = resp.headers.get("Retry-After")
    try:
        return float(value) if value else None
    except ValueError:
        return None
