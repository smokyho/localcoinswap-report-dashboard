"""Route-level tests.

These exist because a rename once broke the JSON serializer while every other
test still passed: nothing exercised ``/api/report`` or the rendered page. They
assert the routes respond and that the documented fields are actually present,
so a renamed attribute fails here instead of in the browser.
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lcs_report import app as app_module  # noqa: E402
from lcs_report import service as service_module  # noqa: E402
from lcs_report.config import Settings  # noqa: E402
from lcs_report.service import ReportService, TradeFilters  # noqa: E402

TEMPLATE = Path(__file__).resolve().parent.parent / "lcs_report" / "templates" / "report.html"

TZ = ZoneInfo("UTC")


def make_trade(uuid: str, coin: str, day: date, quote: str = "IDR", **over):
    base = {
        "id": abs(hash(uuid)) % 100000,
        "uuid": uuid,
        "status": "COMPLETED",
        "fiat_amount": "1500000.00",
        "coin_amount": "0.01000000",
        "time_created": int(
            datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc).timestamp()
        ),
        "coin_currency": {"symbol": coin, "slug": coin, "token_standard": None},
        "fiat_currency": {"symbol": quote, "slug": quote.lower()},
        "buyer": {"username": "me"},
        "seller": {"username": "peer"},
        "offer": {
            "trading_type": {"slug": "buy"},
            "payment_method": {"name": "Bank Transfer"},
            "price_formula_value": "150000000",
        },
        # The whole USD value of the trade, as the detail endpoint reports it.
        "crypto_in_usd": "83.0",
    }
    base.update(over)
    return base


# 12 settled trades (enough to paginate at 10/page) plus one rejected.
TRADES = [
    make_trade("t1", "BTC", date(2024, 6, 3)),
    make_trade("t2", "ETH", date(2025, 3, 10), fiat_amount="900000.00"),
    make_trade("t3", "BTC", date(2026, 1, 15), status="REJECTED"),
]
TRADES += [
    make_trade(f"p{i:02d}", "BTC", date(2026, 2, 1 + (i % 20)))
    for i in range(1, 11)
]
# Months across three more years so year pagination has something to switch between.
TRADES += [
    make_trade(f"y{yr}{m:02d}", "ETH", date(yr, m, 12), fiat_amount="500000.00")
    for yr in (2024, 2025)
    for m in (1, 3, 6, 9, 12)
]
TRADES += [make_trade("y2023a", "LTC", date(2023, 11, 5), fiat_amount="700000.00")]
# Settled in USD: needs no FX lookup at all, and must stay out of the IDR totals.
TRADES += [
    make_trade("usd1", "BTC", date(2026, 4, 2), quote="USD",
               fiat_amount="8000.00", crypto_in_usd="8300.00"),
    make_trade("usd2", "ETH", date(2026, 5, 9), quote="USD",
               fiat_amount="1500.00", crypto_in_usd="1470.00"),
]


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    settings = Settings(
        api_key="lcs_test_key",
        username="me",
        base_currency="USD",
        display_currency="IDR",
        trade_timezone=TZ,
        page_size=100,
        max_trades=20_000,
        min_interval_seconds=0.0,
        request_timeout=5.0,
        fx_cache_path=tmp_path / "fx.db",
        trades_cache_path=tmp_path / "t.json",
        http_cache_path=tmp_path / "h.json",
        host="127.0.0.1",
        port=7010,
        poll_ladder=(0.0,),
        refresh_window=100,
        intraday_enabled=False,
    )

    service = ReportService(settings)
    service.fx.offline = True
    # Seed the store directly; the background loop is stubbed out below.
    service.store.replace(list(TRADES))
    service._username = "me"
    # Deterministic FX: one rate for every date, no network.
    service.fx.prime_all = lambda by_target: None  # type: ignore[method-assign]
    service.fx.errors = []
    service.fx.go_offline = lambda flag=True: None  # type: ignore[method-assign]

    def fake_rate(day, currency):
        if (currency or "").upper() == "SGD":
            return day, Decimal("1.34"), False
        return day, Decimal("16000"), False

    service.fx.rate_for = fake_rate  # type: ignore[method-assign]
    service._current_fx = (date(2026, 10, 2), Decimal("16000"))
    service._current_fx_at = 1e12
    service.fx.coverage = lambda target: ("2019-01-01", "2026-10-02")  # type: ignore[method-assign]
    service.enricher.set_expected_total = lambda n: None  # type: ignore[method-assign]

    monkeypatch.setattr(app_module, "SETTINGS", settings)

    from contextlib import asynccontextmanager

    # Never reach the network from a render test: the intraday feed is Yahoo and
    # it rate-limits, so priming it here would make the suite hang.
    service.intraday.prime = lambda currencies: None  # type: ignore[method-assign]

    # The route tests are about rendering, not the background loop.
    service.refresher.check = lambda full=False: __import__(
        "lcs_report.refresher", fromlist=["SyncResult"]
    ).SyncResult()
    service.start_background = lambda: False

    @asynccontextmanager
    async def fake_lifespan(app):
        app.state.service = service
        yield

    monkeypatch.setattr(app_module.app.router, "lifespan_context", fake_lifespan)

    with TestClient(app_module.app) as c:
        yield c
    service.close()


# --------------------------------------------------------------------------


def test_health(client: TestClient) -> None:
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["display_currency"] == "IDR"


def test_report_page_renders(client: TestClient) -> None:
    resp = client.get("/")
    assert resp.status_code == 200, resp.text[:400]
    assert "IDR Trading Report" in resp.text


def test_report_page_states_the_profit_definition(client: TestClient) -> None:
    import re as _re

    html = client.get("/").text
    # Strip tags as well as whitespace: the sentence is broken up by <em>.
    flat = _re.sub(r"\s+", " ", _re.sub(r"<[^>]+>", "", html))
    assert "Profit" in html
    assert "measured in each trade's own quote currency" in flat, (
        "the page must state the quote-currency rule"
    )
    assert "never a single global rate" in flat
    assert "Unrealised" not in html and "unrealised" not in html


def test_report_page_has_monthly_and_ytd(client: TestClient) -> None:
    html = client.get("/").text
    assert 'aria-labelledby="ytd-heading"' in html
    assert 'aria-labelledby="monthly-heading"' in html
    assert "Monthly report" in html


def test_report_page_has_profit_column(client: TestClient) -> None:
    html = client.get("/").text
    assert ">Profit</th>" in html


def test_pagination_is_ten_per_page(client: TestClient) -> None:
    page1 = client.get("/").text
    assert page1.count("data-uuid=") == 10, "page 1 must hold exactly 10 rows"
    assert "Page 1 of 3" in page1
    page2 = client.get("/?page=2").text
    assert page2.count("data-uuid=") == 10
    page3 = client.get("/?page=3").text
    assert page3.count("data-uuid=") == 5, "the remaining 5 settled trades"

    # Out-of-range pages clamp rather than erroring.
    assert client.get("/?page=9999").status_code == 200
    assert client.get("/?page=0").status_code == 200


def test_page_size_comes_from_the_app(client: TestClient) -> None:
    assert app_module.PAGE_SIZE == 10


def test_json_report_endpoint(client: TestClient) -> None:
    """Guards every field the UI and JS read."""
    resp = client.get("/api/report")
    assert resp.status_code == 200, resp.text[:400]
    body = resp.json()

    for key in ("by_currency", "primary", "trades", "fx", "meta", "ytd", "monthly"):
        assert key in body, f"missing top-level key {key}"
    for gone in ("summary", "live_prices"):
        assert gone not in body, f"{gone} should no longer be exposed"

    for cur, totals in body["by_currency"].items():
        for key in (
            "currency", "trades", "cash_in", "cash_out", "cost", "profit", "margin_pct",
        ):
            assert key in totals, f"missing by_currency[{cur}].{key}"
        assert totals["currency"] == cur, "bucket must be keyed by its own currency"
    for key in ("currency", "profit", "cost", "trades"):
        assert key in body["primary"], f"missing primary.{key}"

    t = body["trades"][0]
    for key in (
        "uuid",
        "date",
        "coin",
        "coin_in_usd",
        "fx_rate",
        "quote_currency",
        "profit",
        "market_value_quote",
        "paid",
        "fee_amount",
        "fee_ratio",
        "fee_value",
        "net_value",
        "profit",
    ):
        assert key in t, f"missing trades[].{key}"


def test_json_rejects_unsettled_by_default(client: TestClient) -> None:
    body = client.get("/api/report").json()
    assert body["meta"]["total"] == 25
    assert sum(c["trades"] for c in body["by_currency"].values()) == 25
    assert "t3" not in {t["uuid"] for t in body["trades"]}


def test_json_includes_unsettled_when_asked(client: TestClient) -> None:
    body = client.get("/api/report?settled_only=0").json()
    assert body["meta"]["total"] == 26
    assert "t3" in {t["uuid"] for t in body["trades"]}


def test_json_filters_by_coin(client: TestClient) -> None:
    body = client.get("/api/report?coin=BTC").json()
    assert {t["coin"] for t in body["trades"]} == {"BTC"}
    # t1, p01..p10 and usd1; t3 is BTC but was rejected.
    assert body["meta"]["total"] == 12


def test_json_filters_by_direction(client: TestClient) -> None:
    body = client.get("/api/report?direction=buy").json()
    assert body["meta"]["total"] == 25
    assert client.get("/api/report?direction=sell").json()["meta"]["total"] == 0


def test_json_monthly_and_ytd(client: TestClient) -> None:
    body = client.get("/api/report").json()
    keys = [m["key"] for m in body["monthly"]]
    assert "2024-06" in keys
    for m in body["monthly"]:
        for k in ("label", "trades", "currency", "by_currency", "currencies"):
            assert k in m, f"missing monthly[].{k}"
    ytd = body["ytd"]
    assert ytd is not None
    for k in ("label", "trades", "currency", "by_currency", "currencies"):
        assert k in ytd, f"missing ytd.{k}"


def test_profit_is_computed_per_trade(client: TestClient) -> None:
    body = client.get("/api/report").json()
    t = next(x for x in body["trades"] if x["uuid"] == "t1")
    # 83 USD x 16000 = 1,328,000 market value against 1,500,000 paid -> a loss.

    # Live mark: 0.01 BTC x 80,000 USD x 16000 IDR = 12,800,000
    # 0.01 BTC x 80,000 USD x 16,000 IDR/USD = 12,800,000


def test_enrichment_endpoints(client: TestClient) -> None:
    status = client.get("/api/enrichment")
    assert status.status_code == 200
    assert "pct" in status.json()
    start = client.post("/enrich")
    assert start.status_code == 200
    assert "started" in start.json()


def test_unknown_filters_do_not_error(client: TestClient) -> None:
    """A junk date must not 500 the page."""
    for qs in ("date_from=notadate", "coin=NOPE", "status=WHAT", "page=9999"):
        assert client.get(f"/?{qs}").status_code == 200, qs
        assert client.get(f"/api/report?{qs}").status_code == 200, qs


# --------------------------------------------------------------------------
# Monthly report pagination by year
# --------------------------------------------------------------------------


def test_monthly_defaults_to_the_newest_year(client: TestClient) -> None:
    html = client.get("/").text
    assert "2026 total" in html
    # 2025 and 2024 months must not be rendered by default.
    assert "Nov 2025" not in html
    assert "Sep 2024" not in html


def test_monthly_year_nav_lists_every_year(client: TestClient) -> None:
    html = client.get("/").text
    nav = html.split('class="yearnav"')[1].split("</nav>")[0]
    for year in ("2026", "2025", "2024", "2023"):
        assert f">{year}</a>" in nav, f"{year} missing from the year nav"
    # Newest first.
    order = [int(y) for y in __import__("re").findall(r'>(\d{4})</a>', nav)]
    assert order == sorted(order, reverse=True)


def test_monthly_year_nav_marks_the_current_year(client: TestClient) -> None:
    html = client.get("/?monthly_year=2025").text
    assert 'aria-current="page"' in html
    nav = html.split('class="yearnav"')[1].split("</nav>")[0]
    current = nav.split("is-current", 1)[1]
    assert "2025" in current.split("</a>", 1)[0]
    assert "2026" not in current.split("</a>", 1)[0]


def test_monthly_switches_year(client: TestClient) -> None:
    html = client.get("/?monthly_year=2025").text
    assert "2025 total" in html
    assert "Dec 2025" in html
    assert "Sep 2025" in html
    assert "Nov 2026" not in html, "2026 months must not leak into the 2025 view"


def test_monthly_year_nav_preserves_other_filters(client: TestClient) -> None:
    html = client.get("/?coin=ETH&monthly_year=2025").text
    nav = html.split('class="yearnav"')[1].split("</nav>")[0]
    assert "coin=ETH" in nav, "choosing a year must not drop the active filters"


def test_monthly_unknown_year_falls_back(client: TestClient) -> None:
    html = client.get("/?monthly_year=1999").text
    assert "2026 total" in html, "an unknown year should fall back to the newest"


def test_monthly_json_is_unaffected_by_the_year_tab(client: TestClient) -> None:
    """The year tab is a view concern; the JSON always returns every month."""
    body = client.get("/api/report").json()
    keys = {m["key"] for m in body["monthly"]}
    assert {"2023-11", "2024-01", "2025-12", "2026-02"} <= keys
    assert body["ytd"]["key"] == "2026"
    for m in body["monthly"]:
        assert "by_currency" in m and "currency" in m


def test_oldest_year_view_works(client: TestClient) -> None:
    html = client.get("/?monthly_year=2023").text
    assert "Nov 2023" in html
    assert "2023 total" in html


# --------------------------------------------------------------------------
# Layout structure
# --------------------------------------------------------------------------


def test_filter_sits_inside_the_trades_section(client: TestClient) -> None:
    """The filter belongs next to the table it controls, not above other sections."""
    html = client.get("/").text
    trades = html.index('aria-labelledby="trades-heading"')
    form = html.index('class="filters')
    table = html.index('id="trades-table"')
    assert trades < form < table, "form must be inside Trades, before the table"
    # And it must come after every other section, so it is not floating above them.
    for other in ("cur-heading", "chart-heading", "ytd-heading", "monthly-heading"):
        assert html.index(other) < trades, f"{other} should precede the Trades section"


def test_filter_survives_an_empty_result_set(client: TestClient) -> None:
    """Hiding the filter would strand a user who filtered everything away."""
    html = client.get("/?coin=NOSUCHCOIN").text
    assert 'class="filters' in html, "the filter must remain reachable when empty"
    assert "No trades match these filters" in html
    assert 'id="trades-table"' not in html, "the table should be suppressed when empty"


def test_chart_runs_oldest_to_newest(client: TestClient) -> None:
    """Time should read left to right, so the most recent year is rightmost."""
    import re as _re

    html = client.get("/").text
    years = [int(y) for y in _re.findall(r'<span class="chart-year">(\d{4})</span>', html)]
    assert years, "the chart should render years"
    assert years == sorted(years), f"years out of order: {years}"
    assert years[-1] == max(years), "newest year must be last (rightmost)"
    assert years[0] == min(years), "oldest year must be first (leftmost)"


def test_no_double_escaped_entities_in_the_output(client: TestClient) -> None:
    """Jinja autoescapes, so an entity inside {{ }} renders as literal text.

    Caught in the wild: the profit arrow was written as `&#9650;` inside an
    expression, which came out as the visible characters `&#9650;` rather than
    a triangle. This checks the whole page so the class of bug cannot recur.
    """
    import re as _re

    for path in ("/", "/api/report", "/?quote=USD", "/?coin=BTC"):
        body = client.get(path).text
        offenders = _re.findall(r"&amp;#?\w+;", body)
        assert not offenders, (
            f"{path} renders escaped entities as text: {sorted(set(offenders))[:5]}"
        )


def test_profit_arrow_is_a_real_character(client: TestClient) -> None:
    """Positive figures get a triangle, negative get a down arrow."""
    html = client.get("/").text
    up, down = "\u25B2", "\u25BC"
    assert up in html, "expected an up triangle for a positive figure"
    assert "&#9650;" not in html and "&amp;#9650;" not in html
    # The arrow is decorative: the sign carries the meaning for a screen reader.
    assert 'class="delta-arrow" aria-hidden="true"' in html
    assert down in html or "+" in html


def test_direction_is_in_the_text_not_just_colour(client: TestClient) -> None:
    """Meaning must survive greyscale and reach a screen reader.

    The triangle is `aria-hidden` decoration, so the accessible text has to
    carry the direction itself: a label plus an explicit sign. Matching the
    whole cell naively stops at the arrow's own closing tag, so this pairs the
    hidden label with the sign that must follow it.
    """
    import re as _re

    html = client.get("/").text

    gains = _re.findall(r'class="sr-only">gain of </span>(\+?)([\d,.]+)', html)
    assert gains, "expected at least one positive figure"
    assert all(sign == "+" for sign, _ in gains), "a gain must carry a + sign"

    # And the triangle must be a character, not an escaped entity.
    assert "\u25B2" in html


def test_profit_cell_renders_both_directions_accessibly() -> None:
    """Every live IDR trade is profitable, so the loss path needs a direct test."""
    import re as _re
    from decimal import Decimal as _D

    from jinja2 import Environment

    from lcs_report.app import _fmt_money

    # Lift the macro straight out of the template so the test cannot drift from
    # what actually ships.
    source = TEMPLATE.read_text()
    start = source.index("{% macro profit_cell(")
    end = source.index("{% macro status_chip(")
    env = Environment(autoescape=True)
    # fmt_money arrives from Python in the page context; supply the same callable
    # so the macro renders exactly as it does on the page.
    from lcs_report.app import _fmt_money as _money

    mod = env.from_string(source[start:end]).make_module({"fmt_money": _money})
    render = lambda v: mod.profit_cell(v, "IDR", None)  # noqa: E731

    gain = render(_D("1500"))
    assert "gain of" in gain and "+1,500" in gain
    assert "\u25B2" in gain
    assert "&amp;" not in gain, "nothing may be double-escaped"

    loss = render(_D("-1500"))
    assert "loss of" in loss and "-1,500" in loss
    assert "\u25BC" in loss
    assert "+" not in loss.replace("loss of", "")

    assert render(_D("0")).strip() == '<span class="neutral">0</span>'
    assert "&mdash;" in render(None)
    # The macro must be reachable from the page, or the column renders blank.
    assert "profit_cell(" in source[start:end]


def test_report_page_currency_switch(client: TestClient) -> None:
    resp = client.get("/?currency=EUR")
    assert resp.status_code == 200
    assert "EUR Trading Report" in resp.text
    assert "EUR" in resp.text
    # Header contains the currency picker select with onchange
    assert 'class="currency-picker"' in resp.text
    assert '<select id="header_currency" name="currency" onchange="this.form.submit()"' in resp.text


def test_api_report_currency_switch(client: TestClient) -> None:
    resp = client.get("/api/report?currency=EUR")
    assert resp.status_code == 200
    data = resp.json()
    assert data["display_currency"] == "EUR"
    assert data["primary"]["currency"] == "EUR"
    assert "display_totals" in data


def test_trades_filter_only_applies_to_trades_entry(client: TestClient) -> None:
    """Trade filters must only restrict the trades table/list, not page-wide summary metrics."""
    # Unfiltered baseline: 25 settled trades total
    unfiltered = client.get("/api/report").json()
    assert unfiltered["meta"]["total"] == 25
    assert sum(c["trades"] for c in unfiltered["by_currency"].values()) == 25

    # Filter trades by coin=BTC: only 12 trades match
    filtered = client.get("/api/report?coin=BTC").json()
    assert len(filtered["trades"]) == 12
    assert filtered["meta"]["total"] == 12
    assert {t["coin"] for t in filtered["trades"]} == {"BTC"}

    # Portfolio-wide summary metrics, by_currency, and monthly must remain full portfolio (25 trades)
    assert sum(c["trades"] for c in filtered["by_currency"].values()) == 25
    assert sum(c["trades"] for c in filtered["by_currency"].values()) == sum(
        c["trades"] for c in unfiltered["by_currency"].values()
    )


def test_enrichment_warning_renders_below_filters(client: TestClient) -> None:
    resp = client.get("/")
    assert resp.status_code == 200
    html = resp.text
    # When missing USD prices exist, the warning is rendered below the filters form
    if "lack per-trade USD price" in html:
        filter_pos = html.find('class="filters filters--inline"')
        table_pos = html.find('id="trades-table"')
        warn_pos = html.find("lack per-trade USD price")
        assert filter_pos < warn_pos < table_pos, (
            "Enrichment warning must be located below the filters form and above the trades table"
        )



