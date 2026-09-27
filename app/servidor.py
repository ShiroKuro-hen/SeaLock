"""SeaLock local web server.

Serves the UI at http://127.0.0.1:8765 and exposes the same functions used by the
MCP servers as a JSON API. No logic duplication: modules are imported and their
functions called directly.

Security (important even though it's local):
  - Listens ONLY on 127.0.0.1. Not accessible from the network.
  - Every /api call requires a random token generated at startup, injected into
    the page when served. A malicious page open in another tab can't read it or
    send custom headers without CORS, so it can't manipulate your data (CSRF
    against localhost).
  - Any request with a non-local Host or Origin is rejected (DNS rebinding).
"""

import concurrent.futures
import json
import os
import secrets
import socket
import sys
import threading
import time
import webbrowser
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "mcp"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import finanzas_server as fin  # noqa: E402
import indicators  # noqa: E402
import llm  # noqa: E402
import mercado_server as mer  # noqa: E402
import metrics  # noqa: E402
import scoring  # noqa: E402
import symbols  # noqa: E402

RAW_CACHE_TTL = 6 * 3600  # seconds; quote/fundamentals/historical/dcf/graham/quarterly per ticker.
# ponytail: el enfriamiento es una constante; el env existe solo para poder probarlo.
FORCED_REFRESH_COOLDOWN = float(os.environ.get("SEALOCK_REFRESH_COOLDOWN", 60))
# Scoring ~110 universe tickers is network-heavy; caching the raw inputs (not the score)
# means a profile change only re-runs scoring.score_stock (pure, cheap) instead of
# refetching Yahoo. Also backs portfolio_scores, replacing its old score-only cache.
UNIVERSE_PATH = os.path.join(ROOT, "recursos", "universo.json")

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
PORT = int(os.environ.get("SEALOCK_PORT", "8765"))
TOKEN = secrets.token_urlsafe(24)
CURRENCY = fin.BASE_CURRENCY

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}

API = {
    "finance": fin.srv.tools(),
    "market": mer.srv.tools(),
}


# ---------------------------------------------------------------- app endpoints
# Combine several tools into a single call, so the UI doesn't have to orchestrate
# half a dozen requests.


def _rates_to_base(currencies):
    """Exchange rates to base currency, tolerating network failures."""
    rates = {CURRENCY: 1.0}
    for c in currencies:
        if not c or c in rates:
            continue
        try:
            rates[c] = mer.exchange_rate(from_currency=c, to_currency=CURRENCY)["rate"] or 1.0
        except Exception:  # noqa: BLE001
            rates[c] = None
    return rates


def portfolio_valued():
    """Open positions with market price, latent P&L and portfolio weight."""
    portfolio = fin.portfolio_view()
    open_positions = [p for p in portfolio["positions"] if p["quantity"] > 0]
    if not open_positions:
        return {"positions": [], "totals": {}, "currency": CURRENCY}

    quotes, failures = {}, []
    try:
        data = mer.quote(tickers=[p["ticker"] for p in open_positions])
        quotes = {q["ticker"]: q for q in data["quotes"]}
        failures = [f["ticker"] for f in data.get("failures", [])]
    except Exception as exc:  # noqa: BLE001
        failures = [p["ticker"] for p in open_positions]
        mer.log(f"[app] quotes unavailable: {exc}")

    currencies = {q.get("currency") for q in quotes.values()}
    currencies |= set(portfolio.get("realized_by_currency", {}))
    currencies |= {p["currency"] for p in open_positions}
    rates = _rates_to_base(currencies)

    # Realized gains also need conversion: summing dollars and euros without an
    # exchange rate produces a meaningless total.
    realized_base, realized_unconverted = 0.0, []
    for currency, amount in (portfolio.get("realized_by_currency") or {}).items():
        rate = rates.get(currency)
        if rate is None:
            realized_unconverted.append(currency)
        else:
            realized_base += amount * rate

    rows, total_value, total_cost, mismatches = [], 0.0, 0.0, []
    for p in open_positions:
        q = quotes.get(p["ticker"]) or {}
        price = q.get("price")

        # The cost currency (the one recorded at purchase) may not match the quote
        # currency. Each needs its own exchange rate: applying the quote rate to the
        # cost falsifies the gain.
        quote_currency = q.get("currency") or p["currency"]
        cost_currency = p["currency"]
        quote_rate, cost_rate = rates.get(quote_currency), rates.get(cost_currency)
        if quote_currency != cost_currency:
            mismatches.append({
                "ticker": p["ticker"], "cost_currency": cost_currency, "quote_currency": quote_currency,
            })

        value = round(p["quantity"] * price, 2) if price else None
        value_base = round(value * quote_rate, 2) if (value and quote_rate) else None
        cost_base = round(p["total_cost"] * cost_rate, 2) if cost_rate else None
        comparable = value_base is not None and cost_base is not None

        if comparable:
            total_value += value_base
            total_cost += cost_base

        rows.append({
            "ticker": p["ticker"],
            "name": q.get("name"),
            "quantity": p["quantity"],
            "average_cost": p["average_cost"],
            "total_cost": p["total_cost"],
            "cost_currency": cost_currency,
            "price": price,
            "currency": quote_currency,
            "day_change_pct": q.get("change_pct"),
            "price_as_of": q.get("price_as_of"),
            "age_seconds": q.get("age_seconds"),
            "market_status": q.get("status"),
            "market_value": value,
            "base_value": value_base,
            # Gain is calculated in base currency, the only way to subtract without
            # error when cost and quote are in different currencies.
            "gain": round(value_base - cost_base, 2) if comparable else None,
            "gain_pct": (
                round(100 * (value_base - cost_base) / cost_base, 2)
                if comparable and cost_base else None
            ),
            "realized": p["realized_gain"],
            "unvalued": price is None,
        })

    for r in rows:
        r["weight_pct"] = (
            round(100 * r["base_value"] / total_value, 1)
            if r["base_value"] and total_value else None
        )

    rows.sort(key=lambda r: -(r["base_value"] or 0))

    # The UI uses this to decide whether it's worth re-fetching prices in the
    # auto-refresh: with all markets closed, the price won't change.
    any_open = any(r["market_status"] in ("OPEN", "PRE_MARKET", "POST_MARKET") for r in rows)

    return {
        "any_market_open": any_open,
        "positions": rows,
        "totals": {
            "market_value": round(total_value, 2),
            "cost": round(total_cost, 2),
            "unrealized_gain": round(total_value - total_cost, 2),
            "unrealized_gain_pct": (
                round(100 * (total_value - total_cost) / total_cost, 2) if total_cost else None
            ),
            "accumulated_realized": round(realized_base, 2),
            "realized_by_currency": portfolio.get("realized_by_currency"),
        },
        "currency": CURRENCY,
        "no_price": failures,
        "not_converted": realized_unconverted,
        "currency_mismatch": mismatches,
    }


