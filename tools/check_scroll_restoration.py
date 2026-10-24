"""Verify that navigating between views keeps the scroll position.

Drives a real browser: scrolls down the monthly report, clicks a year tab, and
checks that the new view is rendered at roughly the same offset rather than at
the top of the document.

    python3 tools/check_scroll_restoration.py [url] [--no-js]

``--no-js`` disables page scripts, which restores the browser's default
behaviour. Running it that way is how you confirm this check actually detects
the regression rather than passing vacuously.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
import urllib.request

import websockets

CHROME = "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"
CDP_PORT = 9334
MIN_TARGET = 24


async def main(url: str, run_js: bool = True) -> int:
    subprocess.Popen(
        [
            CHROME, "--headless=new", "--disable-gpu", "--no-sandbox",
            "--no-first-run", "--disable-extensions",
            f"--remote-debugging-port={CDP_PORT}",
            "--user-data-dir=/tmp/cdp-scroll-profile", "about:blank",
        ],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

    ws_url = None
    for _ in range(60):
        try:
            pages = [
                t for t in json.load(
                    urllib.request.urlopen(f"http://127.0.0.1:{CDP_PORT}/json", timeout=5)
                )
                if t.get("type") == "page"
            ]
            if pages:
                ws_url = pages[0]["webSocketDebuggerUrl"]
                break
        except Exception:
            pass
        time.sleep(0.5)
    if not ws_url:
        print("could not attach to the browser")
        return 2

    failures = 0
    async with websockets.connect(ws_url, max_size=64 * 1024 * 1024) as ws:
        seq = 0

        async def call(method: str, params: dict | None = None) -> dict:
            nonlocal seq
            seq += 1
            await ws.send(json.dumps({"id": seq, "method": method, "params": params or {}}))
            while True:
                msg = json.loads(await ws.recv())
                if msg.get("id") == seq:
                    return msg.get("result", {})

        async def evaluate(expr: str):
            r = await call("Runtime.evaluate", {"expression": expr, "returnByValue": True})
            return r.get("result", {}).get("value")

        await call("Page.enable")
        await call("Runtime.enable")
        if not run_js:
            await call("Emulation.setScriptExecutionDisabled", {"value": True})
            print("(page scripts disabled: expecting the browser default, i.e. a jump)")
        await call("Emulation.setDeviceMetricsOverride",
                   {"width": 1440, "height": 900, "deviceScaleFactor": 1, "mobile": False})
        await call("Page.navigate", {"url": url})
        for _ in range(160):
            await asyncio.sleep(0.25)
            if (await evaluate("document.querySelectorAll('tbody tr').length")) > 0:
                break
        await asyncio.sleep(0.5)

        doc_height = await evaluate("document.documentElement.scrollHeight")
        print(f"document height: {doc_height}px")

        # Scroll well down the page, the way a user reading the monthly report would.
        target_y = 2400
        await evaluate(f"window.scrollTo(0, {target_y})")
        await asyncio.sleep(0.5)
        before = await evaluate("Math.round(window.scrollY)")
        restored_setting = await evaluate("history.scrollRestoration")
        print(f"scrolled to {before}px (history.scrollRestoration={restored_setting})")

        if before < MIN_TARGET:
            print(f"FAIL: could not scroll (page may be too short: {doc_height}px)")
            return 1

        # Find a year tab that is not the current one, and click it.
        clicked = await evaluate(
            """(() => {
              const links = [...document.querySelectorAll('.yearnav__item')];
              const other = links.find(a => !a.classList.contains('is-current'));
              if (!other) return null;
              const label = other.textContent.trim();
              other.click();
              return label;
            })()"""
        )
        if not clicked:
            print("SKIP: only one year available, cannot test navigation")
            return 0
        print(f"clicked year tab: {clicked}")

        # Wait for the new document.
        for _ in range(200):
            await asyncio.sleep(0.25)
            rows = await evaluate("document.querySelectorAll('tbody tr').length")
            if rows > 0:
                break
        await asyncio.sleep(1.0)  # let the restore handler run

        after = await evaluate("Math.round(window.scrollY)")
        current = await evaluate(
            """(() => {
                 const el = document.querySelector('.yearnav__item.is-current');
                 return el ? el.textContent.trim() : null;
               })()"""
        )
        print(f"landed on year {current}, scrollY = {after}px")

        drift = abs(after - before)
        tolerance = max(60, doc_height * 0.02)
        if drift <= tolerance:
            print(f"OK: scroll held within {drift:.0f}px of {before}px")
        else:
            print(f"FAIL: scroll jumped {before} -> {after} (drift {drift:.0f}px)")
            failures += 1

    return 1 if failures else 0


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    use_js = "--no-js" not in sys.argv
    target = args[0] if args else "http://127.0.0.1:7010/"
    sys.exit(asyncio.run(main(target, run_js=use_js)))