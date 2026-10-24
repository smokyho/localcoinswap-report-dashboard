"""Orchestration: pull trades, value them, and answer report queries.

Split from the FastAPI layer so the pipeline can be exercised in tests without
a server or network access.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, time as clock, timezone
from decimal import Decimal
from typing import Any

from .config import Settings
from .enrich import Enricher
from .fx import FxDesk, FxError, FxStore
from .lcs_client import LcsAuthError, LcsClient, LcsError
from .intraday import IntradayClient, IntradayStore, knowledge_date
from .refresher import TradeRefresher, TradeStore
from .valuation import (
    SETTLED_STATUSES,
    Summary,
    UNSETTLED_STATUSES,
    ValuedTrade,
    apply_fx,
    normalise_trade,
    resolve_date,
    settled,
    summarise,
)

log = logging.getLogger(__name__)

TRADE_CACHE_TTL = 300.0
CURRENT_FX_TTL = 1800.0


class ReportError(RuntimeError):
    """A failure whose message is safe to show in the UI."""


@dataclass
class TradeFilters:
    date_from: date | None = None
    date_to: date | None = None
    coin: str = ""
    direction: str = ""
    status: str = ""
    payment: str = ""
    search: str = ""
    quote_currency: str = ""
    settled_only: bool = True

    def as_query(self) -> dict[str, str]:
        bits = []
        if self.date_from or self.date_to:
            bits.append(f"dates {self.date_from or 'start'}..{self.date_to or 'now'}")
        if self.coin:
            bits.append(f"coin {self.coin}")
        if self.direction:
            bits.append(self.direction)
        if self.quote_currency:
            bits.append(f"quote {self.quote_currency}")
        if self.settled_only:
            bits.append("settled only")
        return ", ".join(bits)


@dataclass
class ReportData:
    trades: list[ValuedTrade]
    summary: Summary
    total_available: int
    username: str | None
    fx_rate: Decimal | None
    fx_date: date | None
    fx_coverage: tuple[str | None, str | None]
    ytd: Any = None
    prior_year: Any = None
    service: Any = None
    warnings: list[str] = field(default_factory=list)
    stale: bool = False
    enriched: int = 0
    total: int = 0
    filters: TradeFilters | None = None
    display_currency: str = ""

    @property
    def refresher(self) -> Any:
        return self.service.refresher

    @property
    def data_age(self) -> float | None:
        return self.service.data_age()

    @property
    def enrichment_complete(self) -> bool:
        return self.total > 0 and self.enriched >= self.total


def _year_to_date(summary: Any, year: int) -> Any:
    """Roll up every month of ``year`` seen so far into one YTD period."""
    months = [p for p in summary.monthly if p.key.startswith(f"{year:04d}-")]
    return _roll_up(f"{year:04d}", f"YTD {year}", months)


def _full_year(summary: Any, year: int) -> Any:
    """Roll up every month of ``year`` for prior-year comparison."""
    months = [p for p in summary.monthly if p.key.startswith(f"{year:04d}-")]
    return _roll_up(f"{year:04d}", str(year), months)


def _roll_up(key: str, label: str, months: list[Any]) -> Any:
    """Sum months while keeping each currency in its own bucket."""
    from .valuation import CurrencyTotals, Period

    total = Period(key=key, label=label)
    for m in months:
        total.trades += m.trades
        for cur, src in m.by_currency.items():
            dst = total.bucket(cur)
            dst.trades += src.trades
            for name in ("cash_in", "cash_out", "cost", "market_value", "profit"):
                setattr(dst, name, getattr(dst, name) + getattr(src, name))
        if m.display_totals is not None:
            if total.display_totals is None:
                total.display_totals = CurrencyTotals(currency=m.display_totals.currency)
            d_dst = total.display_totals
            d_src = m.display_totals
            d_dst.trades += d_src.trades
            for name in ("cash_in", "cash_out", "cost", "market_value", "profit"):
                setattr(d_dst, name, getattr(d_dst, name) + getattr(d_src, name))
    total.finalise()
    return total


class ReportService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.intraday_enabled = settings.intraday_enabled
        self._last_quotes: list[Any] = []
        self.fx_store = FxStore(settings.fx_cache_path)
        # One desk, many pairs: a report needs a rate per quote currency that
        # actually appears in the trades.
        self.fx = FxDesk(
            self.fx_store,
            base_currency=settings.base_currency,
            timeout=settings.request_timeout,
        )
        self.enricher = Enricher(settings)
        self._lock = threading.Lock()
        self.store = TradeStore(settings.trades_cache_path)
        # Hourly bars are an improvement layer; the daily desk always works.
        self.intraday = IntradayClient(
            IntradayStore(settings.fx_cache_path.with_name("intraday.db")),
            base_currency=settings.base_currency,
        )
        self.refresher = TradeRefresher(
            self.store,
            lambda: self._client(),
            ladder=settings.poll_ladder,
            window=settings.refresh_window,
            max_items=settings.max_trades,
            on_change=self._on_trades_changed,
        )
        self._username: str | None = settings.username
        self._current_fx: tuple[date, Decimal] | None = None
        self._current_fx_at: float = 0.0
        self._current_fx_map: dict[str, tuple[float, tuple[date, Decimal]]] = {}

    def close(self) -> None:
        self.refresher.stop()
        self.intraday.store.close()
        self.fx_store.close()

    def start_background(self) -> bool:
        """Begin checking LCS for new trades. Safe to call more than once."""
        started = self.refresher.start()
        # Automatically queue pricing for any trades missing per-trade USD price
        if self.settings.has_api_key:
            missing = [
                u for u in self.store.uuids()
                if not (self.enricher.detail(u) or {}).get("crypto_in_usd")
            ]
            if missing and not self.enricher.progress.running:
                log.info("auto-pricing %d trade(s) on startup", len(missing))
                self.enricher.start(missing)
        return started

    def prime_intraday(self) -> None:
        """Fetch hourly bars for every quote currency seen so far.

        Called from the background loop, never from a request: Yahoo takes about
        thirty seconds for a two-year window and rate-limits aggressively, so
        putting it on the request path would make page loads unpredictable. The
        store persists, so this is a one-off per series.
        """
        if not self.intraday_enabled:
            return
        currencies = {t.quote_currency for t in self._last_quotes if t.quote_currency}
        if not currencies:
            return
        try:
            self.intraday.prime(currencies)
            self.intraday.state.errors.clear()
            log.info("intraday rates ready for %s", sorted(currencies))
        except Exception as exc:  # noqa: BLE001 - daily rates still work
            log.info("intraday prime failed: %s", exc)
            self.intraday.state.errors["*"] = str(exc)

    def _on_trades_changed(self, result) -> None:
        """Queue per-trade pricing for anything that arrived without it."""
        self.prime_intraday()
        missing = [
            u for u in self.store.uuids()
            if not (self.enricher.detail(u) or {}).get("crypto_in_usd")
        ]
        if missing:
            log.info("pricing %d newly arrived trade(s)", len(missing))
            self.enricher.start(missing)

    # -- data sources ---------------------------------------------------

    def _client(self) -> LcsClient:
        return LcsClient(
            self.settings.api_key,
            page_size=min(100, self.settings.page_size),
            min_interval=self.settings.min_interval_seconds,
            timeout=self.settings.request_timeout,
        )

    def fetch_trades(self, *, refresh: bool = False) -> tuple[list[dict[str, Any]], bool]:
        """Trades from the store, topping it up if the background loop is idle.

        A page request should never wait on the network: the background loop
        keeps this current. The synchronous path exists for first paint and for
        when the loop is not running.
        """
        trades = self.store.all()
        age = self.store.age()
        is_stale = age is not None and age > TRADE_CACHE_TTL
        if trades and not refresh:
            if is_stale and self.refresher.state.running:
                self.refresher.wake()
            return trades, is_stale

        if not self.refresher.state.running:
            # No loop to wait for, so do the work here.
            result = self.refresher.run_once(full=not trades)
            trades = self.store.all()
            if result.error:
                raise ReportError(f"Could not read trades from LCS: {result.error}")
            return trades, False

        if not self.settings.has_api_key:
            raise ReportError(
                "LCS_API_KEY is not set. Create a key at "
                "localcoinswap.com/preferences/api-token and add it to .env."
            )

        result = self.refresher.run_once(full=True)
        if result.error:
            raise ReportError(f"Could not read trades from LCS: {result.error}")
        return self.store.all(), False

    def data_age(self) -> float | None:
        """Seconds since the trade list was last confirmed against LCS."""
        if self.refresher and self.refresher.state and self.refresher.state.last_check:
            now = time.time()
            store_age = self.store.age()
            refresher_age = max(0.0, now - self.refresher.state.last_check)
            if store_age is None:
                return refresher_age
            return min(store_age, refresher_age)
        return self.store.age()

    def _resolve_username(self, client: LcsClient) -> str | None:
        if self._username:
            return self._username
        try:
            payload = client.get("/referrals/overview/")
        except LcsError as exc:
            log.info("username auto-detect failed: %s", exc)
            return None
        if isinstance(payload, dict):
            user = payload.get("user")
            if isinstance(user, dict) and user.get("username"):
                self._username = str(user["username"])
                return self._username
        return None

    # -- fx -------------------------------------------------------------

    def _current_rate(
        self, warnings: list[str], target_currency: str | None = None
    ) -> Decimal | None:
        target = (target_currency or self.settings.display_currency).strip().upper()
        base = self.settings.base_currency.upper()
        if target == base:
            return Decimal("1.0")

        # Allow tests to set service._current_fx = (date, rate) tuple
        if isinstance(self._current_fx, tuple):
            return self._current_fx[1]

        now = time.monotonic()
        if target in self._current_fx_map:
            cached_time, rate_tuple = self._current_fx_map[target]
            if (now - cached_time) < CURRENT_FX_TTL:
                return rate_tuple[1]

        try:
            rate_tuple = self.fx.client(target).current_rate()
            self._current_fx_map[target] = (now, rate_tuple)
            self._current_fx = rate_tuple
            return rate_tuple[1]
        except FxError as exc:
            warnings.append(f"Could not refresh current {base}/{target} FX rate: {exc}")
            latest = self.fx_store.latest(self.fx.series(target))
            if latest is not None:
                warnings.append(
                    f"Using the most recent cached rate ({latest[0]})."
                )
                rate_tuple = (date.fromisoformat(latest[0]), latest[1])
                self._current_fx_map[target] = (now, rate_tuple)
                self._current_fx = rate_tuple
                return rate_tuple[1]
            return None



    # -- build ----------------------------------------------------------

    def build(
        self,
        filters: TradeFilters | None = None,
        *,
        refresh: bool = False,
        display_currency: str | None = None,
    ) -> ReportData:
        filters = filters or TradeFilters()
        warnings: list[str] = []
        target_currency = (
            display_currency or self.settings.display_currency
        ).strip().upper()

        raw, stale = self.fetch_trades(refresh=refresh)
        if self.refresher.state.error:
            warnings.append(
                f"LCS sync issue: {self.refresher.state.error}"
            )
        self.enricher.set_expected_total(len(raw))

        # Merge cached per-trade detail so market-rate columns can populate.
        uuids = [str(r.get("uuid") or "") for r in raw]
        details = self.enricher.details_for([u for u in uuids if u])

        trades: list[ValuedTrade] = []
        for item in raw:
            merged = dict(item)
            detail = details.get(str(item.get("uuid") or ""))
            if detail:
                for key, value in detail.items():
                    if value is not None:
                        merged.setdefault(key, value)
                        merged[key] = value
            trades.append(
                normalise_trade(
                    merged,
                    username=self._username,
                    display_currency=target_currency,
                )
            )

        for trade in trades:
            trade.trade_date = resolve_date(
                trade.created_ts, self.settings.trade_timezone
            )

        current_rate = self._current_rate(warnings, target_currency)
        base = self.settings.base_currency.upper()
        if current_rate is None and target_currency != base:
            raise ReportError(
                f"Could not load any {base}/{target_currency} rates. Check access to "
                "api.frankfurter.dev."
            )

        # Prime FX for all trades so historical rates exist for any trade date
        by_target: dict[str, list[date]] = {}
        for trade in trades:
            if trade.trade_date is None:
                continue
            quote = (trade.fiat_symbol or "").upper()
            if quote and quote != base:
                by_target.setdefault(quote, []).append(trade.trade_date)
            if target_currency and target_currency != base:
                by_target.setdefault(target_currency, []).append(trade.trade_date)
        self._last_quotes = list(trades)
        self.fx.prime_all(by_target)
        warnings.extend(self.fx.errors)

        if self.intraday.state.errors:
            warnings.extend(
                f"Intraday FX unavailable, using the daily rate: {why}"
                for why in self.intraday.state.errors.values()
            )

        self.fx.go_offline(True)
        try:
            valued_all = self._value(trades, current_rate, target_currency)
        finally:
            self.fx.go_offline(False)

        if not self._username:
            warnings.append(
                "Username unknown, so buy/sell direction may be missing. "
                "Set LCS_USERNAME in .env."
            )
        enriched_here = sum(1 for t in valued_all if t.enriched)
        missing_trades = [t for t in valued_all if not t.enriched]
        # base currency trades don't need enrichment (crypto_in_usd is 1:1)
        missing_non_base = [t for t in missing_trades if t.fiat_symbol != "USD"]
        if missing_non_base:
            missing = len(missing_non_base)
            warnings.append(
                f"{missing} of {len(valued_all)} trades lack per-trade USD price; "
                "their market-rate value is blank until enrichment finishes."
            )

        # Portfolio-wide summary: always reflects the full portfolio in the chosen display currency
        summary = summarise(valued_all, display_currency=target_currency)

        # Filters apply only to the trades list
        filtered_trades = self._apply_filters(valued_all, filters)

        today = date.today()
        # YTD covers every month of the current year seen so far.
        # rather than only the months that happen to have trades.
        ytd_period = _year_to_date(summary, today.year)
        prior_period = _full_year(summary, today.year - 1)

        fx_date, fx_rate = None, None
        if target_currency == base:
            fx_date, fx_rate = today, Decimal("1.0")
        elif isinstance(self._current_fx, tuple):
            fx_date, fx_rate = self._current_fx
        elif target_currency in self._current_fx_map:
            fx_date, fx_rate = self._current_fx_map[target_currency][1]

        return ReportData(
            trades=filtered_trades,
            summary=summary,
            service=self,
            ytd=ytd_period,
            prior_year=prior_period,
            total_available=len(raw),
            username=self._username,
            fx_rate=fx_rate,
            fx_date=fx_date,
            fx_coverage=self.fx.coverage(target_currency),
            warnings=warnings,
            stale=stale,
            enriched=enriched_here,
            total=len(filtered_trades),
            filters=filters,
            display_currency=target_currency,
        )

    def start_enrichment(self) -> bool:
        """Queue the background detail fetch for every known trade."""
        if not self.settings.has_api_key:
            return False
        raw, _ = self.fetch_trades()
        uuids = [str(r.get("uuid") or "") for r in raw if r.get("uuid")]
        return self.enricher.start(uuids)

    def _value(
        self,
        trades: list[Any],
        current_rate: Decimal,
        display_currency: str | None = None,
    ) -> Any:
        """Value trades, preferring a point-in-time hourly bar.

        Falls back to the daily reference rate, shifted by the ECB publication
        lag, so a trade never uses a rate that did not exist yet at the moment it
        happened.
        """
        intraday = self.intraday

        def lookup(trade, currency: str):
            ts = trade.created_ts or int(
                datetime.combine(
                    trade.trade_date, clock(12), tzinfo=timezone.utc
                ).timestamp()
            )
            got = intraday.rate_at(currency, ts)
            if got is not None:
                bar_ts, rate = got
                return (
                    datetime.fromtimestamp(bar_ts, timezone.utc).date(),
                    rate,
                    False,
                    "intraday",
                )
            known = knowledge_date(ts)
            eff, rate, snapped = self.fx.rate_for(known, currency)
            return eff, rate, snapped or eff != known, "daily"

        return apply_fx(
            trades,
            rate_lookup=lookup,
            base_currency=self.settings.base_currency,
            display_currency=display_currency,
        )

    # -- filtering ------------------------------------------------------

    def _apply_filters(
        self, trades: list[ValuedTrade], filters: TradeFilters
    ) -> list[ValuedTrade]:
        out: list[ValuedTrade] = []
        needle = filters.search.strip().lower()
        for trade in trades:
            if filters.date_from and (
                trade.trade_date is None or trade.trade_date < filters.date_from
            ):
                continue
            if filters.date_to and (
                trade.trade_date is None or trade.trade_date > filters.date_to
            ):
                continue
            if filters.coin and trade.coin_label.upper() != filters.coin.upper():
                continue
            if filters.direction and trade.direction != filters.direction:
                continue
            if filters.status and trade.status != filters.status.upper():
                continue
            if filters.payment and (
                (trade.payment_method or "").lower() != filters.payment.lower()
            ):
                continue
            if filters.quote_currency and (
                (trade.fiat_symbol or "").upper() != filters.quote_currency.upper()
            ):
                continue
            if filters.settled_only and not trade.is_settled:
                continue
            if needle:
                haystack = " ".join(
                    filter(
                        None,
                        [
                            trade.counterpart,
                            trade.coin_label,
                            trade.status,
                            trade.payment_method,
                            trade.location,
                        ],
                    )
                ).lower()
                if needle not in haystack:
                    continue
            out.append(trade)
        out.sort(
            key=lambda t: (t.trade_date or date.min, t.created_ts or 0), reverse=True
        )
        return out


__all__ = [
    "ReportData",
    "ReportError",
    "ReportService",
    "SETTLED_STATUSES",
    "TradeFilters",
    "UNSETTLED_STATUSES",
]