def _rate_at(rate, date):
    """FX rate applying on `date`: a scalar, or the last one at-or-before `date`.
    Never a later one — that would be look-ahead (HU-26)."""
    if rate is None:
        return 1.0
    if isinstance(rate, dict):
        usable = [d for d in rate if d <= date]
        return rate[max(usable)] if usable else 1.0
    return float(rate)


def portfolio_history(trades, closes_by_ticker, to_base=None):
    """Point-in-time (date, value, cost) of the portfolio (HU-26, chart of HU-29). Pure:
    the caller supplies prices.

    At date D only the positions already bought by D are counted, valued at D's close.
    The cost side is the average-cost basis of *those same shares* (same rule as
    `finanzas.portfolio_view`: a buy adds price+commission, a sell removes shares at the
    average cost held, never at the sale price), so `value - cost` is a gain built only
    from information available at D. `to_base[ticker]` is a scalar rate, a {date: rate}
    series, or absent (= already in base); the same per-ticker rate prices both sides, and
    a cost/quote currency mismatch is exactly what `portfolio_valued` reports as
    `currency_mismatch`. A date where nothing prices is dropped rather than forward-filled:
    filling would invent a value that was never quoted.
    """
    to_base = to_base or {}
    events = {}
    for t in sorted(trades, key=lambda x: (x["date"], x.get("id") or 0)):
        events.setdefault(t["date"], []).append(t)

    dates = sorted({d for series in closes_by_ticker.values() for d in series})
    held, out, queue = {}, [], sorted(events)
    for d in dates:
        while queue and queue[0] <= d:
            for t in events[queue.pop(0)]:
                qty = float(t["quantity"])
                state = held.setdefault(t["ticker"], [0.0, 0.0])  # [shares, cost basis]
                if t["action"] == "buy":
                    state[0] += qty
                    # price/commission only feed the cost side; a value-only caller
                    # (and the pure tests) can pass trades without them.
                    state[1] += qty * float(t.get("price") or 0) + float(t.get("commission") or 0)
                else:
                    average = state[1] / state[0] if state[0] else 0.0
                    state[1] -= average * qty
                    state[0] -= qty
        total, cost, priced = 0.0, 0.0, False
        for ticker, (qty, basis) in held.items():
            if abs(qty) < 1e-9:
                continue
            close = (closes_by_ticker.get(ticker) or {}).get(d)
            if close is None:
                continue
            rate = _rate_at(to_base.get(ticker), d)
            total += qty * close * rate
            cost += basis * rate
            priced = True
        if priced:
            out.append((d, total, cost))
    return out


def value_series(trades, closes_by_ticker, to_base=None):
    """Daily portfolio value, point-in-time (HU-26): the value side of portfolio_history,
    which is where both the metrics (HU-24) and the chart (HU-29) read from."""
    return [(d, value) for d, value, _ in portfolio_history(trades, closes_by_ticker, to_base)]


def returns_of(values):
    """Simple period returns of a (date, value) series: v[i] / v[i-1] - 1."""
    out = []
    for i in range(1, len(values)):
        prev = values[i - 1][1]
        if prev:
            out.append(values[i][1] / prev - 1.0)
    return out


_series_cache = {}


def _closes(ticker, date_range="5y"):
    """Daily closes {date: price}, cached 6h like _raw_inputs: building a portfolio
    history is network-heavy and the UI must not hammer Yahoo for it. One bad ticker
    comes back empty instead of failing the whole series.

    `5y` and not `max`: Yahoo downsamples `max` to quarterly points, which is useless
    as a daily value series. 5 years is what Sharpe/Sortino/Calmar are read over."""
    key = (ticker, date_range)
    now = time.time()
    hit = _series_cache.get(key)
    if hit and now - hit[0] < RAW_CACHE_TTL:
        return hit[1]
    try:
        h = mer.historical(ticker=ticker, date_range=date_range, interval="1d", include_series=True)
        rows = h.get("closes") or h.get("recent_closes") or []
        series = {r["date"]: r["close"] for r in rows}
    except Exception as exc:  # noqa: BLE001
        series = {}
        mer.log(f"[app] closes unavailable for {ticker}: {exc}")
    _series_cache[key] = (now, series)
    return series


def _to_base(currency):
    """{date: rate} turning `currency` into base, or 1.0 when already base. Empty dict
    when the pair can't be fetched: the caller must say so, never assume parity."""
    if not currency or currency == CURRENCY:
        return 1.0
    pair = _closes(f"{CURRENCY}{currency}=X")
    return {d: 1.0 / v for d, v in pair.items() if v} or None


