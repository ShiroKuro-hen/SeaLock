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
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "mcp"))

import finanzas_server as fin  # noqa: E402
import mercado_server as mer  # noqa: E402

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


def dashboard():
    """Everything the main screen needs, in a single request."""
    accounts = fin.accounts_list()
    cash = round(sum(a["balance"] for a in accounts["accounts"]), 2)
    portfolio = portfolio_valued()
    portfolio_value = portfolio["totals"].get("market_value", 0) or 0

    return {
        "currency": CURRENCY,
        "net_worth": round(cash + portfolio_value, 2),
        "cash": cash,
        "portfolio": round(portfolio_value, 2),
        "unrealized_gain": portfolio["totals"].get("unrealized_gain"),
        "accounts": accounts["accounts"],
        "month": fin.period_summary(),
        "evolution": fin.monthly_evolution(months=12)["series"],
        "budgets": fin.budget_status()["budgets"],
        "positions": portfolio["positions"][:5],
        "no_price": portfolio.get("no_price", []),
    }


def stock_detail(ticker, growth_pct=8, discount_rate_pct=9, terminal_growth_pct=2.5):
    """Quote + fundamentals + history + valuation + news for a single security."""
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
    output["verdict"] = _verdict(output)
    return output


def _verdict(data):
    """Generate plain-language verdict from stock data. NOT investment advice."""
    f = data.get("fundamentals") or {}
    c = data.get("quote") or {}
    h = data.get("historical") or {}
    dcf = data.get("dcf") or {}
    gra = data.get("graham") or {}
    v = f.get("valuation") or {}
    r = f.get("profitability") or {}
    s = f.get("financial_strength") or {}
    d = f.get("dividend") or {}
    ri = f.get("risk") or {}
    pf = f.get("per_share") or {}

    scores = []
    notes = []

    # --- valuation ---
    per = v.get("per_trailing")
    ev = v.get("ev_ebitda")
    margin = dcf.get("margin_of_safety_pct")

    if per is not None:
        if per < 15:
            scores.append(("valuation", 1, f"PER {per:.1f} dentro de rango barato"))
        elif per < 25:
            scores.append(("valuation", 0, f"PER {per:.1f} razonable"))
        else:
            scores.append(("valuation", -1, f"PER {per:.1f} elevado, mercado espera mucho crecimiento"))
    if ev is not None:
        if ev < 10:
            scores.append(("valuation", 1, f"EV/EBITDA {ev:.1f} sugiere precio contenido"))
        elif ev > 18:
            scores.append(("valuation", -1, f"EV/EBITDA {ev:.1f} alto"))
    if margin is not None:
        if margin > 30:
            scores.append(("valuation", 1, f"DCF con margen de seguridad del {margin:.0f}%"))
        elif margin < 0:
            scores.append(("valuation", -1, f"DCF indica sobrevaloracion del {abs(margin):.0f}%"))

    # --- trend ---
    rsi = h.get("rsi_14")
    ret = h.get("period_return_pct")
    m50 = h.get("sma_50")
    m200 = h.get("sma_200")
    price = c.get("price")

    if rsi is not None:
        if rsi > 75:
            notes.append("RSI sobrecomprado, posible retroceso a corto plazo")
        elif rsi < 30:
            scores.append(("trend", 1, "RSI sobrevendido, posible punto de entrada"))
    if ret is not None:
        if ret > 20:
            scores.append(("trend", 1, f"+{ret:.0f}% en el ultimo ano, momentum fuerte"))
        elif ret < -20:
            scores.append(("trend", -1, f"{ret:.0f}% en el ultimo ano, tendencia bajista"))
    if m50 and m200 and price:
        if price > m50 > m200:
            scores.append(("trend", 1, "Precio por encima de medias 50 y 200, tendencia alcista"))
        elif price < m50 < m200:
            scores.append(("trend", -1, "Precio por debajo de medias 50 y 200, tendencia bajista"))

    # --- solidity ---
    roe = r.get("roe_pct")
    net_margin = r.get("net_margin_pct")
    debt = s.get("net_debt")

    if roe is not None:
        if roe > 15:
            scores.append(("solidity", 1, f"ROE del {roe:.0f}% indica buen rendimiento del capital"))
        elif roe < 5:
            scores.append(("solidity", -1, f"ROE del {roe:.0f}% bajo"))
    if net_margin is not None and net_margin < 3:
        notes.append("Margen neto muy bajo, poca eficiencia o margenes presionados")
    if debt is not None and debt < 0:
        scores.append(("solidity", 1, "Caja supera deuda: balanza financiera solida"))
    elif debt is not None and s.get("ebitda") and debt / max(s["ebitda"], 1) > 3:
        notes.append("Deuda neta superior a 3x EBITDA: apalancamiento elevado")

    # --- dividend ---
    dy = d.get("yield_pct")
    payout = d.get("payout_pct")

    if dy is not None:
        if dy > 4:
            scores.append(("dividend", 1, f"Dividendo del {dy:.1f}%, atractivo para renta fija"))
        elif dy > 2.5:
            scores.append(("dividend", 0, f"Dividendo del {dy:.1f}%, moderado"))
    if payout is not None and payout > 90:
        notes.append(f"Payout del {payout:.0f}%: dividendo puede no ser sostenible")

    # --- risk ---
    vol = h.get("annualized_volatility_pct")
    dd = h.get("max_drawdown_pct")
    beta = ri.get("beta")

    if vol is not None:
        if vol > 35:
            notes.append(f"Volatilidad alta ({vol:.0f}%): fuertes oscilaciones de precio")
        elif vol < 15:
            scores.append(("risk", 1, f"Volatilidad baja ({vol:.0f}%): empresa estable"))
    if dd is not None and dd < -40:
        notes.append(f"Caida maxima historica del {abs(dd):.0f}%: considerar si aguantarias esa caida")

    # --- build summary ---
    total = sum(s[1] for s in scores)
    positives = [s[2] for s in scores if s[1] > 0]
    negatives = [s[2] for s in scores if s[1] < 0]

    # These values are consumed directly as CSS class suffixes by app.js
    # (estilo.css defines .veredicto-verde/.veredicto-ambar/.veredicto-rojo,
    # not translated), so they stay Spanish rather than becoming green/amber/red.
    if total >= 3:
        sentiment_text = " Los indicadores apuntan mayormente a favor."
        color = "verde"
    elif total >= 1:
        sentiment_text = " Mixto: hay argumentos a favor y en contra."
        color = "ambar"
    elif total >= -1:
        sentiment_text = " Mixto, con algo de cautela."
        color = "ambar"
    else:
        sentiment_text = " Los indicadores apuntan mayormente en contra."
        color = "rojo"

    return {
        "sentiment": color,
        "summary": (
            f"Segun los datos disponibles ({c.get('market', 'mercado')}), "
            f"{f.get('name', data.get('ticker', ''))} presenta un panorama{sentiment_text}"
        ),
        "pros": positives,
        "cons": negatives,
        "watch_points": notes,
        "notice": (
            "Esto es un resumen automatico de los datos publicos, no una recomendacion "
            "de compra ni de venta. Cada inversion depende de tu situacion personal, "
            "tu horizonte temporal y tu tolerancia al riesgo. Consulta a un asesor "
            "financiero antes de tomar decisiones."
        ),
    }


API["app"] = {
    "dashboard": dashboard,
    "portfolio_valued": portfolio_valued,
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
