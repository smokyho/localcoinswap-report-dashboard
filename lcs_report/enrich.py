"""Background enrichment: fetch the per-trade detail records.

``/trades/list/`` does not include ``crypto_in_usd`` -- the price actually
frozen at trade creation. It is only on ``/trades/{uuid}/``. For an account
with ~1900 trades that is ~1900 requests, which against the 60/min ceiling is
roughly half an hour. Far too slow for a page load, so it runs once in the
background and is cached on disk.

The report is fully usable before enrichment finishes; market-rate columns
simply stay empty until the detail data arrives.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .config import Settings
from .lcs_client import LcsAuthError, LcsClient, LcsError

log = logging.getLogger(__name__)

SAVE_EVERY = 25


@dataclass
class EnrichmentProgress:
    total: int = 0
    done: int = 0
    running: bool = False
    started_at: float | None = None
    finished_at: float | None = None
    error: str | None = None
    last_saved: float = 0.0

    @property
    def pct(self) -> float:
        # `total` is only known once trades have been listed, so a store that is
        # already fully priced would otherwise report 0% and look like a stall.
        if not self.total:
            return 100.0 if self.done else 0.0
        return round(100.0 * min(self.done, self.total) / self.total, 1)

    @property
    def eta_seconds(self) -> float | None:
        if not self.running or self.done < 5 or not self.started_at:
            return None
        rate = (time.monotonic() - self.started_at) / self.done
        return max(0.0, round(rate * (self.total - self.done)))

    def as_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "done": self.done,
            "pct": self.pct,
            "running": self.running,
            "eta_seconds": self.eta_seconds,
            "error": self.error,
        }


class Enricher:
    """Fetches and caches ``crypto_in_usd`` / ``crypto_in_local_currency``."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.path: Path = settings.http_cache_path.parent / "trade_details.json"
        self._lock = threading.Lock()
        self._cache: dict[str, dict[str, Any]] = {}
        self._thread: threading.Thread | None = None
        self.progress = EnrichmentProgress()
        self._load()

    # -- persistence ----------------------------------------------------

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text())
            if isinstance(data, dict):
                self._cache = {k: v for k, v in data.items() if isinstance(v, dict)}
                # `done` is what we hold; `total` is the account's trade count,
                # which only becomes known once trades have been listed. Leaving
                # total at 0 avoids reporting a misleading "75 of 75".
                self.progress.done = len(self._cache)
                self.progress.finished_at = self.path.stat().st_mtime
                log.info("loaded %d cached trade details", len(self._cache))
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("could not read enrichment cache: %s", exc)

    def set_expected_total(self, total: int) -> None:
        """Record how many trades exist, so progress can be reported as a ratio."""
        with self._lock:
            if self.progress.running:
                return
            self.progress.total = total
            self.progress.done = len(self._cache)

    def _save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(self._cache))
            tmp.replace(self.path)
            self.progress.last_saved = time.time()
        except OSError as exc:
            log.warning("could not persist enrichment cache: %s", exc)

    # -- reads ----------------------------------------------------------

    def detail(self, uuid: str) -> dict[str, Any] | None:
        with self._lock:
            return self._cache.get(uuid)

    def details_for(self, uuids: list[str]) -> dict[str, dict[str, Any]]:
        with self._lock:
            return {u: self._cache[u] for u in uuids if u in self._cache}

    @property
    def cached_count(self) -> int:
        with self._lock:
            return len(self._cache)

    def missing(self, uuids: list[str]) -> list[str]:
        with self._lock:
            return [u for u in uuids if u not in self._cache]

    # -- writes ---------------------------------------------------------

    def apply(self, details: dict[str, dict[str, Any]]) -> int:
        """Merge fetched detail records into the cache. Returns count added."""
        if not details:
            return 0
        with self._lock:
            before = len(self._cache)
            self._cache.update(details)
            return len(self._cache) - before

    def prune(self, keep_uuids: set[str]) -> int:
        with self._lock:
            stale = [u for u in self._cache if u not in keep_uuids]
            for u in stale:
                del self._cache[u]
            if stale:
                self._save()
            return len(stale)

    # -- background run -------------------------------------------------

    def start(
        self,
        uuids: list[str],
        *,
        client_factory: Callable[[], LcsClient] | None = None,
    ) -> bool:
        """Kick off a background fetch. Returns False if already running."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False

        pending = self.missing(uuids)
        self.progress = EnrichmentProgress(
            total=len(uuids), done=len(uuids) - len(pending), running=True
        )
        self.progress.started_at = time.monotonic()
        if not pending:
            self.progress.running = False
            self.progress.finished_at = time.time()
            return True

        def run() -> None:
            factory = client_factory or (lambda: LcsClient(
                self.settings.api_key,
                page_size=self.settings.page_size,
                min_interval=self.settings.min_interval_seconds,
                timeout=self.settings.request_timeout,
            ))
            client = factory()
            batch: dict[str, dict[str, Any]] = {}
            try:
                for uuid in pending:
                    try:
                        payload = client.get(f"/trades/{uuid}/")
                    except LcsAuthError as exc:
                        self.progress.error = str(exc)
                        self.progress.running = False
                        self.progress.finished_at = time.time()
                        return
                    except LcsError as exc:
                        # One bad trade should not abandon the whole run.
                        log.info("detail fetch failed for %s: %s", uuid, exc)
                        payload = None
                    if isinstance(payload, dict):
                        batch[uuid] = {
                            k: payload.get(k)
                            for k in (
                                "crypto_in_usd",
                                "crypto_in_local_currency",
                                "id",
                                "fees",
                                "amount_to_escrow",
                                "non_custodial",
                                "time_created",
                            )
                            if k in payload
                        }
                        if "local_currency_symbol" in payload:
                            batch[uuid]["local_currency_symbol"] = payload[
                                "local_currency_symbol"
                            ]
                    self.progress.done += 1
                    if len(batch) >= SAVE_EVERY:
                        self.apply(batch)
                        self._save()
                        batch = {}
                if batch:
                    self.apply(batch)
                    self._save()
            except Exception as exc:  # noqa: BLE001 - never kill the thread silently
                self.progress.error = f"{type(exc).__name__}: {exc}"
                log.exception("enrichment run failed")
            finally:
                client.close()
                self.progress.running = False
                self.progress.finished_at = time.time()

        self._thread = threading.Thread(target=run, daemon=True, name="enricher")
        self._thread.start()
        return True


__all__ = ["Enricher", "EnrichmentProgress"]