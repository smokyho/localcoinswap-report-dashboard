"""Live-price feed used for portfolio valuation.

The LCS API does not expose current prices -- ``metadata.cmc_analytics`` comes
back empty -- so an up-to-date mark has to come from elsewhere. CoinGecko's
free ``/simple/price`` needs no key and returns USD and IDR for many coins in a
single request.

Chain is deliberately ignored when mapping a symbol to a CoinGecko id:
USDT-TRC20, USDT-ERC20 and USDT-BEP20 are the same asset priced in IDR, and a
report that valued them at three different prices would be wrong.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger(__name__)

COINGECKO_BASE = "https://api.coingecko.com/api/v3"
BINANCE_BASE = "https://api.binance.com/api/v3"
BYBIT_BASE = "https://api.bybit.com/v5"

# Sources are tried in order and the first to answer a symbol wins.
# Bybit precedes Binance because its /market/tickers returns every spot pair in
# one request (including BNBUSDT, which Binance does not list).
SOURCE_ORDER = ("coingecko", "bybit", "binance")

# Symbol -> CoinGecko id. Keyed on the bare symbol so all chains of a token
# resolve to one price.
SYMBOL_TO_ID: dict[str, str] = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "LTC": "litecoin",
    "BCH": "bitcoin-cash",
    "DOGE": "dogecoin",
    "XRP": "ripple",
    "ADA": "cardano",
    "SOL": "solana",
    "DOT": "polkadot",
    "MATIC": "matic-network",
    "AVAX": "avalanche-2",
    "TRX": "tron",
    "BNB": "binancecoin",
    "USDT": "tether",
    "USDC": "usd-coin",
    "BUSD": "binance-usd",
    "DAI": "dai",
    "TUSD": "true-usd",
    "PAX": "paxos-standard",
    "CELO": "celo",
    "SHIB": "shiba-inu",
    "LCS": "localcoinswap",
}

PRICE_TTL = 900.0  # seconds

# Symbols with no <SYMBOL>USDT spot pair. USDT is the quote currency itself, so
# USDTUSDT is not a market; BUSD has been delisted from spot on both venues.
# These fall through to CoinGecko, which still lists them.
_NO_USDT_PAIR = frozenset({"USDT", "BUSD"})


def _usdt_pair(symbol: str) -> str | None:
    """Exchange pair for a symbol, or None when it is not quoted in USDT."""
    bare = bare_symbol(symbol)
    if bare in _NO_USDT_PAIR:
        return None
    return f"{bare}USDT"


def bare_symbol(symbol: str) -> str:
    """Strip a chain suffix: ``USDT-TRC20`` -> ``USDT``.

    All chains of a token price the same asset, so the chain must not change
    which price is looked up.
    """
    return symbol.split("-", 1)[0].strip().upper()


@dataclass
class PriceBook:
    """Current unit prices, keyed by bare symbol."""

    usd: dict[str, Decimal] = field(default_factory=dict)
    idr: dict[str, Decimal] = field(default_factory=dict)
    # Display-currency per USD, used to derive the table above.
    target_rate: Decimal | None = None
    target_currency: str = "IDR"
    fetched_at: float = 0.0
    error: str | None = None

    @property
    def fresh(self) -> bool:
        return bool(self.usd) and (time.monotonic() - self.fetched_at) < PRICE_TTL

    def usd_price(self, symbol: str) -> Decimal | None:
        return self.usd.get(symbol.upper())

    def idr_price(self, symbol: str) -> Decimal | None:
        return self.idr.get(symbol.upper())

    def as_dict(self) -> dict[str, Any]:
        return {
            "usd": {k: str(v) for k, v in self.usd.items()},
            "target_rate": str(self.target_rate) if self.target_rate else None,
            "target_currency": self.target_currency,
            "fetched_at": self.fetched_at,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PriceBook":
        rate = data.get("target_rate")
        book = cls(
            usd={k: Decimal(v) for k, v in (data.get("usd") or {}).items()},
            target_rate=Decimal(rate) if rate else None,
            target_currency=str(data.get("target_currency") or "IDR"),
            fetched_at=float(data.get("fetched_at") or 0.0),
        )
        if book.target_rate:
            book._derive(book.target_rate)
        return book

    def _derive(self, rate: Decimal) -> None:
        self.idr = {k: v * rate for k, v in self.usd.items()}


class PriceClient:
    """Fetches and caches current prices, with a disk fallback."""

    def __init__(
        self,
        cache_path: Path,
        *,
        base_url: str = COINGECKO_BASE,
        binance_base: str = BINANCE_BASE,
        bybit_base: str = BYBIT_BASE,
        timeout: float = 25.0,
    ) -> None:
        self.cache_path = cache_path
        self.base_url = base_url.rstrip("/")
        self.binance_base = binance_base.rstrip("/")
        self.bybit_base = bybit_base.rstrip("/")
        self.timeout = timeout
        self.book = PriceBook()
        self._load()

    def _load(self) -> None:
        if not self.cache_path.exists():
            return
        try:
            self.book = PriceBook.from_dict(
                json.loads(self.cache_path.read_text())
            )
            log.info("loaded %d cached live prices", len(self.book.usd))
        except (json.JSONDecodeError, OSError, ValueError) as exc:
            log.info("no usable price cache: %s", exc)

    def _save(self) -> None:
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.cache_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.book.as_dict()))
            tmp.replace(self.cache_path)
        except OSError as exc:
            log.warning("could not persist price cache: %s", exc)

    @staticmethod
    def needed_ids(symbols: set[str]) -> list[str]:
        wanted = set()
        for s in symbols:
            b = bare_symbol(s)
            if b in SYMBOL_TO_ID:
                wanted.add(SYMBOL_TO_ID[b])
        return sorted(wanted)

    def refresh(self, symbols: set[str]) -> PriceBook:
        """Fetch current prices for ``symbols``, trying each source in turn.

        A symbol is only skipped once every source has been tried, so one dead
        feed cannot blank the report. Whatever succeeds is cached; on total
        failure the previous cache is kept and ``error`` explains why.
        """
        wanted = {
            bare_symbol(s) for s in symbols if bare_symbol(s) in SYMBOL_TO_ID
        }
        if not wanted:
            self.book.error = None
            return self.book

        if self.book.fresh and wanted.issubset(set(self.book.usd)):
            return self.book

        collected: dict[str, Decimal] = {}
        notes: list[str] = []

        for name in SOURCE_ORDER:
            missing = wanted - set(collected)
            if not missing:
                break
            try:
                got = self._fetch_from(name, missing)
            except Exception as exc:  # noqa: BLE001 - try the next source
                notes.append(f"{name}: {exc}")
                log.info("price source %s failed: %s", name, exc)
                continue
            if got:
                collected.update(got)
            else:
                notes.append(f"{name}: no quote for {len(missing)} symbol(s)")

        if collected:
            log.info(
                "live prices: %d symbols, target rate %s per USD",
                len(collected),
                self.book.target_rate,
            )
            self.book.usd = dict(collected)
            # The target-currency table is derived, not fetched, so a change of
            # display currency does not require a new upstream call.
            self.book.fetched_at = time.monotonic()
            self.book.error = "; ".join(notes) if notes and len(collected) < len(wanted) else None
            self._save()
        else:
            self.book.error = (
                "All price sources failed (" + "; ".join(notes) + ")"
                if notes
                else "No price sources configured"
            )
        self._derive_target(self.book.target_rate)
        return self.book

    def _fetch_from(self, source: str, symbols: set[str]) -> dict[str, Decimal]:
        if source == "coingecko":
            return self._fetch_coingecko(symbols)
        if source == "binance":
            return self._fetch_binance(symbols)
        if source == "bybit":
            return self._fetch_bybit(symbols)
        raise ValueError(f"unknown price source {source!r}")

    def _fetch_coingecko(self, symbols: set[str]) -> dict[str, Decimal]:
        ids = sorted({SYMBOL_TO_ID[b] for b in symbols})
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.get(
                f"{self.base_url}/simple/price",
                params={"ids": ",".join(ids), "vs_currencies": "usd"},
            )
        resp.raise_for_status()
        payload = resp.json()
        out: dict[str, Decimal] = {}
        for cid, body in payload.items():
            if not isinstance(body, dict) or body.get("usd") is None:
                continue
            for symbol, mapped in SYMBOL_TO_ID.items():
                if mapped == cid:
                    out[symbol] = Decimal(str(body["usd"]))
        return out

    def _fetch_binance(self, symbols: set[str]) -> dict[str, Decimal]:
        """Binance rejects a whole batch if any symbol is unknown to it, so a
        rejected batch is retried one symbol at a time and the failures are
        dropped rather than propagated."""
        wanted = {s: _usdt_pair(s) for s in symbols}
        pairs = [p for p in wanted.values() if p]
        if not pairs:
            return {}

        by_pair: dict[str, str] = {}
        with httpx.Client(timeout=self.timeout) as client:
            try:
                resp = client.get(
                    f"{self.binance_base}/ticker/price",
                    params={"symbols": json.dumps(pairs)},
                )
                resp.raise_for_status()
                payload = resp.json()
                if isinstance(payload, list):
                    for row in payload:
                        if isinstance(row, dict) and row.get("symbol"):
                            by_pair[row["symbol"]] = row.get("price")
            except httpx.HTTPError as exc:
                log.info("binance batch rejected (%s); retrying per symbol", exc)
                for pair in pairs:
                    try:
                        one = client.get(
                            f"{self.binance_base}/ticker/price", params={"symbol": pair}
                        )
                        one.raise_for_status()
                        body = one.json()
                        if isinstance(body, dict) and body.get("symbol"):
                            by_pair[body["symbol"]] = body.get("price")
                    except httpx.HTTPError:
                        continue

        out: dict[str, Decimal] = {}
        for symbol, pair in wanted.items():
            price = by_pair.get(pair or "")
            if price:
                out[symbol] = Decimal(str(price))
        return out

    def _fetch_bybit(self, symbols: set[str]) -> dict[str, Decimal]:
        """One request returns every spot pair, so no symbol list is needed."""
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.get(
                f"{self.bybit_base}/market/tickers", params={"category": "spot"}
            )
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("retCode") != 0:
            raise RuntimeError(payload.get("retMsg") or "bybit error")
        listing = (payload.get("result") or {}).get("list") or []
        by_pair = {
            row.get("symbol"): row.get("lastPrice")
            for row in listing
            if isinstance(row, dict)
        }
        out: dict[str, Decimal] = {}
        for symbol in symbols:
            price = by_pair.get(_usdt_pair(symbol) or "")
            if price:
                out[symbol] = Decimal(str(price))
        return out

    def set_target_rate(self, rate: Decimal | None) -> None:
        """Store the display-currency rate used to convert the USD feed."""
        self.book.target_rate = rate
        self._derive_target(rate)

    def _derive_target(self, rate: Decimal | None) -> None:
        if not rate:
            return
        self.book.target_rate = rate
        self.book.idr = {k: v * rate for k, v in self.book.usd.items()}

    def mark(
        self, trades, fx: Decimal
    ) -> tuple[dict[str, Decimal], list[str]]:
        """Attach a live valuation to each trade.

        Returns ``(value_by_uuid, unpriced_symbols)``. IDR is preferred because
        it is what the report is denominated in; USD is converted using the
        supplied spot rate as a fallback.
        """
        values: dict[str, Decimal] = {}
        unpriced: list[str] = []

        for trade in trades:
            # Accept either a bare symbol or a chain-qualified one.
            symbol = bare_symbol(trade.coin_symbol or "")
            if symbol not in SYMBOL_TO_ID:
                unpriced.append(trade.coin_symbol)
                continue
            price = self.book.idr_price(symbol)
            if price is None:
                usd = self.book.usd_price(symbol)
                price = (usd * fx) if (usd is not None and fx) else None
            if price is None:
                unpriced.append(trade.coin_symbol)
                continue
            trade.unit_price_idr = price
            if trade.coin_amount is not None:
                # Signed by direction: a buy adds to holdings, a sell removes
                # them. Without the sign a sale would inflate the portfolio.
                trade.value_idr_now = trade.coin_amount * price * Decimal(-trade.sign)
                values[trade.uuid] = trade.value_idr_now

        return values, sorted(set(unpriced))


__all__ = [
    "BINANCE_BASE",
    "BYBIT_BASE",
    "COINGECKO_BASE",
    "PRICE_TTL",
    "SOURCE_ORDER",
    "PriceBook",
    "PriceClient",
    "SYMBOL_TO_ID",
    "bare_symbol",
]
