"""Tests for the valuation core, using real LocalCoinSwap payload shapes.

Field casing is the trap here: the published spec says camelCase, the live API
sends snake_case. Both must be accepted.

The rule under test: profit is measured in each trade's own quote currency, at
the rate for that trade's own date. Nothing is ever summed across currencies.
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lcs_report.fx import FxError, FxStore, resolve_trade_date  # noqa: E402
from lcs_report.service import _full_year, _roll_up, _year_to_date  # noqa: E402
from lcs_report.valuation import (  # noqa: E402
    apply_fx,
    minor_unit,
    normalise_trade,
    q_money,
    resolve_date,
    settled,
    summarise,
    to_decimal,
)

TZ = ZoneInfo("Asia/Jakarta")

# Deterministic stand-in for the historical desk.
RATES = {"IDR": Decimal("16257"), "SGD": Decimal("1.34"), "EUR": Decimal("0.92")}


def lookup(trade, currency: str):
    rate = RATES.get(currency.upper())
    if rate is None:
        raise FxError(f"no series for {currency}")
    return trade.trade_date, rate, False


def make_trade(**over):
    """A snake_case trade shaped exactly like /trades/list/ returns."""
    base = {
        "status": "COMPLETED",
        "uuid": "aaaa-bbbb-cccc",
        "buyer": {"username": "me", "last_seen": 1791165979},
        "seller": {"username": "alice", "last_seen": 1779822178},
        "fiat_amount": "1548667.06",
        "coin_amount": "100.000000000000000000",
        "time_created": 1718445917,
        "coin_currency": {
            "title": "Tether",
            "symbol": "USDT",
            "token_standard": "TRC20",
            "slug": "USDT-TRC20",
        },
        "fiat_currency": {"title": "Indonesian Rupiah", "symbol": "IDR"},
        "offer": {
            "trading_type": {"slug": "buy", "name": "buy"},
            "payment_method": {"name": "Bank Transfer", "slug": "bank-transfers"},
            "price_formula_value": "15486.670555828881",
            "location_name": "Denpasar",
        },
        "crypto_in_usd": "99.931346510000000000",
    }
    base.update(over)
    return base


def valued(uuid="t", *, day=date(2024, 6, 15), username="me", **over):
    t = normalise_trade(make_trade(uuid=uuid, **over), username=username)
    t.trade_date = day
    return apply_fx([t], rate_lookup=lookup, base_currency="USD")[0]


# --------------------------------------------------------------------------
# Decimal parsing
# --------------------------------------------------------------------------


def test_to_decimal_never_uses_binary_float() -> None:
    assert to_decimal(0.1) == Decimal("0.1")
    assert str(to_decimal(0.1)) == "0.1"
    assert to_decimal("1548667.060000000000000000") == Decimal("1548667.06")
    assert to_decimal(42) == Decimal("42")
    assert to_decimal(None) is None
    assert to_decimal("") is None
    assert to_decimal("abc") is None
    assert to_decimal(True) is None


def test_minor_units_respect_the_currency() -> None:
    """Rounding a USD profit to a whole unit would erase it."""
    assert minor_unit("IDR") == Decimal("1")
    assert minor_unit("JPY") == Decimal("1")
    assert minor_unit("USD") == Decimal("0.01")
    assert minor_unit("SGD") == Decimal("0.01")
    assert q_money(Decimal("10.5"), "USD") == Decimal("10.50")
    assert q_money(Decimal("10.5"), "IDR") == Decimal("11")


# --------------------------------------------------------------------------
# snake_case is the live shape
# --------------------------------------------------------------------------


def test_parses_real_snake_case_shape() -> None:
    t = normalise_trade(make_trade(), username="me")
    assert t.coin_amount == Decimal("100.0000000000")
    # IDR has no minor unit, so the 18-decimal input is rounded to whole rupiah.
    assert t.fiat_amount == Decimal("1548667")
    assert t.coin_symbol == "USDT"
    assert t.coin_label == "USDT-TRC20"
    assert t.fiat_symbol == "IDR"
    assert t.direction == "buy"
    assert t.counterpart == "alice"
    assert t.created_ts == 1718445917


def test_camel_case_still_accepted() -> None:
    camel = {
        "uuid": "x",
        "status": "COMPLETED",
        "coinAmount": "1.5",
        "fiatAmount": "24000",
        "cryptoInUsd": "65000",
        "timeCreated": 1718445917,
        "coinCurrency": {"symbol": "BTC", "slug": "BTC"},
        "fiatCurrency": {"symbol": "IDR"},
        "buyer": {"username": "me"},
        "seller": {"username": "bob"},
        "ad": {"price_formula_value": "16000"},
    }
    t = normalise_trade(camel, username="me")
    assert t.coin_amount == Decimal("1.50000000")
    assert t.coin_in_usd == Decimal("65000")
    assert t.direction == "buy"


def test_token_standard_is_folded_into_label() -> None:
    trc = normalise_trade(make_trade(), username="me")
    erc = normalise_trade(
        make_trade(coin_currency={"symbol": "USDT", "token_standard": "ERC20"}),
        username="me",
    )
    btc = normalise_trade(
        make_trade(coin_currency={"symbol": "BTC", "token_standard": None}), username="me"
    )
    assert (trc.coin_label, erc.coin_label, btc.coin_label) == (
        "USDT-TRC20",
        "USDT-ERC20",
        "BTC",
    )


def test_offer_and_ad_both_resolved() -> None:
    a = normalise_trade(make_trade(), username="me")
    b = normalise_trade(
        make_trade(offer=None, ad={"price_formula_value": "999"}), username="me"
    )
    assert a.price_formula_value == Decimal("15486.670555828881")
    assert b.price_formula_value == Decimal("999")


# --------------------------------------------------------------------------
# Profit is in the quote currency, at that trade's own rate
# --------------------------------------------------------------------------


def test_profit_is_in_the_quote_currency_at_its_own_rate() -> None:
    t = valued(fiat_amount="1500000", crypto_in_usd="90")
    assert t.quote_currency == "IDR"
    assert t.fx_rate == Decimal("16257")
    assert t.market_value_quote == Decimal("90") * Decimal("16257")
    assert t.profit == t.market_value_quote - Decimal("1500000")


def test_quote_currency_is_whichever_leg_settled() -> None:
    for fiat in ("IDR", "USD", "SGD"):
        t = valued(fiat_currency={"symbol": fiat}, fiat_amount="100")
        assert t.quote_currency == fiat


def test_usd_quote_needs_no_fx_lookup() -> None:
    """crypto_in_usd and a USD quote are already the same units."""

    def boom(day, currency):
        raise AssertionError("must not look up a rate for a USD-quoted trade")

    t = normalise_trade(
        make_trade(
            fiat_currency={"symbol": "USD"},
            fiat_amount="90.00",
            crypto_in_usd="100.00",
        ),
        username="me",
    )
    t.trade_date = date(2024, 6, 15)
    out = apply_fx([t], rate_lookup=boom, base_currency="USD")[0]
    assert out.fx_rate == Decimal(1)
    assert out.fx_snapped is False
    assert out.profit == Decimal("10.00"), "cents preserved"


def test_usd_profit_keeps_its_cents() -> None:
    t = valued(
        fiat_currency={"symbol": "USD"},
        fiat_amount="89.995",
        crypto_in_usd="90.005",
    )
    assert t.profit == Decimal("0.01"), f"got {t.profit}"
    assert t.profit.as_tuple().exponent == -2


def test_different_quote_currencies_use_different_series() -> None:
    seen = []

    def spy(trade, currency):
        seen.append(currency)
        return trade.trade_date, RATES[currency], False

    trades = []
    for fiat in ("IDR", "SGD"):
        t = normalise_trade(
            make_trade(uuid=f"u{fiat}", fiat_currency={"symbol": fiat}), username="me"
        )
        t.trade_date = date(2024, 6, 15)
        trades.append(t)
    apply_fx(trades, rate_lookup=spy, base_currency="USD")
    assert sorted(seen) == ["IDR", "SGD"]


def test_each_trade_uses_its_own_date() -> None:
    """Two trades a year apart must not share a rate."""

    def stepping(trade, currency):
        rate = Decimal("16000") if trade.trade_date.year == 2024 else Decimal("18000")
        return trade.trade_date, rate, False

    early = normalise_trade(make_trade(uuid="a"), username="me")
    early.trade_date = date(2024, 6, 15)
    late = normalise_trade(make_trade(uuid="b"), username="me")
    late.trade_date = date(2025, 6, 15)

    out = apply_fx([early, late], rate_lookup=stepping, base_currency="USD")
    assert out[0].fx_rate == Decimal("16000")
    assert out[1].fx_rate == Decimal("18000")


def test_sell_profit_is_proceeds_minus_value() -> None:
    t = valued(offer={"trading_type": {"slug": "sell"}}, username="alice")
    assert t.direction == "sell"
    assert t.profit == t.fiat_amount - t.market_value_quote


def test_profit_sign_follows_direction() -> None:
    """A buy and a sell of the same size must not produce the same number."""
    buy = valued(uuid="b", offer={"trading_type": {"slug": "buy"}}, username="me")
    sell = valued(uuid="s", offer={"trading_type": {"slug": "sell"}}, username="alice")
    assert buy.direction == "buy" and buy.sign == -1
    assert sell.direction == "sell" and sell.sign == 1
    assert buy.profit == buy.market_value_quote - buy.fiat_amount
    assert sell.profit == sell.fiat_amount - sell.market_value_quote
    assert buy.profit == -sell.profit, "mirror trades must mirror"


def test_profit_blank_without_usd_price() -> None:
    t = valued(crypto_in_usd=None)
    assert t.profit is None
    assert t.market_value_quote is None


def test_fx_failure_isolated_to_one_trade() -> None:
    good = normalise_trade(make_trade(uuid="g"), username="me")
    good.trade_date = date(2024, 6, 15)
    bad = normalise_trade(
        make_trade(uuid="b", fiat_currency={"symbol": "ZAR"}), username="me"
    )
    bad.trade_date = date(2024, 6, 15)

    out = apply_fx([good, bad], rate_lookup=lookup, base_currency="USD")
    by = {t.uuid: t for t in out}
    assert by["g"].profit is not None
    assert by["b"].profit is None
    assert any("FX lookup failed for ZAR" in n for n in by["b"].notes)


# --------------------------------------------------------------------------
# Summaries keep currencies apart
# --------------------------------------------------------------------------


def test_currencies_are_never_summed() -> None:
    trades = []
    for fiat, paid in (("IDR", "1500000"), ("USD", "90"), ("SGD", "120")):
        t = normalise_trade(
            make_trade(
                uuid=f"u{fiat}",
                fiat_currency={"symbol": fiat},
                fiat_amount=paid,
                crypto_in_usd="100.00",
            ),
            username="me",
        )
        t.trade_date = date(2024, 6, 15)
        trades.append(t)
    s = summarise(apply_fx(trades, rate_lookup=lookup, base_currency="USD"))

    assert s.currencies == ["IDR", "SGD", "USD"]
    idr = s.by_currency["IDR"]
    usd = s.by_currency["USD"]
    assert idr.profit == Decimal("100") * Decimal("16257") - Decimal("1500000")
    assert usd.profit == Decimal("10.00")
    # The dominant is chosen by deployed value, not alphabetically.
    assert s.dominant.currency == "IDR"
    assert "profit_idr" not in dir(s), "no blended cross-currency total"


def test_unsettled_excluded_from_money() -> None:
    ok = valued(uuid="s", fiat_amount="1000000")
    bad = valued(uuid="r", status="REJECTED", fiat_amount="9000000")
    s = summarise([ok, bad])
    assert s.trade_count == 2
    assert s.settled_count == 1
    assert s.by_currency["IDR"].cash_out == Decimal("1000000")


@pytest.mark.parametrize(
    "status,expected",
    [
        ("COMPLETED", True),
        ("CRYPTO_REL", True),
        ("DISPUTE_RESOLVE_BUYER", True),
        ("REJECTED", False),
        ("EXPIRED", False),
        ("CANCELLED", False),
        ("CANCELLED_AFTER_ESCROW", False),
        ("DISPUTED", False),
    ],
)
def test_settled_classification(status: str, expected: bool) -> None:
    assert settled(status) is expected


def test_monthly_buckets_newest_first() -> None:
    trades = []
    for i, (y, m) in enumerate([(2024, 1), (2024, 1), (2024, 2), (2025, 3)]):
        t = normalise_trade(make_trade(uuid=f"t{i}"), username="me")
        t.trade_date = date(y, m, 15)
        trades.append(t)
    s = summarise(apply_fx(trades, rate_lookup=lookup, base_currency="USD"))
    assert [p.key for p in s.monthly] == ["2025-03", "2024-02", "2024-01"]
    assert s.period("2024-01").trades == 2


def test_month_keeps_each_currency_apart() -> None:
    trades = []
    for fiat, paid in (("IDR", "1500000"), ("USD", "90.00")):
        t = normalise_trade(
            make_trade(
                uuid=f"u{fiat}",
                fiat_currency={"symbol": fiat},
                fiat_amount=paid,
                crypto_in_usd="100.00",
            ),
            username="me",
        )
        t.trade_date = date(2024, 6, 15)
        trades.append(t)
    s = summarise(apply_fx(trades, rate_lookup=lookup, base_currency="USD"))
    period = s.monthly[0]
    assert period.currencies == ["IDR", "USD"]
    # A USD profit must not be inflated by an IDR-sized amount.
    assert period.by_currency["USD"].cash_out == Decimal("90.00")
    assert period.by_currency["USD"].profit == Decimal("10.00")
    assert period.by_currency["IDR"].cash_out == Decimal("1500000")


def test_monthly_profit_sums_to_summary() -> None:
    trades = []
    for i in range(6):
        t = normalise_trade(make_trade(uuid=f"t{i}"), username="me")
        t.trade_date = date(2024, (i % 4) + 1, 10)
        trades.append(t)
    s = summarise(apply_fx(trades, rate_lookup=lookup, base_currency="USD"))
    for cur in s.currencies:
        assert sum(p.by_currency[cur].profit for p in s.monthly) == (
            s.by_currency[cur].profit
        )
    assert sum(p.trades for p in s.monthly) == s.trade_count


def test_currency_totals_margin() -> None:
    s = summarise([valued(fiat_amount="1500000", crypto_in_usd="90")])
    c = s.by_currency["IDR"]
    assert c.margin_pct == (c.profit / c.cost * 100).quantize(Decimal("0.01"))
    assert c.net == -c.cash_out + c.cash_in


def test_summary_of_nothing() -> None:
    s = summarise([])
    assert s.trade_count == 0
    assert s.currencies == []
    assert s.dominant is None
    assert s.monthly == []


# --------------------------------------------------------------------------
# YTD roll-up
# --------------------------------------------------------------------------


def test_year_to_date_rolls_up_only_that_year() -> None:
    trades = []
    for i, y in enumerate([2024, 2025, 2026, 2026]):
        t = normalise_trade(make_trade(uuid=f"t{i}"), username="me")
        t.trade_date = date(y, 4, 1)
        trades.append(t)
    s = summarise(apply_fx(trades, rate_lookup=lookup, base_currency="USD"))
    assert _year_to_date(s, 2026).trades == 2
    assert _year_to_date(s, 2026).label == "YTD 2026"
    assert _full_year(s, 2025).trades == 1
    assert _full_year(s, 2025).label == "2025"


def test_roll_up_preserves_currency_split() -> None:
    trades = []
    for i, fiat in enumerate(["IDR", "USD", "IDR"]):
        t = normalise_trade(
            make_trade(uuid=f"u{i}", fiat_currency={"symbol": fiat}), username="me"
        )
        t.trade_date = date(2026, 4, 1)
        trades.append(t)
    s = summarise(apply_fx(trades, rate_lookup=lookup, base_currency="USD"))
    ytd = _year_to_date(s, 2026)
    assert ytd.currencies == ["IDR", "USD"]
    assert ytd.by_currency["IDR"].trades == 2
    assert ytd.by_currency["USD"].trades == 1


def test_roll_up_of_nothing_is_zero() -> None:
    r = _roll_up("2026", "YTD 2026", [])
    assert r.trades == 0
    assert r.currencies == []
    assert r.dominant is None


# --------------------------------------------------------------------------
# LCS comparison, and dates
# --------------------------------------------------------------------------


def test_lcs_comparison_only_for_idr() -> None:
    t = valued(
        crypto_in_usd="99.931346510",
        crypto_in_local_currency="1788997.80",
    )
    assert t.enriched is True
    assert t.lcs_drift_idr == t.lcs_local_value_idr - t.market_value_quote
    assert t.lcs_drift_idr > 0

    usd_trade = valued(
        fiat_currency={"symbol": "USD"}, crypto_in_local_currency="1788997.80"
    )
    assert usd_trade.enriched is False, "LCS's field is IDR-only; do not compare"


def test_resolve_date_uses_configured_zone() -> None:
    assert resolve_date(1718445917, TZ) == date(2024, 6, 15)
    assert resolve_date(1718445917, timezone.utc) == date(2024, 6, 15)


def test_resolve_date_can_shift_across_midnight() -> None:
    ts = int(datetime(2024, 6, 14, 23, 0, tzinfo=timezone.utc).timestamp())
    assert resolve_date(ts, timezone.utc) == date(2024, 6, 14)
    assert resolve_date(ts, TZ) == date(2024, 6, 15)


def test_resolve_date_handles_garbage() -> None:
    assert resolve_date(None, TZ) is None
    assert resolve_date("nope", TZ) is None
    assert resolve_trade_date(None, timezone.utc) is None


# --------------------------------------------------------------------------
# Trading fee
# --------------------------------------------------------------------------


def test_fee_is_parsed_from_the_payload() -> None:
    t = valued(coin_amount="10", fees="0.1", crypto_in_usd="1000")
    assert t.fee_amount == Decimal("0.1")
    assert t.fee_ratio == Decimal("0.01")


def test_fee_is_valued_in_the_coin_not_the_fiat() -> None:
    """1% of coin_amount, priced at that day's rate."""
    t = valued(coin_amount="10", fees="0.1", crypto_in_usd="1000", fiat_amount="1500000")
    # market = 1000 USD x 16257; fee = 0.1 coin = 1% of that
    assert t.market_value_quote == Decimal("1000") * Decimal("16257")
    assert t.fee_value_quote == t.market_value_quote * Decimal("0.01")
    assert t.net_value_quote == t.market_value_quote - t.fee_value_quote


