/* ---------- Navigation ergonomics ----------
 *
 * The year tabs, filters and pager are real links with real URLs, so the view is
 * shareable and the back button works. The cost is that each click is a full
 * document load, and the browser discards scroll position -- so clicking a year
 * dumped you back at the top of a very long page.
 *
 * Scroll is therefore saved before an in-app link is followed and restored
 * afterwards, and a thin progress bar shows that the navigation is underway
 * rather than leaving the page looking frozen.
 */
(function () {
  var KEY = "lcs:scrollY";
  var memory = {};

  function storage() {
    try {
      return window.sessionStorage;
    } catch (e) {
      return null;
    }
  }

  function readStored() {
    var s = storage();
    if (!s) return null;
    try {
      var raw = s.getItem(KEY);
      return raw === null ? null : parseInt(raw, 10) || 0;
    } catch (e) {
      return null;
    }
  }

  function writeStored(y) {
    var s = storage();
    if (!s) return;
    try {
      s.setItem(KEY, String(Math.round(y)));
    } catch (e) {
      /* private mode: fall back to the in-memory copy */
    }
  }

  // Take control of restoration so the browser does not fight us.
  if ("scrollRestoration" in history) {
    history.scrollRestoration = "manual";
  }

  var bar = document.createElement("div");
  bar.className = "nav-progress";
  bar.setAttribute("aria-hidden", "true");
  document.addEventListener("DOMContentLoaded", function () {
    document.body.appendChild(bar);
  });

  function begin() {
    bar.classList.add("is-active");
  }

  function end() {
    bar.classList.remove("is-active");
  }

  // Restore first, before paint, so there is no visible jump to the top.
  var saved = readStored();
  if (saved !== null && saved > 0) {
    var apply = function () {
      window.scrollTo(0, saved);
      end();
    };
    // The stored position is cleared once used, so a later manual visit to the
    // same page starts at the top again.
    var s = storage();
    if (s) { try { s.removeItem(KEY); } catch (e) {} }
    if (document.readyState === "complete") {
      apply();
    } else {
      window.addEventListener("load", function () { requestAnimationFrame(apply); });
    }
  } else {
    window.addEventListener("load", end);
  }

  function remember() {
    writeStored(window.scrollY);
    memory.y = window.scrollY;
  }

  function isInternalLink(a) {
    if (!a || !a.getAttribute("href")) return false;
    if (a.target && a.target !== "_self") return false;
    if (a.hasAttribute("download")) return false;
    var href = a.getAttribute("href");
    if (href.charAt(0) === "#") return false;
    if (/^(https?:)?\/\//.test(href)) {
      try {
        return new URL(href, location.href).origin === location.origin;
      } catch (e) {
        return false;
      }
    }
    return true;
  }

  document.addEventListener("click", function (e) {
    var a = e.target && e.target.closest ? e.target.closest("a") : null;
    if (!isInternalLink(a)) return;
    remember();
    begin();
  });

  // Keep the stored value current while the user scrolls, so a click on a
  // control far down the page resumes from there.
  var pending = 0;
  window.addEventListener(
    "scroll",
    function () {
      if (pending) return;
      pending = window.setTimeout(function () {
        pending = 0;
        memory.y = window.scrollY;
      }, 200);
    },
    { passive: true }
  );

  window.addEventListener("pagehide", remember);
  window.addEventListener("beforeunload", remember);
})();

