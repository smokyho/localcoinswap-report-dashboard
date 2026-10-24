# LocalCoinSwap API: verified behaviour

`lcs-openapi.json` in this directory is the vendor's published spec, fetched
from `https://api.localcoinswap.com/swagger.json`. **It is wrong in several
places that matter.** This file records what the live API actually does, each
item checked against a real account (1,874 trades, 2019-11 to 2026-10) on
2026-10-05.

Where the spec and reality disagree, this document wins. Anything below that is
not marked "verified" should be treated as unconfirmed.

Refresh the spec with `./fetch-api-spec.sh`. It refuses to write a truncated
download, because the docs page itself returns only a `<title>` and looks
suspiciously like a valid spec at a glance.

---

## Where to find the spec at all

The page a human sees, `https://api.localcoinswap.com/api-docs/`, is an Angular
SPA (drf-yasg + Redoc). Fetching it returns the document title and nothing else.

The spec is served by the backend at either:

- `https://api.localcoinswap.com/swagger.json`
- `https://api.localcoinswap.com/api-docs/?format=openapi`

Both return the same 243KB Swagger 2.0 document: 92 paths, 100 operations,
72 definitions. Base path `/api/v2`, HTTPS only.

## Getting the spec

Authentication is a single `ApiKey` scheme, sent as:

```
Authorization: ApiKey lcs_<prefix>_<secret>
```

Keys are created at <https://localcoinswap.com/preferences/api-token> and
require an authenticator code. Up to five per account. The account-wide limit
is **60 requests/minute**, on top of any per-endpoint limit.

---

## 1. Field naming: the spec says camelCase, the API sends snake_case

**Verified.** The biggest trap in the whole API.

The spec documents `coinAmount`, `fiatAmount`, `timeCreated`, `cryptoInUsd`,
`coinCurrency`. The live API returns:

| Spec says | Actually returns |
|---|---|
| `coinAmount` | `coin_amount` |
| `fiatAmount` | `fiat_amount` |
| `timeCreated` | `time_created` |
| `coinCurrency` | `coin_currency` |
| `fiatCurrency` | `fiat_currency` |
| `cryptoInUsd` | `crypto_in_usd` |
| `cryptoInLocalCurrency` | `crypto_in_local_currency` |

Nested objects follow suit: `trading_type`, `payment_method`, `created_by`,
`token_standard`, `price_formula_value`, `user_uuid`, `platform_volume`.

A client generated from the spec alone silently gets `null` for every numeric
field, with no error -- DRF just omits unmatched keys. Both spellings are
accepted by `lcs_report/valuation.py` (`_pick()`), because the spec is at least
a real published contract.

## 2. `/trades/list/` and `/trades/{uuid}/` return different fields

**Verified.** The offer is nested under **`offer`** on list responses and under
**`ad`** on detail responses.

The decisive difference: **`crypto_in_usd` is not on the list endpoint.** It
only appears on the detail endpoint. So a historical valuation needs one request
per trade, which is why this project has a background enrichment step.

| Field | `/trades/list/` | `/trades/{uuid}/` |
|---|---|---|
| `offer` / `ad` | `offer` | `ad` |
| `crypto_in_usd` | absent | present |
| `crypto_in_local_currency` | absent | present |
| `id` | absent | present |
| `fees`, `amount_to_escrow`, `non_custodial` | absent | present |
| `time_created`, `coin_amount`, `fiat_amount`, `status`, `buyer`, `seller` | present | present |

## 3. `crypto_in_usd` is the trade's TOTAL USD value, not a per-unit price

**Verified.** Confusing, and easy to get wrong by a factor of the coin amount.

For a trade of 100 USDT on 2024-06-15, `crypto_in_usd` is `99.931346510`. That
is $99.93 for the whole 100 USDT -- i.e. USDT was trading at $0.9993. It is not
the price of one unit.

So the market value is simply `crypto_in_usd x rate`, with no multiplication by
`coin_amount`.

## 4. `crypto_in_local_currency` revalues every trade at TODAY's rate

**Verified, and this is the bug the report exists to correct.**

Sampled `crypto_in_usd / crypto_in_local_currency` across trades spanning three
years:

| Trade date | `crypto_in_usd` | `crypto_in_local_currency` | Implied rate | ECB rate that day |
|---|---|---|---|---|
| 2023-07-04 | 70.03 | 1,253,735 | **17,902.27** | 15,013 |
| 2024-01-31 | 672.97 | 12,047,776 | **17,902.27** | 15,757 |
| 2024-06-15 | 99.93 | 1,788,998 | **17,902.27** | 16,486 |
| 2024-10-13 | 683.29 | 12,232,502 | **17,902.27** | 15,581 |
| 2026-07-14 | 209.76 | 3,755,139 | **17,902.27** | 18,060 |

The implied rate is identical across all of them and matches the current rate.
Across 1,718 samples it only wobbles between 17,902.27 and 17,922.84 (0.11%),
so the feed ticks slightly -- but it is always a *recent* rate, never the
historical one. This field is useless for valuing history.

`crypto_in_usd` by contrast is correctly frozen per trade (99.9313 for USDT on
2024-06-15 is right for that date).

## 5. `offer.price_formula_value` IS frozen, and equals `fiat_amount`

**Verified.** `coin_amount x price_formula_value == fiat_amount` exactly for
1,766 of 1,874 trades. The 108 that differ are all under 0.3% and are users
rounding to tidy rupiah figures (e.g. 4,600,000 paid against 4,586,236
computed). 88 of those are `COMPLETED`.

Two consequences:

- `price_formula_value` is a **snapshot taken at trade time**, despite living in
  an object called `price_formula` which sounds live. The offer's *listing*
  price is live; the copy embedded in a settled trade is not.
- It is the **executed** price, i.e. it includes the counterparty's margin. It
  is not a market rate, so it is not interchangeable with
  `crypto_in_usd x fx`.

`prefix_formula` shows how the listing price is built, for example:

```
exchange.usdtusd|v||exchange.usdidr|v||*|o||0.94|n||*|o
```

which reads USDT/USD x USD/IDR x 0.94. A `-6` `display_formula` means the
advertiser takes 6% off market, so a buyer pays ~4.7% under the mid rate once
the two FX legs are combined.

## 6. `next` pagination links are broken twice over

**Verified.** Two independent defects.

**a) The links are on `http://`, and the redirect drops the query string.**
`next` comes back as
`http://api.localcoinswap.com/api/v2/trades/list/?limit=100&offset=100`.
Requesting it triggers Django's http-to-https 301, and **that redirect discards
the query string**. You get page 1 back, whose `next` is the same broken link --
an infinite loop. This is what made the first version of this report hang.

**b) The links can also rewind.** After a request for `?limit=100&offset=100`,
the server returned `next` as `?limit=5&offset=5` -- back to the default page
size and near the start of the list. Following it would silently re-read
records already seen.

`lcs_report/lcs_client.py` therefore ignores `next` entirely and drives paging
with its own offset arithmetic, stopping on a short page or on reaching `count`.

## 7. `limit` silently caps at 100

**Verified.** Requesting 100, 200, 500 or 1000 all return exactly 100 records.
There is no error or warning.

## 8. There is no cap on how many trades you can read

**Verified.** For the reference account:

- `GET /trades/list/?limit=1&offset=0` reports `count: 1874`
- Walking `offset` in steps of 100 returns 1,874 rows with 1,874 **unique** UUIDs
- `offset=1874` returns 0 results and `next: null`

So if a report appears to stop at some number, that is a filter, not a limit.

## 9. `GET /trades/history/` is CSV, and is not capped -- but it is filtered

**Verified.** Returns `text/csv`, the entire history in a single request
(372KB for this account). Columns:

```
UUID,Time created,Time completed,Status,Buyer,Seller,Crypto Amount,
Cryptocurrency,Local Currency Amount,Local Currency,Market Value,
Trading Fee,Offer UUID,Payment Method
```

It returns exactly the **1,500 settled** trades -- `COMPLETED` only. Verified by
set comparison: identical to the settled UUID set from `/trades/list/`, no
extras either way. Dispute-resolved trades are labelled `COMPLETED` here.

The **Market Value** column is *not* historical. Checked against ECB:
for a 2019-11-13 BTC trade it implies ~$8,793/BTC, which is that date's BTC price
multiplied by the *current* IDR rate rather than the 2019 one. Same defect as
`crypto_in_local_currency`.

Consequence: this endpoint is a fast way to get amounts and dates, but supplies
no usable historical USD price. See section 12 for the workaround that was
considered and rejected.

