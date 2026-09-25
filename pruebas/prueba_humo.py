"""Smoke test: launches each MCP server as a subprocess and speaks JSON-RPC with it.

Usage:  python pruebas/prueba_humo.py
Checks the handshake, the tool listing and a real call against each server.
The 'market' part needs an internet connection; if it fails, it's marked as WARNING.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Tests write to a disposable database: the user's real data in datos/finanzas.db
# is never touched.
TEST_DIR = tempfile.mkdtemp(prefix="sealock-test-")


class Client:
    def __init__(self, script):
        env = {**os.environ, "SEALOCK_DATA": TEST_DIR, "PYTHONIOENCODING": "utf-8"}
        self.proc = subprocess.Popen(
            [sys.executable, "-u", os.path.join(ROOT, "mcp", script)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            cwd=ROOT,
            env=env,
        )
        self.n = 0

    def request(self, method, params=None):
        self.n += 1
        self.proc.stdin.write(
            json.dumps({"jsonrpc": "2.0", "id": self.n, "method": method, "params": params or {}})
            + "\n"
        )
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        if not line:
            raise RuntimeError(f"Server died. stderr:\n{self.proc.stderr.read()}")
        return json.loads(line)

    def close(self):
        self.proc.stdin.close()
        self.proc.terminate()
        self.proc.wait(timeout=5)


def text(response):
    return response["result"]["content"][0]["text"]


failures = []


def check(label, condition, detail="", critical=True):
    if condition:
        print(f"  OK    {label}")
    elif critical:
        print(f"  FALLO {label}  {detail}")
        failures.append(label)
    else:
        print(f"  AVISO {label}  {detail}")


# --------------------------------------------------------------------- finance
print("\n== servidor finance ==")
c = Client("finanzas_server.py")
try:
    init = c.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}})
    check("handshake initialize", init["result"]["serverInfo"]["name"] == "finance", str(init))

    tools = c.request("tools/list")["result"]["tools"]
    names = {t["name"] for t in tools}
    check(f"tools/list ({len(tools)} herramientas)", len(tools) >= 8)
    check(
        "herramientas clave presentes",
        {"trade_create", "portfolio_view", "profile_get", "profile_set", "watchlist_add"} <= names,
    )

    # --- perfil inversor ---
    defaults = json.loads(text(c.request("tools/call", {"name": "profile_get", "arguments": {}})))
    check(
        "profile_get devuelve valores por defecto",
        defaults == {"risk_tolerance": "moderate", "horizon": "long", "preference": "balanced", "updated_at": None},
        str(defaults),
    )

    set_result = json.loads(text(c.request("tools/call", {
        "name": "profile_set",
        "arguments": {"risk_tolerance": "aggressive", "preference": "growth"},
    })))
    check(
        "profile_set actualiza los campos dados",
        set_result["risk_tolerance"] == "aggressive" and set_result["preference"] == "growth"
        and set_result["horizon"] == "long",
        str(set_result),
    )

    reread = json.loads(text(c.request("tools/call", {"name": "profile_get", "arguments": {}})))
    check(
        "profile_get persiste el cambio (actualizacion parcial)",
        reread["risk_tolerance"] == "aggressive" and reread["preference"] == "growth"
        and reread["horizon"] == "long",
        str(reread),
    )

    bad_profile = c.request("tools/call", {
        "name": "profile_set", "arguments": {"risk_tolerance": "yolo"},
    })
    check("profile_set rechaza valor de enum invalido", bad_profile["result"].get("isError") is True, text(bad_profile))

    trade = c.request(
        "tools/call",
        {
            "name": "trade_create",
            "arguments": {"ticker": "aapl", "action": "buy", "quantity": 10, "price": 150},
        },
    )
    check("registrar compra", json.loads(text(trade)).get("ok") is True, text(trade))

    bad_sell = c.request(
        "tools/call",
        {
            "name": "trade_create",
            "arguments": {"ticker": "AAPL", "action": "sell", "quantity": 999, "price": 200},
        },
    )
    check("rechaza venta en corto", bad_sell["result"].get("isError") is True)

    portfolio = json.loads(text(c.request("tools/call", {"name": "portfolio_view", "arguments": {}})))
    pos = next((p for p in portfolio["positions"] if p["ticker"] == "AAPL"), None)
    check("coste medio correcto", pos and abs(pos["average_cost"] - 150) < 0.01, str(pos))
    check(
        "con una sola moneda si agrega el total",
        portfolio["total_cost_invested"] is not None,
        str(portfolio["total_cost_invested"]),
    )

    # Al meter una posicion en otra divisa, los totales agregados deben anularse en vez
    # de sumar euros con dolares.
    c.request("tools/call", {"name": "trade_create", "arguments": {
        "ticker": "MSFT", "action": "buy", "quantity": 2, "price": 400, "currency": "USD"}})
    mixed = json.loads(text(c.request("tools/call", {"name": "portfolio_view", "arguments": {}})))
    check(
        "no suma monedas distintas en un total unico",
        mixed["total_cost_invested"] is None and mixed["accumulated_realized"] is None,
        f"cost={mixed['total_cost_invested']} realized={mixed['accumulated_realized']}",
    )
    check(
        "desglosa el coste por moneda",
        set(mixed["cost_by_currency"]) == {"EUR", "USD"},
        str(mixed["cost_by_currency"]),
    )

    bad_sql = c.request(
        "tools/call", {"name": "sql_query", "arguments": {"sql": "DELETE FROM trades"}}
    )
    check("sql_query bloquea escritura", bad_sql["result"].get("isError") is True)

    # --- watchlist ---
    added = json.loads(text(c.request("tools/call", {
        "name": "watchlist_add",
        "arguments": {"ticker": "nvda", "thesis": "prueba-humo", "target_price": 200},
    })))
    check("watchlist_add normaliza el ticker", added.get("ticker") == "NVDA", str(added))

    wl = json.loads(text(c.request("tools/call", {"name": "watchlist_list", "arguments": {}})))
    check("watchlist_list incluye lo anadido", any(w["ticker"] == "NVDA" for w in wl["watchlist"]), str(wl))

    removed = json.loads(text(c.request("tools/call", {"name": "watchlist_remove", "arguments": {"ticker": "NVDA"}})))
    check("watchlist_remove confirma el borrado", removed.get("ok") is True, str(removed))
finally:
    c.close()

# ---------------------------------------------------------------------- market
print("\n== servidor market ==")
c = Client("mercado_server.py")
try:
    init = c.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}})
    check("handshake initialize", init["result"]["serverInfo"]["name"] == "market")

    tools = c.request("tools/list")["result"]["tools"]
    check(f"tools/list ({len(tools)} herramientas)", len(tools) >= 8)

    # DCF con datos manuales: no toca la red, debe salir siempre.
    dcf = json.loads(
        text(
            c.request(
                "tools/call",
                {
                    "name": "dcf_valuation",
                    "arguments": {
                        "ticker": "TEST",
                        "free_cash_flow": 1_000_000_000,
                        "shares_outstanding": 100_000_000,
                        "net_debt": 0,
                        "current_price": 100,
                        "growth_pct": 5,
                        "discount_rate_pct": 9,
                        "terminal_growth_pct": 2.5,
                    },
                },
            )
        )
    )
    check(
        "DCF calcula valor por accion",
        isinstance(dcf.get("intrinsic_value_per_share"), (int, float)),
        str(dcf)[:200],
    )
    check("DCF incluye sensibilidad", len(dcf.get("sensitivity", [])) == 5)

    infinite = c.request(
        "tools/call",
        {
            "name": "dcf_valuation",
            "arguments": {
                "ticker": "TEST",
                "free_cash_flow": 1000,
                "shares_outstanding": 100,
                "terminal_growth_pct": 12,
                "discount_rate_pct": 9,
            },
        },
    )
    check("rechaza g_terminal >= tasa descuento", infinite["result"].get("isError") is True)

    q = c.request("tools/call", {"name": "quote", "arguments": {"tickers": ["AAPL"]}})
    data = json.loads(text(q))
    has_price = bool(data.get("quotes")) and data["quotes"][0].get("price")
    check("quote AAPL (red)", has_price, str(data)[:300], critical=False)

    news = c.request("tools/call", {"name": "news", "arguments": {"ticker": "AAPL", "limit": 3}})
    check(
        "news AAPL (red)",
        bool(json.loads(text(news)).get("news")),
        text(news)[:200],
        critical=False,
    )

    fund = c.request("tools/call", {"name": "fundamentals", "arguments": {"ticker": "AAPL"}})
    fd = json.loads(text(fund))
    check(
        "fundamentals AAPL (red, puede bloquear Yahoo)",
        not fd.get("error") and fd.get("current_price"),
        str(fd.get("error"))[:200],
        critical=False,
    )

    # Yahoo no lista ITX.MC al buscar 'Inditex': solo Varsovia, Milan y Hamburgo.
    # El sondeo de mercados principales debe recuperarlo y ponerlo primero.
    search = json.loads(text(c.request("tools/call", {"name": "ticker_search", "arguments": {"query": "Inditex"}})))
    check(
        "ticker_search recupera la cotizacion principal (ITX.MC)",
        search.get("suggested") == "ITX.MC",
        f"suggested={search.get('suggested')} results={[r['ticker'] for r in search.get('results', [])][:5]}",
        critical=False,
    )

    ibe = json.loads(text(c.request("tools/call", {"name": "ticker_search", "arguments": {"query": "Iberdrola"}})))
    check(
        "ticker_search degrada los ADR OTC (IBDSF)",
        ibe.get("suggested") == "IBE.MC",
        f"suggested={ibe.get('suggested')}",
        critical=False,
    )

    # Yahoo no publica capitalizacion ni acciones para valores espanoles; deben
    # derivarse por identidad contable a partir del valor de empresa.
    itx = json.loads(text(c.request("tools/call", {"name": "fundamentals", "arguments": {"ticker": "ITX.MC"}})))
    shares = itx.get("per_share", {}).get("shares_outstanding")
    check(
        "deriva acciones de ITX.MC (~3.116 M, no el free float de 1.108 M)",
        shares and 3.0e9 < shares < 3.3e9,
        f"shares={shares}",
        critical=False,
    )
    check(
        "marca los campos derivados como tales",
        bool(itx.get("derived_fields")),
        str(itx.get("derived_fields")),
        critical=False,
    )

    # Sin FCF el DCF no debe inventarse nada: debe fallar diciendo que falta y que hay.
    no_fcf = c.request("tools/call", {"name": "dcf_valuation", "arguments": {"ticker": "ITX.MC"}})
    msg = text(no_fcf)
    check(
        "DCF sin FCF falla con mensaje accionable",
        no_fcf["result"].get("isError") is True and "free_cash_flow" in msg,
        msg[:150],
        critical=False,
    )
finally:
    c.close()

# ---------------------------------------------------------------------- scoring
# Pure module, no network: exercised directly instead of through a subprocess.
print("\n== motor de puntuacion (scoring.py) ==")
sys.path.insert(0, os.path.join(ROOT, "mcp"))
import scoring  # noqa: E402

HEALTHY = {
    "fundamentals": {
        "profitability": {"roe_pct": 25, "net_margin_pct": 20, "roa_pct": 12},
        "valuation": {"per_trailing": 12, "peg": 0.8},
        "financial_strength": {"debt_to_equity": 30, "current_ratio": 2.5},
        "growth": {"revenue_yoy_pct": 12, "earnings_yoy_pct": 15},
        "dividend": {},
    },
    "quote": {"price": 120},
    "historical": {"sma_50": 100, "sma_200": 90, "period_return_pct": 18, "rsi_14": 55},
}
healthy_score = scoring.score_stock(
    HEALTHY["quote"], HEALTHY["fundamentals"], HEALTHY["historical"],
)
check(
    "empresa sana y barata saca nota >= 7",
    healthy_score["score"] >= 7,
    str(healthy_score["score"]),
)

DISTRESSED = {
    "fundamentals": {
        "profitability": {},
        "valuation": {},
        "financial_strength": {"debt_to_equity": 300},
        "growth": {"revenue_yoy_pct": -8, "earnings_yoy_pct": -20},
        "dividend": {"yield_pct": 9, "payout_pct": 120},
        "cash_flow": {"free_cash_flow": -500},
    },
    "quote": {"price": 40},
    "historical": {"sma_50": 55, "sma_200": 60, "period_return_pct": -35, "rsi_14": 25},
}
distressed_score = scoring.score_stock(
    DISTRESSED["quote"], DISTRESSED["fundamentals"], DISTRESSED["historical"],
)
check(
    "empresa en apuros saca nota <= 3",
    distressed_score["score"] <= 3,
    str(distressed_score["score"]),
)
check(
    "empresa en apuros dispara senales de alerta",
    len(distressed_score["red_flags"]) > 0,
    str(distressed_score["red_flags"]),
)

empty_score = scoring.score_stock({}, {}, {})
check(
    "sin fundamentales, cobertura baja y con aviso",
    empty_score["coverage_pct"] < 50 and empty_score["coverage_warning"],
    str(empty_score["coverage_pct"]),
)

combos_ok = True
for risk in ("conservative", "moderate", "aggressive"):
    for horizon in ("short", "medium", "long"):
        for preference in ("dividend", "growth", "balanced"):
            w = scoring._adjust_weights(risk, horizon, preference)
            if abs(sum(w.values()) - 100) > 0.01:
                combos_ok = False
check("los pesos del perfil suman 100 en las 27 combinaciones", combos_ok)

shutil.rmtree(TEST_DIR, ignore_errors=True)

print("\n" + ("TODO CORRECTO" if not failures else f"FALLOS CRITICOS: {failures}"))
sys.exit(1 if failures else 0)