def portfolio_metrics(benchmark="^IBEX"):
    """Sharpe/Sortino/Calmar of the portfolio against a benchmark (HU-24), plus the raw
    `series` the Cartera chart draws (HU-29).

    The value series is point-in-time (HU-26): each date's close over the positions
    already held by that date. Currencies that can't be converted are excluded and
    reported, exactly like portfolio_valued — never summed as if they were equal.
    """
    trades = fin.sql_query("SELECT * FROM trades ORDER BY date, id", 100000)["rows"]
    tickers = sorted({t["ticker"] for t in trades})
    if not tickers:
        return {"portfolio": None, "benchmark": None, "series": [],
                "note": "Sin operaciones registradas."}

    currencies = {}
    try:
        for q in mer.quote(tickers=tickers)["quotes"]:
            currencies[q["ticker"]] = q.get("currency") or CURRENCY
    except Exception as exc:  # noqa: BLE001 - fall back to the trade currency
        mer.log(f"[app] quotes for currency unknown: {exc}")
    for t in trades:
        currencies.setdefault(t["ticker"], t.get("currency") or CURRENCY)

    to_base, not_converted, closes = {}, [], {}
    for ticker in tickers:
        currency = currencies.get(ticker) or CURRENCY
        rate = _to_base(currency)
        if rate is None:
            not_converted.append({"ticker": ticker, "currency": currency})
            continue
        series = _closes(ticker)
        if series:
            closes[ticker] = series
            to_base[ticker] = rate

    used = [t for t in trades if t["ticker"] in closes]
    if not used:
        return {
            "portfolio": None, "benchmark": None, "series": [], "not_converted": not_converted,
            "note": "Sin precios historicos para ninguna posicion: no hay serie que medir.",
        }

    history = portfolio_history(used, closes, to_base)
    if not history:
        return {
            "portfolio": None, "benchmark": None, "series": [], "not_converted": not_converted,
            "note": "Ninguna fecha con precio posterior a la primera operacion: no hay serie.",
        }
    values = [(d, v) for d, v, _ in history]
    returns = returns_of(values)
    # Annualize from the series' own spacing, never from an assumed 252: Yahoo
    # downsamples long ranges and a "daily" request can come back quarterly.
    ppy = metrics.periods_per_year_from_dates([d for d, _ in values]) or 252

    bench = None
    benchmark_returns = []
    try:
        bench_series = _closes(benchmark)
        if bench_series and values:
            lo, hi = values[0][0], values[-1][0]
            window = [(d, c) for d, c in sorted(bench_series.items()) if lo <= d <= hi]
            benchmark_returns = returns_of(window)
            bench = {
                "ticker": benchmark,
                "date_from": window[0][0] if window else None,
                "date_to": window[-1][0] if window else None,
                **metrics.summary(benchmark_returns, ppy),
            }
    except Exception as exc:  # noqa: BLE001 - a missing benchmark must not hide ours
        mer.log(f"[app] benchmark {benchmark} unavailable: {exc}")

    return {
        "portfolio": {
            "date_from": values[0][0],
            "date_to": values[-1][0],
            "observations": len(returns),
            "start_value": round(values[0][1], 2),
            "end_value": round(values[-1][1], 2),
            **metrics.summary(returns, ppy),
        },
        "benchmark": bench,
        # The raw curve behind the metrics, for the chart (HU-29): one point per date
        # with a price, value and cost of exactly the same shares.
        "series": [
            {"date": d, "value": round(v, 2), "cost": round(c, 2), "gain": round(v - c, 2)}
            for d, v, c in history
        ],
        "currency": CURRENCY,
        "not_converted": not_converted,
        "note": (
            "Metricas sobre una serie diaria de valor de cartera, construida con la "
            "posicion de cada fecha (HU-26). No son asesoramiento."
        ),
    }


def _position_snapshot(ticker, holding, quote_data):
    """Quantity/cost/current value for a single held ticker, using the quote already
    fetched for the ficha. No cross-currency conversion here (that full treatment
    lives in portfolio_valued): a single-stock detail assumes cost and quote share
    a currency, which holds for the common case.
    # ponytail: no FX conversion; extend like portfolio_valued's _rates_to_base if
    # a user reports a mismatched-currency position looking wrong here.
    """
    if not holding:
        return None
    price = quote_data.get("price") if isinstance(quote_data, dict) else None
    qty = holding.get("quantity")
    cost_total = holding.get("total_cost")
    value = round(qty * price, 2) if price else None
    gain = round(value - cost_total, 2) if value is not None and cost_total is not None else None
    gain_pct = round(100 * gain / cost_total, 2) if gain is not None and cost_total else None
    return {
        "quantity": qty,
        "average_cost": holding.get("average_cost"),
        "invested": cost_total,
        "currency": holding.get("currency"),
        "current_value": value,
        "gain": gain,
        "gain_pct": gain_pct,
    }


def stock_detail(ticker, growth_pct=8, discount_rate_pct=9, terminal_growth_pct=2.5,
                 latest_quarter_end=None, refresh=False):
    """Quote + fundamentals + history + valuation + score for a single security. News
    lives apart in stock_news() so the ficha doesn't wait for the RSS feeds.

    `refresh=true` forces a refetch of the cached inputs (at most one per minute).
    `latest_quarter_end` (AAAA-MM-DD) is the newest quarterly close the caller knows about;
    it is what tells whether Claude's review is still up to date.
    """
    tkr = ticker.upper().strip()
    output = {"ticker": tkr}

    def try_section(key, fn):
        try:
            output[key] = fn()
        except Exception as exc:  # noqa: BLE001
            output[key] = {"error": f"{type(exc).__name__}: {exc}"}

    # Price, fundamentals and the score's inputs come from the 6h cache so the 60s
    # auto-refresh doesn't hit Yahoo on every tick; "Actualizar nota" (refresh=true)
    # forces a refetch at most once a minute. The DCF and Graham, which follow the
    # assumptions or are cheap, stay per call.
    raw = _raw_inputs(ticker, refresh=refresh)
    for key in ("quote", "fundamentals", "historical", "quarterly", "estimates"):
        section = raw.get(key)
        if key == "quote" and not section and raw.get("quote_error"):
            section = {"error": raw["quote_error"]}
        output[key] = section or {}
    output["score_age_seconds"] = _inputs_age(ticker)

    try_section("dcf", lambda: mer.dcf_valuation(
        ticker=ticker,
        growth_pct=growth_pct,
        discount_rate_pct=discount_rate_pct,
        terminal_growth_pct=terminal_growth_pct,
    ))
    try_section("graham", lambda: mer.graham_valuation(ticker=ticker))

    def clean(key):
        section = output.get(key)
        return None if not isinstance(section, dict) or "error" in section else section

    holding = None
    try:
        portfolio = fin.portfolio_view()
        holding = next((p for p in portfolio["positions"] if p["ticker"] == output["ticker"]), None)
    except Exception:  # noqa: BLE001 - score just runs without holding context
        holding = None

    output["position"] = _position_snapshot(output["ticker"], holding, clean("quote") or {})
    # The newest quarter close is already in this same response, so a review that saw an
    # older one is flagged stale here instead of waiting for the caller to say so.
    quarters = (clean("quarterly") or {}).get("quarters") or []
    output["score"] = _with_review(output["ticker"], scoring.score_stock(
        clean("quote") or {}, clean("fundamentals") or {}, clean("historical") or {},
        dcf=clean("dcf"), graham=clean("graham"), profile=fin.profile_get(), holding=holding,
        quarterly=clean("quarterly") or {}, estimates=clean("estimates") or {},
    ), latest_quarter_end or (quarters[0].get("period_end") if quarters else None))
    return output


NEWS_CACHE_TTL = 30 * 60  # seconds
_news_cache = {}  # ticker -> (timestamp, stock_news() result)


