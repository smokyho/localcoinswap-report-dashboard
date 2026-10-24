"""Valuation logic for real LocalCoinSwap payloads.

Field naming
------------
The published Swagger spec documents camelCase (``cryptoInUsd``,
``coinAmount``). The live API returns **snake_case** (``crypto_in_usd``,
``coin_amount``). Both are accepted here, because the spec is the documented
contract and silently breaking on either would be worse than carrying the
alias. Verified against a live account on 2026-10-05.

Which fields appear where
-------------------------
``/trades/list/`` (paginated, every trade) returns the offer nested under
``offer`` and has **no** ``crypto_in_usd``.

``/trades/{uuid}/`` (one trade) returns the offer under ``ad`` and **does**
carry ``crypto_in_usd`` -- the price genuinely frozen at trade creation.

That asymmetry is why enrichment is a separate, cached step.

Three distinct numbers
----------------------
These are never conflated:

1. ``fiat_amount`` -- rupiah that actually changed hands. Realised cash flow.
   Already correct; no FX conversion is applied to it.
2. executed-price value -- ``coin_amount x offer.price_formula_value``. Equals
   ``fiat_amount``; it is the price including the counterparty's margin.
3. market-rate notional -- ``coin_in_usd x historical_fx(trade_date)``. What
   this report is for: the value at the rate of that day.

Note LCS's own ``crypto_in_local_currency`` is NOT usable for history: it is
``crypto_in_usd`` multiplied by *today's* rate. Measured across trades from
2023-07 to 2026-09, the implied rate is identical to four decimal places.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Iterable, Sequence

CENT = Decimal("0.01")
CRYPTO_EXP = Decimal("0.00000001")
PCT_EXP = Decimal("0.01")

# Currencies with no minor unit. Everything else is assumed to have two, which
# matters: rounding a USD profit to a whole unit would erase it.
ZERO_DECIMAL = frozenset(
    {"IDR", "JPY", "KRW", "VND", "CLP", "ISK", "HUF", "TWD", "COP", "UGX", "RWF"}
)


def minor_unit(currency: str | None) -> Decimal:
    """Quantum for a currency: 1 for IDR/JPY, 0.01 for USD/SGD/EUR."""
    return Decimal("1") if (currency or "").upper() in ZERO_DECIMAL else CENT


def q_money(value: Decimal | None, currency: str | None = None) -> Decimal | None:
    if value is None:
        return None
    return value.quantize(minor_unit(currency), rounding=ROUND_HALF_UP)


def q_idr(value: Decimal | None) -> Decimal | None:
    """Kept for IDR-specific call sites; prefer q_money with the currency."""
    return q_money(value, "IDR")

# Statuses where value genuinely changed hands.
SETTLED_STATUSES = frozenset(
    {"COMPLETED", "CRYPTO_REL", "DISPUTE_RESOLVE_BUYER", "DISPUTE_RESOLVE_SELLER"}
)
# Statuses that represent a proposal that never executed.
UNSETTLED_STATUSES = frozenset(
    {
        "CREATED",
        "ACCEPTED",
        "REJECTED",
        "EXPIRED",
        "CANCELLED",
        "CANCELLED_AFTER_ESCROW",
        "CANCELLATION_REQUESTED",
        "CANCELLATION_REJECTED",
        "DISPUTED",
        "BROADCASTING_ESCROW_FAILED",
        "CRYPTO_ESC_FAILED",
        "BROADCASTING_RELEASE_FAILED",
        "CRYPTO_REL_FAILED",
    }
)


def settled(status: str) -> bool:
    """True when the trade settled and value moved."""
    return str(status).upper() in SETTLED_STATUSES


def to_decimal(value: Any) -> Decimal | None:
    """Parse a value the API may send as string, int, float, or None.

    Floats are routed through ``str`` so ``0.1`` becomes ``Decimal("0.1")``
    rather than the binary approximation ``Decimal(0.1)``.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, str)):
        try:
            return Decimal(str(value).strip())
        except (InvalidOperation, ValueError):
            return None
    if isinstance(value, float):
        return Decimal(str(value))
    return None


