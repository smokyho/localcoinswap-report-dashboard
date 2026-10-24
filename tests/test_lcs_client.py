"""Tests for the LCS HTTP client, focused on pagination correctness.

The regression that motivated these: LCS returns ``next`` links on ``http://``
and Django's http-to-https 301 drops the query string, so a naive client
re-reads page 1 forever. Verified against the live API on 2026-10-05.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lcs_report.lcs_client import (  # noqa: E402
    LcsAuthError,
    LcsClient,
    LcsError,
    LcsRateLimited,
)

BASE = "https://api.localcoinswap.com/api/v2"


class FakeResponse:
    def __init__(self, status_code: int, payload: Any = None, headers=None):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}
        self.text = str(payload)

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("boom", request=None, response=None)

    def json(self) -> Any:
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


@pytest.fixture()
def client() -> LcsClient:
    c = LcsClient("lcs_test_secret", min_interval=0.0, timeout=1.0)
    yield c
    c.close()


# --------------------------------------------------------------------------
# Pagination
# --------------------------------------------------------------------------


def _paged_payloads(total: int, page: int, limit: int) -> dict[str, Any]:
    start = page * limit
    results = [
        {"uuid": f"t{i}", "n": i} for i in range(start, min(start + limit, total))
    ]
    nxt = (
        f"http://api.localcoinswap.com/api/v2/trades/list/?limit={limit}&offset={start + limit}"
        if start + limit < total
        else None
    )
    return {"count": total, "next": nxt, "previous": None, "results": results}


def _served(params, default_limit=10):
    """Read the offset/limit the client actually asked for."""
    p = params or {}
    return int(p.get("offset", 0)), int(p.get("limit", default_limit))


def test_paginate_walks_every_page(monkeypatch, client: LcsClient) -> None:
    total = 25
    seen: list[tuple[int, int]] = []

    def fake_get(url, params=None):
        offset, limit = _served(params)
        seen.append((offset, limit))
        return _paged_payloads(total, offset // limit, limit)

    monkeypatch.setattr(client, "get", fake_get)
    # Explicit limit so 25 records needs three pages.
    items = list(client.paginate("/trades/list/", {"limit": 10}, max_items=1000))

    assert len(items) == total
    assert [i["n"] for i in items] == list(range(total)), "records must not repeat"
    assert [o for o, _ in seen] == [0, 10, 20]


def test_paginate_ignores_a_bogus_next_link(monkeypatch, client: LcsClient) -> None:
    """Live API returned ?limit=5&offset=5 after page 2. That must not rewind."""
    total = 30

    def fake_get(url, params=None):
        offset, limit = _served(params)
        page = _paged_payloads(total, offset // limit, limit)
        if offset >= limit:
            # Exactly what LCS does: a next link that resets to the default size.
            page["next"] = (
                "http://api.localcoinswap.com/api/v2/trades/list/?limit=5&offset=5"
            )
        return page

    monkeypatch.setattr(client, "get", fake_get)
    items = list(client.paginate("/trades/list/", max_items=1000))
    assert len(items) == total
    assert [i["n"] for i in items] == list(range(total))


def test_paginate_terminates_on_repeating_records(monkeypatch, client: LcsClient) -> None:
    """A server ignoring offset must not hang us forever."""
    calls = {"n": 0}

    def fake_get(url, params=None):
        calls["n"] += 1
        if calls["n"] > 200:
            raise AssertionError("pagination did not terminate")
        _, limit = _served(params)
        # Always the same page; only the reported count can stop us.
        return {"count": 1000, "results": [{"n": 0} for _ in range(limit)]}

    monkeypatch.setattr(client, "get", fake_get)
    items = list(client.paginate("/trades/list/", max_items=10_000))
    # page_size is 200 and count is 1000, so exactly five pages then stop.
    expected_pages = 1000 // client.page_size
    assert calls["n"] == expected_pages, "should stop once count is reached"
    assert len(items) == expected_pages * client.page_size


def test_paginate_respects_max_items(monkeypatch, client: LcsClient) -> None:
    def fake_get(url, params=None):
        offset, limit = _served(params)
        return _paged_payloads(10_000, offset // limit, limit)

    monkeypatch.setattr(client, "get", fake_get)
    got = list(client.paginate("/trades/list/", max_items=25))
    assert len(got) == 25


def test_paginate_stops_on_short_final_page(monkeypatch, client: LcsClient) -> None:
    def fake_get(url, params=None):
        _, limit = _served(params)
        # Fewer records than requested: the tail.
        return {"count": 7, "next": "http://x/?limit=5&offset=5",
                "results": [{"n": i} for i in range(7)]}

    monkeypatch.setattr(client, "get", fake_get)
    assert len(list(client.paginate("/trades/list/"))) == 7


def test_paginate_respects_caller_limit_and_offset(monkeypatch, client: LcsClient) -> None:
    seen: list[tuple[int, int]] = []

    def fake_get(url, params=None):
        off, lim = _served(params)
        seen.append((off, lim))
        return _paged_payloads(100, off // lim, lim)

    monkeypatch.setattr(client, "get", fake_get)
    list(client.paginate("/trades/list/", {"limit": 10, "offset": 30}))
    assert seen[0] == (30, 10)


def test_paginate_stops_on_empty_results(monkeypatch, client: LcsClient) -> None:
    monkeypatch.setattr(client, "get", lambda url, params=None: {"count": 5, "results": []})
    assert list(client.paginate("/trades/list/")) == []


def test_paginate_survives_non_dict_payload(monkeypatch, client: LcsClient) -> None:
    monkeypatch.setattr(client, "get", lambda url, params=None: ["not", "a", "dict"])
    assert list(client.paginate("/trades/list/")) == []


def test_paginate_survives_missing_results_key(monkeypatch, client: LcsClient) -> None:
    monkeypatch.setattr(client, "get", lambda url, params=None: {"count": 0})
    assert list(client.paginate("/trades/list/")) == []


def test_paginate_preserves_extra_filters(monkeypatch, client: LcsClient) -> None:
    seen: list[dict] = []

    def fake_get(url, params=None):
        seen.append(dict(params or {}))
        _, limit = _served(params)
        return {"count": 1, "results": [{"n": 0}] * 0 + [{"n": 0}]}

    monkeypatch.setattr(client, "get", fake_get)
    list(client.paginate("/trades/list/", {"stage": "past", "coin_currency": "BTC"}))
    assert seen[0]["stage"] == "past"
    assert seen[0]["coin_currency"] == "BTC"


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


def test_401_raises_auth_error(monkeypatch, client: LcsClient) -> None:
    monkeypatch.setattr(
        client,
        "_client",
        _StubClient(FakeResponse(401, {"message": "Invalid API key."})),
    )
    with pytest.raises(LcsAuthError, match="Invalid API key"):
        client.get("/trades/list/")


def test_403_raises_auth_error(monkeypatch, client: LcsClient) -> None:
    monkeypatch.setattr(
        client,
        "_client",
        _StubClient(FakeResponse(403, {"message": "Forbidden"})),
    )
    with pytest.raises(LcsAuthError):
        client.get("/trades/list/")


def test_429_raises_after_retries(monkeypatch, client: LcsClient) -> None:
    monkeypatch.setattr(
        client, "_client", _StubClient(FakeResponse(429, {}, {"Retry-After": "0"}))
    )
    monkeypatch.setattr("lcs_report.lcs_client.time.sleep", lambda *_: None)
    with pytest.raises(LcsRateLimited):
        client.get("/trades/list/")


def test_400_raises_lcs_error_with_detail(monkeypatch, client: LcsClient) -> None:
    monkeypatch.setattr(
        client,
        "_client",
        _StubClient(FakeResponse(400, {"message": "coin_currency invalid"})),
    )
    with pytest.raises(LcsError, match="coin_currency invalid"):
        client.get("/trades/list/")


def test_500_is_retried_then_raises(monkeypatch, client: LcsClient) -> None:
    monkeypatch.setattr(
        client, "_client", _StubClient(FakeResponse(500, {"message": "boom"}))
    )
    monkeypatch.setattr("lcs_report.lcs_client.time.sleep", lambda *_: None)
    with pytest.raises(LcsError):
        client.get("/trades/list/")


def test_network_error_is_retried_then_raises(monkeypatch, client: LcsClient) -> None:
    class Boom:
        def get(self, *_a, **_k):
            raise httpx.ConnectError("no route to host")

        def close(self) -> None:
            return None

    monkeypatch.setattr(client, "_client", Boom())
    monkeypatch.setattr("lcs_report.lcs_client.time.sleep", lambda *_: None)
    with pytest.raises(LcsError, match="Network error"):
        client.get("/trades/list/")


def test_non_json_response_raises(monkeypatch, client: LcsClient) -> None:
    monkeypatch.setattr(client, "_client", _StubClient(FakeResponse(200, None)))
    with pytest.raises(LcsError, match="Non-JSON"):
        client.get("/trades/list/")


def test_no_key_still_works_for_public_endpoints(monkeypatch) -> None:
    """The spec wrongly marks every endpoint as authenticated."""
    c = LcsClient(None, min_interval=0.0, timeout=1.0)
    try:
        monkeypatch.setattr(
            c, "_client", _StubClient(FakeResponse(200, {"count": 0, "results": []}))
        )
        assert c.get("/currencies/active-cryptos/") == {"count": 0, "results": []}
        assert c._headers() == {}
    finally:
        c.close()


def test_auth_header_format() -> None:
    c = LcsClient("lcs_a1b2c3d4_secret", min_interval=0.0)
    try:
        assert c._headers() == {"Authorization": "ApiKey lcs_a1b2c3d4_secret"}
    finally:
        c.close()


class _StubClient:
    """Stands in for the internal httpx client."""

    def __init__(self, response: FakeResponse):
        self._response = response

    def get(self, url, params=None, headers=None):
        return self._response

    def close(self) -> None:
        return None