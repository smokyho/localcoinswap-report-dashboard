"""Configuration, from the environment with a .env fallback."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
CACHE_DIR = BASE_DIR / ".cache"

LCS_API_BASE = "https://api.localcoinswap.com/api/v2"
FRANKFURTER_BASE = "https://api.frankfurter.dev/v1"

ENV_PATH = BASE_DIR / ".env"


def load_env(path: Path | None = None) -> Path | None:
    """Load ``.env`` into ``os.environ`` before settings are read.

    Without this the .env file is silently ignored: the setup instructions and
    .env.example both imply it is picked up, so running ``python3 -m lcs_report``
    from a fresh shell used to start with no API key at all.

    Real environment variables win, so an explicit override on the command line
    still takes effect.
    """
    target = path or ENV_PATH
    if not target.exists():
        return None
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover - exercised only without the extra
        _load_env_fallback(target)
        return target
    load_dotenv(target, override=False)
    return target


def _load_env_fallback(path: Path) -> None:
    """Minimal reader used when python-dotenv is not installed.

    Handles ``KEY=value``, ``export KEY=value``, ``#`` comments, blank lines and
    single or double quoted values. It deliberately does not expand variables.
    """
    try:
        lines = path.read_text().splitlines()
    except OSError as exc:
        log.warning("could not read %s: %s", path, exc)
        return
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


class ConfigError(RuntimeError):
    """Raised when required configuration is missing."""


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


def _env_tz(name: str, default: str) -> ZoneInfo:
    raw = os.environ.get(name, default).strip() or default
    try:
        return ZoneInfo(raw)
    except Exception as exc:  # noqa: BLE001 - surface a friendly message
        raise ConfigError(f"{name} is not a valid IANA timezone: {raw!r}") from exc


@dataclass(frozen=True)
class Settings:
    api_key: str | None
    username: str | None
    base_currency: str
    display_currency: str
    trade_timezone: ZoneInfo
    page_size: int
    max_trades: int
    min_interval_seconds: float
    request_timeout: float
    fx_cache_path: Path
    trades_cache_path: Path
    http_cache_path: Path
    host: str
    port: int
    poll_ladder: tuple[float, ...]
    refresh_window: int
    intraday_enabled: bool

    @property
    def has_api_key(self) -> bool:
        return bool(self.api_key)


def load_settings() -> Settings:
    """Build settings from the environment, after loading ``.env``.

    The API key is read here but never logged. It is sent only in the
    Authorization header.
    """
    loaded = load_env()
    if loaded is None:
        log.info(
            "no .env at %s; relying on the process environment "
            "(LCS_API_KEY must be exported)", ENV_PATH
        )
    return Settings(
        api_key=(os.environ.get("LCS_API_KEY") or "").strip() or None,
        username=(os.environ.get("LCS_USERNAME") or "").strip() or None,
        base_currency=(os.environ.get("LCS_BASE_CURRENCY") or "USD").strip().upper(),
        display_currency=(
            os.environ.get("LCS_DISPLAY_CURRENCY") or "IDR"
        ).strip().upper(),
        # Trades carry a unix timestamp. Which calendar day that maps to can
        # shift the FX rate by one day near midnight, so the zone is explicit.
        trade_timezone=_env_tz("LCS_TRADE_TIMEZONE", "UTC"),
        page_size=max(1, min(200, _env_int("LCS_PAGE_SIZE", 200))),
        max_trades=max(1, _env_int("LCS_MAX_TRADES", 20_000)),
        # The API allows 60 requests/minute/account. 1.1s spacing keeps a
        # single account comfortably under that ceiling.
        min_interval_seconds=_env_float("LCS_MIN_INTERVAL", 1.1),
        request_timeout=_env_float("LCS_TIMEOUT", 30.0),
        fx_cache_path=CACHE_DIR / "fx_usd_idr.json",
        trades_cache_path=CACHE_DIR / "trades.json",
        http_cache_path=CACHE_DIR / "http_cache.json",
        # 0.0.0.0 so the report is reachable from other devices on the network.
        host=(os.environ.get("LCS_HOST") or "0.0.0.0").strip(),
        port=_env_int("LCS_PORT", 7010),
        # Seconds between background checks of LCS for new trades. The ladder is
        # walked in order while nothing changes, and reset to the front as soon
        # as something does, so an app in active use stays live while an
        # abandoned one costs almost nothing. Over a year at a flat 60s this
        # would be half a million requests for no reason.
        poll_ladder=tuple(
            float(x)
            for x in (
                os.environ.get("LCS_POLL_LADDER")
                or "60,180,300,900"
            ).split(",")
            if x.strip()
        ),
        # How many of the most recent trades to inspect on a cheap check. New
        # trades appear at the top because the list is sorted newest-first.
        refresh_window=_env_int("LCS_REFRESH_WINDOW", 100),
        # Hourly bars improve timing; the daily desk always works without them.
        intraday_enabled=(
            os.environ.get("LCS_INTRADAY", "1").strip().lower()
            not in ("0", "false", "off", "no")
        ),
    )


SETTINGS = load_settings()

__all__ = [
    "BASE_DIR",
    "CACHE_DIR",
    "ConfigError",
    "FRANKFURTER_BASE",
    "LCS_API_BASE",
    "SETTINGS",
    "Settings",
    "Decimal",
    "load_settings",
]
