"""FastAPI app serving the IDR trading report."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .config import SETTINGS
from .service import ReportError, ReportService, TradeFilters, _year_to_date

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s"
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("lcs_report")

HERE = Path(__file__).resolve().parent
PAGE_SIZE = 10


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.getLogger("httpx").setLevel(logging.WARNING)
    service = ReportService(SETTINGS)
    app.state.service = service
    # Restore any persisted trade list so the first paint is instant, then let
    # the loop take over keeping it current.
    service.store.load()
    service.start_background()
    try:
        yield
    finally:
        service.close()


app = FastAPI(
    title="LCS IDR Trading Report",
    description=(
        "Values LocalCoinSwap trades in IDR at the exchange rate of the day each "
        "trade happened, rather than today's rate."
    ),
    lifespan=lifespan,
)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=str(HERE / "templates"))


# --------------------------------------------------------------------------
# formatting
# --------------------------------------------------------------------------


def _fmt_money(v: Decimal | None, currency: str = "IDR") -> str:
    """Format with the currency's own minor unit, so USD keeps its cents."""
    if v is None:
        return "-"
    from .valuation import ZERO_DECIMAL

    if (currency or "IDR").upper() in ZERO_DECIMAL:
        return f"{v:,.0f}"
    return f"{v:,.2f}"


def _fmt_idr(v: Decimal | None) -> str:
    return "-" if v is None else f"{v:,.0f}"


def _fmt_usd(v: Decimal | None) -> str:
    if v is None:
        return "-"
    if v >= 1000:
        return f"{v:,.0f}"
    return f"{v:,.4f}".rstrip("0").rstrip(".")


def _fmt_crypto(v: Decimal | None) -> str:
    if v is None:
        return "-"
    text = f"{v:,.8f}".rstrip("0").rstrip(".")
    return text or "0"


def _fmt_rate(v: Decimal | None) -> str:
    if v is None:
        return "-"
    if v >= 100:
        return f"{v:,.0f}"
    if v >= 10:
        return f"{v:,.2f}"
    return f"{v:,.4f}"


def _fmt_signed(v: Decimal | None) -> str:
    if v is None:
        return "-"
    return f"{'+' if v > 0 else ''}{v:,.0f}"


def _fmt_short(v: Decimal) -> str:
    """Compact notation for chart labels: 1.2B / 340M / 12k."""
    n = abs(v)
    if n >= 1_000_000_000:
        return f"{v / Decimal('1e9'):,.1f}B"
    if n >= 1_000_000:
        return f"{v / Decimal('1e6'):,.0f}M"
    if n >= 1_000:
        return f"{v / Decimal('1e3'):,.0f}k"
    return f"{v:,.0f}"


def _parse_date(value: str | None) -> date | None:
    if not value or not value.strip():
        return None
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(value.strip(), fmt).date()
        except ValueError:
            continue
    return None


def _service(request: Request) -> ReportService:
    return request.app.state.service


def _freshness(age: float | None) -> str:
    """How long ago the trade list was last confirmed against LCS."""
    if age is None:
        return "never"
    seconds = int(age)
    if seconds < 90:
        return f"{seconds}s ago"
    minutes = seconds // 60
    if minutes < 90:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 48:
        return f"{hours}h ago"
    return f"{hours // 24}d ago"


def _poll_label(data) -> str:
    """How long until the next check, phrased for a person."""
    if not data.refresher.state.running:
        return "automatic checks are off"
    due = data.refresher.next_due_in
    if due is None or due < 5:
        return "next check now"
    if due < 90:
        return f"next check in {int(due)}s"
    minutes = int(due) // 60
    if minutes < 90:
        return f"next check in {minutes}m"
    return f"next check in {round(minutes / 60)}h"


def _as_row(totals) -> dict:
    """Currency totals for the template.

    Money stays Decimal here so the template formats it; as_dict() exists for
    JSON, where strings are correct. Converting only some of the fields once
    produced a ValueError deep inside a format filter, so convert them all.
    """
    return {
        "currency": totals.currency,
        "trades": totals.trades,
        "cash_in": totals.cash_in,
        "cash_out": totals.cash_out,
        "cost": totals.cost,
        "market_value": totals.market_value,
        "profit": totals.profit,
        "net": totals.net,
        "margin_pct": totals.margin_pct,
    }


