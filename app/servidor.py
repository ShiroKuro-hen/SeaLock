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

import json
import os
import secrets
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "mcp"))

import finanzas_server as fin  # noqa: E402
import mercado_server as mer  # noqa: E402
import scoring  # noqa: E402

SCORE_CACHE_TTL = 3600  # seconds; avoids hammering Yahoo when the Cartera tab polls

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


def stock_detail(ticker, growth_pct=8, discount_rate_pct=9, terminal_growth_pct=2.5):
    """Quote + fundamentals + history + valuation + news + score for a single security."""
    output = {"ticker": ticker.upper().strip()}

    def try_section(key, fn):
        try:
            output[key] = fn()
        except Exception as exc:  # noqa: BLE001
            output[key] = {"error": f"{type(exc).__name__}: {exc}"}

    try_section("quote", lambda: mer.quote(tickers=[ticker])["quotes"][0])
    try_section("fundamentals", lambda: mer.fundamentals(ticker=ticker))
    try_section("historical", lambda: mer.historical(ticker=ticker, date_range="1y"))
    try_section("news", lambda: mer.news(ticker=ticker, limit=8)["news"])
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
    output["score"] = scoring.score_stock(
        clean("quote") or {}, clean("fundamentals") or {}, clean("historical") or {},
        dcf=clean("dcf"), graham=clean("graham"), profile=fin.profile_get(), holding=holding,
    )
    return output


_score_cache = {}  # ticker -> (timestamp, score dict)


def portfolio_scores():
    """One score per held ticker, cached in-process for an hour so the Cartera tab's
    periodic refresh doesn't hammer Yahoo for the same handful of tickers."""
    portfolio = fin.portfolio_view()
    profile = fin.profile_get()
    now = time.time()
    out = {}
    for p in portfolio["positions"]:
        ticker = p["ticker"]
        cached = _score_cache.get(ticker)
        if cached and now - cached[0] < SCORE_CACHE_TTL:
            out[ticker] = cached[1]
            continue
        try:
            q = mer.quote(tickers=[ticker])["quotes"]
            q = q[0] if q else {}
            f = mer.fundamentals(ticker=ticker)
            h = mer.historical(ticker=ticker, date_range="1y")
            dcf = graham = None
            try:
                dcf = mer.dcf_valuation(ticker=ticker)
            except Exception:  # noqa: BLE001 - optional input
                pass
            try:
                graham = mer.graham_valuation(ticker=ticker)
            except Exception:  # noqa: BLE001 - optional input
                pass
            result = scoring.score_stock(q, f, h, dcf=dcf, graham=graham, profile=profile, holding=p)
        except Exception as exc:  # noqa: BLE001 - surfaced per-ticker, not fatal
            result = {"error": f"{type(exc).__name__}: {exc}"}
        _score_cache[ticker] = (now, result)
        out[ticker] = result
    return {"scores": out}


API["app"] = {
    "portfolio_valued": portfolio_valued,
    "portfolio_scores": portfolio_scores,
    "stock_detail": stock_detail,
    "config": lambda: {"currency": CURRENCY, "database_path": fin.DB_PATH},
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
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://127.0.0.1:{PORT}/"
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