def _pick(raw: dict[str, Any], *names: str) -> Any:
    """Return the first present key among snake_case/camelCase aliases."""
    for name in names:
        if name in raw and raw[name] is not None:
            return raw[name]
    return None


def q_crypto(value: Decimal | None) -> Decimal | None:
    return None if value is None else value.quantize(CRYPTO_EXP, rounding=ROUND_HALF_UP)


def q_pct(value: Decimal | None) -> Decimal | None:
    return None if value is None else value.quantize(PCT_EXP, rounding=ROUND_HALF_UP)


@dataclass
class ValuedTrade:
    """One trade, valued at trade-time FX and compared to LCS's own figure."""

    uuid: str
    coin_symbol: str
    coin_slug: str
    coin_label: str
    token_standard: str
    fiat_symbol: str
    status: str
    direction: str  # buy | sell | unknown
    counterpart: str | None
    payment_method: str | None
    location: str | None
    headline: str | None
    created_ts: int | None
    trade_date: date | None

    coin_amount: Decimal | None
    fiat_amount: Decimal | None
    price_formula_value: Decimal | None
    coin_in_usd: Decimal | None

    # populated by the FX pass
    fx_date: date | None = None
    fx_rate: Decimal | None = None
    fx_snapped: bool = False
    # "intraday" when an hourly bar covered the trade, "daily" when the ECB
    # reference rate was used instead. Surfaced per trade so a mixed-basis
    # report is honest about which trades came from where.
    fx_source: str = ""

    # Value of the coin at this trade's own date, expressed in the trade's
    # quote currency (fiat_symbol). Both this and fiat_amount are in the same
    # currency by construction, so profit is a plain subtraction.
    quote_currency: str = ""
    market_value_quote: Decimal | None = None  # gross market value of the coin
    # The trading fee is charged in the coin, not in the quote currency, so it is
    # valued at the coin's price on the trade's own date. On a buy it comes out
    # of the crypto received; on a sell it comes out of the proceeds. Either way
    # it reduces what the account keeps, so it subtracts exactly once.
    fee_amount: Decimal | None = None
    fee_ratio: Decimal | None = None
    fee_value_quote: Decimal | None = None
    net_value_quote: Decimal | None = None  # what the account actually keeps
    profit: Decimal | None = None  # net of fee

    # Valuation in chosen display currency (e.g. EUR, USD, IDR)
    display_currency: str = ""
    market_value_display: Decimal | None = None
    fee_value_display: Decimal | None = None
    net_value_display: Decimal | None = None
    fiat_amount_display: Decimal | None = None
    profit_display: Decimal | None = None

    # Retained only to compare against LCS's own current-rate figure.
    market_value_idr: Decimal | None = None
    lcs_local_value_idr: Decimal | None = None
    lcs_drift_idr: Decimal | None = None
    enriched: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def is_settled(self) -> bool:
        return settled(self.status)

    @property
    def has_market_value(self) -> bool:
        return self.market_value_idr is not None

    @property
    def sign(self) -> int:
        if self.direction == "sell":
            return 1
        if self.direction == "buy":
            return -1
        return 0

    @property
    def cash_flow_idr(self) -> Decimal | None:
        """Signed realised rupiah. A buy is money out, a sale is money in."""
        if self.fiat_amount is None:
            return None
        return self.fiat_amount * self.sign


def _coin_info(raw_coin: Any) -> tuple[str, str, str, str]:
    """Return ``(symbol, slug, token_standard, display_label)``.

    USDT exists on TRC20, ERC20 and BEP20 as genuinely different assets, so
    the chain is folded into the label rather than silently merging them.
    """
    if not isinstance(raw_coin, dict):
        if isinstance(raw_coin, str) and raw_coin.strip():
            sym = raw_coin.strip().upper()
            return sym, "", "", sym
        return "?", "", "", "?"

    symbol = str(raw_coin.get("symbol") or raw_coin.get("title") or "?").upper()
    token_standard = str(raw_coin.get("token_standard") or "").strip().upper()
    slug = str(raw_coin.get("slug") or "").strip()
    label = f"{symbol}-{token_standard}" if token_standard else symbol
    return symbol, slug, token_standard, label