def _yearly(summary) -> list[dict]:
    """Volume by year, summed within each quote currency.

    Year bars only mix-safe when a year holds a single quote currency, so the
    series is built from the dominant currency and anything else is excluded
    rather than blended in.
    """
    buckets: dict[int, dict[str, Decimal]] = {}
    counts: dict[int, int] = {}
    for period in summary.monthly:
        year = int(period.key[:4])
        dom = period.dominant
        if dom is None:
            continue
        got = buckets.setdefault(year, {})
        got[dom.currency] = got.get(dom.currency, Decimal("0")) + dom.cash_out
        counts[year] = counts.get(year, 0) + period.trades
    out = []
    # Ascending, so the chart reads left-to-right in time and the most recent
    # year ends up at the far right.
    for year in sorted(buckets):
        cur, amount = max(buckets[year].items(), key=lambda kv: kv[1])
        out.append({"year": year, "currency": cur,
                    "volume": amount, "trades": counts.get(year, 0)})
    return out


def _primary(data) -> Any:
    """The currency the tables and headline figures are shown in.

    When no quote is selected this is the currency holding most deployed value,
    which keeps every table single-currency and unambiguous. The others are
    never summed with it; they are reported separately.
    """
    summary = data.summary
    if not summary.by_currency:
        return None
    if data.filters and data.filters.quote_currency:
        return summary.by_currency.get(data.filters.quote_currency.upper())
    if summary.display_totals is not None and summary.display_totals.trades > 0:
        return summary.display_totals
    return summary.dominant