def test_buy_profit_is_net_of_fee() -> None:
    t = valued(coin_amount="10", fees="0.1", crypto_in_usd="1000", fiat_amount="1500000")
    # You receive slightly less crypto than the gross market value.
    assert t.profit == t.net_value_quote - t.fiat_amount
    assert t.profit < t.market_value_quote - t.fiat_amount


def test_sell_profit_bears_the_fee() -> None:
    """The seller hands over the full coin; the fee comes out of the proceeds."""
    t = valued(
        offer={"trading_type": {"slug": "sell"}},
        username="alice",
        coin_amount="10",
        fees="0.1",
        crypto_in_usd="1000",
        fiat_amount="1500000",
    )
    assert t.direction == "sell"
    assert t.profit == (t.fiat_amount - t.fee_value_quote) - t.market_value_quote


def test_fee_reduces_profit_exactly_once() -> None:
    """A zero fee must reproduce the gross figure, or it is applied twice."""
    with_fee = valued(coin_amount="10", fees="0.1", crypto_in_usd="1000", fiat_amount="1500000")
    without = valued(coin_amount="10", crypto_in_usd="1000", fiat_amount="1500000")
    assert without.fee_value_quote == 0
    assert with_fee.profit == without.profit - with_fee.fee_value_quote


def test_missing_fee_falls_back_to_gross() -> None:
    t = valued(coin_amount="10", crypto_in_usd="1000", fiat_amount="1500000")
    assert t.fee_amount is None
    assert t.fee_value_quote == 0
    assert t.profit == t.market_value_quote - t.fiat_amount