def stock_news(ticker):
    """Latest news for the company and for its industry/sector, cached 30 min per ticker.
    Sector headlines already in the company list are dropped. The sector query comes from
    the cached fundamentals, so this adds no Yahoo call when the ficha is already open."""
    tkr = ticker.upper().strip()
    hit = _news_cache.get(tkr)
    if hit and time.time() - hit[0] < NEWS_CACHE_TTL:
        return hit[1]

    f = _raw_inputs(tkr).get("fundamentals") or {}
    sector_query = " ".join(x for x in (f.get("industry"), f.get("sector")) if x) or None
    seen = set()
    company = mer.news_dedupe(mer.news(ticker=tkr, limit=15)["news"], seen)
    sector = (mer.news_dedupe(mer.news(query=sector_query, limit=15)["news"], seen)
              if sector_query else [])
    result = {
        "ticker": tkr,
        "company": mer.news_sorted(company),
        "sector": mer.news_sorted(sector),
        "sector_query": sector_query,
    }
    # An empty result is usually a network failure: don't pin it for 30 min.
    if company or sector:
        _news_cache[tkr] = (time.time(), result)
    return result


def _with_news(detail):
    """stock_detail plus company headlines, for the Claude context and the copied prompt."""
    try:
        return {**detail, "news": stock_news(detail["ticker"])["company"]}
    except Exception:  # noqa: BLE001 - the context just goes without news
        return detail


_raw_cache = {}  # ticker -> (timestamp, {"quote", "fundamentals", "historical", "dcf", "graham", "quarterly"})


def _fetch_raw(ticker):
    """Quote/fundamentals/historical/dcf/graham for one ticker, no scoring. Network-heavy:
    go through _raw_inputs() for the 6h cache instead of calling this directly."""
    raw = {}
    try:
        q = mer.quote(tickers=[ticker])["quotes"]
        raw["quote"] = q[0] if q else {}
    except Exception as exc:  # noqa: BLE001
        raw["quote"] = {}
        raw["quote_error"] = f"{type(exc).__name__}: {exc}"
    try:
        raw["fundamentals"] = mer.fundamentals(ticker=ticker)
    except Exception as exc:  # noqa: BLE001
        raw["fundamentals"] = {"error": f"{type(exc).__name__}: {exc}"}
    try:
        # include_series: the ficha draws a 1-year sparkline from the same call.
        raw["historical"] = mer.historical(ticker=ticker, date_range="1y", include_series=True)
    except Exception:  # noqa: BLE001 - momentum pillar just skips
        raw["historical"] = {}
    try:
        raw["dcf"] = mer.dcf_valuation(ticker=ticker)
    except Exception:  # noqa: BLE001 - optional input
        raw["dcf"] = None
    try:
        raw["graham"] = mer.graham_valuation(ticker=ticker)
    except Exception:  # noqa: BLE001 - optional input
        raw["graham"] = None
    try:
        raw["quarterly"] = mer.quarterly(ticker=ticker)
    except Exception:  # noqa: BLE001 - recent results, just skipped
        raw["quarterly"] = {}
    try:
        raw["estimates"] = mer.earnings_estimates(ticker=ticker)
    except Exception:  # noqa: BLE001 - analyst projections, just skipped
        raw["estimates"] = {}
    return raw


def _raw_inputs(ticker, refresh=False):
    """Raw market data for a ticker, cached 6h in-process. Reused across profile changes
    and across the Cartera/Recomendaciones tabs so they don't refetch the same ticker.

    `refresh` drops this ticker's entry and refetches, but at most one forced refresh per
    FORCED_REFRESH_COOLDOWN seconds: the button must not turn into a Yahoo hammer.
    """
    now = time.time()
    cached = _raw_cache.get(ticker)
    if cached:
        if refresh:
            if now - cached[0] < FORCED_REFRESH_COOLDOWN:
                return cached[1]  # too soon: serve the cache and report its age
        elif now - cached[0] < RAW_CACHE_TTL:
            return cached[1]
    raw = _fetch_raw(ticker)
    _raw_cache[ticker] = (now, raw)
    return raw


def _inputs_age(ticker):
    cached = _raw_cache.get(ticker)
    return int(time.time() - cached[0]) if cached else None


def _review_for(ticker, latest_quarter_end=None):
    """Claude's review for a ticker, or None. Finance is local so this is a fast DB hit."""
    try:
        return fin.review_get(ticker, latest_quarter_end=latest_quarter_end)["review"]
    except Exception:  # noqa: BLE001 - a missing review must never break scoring
        return None


def _with_review(ticker, score_result, latest_quarter_end=None):
    """Merge mechanical score and Claude's adjustment into one flat result:
    score (mecanica), final_score, review, label and sentiment already on final_score."""
    return {**score_result, **scoring.apply_review(score_result, _review_for(ticker, latest_quarter_end))}


def _score_ticker(ticker, profile, holding=None):
    """Score one ticker for a profile, from cached raw inputs. No network of its own:
    scoring.score_stock is a pure function, so this is cheap to call per profile change."""
    raw = _raw_inputs(ticker)
    fundamentals = raw.get("fundamentals") or {}
    ok_fundamentals = not fundamentals.get("error")
    result = scoring.score_stock(
        raw.get("quote") or {}, fundamentals, raw.get("historical") or {},
        dcf=raw.get("dcf"), graham=raw.get("graham"), profile=profile, holding=holding,
        quarterly=raw.get("quarterly") or {}, estimates=raw.get("estimates") or {},
    )
    result["ticker"] = ticker
    result["sector"] = fundamentals.get("sector") if ok_fundamentals else None
    result["name"] = (fundamentals.get("name") if ok_fundamentals else None) or (raw.get("quote") or {}).get("name")
    return _with_review(ticker, result)


def _score_many(tickers, profile, holdings=None):
    """Score several tickers concurrently (network-bound, so threads help). Per-ticker
    failures never fail the batch: they come back as {"ticker", "error"}."""
    holdings = holdings or {}
    tickers = list(dict.fromkeys(tickers))  # de-dup, keep order
    if not tickers:
        return {}

    def run(ticker):
        try:
            return ticker, _score_ticker(ticker, profile, holding=holdings.get(ticker))
        except Exception as exc:  # noqa: BLE001 - surfaced per-ticker, not fatal
            return ticker, {"ticker": ticker, "error": f"{type(exc).__name__}: {exc}"}

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(6, len(tickers))) as ex:
        return dict(ex.map(run, tickers))


def portfolio_scores():
    """One score per held ticker. Thin caller over the shared 6h raw-input cache, scored
    concurrently, so the Cartera tab's periodic refresh doesn't hammer Yahoo."""
    portfolio = fin.portfolio_view()
    profile = fin.profile_get()
    holdings = {p["ticker"]: p for p in portfolio["positions"]}
    return {"scores": _score_many(list(holdings), profile, holdings)}