def _offer_block(raw: dict[str, Any]) -> dict[str, Any]:
    """The offer is ``offer`` on list responses and ``ad`` on detail."""
    for key in ("offer", "ad"):
        block = raw.get(key)
        if isinstance(block, dict) and block:
            return block
    return {}


def _username(node: Any) -> str | None:
    if isinstance(node, dict):
        name = node.get("username")
        return str(name) if name else None
    if isinstance(node, str) and node.strip():
        return node.strip()
    return None


def _resolve_direction(
    raw: dict[str, Any], buyer: str | None, seller: str | None, me: str | None
) -> tuple[str, str | None]:
    """Decide whether this was a buy or a sell from the viewer's perspective.

    Comparing buyer/seller against the authenticated username is authoritative.
    The offer's trading type is a fallback for when the username is unknown.
    """
    counterpart: str | None = None
    if me:
        if buyer and buyer == me:
            return "buy", seller or counterpart
        if seller and seller == me:
            return "sell", buyer or counterpart

    offer = _offer_block(raw)
    tt = offer.get("trading_type")
    if isinstance(tt, dict):
        slug = str(tt.get("slug") or tt.get("name") or "").strip().lower()
        if slug in ("buy", "sell"):
            return slug, counterpart
    if isinstance(tt, str) and tt.strip().lower() in ("buy", "sell"):
        return tt.strip().lower(), counterpart

    created_by = _username(offer.get("created_by"))
    if created_by and seller and created_by == seller:
        return "sell", buyer
    return "unknown", counterpart


def normalise_trade(
    raw: dict[str, Any],
    *,
    username: str | None = None,
    display_currency: str = "IDR",
) -> ValuedTrade:
    """Map one raw trade payload onto :class:`ValuedTrade`. Pure; no I/O."""
    offer = _offer_block(raw)
    symbol, slug, token_standard, label = _coin_info(
        _pick(raw, "coin_currency", "coinCurrency")
    )
    # The quote currency comes from the trade's own `fiat_currency`, NOT from
    # `local_currency_symbol` on the detail endpoint. Verified: for trades the
    # list endpoint reports as USD, the detail endpoint still says
    # `local_currency_symbol: IDR`. That field tracks an account-level display
    # preference, whereas `fiat_currency.slug` is "united-states-dollar" and the
    # amounts are plainly dollars. Trusting the detail field would mis-bucket
    # every non-IDR trade. See docs/api-behaviour.md.
    fiat = _pick(raw, "fiat_currency", "fiatCurrency")
    fiat_symbol = (
        str(fiat.get("symbol") or "?").upper() if isinstance(fiat, dict) else "?"
    )

    buyer = _username(_pick(raw, "buyer"))
    seller = _username(_pick(raw, "seller"))
    direction, counterpart = _resolve_direction(raw, buyer, seller, username)

    coin_amount = to_decimal(_pick(raw, "coin_amount", "coinAmount"))
    fiat_amount = to_decimal(_pick(raw, "fiat_amount", "fiatAmount"))
    pfv = to_decimal(offer.get("price_formula_value"))
    coin_in_usd = to_decimal(_pick(raw, "crypto_in_usd", "cryptoInUsd"))
    # The trading fee, in coin units. Present on the list endpoint as `fees`.
    fee_amount = to_decimal(_pick(raw, "fees", "fee", "trading_fee"))

    notes: list[str] = []
    if coin_in_usd is None:
        notes.append("Needs per-trade fetch for historical market value.")
    if direction == "unknown":
        notes.append("Direction unknown: set LCS_USERNAME.")
    if fiat_symbol != display_currency.upper():
        notes.append(
            f"Denominated in {fiat_symbol}, not {display_currency.upper()}: "
            "excluded from the money totals."
        )

    payment = offer.get("payment_method")
    payment_name = (
        payment.get("name")
        if isinstance(payment, dict)
        else (payment if isinstance(payment, str) else None)
    )

    created = _pick(raw, "time_created", "timeCreated")
    created_ts = int(created) if isinstance(created, (int, float)) else None

    return ValuedTrade(
        uuid=str(_pick(raw, "uuid") or ""),
        coin_symbol=symbol,
        coin_slug=slug,
        coin_label=label,
        token_standard=token_standard,
        fiat_symbol=fiat_symbol,
        status=str(_pick(raw, "status") or "UNKNOWN").upper(),
        direction=direction,
        counterpart=counterpart,
        payment_method=payment_name,
        location=offer.get("location_name"),
        headline=offer.get("headline"),
        created_ts=created_ts,
        trade_date=None,
        coin_amount=q_crypto(coin_amount),
        fiat_amount=q_money(fiat_amount, fiat_symbol),
        price_formula_value=pfv,
        coin_in_usd=coin_in_usd,
        fee_amount=fee_amount,
        # Only trust LCS's own local-currency figure when it is present.
        lcs_local_value_idr=q_idr(
            to_decimal(_pick(raw, "crypto_in_local_currency", "cryptoInLocalCurrency"))
        ),
        notes=notes,
    )


