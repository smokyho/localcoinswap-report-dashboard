# LCS Trading Report

A trading report and valuation dashboard for LocalCoinSwap (LCS) P2P trades. Values historical trades in any target fiat currency using historical exchange rates (from ECB / Frankfurter) pegged to the trade date, rather than today's exchange rate.

## Tech Stack
- **Runtime**: Python 3.12+
- **Web & Templates**: FastAPI, Uvicorn, Jinja2, vanilla JS / CSS
- **HTTP & FX**: HTTPX, Frankfurter API (ECB reference rates)
- **Testing**: pytest

## Commands
- Run application: `python3 -m lcs_report` (default: `http://0.0.0.0:7010`)
- Run all tests: `pytest`
- Run specific test file: `pytest tests/test_valuation.py`
- Run with verbose output: `pytest -v`

## Core Architecture & Design Invariants

1. **Currency-Agnostic Engine**:
   - The system is designed to support arbitrary fiat currencies, not just IDR. IDR is simply the default offered currency (`LCS_DISPLAY_CURRENCY="IDR"`, `LCS_BASE_CURRENCY="USD"`).
   - FX caching (`FxStore` SQLite) indexes rates by `(series, day)` where `series = f"{base}_{display}"`.
   - Never hardcode currency assumptions in domain/valuation logic.

2. **Historical Valuation Invariant**:
   - The LCS API's `crypto_in_local_currency` field dynamically converts all historical trades using *today's* exchange rate, rendering it invalid for historical accounting.
   - **Always calculate**: `crypto_in_usd * historical_fx(trade_date)` for the display currency.
   - For non-trading days (weekends, ECB holidays), FX rates snap back to the most recent preceding business day (`MAX_SNAP_DAYS = 10`).

3. **Decoupled Architecture**:
   - `lcs_report/app.py`: FastAPI routes, template rendering, and JSON endpoints.
   - `lcs_report/service.py`: Orchestration layer (background polling ladder, trade store synchronization, filtering).
   - `lcs_report/valuation.py`: Pure domain logic for trade normalization, status categorization, filtering, and summary metrics.
   - `lcs_report/fx.py`: Historical FX fetching, SQLite cache (`fx_rates`), and calendar date resolution.
   - Keep valuation and domain calculations free of FastAPI dependencies so tests run quickly without HTTP servers.

4. **Precision & Types**:
   - Always use `Decimal` for financial math and valuations; avoid floating point arithmetic for currency calculations.
   - Code conventions: `from __future__ import annotations`, dataclasses, strict type hinting.

5. **Testing & Offline Boundaries**:
   - Unit tests in `tests/` must remain completely offline (using fixtures or mocked HTTP responses).
   - Avoid making outbound requests to LCS or Frankfurter during test runs.

6. **Environment & Networking**:
   - Port 7000 is held by macOS AirPlay Receiver; the app defaults to port 7010 (`LCS_PORT`).
   - `.env` is loaded automatically by `lcs_report/config.py`. Never commit `.env` or log API keys.
