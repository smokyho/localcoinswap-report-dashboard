"""Keeps the trade history current without anyone asking.

Two facts about LocalCoinSwap drive the design:

* ``/trades/list/?limit=100&offset=0`` returns the newest trades first, so a
  single request tells us whether anything new has appeared.
* Trades only enter your history once they are live, and every status the
  account actually holds is terminal (COMPLETED, REJECTED, EXPIRED, and so
  on). A trade therefore does not sit in your list in a state it later leaves.

So one cheap request is enough to detect new trades. It cannot see a status
change on a trade older than the window, and on this account that does not
happen -- but the gap after a long outage is unbounded, so the first check
after startup is always a full re-read rather than a window diff.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .lcs_client import LcsAuthError, LcsClient, LcsError

log = logging.getLogger(__name__)


@dataclass
class SyncResult:
    """What one pass of the loop found."""

    full: bool = False
    added: int = 0
    status_changed: int = 0
    removed: int = 0
    error: str | None = None

    @property
    def changed(self) -> bool:
        return bool(self.added or self.status_changed or self.removed)

    def as_dict(self) -> dict[str, Any]:
        return {
            "full": self.full,
            "added": self.added,
            "status_changed": self.status_changed,
            "removed": self.removed,
            "error": self.error,
        }


class TradeStore:
    """The trade list, held in memory and mirrored to disk.

    Persisting matters because the background loop lives in the server process:
    without it, every restart would pay for a full re-read of the whole
    history.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._by_uuid: dict[str, dict[str, Any]] = {}
        self._order: list[str] = []
        self.loaded_at: float = 0.0
        self.confirmed_at: float = 0.0
        self.loaded_from_disk = False

    # -- reads ----------------------------------------------------------

    def __len__(self) -> int:
        with self._lock:
            return len(self._order)

    def all(self) -> list[dict[str, Any]]:
        """Trades newest first, matching the API's own ordering."""
        with self._lock:
            return [self._by_uuid[u] for u in self._order if u in self._by_uuid]

    def get(self, uuid: str) -> dict[str, Any] | None:
        with self._lock:
            return self._by_uuid.get(uuid)

    def has(self, uuid: str) -> bool:
        with self._lock:
            return uuid in self._by_uuid

    def uuids(self) -> set[str]:
        with self._lock:
            return set(self._by_uuid)

    def age(self) -> float | None:
        with self._lock:
            ref = self.confirmed_at or self.loaded_at
            if not self._order or not ref:
                return None
            return time.time() - ref

    def touch_confirmed(self, ts: float | None = None) -> None:
        with self._lock:
            self.confirmed_at = ts or time.time()

    # -- writes ---------------------------------------------------------

    def replace(self, trades: list[dict[str, Any]]) -> None:
        with self._lock:
            self._by_uuid = {
                str(t.get("uuid")): t for t in trades if t.get("uuid")
            }
            self._order = self._newest_first(self._by_uuid.values())
            self.loaded_at = time.time()
        self.save()

    def merge(self, trades: list[dict[str, Any]]) -> SyncResult:
        """Fold a page of trades into the store, reporting what moved."""
        result = SyncResult()
        with self._lock:
            for trade in trades:
                uuid = str(trade.get("uuid") or "")
                if not uuid:
                    continue
                existing = self._by_uuid.get(uuid)
                if existing is None:
                    self._by_uuid[uuid] = trade
                    result.added += 1
                elif existing.get("status") != trade.get("status"):
                    self._by_uuid[uuid] = trade
                    result.status_changed += 1
            if result.changed:
                self._order = self._newest_first(self._by_uuid.values())
                self.loaded_at = time.time()
        if result.changed:
            self.save()
        return result

    def drop_missing(self, keep: set[str]) -> int:
        with self._lock:
            gone = [u for u in self._by_uuid if u not in keep]
            for u in gone:
                del self._by_uuid[u]
            if gone:
                self._order = self._newest_first(self._by_uuid.values())
                self.loaded_at = time.time()
        if gone:
            self.save()
        return len(gone)

    @staticmethod
    def _newest_first(trades) -> list[str]:
        return sorted(
            (str(t.get("uuid")) for t in trades),
            key=lambda u: next(
                (
                    t.get("time_created") or 0
                    for t in trades
                    if str(t.get("uuid")) == u
                ),
                0,
            ),
            reverse=True,
        )

    # -- persistence ----------------------------------------------------

    def load(self) -> bool:
        if not self.path.exists():
            return False
        try:
            data = json.loads(self.path.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("could not read the trade cache: %s", exc)
            return False
        trades = data.get("trades") if isinstance(data, dict) else None
        if not isinstance(trades, list):
            return False
        self.replace([t for t in trades if isinstance(t, dict)])
        with self._lock:
            self.loaded_at = float(data.get("saved_at") or time.time())
            self.loaded_from_disk = True
        log.info("loaded %d cached trades from disk", len(trades))
        return True

    def save(self) -> None:
        with self._lock:
            payload = {
                "saved_at": time.time(),
                "trades": self.all(),
            }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload))
            tmp.replace(self.path)
        except OSError as exc:
            log.warning("could not persist the trade cache: %s", exc)

    def mark_stale(self) -> None:
        with self._lock:
            self.loaded_at = 0.0