def resolve_date(timestamp: int | float | None, tz: Any) -> date | None:
    if timestamp is None:
        return None
    try:
        return datetime.fromtimestamp(float(timestamp), tz=timezone.utc).astimezone(
            tz
        ).date()
    except (OverflowError, OSError, ValueError, TypeError):
        return None


def apply_fx(
    trades: Iterable[ValuedTrade],
    *,
    rate_lookup,
    base_currency: str = "USD",
    display_currency: str | None = None,
) -> list[ValuedTrade]:
    """Value each trade in its own quote currency, at that trade's own rate.

    ``rate_lookup(trade, currency)`` returns ``(effective_day, rate, snapped)`` or
    ``(effective_day, rate, snapped, source)``. It receives the trade rather than
    just its date because a point-in-time rate needs the trade's timestamp, not
    just the calendar day. Injected, so this stays free of I/O.

    A trade quoted in the base currency needs no rate at all -- ``crypto_in_usd``
    and ``fiat_amount`` are already in the same units -- so that case short
    circuits rather than looking up a series that would be identically 1.0.

    When ``display_currency`` is provided (e.g. EUR, USD, IDR), trades are also
    revalued into that currency at the trade's date rate.
    """
    base = base_currency.upper()
    target = display_currency.strip().upper() if display_currency else None
    out: list[ValuedTrade] = []

    for trade in trades:
        quote = (trade.fiat_symbol or "").upper()
        trade.quote_currency = quote

        if trade.trade_date is None or trade.coin_in_usd is None:
            out.append(trade)
            continue

        if quote == base:
            effective, rate, snapped, source = (
                trade.trade_date, Decimal(1), False, "base"
            )
        else:
            try:
                answer = rate_lookup(trade, quote)
                if len(answer) == 4:
                    effective, rate, snapped, source = answer
                else:
                    effective, rate, snapped = answer
                    source = "daily"
            except Exception as exc:  # noqa: BLE001 - isolated to this trade
                trade.notes.append(f"FX lookup failed for {quote}: {exc}")
                out.append(trade)
                continue

        trade.fx_date = effective
        trade.fx_rate = rate
        trade.fx_snapped = snapped
        trade.fx_source = source or "daily"

        market = q_money(trade.coin_in_usd * rate, quote)
        trade.market_value_quote = market

        # Fee: `fees` coin units, valued here at the coin's own price.
        if (
            trade.fee_amount is not None
            and trade.fee_amount != 0
            and trade.coin_amount
            and trade.coin_in_usd is not None
        ):
            ratio = trade.fee_amount / trade.coin_amount
            trade.fee_ratio = ratio
            trade.fee_value_quote = q_money(trade.fee_amount * rate * (trade.coin_in_usd / trade.coin_amount), quote)
        else:
            trade.fee_ratio = Decimal(0)
            trade.fee_value_quote = Decimal(0)
        trade.fee_value_quote = trade.fee_value_quote or Decimal(0)
        trade.net_value_quote = q_money(market - trade.fee_value_quote, quote)

        if trade.fiat_amount is not None:
            trade.profit = q_money(
                Decimal(trade.sign) * (trade.fiat_amount - market)
                - trade.fee_value_quote,
                quote,
            )

        # Revalue into target display currency if specified
        if target:
            trade.display_currency = target
            if quote == target:
                trade.market_value_display = trade.market_value_quote
                trade.fee_value_display = trade.fee_value_quote
                trade.net_value_display = trade.net_value_quote
                trade.fiat_amount_display = trade.fiat_amount
                trade.profit_display = trade.profit
            else:
                try:
                    if target == base:
                        rate_target = Decimal(1)
                    else:
                        ans_target = rate_lookup(trade, target)
                        rate_target = ans_target[1] if isinstance(ans_target, tuple) else ans_target

                    market_disp = q_money(trade.coin_in_usd * rate_target, target)
                    trade.market_value_display = market_disp

                    if (
                        trade.fee_amount is not None
                        and trade.fee_amount != 0
                        and trade.coin_amount
                        and trade.coin_in_usd is not None
                    ):
                        trade.fee_value_display = q_money(
                            trade.fee_amount * rate_target * (trade.coin_in_usd / trade.coin_amount),
                            target,
                        )
                    else:
                        trade.fee_value_display = Decimal(0)
                    trade.net_value_display = q_money(market_disp - trade.fee_value_display, target)

                    if trade.fiat_amount is not None:
                        if quote == base:
                            fiat_usd = trade.fiat_amount
                        elif trade.fx_rate and trade.fx_rate != 0:
                            fiat_usd = trade.fiat_amount / trade.fx_rate
                        else:
                            fiat_usd = None
                        if fiat_usd is not None:
                            fiat_disp = q_money(fiat_usd * rate_target, target)
                            trade.fiat_amount_display = fiat_disp
                            trade.profit_display = q_money(
                                Decimal(trade.sign) * (fiat_disp - market_disp) - trade.fee_value_display,
                                target,
                            )
                except Exception as exc:  # noqa: BLE001
                    trade.notes.append(f"FX lookup failed for display currency {target}: {exc}")

        # LCS's own figure, kept only to quantify its error. It is always
        # crypto_in_usd revalued at a *current* rate.
        if quote == "IDR" and trade.lcs_local_value_idr is not None:
            trade.market_value_idr = q_money(trade.coin_in_usd * rate, "IDR")
            trade.lcs_drift_idr = q_money(
                trade.lcs_local_value_idr - trade.market_value_idr, "IDR"
            )
            trade.enriched = True

        out.append(trade)
    return out


