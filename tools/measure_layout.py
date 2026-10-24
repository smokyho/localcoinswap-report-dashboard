"""Measure the report's layout at each breakpoint over DevTools Protocol.

Reports any element wider than the viewport, so horizontal overflow is caught
by measurement rather than by eyeballing a screenshot.

    python3 tools/measure_layout.py [url] [port]
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
CDP_PORT = 9333

WIDTHS = [(320, 900), (375, 900), (768, 1000), (1024, 1100), (1440, 1200)]

PROBE = """
(() => {
  const vw = document.documentElement.clientWidth;
  const bad = [];
  for (const el of document.querySelectorAll('body *')) {
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) continue;
    const cls = (el.className && el.className.baseVal !== undefined)
      ? el.className.baseVal : (el.className || '');
    if (r.right > vw + 1) {
      // An element that scrolls inside its own box is fine.
      let p = el, contained = false;
      while (p && p !== document.body) {
        const s = getComputedStyle(p);
        if (s.overflowX === 'auto' || s.overflowX === 'scroll') { contained = true; break; }
        p = p.parentElement;
      }
      if (!contained) {
        bad.push({
          tag: el.tagName.toLowerCase(),
          cls: String(cls).slice(0, 60),
          w: Math.round(r.width),
          right: Math.round(r.right),
          overflowBy: Math.round(r.right - vw),
        });
      }
    }
  }
  // Any element whose own content overflows it without scrolling.
  const clipped = [];
  for (const el of document.querySelectorAll('body *')) {
    if (el.scrollWidth > el.clientWidth + 1) {
      const s = getComputedStyle(el);
      if (s.overflowX === 'visible' && el.clientWidth > 0) {
        const cls = (el.className && el.className.baseVal !== undefined)
          ? el.className.baseVal : (el.className || '');
        clipped.push({
          tag: el.tagName.toLowerCase(),
          cls: String(cls).slice(0, 60),
          clientW: el.clientWidth,
          scrollW: el.scrollWidth,
        });
      }
    }
  }
  // WCAG 2.5.8 measures the *target*, and a control wrapped in a label
  // inherits the label's hit area. Measure the effective target, not just the
  // control, otherwise every wrapped checkbox is a false positive.
  const tapTooSmall = [];
  for (const el of document.querySelectorAll('a, button, select, input')) {
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) continue;
    const label = el.closest('label');
    const lr = label ? label.getBoundingClientRect() : null;
    const effH = lr && lr.height > 0 ? Math.max(r.height, lr.height) : r.height;
    const effW = lr && lr.width > 0 ? Math.max(r.width, lr.width) : r.width;
    if (effH < 24 || effW < 24) {
      tapTooSmall.push({
        tag: el.tagName.toLowerCase(),
        text: (el.textContent || el.getAttribute('aria-label') || '').trim().slice(0, 24),
        controlH: Math.round(r.height),
        targetH: Math.round(effH),
        targetW: Math.round(effW),
        viaLabel: !!label,
      });
    }
  }
  // Find the deepest elements that stick out, *including* ones inside scroll
  // containers. A scroll container should stop the width propagating, so if the
  // document still overflows, the containment is not doing its job.
  const outliers = [];
  for (const el of document.querySelectorAll('body *')) {
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) continue;
    if (r.right <= vw + 1) continue;
    const cls = (el.className && el.className.baseVal !== undefined)
      ? el.className.baseVal : (el.className || '');
    const s = getComputedStyle(el);
    if (s.position === 'fixed') continue;
    // A visually-hidden label inside a scrolling table is expected to sit past
    // the viewport; what matters is whether the *document* ends up wider.
    if (String(cls).indexOf('sr-only') !== -1) continue;
    // Record the chain of scroll containers, to see where containment is lost.
    const chain = [];
    let p = el.parentElement;
    while (p && p !== document.body) {
      const ps = getComputedStyle(p);
      chain.push(p.tagName.toLowerCase() + (p.className ? '.' + String(p.className).split(/\s+/)[0] : '') + '=' + ps.overflowX);
      p = p.parentElement;
    }
    outliers.push({
      chain: chain.slice(0, 3).join(' < '),
      tag: el.tagName.toLowerCase(),
      cls: String(cls).slice(0, 50),
      right: Math.round(r.right),
      w: Math.round(r.width),
      overflowX: s.overflowX,
      minWidth: s.minWidth,
      whiteSpace: s.whiteSpace,
    });
  }
  // Which top-level blocks are wider than the viewport?
  const wide = [];
  for (const el of document.querySelectorAll('main > *, .wrap > *')) {
    const r = el.getBoundingClientRect();
    if (r.width > vw + 1) {
      const cs = getComputedStyle(el);
      wide.push({
        tag: el.tagName.toLowerCase(),
        cls: String(el.className || '').slice(0, 40),
        w: Math.round(r.width),
        scrollW: el.scrollWidth,
        minW: cs.minWidth,
        display: cs.display,
        children: [...el.children].map(c => c.tagName.toLowerCase() + '.' + String(c.className || '').split(/\s+/)[0] + '=' + Math.round(c.getBoundingClientRect().width)).slice(0, 6),
      });
    }
  }

  return {
    vw,
    wide,
    docScrollW: document.documentElement.scrollWidth,
    pageScrollsSideways: document.documentElement.scrollWidth > vw + 1,
    outliers: outliers.slice(-10),
    outlierCount: outliers.length,
    overflowing: bad.slice(0, 12),
    overflowCount: bad.length,
    clipped: clipped.slice(0, 8),
    clippedCount: clipped.length,
    tapTooSmall: tapTooSmall.slice(0, 8),
    tapTooSmallCount: tapTooSmall.length,
    sections: document.querySelectorAll('section').length,
    rows: document.querySelectorAll('tbody tr').length,
  };
})()
"""


def cdp_targets() -> list[dict]:
    with urllib.request.urlopen(f"http://127.0.0.1:{CDP_PORT}/json", timeout=10) as r:
        return json.load(r)


async def main(url: str) -> int:
    subprocess.Popen(
        [
            CHROME,
            "--headless=new",
            "--disable-gpu",
            "--no-sandbox",
            "--no-first-run",
            "--disable-extensions",
            f"--remote-debugging-port={CDP_PORT}",
            "--user-data-dir=/tmp/cdp-profile",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    ws_url = None
    for _ in range(60):
        try:
            targets = cdp_targets()
            pages = [t for t in targets if t.get("type") == "page"]
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
            await ws.send(
                json.dumps({"id": seq, "method": method, "params": params or {}})
            )
            while True:
                msg = json.loads(await ws.recv())
                if msg.get("id") == seq:
                    return msg.get("result", {})

        await call("Page.enable")
        await call("Runtime.enable")

        for width, height in WIDTHS:
            await call(
                "Emulation.setDeviceMetricsOverride",
                {
                    "width": width,
                    "height": height,
                    "deviceScaleFactor": 1,
                    "mobile": width < 768,
                },
            )
            await call("Page.navigate", {"url": url})
            # Wait for the report body, not just the load event.
            for _ in range(120):
                await asyncio.sleep(0.25)
                r = await call(
                    "Runtime.evaluate",
                    {
                        "expression": "document.querySelectorAll('tbody tr').length",
                        "returnByValue": True,
                    },
                )
                if r.get("result", {}).get("value", 0) > 0:
                    break
            await asyncio.sleep(0.4)

            r = await call(
                "Runtime.evaluate",
                {"expression": PROBE, "returnByValue": True},
            )
            data = r.get("result", {}).get("value")
            if not data:
                print(f"{width:>5}px  PROBE FAILED")
                failures += 1
                continue

            ok = not data["pageScrollsSideways"] and data["overflowCount"] == 0
            mark = "ok  " if ok else "FAIL"
            print(f"{width:>5}px  {mark}  sideways={data['pageScrollsSideways']} "
                  f"overflow={data['overflowCount']} clipped={data['clippedCount']} "
                  f"small-taps={data['tapTooSmallCount']} rows={data['rows']}")
            if not ok:
                failures += 1
                for item in data.get("outliers", []):
                    print(f"          OUTLIER {item['tag']}.{item['cls']} right={item['right']} "
                          f"w={item['w']} chain={item.get('chain')}")
                for item in data["overflowing"]:
                    print(f"          +{item['overflowBy']}px  {item['tag']}.{item['cls']} "
                          f"w={item['w']}")
            for item in data["clipped"][:4]:
                print(f"          clipped {item['tag']}.{item['cls']} "
                      f"{item['clientW']}->{item['scrollW']}")
            for item in data["tapTooSmall"][:4]:
                print(f"          small target {item['tag']} '{item['text']}' "
                      f"control={item.get('controlH')} target={item.get('targetH')}x"
                      f"{item.get('targetW')} viaLabel={item.get('viaLabel')}")

    print()
    print("all breakpoints clean" if not failures else f"{failures} breakpoint(s) with issues")
    return 1 if failures else 0


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:7010/"
    sys.exit(asyncio.run(main(target)))