# LCS Trading Report

Values LocalCoinSwap trades in non USD local currency using the exchange rate of the day each
trade happened**, rather than today's rate.
For example here we are using IDR.

![report](docs/report.png)

## Why this is needed

LocalCoinSwap's own `crypto_in_local_currency` field converts *every* trade at
*today's* exchange rate. The report never uses that field; the bug it exposed
is described in [`docs/api-behaviour.md`](docs/api-behaviour.md). Measured against a live account (2026-10-05):

| Trade date | `crypto_in_usd` | `crypto_in_local_currency` | Implied rate | ECB that day |
|---|---|---|---|---|
| 2023-07-04 | 70.03 | 1,253,735 | **17,902.27** | 15,013 |
| 2024-01-31 | 672.97 | 12,047,776 | **17,902.27** | 15,757 |
| 2024-10-13 | 683.29 | 12,232,502 | **17,902.27** | 15,581 |
| 2026-07-14 | 209.76 | 3,755,139 | **17,902.27** | 18,060 |

The implied rate is identical across three years of trades, to four decimal
places, and matches today's rate. That field is useless for historical
valuation.

`crypto_in_usd`, by contrast, **is** correctly frozen per trade. So the fix is
one multiply:

```
IDR at trade time = crypto_in_usd x historical_fx(trade_date)
```

No third-party historical crypto price feed is required.

## Quick start

```bash
pip install -r requirements.txt

# Create a key at https://localcoinswap.com/preferences/api-token
# (an authenticator code is required). Shown only once.
printf 'LCS_API_KEY=lcs_a1b2c3d4_your_secret\nLCS_USERNAME=your_username\n' > .env

python3 -m lcs_report
```

Serves on `http://0.0.0.0:7010` (override with `LCS_HOST` / `LCS_PORT`).
Port 7000 is skipped because macOS AirPlay Receiver holds it.

`.env` is read automatically from the project root, so there is nothing to
source and no need to export anything. Real environment variables take
precedence, so `LCS_PORT=8080 python3 -m lcs_report` still works. On startup the
server prints whether a key was found and how long it is -- never the key
itself:

```
INFO  LCS_API_KEY loaded (56 chars) - serving on 0.0.0.0:7010
INFO  No API key found. Add LCS_API_KEY to .env at the project root, ...
```

First load takes ~100s: it pages through every trade (the API caps `limit` at
100) and fetches the FX history in one request. Subsequent loads are ~0.2s.

Without a key the app serves a setup page instead of crashing.

## The three IDR numbers

Kept deliberately separate, because conflating them is the root of the problem.

| Column | Meaning |
|---|---|
| **Paid** | `fiat_amount`. Rupiah that actually changed hands, agreed at trade time. Already correct -- **no FX applied.** Realised cash flow. |
| **IDR @ trade** | `crypto_in_usd x fx(that date)`. Market value of the crypto *at that moment*. This is the fix. |
| **IDR @ today** | The same notional amount at today's rate. **What LCS reports**, and why old trades look wrong. |
| **Drift** | Gap between the two, in rupiah and percent. |
| **LCS overstatement** | Header card: `crypto_in_local_currency - IDR @ trade`, summed. On a real account this reached **+34.3M IDR (19.2%)** on the first 22 priced trades. |

`offer.price_formula_value x coin_amount` also equals `fiat_amount` (within
0.1% on ~88% of trades; the rest are users rounding to tidy rupiah figures).
That is the *executed* price including the counterparty's margin, so it is a
different concept again and is not used for valuation.

## Profit: what the number means

Three different figures, deliberately not conflated:

### Profit is measured in the trade's own quote currency

The quote currency is the leg the trade settled in -- `fiat_currency` on the
trade record. Buying BTC with IDR quotes in IDR; anything settled in USD quotes
in USD. Direction does not change it: buying BTC with IDR and selling BTC for
IDR both settle in IDR.

```
profit = coin_in_usd x fx(quote, that trade's date) x coin_amount - amount paid
```