_universe_cache = None


def _load_universe():
    global _universe_cache
    if _universe_cache is None:
        with open(UNIVERSE_PATH, encoding="utf-8") as fh:
            _universe_cache = json.load(fh)
    return _universe_cache


def recommendation_universe():
    """Candidate list for Recomendaciones: universe blue chips + watchlist + held tickers,
    deduped. No network — the UI scores them separately via stock_scores, in batches."""
    universe = _load_universe()
    candidates = {s["ticker"]: {"ticker": s["ticker"], "name": s["name"], "indices": s["indices"]}
                  for s in universe["stocks"]}

    try:
        held = sorted({p["ticker"] for p in fin.portfolio_view()["positions"]})
    except Exception:  # noqa: BLE001 - the tab still works, just without held-aware dedup
        held = []
    try:
        watchlist = fin.watchlist_list()["watchlist"]
    except Exception:  # noqa: BLE001
        watchlist = []

    for ticker in held:
        candidates.setdefault(ticker, {"ticker": ticker, "name": None, "indices": []})
    for w in watchlist:
        entry = candidates.setdefault(w["ticker"], {"ticker": w["ticker"], "name": None, "indices": []})
        entry["name"] = entry["name"] or w.get("name")

    return {
        "candidates": sorted(candidates.values(), key=lambda c: c["ticker"]),
        "held": held,
        "profile": fin.profile_get(),
        "source": universe.get("source"),
        "as_of": universe.get("as_of"),
    }


def stock_scores(tickers):
    """Score up to 8 tickers concurrently, for the Recomendaciones batched loader."""
    tickers = list(dict.fromkeys(t.upper().strip() for t in (tickers or []) if t))[:8]
    if not tickers:
        return {"scores": {}}
    profile = fin.profile_get()
    try:
        holdings = {p["ticker"]: p for p in fin.portfolio_view()["positions"]}
    except Exception:  # noqa: BLE001 - scoring still works without holding context
        holdings = {}
    return {"scores": _score_many(tickers, profile, holdings)}


def recommendation_positions():
    """Advice for every held position: score + portfolio weight/P&L + position_advice
    rules. Small enough (a real portfolio has few positions) to score in one call,
    unlike the ~110-ticker universe which the UI batches through stock_scores."""
    valued = portfolio_valued()
    profile = fin.profile_get()
    holdings_by_ticker = {r["ticker"]: r for r in valued["positions"]}
    scores = _score_many(list(holdings_by_ticker), profile, holdings_by_ticker)

    out = []
    for ticker, row in holdings_by_ticker.items():
        score_result = scores.get(ticker) or {}
        advice = scoring.position_advice(score_result, row.get("weight_pct"), row.get("gain_pct"), profile)
        out.append({
            "ticker": ticker,
            "name": row.get("name") or score_result.get("name"),
            "score": score_result,
            "weight_pct": row.get("weight_pct"),
            "gain_pct": row.get("gain_pct"),
            "advice": advice,
        })
    out.sort(key=lambda r: -(r["weight_pct"] or 0))
    return {"positions": out, "profile": profile}


def stock_compare(tickers):
    """Side-by-side comparison of 2-4 tickers: fundamentals, pillar subscores, latest
    quarter and final score. Reuses the same cached inputs and scoring as everywhere
    else; a per-ticker failure never fails the whole batch."""
    if isinstance(tickers, str):
        tickers = tickers.split(",")
    tickers = list(dict.fromkeys(t.upper().strip() for t in (tickers or []) if t and t.strip()))
    if len(tickers) < 2:
        raise ValueError("Compare needs at least 2 tickers")
    if len(tickers) > 4:
        raise ValueError("Compare accepts at most 4 tickers")

    profile = fin.profile_get()
    try:
        holdings = {p["ticker"]: p for p in fin.portfolio_view()["positions"]}
    except Exception:  # noqa: BLE001 - comparison still works without holding context
        holdings = {}
    scores = _score_many(tickers, profile, holdings)

    rows = []
    for ticker in tickers:
        s = scores.get(ticker) or {}
        if s.get("error"):
            rows.append({"ticker": ticker, "error": s["error"]})
            continue
        raw = _raw_inputs(ticker)
        fundamentals = raw.get("fundamentals") or {}
        valuation = fundamentals.get("valuation") or {}
        profitability = fundamentals.get("profitability") or {}
        growth = fundamentals.get("growth") or {}
        quarters = (raw.get("quarterly") or {}).get("quarters") or []
        latest_q = quarters[0] if quarters else {}
        rows.append({
            "ticker": ticker,
            "name": s.get("name") or fundamentals.get("name"),
            "score": s.get("score"),
            "final_score": s.get("final_score"),
            "label": s.get("label"),
            "sentiment": s.get("sentiment"),
            "pillars": s.get("pillars"),
            "price": (raw.get("quote") or {}).get("price"),
            "per_trailing": valuation.get("per_trailing"),
            "ev_ebitda": valuation.get("ev_ebitda"),
            "roe_pct": profitability.get("roe_pct"),
            "net_margin_pct": profitability.get("net_margin_pct"),
            "revenue_growth_pct": growth.get("revenue_yoy_pct"),
            "dividend_yield_pct": (fundamentals.get("dividend") or {}).get("yield_pct"),
            "quarter_revenue_yoy_pct": latest_q.get("revenue_yoy_pct"),
            "quarter_net_margin_pct": latest_q.get("net_margin_pct"),
        })
    return {"rows": rows}



# --------------------------------------------------------------------- hidden gems
#
# Candidate universe (HU-11), background scanner + cache (HU-12), UI reads the results
# via finance.gems_list (mcp/finanzas_server.py), which applies scoring.select_gems and
# any Claude review with no network of its own.

# Exchange -> region, from the exchanges already present in recursos/simbolos/equities.csv
# (see docs/plan.md HU-14). An exchange outside this table is not a gem candidate.
EXCHANGE_REGION = {
    "NYQ": "Norteamerica", "NMS": "Norteamerica", "NGM": "Norteamerica", "NCM": "Norteamerica",
    "ASE": "Norteamerica", "TOR": "Norteamerica",
    "LSE": "Europa", "MCE": "Europa", "PAR": "Europa", "AMS": "Europa", "MIL": "Europa",
    "GER": "Europa", "EBS": "Europa", "LIS": "Europa", "BRU": "Europa", "STO": "Europa",
    "CPH": "Europa", "HEL": "Europa", "OSL": "Europa", "VIE": "Europa", "ISE": "Europa",
    "JPX": "Asia-Pacifico", "HKG": "Asia-Pacifico", "KSC": "Asia-Pacifico", "TAI": "Asia-Pacifico",
    "NSE": "Asia-Pacifico", "NSI": "Asia-Pacifico", "ASX": "Asia-Pacifico", "SES": "Asia-Pacifico",
    "SAO": "Latinoamerica", "MEX": "Latinoamerica",
    "JNB": "Africa",
}