# --------------------------------------------------------------------------
# routes
# --------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
def report_page(
    request: Request,
    date_from: str | None = None,
    date_to: str | None = None,
    coin: str = "",
    direction: str = "",
    status: str = "",
    search: str = "",
    settled_only: str = "",
    page: int = 1,
    monthly_year: str = "",
    quote: str = "",
    currency: str = "",
) -> Any:
    service = _service(request)

    # Default window is all history when no dates are given, so the report is
    # useful on first load without the user having to pick a range.
    filters = TradeFilters(
        date_from=_parse_date(date_from),
        date_to=_parse_date(date_to),
        coin=coin.strip(),
        direction=direction.strip().lower(),
        status=status.strip().upper(),
        search=search.strip(),
        quote_currency=quote.strip().upper(),
        settled_only=settled_only not in ("0", "false", "off"),
    )
    target_currency = (
        currency.strip().upper() if currency else SETTINGS.display_currency
    )

    try:
        data = service.build(filters, display_currency=target_currency)
    except ReportError as exc:
        return templates.TemplateResponse(
            request,
            "setup.html",
            {"error": str(exc), "has_key": SETTINGS.has_api_key},
            status_code=503 if not SETTINGS.has_api_key else 200,
        )

    page = max(1, page)
    page_count = max(1, -(-len(data.trades) // PAGE_SIZE))

    # The monthly report is paginated by year rather than by row: 60+ months in
    # one table is unusable, and years are the unit anyone actually reports in.
    monthly_years = sorted(
        {m.key[:4] for m in data.summary.monthly}, reverse=True
    )
    year = monthly_year.strip() or (monthly_years[0] if monthly_years else "")
    if year and year not in monthly_years:
        year = monthly_years[0] if monthly_years else ""
    monthly_rows = [m for m in data.summary.monthly if m.key[:4] == year]
    year_total = _year_to_date(data.summary, int(year)) if year else None
    page = min(page, page_count)
    start = (page - 1) * PAGE_SIZE
    window = data.trades[start : start + PAGE_SIZE]

    # Count what the settled-only filter is hiding, so the empty state can say so.
    excluded = data.total_available - data.summary.settled_count

    popular_currencies = ["EUR", "USD", "IDR", "GBP", "AUD", "CAD", "CHF", "JPY", "SGD"]
    display_currency_options = list(popular_currencies)
    for c in data.summary.currencies:
        if c not in display_currency_options:
            display_currency_options.append(c)
    if target_currency not in display_currency_options:
        display_currency_options.append(target_currency)

    query = {
        k: v
        for k, v in (
            ("date_from", date_from),
            ("date_to", date_to),
            ("coin", coin),
            ("direction", direction),
            ("status", status),
            ("search", search),
            ("quote", quote),
            ("currency", currency if currency and currency != SETTINGS.display_currency else ""),
            ("settled_only", "1" if filters.settled_only else "0"),
        )
        if v
    }
    query_string = urlencode(query)

    def page_url(n: int) -> str:
        q = dict(query, page=str(n))
        if year:
            q["monthly_year"] = year
        return "/?" + urlencode(q)

    def year_url(y: str) -> str:
        q = dict(query, monthly_year=y)
        q.pop("page", None)
        return "/?" + urlencode(q)

    return templates.TemplateResponse(
        request,
        "report.html",
        {
            "trades": window,
            "summary": data.summary,
            "total": data.total,
            "enriched": data.enriched,
            "total_available": data.total_available,
            "summary_settled": data.summary.settled_count,
            "excluded_count": excluded,
            "yearly": _yearly(data.summary),
            "quote_options": data.summary.currencies,
            "active_quote": filters.quote_currency or "",
            "totals_by_currency": [
                _as_row(data.summary.by_currency[c]) for c in data.summary.currencies
            ],
            "primary": _primary(data),
            "monthly": monthly_rows,
            "monthly_years": monthly_years,
            "monthly_year": year,
            "year_url": year_url,
            "year_total": year_total,
            "ytd": data.ytd,
            "prior_year": data.prior_year,
            "ytd_keys": {
                m.key for m in data.summary.monthly
                if data.ytd and m.key.startswith(data.ytd.key[:4])
            },
            # Coin filter options come from the full history, not the filtered
            # set, so selecting one coin does not empty the other options.
            "coin_options": data.summary.coins,
            "status_options": sorted({t.status for t in data.trades}),
            "username": data.username,
            "fx_rate": data.fx_rate,
            "fx_date": data.fx_date,
            "fx_coverage": data.fx_coverage,
            "freshness": _freshness(data.data_age),
            "poll_now": _poll_label(data),
            "warnings": data.warnings,
            "stale": data.stale,
            "filters": filters,
            "query_string": query_string,
            "page_url": page_url,
            "page": page,
            "page_count": page_count,
            "first_row": start + 1 if data.trades else 0,
            "last_row": start + len(window),
            "filtered_count": len(data.trades),
            "enrichment": service.enricher.progress.as_dict(),
            "base_currency": SETTINGS.base_currency,
            "display_currency": target_currency,
            "default_display_currency": SETTINGS.display_currency,
            "active_filters": {k: v for k, v in query.items() if k != "currency"},
            "display_currency_options": display_currency_options,
            "trade_timezone": str(SETTINGS.trade_timezone),
            "fmt_money": _fmt_money,
            "fmt_idr": _fmt_idr,
            "fmt_usd": _fmt_usd,
            "fmt_crypto": _fmt_crypto,
            "fmt_rate": _fmt_rate,
            "fmt_signed": _fmt_signed,
            "fmt_short": _fmt_short,
        },
    )


@app.get("/api/report")
def api_report(
    request: Request,
    date_from: str | None = None,
    date_to: str | None = None,
    coin: str = "",
    direction: str = "",
    status: str = "",
    search: str = "",
    quote: str = "",
    settled_only: str = "",
    currency: str = "",
) -> JSONResponse:
    service = _service(request)
    filters = TradeFilters(
        date_from=_parse_date(date_from),
        date_to=_parse_date(date_to),
        coin=coin.strip(),
        direction=direction.strip().lower(),
        status=status.strip().upper(),
        search=search.strip(),
        quote_currency=quote.strip().upper(),
        settled_only=settled_only not in ("0", "false", "off"),
    )
    target_currency = (
        currency.strip().upper() if currency else SETTINGS.display_currency
    )
    try:
        data = service.build(filters, display_currency=target_currency)
    except ReportError as exc:
        return JSONResponse({"error": str(exc)}, status_code=503)

    primary = _primary(data)
    return JSONResponse(
        {
            "base_currency": SETTINGS.base_currency,
            "display_currency": target_currency,
            "display_totals": (
                data.summary.display_totals.as_dict()
                if data.summary.display_totals
                else None
            ),
            "active_quote": filters.quote_currency,
            "quote_options": data.summary.currencies,
            "primary": primary.as_dict() if primary else None,
            "by_currency": {
                c: data.summary.by_currency[c].as_dict()
                for c in data.summary.currencies
            },
            "trades": [
                {
                    "uuid": t.uuid,
                    "date": t.trade_date.isoformat() if t.trade_date else None,
                    "coin": t.coin_label,
                    "amount": str(t.coin_amount) if t.coin_amount is not None else None,
                    "direction": t.direction,
                    "status": t.status,
                    "settled": t.is_settled,
                    "quote_currency": t.quote_currency,
                    "coin_in_usd": (
                        str(t.coin_in_usd) if t.coin_in_usd is not None else None
                    ),
                    "fx_rate": str(t.fx_rate) if t.fx_rate is not None else None,
                    "fx_date": t.fx_date.isoformat() if t.fx_date else None,
                    "fx_snapped": t.fx_snapped,
                    "fx_source": t.fx_source,
                    "market_value_quote": (
                        str(t.market_value_quote)
                        if t.market_value_quote is not None
                        else None
                    ),
                    "paid": str(t.fiat_amount) if t.fiat_amount is not None else None,
                    "fee_amount": (
                        str(t.fee_amount) if t.fee_amount is not None else None
                    ),
                    "fee_ratio": (
                        str(t.fee_ratio) if t.fee_ratio is not None else None
                    ),
                    "fee_value": (
                        str(t.fee_value_quote)
                        if t.fee_value_quote is not None
                        else None
                    ),
                    "net_value": (
                        str(t.net_value_quote)
                        if t.net_value_quote is not None
                        else None
                    ),
                    "profit": str(t.profit) if t.profit is not None else None,
                    "lcs_local_value_idr": (
                        str(t.lcs_local_value_idr)
                        if t.lcs_local_value_idr is not None
                        else None
                    ),
                    "lcs_drift_idr": (
                        str(t.lcs_drift_idr)
                        if t.lcs_drift_idr is not None
                        else None
                    ),
                    "notes": t.notes,
                }
                for t in data.trades
            ],
            "monthly": [m.as_dict() for m in data.summary.monthly],
            "ytd": data.ytd.as_dict() if data.ytd else None,
            "prior_year": data.prior_year.as_dict() if data.prior_year else None,
            "fx": {
                "current_rate": str(data.fx_rate) if data.fx_rate is not None else None,
                "current_date": data.fx_date.isoformat() if data.fx_date else None,
                "coverage": list(data.fx_coverage),
            },
            "meta": {
                "total_available": data.total_available,
            "summary_settled": data.summary.settled_count,
                "enriched": data.enriched,
                "total": data.total,
                "username": data.username,
                "warnings": data.warnings,
                "stale": data.stale,
            },
        }
    )


@app.post("/enrich")
def start_enrichment(request: Request) -> JSONResponse:
    """Queue the background per-trade detail fetch."""
    service = _service(request)
    if not SETTINGS.has_api_key:
        return JSONResponse({"error": "LCS_API_KEY is not set"}, status_code=503)
    try:
        started = service.start_enrichment()
    except ReportError as exc:
        return JSONResponse({"error": str(exc)}, status_code=503)
    return JSONResponse({"started": started, **service.enricher.progress.as_dict()})


@app.get("/api/enrichment")
def enrichment_status(request: Request) -> JSONResponse:
    return JSONResponse(_service(request).enricher.progress.as_dict())


@app.get("/refresh")
def refresh(request: Request):
    """Force an immediate full re-read rather than waiting for the loop."""
    service = _service(request)
    result = service.refresher.run_once(full=True)
    target = "/api/enrichment" if result.changed else "/"
    return HTMLResponse(
        f'<meta http-equiv="refresh" content="0;url={target}">Synced.</meta>'
    )


@app.get("/health")
def health(request: Request) -> JSONResponse:
    service = _service(request)
    display = SETTINGS.display_currency
    coverage = service.fx.coverage(display)
    return JSONResponse(
        {
            "status": "ok",
            "api_key_configured": SETTINGS.has_api_key,
            "username": service._username,
            "fx_series": service.fx.series(display),
            "fx_series_pairs": [
                service.fx.series(c) for c in service.fx._clients
            ],
            "fx_coverage": {"from": coverage[0], "to": coverage[1]},
            "trades_cached": len(service.store),
            "trades_age_seconds": service.data_age(),
            "refresher": service.refresher.state.as_dict(),
            "trade_details_cached": service.enricher.cached_count,
            "enrichment": service.enricher.progress.as_dict(),
            "trade_timezone": str(SETTINGS.trade_timezone),
            "display_currency": SETTINGS.display_currency,
        }
    )