For a **buy** that is what the coin was worth that day *minus* what you paid.
For a **sell** it is proceeds minus what the coin was worth. Both reduce to
`sign x (paid - value)`, because a buy turns fiat into the coin while a sell
turns the coin into fiat.

Three consequences, all enforced by tests:

- **Each trade uses its own rate.** `fx` is looked up per `(date, quote)` pair,
  so a 2019 IDR trade uses a 2019 rate and a 2026 one uses a 2026 rate. No
  single global rate is ever applied.
- **A USD-quoted trade needs no rate at all.** `crypto_in_usd` and a USD quote
  are already the same units, so the lookup short-circuits to 1.0. That is both
  cheaper and impossible to get wrong.
- **Currencies are never added together.** Every table, card and total is
  bucketed by quote currency. IDR profit and USD profit are not commensurable, so
  there is no blended total anywhere -- `summary.total_profit` does not exist and
  a test asserts it stays gone.

Money is quantised to the currency's own minor unit: IDR to whole rupiah, USD to
cents. Rounding a USD profit to a whole unit would erase it.

The quote currency comes from `fiat_currency` on `/trades/list/`, **not** from
`local_currency_symbol` on the detail endpoint -- those disagree, and the detail
field is an account display preference. See
[`docs/api-behaviour.md`](docs/api-behaviour.md) section 13.

### Figures deliberately not shown

- **Realised P&L.** Only 1 of 6 sells on the reference account ever settled, so
  it would be a single trade and misleading as a headline.
- **Mark-to-market P&L.** Was built, then removed. It required a live price
  feed, assumed you would exit at mid-market (you would not, on P2P), and
  ignored exit fees and spread. Calling it "real P&L" overstated it; there is no
  real P&L until you sell. `lcs_report/pricing.py` retains the CoinGecko /
  Bybit / Binance fallback chain and its tests, but is deliberately not wired
  in.

Sign convention: for a **buy**, profit is what the coin was worth *minus* what
you paid, so buying below market is positive. For a **sell** it is proceeds
minus market value.

Per-trade profit uses that trade's own historical rate. The live mark uses
CoinGecko's current price (`/simple/price`, free, no key) in IDR, falling back
to USD converted at the current rate. Chain is ignored when mapping a symbol to
a price: USDT-TRC20 and USDT-ERC20 are the same asset and must not be valued
differently. Coins with no available price are listed in a warning banner and
excluded, which makes the unrealised figure a lower bound whenever that list is
non-empty.

## Reports

- **Monthly report** -- one year at a time, switched by a year tab. A month with
  trades in more than one quote currency gets one row per currency rather than a
  blended row. Footer totals the visible year.
- **By quote currency** -- a table of every currency present, with the explicit
  note that the figures are never summed. Appears only when more than one is in
  play.
- **YTD** -- every month of the current year rolled up, shown beside the prior
  full year for comparison.

## Only settled trades count

374 of 1,874 trades on the reference account never executed (299 REJECTED, 62
EXPIRED, 12 cancelled, 1 disputed). Including them inflates volume by **33%**
(+2.48B IDR). Settled-only is therefore the default; uncheck
**Settled trades only** to inspect the rest.

A trade counts as settled when its status is `COMPLETED`, `CRYPTO_REL`, or a
dispute resolved to either side.

## Chain-aware coins

USDT exists on TRC20, ERC20 and BEP20 as genuinely different assets. They are
reported separately (`USDT-TRC20`, `USDT-ERC20`, `USDT-BEP20`) rather than
merged, because a merged "USDT" line hides real position split.

## How the FX rate is chosen

Rates are resolved per trade, in this order:

1. **Intraday** -- an hourly bar at or immediately before the trade, from Yahoo
   (`USDIDR=X`). Covers the last two years, about 846 of 1,502 settled trades.
   Marked with an `h` chip in the FX column.
2. **Daily** -- the ECB reference rate, for anything beyond the intraday window.
   Marked with the substituted date in amber when it differs from the trade date.

Both are cached permanently, so a figure computed last year does not change.