# HU-11: candidate market_cap categories from the catalog (Small/Mid Cap only).
GEM_CAP_CATEGORIES = ("Small Cap", "Mid Cap")
COVERAGE_PROBE_MIN_SAMPLE = 5  # a market needs at least this many scored entries to judge
MAX_SCAN_CANDIDATES = 300
SCAN_STALE_DAYS = 7  # HU-12: a cache entry is skipped for a re-scan within this window
SCAN_WORKERS = 4
SCAN_BATCH = 8
SCAN_BACKOFF_SECONDS = (5, 15, 45)  # growing wait after a fully-failed batch
SCAN_BATCH_PAUSE = 1.0  # short pause between successful batches, to be gentle with Yahoo


def _excluded_markets():
    """Coverage probe (HU-11 simplification, no separate probe table/30-day job): once
    an exchange has >= COVERAGE_PROBE_MIN_SAMPLE scored entries in score_cache, if fewer
    than half reach coverage_pct >= 50 the exchange is excluded from further scans. This
    refreshes naturally as the cache rotates (old entries get rescored over time)."""
    by_exchange = {}
    for r in fin.score_cache_all():
        row = symbols.lookup(r["ticker"])
        if not row:
            continue
        by_exchange.setdefault(row["exchange"], []).append(r.get("coverage_pct") or 0)

    excluded = set()
    for exch, coverages in by_exchange.items():
        if len(coverages) < COVERAGE_PROBE_MIN_SAMPLE:
            continue
        good = sum(1 for c in coverages if c >= 50)
        if good < len(coverages) / 2:
            excluded.add(exch)
    return excluded


def gems_universe():
    """Candidate list for Hidden Gems (HU-11): catalog rows that are home listings, not
    OTC, Small/Mid Cap, outside the 111 blue-chip universo.json. No network — the real
    scan happens separately via gems_scan_start. Held tickers are flagged, not
    suggested (already visible in Cartera/Recomendaciones)."""
    blue_chips = {s["ticker"] for s in _load_universe()["stocks"]}
    try:
        held = {p["ticker"] for p in fin.portfolio_view()["positions"]}
    except Exception:  # noqa: BLE001 - the tab still works without held-aware flags
        held = set()

    excluded_markets = _excluded_markets()

    candidates = []
    for row in symbols.rows():
        if row.get("market_cap") not in GEM_CAP_CATEGORIES:
            continue
        if symbols.is_otc(row) or not symbols.is_home(row):
            continue
        if row["symbol"] in blue_chips:
            continue
        if row["exchange"] in excluded_markets:
            continue
        region = EXCHANGE_REGION.get(row["exchange"])
        if not region:
            continue
        candidates.append({
            "ticker": row["symbol"], "name": row["name"], "country": row["country"],
            "sector": row["sector"], "exchange": row["exchange"], "region": region,
            "held": row["symbol"] in held,
        })

    counts_by_region = {}
    for c in candidates:
        counts_by_region[c["region"]] = counts_by_region.get(c["region"], 0) + 1

    return {
        "candidates": candidates,
        "excluded_markets": sorted(excluded_markets),
        "counts_by_region": counts_by_region,
        "total": len(candidates),
    }


def _select_scan_candidates(limit=None):
    """Up to `limit` (or MAX_SCAN_CANDIDATES) tickers to (re)score: never-scored first,
    then oldest, spread round-robin across regions so one region can't starve the
    others. A ticker whose cache entry is younger than SCAN_STALE_DAYS is skipped."""
    universe = gems_universe()
    cache_by_ticker = {r["ticker"]: r for r in fin.score_cache_all()}
    cutoff = (datetime.now() - timedelta(days=SCAN_STALE_DAYS)).isoformat(timespec="seconds")

    by_region = {}
    for c in universe["candidates"]:
        if c["held"]:
            continue
        cached = cache_by_ticker.get(c["ticker"])
        if cached and cached["scored_at"] >= cutoff:
            continue
        by_region.setdefault(c["region"], []).append((cached["scored_at"] if cached else "", c["ticker"]))

    for region in by_region:
        by_region[region].sort(key=lambda x: x[0])  # "" (never scored) sorts first, then oldest

    cap = int(limit) if limit else MAX_SCAN_CANDIDATES
    ordered = []
    regions = [r for r in by_region if by_region[r]]
    i = 0
    while len(ordered) < cap and regions:
        region = regions[i % len(regions)]
        ordered.append(by_region[region].pop(0)[1])
        if not by_region[region]:
            regions.remove(region)
        else:
            i += 1
    return ordered


def _scan_one(ticker, profile):
    """Score one ticker and persist it to score_cache. Raises on failure: the caller
    (batch runner) turns that into a per-ticker error, never a scan-wide stop."""
    raw = _raw_inputs(ticker)
    fundamentals = raw.get("fundamentals") or {}
    if fundamentals.get("error"):
        raise RuntimeError(fundamentals["error"])

    result = scoring.score_stock(
        raw.get("quote") or {}, fundamentals, raw.get("historical") or {},
        dcf=raw.get("dcf"), graham=raw.get("graham"), profile=profile,
        quarterly=raw.get("quarterly") or {}, estimates=raw.get("estimates") or {},
    )
    payload = {
        "name": fundamentals.get("name"),
        "pillars": [{"key": p["key"], "name_es": p["name_es"], "subscore": p["subscore"],
                     "details": p["details"]} for p in result["pillars"]],
        "dividend_variants": scoring.dividend_variants(fundamentals),
        "red_flags": result["red_flags"],
        "quarters_available": result["quarters_available"],
        "quarters_note_es": result["quarters_note_es"],
        "pros": result["pros"],
        "cons": result["cons"],
    }

    catalog_row = symbols.lookup(ticker)
    region = EXCHANGE_REGION.get(catalog_row["exchange"]) if catalog_row else None
    currency = fundamentals.get("currency")
    market_cap = fundamentals.get("market_cap")
    market_cap_eur = None
    if market_cap is not None and currency:
        rate = _rates_to_base([currency]).get(currency)
        if rate is not None:
            market_cap_eur = round(market_cap * rate, 2)
    num_analysts = (fundamentals.get("analyst_consensus") or {}).get("num_analysts")

    fin.score_cache_upsert(
        ticker=ticker, scored_at=datetime.now().isoformat(timespec="seconds"),
        score=result["score"], coverage_pct=result["coverage_pct"],
        red_flags=result["red_flags"], market_cap_eur=market_cap_eur,
        # Catalog (GICS) sector, not Yahoo's: gems_list compares it with the held
        # tickers' catalog sectors to mark `diversifies`, offline.
        num_analysts=num_analysts,
        sector=(catalog_row or {}).get("sector") or fundamentals.get("sector"),
        country=fundamentals.get("country") or (catalog_row or {}).get("country"),
        region=region, quarters_available=result["quarters_available"], payload=payload,
    )