def test_zero_fee_is_not_a_division_error() -> None:
    t = valued(coin_amount="10", fees="0", crypto_in_usd="1000", fiat_amount="1500000")
    assert t.fee_ratio == 0
    assert t.fee_value_quote == 0
    assert t.profit is not None


def test_display_currency_revalues_trades_and_summarises_in_target() -> None:
    rates = {
        "IDR": Decimal("16000"),
        "EUR": Decimal("0.90"),
    }

    def lookup(trade, currency):
        return trade.trade_date, rates[currency], False

    t = normalise_trade(
        make_trade(
            uuid="t1",
            fiat_currency={"symbol": "IDR"},
            fiat_amount="1600000",
            crypto_in_usd="100",
        ),
        username="me",
    )
    t.trade_date = date(2024, 6, 15)
    valued_trades = apply_fx([t], rate_lookup=lookup, base_currency="USD", display_currency="EUR")
    assert len(valued_trades) == 1
    v = valued_trades[0]
    assert v.display_currency == "EUR"
    assert v.market_value_display == Decimal("90.00")
    assert v.fiat_amount_display == Decimal("90.00")

    summary = summarise(valued_trades, display_currency="EUR")
    assert summary.display_totals is not None
    assert summary.display_totals.currency == "EUR"
    assert summary.display_totals.market_value == Decimal("90.00")
    assert summary.display_totals.cost == Decimal("90.00")
    assert summary.dominant.currency == "EUR"

