"""Contract tests pinned to real, redacted API payloads.

The fixtures in tests/fixtures/ are genuine responses with usernames replaced.
They exist so the behaviours documented in docs/api-behaviour.md stay true:

* the API sends snake_case while the spec says camelCase
* `/trades/list/` has no `crypto_in_usd`; only the detail endpoint does
* `crypto_in_usd` is the trade's TOTAL USD value, not a per-unit price
* `crypto_in_local_currency` equals `crypto_in_usd` at a *recent* rate
* the offer snapshot's `price_formula_value` x `coin_amount` == `fiat_amount`

If the vendored spec and these fixtures ever disagree again, this is where it
should be noticed.
"""

from __future__ import annotations

import json
import sys
from datetime import date, timezone
from decimal import Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lcs_report.valuation import (  # noqa: E402
    apply_fx,
    normalise_trade,
    resolve_date,
)

FIX = ROOT / "tests" / "fixtures"
LIST_SAMPLE = json.loads((FIX / "lcs_trade_list_sample.json").read_text())
DETAIL_SAMPLE = json.loads((FIX / "lcs_trade_detail_sample.json").read_text())
SPEC = json.loads((ROOT / "docs" / "lcs-openapi.json").read_text())

SELF = "acct_self"
PEER = "peer_a"


def _merged() -> dict:
    """A list row with the detail record's pricing fields merged in."""
    row = dict(LIST_SAMPLE["results"][0])
    row["crypto_in_usd"] = DETAIL_SAMPLE["crypto_in_usd"]
    row["crypto_in_local_currency"] = DETAIL_SAMPLE["crypto_in_local_currency"]
    return row


# --------------------------------------------------------------------------
# The spec is wrong in known ways -- assert that so it cannot be "corrected"
# by regenerating it and trusting the result.
# --------------------------------------------------------------------------


def test_spec_documents_camel_case_that_the_api_does_not_send() -> None:
    """Guard for the trap: if this ever flips, re-check the whole parser."""
    documented = SPEC["definitions"]["TradeRead"]["properties"]
    assert "coinAmount" in documented and "fiatAmount" in documented
    assert "timeCreated" in documented and "cryptoInUsd" in documented

    live_keys = set(LIST_SAMPLE["results"][0])
    for camel in ("coinAmount", "fiatAmount", "timeCreated", "cryptoInUsd"):
        assert camel not in live_keys, f"{camel} unexpectedly present in a real payload"
    for snake in ("coin_amount", "fiat_amount", "time_created"):
        assert snake in live_keys


def test_spec_overstates_authentication() -> None:
    """The spec declares ApiKey globally with no public overrides."""
    assert SPEC["securityDefinitions"]["ApiKey"]["type"] == "apiKey"
    overrides = [
        op.get("security")
        for methods in SPEC["paths"].values()
        for op in methods.values()
        if isinstance(op, dict) and "security" in op
    ]
    assert overrides == [], "an operation now explicitly waives auth; update the docs"


def test_list_endpoint_omits_crypto_in_usd() -> None:
    """Forces the per-trade enrichment step to stay."""
    assert "crypto_in_usd" not in LIST_SAMPLE["results"][0]
    assert DETAIL_SAMPLE["crypto_in_usd"] is not None


def test_offer_key_differs_between_endpoints() -> None:
    """List nests under `offer`; detail nests under `ad`."""
    assert "offer" in LIST_SAMPLE["results"][0]
    assert DETAIL_SAMPLE["ad_keys_present"], "detail payload should carry an `ad` block"
    assert "ad" not in LIST_SAMPLE["results"][0]


# --------------------------------------------------------------------------
# Real values
# --------------------------------------------------------------------------


def test_crypto_in_usd_is_total_not_per_unit() -> None:
    """100 USDT for ~$99.93 total, not $99.93 per unit."""
    row = LIST_SAMPLE["results"][0]
    coin_amount = Decimal(row["coin_amount"])
    total_usd = Decimal(DETAIL_SAMPLE["crypto_in_usd"])
    assert coin_amount == Decimal("100.000000000000000000")
    assert Decimal("99") < total_usd < Decimal("101"), "must be the whole trade"
    # A per-unit reading would put this near 10,000 USD.
    assert total_usd < coin_amount


def test_lcs_local_currency_uses_a_recent_rate_not_the_historical_one() -> None:
    """The defect the report corrects."""
    usd = Decimal(DETAIL_SAMPLE["crypto_in_usd"])
    local = Decimal(DETAIL_SAMPLE["crypto_in_local_currency"])
    implied = local / usd

    trade_day = resolve_date(LIST_SAMPLE["results"][0]["time_created"], timezone.utc)
    assert isinstance(trade_day, date)

    historical = Decimal("16257")  # ECB on 2024-05-31, verified
    assert abs(implied - historical) / historical > Decimal("0.05"), (
        "if this now matches the historical rate, LCS fixed it -- "
        "revisit whether the report still needs its own FX source"
    )
    # It matches a recent rate instead.
    assert Decimal("17000") < implied < Decimal("19000")