## 10. `/trades/list/` has no date-range filter

**Verified.** The documented parameters are `status`, `coin_currency`,
`fiat_currency`, `payment_method`, `trade_type`, `trade_format`, `stage`,
`user`, `text`, `ordering`, `limit`, `offset`. No date parameters. Filtering by
date has to happen client-side, so the full list must be fetched.

`stage` accepts `live` / `past`. `trade_format` accepts
`standard` / `non-custodial`.

## 11. The spec over-declares authentication

**Verified.** Global `security` is `[{ApiKey: []}]` with **zero** per-operation
overrides, so the spec says all 100 operations need a key.

Tested unauthenticated:

| Endpoint | Without a key |
|---|---|
| `/trades/list/` | 401 |
| `/trades/history/` | 401 |
| `/trades/history-metrics/{user}/` | 401 |
| `/referrals/overview/` | 401 |
| `/referrals/trade-commissions/list/` | 401 |
| `/offers/trade-types/` | 401 |
| `/offers/dryrun-formula/` | 401 |
| `/profile/encrypted-blob/` | 401 |
| `/ip-whitelist/` | 401 |
| `/currencies/active-cryptos/` | **200** |
| `/currencies/{symbol}/` | **200** |
| `/offers/search/` | **200** |
| `/offers/payment-methods/` | **200** |
| `/offers/featured/`, `/offers/slugs/` | **200** |
| `/leaderboard/xp/`, `/volume/weekly/`, `/approx-geo/` | **200** |

Do not generate a client that hardcodes auth on every call because the spec says
so. Note `/trades/history/` is *not* public even though it is a "download".

Error shapes:

```json
{"message": "Authentication credentials were not provided.",
 "code": "not_authenticated", "error": true}
```

```json
{"message": "Invalid API key.", "code": "authentication_failed", "error": true}
```

A 401 carries `WWW-Authenticate: ApiKey`.

## 12. Currency metadata has no live price

**Verified.** `GET /currencies/{slug}/` returns `metadata.cmс_analytics`, but it
is **empty** (`usd_price: null`). So the exchange exposes no current price and a
mark-to-market valuation needs an external feed.

This project uses CoinGecko's free `/simple/price`, falling back to the public
Bybit and Binance spot APIs (see `lcs_report/pricing.py`). All three quote in
USD, so the display-currency table is derived by multiplying by the USD rate for
the target currency.

A shortcut was tried and **rejected**: `crypto_in_usd = MarketValue / currentRate`
would recover the USD price for every trade from a single CSV request, avoiding
1,874 detail calls. On 1,416 of 1,422 verified trades it matched within 0.05%,
but 6 rows were out by up to 99.99% and the divisor drifts with their live feed.
Too fragile for a financial report.

## 13. The quote currency: use `fiat_currency`, not `local_currency_symbol`

**Verified.** Two fields describe the currency a trade settled in, and they
disagree.

`/trades/list/` carries a nested `fiat_currency` object. On `/trades/{uuid}/`
there is a flat `local_currency_symbol`. For trades the list endpoint reports as
USD, the detail endpoint still reports `local_currency_symbol: IDR`:

| Field | Value for these trades |
|---|---|
| `fiat_currency.symbol` (list) | `USD` |
| `fiat_currency.slug` (list) | `united-states-dollar` |
| `local_currency_symbol` (detail) | `IDR` |

Corroborating evidence that the list value is the real one: the amounts on those
trades are 100.00 and 500.00 with two decimal places, which is plainly dollars.
`fiat_amount` of "100 IDR" for a 0.09 LTC purchase would be absurd.

`local_currency_symbol` appears to track an account-level display preference
rather than the settlement currency of the trade. **Use `fiat_currency` from the
list endpoint.** This matters directly: getting it wrong mis-buckets every
non-IDR trade into the wrong currency bucket and mixes money that must stay
apart.

## 14. The trading fee is in coin units, and has a floor

**Verified.** `fees` on a trade is denominated in **the coin**, not in the quote
currency. It is roughly 1% of `coin_amount`, but on small trades a per-coin
minimum applies instead, so the effective rate exceeds 1%.

Measured over the reference account's 1,873 priced trades:

| Coin | trades at ~1% | trades on the floor | lowest fee seen |
|---|---|---|---|
| BTC | 712 | 724 | 0.00001202 |
| LTC | 28 | 2 | 0.005 |
| USDT, BUSD, ETH, BNB, others | all | 0 | 1% of amount |

