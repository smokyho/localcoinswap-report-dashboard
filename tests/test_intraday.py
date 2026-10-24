"""Tests for the point-in-time FX layer.

Yahoo is rate-limited and sometimes down, so every path here is exercised against
a stubbed feed. The tests pin the two rules that matter:

* an intraday bar is used when one exists at or before the trade
* otherwise the daily rate is used, shifted by the ECB publication lag, because
  the ECB publishes day D's rate at ~16:00 CET and a morning trade cannot
  know it
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lcs_report.intraday import (  # noqa: E402
    ECB_PUBLISH_UTC_HOUR,
    IntradayClient,
    IntradayStore,
    knowledge_date,
    point_in_time_day,
)
from lcs_report.valuation import (  # noqa: E402
    apply_fx,
    normalise_trade,
    summarise,
)


# --------------------------------------------------------------------------
# The publication-lag rule
# --------------------------------------------------------------------------


def _ts(y: int, m: int, d: int, hh: int, mm: int = 0) -> int:
    return int(datetime(y, m, d, hh, mm, tzinfo=timezone.utc).timestamp())


def test_rate_for_today_is_only_known_after_1600_cet() -> None:
    before = _ts(2026, 6, 15, ECB_PUBLISH_UTC_HOUR - 1)
    after = _ts(2026, 6, 15, ECB_PUBLISH_UTC_HOUR)
    assert knowledge_date(before) == date(2026, 6, 14)
    assert knowledge_date(after) == date(2026, 6, 15)


def test_indonesian_morning_trades_use_the_prior_day() -> None:
    """09:00 WIB is 02:00 UTC, twelve hours before the ECB publishes."""
    assert knowledge_date(_ts(2026, 6, 15, 2)) == date(2026, 6, 14)
    assert knowledge_date(_ts(2026, 6, 15, 10)) == date(2026, 6, 14)
    assert knowledge_date(_ts(2026, 6, 15, 13)) == date(2026, 6, 14)


def test_evening_trades_use_the_same_day() -> None:
    assert knowledge_date(_ts(2026, 6, 15, 15)) == date(2026, 6, 15)
    assert knowledge_date(_ts(2026, 6, 15, 23)) == date(2026, 6, 15)


def test_point_in_time_day_is_just_the_utc_date() -> None:
    assert point_in_time_day(_ts(2026, 6, 15, 2)) == date(2026, 6, 15)


# --------------------------------------------------------------------------
# Store
# --------------------------------------------------------------------------


@pytest.fixture()
def store(tmp_path: Path) -> IntradayStore:
    return IntradayStore(tmp_path / "intraday.db")


def test_store_returns_the_bar_at_or_before_the_trade(store: IntradayStore) -> None:
    store.put("USD_IDR", {1_000: Decimal("16000"), 2_000: Decimal("16100")})
    ts, rate = store.rate_at("USD_IDR", 1_500)
    assert (ts, rate) == (1_000, Decimal("16000"))
    ts, rate = store.rate_at("USD_IDR", 9_999)
    assert (ts, rate) == (2_000, Decimal("16100"))


def test_store_returns_nothing_before_the_first_bar(store: IntradayStore) -> None:
    store.put("USD_IDR", {5_000: Decimal("16000")})
    assert store.rate_at("USD_IDR", 1_000) is None


def test_store_keeps_series_apart(store: IntradayStore) -> None:
    store.put("USD_IDR", {1_000: Decimal("16000")})
    store.put("USD_SGD", {1_000: Decimal("1.34")})
    assert store.rate_at("USD_SGD", 1_000)[1] == Decimal("1.34")
    assert store.series_present() == {"USD_IDR", "USD_SGD"}


# --------------------------------------------------------------------------
# Client, against a stubbed feed
# --------------------------------------------------------------------------


class _Resp:
    def __init__(self, payload: Any, status: int = 200) -> None:
        self._payload = payload
        self.status_code = status

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            import httpx

            raise httpx.HTTPStatusError("boom", request=None, response=None)

    def json(self) -> Any:
        return self._payload


def _yahoo_payload(pairs: list[tuple[int, float]]) -> dict:
    return {
        "chart": {
            "result": [
                {
                    "timestamp": [p[0] for p in pairs],
                    "indicators": {"quote": [{"close": [p[1] for p in pairs]}]},
                }
            ]
        }
    }


def _patch_yahoo(monkeypatch, handler) -> None:
    import lcs_report.intraday as mod

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, params=None, headers=None):
            return handler(url, params or {})

    monkeypatch.setattr(mod.httpx, "Client", _FakeClient)
    monkeypatch.setattr(mod.time, "sleep", lambda *_: None)


def test_prime_stores_bars_and_is_idempotent(tmp_path, monkeypatch) -> None:
    calls: list[str] = []

    def handler(url, params):
        calls.append(url)
        return _Resp(_yahoo_payload([(params["period1"], 16257), (params["period1"] + 3600, 16300)]))

    _patch_yahoo(monkeypatch, handler)
    store = IntradayStore(tmp_path / "i.db")
    c = IntradayClient(store)
    c.prime({"IDR"})
    c.prime({"IDR"})
    assert len(calls) == 1, "a cached series must not be refetched"
    assert store.count("USD_IDR") == 2


def test_yahoo_failure_falls_back_to_daily(tmp_path, monkeypatch) -> None:
    def handler(url, params):
        return _Resp({"chart": {"result": None, "error": "rate limited"}}, status=429)

    _patch_yahoo(monkeypatch, handler)
    store = IntradayStore(tmp_path / "i.db")
    c = IntradayClient(store)
    c.prime({"IDR"})
    assert store.count("USD_IDR") == 0
    assert c.state.errors, "the failure must be recorded, not silent"
    assert c.rate_at("IDR", 1_000) is None


def test_inverse_pairs_are_inverted(tmp_path, monkeypatch) -> None:
    """EURUSD=X is quoted the other way, so it must be reciprocated."""

    def handler(url, params):
        return _Resp(_yahoo_payload([(params["period1"], 1.09)]))

    _patch_yahoo(monkeypatch, handler)
    store = IntradayStore(tmp_path / "i.db")
    c = IntradayClient(store)
    c.prime({"EUR"})
    assert store.count("USD_EUR") == 1
    lo, _ = store.bounds("USD_EUR")
    _, rate = store.rate_at("USD_EUR", lo)
    assert rate == Decimal("1") / Decimal("1.09")


def test_unknown_currency_is_not_fetched(tmp_path, monkeypatch) -> None:
    def handler(url, params):
        raise AssertionError("must not fetch a pair with no symbol")

    _patch_yahoo(monkeypatch, handler)
    store = IntradayStore(tmp_path / "i.db")
    IntradayClient(store).prime({"XYZ"})
    assert store.count("USD_XYZ") == 0


# --------------------------------------------------------------------------
# End to end: intraday preferred, daily otherwise
# --------------------------------------------------------------------------


def _trade(ts: int, **over) -> dict[str, Any]:
    base = {
        "status": "COMPLETED",
        "uuid": "u",
        "buyer": {"username": "me"},
        "seller": {"username": "x"},
        "fiat_amount": "1500000",
        "coin_amount": "10",
        "time_created": ts,
        "coin_currency": {"symbol": "BTC", "slug": "BTC"},
        "fiat_currency": {"symbol": "IDR"},
        "offer": {"trading_type": {"slug": "buy"}},
        "crypto_in_usd": "90",
        "fees": "0.1",
    }
    base.update(over)
    return base


def _valued(ts: int, intraday: IntradayClient, **over):
    t = normalise_trade(_trade(ts, **over), username="me")
    t.trade_date = point_in_time_day(ts)

    # The resolver mirrors what the service wires up: prefer an hourly bar at or
    # before the trade, otherwise the daily rate with the publication lag.
    when = ts

    def resolve(trade_ts: int, currency: str):
        got = intraday.rate_at(currency, trade_ts)
        if got is not None:
            return (
                datetime.fromtimestamp(got[0], timezone.utc).date(),
                got[1],
                False,
                "intraday",
            )
        day = knowledge_date(trade_ts)
        return day, Decimal("16000"), True, "daily"

    return apply_fx(
        [t],
        rate_lookup=lambda _d, c: resolve(when, c),
        base_currency="USD",
    )[0]


def test_intraday_is_used_when_available(tmp_path) -> None:
    store = IntradayStore(tmp_path / "i.db")
    c = IntradayClient(store)
    store.put("USD_IDR", {1_000: Decimal("16000"), 2_000: Decimal("17000")})
    v = _valued(1_500, c)
    assert v.fx_rate == Decimal("16000"), "must use the bar at or before the trade"
    assert v.fx_source == "intraday"


def test_daily_is_used_outside_the_window(tmp_path) -> None:
    store = IntradayStore(tmp_path / "i.db")
    c = IntradayClient(store)
    store.put("USD_IDR", {9_000_000: Decimal("16000")})
    v = _valued(1_500, c)
    assert v.fx_rate == Decimal("16000")
    assert v.fx_source == "daily"


def test_daily_leg_respects_the_publication_lag(tmp_path) -> None:
    """A morning trade cannot use its own day's ECB rate."""
    store = IntradayStore(tmp_path / "i.db")
    c = IntradayClient(store)
    morning = _ts(2026, 6, 15, 2)   # 09:00 WIB, before publication
    v = _valued(morning, c)
    assert v.fx_date == date(2026, 6, 14)
    assert v.fx_source == "daily"
