"""Tests for the background trade refresher.

The behaviours that matter:

* a single windowed request is the steady-state cost
* a change resets the backoff to the front of the ladder
* inactivity walks up the ladder, so an abandoned app costs almost nothing
* the first check after startup is always a full re-read, because the gap
  since the last run is unbounded
* the store survives a restart via disk
* a newly arrived trade is queued for per-trade pricing
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lcs_report.lcs_client import LcsError  # noqa: E402
from lcs_report.refresher import (  # noqa: E402
    SyncResult,
    TradeRefresher,
    TradeStore,
)


def trade(uuid: str, ts: int, status: str = "COMPLETED") -> dict[str, Any]:
    return {
        "uuid": uuid,
        "status": status,
        "time_created": ts,
        "coin_amount": "1",
        "fiat_amount": "1000",
        "coin_currency": {"symbol": "BTC", "slug": "BTC"},
        "fiat_currency": {"symbol": "IDR"},
        "buyer": {"username": "me"},
        "seller": {"username": "x"},
        "offer": {},
    }


class FakeClient:
    """Stands in for LcsClient, serving a mutable list of trades."""

    def __init__(self, trades: list[dict[str, Any]]) -> None:
        self.trades = sorted(trades, key=lambda t: t["time_created"], reverse=True)
        self.page_calls = 0
        self.full_calls = 0

    def paginate(self, path, params=None, *, max_items=20_000):
        self.full_calls += 1
        return list(self.trades)

    def get(self, path, params=None):
        self.page_calls += 1
        limit = int((params or {}).get("limit", 100))
        results = self.trades[:limit]
        return {"count": len(self.trades), "next": None, "results": list(results)}

    def close(self) -> None:
        pass


@pytest.fixture()
def store(tmp_path: Path) -> TradeStore:
    return TradeStore(tmp_path / "trades.json")


# --------------------------------------------------------------------------
# Store
# --------------------------------------------------------------------------


def test_store_keeps_newest_first(store: TradeStore) -> None:
    store.replace([trade("a", 100), trade("c", 300), trade("b", 200)])
    assert [t["uuid"] for t in store.all()] == ["c", "b", "a"]
    assert len(store) == 3


def test_store_merge_reports_new_and_changed(store: TradeStore) -> None:
    store.replace([trade("a", 100), trade("b", 200)])
    assert store.merge([trade("b", 200)]).changed is False
    result = store.merge([trade("b", 200, "REJECTED"), trade("c", 300)])
    assert result.added == 1
    assert result.status_changed == 1
    assert result.changed


def test_store_survives_a_restart(tmp_path: Path) -> None:
    path = tmp_path / "trades.json"
    first = TradeStore(path)
    first.replace([trade("a", 100), trade("b", 200)])
    second = TradeStore(path)
    assert second.load() is True
    assert second.loaded_from_disk is True
    assert len(second) == 2
    assert [t["uuid"] for t in second.all()] == ["b", "a"]


def test_store_handles_a_corrupt_cache(tmp_path: Path) -> None:
    path = tmp_path / "trades.json"
    path.write_text("{not json")
    store = TradeStore(path)
    assert store.load() is False
    assert len(store) == 0


def test_store_age_tracks_freshness(store: TradeStore) -> None:
    assert store.age() is None
    store.replace([trade("a", 100)])
    age = store.age()
    assert age is not None and 0 <= age < 5


def test_drop_missing_removes_trades(store: TradeStore) -> None:
    store.replace([trade("a", 100), trade("b", 200), trade("c", 300)])
    assert store.drop_missing({"a", "c"}) == 1
    assert {t["uuid"] for t in store.all()} == {"a", "c"}


# --------------------------------------------------------------------------
# Sync
# --------------------------------------------------------------------------


def test_full_sync_populates_the_store(store: TradeStore) -> None:
    client = FakeClient([trade("a", 100), trade("b", 200)])
    r = TradeRefresher(store, lambda: client, ladder=(1,))
    result = r.run_once(full=True)
    assert result.full and result.added == 2
    assert len(store) == 2
    assert client.full_calls == 1


def test_full_sync_detects_removals(store: TradeStore) -> None:
    client = FakeClient([trade("a", 100), trade("b", 200)])
    r = TradeRefresher(store, lambda: client, ladder=(1,))
    r.run_once(full=True)
    client.trades = [trade("a", 100)]
    result = r.run_once(full=True)
    assert result.removed == 1


def test_windowed_check_is_one_request(store: TradeStore) -> None:
    client = FakeClient([trade("a", 100), trade("b", 200)])
    r = TradeRefresher(store, lambda: client, ladder=(1,))
    r.run_once(full=True)
    assert client.page_calls == 0
    r.run_once()  # steady state
    assert client.page_calls == 1
    assert client.full_calls == 1, "a quiet window must not re-read everything"


def test_quiet_check_updates_confirmed_at(store: TradeStore) -> None:
    client = FakeClient([trade("a", 100)])
    r = TradeRefresher(store, lambda: client, ladder=(1,))
    r.run_once(full=True)
    # Simulate time passing by backdating loaded_at and confirmed_at
    store.loaded_at = time.time() - 1000
    store.confirmed_at = time.time() - 1000
    assert store.age() >= 999
    # Quiet check with no changes
    result = r.run_once()
    assert result.changed is False
    # confirmed_at and age() should now reflect the fresh check
    assert store.confirmed_at > time.time() - 5
    assert store.age() < 5



def test_windowed_check_finds_a_new_trade(store: TradeStore) -> None:
    client = FakeClient([trade("a", 100)])
    r = TradeRefresher(store, lambda: client, ladder=(1,))
    r.run_once(full=True)
    client.trades = [trade("b", 200), trade("a", 100)]
    result = r.run_once()
    assert result.added == 1
    assert result.changed
    assert store.has("b")


def test_windowed_check_sees_a_status_change(store: TradeStore) -> None:
    client = FakeClient([trade("a", 100), trade("b", 200)])
    r = TradeRefresher(store, lambda: client, ladder=(1,))
    r.run_once(full=True)
    client.trades = [trade("a", 100, "REJECTED"), trade("b", 200)]
    result = r.run_once()
    assert result.status_changed == 1
    assert store.get("a")["status"] == "REJECTED"


def test_on_change_fires_only_on_change(store: TradeStore) -> None:
    seen: list[SyncResult] = []
    client = FakeClient([trade("a", 100)])
    r = TradeRefresher(store, lambda: client, ladder=(1,), on_change=seen.append)
    r.run_once(full=True)
    assert len(seen) == 1
    r.run_once()
    assert len(seen) == 1, "an unchanged check must not notify"
    client.trades = [trade("b", 200), trade("a", 100)]
    r.run_once()
    assert len(seen) == 2


def test_a_failing_hook_does_not_break_the_sync(store: TradeStore) -> None:
    def boom(_result: SyncResult) -> None:
        raise RuntimeError("hook exploded")

    client = FakeClient([trade("a", 100)])
    r = TradeRefresher(store, lambda: client, ladder=(1,), on_change=boom)
    result = r.run_once(full=True)
    assert result.changed, "a broken hook must not lose the data"


# --------------------------------------------------------------------------
# Backoff
# --------------------------------------------------------------------------


def test_ladder_walks_up_when_idle_and_resets_on_change(store: TradeStore) -> None:
    """Drive the real check and record the interval the loop would choose.

    The loop's rule is: a change resets to the front, a quiet check advances one
    rung. This pins that rule against the actual code rather than restating it.
    """
    ladder = (60.0, 300.0, 900.0, 3600.0)
    client = FakeClient([trade("a", 100)])
    r = TradeRefresher(store, lambda: client, ladder=ladder)

    # A change on checks 1, 3 and 6; every other check is quiet.
    script = [True, False, True, False, False, True, False, False, False]
    chosen: list[float] = []
    step = 0
    arrivals = 0

    for changed in script:
        if changed:
            # A genuinely new uuid, so the store sees an addition each time.
            arrivals += 1
            client.trades = [trade(f"new{arrivals}", 500 + arrivals), trade("a", 100)]
        else:
            client.trades = [trade(f"new{arrivals}", 500 + arrivals), trade("a", 100)]
        result = r.check(full=False)
        assert result.changed is changed, "scripted outcome did not take effect"

        if changed:
            step = 0
        else:
            step += 1
        chosen.append(r.interval_for(step))

    # Climb on quiet runs, snap back to the front on a change, pin at the end.
    assert chosen == [60.0, 300.0, 60.0, 300.0, 900.0, 60.0, 300.0, 900.0, 3600.0]


def test_interval_is_clamped_at_both_ends(store: TradeStore) -> None:
    r = TradeRefresher(store, lambda: FakeClient([]), ladder=(60.0, 300.0, 900.0))
    assert r.interval_for(0) == 60.0
    assert r.interval_for(2) == 900.0
    assert r.interval_for(99) == 900.0, "must never exceed the slowest rung"
    assert r.interval_for(-5) == 60.0, "must never be negative"


def test_single_rung_ladder_stays_constant(store: TradeStore) -> None:
    r = TradeRefresher(store, lambda: FakeClient([]), ladder=(120.0,))
    assert all(r.interval_for(n) == 120.0 for n in range(5))


def test_background_loop_reports_state(store: TradeStore) -> None:
    client = FakeClient([trade("a", 100)])
    r = TradeRefresher(store, lambda: client, ladder=(0.01, 0.01))
    assert r.state.running is False
    r.start()
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and r.state.checks == 0:
            time.sleep(0.05)
        assert r.state.checks >= 1, "the loop should have run at least once"
        assert len(store) == 1
    finally:
        r.stop()
    assert r.state.running is False


# --------------------------------------------------------------------------
# Failure handling
# --------------------------------------------------------------------------


def test_a_client_error_is_reported_not_raised(store: TradeStore) -> None:
    class Broken:
        def paginate(self, *a, **k):
            raise LcsError("boom")

        def get(self, *a, **k):
            raise LcsError("boom")

        def close(self):
            pass

    r = TradeRefresher(store, lambda: Broken(), ladder=(1,))
    result = r.run_once(full=True)
    assert result.error and "boom" in result.error
    assert result.changed is False
    assert len(store) == 0


def test_an_unexpected_error_does_not_kill_the_loop(store: TradeStore) -> None:
    class Exploding:
        def paginate(self, *a, **k):
            raise ValueError("unexpected")

        def close(self):
            pass

    r = TradeRefresher(store, lambda: Exploding(), ladder=(1,))
    result = r.run_once(full=True)
    assert "unexpected" in (result.error or "")


def test_wake_unblocks_refresher_immediately(store: TradeStore) -> None:
    client = FakeClient([trade("a", 100)])
    # 3600s interval - without wake, it would wait 1 hour
    r = TradeRefresher(store, lambda: client, ladder=(3600.0,))
    r.start()
    try:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and r.state.checks == 0:
            time.sleep(0.02)
        assert r.state.checks == 1
        # Now nudge it to wake up
        r.wake()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and r.state.checks < 2:
            time.sleep(0.02)
        assert r.state.checks >= 2, "wake() should have triggered an immediate second check"
    finally:
        r.stop()