_scan_lock = threading.Lock()
_scan_state = {
    "running": False, "done": 0, "total": 0, "errors": [],
    "started_at": None, "finished_at": None, "status": "idle",
}
_scan_cancel = False


def _scan_batch(tickers, profile):
    def run(ticker):
        try:
            _scan_one(ticker, profile)
            return True, None
        except Exception as exc:  # noqa: BLE001 - one ticker's failure never stops the scan
            return False, {"ticker": ticker, "error": f"{type(exc).__name__}: {exc}"}

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(SCAN_WORKERS, len(tickers))) as ex:
        return list(ex.map(run, tickers))


def _run_scan(candidates):
    global _scan_cancel
    profile = fin.profile_get()
    consecutive_failed_batches = 0
    status = "done"

    for i in range(0, len(candidates), SCAN_BATCH):
        if _scan_cancel:
            status = "cancelled"
            break
        batch = candidates[i:i + SCAN_BATCH]
        results = _scan_batch(batch, profile)
        ok_count = sum(1 for ok, _ in results if ok)
        with _scan_lock:
            _scan_state["done"] += len(batch)
            _scan_state["errors"].extend(err for ok, err in results if not ok)

        if ok_count == 0 and batch:
            consecutive_failed_batches += 1
            if consecutive_failed_batches >= 3:
                status = "paused_rate_limited"
                break
            time.sleep(SCAN_BACKOFF_SECONDS[min(consecutive_failed_batches - 1, len(SCAN_BACKOFF_SECONDS) - 1)])
        else:
            consecutive_failed_batches = 0
            time.sleep(SCAN_BATCH_PAUSE)

    with _scan_lock:
        _scan_state["running"] = False
        _scan_state["status"] = status
        _scan_state["finished_at"] = datetime.now().isoformat(timespec="seconds")
    _scan_cancel = False


def gems_scan_start(limit=None):
    """Start a background scan (HU-12). Only one at a time; `limit` caps how many
    candidates it scores (mainly for tests — production scans use MAX_SCAN_CANDIDATES)."""
    global _scan_cancel
    with _scan_lock:
        if _scan_state["running"]:
            return {"ok": False, "error": "Ya hay un escaneo de gemas en curso."}
        candidates = _select_scan_candidates(limit)
        _scan_cancel = False
        _scan_state.update({
            "running": True, "done": 0, "total": len(candidates), "errors": [],
            "started_at": datetime.now().isoformat(timespec="seconds"),
            "finished_at": None, "status": "running",
        })
    threading.Thread(target=_run_scan, args=(candidates,), daemon=True).start()
    return {"ok": True, "total": len(candidates)}


def gems_scan_status():
    with _scan_lock:
        return dict(_scan_state)


def gems_scan_cancel():
    global _scan_cancel
    with _scan_lock:
        if not _scan_state["running"]:
            return {"ok": False, "error": "No hay ningun escaneo de gemas en curso."}
        _scan_cancel = True
    return {"ok": True}


def export_all():
    """Everything the user owns, as plain rows: local backup to CSV/JSON, no network."""
    return {
        "trades": fin.sql_query("SELECT * FROM trades ORDER BY date, id", 100000)["rows"],
        "watchlist": fin.watchlist_list()["watchlist"],
        "reviews": fin.reviews_list()["reviews"],
    }


def build_prompt(ticker):
    """Text to copy into any AI: the ticker's numbers with URLs, the investor profile and
    the whole portfolio. Stays on this machine until the user pastes it somewhere."""
    detail = _with_news(stock_detail(ticker))
    valued = portfolio_valued()
    positions = valued.get("positions") or []
    # The valued position is the complete one (weight, cost currency); stock_detail's
    # snapshot is a subset and would print "—" for the weight.
    full = next((p for p in positions if p["ticker"] == detail["ticker"]), detail.get("position"))
    if full:
        detail = {**detail, "position": full}
    return {
        "ticker": detail["ticker"],
        "text": llm.build_prompt(detail, fin.profile_get(), positions),
    }


def ask(ticker, question, include_position=False, web_search=True):
    """One question to Claude, with this ticker's numbers as context. Network only here,
    and only when the user presses the button: see app/llm.py for the privacy boundary."""
    detail = _with_news(stock_detail(ticker, latest_quarter_end=None))
    return llm.ask(ticker, question, detail, include_position=include_position,
                   web_search=web_search)


def config():
    """App settings the UI needs at start, including what is still pending in the broker.
    SeaLock never sends anything to a broker: this is the list of what YOU have to copy."""
    try:
        pending = fin.trades_pending()["by_broker"]
    except Exception:  # noqa: BLE001 - a missing count must not break the app
        pending = {}
    return {
        "currency": CURRENCY,
        "database_path": fin.DB_PATH,
        "brokers": list(fin.BROKERS),
        "pending_by_broker": {k: v["count"] for k, v in pending.items()},
        "claude_available": llm.available(),
        "claude_model": llm.MODEL,
    }


def sparklines(tickers, date_range="1y"):
    """Daily closes for several tickers, for the sparklines of a card grid (HU-16).
    Batched on purpose: one request from the UI instead of N. Cached 6h in
    `_series_cache`, so re-rendering a card grid is free. Failures come back as an
    empty series, never as an error that hides the whole grid."""
    out = {}
    tickers = [t.upper().strip() for t in (tickers or []) if t]
    if not tickers:
        return {"series": out}
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(6, len(tickers))) as ex:
        for ticker, series in zip(tickers, ex.map(lambda t: _closes(t, date_range), tickers)):
            out[ticker] = [{"date": d, "close": c} for d, c in sorted(series.items())]
    return {"series": out}