@dataclass
class RefresherState:
    running: bool = False
    checks: int = 0
    last_check: float | None = None
    last_change: float | None = None
    next_interval: float | None = None
    last_result: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "checks": self.checks,
            "last_check": self.last_check,
            "last_change": self.last_change,
            "next_interval": self.next_interval,
            "last_result": self.last_result,
            "error": self.error,
        }


class TradeRefresher:
    """Background loop that keeps the trade store current."""

    def __init__(
        self,
        store: TradeStore,
        client_factory: Callable[[], LcsClient],
        *,
        ladder: tuple[float, ...] = (60.0, 300.0, 900.0, 3600.0),
        window: int = 100,
        max_items: int = 20_000,
        on_change: Callable[[SyncResult], None] | None = None,
    ) -> None:
        self.store = store
        self.client_factory = client_factory
        self.ladder = ladder or (60.0,)
        self.window = window
        self.max_items = max_items
        self.on_change = on_change
        self.state = RefresherState()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

    # -- lifecycle ------------------------------------------------------

    def start(self) -> bool:
        if self._thread is not None and self._thread.is_alive():
            return False
        self._stop.clear()
        self._wake.clear()
        self.state.running = True
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="trade-refresher"
        )
        self._thread.start()
        return True

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        self.state.running = False
        self._thread = None

    def wake(self) -> None:
        """Wake the background loop immediately if it is sleeping between checks."""
        self._wake.set()

    @property
    def next_due_in(self) -> float | None:
        if self.state.next_interval is None or self.state.last_check is None:
            return None
        due = self.state.last_check + self.state.next_interval
        return max(0.0, round(due - time.time(), 1))

    # -- the loop -------------------------------------------------------

    def interval_for(self, step: int) -> float:
        """Seconds to wait after ``step`` consecutive quiet checks.

        Walks the ladder and holds at the slowest rung, so a report left alone
        for a year costs a handful of requests a day rather than half a million.
        """
        return self.ladder[min(max(step, 0), len(self.ladder) - 1)]

    def _run(self) -> None:
        step = 0
        first = True
        log.info("trade-refresher background loop started")
        while not self._stop.is_set():
            result = self.check(full=first)
            first = False
            self.state.checks += 1
            self.state.last_check = time.time()
            self.state.last_result = result.as_dict()
            self.state.error = result.error

            if result.error:
                # Do not advance quiet ladder on error; retry after a short interval.
                interval = self.interval_for(0)
            elif result.changed:
                step = 0
                self.state.last_change = time.time()
                interval = self.interval_for(step)
            else:
                step += 1
                interval = self.interval_for(step)

            self.state.next_interval = interval
            if self._wake.wait(timeout=interval):
                self._wake.clear()
                step = 0


    def check(self, *, full: bool = False) -> SyncResult:
        """One sync pass. ``full=True`` re-reads the entire history."""
        client = None
        try:
            client = self.client_factory()
            if full:
                res = self._full(client)
            else:
                res = self._window(client)
            if not res.error:
                self.store.touch_confirmed()
            return res
        except LcsAuthError as exc:
            # Bad key: nothing will improve by retrying on a timer.
            self.state.error = str(exc)
            return SyncResult(full=full, error=str(exc))
        except LcsError as exc:
            return SyncResult(full=full, error=str(exc))
        except Exception as exc:  # noqa: BLE001 - the loop must not die
            log.exception("trade refresh failed")
            return SyncResult(full=full, error=f"{type(exc).__name__}: {exc}")
        finally:
            if client is not None:
                client.close()

    def _full(self, client: LcsClient) -> SyncResult:
        trades = list(
            client.paginate(
                "/trades/list/",
                {"ordering": "-time_created"},
                max_items=self.max_items,
            )
        )
        before = self.store.uuids()
        before_status = {
            t["uuid"]: t.get("status")
            for t in self.store.all()
            if t.get("uuid") in before
        }
        self.store.replace(trades)
        result = SyncResult(full=True)
        now = {str(t.get("uuid")) for t in trades}
        result.added = len(now - before)
        result.removed = len(before - now)
        for uuid in now & before:
            row = next(t for t in trades if str(t.get("uuid")) == uuid)
            if before_status.get(uuid) != row.get("status"):
                result.status_changed += 1
        if result.changed:
            log.info(
                "full sync: +%d new, %d status changes, -%d gone",
                result.added, result.status_changed, result.removed,
            )
        if self.on_change and result.changed:
            try:
                self.on_change(result)
            except Exception:  # noqa: BLE001
                log.exception("post-refresh hook failed")
        return result

    def _window(self, client: LcsClient) -> SyncResult:
        """Inspect only the newest trades. One request in the steady state."""
        page = client.get(
            "/trades/list/",
            {"limit": self.window, "offset": 0, "ordering": "-time_created"},
        )
        if not isinstance(page, dict):
            return SyncResult()
        trades = [t for t in (page.get("results") or []) if isinstance(t, dict)]
        result = self.store.merge(trades)
        if result.changed:
            log.info(
                "window sync: +%d new, %d status changes",
                result.added, result.status_changed,
            )
            if self.on_change:
                try:
                    self.on_change(result)
                except Exception:  # noqa: BLE001
                    log.exception("post-refresh hook failed")
        return result

    def run_once(self, *, full: bool = False) -> SyncResult:
        """Synchronous entry point, used by tests and ``/refresh``."""
        return self.check(full=full)


__all__ = ["RefresherState", "SyncResult", "TradeRefresher", "TradeStore"]
