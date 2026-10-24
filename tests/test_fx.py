"""Tests for FX business-day snapping and the disk cache."""

from __future__ import annotations

import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lcs_report.fx import FrankfurterClient, FxError, FxStore  # noqa: E402


@pytest.fixture()
def store(tmp_path: Path) -> FxStore:
    return FxStore(tmp_path / "fx.db")


def test_store_roundtrip(store: FxStore) -> None:
    store.put_many("USD_IDR", {"2024-05-29": "16192", "2024-05-30": "16240"})
    assert store.get("USD_IDR", "2024-05-30") == Decimal("16240")
    assert store.get("USD_IDR", "2024-05-31") is None
    assert store.latest("USD_IDR") == ("2024-05-30", Decimal("16240"))
    assert store.coverage("USD_IDR") == ("2024-05-29", "2024-05-30")


def test_store_is_idempotent(store: FxStore) -> None:
    store.put_many("USD_IDR", {"2024-05-30": "16240"})
    store.put_many("USD_IDR", {"2024-05-30": "16299"})
    assert store.get("USD_IDR", "2024-05-30") == Decimal("16299")
    assert store.coverage("USD_IDR") == ("2024-05-30", "2024-05-30")


def test_series_are_isolated(store: FxStore) -> None:
    store.put_many("USD_IDR", {"2024-05-30": "16240"})
    store.put_many("EUR_IDR", {"2024-05-30": "17000"})
    assert store.get("USD_IDR", "2024-05-30") == Decimal("16240")
    assert store.get("EUR_IDR", "2024-05-30") == Decimal("17000")


def test_snap_back_over_weekend(store: FxStore) -> None:
    """A Saturday must resolve to Friday."""
    store.put_many("USD_IDR", {"2024-05-30": "16240", "2024-05-31": "16257"})
    effective, rate = store.snap_back("USD_IDR", date(2024, 6, 1))
    assert effective == date(2024, 5, 31)
    assert rate == Decimal("16257")


def test_snap_back_over_long_holiday(store: FxStore) -> None:
    store.put_many("USD_IDR", {"2024-05-24": "16100"})
    effective, rate = store.snap_back("USD_IDR", date(2024, 5, 27))
    assert effective == date(2024, 5, 24)
    assert rate == Decimal("16100")


def test_snap_back_raises_when_window_empty(store: FxStore) -> None:
    with pytest.raises(FxError, match="No USD_IDR rate found"):
        store.snap_back("USD_IDR", date(2024, 6, 1))


def test_rate_for_flags_snapping(store: FxStore, monkeypatch) -> None:
    store.put_many("USD_IDR", {"2024-05-31": "16257"})
    monkeypatch.setattr(FrankfurterClient, "_fetch_range", lambda self, *a, **k: 0)

    client = FrankfurterClient(store)
    eff, rate, snapped = client.rate_for(date(2024, 6, 1))
    assert (eff, rate, snapped) == (date(2024, 5, 31), Decimal("16257"), True)

    eff2, _, snapped2 = client.rate_for(date(2024, 5, 31))
    assert (eff2, snapped2) == (date(2024, 5, 31), False)


def test_warm_cache_avoids_network(store: FxStore, monkeypatch) -> None:
    def explode(*_a, **_k):
        raise AssertionError("network used despite a warm cache")

    monkeypatch.setattr(FrankfurterClient, "_fetch_range", explode)
    store.put_many(
        "USD_IDR", {f"2024-05-{d:02d}": "16000" for d in range(27, 32)}
    )
    FrankfurterClient(store).ensure_range(date(2024, 5, 27), date(2024, 5, 31))


def test_cold_cache_fetches(store: FxStore, monkeypatch) -> None:
    calls = []

    def fake(self, start, end, replace=True):
        calls.append((start, end))
        return self.store.put_many("USD_IDR", {"2024-05-30": "16240"})

    monkeypatch.setattr(FrankfurterClient, "_fetch_range", fake)
    FrankfurterClient(store).ensure_range(date(2024, 5, 27), date(2024, 5, 31))
    assert calls == [(date(2024, 5, 27), date(2024, 5, 31))]


def test_current_rate_uses_latest_cached(store: FxStore, monkeypatch) -> None:
    monkeypatch.setattr(FrankfurterClient, "_fetch_range", lambda self, *a, **k: 0)
    store.put_many("USD_IDR", {"2024-05-31": "16257"})
    day, rate = FrankfurterClient(store).current_rate()
    assert rate == Decimal("16257")
    assert day <= date.today()


def test_ensure_range_rejects_inverted_window(store: FxStore) -> None:
    with pytest.raises(ValueError, match="precedes"):
        FrankfurterClient(store).ensure_range(date(2024, 6, 1), date(2024, 5, 1))


def test_fetch_range_parses_and_stores(store: FxStore, monkeypatch) -> None:
    """The real Frankfurter payload shape."""
    payload = {
        "amount": 1.0,
        "base": "USD",
        "start_date": "2024-05-27",
        "end_date": "2024-06-04",
        "rates": {
            "2024-05-28": {"IDR": 16078},
            "2024-05-29": {"IDR": 16192},
            "2024-05-30": {"IDR": 16240},
        },
    }

    class FakeResp:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return payload

    class FakeClient:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return None

        def get(self, url, params=None):
            return FakeResp()

    monkeypatch.setattr("lcs_report.fx.httpx.Client", FakeClient)
    written = FrankfurterClient(store)._fetch_range(
        date(2024, 5, 28), date(2024, 6, 4), replace=True
    )
    assert written == 3
    assert store.get("USD_IDR", "2024-05-30") == Decimal("16240")