def alerts_evaluate():
    """Evaluate the stored alerts against the latest readings (HU-25).

    Only the web server can do this: `finance` has no network. Prices come from
    one batched quote; the daily metrics (SMA 50/200, RSI 14) are computed from
    the closes `_closes` already caches 6h, so a 60s loop costs one quote request
    instead of one per alert. Reads facts, writes trigger states, and its answer
    is only ever rendered as a fact: never as advice."""
    alerts = fin.alerts_list()["alerts"]
    active = [a for a in alerts if a["active"]]
    if not active:
        return {"ok": True, "evaluated": 0, "triggered": [], "unavailable": [],
                "alerts": alerts, "any_market_open": False}

    readings = {}
    price_tickers = sorted({a["ticker"] for a in active if a["metric"] == "price"})
    if price_tickers:
        quotes = mer.quote(price_tickers).get("quotes", [])
        for q in quotes:
            if q.get("price") is not None:
                readings.setdefault(q["ticker"], {})["price"] = q["price"]
        # Gate for the 60s loop (same idea as the portfolio one): when every
        # price alert sits in a closed market, polling again changes nothing.
        # An unknown state counts as open: better to evaluate than to go quiet.
        states = [q.get("status") for q in quotes]
        any_open = any(s != "CLOSED" for s in states) if states else True
    else:
        # No price alert: the only readings are daily, from the 6h cache.
        any_open = True

    for ticker in sorted({a["ticker"] for a in active if a["metric"] != "price"}):
        series = _closes(ticker, "5y")
        closes = [c for _, c in sorted(series.items()) if c]
        if not closes:
            continue
        daily = readings.setdefault(ticker, {})
        if len(closes) >= 50:
            daily["sma_50"] = indicators.SMA(50, closes).last()
        if len(closes) >= 200:
            daily["sma_200"] = indicators.SMA(200, closes).last()
        if len(closes) > 14:
            rsi = indicators.RSI(14, closes).last()
            daily["rsi_14"] = round(rsi, 1) if rsi is not None else None

    result = fin.evaluate_alerts(active, readings)
    fin.alerts_apply(result["updates"])
    return {"ok": True, "evaluated": len(active), "triggered": result["fired"],
            "unavailable": result["unavailable"], "alerts": fin.alerts_list()["alerts"],
            "any_market_open": any_open}


API["app"] = {
    "portfolio_valued": portfolio_valued,
    "portfolio_metrics": portfolio_metrics,
    "portfolio_scores": portfolio_scores,
    "stock_detail": stock_detail,
    "stock_news": stock_news,
    "alerts_evaluate": alerts_evaluate,
    "recommendation_universe": recommendation_universe,
    "stock_scores": stock_scores,
    "recommendation_positions": recommendation_positions,
    "stock_compare": stock_compare,
    "sparklines": sparklines,
    "gems_universe": gems_universe,
    "gems_scan_start": gems_scan_start,
    "gems_scan_status": gems_scan_status,
    "gems_scan_cancel": gems_scan_cancel,
    "export_all": export_all,
    "ask": ask,
    "build_prompt": build_prompt,
    "config": config,
}


# -------------------------------------------------------------------- server


class Handler(BaseHTTPRequestHandler):
    server_version = "SeaLock"

    def log_message(self, fmt, *args):  # suppress noisy API logs
        if "/api/" in str(args[0] if args else ""):
            return
        sys.stderr.write("  %s\n" % (fmt % args))

    # -- helpers

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _is_local(self):
        """Reject non-local Host/Origin: defense against DNS rebinding."""
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
        if host not in ("127.0.0.1", "localhost", "::1"):
            return False
        origin = self.headers.get("Origin")
        if origin and urlparse(origin).hostname not in ("127.0.0.1", "localhost", "::1"):
            return False
        return True

    # -- routes

    def do_GET(self):
        if not self._is_local():
            self._json({"error": "Local requests only"}, 403)
            return

        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._serve_index()
            return

        filename = os.path.basename(path)
        dest = os.path.join(WEB_DIR, filename)
        if not os.path.isfile(dest) or os.path.dirname(os.path.abspath(dest)) != WEB_DIR:
            self._json({"error": "Not found"}, 404)
            return

        with open(dest, "rb") as fh:
            body = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", CONTENT_TYPES.get(os.path.splitext(filename)[1], "text/plain"))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _serve_index(self):
        with open(os.path.join(WEB_DIR, "index.html"), encoding="utf-8") as fh:
            html = fh.read().replace("{{TOKEN}}", TOKEN).replace("{{CURRENCY}}", CURRENCY)
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if not self._is_local():
            self._json({"error": "Local requests only"}, 403)
            return
        if not secrets.compare_digest(self.headers.get("X-SeaLock-Token", ""), TOKEN):
            self._json({"error": "Invalid token. Reload the page."}, 403)
            return

        parts = urlparse(self.path).path.strip("/").split("/")
        if len(parts) != 3 or parts[0] != "api":
            self._json({"error": "Unknown route"}, 404)
            return

        _, group, tool_name = parts
        fn = API.get(group, {}).get(tool_name)
        if fn is None:
            self._json({"error": f"{group}.{tool_name} does not exist"}, 404)
            return

        try:
            length = int(self.headers.get("Content-Length") or 0)
            args = json.loads(self.rfile.read(length) or "{}") if length else {}
        except (ValueError, json.JSONDecodeError):
            self._json({"error": "Invalid JSON body"}, 400)
            return

        try:
            self._json({"ok": True, "data": fn(**args)})
        except TypeError as exc:
            self._json({"ok": False, "error": f"Invalid parameters: {exc}"}, 400)
        except Exception as exc:  # noqa: BLE001 - shown in the UI
            self._json({"ok": False, "error": str(exc)}, 400)


def main():
    url = f"http://127.0.0.1:{PORT}/"
    # Probe first: on Windows allow_reuse_address lets a second server bind the same
    # port silently, so a failed bind can't be relied on to detect a running copy.
    try:
        socket.create_connection(("127.0.0.1", PORT), timeout=0.5).close()
        running = True
    except OSError:
        running = False
    if running:
        # Already running (e.g. second double-click): just open the existing one.
        print(f"\n  SeaLock is already running: {url}")
        if os.environ.get("SEALOCK_NO_OPEN") != "1":
            webbrowser.open(url)
        sys.exit(0)

    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"\n  SeaLock running:   {url}")
    print(f"  Database:          {fin.DB_PATH}")
    print("  Ctrl+C to stop.\n")

    if os.environ.get("SEALOCK_NO_OPEN") != "1":
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopped.")
        server.shutdown()


if __name__ == "__main__":
    main()