@dataclass
class CurrencyTotals:
    """Money totals for a single currency. Never add these together."""

    currency: str
    trades: int = 0
    cash_in: Decimal = Decimal("0")
    cash_out: Decimal = Decimal("0")
    cost: Decimal = Decimal("0")
    market_value: Decimal = Decimal("0")
    profit: Decimal = Decimal("0")

    @property
    def net(self) -> Decimal:
        return q_money(self.cash_in - self.cash_out, self.currency) or Decimal("0")

    @property
    def margin_pct(self) -> Decimal:
        """Profit over capital actually deployed."""
        basis = self.cost
        if not basis:
            return Decimal("0")
        return q_pct(self.profit / basis * Decimal("100")) or Decimal("0")

    def finalise(self) -> None:
        for name in ("cash_in", "cash_out", "cost", "market_value", "profit"):
            setattr(self, name, q_money(getattr(self, name), self.currency) or Decimal("0"))

    def as_dict(self) -> dict[str, Any]:
        return {
            "currency": self.currency,
            "trades": self.trades,
            "cash_in": str(self.cash_in),
            "cash_out": str(self.cash_out),
            "cost": str(self.cost),
            "market_value": str(self.market_value),
            "profit": str(self.profit),
            "net": str(self.net),
            "margin_pct": str(self.margin_pct),
        }


