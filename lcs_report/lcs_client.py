"""HTTP client for the LocalCoinSwap v2 API.

Handles the three things that bite when you script against this API:
pagination (it is cursor-free, offset/limit only), the 60 requests/minute
account ceiling, and the 401 shape used for both missing and invalid keys.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Iterator

import httpx

from .config import LCS_API_BASE

log = logging.getLogger(__name__)


class LcsError(RuntimeError):
    """Raised when the LCS API cannot satisfy a request."""


class LcsAuthError(LcsError):
    """Raised on 401/403 -- missing, invalid, or revoked API key."""


class LcsRateLimited(LcsError):
    """Raised when the account-level rate limit is hit."""


class LcsClient:
    """Thin, synchronous client over the LCS REST API."""

    def __init__(
        self,
        api_key: str | None,
        *,
        base_url: str = LCS_API_BASE,
        page_size: int = 200,
        min_interval: float = 1.1,
        timeout: float = 30.0,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.page_size = page_size
        self.min_interval = min_interval
        self._last_request = 0.0
        self._client = httpx.Client(
            timeout=timeout,
            headers={"Accept": "application/json"},
            follow_redirects=True,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "LcsClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self.api_key:
            headers["Authorization"] = f"ApiKey {self.api_key}"
        return headers

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self._last_request = time.monotonic()

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """GET a path, returning decoded JSON.

        Retries on 429 and transient 5xx with backoff, and re-reads the
        ``next`` link from the body rather than constructing URLs, so server
        side changes to the pagination scheme are picked up automatically.
        """
        url = path if path.startswith("http") else f"{self.base_url}{path}"
        query = dict(params or {})
        last_error: Exception | None = None

        for attempt in range(4):
            self._throttle()
            try:
                resp = self._client.get(url, params=query, headers=self._headers())
            except httpx.HTTPError as exc:
                last_error = exc
                if attempt == 3:
                    raise LcsError(f"Network error calling {url}: {exc}") from exc
                time.sleep(1.5 * (attempt + 1))
                continue

            if resp.status_code in (401, 403):
                detail = _safe_detail(resp)
                raise LcsAuthError(
                    "LCS rejected the API key (HTTP "
                    f"{resp.status_code}). {detail} "
                    "Check LCS_API_KEY in your .env -- the key must be created "
                    "under Preferences > API with an authenticator code."
                )

            if resp.status_code == 429:
                last_error = LcsRateLimited("Rate limited by LCS.")
                retry_after = resp.headers.get("Retry-After")
                delay = float(retry_after) if (retry_after or "").isdigit() else (
                    5.0 * (attempt + 1)
                )
                log.warning("429 from %s, sleeping %.1fs", url, delay)
                time.sleep(delay)
                continue

            if resp.status_code >= 500:
                last_error = LcsError(f"HTTP {resp.status_code} from {url}")
                time.sleep(1.5 * (attempt + 1))
                continue

            if resp.status_code >= 400:
                raise LcsError(
                    f"HTTP {resp.status_code} from {url}: {_safe_detail(resp)}"
                )

            try:
                return resp.json()
            except ValueError as exc:
                raise LcsError(f"Non-JSON response from {url}") from exc

        # Surface the specific failure when there is one: a rate limit should
        # not be reported as a generic exhaustion.
        if isinstance(last_error, LcsError):
            raise last_error
        raise LcsError(f"Exhausted retries for {url}: {last_error}")

    def paginate(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        max_items: int = 20_000,
    ) -> Iterator[dict[str, Any]]:
        """Yield every record across all pages of a list endpoint.

        Paging is driven by our own offset arithmetic rather than by following
        the server's ``next`` link, because that link is unreliable in two ways
        observed against the live API on 2026-10-05:

        1. It is returned on ``http://``, and Django's http-to-https 301 drops
           the query string, so following it verbatim re-reads page 1 forever.
        2. After ``?limit=100&offset=100`` it can come back as
           ``?limit=5&offset=5``, silently resetting to the default page size
           and re-reading records already seen.

        A short page (or reaching the reported ``count``) ends the walk, which
        is both deterministic and independent of those defects.
        """
        query = dict(params or {})
        limit = int(query.pop("limit", self.page_size) or self.page_size)
        limit = max(1, limit)
        offset = int(query.pop("offset", 0) or 0)

        yielded = 0
        total: int | None = None

        while yielded < max_items:
            payload = self.get(path, {**query, "limit": limit, "offset": offset})
            if not isinstance(payload, dict):
                return

            if total is None:
                raw_count = payload.get("count")
                total = int(raw_count) if isinstance(raw_count, int) else None

            results = payload.get("results")
            if not isinstance(results, list):
                return

            items = [r for r in results if isinstance(r, dict)]
            for item in items:
                yield item
                yielded += 1
                if yielded >= max_items:
                    log.warning("hit max_items=%d on %s", max_items, path)
                    return

            if not items:
                return
            # A short page means we have read the tail.
            if len(items) < limit:
                return
            if total is not None and offset + len(items) >= total:
                return

            offset += len(items)

    def fetch_trades(self, **filters: Any) -> list[dict[str, Any]]:
        """Fetch trades matching ``filters`` across all pages."""
        clean = {k: v for k, v in filters.items() if v not in (None, "", [])}
        clean.setdefault("ordering", "-time_created")
        return list(self.paginate("/trades/list/", clean))

    def fetch_currencies(self) -> list[dict[str, Any]]:
        return list(self.paginate("/currencies/active-cryptos/", {"limit": 100}))


def _safe_detail(resp: httpx.Response) -> str:
    """Best-effort human message from an error response."""
    try:
        body = resp.json()
    except ValueError:
        return (resp.text or "")[:300]
    if not isinstance(body, dict):
        return str(body)[:300]
    for key in ("message", "detail", "error"):
        value = body.get(key)
        if isinstance(value, str) and value:
            return value[:300]
        if isinstance(value, dict) and value:
            first = next(iter(value.values()))
            if isinstance(first, dict) and first.get("message"):
                return str(first["message"])[:300]
    return str(body)[:300]


__all__ = [
    "LcsAuthError",
    "LcsClient",
    "LcsError",
    "LcsRateLimited",
]