def test_price_formula_value_reproduces_fiat_amount() -> None:
    """The offer snapshot is frozen and equals the executed price."""
    row = LIST_SAMPLE["results"][0]
    computed = Decimal(row["coin_amount"]) * Decimal(row["offer"]["price_formula_value"])
    paid = Decimal(row["fiat_amount"])
    assert abs(computed - paid) / paid < Decimal("0.001")


def test_normalise_trades_a_real_payload() -> None:
    t = normalise_trade(_merged(), username=SELF)
    assert t.status == "COMPLETED"
    assert t.coin_label == "USDT-TRC20", "chain must survive"
    assert t.direction == "buy"
    assert t.counterpart and t.counterpart != SELF
    assert t.fiat_amount is not None
    assert t.coin_in_usd is not None
    assert t.lcs_local_value_idr is not None
    assert not [n for n in t.notes if "per-trade fetch" in n]


def test_valuing_the_real_payload_reproduces_the_documented_figures() -> None:
    """The worked example in docs/api-behaviour.md."""
    t = normalise_trade(_merged(), username=SELF)
    t.trade_date = resolve_date(t.created_ts, timezone.utc)

    def lookup(trade, currency: str):
        # 2024-06-15 was a Saturday, so the prior business day is used.
        assert currency == "IDR", "a single-currency report should ask only for IDR"
        return date(2024, 5, 31), Decimal("16257"), True

    v, = apply_fx([t], rate_lookup=lookup, base_currency="USD")

    assert v.market_value_quote == Decimal("1624584")
    assert v.fiat_amount == Decimal("1548667")
    assert v.quote_currency == "IDR"
    assert v.profit == Decimal("75917")
    assert v.lcs_drift_idr is not None and v.lcs_drift_idr > 0


def test_timestamps_resolve_to_the_documented_date() -> None:
    t = normalise_trade(_merged(), username=SELF)
    assert resolve_date(t.created_ts, timezone.utc) == date(2024, 6, 15)


# --------------------------------------------------------------------------
# The vendored spec itself
# --------------------------------------------------------------------------


def test_vendored_spec_is_complete() -> None:
    assert len(SPEC["paths"]) >= 90
    assert len(SPEC["definitions"]) >= 70
    assert SPEC["basePath"] == "/api/v2"
    assert "ApiKey" in SPEC["securityDefinitions"]


def test_spec_file_has_the_expected_reference() -> None:
    readme = (ROOT / "README.md").read_text()
    assert "docs/api-behaviour.md" in readme
    assert (ROOT / "docs" / "fetch-api-spec.sh").exists()


# --------------------------------------------------------------------------
# Privacy guard on the fixtures themselves
# --------------------------------------------------------------------------


def test_fixtures_contain_no_real_identifiers() -> None:
    """Fixtures are committed, so they must carry no real handles or UUIDs.

    Only the monetary amounts are kept real -- they are what the arithmetic is
    verified against, and they reveal nothing about who was transacting.
    """
    import re

    allowed = {"acct_self", "peer_a", "peer_b", "peer_x"}
    for path in FIX.glob("*.json"):
        text = path.read_text()

        for name in re.findall(r'"username":\s*"([^"]+)"', text):
            assert name in allowed, f"{path.name} leaks the username {name!r}"

        # user_uuid values must be gone.
        for value in re.findall(r'"user_uuid":\s*"([^"]+)"', text):
            assert value == "uuid-redacted", f"{path.name} leaks a user UUID"

        # A real uuid appears as a trade/offer identifier; that is acceptable,
        # but nothing should look like a user profile link or bio.
        assert "linktr.ee" not in text
        # The LCS API's own pagination link is deliberately kept, because its
        # http:// scheme is the defect documented in section 6. Anything else
        # pointing off-site would be a profile or external link.
        for url in re.findall(r'"(https?://[^"]+)"', text):
            assert url.startswith("http://api.localcoinswap.com/api/v2/"), (
                f"{path.name} contains an unexpected URL: {url}"
            )


def test_quote_currency_comes_from_fiat_currency_not_the_detail_field() -> None:
    """`local_currency_symbol` on the detail endpoint is not the trade's currency.

    Verified against the live account: trades the list endpoint reports as USD
    carry `local_currency_symbol: IDR` on their detail record. Trusting that
    field would mis-bucket every non-IDR trade.
    """
    list_row = LIST_SAMPLE["results"][0]
    assert list_row["fiat_currency"]["symbol"] == "IDR"

    t = normalise_trade(_merged(), username=SELF)
    assert t.fiat_symbol == list_row["fiat_currency"]["symbol"]

    # Even when a payload carries the misleading flat field, we ignore it:
    # normalise_trade reads only fiat_currency.
    payload = dict(_merged(), local_currency_symbol="XYZ")
    assert normalise_trade(payload, username=SELF).fiat_symbol == "IDR"
