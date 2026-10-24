"""Point-in-time FX rates, layered over the daily reference rates.

Two sources, in order of preference:

* **Intraday** -- hourly bars from Yahoo's chart API for the recent window.
  A genuine point-in-time quote: the bar at or immediately before the trade.
* **Daily** -- the ECB reference rate via Frankfurter, which is what everything
  beyond the intraday window uses.

Why the daily leg is also shifted by a publication lag
------------------------------------------------------
The ECB publishes *day D*'s reference rate at about 16:00 CET (14:00 UTC). A
trade that happens at 02:00 UTC on day D therefore happens roughly twelve hours
*before* that day's rate exists. The most recent rate actually knowable at that
moment is day D-1's. This module applies that shift, so a "daily" lookup is
still point-in-time honest rather than using a rate from the future.

Yahoo is rate-limited and occasionally unavailable, so every failure falls back
to the daily path. The intraday layer is an improvement, never a dependency.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger(__name__)

YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart"
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

# The ECB publishes the target day's rate at ~16:00 CET.
ECB_PUBLISH_UTC_HOUR = 14

# Yahoo's own retention for hourly bars is about two years; asking for more
# returns an error, so the window is bounded rather than discovered.
DEFAULT_WINDOW_DAYS = 730

# Yahoo quotes most of these directly against USD.
YAHOO_SYMBOLS = {
    "IDR": "USDIDR=X",
    "SGD": "USDSGD=X",
    "MYR": "USDMYR=X",
    "PHP": "USDPHP=X",
    "THB": "USDTHB=X",
    "VND": "USDVND=X",
    "EUR": "EURUSD=X",   # inverse: handled below
    "GBP": "GBPUSD=X",   # inverse
    "JPY": "USDJPY=X",
    "AUD": "AUDUSD=X",   # inverse
    "CNY": "USDCNY=X",
    "HKD": "USDHKD=X",
    "KRW": "USDKRW=X",
    "CHF": "USDCHF=X",
    "NZD": "NZDUSD=X",  # inverse
}


def invert(quote: str) -> bool:
    return quote in {"EUR", "GBP", "AUD", "NZD"}


class IntradayStore:
    """Hourly bars, cached to SQLite so a fetch happens once."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS intraday_rates (
                series  TEXT NOT NULL,
                ts      INTEGER NOT NULL,
                rate    TEXT NOT NULL,
                PRIMARY KEY (series, ts)
            )
            """
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def put(self, series: str, bars: dict[int, Decimal]) -> int:
        if not bars:
            return 0
        self._conn.executemany(
            "INSERT OR REPLACE INTO intraday_rates (series, ts, rate) VALUES (?, ?, ?)",
            [(series, ts, str(r)) for ts, r in bars.items()],
        )
        self._conn.commit()
        return len(bars)

    def rate_at(self, series: str, ts: int) -> tuple[int, Decimal] | None:
        """The bar at or immediately before ``ts``."""
        row = self._conn.execute(
            "SELECT ts, rate FROM intraday_rates WHERE series = ? AND ts <= ? "
            "ORDER BY ts DESC LIMIT 1",
            (series, ts),
        ).fetchone()
        return (int(row[0]), Decimal(row[1])) if row else None

    def bounds(self, series: str) -> tuple[int | None, int | None]:
        row = self._conn.execute(
            "SELECT MIN(ts), MAX(ts) FROM intraday_rates WHERE series = ?", (series,)
        ).fetchone()
        return (int(row[0]), int(row[1])) if row and row[0] is not None else (None, None)

    def count(self, series: str) -> int:
        return self._conn.execute(
            "SELECT COUNT(*) FROM intraday_rates WHERE series = ?", (series,)
        ).fetchone()[0]

    def series_present(self) -> set[str]:
        return {
            r[0] for r in self._conn.execute("SELECT DISTINCT series FROM intraday_rates")
        }


@dataclass
class IntradayState:
    series: dict[str, dict[str, Any]] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"series": self.series, "errors": self.errors}


class IntradayClient:
    """Fetches and caches hourly bars, and resolves a point-in-time rate."""

    def __init__(
        self,
        store: IntradayStore,
        *,
        base_currency: str = "USD",
        window_days: int = DEFAULT_WINDOW_DAYS,
        timeout: float = 30.0,
    ) -> None:
        self.store = store
        self.base_currency = base_currency.upper()
        self.window_days = window_days
        self.timeout = timeout
        self.state = IntradayState()

    def series(self, currency: str) -> str:
        return f"{self.base_currency}_{currency.upper()}"

    def _symbol(self, currency: str) -> str | None:
        if self.base_currency != "USD":
            return None  # only USD-based feeds are wired up
        return YAHOO_SYMBOLS.get(currency.upper())

    def prime(self, currencies: set[str]) -> None:
        """Ensure hourly bars exist for the currencies that can have them."""
        now = int(time.time())
        want_from = now - self.window_days * 86400

        for currency in sorted(c.upper() for c in currencies):
            series = self.series(currency)
            if series in self.store.series_present():
                lo, _ = self.store.bounds(series)
                if lo is not None and lo <= want_from + 86400:
                    continue  # already covers the window

            symbol = self._symbol(currency)
            if symbol is None:
                self.state.errors[currency] = f"no Yahoo symbol for {currency}"
                continue

            bars = self._fetch(symbol, want_from, now)
            if bars is None:
                continue
            if invert(currency):
                bars = {
                    ts: (Decimal(1) / r if r else r) for ts, r in bars.items()
                }
            written = self.store.put(series, bars)
            lo, hi = self.store.bounds(series)
            log.info(
                "intraday %s: %d hourly bars %s -> %s",
                series, written,
                datetime.fromtimestamp(lo, timezone.utc).date() if lo else "?",
                datetime.fromtimestamp(hi, timezone.utc).date() if hi else "?",
            )
            self.state.series[series] = {
                "bars": written,
                "from": lo, "to": hi,
            }

    def _fetch(self, symbol: str, start: int, end: int) -> dict[int, Decimal] | None:
        url = f"{YAHOO_CHART}/{symbol}"
        params = {"period1": start, "period2": end, "interval": "1h"}
        for attempt in range(3):
            try:
                with httpx.Client(timeout=self.timeout) as client:
                    resp = client.get(url, params=params, headers={"User-Agent": UA})
                if resp.status_code == 429:
                    wait = 5 * (attempt + 1)
                    log.info("Yahoo rate limited; waiting %ds", wait)
                    time.sleep(wait)
                    continue
                resp.raise_for_status()
                payload = resp.json()
                break
            except Exception as exc:  # noqa: BLE001 - fall back to daily
                log.info("Yahoo fetch failed for %s: %s", symbol, exc)
                time.sleep(2 * (attempt + 1))
        else:
            self.state.errors[symbol] = "Yahoo unavailable; using daily rates"
            return None

        results = ((payload.get("chart") or {}).get("result")) or []
        if not results:
            self.state.errors[symbol] = "empty response; using daily rates"
            return None

        stamps = results[0].get("timestamp") or []
        closes = ((results[0].get("indicators") or {}).get("quote") or [{}])[0].get(
            "close"
        ) or []
        bars: dict[int, Decimal] = {}
        for ts, close in zip(stamps, closes):
            if close is None:
                continue
            try:
                bars[int(ts)] = Decimal(str(close))
            except Exception:  # noqa: BLE001 - skip a bad bar
                continue
        if not bars:
            self.state.errors[symbol] = "no usable bars; using daily rates"
            return None
        return bars

    def rate_at(self, currency: str, ts: int) -> tuple[int, Decimal] | None:
        series = self.series(currency)
        if series not in self.store.series_present():
            return None
        return self.store.rate_at(series, ts)


def ebp_publish_moment(day: date) -> int:
    """Unix seconds at which the ECB rate for ``day`` becomes public."""
    base = datetime(day.year, day.month, day.day, ECB_PUBLISH_UTC_HOUR, tzinfo=timezone.utc)
    return int(base.timestamp())


def point_in_time_day(ts: int) -> date:
    return datetime.fromtimestamp(ts, timezone.utc).date()


def knowledge_date(ts: int) -> date:
    """The most recent date whose ECB rate was published by ``ts``.

    A trade before 14:00 UTC cannot know its own day's rate, so it falls back to
    the previous business day.
    """
    day = point_in_time_day(ts)
    if ts < ebp_publish_moment(day):
        return day - timedelta(days=1)
    return day


__all__ = [
    "DEFAULT_WINDOW_DAYS",
    "IntradayClient",
    "IntradayState",
    "IntradayStore",
    "YAHOO_SYMBOLS",
    "knowledge_date",
    "point_in_time_day",
]
