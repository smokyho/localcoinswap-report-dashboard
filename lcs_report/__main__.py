"""Run the report server.

``python3 -m lcs_report`` -- host and port come from LCS_HOST/LCS_PORT and
default to 0.0.0.0:7010 (7000 is taken by macOS AirPlay Receiver).
"""

from __future__ import annotations

import logging

import uvicorn

from .config import SETTINGS


def main() -> None:
    # Say plainly whether the key was found. Its value is never printed.
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    log = logging.getLogger("lcs_report")
    if SETTINGS.has_api_key:
        log.info(
            "LCS_API_KEY loaded (%d chars) - serving on %s:%d",
            len(SETTINGS.api_key or ""),
            SETTINGS.host,
            SETTINGS.port,
        )
    else:
        log.warning(
            "No API key found. Add LCS_API_KEY to .env at the project root, "
            "or export it. The report will show setup instructions."
        )

    uvicorn.run(
        "lcs_report.app:app",
        host=SETTINGS.host,
        port=SETTINGS.port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
