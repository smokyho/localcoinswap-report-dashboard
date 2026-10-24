"""Historical FX rates, cached locally.

The LCS API freezes the crypto price in USD per trade (``cryptoInUsd``), so
converting to IDR needs exactly one external series: USD -> IDR by date.

Source: Frankfurter (ECB reference rates). Free, no API key, full history.
Business days only -- weekends and ECB holidays are simply absent from the
response, so every lookup snaps back to the most recent available prior date.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from datetime import date, datetime, timedelta, timezone, tzinfo
from decimal import Decimal
from pathlib import Path

import httpx

from .config import FRANKFURTER_BASE

log = logging.getLogger(__name__)

# How far back to walk when a target date has no published rate. Ten days
# comfortably covers a long weekend plus an ECB holiday cluster.
MAX_SNAP_DAYS = 10


class FxError(RuntimeError):
    """Raised when a rate cannot be resolved."""


class FxStore:
    """SQLite-backed store of ``date -> rate`` pairs.

    Caching matters for two reasons: it keeps the report reproducible (a rate
    that was correct last month does not silently change today), and it avoids
    re-fetching ranges on every page load.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS fx_rates (
                series  TEXT NOT NULL,
                day     TEXT NOT NULL,
                rate    TEXT NOT NULL,
                PRIMARY KEY (series, day)
            )
            """
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def put_many(self, series: str, rates: dict[str, str]) -> int:
        if not rates:
            return 0
        self._conn.executemany(
            "INSERT OR REPLACE INTO fx_rates (series, day, rate) VALUES (?, ?, ?)",
            [(series, day, str(rate)) for day, rate in rates.items()],
        )
        self._conn.commit()
        return len(rates)

    def get(self, series: str, day: str) -> Decimal | None:
        row = self._conn.execute(
            "SELECT rate FROM fx_rates WHERE series = ? AND day = ?", (series, day)
        ).fetchone()
        return Decimal(row[0]) if row else None

    def latest(self, series: str) -> tuple[str, Decimal] | None:
        row = self._conn.execute(
            "SELECT day, rate FROM fx_rates WHERE series = ? ORDER BY day DESC LIMIT 1",
            (series,),
        ).fetchone()
        return (row[0], Decimal(row[1])) if row else None

    def coverage(self, series: str) -> tuple[str | None, str | None]:
        row = self._conn.execute(
            "SELECT MIN(day), MAX(day) FROM fx_rates WHERE series = ?", (series,)
        ).fetchone()
        return (row[0], row[1]) if row else (None, None)

    def _cached_range(self, series: str, start: date, end: date) -> set[str]:
        rows = self._conn.execute(
            "SELECT day FROM fx_rates WHERE series = ? AND day BETWEEN ? AND ?",
            (series, start.isoformat(), end.isoformat()),
        ).fetchall()
        return {r[0] for r in rows}

    def snap_back(self, series: str, target: date) -> tuple[date, Decimal]:
        """Most recent rate on or before ``target``.

        Frankfurter publishes on business days only, so a Saturday trade
        resolves to Friday's rate. Walking back day by day is correct here and
        keeps the chosen date visible to the report so the user can see a
        weekend rate was substituted.
        """
        for offset in range(MAX_SNAP_DAYS + 1):
            day = target - timedelta(days=offset)
            rate = self.get(series, day.isoformat())
            if rate is not None:
                return day, rate
        raise FxError(
            f"No {series} rate found within {MAX_SNAP_DAYS} days before {target}. "
            "Fetch a wider range first."
        )


class FrankfurterClient:
    """Fetches and caches USD-based FX series from Frankfurter."""

    def __init__(
        self,
        store: FxStore,
        *,
        base_url: str = FRANKFURTER_BASE,
        base_currency: str = "USD",
        target_currency: str = "IDR",
        timeout: float = 30.0,
    ) -> None:
        self.store = store
        self.base_url = base_url.rstrip("/")
        self.base_currency = base_currency.upper()
        self.target_currency = target_currency.upper()
        self.timeout = timeout
        # When True, a lookup that is not already cached fails fast instead of
        # issuing a request. Used once a whole span has been prefetched, so a
        # report over thousands of trades cannot degenerate into one HTTP call
        # per trade.
        self.offline = False

    @property
    def series(self) -> str:
        return f"{self.base_currency}_{self.target_currency}"

    @property
    def target(self) -> str:
        return self.target_currency

    def prime(self, days: list[date]) -> None:
        """Cache every rate needed for ``days`` in a single request.

        Without this, a report covering several years of trades would issue one
        range request per distinct trade date.
        """
        if not days:
            return
        lo = min(days) - timedelta(days=MAX_SNAP_DAYS)
        hi = max(days) + timedelta(days=2)
        self.ensure_range(lo, hi)

    def ensure_range(self, start: date, end: date) -> None:
        """Make sure ``start..end`` is cached.

        Decides coverage by comparing the *bounds* of what is cached against
        what is needed, not by counting days. Counting is wrong here: a
        perfectly cached span still has gaps at weekends and holidays, so a
        day-count heuristic would refetch on every call.
        """
        if end < start:
            raise ValueError(f"end {end} precedes start {start}")

        cached_from, cached_to = self.store.coverage(self.series)
        if cached_from and cached_to:
            if (
                date.fromisoformat(cached_from) <= start
                and date.fromisoformat(cached_to) >= end
            ):
                return

        self._fetch_range(start, end, replace=True)

    def _fetch_range(self, start: date, end: date, *, replace: bool) -> int:
        url = f"{self.base_url}/{start.isoformat()}..{end.isoformat()}"
        params = {"base": self.base_currency, "symbols": self.target_currency}
        log.info("fetching FX range %s..%s", start, end)

        last_error: Exception | None = None
        for attempt in range(3):
            try:
                with httpx.Client(timeout=self.timeout) as client:
                    resp = client.get(url, params=params)
                resp.raise_for_status()
                payload = resp.json()
                break
            except Exception as exc:  # noqa: BLE001 - retried below
                last_error = exc
                if attempt == 2:
                    raise FxError(
                        f"Failed to fetch FX rates {start}..{end}: {exc}"
                    ) from exc
                time.sleep(1.5 * (attempt + 1))
        else:  # pragma: no cover - loop always breaks or raises
            raise FxError(str(last_error))

        rates = payload.get("rates") or {}
        if not rates:
            raise FxError(
                f"Frankfurter returned no rates for {start}..{end}. "
                "The range may be entirely non-publication days."
            )

        # The key inside each date object is whatever symbol was requested, so
        # match case-insensitively rather than assuming one currency.
        wanted = self.target_currency.upper()
        cleaned: dict[str, str] = {}
        for day, body in rates.items():
            if not isinstance(body, dict):
                continue
            value = None
            for key, candidate in body.items():
                if key.upper() == wanted:
                    value = candidate
                    break
            if value is not None:
                cleaned[day] = str(Decimal(str(value)))

        written = self.store.put_many(self.series, cleaned)
        log.info("cached %d FX rates (%s)", written, self.series)
        return written

    def rate_for(self, target: date) -> tuple[date, Decimal, bool]:
        """Return ``(effective_date, rate, was_snapped)`` for ``target``."""
        if not self.offline:
            self.ensure_range(
                target - timedelta(days=MAX_SNAP_DAYS), target + timedelta(days=2)
            )
        # In offline mode, snap_back either finds a cached prior business day or
        # raises. Do not short-circuit here: a Sunday has no rate of its own, but
        # Friday does, and only snap_back knows to look that far back.
        effective, rate = self.store.snap_back(self.series, target)
        return effective, rate, effective != target

    def current_rate(self) -> tuple[date, Decimal]:
        today = datetime.now(timezone.utc).date()
        self.ensure_range(today - timedelta(days=MAX_SNAP_DAYS), today)
        latest = self.store.latest(self.series)
        if latest is None:
            raise FxError("No FX rates cached at all.")
        return date.fromisoformat(latest[0]), latest[1]


def resolve_trade_date(
    timestamp: int | float | None, tz: tzinfo
) -> date | None:
    """Map a unix timestamp to a calendar day in the configured timezone.

    Near midnight this can land a trade on a different day than UTC would,
    which in turn can select a different FX rate -- so the zone is explicit
    rather than assumed.
    """
    if timestamp is None:
        return None
    try:
        moment = datetime.fromtimestamp(float(timestamp), tz=timezone.utc)
    except (OverflowError, OSError, ValueError, TypeError):
        return None
    return moment.astimezone(tz).date()


__all__ = [
    "FrankfurterClient",
    "FxError",
    "FxStore",
    "MAX_SNAP_DAYS",
    "resolve_trade_date",
]


class FxDesk:
    """Serves historical rates for several currency pairs on demand.

    A report normally needs one series -- the account's quote currency -- but a
    single account can hold trades settled in more than one. Each additional
    target gets its own :class:`FrankfurterClient` over the same SQLite store,
    so nothing is duplicated on disk and a pair is only fetched if a trade
    actually needs it.

    The base currency short-circuits: no trade quoted in USD needs a rate,
    because the USD side is already in USD.
    """

    def __init__(
        self,
        store: FxStore,
        *,
        base_currency: str = "USD",
        base_url: str = FRANKFURTER_BASE,
        timeout: float = 30.0,
    ) -> None:
        self.store = store
        self.base_currency = base_currency.upper()
        self.base_url = base_url
        self.timeout = timeout
        self._clients: dict[str, FrankfurterClient] = {}
        self.errors: list[str] = []

    def client(self, target: str) -> FrankfurterClient:
        key = target.upper()
        got = self._clients.get(key)
        if got is None:
            got = FrankfurterClient(
                self.store,
                base_url=self.base_url,
                base_currency=self.base_currency,
                target_currency=key,
                timeout=self.timeout,
            )
            self._clients[key] = got
        return got

    def series(self, target: str) -> str:
        return f"{self.base_currency}_{target.upper()}"

    def prime(self, days: list[date], target: str) -> None:
        """Cache every rate ``days`` needs for one target, in one request."""
        if not days:
            return
        client = self.client(target)
        lo = min(days) - timedelta(days=MAX_SNAP_DAYS)
        hi = max(days) + timedelta(days=2)
        client.ensure_range(lo, hi)

    def prime_all(self, days_by_target: dict[str, list[date]]) -> None:
        for target, days in days_by_target.items():
            if not days:
                continue
            try:
                self.prime(days, target)
            except FxError as exc:
                note = f"FX history for {target} incomplete: {exc}"
                self.errors.append(note)
                log.info("%s", note)

    def rate_for(self, day: date, target: str) -> tuple[date, Decimal, bool]:
        if not self.client(target).offline:
            self.client(target).ensure_range(
                day - timedelta(days=MAX_SNAP_DAYS), day + timedelta(days=2)
            )
        effective, rate = self.store.snap_back(self.series(target), day)
        return effective, rate, effective != day

    def go_offline(self, flag: bool = True) -> None:
        for client in self._clients.values():
            client.offline = flag

    def coverage(self, target: str) -> tuple[str | None, str | None]:
        return self.store.coverage(self.series(target))


__all__ = ["FxDesk", "FxError", "FxStore", "MAX_SNAP_DAYS", "resolve_trade_date"]
