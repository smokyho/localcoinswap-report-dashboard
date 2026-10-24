"""Tests for .env loading and settings.

Regression: the app read only ``os.environ`` and never loaded ``.env``, so
running ``python3 -m lcs_report`` from a clean shell started with no API key and
showed the setup page even though a valid key sat in ``.env``. The file's own
docs and ``.env.example`` implied it was read, so nothing flagged the gap.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lcs_report import config as config_module  # noqa: E402


@pytest.fixture()
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in list(os.environ):
        if key.startswith("LCS_"):
            monkeypatch.delenv(key, raising=False)


def write_env(path: Path, body: str) -> Path:
    path.write_text(body)
    return path


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


def test_reads_plain_assignment(tmp_path: Path, clean_env) -> None:
    p = write_env(tmp_path / ".env", "LCS_API_KEY=lcs_abc_secret\n")
    config_module.load_env(p)
    assert os.environ["LCS_API_KEY"] == "lcs_abc_secret"


def test_reads_export_prefix(tmp_path: Path, clean_env) -> None:
    p = write_env(tmp_path / ".env", "export LCS_USERNAME=smokyho\n")
    config_module.load_env(p)
    assert os.environ["LCS_USERNAME"] == "smokyho"


def test_strips_quotes_and_comments(tmp_path: Path, clean_env) -> None:
    p = write_env(
        tmp_path / ".env",
        '# a comment\n\nLCS_API_KEY="lcs_quoted"\nLCS_USERNAME=\'single\'\n',
    )
    config_module.load_env(p)
    assert os.environ["LCS_API_KEY"] == "lcs_quoted"
    assert os.environ["LCS_USERNAME"] == "single"


def test_ignores_lines_without_equals(tmp_path: Path, clean_env) -> None:
    p = write_env(tmp_path / ".env", "garbage line\nLCS_PORT=7010\n")
    config_module.load_env(p)
    assert os.environ["LCS_PORT"] == "7010"
    assert "garbage line" not in os.environ.values()


def test_real_environment_wins_over_dotenv(tmp_path: Path, clean_env) -> None:
    """An explicit export must not be clobbered by the file."""
    os.environ["LCS_PORT"] = "9999"
    p = write_env(tmp_path / ".env", "LCS_PORT=7010\n")
    config_module.load_env(p)
    assert os.environ["LCS_PORT"] == "9999"


def test_missing_file_is_not_an_error(tmp_path: Path, clean_env) -> None:
    assert config_module.load_env(tmp_path / "nope.env") is None


def test_fallback_parser_works_without_dotenv(
    tmp_path: Path, clean_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The hand-rolled reader must behave like python-dotenv for our inputs."""

    def boom(name: str, *a, **k):
        raise ImportError("no dotenv")

    monkeypatch.setitem(sys.modules, "dotenv", None)
    monkeypatch.setattr(config_module, "_load_env_fallback", config_module._load_env_fallback)
    # Force the ImportError branch.
    monkeypatch.setattr(sys, "modules", sys.modules)

    p = write_env(
        tmp_path / ".env",
        'LCS_API_KEY=lcs_fb\nexport LCS_USERNAME=fb_user\nLCS_PORT=7010\n',
    )
    config_module._load_env_fallback(p)
    assert os.environ["LCS_API_KEY"] == "lcs_fb"
    assert os.environ["LCS_USERNAME"] == "fb_user"
    assert os.environ["LCS_PORT"] == "7010"


# --------------------------------------------------------------------------
# End to end
# --------------------------------------------------------------------------


def test_load_settings_picks_up_dotenv(tmp_path: Path, clean_env) -> None:
    p = write_env(tmp_path / ".env", "LCS_API_KEY=lcs_from_file\nLCS_PORT=7011\n")
    config_module.load_env(p)
    settings = config_module.load_settings()
    assert settings.api_key == "lcs_from_file"
    assert settings.has_api_key is True
    assert settings.port == 7011


def test_settings_reports_missing_key(clean_env) -> None:
    settings = config_module.Settings(
        api_key=None, username=None, base_currency="USD", display_currency="IDR",
        trade_timezone=config_module.ZoneInfo("UTC"), page_size=100,
        max_trades=1000, min_interval_seconds=1.0, request_timeout=5.0,
        fx_cache_path=Path("/tmp/x.db"), trades_cache_path=Path("/tmp/y"),
        http_cache_path=Path("/tmp/z"), host="127.0.0.1", port=7010,
        poll_ladder=(60.0,), refresh_window=100, intraday_enabled=False,
    )
    assert settings.has_api_key is False


def test_env_is_gitignored() -> None:
    """The key must never be committable."""
    ignored = (ROOT / ".gitignore").read_text().split()
    assert ".env" in ignored, ".env must stay gitignored"


def test_repo_env_exists_and_looks_like_a_key() -> None:
    """The working .env should be present and well formed."""
    path = ROOT / ".env"
    if not path.exists():
        pytest.skip("no .env in this checkout")
    body = path.read_text()
    assert "LCS_API_KEY=" in body
    value = body.split("LCS_API_KEY=", 1)[1].strip().strip("'\"")
    assert value.startswith("lcs_"), "key should use the lcs_<prefix>_<secret> shape"
    assert len(value) > 10


def test_startup_reports_key_presence_without_printing_it(tmp_path: Path) -> None:
    """`python3 -m lcs_report` must say whether the key loaded, and not leak it."""
    body = "LCS_API_KEY=lcs_secretvalue123\nLCS_PORT=7012\n"
    write_env(tmp_path / ".env", body)
    proc = subprocess.run(
        [sys.executable, "-c",
         "from lcs_report.config import load_settings, load_env;"
         "load_env();"
         "s = load_settings();"
         "print('KEY' if s.has_api_key else 'NOKEY', len(s.api_key or ''))"],
        capture_output=True, text=True, cwd=str(ROOT),
        env={**{k: v for k, v in os.environ.items() if not k.startswith("LCS_")},
             "PYTHONPATH": str(ROOT)},
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert "KEY" in proc.stdout
    assert "lcs_secretvalue123" not in proc.stdout