@dataclass
class Period:
    """One reporting period (a month, or a year for the YTD view).

    Money is kept per currency: a month can contain trades settled in different
    fiat, and adding IDR to USD would produce a meaningless number.
    """

    key: str
    label: str
    trades: int = 0
    by_currency: dict[str, CurrencyTotals] = field(default_factory=dict)
    display_totals: CurrencyTotals | None = None

    def bucket(self, currency: str) -> CurrencyTotals:
        cur = (currency or "?").upper()
        got = self.by_currency.get(cur)
        if got is None:
            got = CurrencyTotals(currency=cur)
            self.by_currency[cur] = got
        return got

    @property
    def currencies(self) -> list[str]:
        return sorted(self.by_currency)

    @property
    def dominant(self) -> CurrencyTotals | None:
        """The currency holding most deployed value; what the table shows."""
        if self.display_totals is not None and self.display_totals.trades > 0:
            return self.display_totals
        if not self.by_currency:
            return None
        return max(self.by_currency.values(), key=lambda c: c.cost)

    def finalise(self) -> None:
        for c in self.by_currency.values():
            c.finalise()
        if self.display_totals is not None:
            self.display_totals.finalise()

    def as_dict(self) -> dict[str, Any]:
        dom = self.dominant
        return {
            "key": self.key,
            "label": self.label,
            "trades": self.trades,
            "currency": dom.currency if dom else "",
            "by_currency": {k: v.as_dict() for k, v in self.by_currency.items()},
            "currencies": self.currencies,
            "trades_count_only": self.trades,
            "cost": str(dom.cost) if dom else "0",
            "cash_in": str(dom.cash_in) if dom else "0",
            "cash_out": str(dom.cash_out) if dom else "0",
            "market_value": str(dom.market_value) if dom else "0",
            "profit": str(dom.profit) if dom else "0",
            "margin_pct": str(dom.margin_pct) if dom else "0",
        }


MONTH_NAMES = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)


@dataclass
class Summary:
    """Aggregates for the header, grouped by quote currency."""

    trade_count: int = 0
    settled_count: int = 0
    unsettled_count: int = 0
    enriched_count: int = 0
    by_currency: dict[str, CurrencyTotals] = field(default_factory=dict)
    monthly: list[Period] = field(default_factory=list)
    display_currency: str = ""
    display_totals: CurrencyTotals | None = None

    @property
    def currencies(self) -> list[str]:
        return sorted(self.by_currency)

    @property
    def coins(self) -> list[str]:
        """Coin labels present, for the filter dropdown.

        Kept here rather than in the template because the option list must come
        from the same set the rows were filtered from.
        """
        return sorted(self._coins)

    _coins: set[str] = field(default_factory=set)

    @property
    def dominant(self) -> CurrencyTotals | None:
        if self.display_totals is not None and self.display_totals.trades > 0:
            return self.display_totals
        if not self.by_currency:
            return None
        return max(self.by_currency.values(), key=lambda c: c.cost)

    def bucket(self, currency: str) -> CurrencyTotals:
        cur = (currency or "?").upper()
        got = self.by_currency.get(cur)
        if got is None:
            got = CurrencyTotals(currency=cur)
            self.by_currency[cur] = got
        return got

    def period(self, key: str) -> Period | None:
        for p in self.monthly:
            if p.key == key:
                return p
        return None


