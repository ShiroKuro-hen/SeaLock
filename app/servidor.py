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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "mcp"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import finanzas_server as fin  # noqa: E402
import llm  # noqa: E402
import mercado_server as mer  # noqa: E402
import scoring  # noqa: E402

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
        raw["historical"] = mer.historical(ticker=ticker, date_range="1y")
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


API["app"] = {
    "portfolio_valued": portfolio_valued,
    "portfolio_scores": portfolio_scores,
    "stock_detail": stock_detail,
    "stock_news": stock_news,
    "recommendation_universe": recommendation_universe,
    "stock_scores": stock_scores,
    "recommendation_positions": recommendation_positions,
    "stock_compare": stock_compare,
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