**The daily leg is also shifted by the publication lag.** The ECB publishes day
D's rate at about 16:00 CET (14:00 UTC). A trade at 09:00 WIB on day D happens
roughly twelve hours before that day's rate exists, so it uses day D-1's. Without
that shift the report would use a rate from the future, which is the same class
of error this project exists to fix.

The intraday feed is fetched once, in the background, never on a request path:
Yahoo takes about 30 seconds for a two-year window and rate-limits hard. Every
failure falls back to the daily rate, so the intraday layer is an improvement
and never a dependency. Set `LCS_INTRADAY=0` to disable it.

Coverage of the intraday window, by granularity:

| Granularity | Window | Your trades covered |
|---|---|---|
| 1-minute | 7 days | 16 (1.1%) |
| 5-minute | 60 days | 107 (7.1%) |
| **hourly** | **2 years** | **846 (56.3%)** |
| daily (ECB) | all history | 1,500 (99.9%) |

### Daily source

Source: [Frankfurter](https://www.frankfurter.app/) (ECB reference rates). Free,
no API key, full history back to 1999, one request per date range.

ECB publishes on **business days only**. Weekends and holidays are absent from
the response, so each lookup walks back to the most recent published prior date.
When that happens the substituted date is shown in amber beneath the rate, so a
weekend rate is never passed off as an exact-date rate.

Rates are cached in SQLite under `.cache/`, so a report stays reproducible: a
figure correct last month does not change because upstream revised data.

The whole trade-date span is fetched in a **single** request before valuation
begins, then all lookups are served from SQLite. Without that, a report over
1,900 trades spanning seven years degenerates into ~800 HTTP requests.

Cross-checked against Yahoo `USDIDR=X`; the two agree within 0.25%.

### Why not the alternatives

| Source | Verdict |
|---|---|
| exchangerate.host | Now requires a paid API key |
| CoinGecko (free) | Caps historical queries at 365 days -- breaks on exactly the old trades you care about |
| open.er-api.com | Current rate only, no history |
| Yahoo `USDIDR=X` | Works, but unofficial and needs a spoofed User-Agent. Cross-check only. |

## Two-step pricing

`crypto_in_usd` is **not** on `/trades/list/`. It only appears on
`/trades/{uuid}/`, one trade per request. For ~1,900 trades that is ~1,900
requests against a 60/minute ceiling -- roughly 35 minutes.

So the report is usable immediately from list data, and **Price all trades**
runs the detail fetch in the background, persisting to `.cache/trade_details.json`
as it goes. Progress is shown with an ETA; the page picks itself up when the run
completes. Interrupted runs resume from the cache.

Until a trade is priced, its market-rate columns are blank rather than wrong.

## API documentation, vendored

| Path | What it is |
|---|---|
| `docs/lcs-openapi.json` | The published spec (92 paths, 72 definitions). Regenerate with `docs/fetch-api-spec.sh`. |
| `docs/api-behaviour.md` | **Read this first.** What the live API actually does, where the spec is wrong, with evidence. |
| `tests/fixtures/*.json` | Redacted real payloads from `/trades/list/` and `/trades/{uuid}/`. |
| `tests/test_live_contract.py` | Asserts the documented behaviours still hold against those payloads. |

The spec alone would mislead: it is wrong about field casing, about which
endpoint carries `crypto_in_usd`, and about authentication. `api-behaviour.md`
records all of it. `test_live_contract.py` pins those findings, so if LCS ever
fixes one of them the suite fails and asks for a re-read rather than letting the
report silently drift.

## API quirks worth knowing

Found the hard way, all verified against the live API. Full detail in
[`docs/api-behaviour.md`](docs/api-behaviour.md):

1. **The Swagger spec documents camelCase; the API returns snake_case.**
   `cryptoInUsd` vs `crypto_in_usd`. A client generated from the spec alone
   silently yields null for every field. Both casings are accepted here.
2. **The spec over-declares authentication.** It marks all 100 operations as
   requiring `ApiKey` with no public overrides. Currency lookups, offer search,
   payment methods and leaderboards are genuinely public; only your own data
   401s.
3. **`next` links are unusable.** They are returned on `http://`, and Django's
   http-to-https 301 *drops the query string* -- so following one verbatim
   re-reads page 1 forever. Worse, after `?limit=100&offset=100` the server can
   return `?limit=5&offset=5`, rewinding to the default page size. Paging here
   is therefore driven by our own offset arithmetic and stops on a short page.
4. **`limit` silently caps at 100** regardless of what you ask for.
5. **The offer is `offer` on list responses but `ad` on detail responses.**
6. `info.version` is empty and LCS notes the API may change on short notice.
   Base path `/api/v2` is pinned.
7. `/trades/list/` has no date-range filter, so filtering happens client-side.

## Timezone caveat

A trade's calendar day picks its FX rate. A trade near midnight can land on a
different day depending on zone, selecting a different rate. `LCS_TRADE_TIMEZONE`
makes this explicit and defaults to `UTC`. For Indonesian trades set
`Asia/Jakarta` (UTC+7, no daylight saving).

## Endpoints

| Route | Purpose |
|---|---|
| `/` | HTML report with filters, pagination and client-side sorting |
| `/api/report` | Same data as JSON, same query params |
| `/api/enrichment` | Enrichment progress, for polling |
| `/enrich` | POST: start the background detail fetch |
| `/health` | Config and cache state. Never triggers a fetch. |
| `/refresh` | Drops the trade cache, then redirects |

## Staying current

A background thread inside the server process keeps the trade history
up to date. When viewing the report in a browser, the page keeps its freshness
ticker updated in real time via `/health` and automatically reloads without
losing your scroll position if new trades or status changes arrive.

**Steady state is one request.** `/trades/list/?limit=100&offset=0` returns the
newest trades first, so a single call reveals anything new. It also catches a
trade that entered live and has since settled, because that trade appears in the
window with its new status.

**The backoff ladder**, applied while nothing changes and reset the moment
something does:

| Consecutive quiet checks | Next wait |
|---|---|
| just changed | 60s |
| 1 quiet | 3 min (180s) |
| 2 quiet | 5 min (300s) |
| 3+ quiet | 15 min (900s) |

A flat 60-second poll for a year would be 525,600 requests; with the ladder an
untouched app costs a few dozen a week. Configurable via `LCS_POLL_LADDER`.

**The first check after a restart is always a full re-read**, not a window diff,
because the gap could be unbounded. Combined with persistence this gives:

| Scenario | Cost on startup |
|---|---|
| Restart seconds later | none -- the list is loaded from `.cache/trades.json` |
| Restart after a year offline | 19 requests, then it is current |
| New trades appear while running | 1 request per check, plus one per new trade to price it |

A newly arrived trade is also queued for per-trade USD pricing, so its profit
column is never left blank. The existing 1,873 priced trades stay cached, so a
long gap does not repeat that work.

**The thread lives in the server process.** If the server is stopped, nothing
refreshes -- the machine has to be awake and the process running. It is not a
daemon that survives a restart. `/health` reports `refresher.checks`,
`trades_age_seconds` and the next scheduled check; the header shows how long ago
the list was last confirmed.

## Navigation and responsiveness

Year tabs, filters and the pager are real links with real URLs, so a view is
shareable and the back button works. The cost is a full document load, which
discards scroll position -- clicking a year used to dump you at the top of a
very long page.

Scroll position is saved before an in-app link is followed and restored before
first paint, so no jump is visible. `history.scrollRestoration` is set to
`manual` so the browser does not fight this. A 2px bar appears at the top while
a navigation is in flight, because a cold load can take a while and a page that
looks frozen is indistinguishable from one that missed the click. The stored
position is cleared once used, so arriving later from outside still starts at the
top.

Responsive from 320px up. Wide tables sit inside a scroll container so their
width never propagates up and forces the whole page sideways.

## Verification tooling

Two scripts drive a real browser over the DevTools Protocol. Screenshots were
unreliable here; these read computed layout instead.

```bash
# Layout at 320 / 375 / 768 / 1024 / 1440: overflow, clipping, tap targets
python3 tools/measure_layout.py http://127.0.0.1:7010/

# Scroll position across a year-tab click
python3 tools/check_scroll_restoration.py http://127.0.0.1:7010/
python3 tools/check_scroll_restoration.py http://127.0.0.1:7010/ --no-js  # control
```

`--no-js` disables page scripts to confirm the check detects the regression
rather than passing vacuously. Measured:

```
document height: 2432px
scrolled to 1532px (history.scrollRestoration=manual)
clicked year tab: 2025
landed on year 2025, scrollY = 1532px
OK: scroll held within 0px of 1532px
```

With scripts disabled the same run reports `1532 -> 0`, which is the bug this
fixes. The layout probe already caught the real overflow when it existed: the
by-currency table was not inside a scroll container, giving 16 overflowing
elements and a 557px scroll width in a 375px viewport.

## Accessibility

WCAG 2.1 AA is enforced, not assumed. `tests/test_contrast.py` parses the actual
CSS custom properties and fails if any text pair drops below 4.5:1 (3:1 for the
focus ring), so a palette edit cannot silently regress contrast.

- Semantic table markup with `scope` and captions; sortable headers are real
  buttons reachable by keyboard and expose `aria-sort`
- Direction of value is carried by an arrow glyph **and** an explicit sign, so it
  never depends on colour alone
- Skip link, visible focus rings, `prefers-reduced-motion` honoured
- Dark and light themes both verified; `prefers-color-scheme` respected
- Responsive from 320px up; sticky table headers on long tables

## Configuration

See `.env.example`. All optional except `LCS_API_KEY`.

| Variable | Default | Notes |
|---|---|---|
| `LCS_API_KEY` | - | Required. Never logged. |
| `LCS_USERNAME` | auto | Distinguishes buy from sell. Auto-detected from `/referrals/overview/`. |
| `LCS_DISPLAY_CURRENCY` | `IDR` | Currency for the FX series. Every quote currency present is reported regardless. |
| `LCS_BASE_CURRENCY` | `USD` | Currency `crypto_in_usd` is quoted in. |
| `LCS_TRADE_TIMEZONE` | `UTC` | Which calendar day a timestamp maps to. |
| `LCS_HOST` / `LCS_PORT` | `0.0.0.0` / `7010` | |
| `LCS_INTRADAY` | `1` | Set `0` to use daily reference rates only. |
| `LCS_POLL_LADDER` | `60,180,300,900` | Seconds between background checks, walked in order while idle. |
| `LCS_REFRESH_WINDOW` | `100` | How many of the newest trades a cheap check inspects. |
| `LCS_PAGE_SIZE` | `200` | Live API caps at 100 regardless. |
| `LCS_MAX_TRADES` | `20000` | Safety cap. |
| `LCS_MIN_INTERVAL` | `1.1` | Seconds between requests; the API allows 60/minute. |
| `LCS_TIMEOUT` | `30` | |

## Development

```bash
python3 -m pytest tests/ -q
```

207 tests, fully offline: the LCS API is mocked and the FX store pre-seeded.

```bash
# Verify the FX layer against the real Frankfurter API
python3 - <<'PY'
from pathlib import Path
from datetime import date
from lcs_report.fx import FxStore, FrankfurterClient
s = FxStore(Path("/tmp/fx.db")); fx = FrankfurterClient(s)
print(fx.rate_for(date(2024, 6, 1)))   # Saturday -> snaps to Friday
PY
```

All monetary arithmetic uses `decimal.Decimal`; binary floats are never used for
amounts, including when parsing a float that arrived as JSON.

## Security

The API key is read from the environment and sent only in the `Authorization`
header. It is never logged and never rendered. `.env` is gitignored.

Note that `0.0.0.0` exposes this to your local network, and the report contains
your full trade history. Anyone who can reach the port can read it. Set
`LCS_HOST=127.0.0.1` if that is not wanted.

LCS keys can reach most of your account except security settings (password,
email, MFA, key management), and `/profile/encrypted-blob/` can return your
encrypted account key -- do not log response bodies from it.