(function () {
  "use strict";

  /* ---------- Client-side column sorting ---------- */

  var table = document.getElementById("trades-table");
  if (table) {
    var headers = table.querySelectorAll("th.sortable");
    var tbody = table.querySelector("tbody");

    function cellValue(row, index) {
      var cell = row.children[index];
      if (!cell) return "";
      var text = cell.textContent.trim();
      // Strip grouping separators, symbols and arrows so numeric sort works.
      var numeric = text.replace(/[^0-9.,\-]/g, "").replace(/,/g, "");
      if (numeric !== "" && !isNaN(Number(numeric))) return Number(numeric);
      var dash = text.match(/^(\d{4})-(\d{2})-(\d{2})$/);
      if (dash) return Date.UTC(+dash[1], +dash[2] - 1, +dash[3]);
      return text.toLowerCase();
    }

    headers.forEach(function (th, index) {
      th.setAttribute("tabindex", "0");
      th.setAttribute("role", "columnheader");

      function sort() {
        var current = th.getAttribute("aria-sort");
        var ascending = current === "descending";

        Array.from(tbody.rows).forEach(function (row) {
          row.dataset.sortValue = cellValue(row, index);
        });
        var rows = Array.from(tbody.rows);
        rows.sort(function (a, b) {
          var av = a.dataset.sortValue;
          var bv = b.dataset.sortValue;
          var cmp;
          if (typeof av === "number" && typeof bv === "number") cmp = av - bv;
          else cmp = String(av).localeCompare(String(bv));
          return ascending ? cmp : -cmp;
        });
        rows.forEach(function (row) {
          tbody.appendChild(row);
          delete row.dataset.sortValue;
        });

        headers.forEach(function (h) {
          h.setAttribute("aria-sort", "none");
        });
        th.setAttribute("aria-sort", ascending ? "ascending" : "descending");
      }

      th.addEventListener("click", sort);
      th.addEventListener("keydown", function (e) {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          sort();
        }
      });
    });
  }

  /* ---------- Background enrichment ----------
   *
   * Progress updates are written into the existing DOM. When the run finishes
   * the priced figures are patched into the current rows from /api/report.
   * There is deliberately NO location.reload(): a full reload discards scroll
   * position, sort order and filter state, and on a page holding hundreds of
   * rows it produces a visible white flash.
   */

  var enrichBtn = document.getElementById("enrich-btn");
  if (enrichBtn) {
    enrichBtn.addEventListener("click", function () {
      enrichBtn.disabled = true;
      enrichBtn.textContent = "Queued...";
      fetch("/enrich", { method: "POST" })
        .then(function () { poll(); })
        .catch(function () {
          enrichBtn.disabled = false;
          enrichBtn.textContent = "Price all trades";
        });
    });
  }

  // Mirrors valuation.ZERO_DECIMAL: currencies with no minor unit.
  var ZERO_DECIMAL = { IDR: 1, JPY: 1, KRW: 1, VND: 1, CLP: 1, ISK: 1,
                       HUF: 1, TWD: 1, COP: 1, UGX: 1, RWF: 1 };

  function fmtMoney(v, currency) {
    if (v === null || v === undefined || v === "") return "\u2014";
    var n = Number(v);
    if (!isFinite(n)) return "\u2014";
    var places = ZERO_DECIMAL[(currency || "IDR").toUpperCase()] ? 0 : 2;
    return n.toLocaleString("en-US", {
      minimumFractionDigits: places, maximumFractionDigits: places
    });
  }

  function fmtSignedIn(v, currency) {
    if (v === null || v === undefined || v === "") return "\u2014";
    var n = Number(v);
    if (!isFinite(n)) return "\u2014";
    var places = ZERO_DECIMAL[(currency || "IDR").toUpperCase()] ? 0 : 2;
    return (n > 0 ? "+" : "") + n.toLocaleString("en-US", {
      minimumFractionDigits: places, maximumFractionDigits: places
    });
  }

  function patchFigures() {
    // Reuse the filters currently in the URL so the JSON matches what is shown.
    return fetch("/api/report" + window.location.search, {
      headers: { Accept: "application/json" }
    })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (!data || !data.trades) return;
        var byUuid = {};
        data.trades.forEach(function (t) { byUuid[t.uuid] = t; });

        var updated = 0;
        Array.prototype.forEach.call(
          document.querySelectorAll("#trades-table tbody tr[data-uuid]"),
          function (row) {
            var t = byUuid[row.getAttribute("data-uuid")];
            if (!t) return;
            var usd = row.querySelector('[data-cell="usd"]');
            var mkt = row.querySelector('[data-cell="market"]');
            var pro = row.querySelector('[data-cell="profit"]');
            if (usd) usd.textContent = t.coin_in_usd ? fmtUsd(t.coin_in_usd) : "\u2014";
            if (mkt) mkt.textContent = fmtMoney(t.market_value_quote, t.quote_currency);
            if (pro) {
              pro.textContent = t.profit === null || t.profit === undefined
                ? "\u2014"
                : fmtSignedIn(t.profit, t.quote_currency);
              var p = Number(t.profit);
              pro.className = "num " + (p > 0 ? "pos" : p < 0 ? "neg" : "neutral");
            }
            if (t.market_value_quote) updated++;
          }
        );

        if (data.primary) {
          patchMetric("metric-market-value", fmtMoney(data.primary.market_value, data.primary.currency));
        }
        patchMetric("metric-priced-count", data.meta.enriched + " of " + data.meta.total + " trades priced");
        if (data.primary && data.primary.profit !== undefined) {
          patchMetric("metric-profit", fmtSignedIn(data.primary.profit, data.primary.currency));
        }

        // The banner is informational only; drop it once nothing is missing.
        var warn = document.querySelector(".notice--warn");
        if (warn && data.meta.enriched >= data.meta.total) warn.remove();

        var bar = document.querySelector(".progress");
        if (bar) bar.remove();
        var eb = document.getElementById("enrich-btn");
        if (eb) { eb.disabled = false; eb.textContent = "Refresh priced figures"; }
      })
      .catch(function () { /* leave the stale figures rather than blanking them */ });
  }

  function fmtUsd(v) {
    var n = Number(v);
    if (!isFinite(n)) return "-";
    return n >= 1000
      ? n.toLocaleString("en-US", { maximumFractionDigits: 0 })
      : n.toLocaleString("en-US", { maximumFractionDigits: 4 });
  }

  function patchMetric(id, value) {
    var el = document.getElementById(id);
    if (el && value !== null && value !== undefined) el.textContent = value;
  }

  var pollTimer = null;
  function poll() {
    if (pollTimer) clearTimeout(pollTimer);
    fetch("/api/enrichment")
      .then(function (r) { return r.json(); })
      .then(function (s) {
        var bar = document.querySelector(".progress-fill");
        var track = document.querySelector(".progress-track");
        var label = document.querySelector(".progress-head span");
        if (bar && s.pct !== undefined) {
          bar.style.width = s.pct + "%";
          if (track) track.setAttribute("aria-valuenow", String(s.pct));
        }
        if (label && s.total) {
          label.textContent = s.running
            ? "Fetching per-trade USD prices — " + s.done + " of " + s.total
            : "Per-trade USD prices: " + s.done + " of " + s.total + " cached";
        }
        var pct = document.querySelector(".progress-head span:last-child");
        if (pct && s.pct !== undefined) pct.textContent = s.pct + "%";

        if (s.running) {
          pollTimer = setTimeout(poll, 4000);
        } else {
          patchFigures();
        }
      })
      .catch(function () {
        pollTimer = setTimeout(poll, 10000);
      });
  }

  // Only start polling if a run is actually in flight.
  fetch("/api/enrichment")
    .then(function (r) { return r.json(); })
    .then(function (s) {
      if (s.running) poll();
    })
    .catch(function () {});

  /* ---------- Live Trade Updates & Freshness Ticker ----------
   *
   * Periodically checks /health to keep the masthead freshness timer accurate
   * ("Trades last checked 12s ago", "next check in 45s").
   * If new trades arrive or trade count/statuses change in the background,
   * automatically reloads the page while preserving scroll position seamlessly.
   */
  var initialTradesCount = null;
  var initialLastChange = null;

  function fmtFreshness(age) {
    if (age === null || age === undefined) return "never";
    var seconds = Math.floor(age);
    if (seconds < 90) return seconds + "s ago";
    var minutes = Math.floor(seconds / 60);
    if (minutes < 90) return minutes + "m ago";
    var hours = Math.floor(minutes / 60);
    if (hours < 48) return hours + "h ago";
    return Math.floor(hours / 24) + "d ago";
  }

  function fmtPollLabel(refresher) {
    if (!refresher || !refresher.running) return "automatic checks are off";
    if (refresher.next_interval === null || refresher.last_check === null) return "next check soon";
    var due = (refresher.last_check + refresher.next_interval) - (Date.now() / 1000);
    var dueSec = Math.round(Math.max(0, due));
    if (dueSec < 5) return "next check now";
    if (dueSec < 90) return "next check in " + dueSec + "s";
    return "next check in " + Math.round(dueSec / 60) + "m";
  }

  function checkHealth() {
    fetch("/health")
      .then(function (r) { return r.json(); })
      .then(function (h) {
        if (!h) return;
        var freshEl = document.getElementById("freshness-val");
        var pollEl = document.getElementById("poll-label-val");
        var totalEl = document.getElementById("total-trades-val");

        if (freshEl && h.trades_age_seconds !== undefined) {
          freshEl.textContent = fmtFreshness(h.trades_age_seconds);
        }
        if (pollEl && h.refresher) {
          pollEl.textContent = fmtPollLabel(h.refresher);
        }
        if (totalEl && h.trades_cached !== undefined) {
          totalEl.textContent = h.trades_cached;
        }

        // Detect if trades have changed or new trades arrived
        if (h.trades_cached !== undefined) {
          if (initialTradesCount === null) {
            initialTradesCount = h.trades_cached;
          } else if (h.trades_cached !== initialTradesCount) {
            // New trades arrived! Reload page seamlessly (scroll position is preserved).
            window.location.reload();
            return;
          }
        }

        if (h.refresher && h.refresher.last_change !== undefined) {
          if (initialLastChange === null) {
            initialLastChange = h.refresher.last_change;
          } else if (h.refresher.last_change && h.refresher.last_change !== initialLastChange) {
            // Background sync detected trade update (status or detail changed)
            window.location.reload();
            return;
          }
        }
      })
      .catch(function () {});
  }

  // Poll health every 15 seconds to keep masthead live and detect new trades
  setInterval(checkHealth, 15000);
  // Also run an immediate check after initial page settle
  setTimeout(checkHealth, 2000);
})();