As a percentage of `fiat_amount` the fee is 0.00%, which confirms the unit. And
since `coin_amount x price_formula_value == fiat_amount` holds exactly, the fee
is **not** inside the fiat amount -- it is deducted separately, from the crypto
on a purchase.

BTC shows two distinct floor values, `0.000025` (503 trades) and exactly double
that, `0.00005` (221 trades). Both occur on `non_custodial: true` trades and
nothing else in the payload distinguishes them. The reason for the doubling is
**not determined** -- the report does not need it, because it uses the `fees`
value as given rather than recomputing it.

Consequences for any report:

- Profit must be reduced by the fee's **quote-currency value**,
  `fees x (crypto_in_usd / coin_amount) x fx(trade_date)`, not by a flat
  percentage. Hardcoding 1% overstates the fee on 724 BTC trades.
- On a **purchase** the crypto received is short by the fee, so the received
  value is `market_value - fee_value`.
- On a **sale** the seller bears it: the full coin leaves the account and the
  fee comes out of the proceeds, so
  `profit = (proceeds - fee_value) - value_of_coin_given`.
  On this account only one sale ever settled, so the sale-side treatment could
  not be confirmed empirically.

## 15. Other small things

- `info.version` in the spec is **empty**. LCS states the API may change on
  short notice. `/api/v2` is pinned here.
- Timestamps are unix seconds. There is no completion timestamp on
  `/trades/list/`; the CSV endpoint has `Time completed`.
- `buying`/`selling` direction is inferred from `buyer`/`seller` usernames
  versus the authenticated user. `LCS_USERNAME` avoids an extra request;
  otherwise `/referrals/overview/` nests a full `UserDetails` with `username`.
- `status` values seen: `COMPLETED` (1471), `REJECTED` (299), `EXPIRED` (62),
  `DISPUTE_RESOLVE_BUYER` (29), `CANCELLED_AFTER_ESCROW` (11), `CANCELLED` (1),
  `DISPUTED` (1). The spec's enum lists the full 28-state lifecycle.
- Coin identity needs the chain: `coin_currency.token_standard` distinguishes
  `USDT-TRC20` from `USDT-ERC20` from `USDT-BEP20`, which are different assets
  and price differently on-chain.
- `/profile/encrypted-blob/` can return `encryptedAccountKey` and
  `encryptedMnemonic`. Do not log response bodies from it.

## 14. Worked example: one trade end to end

```
status            COMPLETED
coin_currency     {"symbol": "USDT", "token_standard": "TRC20", "slug": "USDT-TRC20"}
coin_amount       100.000000000000000000
fiat_amount       1548667.06
time_created      1718445917            -> 2024-06-15
offer.trading_type.slug                buy
offer.price_formula_value              15486.670555828881
offer.price_formula.prefix_formula     exchange.usdtusd|v||exchange.usdidr|v||*|o||0.94|n||*|o
```

From `/trades/{uuid}/`:

```
crypto_in_usd              99.931346510000000000
crypto_in_local_currency   1788997.797787870000000000
```

Derived:

```
ECB USD/IDR on 2024-05-31 (2024-06-15 is a Saturday)   16,257
market value at trade time  = 99.931346510 x 16,257  = 1,624,584 IDR
paid                        = 1,548,667.06 IDR
profit                      = 1,624,584 - 1,548,667 = +75,917 IDR

LCS's own figure            = 99.931346510 x ~17,902  = 1,788,998 IDR
LCS overstatement on this trade                      = +164,414 IDR (+10.8%)
```

And the executed price cross-check:

```
100 x 15,486.670555828881 = 1,548,667.06 = fiat_amount   (exact)
```

## Local references

| Path | What it is |
|---|---|
| `docs/lcs-openapi.json` | Vendored spec (regenerate with `fetch-api-spec.sh`) |
| `docs/api-behaviour.md` | This file |
| `tests/fixtures/lcs_trade_list_sample.json` | Redacted real payloads from both endpoints |
| `tests/fixtures/lcs_trade_detail_sample.json` | Redacted real detail payload |
| `lcs_report/valuation.py` | `_pick()` handles both field casings |
| `lcs_report/lcs_client.py` | Offset-driven pagination, ignoring `next` |