def summarise(
    trades: Sequence[ValuedTrade],
    display_currency: str | None = None,
) -> Summary:
    """Aggregate by quote currency and optional target display currency.

    Money is bucketed per ``fiat_symbol`` and also optionally revalued into
    ``display_currency`` so a unified portfolio view can be shown in any
    supported currency.
    """
    summary = Summary(trade_count=len(trades))
    target_cur = display_currency.strip().upper() if display_currency else ""
    if target_cur:
        summary.display_currency = target_cur
        summary.display_totals = CurrencyTotals(currency=target_cur)
    periods: dict[str, Period] = {}

    for trade in trades:
        quote = (trade.quote_currency or trade.fiat_symbol or "?").upper()

        if trade.is_settled:
            summary.settled_count += 1
        else:
            summary.unsettled_count += 1
        if trade.enriched:
            summary.enriched_count += 1

        if trade.coin_label and trade.coin_label != "?":
            summary._coins.add(trade.coin_label)

        if not trade.is_settled:
            continue

        totals = summary.bucket(quote)
        totals.trades += 1
        totals.market_value += trade.market_value_quote or Decimal("0")
        if trade.profit is not None:
            totals.profit += trade.profit

        if trade.fiat_amount is not None:
            if trade.sign > 0:
                totals.cash_in += trade.fiat_amount
            elif trade.sign < 0:
                totals.cash_out += trade.fiat_amount
                totals.cost += trade.fiat_amount

        # Accumulate display totals if display currency is enabled
        if summary.display_totals is not None and trade.profit_display is not None:
            dt = summary.display_totals
            dt.trades += 1
            dt.market_value += trade.market_value_display or Decimal("0")
            dt.profit += trade.profit_display
            if trade.fiat_amount_display is not None:
                if trade.sign > 0:
                    dt.cash_in += trade.fiat_amount_display
                elif trade.sign < 0:
                    dt.cash_out += trade.fiat_amount_display
                    dt.cost += trade.fiat_amount_display

        if trade.trade_date is None:
            continue
        d = trade.trade_date
        key = f"{d.year:04d}-{d.month:02d}"
        period = periods.get(key)
        if period is None:
            period = Period(key=key, label=f"{MONTH_NAMES[d.month - 1]} {d.year}")
            if target_cur:
                period.display_totals = CurrencyTotals(currency=target_cur)
            periods[key] = period
        period.trades += 1
        bucket = period.bucket(quote)
        bucket.trades += 1
        bucket.market_value += trade.market_value_quote or Decimal("0")
        if trade.profit is not None:
            bucket.profit += trade.profit
        if trade.fiat_amount is not None:
            if trade.sign > 0:
                bucket.cash_in += trade.fiat_amount
            elif trade.sign < 0:
                bucket.cash_out += trade.fiat_amount
                bucket.cost += trade.fiat_amount

        if target_cur and period.display_totals is not None and trade.profit_display is not None:
            p_dt = period.display_totals
            p_dt.trades += 1
            p_dt.market_value += trade.market_value_display or Decimal("0")
            p_dt.profit += trade.profit_display
            if trade.fiat_amount_display is not None:
                if trade.sign > 0:
                    p_dt.cash_in += trade.fiat_amount_display
                elif trade.sign < 0:
                    p_dt.cash_out += trade.fiat_amount_display
                    p_dt.cost += trade.fiat_amount_display

    summary.monthly = [periods[k] for k in sorted(periods, reverse=True)]
    for totals in summary.by_currency.values():
        totals.finalise()
    if summary.display_totals is not None:
        summary.display_totals.finalise()
    for period in summary.monthly:
        period.finalise()
    return summary


__all__ = [
    "CurrencyTotals",
    "Period",
    "ZERO_DECIMAL",
    "minor_unit",
    "q_money",
    "SETTLED_STATUSES",
    "UNSETTLED_STATUSES",
    "Summary",
    "ValuedTrade",
    "apply_fx",
    "normalise_trade",
    "q_crypto",
    "q_idr",
    "q_pct",
    "resolve_date",
    "settled",
    "summarise",
    "to_decimal",
]