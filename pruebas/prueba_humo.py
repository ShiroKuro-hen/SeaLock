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

    # --- revisiones de la nota (score_reviews) ---
    def review_set(**args):
        return c.request("tools/call", {"name": "review_set", "arguments": args})

    check("review_set rechaza adjustment fuera de [-2, 2]",
          review_set(ticker="AAPL", adjustment=2.5, rationale="muy buena")["result"].get("isError") is True)
    check("review_set rechaza rationale vacio",
          review_set(ticker="AAPL", adjustment=1, rationale="   ")["result"].get("isError") is True)
    check("review_set rechaza rationale de mas de 2000 caracteres",
          review_set(ticker="AAPL", adjustment=1, rationale="x" * 2001)["result"].get("isError") is True)
    check("review_set rechaza sources que no son URLs http(s)",
          review_set(ticker="AAPL", adjustment=1, rationale="bien", sources=["nota://x"])["result"].get("isError") is True)

    saved = json.loads(text(review_set(
        ticker="aapl", adjustment=1, rationale="BPA del Q2 +40%", sources=["https://example.com/a"],
        mechanical_score=6.4, last_quarter_seen="2026-06-30")))
    check("review_set guarda la revision y normaliza el ticker",
          saved["ok"] and saved["review"]["ticker"] == "AAPL", str(saved)[:200])
    check("la revision guardada sale activa y con su fuente",
          saved["review"]["status"] == "active" and saved["review"]["sources"] == ["https://example.com/a"],
          str(saved["review"])[:200])

    got = json.loads(text(c.request("tools/call", {"name": "review_get", "arguments": {"ticker": "AAPL"}})))
    check("review_get devuelve la revision vigente", got["review"]["adjustment"] == 1, str(got)[:200])
    stale = json.loads(text(c.request("tools/call", {
        "name": "review_get", "arguments": {"ticker": "AAPL", "latest_quarter_end": "2026-09-30"}})))
    check("review_get marca obsoleta si hay trimestre mas nuevo que last_quarter_seen",
          stale["review"]["status"] == "stale", str(stale["review"]["status"]))

    review_set(ticker="AAPL", adjustment=-1, rationale="Revision nueva, mejor de lo esperado")
    got2 = json.loads(text(c.request("tools/call", {"name": "review_get", "arguments": {"ticker": "AAPL"}})))
    check("una revision nueva sustituye a la anterior como vigente",
          got2["review"]["adjustment"] == -1, str(got2)[:200])
    listed = json.loads(text(c.request("tools/call", {"name": "reviews_list", "arguments": {}})))
    check("la revision antigua queda en el historico", len(listed["reviews"]) == 2, str(len(listed["reviews"])))

    rid = listed["reviews"][0]["id"]
    shown = json.loads(text(c.request("tools/call", {"name": "review_delete", "arguments": {"id": rid}})))
    check("review_delete sin confirmacion muestra el registro y no borra",
          shown.get("needs_confirmation") is True and shown["review"]["id"] == rid, str(shown)[:200])
    gone = json.loads(text(c.request("tools/call", {
        "name": "review_delete", "arguments": {"id": rid, "confirm": True}})))
    check("review_delete con confirmacion borra",
          gone.get("ok") is True and gone["deleted"]["id"] == rid, str(gone)[:200])
    empty = json.loads(text(c.request("tools/call", {"name": "review_get", "arguments": {"ticker": "ZZZZ"}})))
    check("review_get sin revisiones devuelve null", empty["review"] is None, str(empty))

    # --- broker: donde se ejecuto y que falta por subir a la plataforma ---
    bad_broker = c.request("tools/call", {"name": "trade_create", "arguments": {
        "ticker": "AAPL", "action": "buy", "quantity": 1, "price": 10, "broker": "Citi"}})
    check("trade_create rechaza un broker fuera de la lista",
          bad_broker["result"].get("isError") is True, text(bad_broker)[:150])

    hapi = json.loads(text(c.request("tools/call", {"name": "trade_create", "arguments": {
        "ticker": "VOO", "action": "buy", "quantity": 2, "price": 500, "currency": "USD",
        "broker": "HapiBroker"}})))
    check("trade_create guarda el broker", hapi.get("broker") == "HapiBroker", str(hapi)[:150])

    ya_subida = json.loads(text(c.request("tools/call", {"name": "trade_create", "arguments": {
        "ticker": "AAPL", "action": "buy", "quantity": 1, "price": 10,
        "broker": "ETF", "uploaded": True}})))
    check("trade_create acepta 'ya esta en el broker'", ya_subida.get("uploaded") is True,
          str(ya_subida)[:150])

    pending = json.loads(text(c.request("tools/call", {"name": "trades_pending", "arguments": {}})))
    check("trades_pending agrupa lo que falta por broker",
          pending["pending"] >= 1 and "HapiBroker" in pending["by_broker"]
          and any(t["ticker"] == "VOO" for t in pending["by_broker"]["HapiBroker"]["trades"]),
          str(pending)[:250])
    check("una operacion ya subida no sale como pendiente",
          not any(t["id"] == ya_subida["id"]
                  for g in pending["by_broker"].values() for t in g["trades"]))

    marked = json.loads(text(c.request("tools/call", {
        "name": "trades_mark_uploaded", "arguments": {"broker": "HapiBroker"}})))
    check("trades_mark_uploaded marca las de ese broker",
          marked["marked"] >= 1 and bool(marked["uploaded_at"]), str(marked)[:150])
    after = json.loads(text(c.request("tools/call", {
        "name": "trades_pending", "arguments": {"broker": "HapiBroker"}})))
    check("tras marcar, HapiBroker queda limpio", after["pending"] == 0, str(after)[:150])

    port = json.loads(text(c.request("tools/call", {"name": "portfolio_view", "arguments": {}})))
    voo = next((p for p in port["positions"] if p["ticker"] == "VOO"), None)
    check("portfolio_view dice en que broker esta la posicion",
          voo and voo.get("brokers") == "HapiBroker", str(voo))
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

    # --- trimestres fiscales ---
    qaapl = json.loads(text(c.request("tools/call", {"name": "quarterly", "arguments": {"ticker": "AAPL"}})))
    check(
        "quarterly AAPL: 3 trimestres y sin huecos (red)",
        qaapl.get("available") == 3 and qaapl.get("gaps") == [],
        str(qaapl)[:250],
        critical=False,
    )
    check(
        "quarterly AAPL: trae ventas y crecimiento interanual (red)",
        qaapl.get("quarters")
        and all(q.get("revenue") for q in qaapl["quarters"])
        and any(q.get("revenue_yoy_pct") is not None for q in qaapl["quarters"]),
        str(qaapl.get("quarters"))[:250],
        critical=False,
    )

    qitx = json.loads(text(c.request("tools/call", {"name": "quarterly", "arguments": {"ticker": "ITX.MC"}})))
    check(
        "quarterly ITX.MC: detecta el hueco de enero (red)",
        bool(qitx.get("gaps")),
        str(qitx.get("gaps"))[:150],
        critical=False,
    )
    check(
        "quarterly ITX.MC: free_cash_flow en missing_fields (red)",
        "free_cash_flow" in (qitx.get("missing_fields") or []),
        str(qitx.get("missing_fields"))[:150],
        critical=False,
    )

    qbad = json.loads(text(c.request("tools/call", {
        "name": "quarterly", "arguments": {"ticker": "NOEXISTEXYZ.QQ"}})))
    check(
        "quarterly sin datos no rompe: error o disponible 0",
        "error" in qbad or qbad.get("available") == 0,
        str(qbad)[:150],
        critical=False,
    )

    est = json.loads(text(c.request("tools/call", {
        "name": "earnings_estimates", "arguments": {"ticker": "AAPL"}})))
    check(
        "earnings_estimates AAPL: 4 periodos con BPA y analistas (red)",
        "error" not in est and len(est.get("periods", [])) >= 3
        and all(p.get("eps") is not None for p in est["periods"][:3]),
        str(est)[:250],
        critical=False,
    )
    check(
        "earnings_estimates trae el precio objetivo y el numero de analistas",
        (est.get("target") or {}).get("mean") is not None,
        str(est.get("target"))[:150],
        critical=False,
    )
finally:
    c.close()

# --------------------------------------------------------------------- quarterly
# quarterly_derive is pure: the shaping of raw quarters is checked without network.
print("\n== trimestres: quarterly_derive (mercado_server) ==")
sys.path.insert(0, os.path.join(ROOT, "mcp"))
import mercado_server as mer  # noqa: E402

GROWING = [
    {"period_end": "2026-06-30", "revenue": 110, "net_income": 22, "operating_income": 30,
     "diluted_eps": 1.20, "free_cash_flow": 20, "currency": "USD"},
    {"period_end": "2026-03-31", "revenue": 105, "net_income": 20, "operating_income": 28,
     "diluted_eps": 1.10, "free_cash_flow": 18, "currency": "USD"},
    {"period_end": "2025-12-31", "revenue": 100, "net_income": 18, "operating_income": 26,
     "diluted_eps": 1.00, "free_cash_flow": 16, "currency": "USD"},
    {"period_end": "2025-06-30", "revenue": 90, "net_income": 15, "operating_income": 24,
     "diluted_eps": 0.90, "free_cash_flow": 12, "currency": "USD"},
]
g = mer.quarterly_derive(GROWING, "TEST")
check("quarterly_derive: 3 trimestres, sin huecos ni campos ausentes",
      g["available"] == 3 and g["gaps"] == [] and g["missing_fields"] == [], str(g)[:250])
check("quarterly_derive: ordena del mas nuevo al mas viejo",
      [q["period_end"] for q in g["quarters"]] == ["2026-06-30", "2026-03-31", "2025-12-31"],
      str([q["period_end"] for q in g["quarters"]]))
check("quarterly_derive: calcula el crecimiento interanual contra el mismo trimestre del ano anterior",
      g["quarters"][0]["revenue_yoy_pct"] == 22.22
      and g["quarters"][0]["eps_yoy_pct"] == 33.33
      and g["quarters"][0]["net_income_yoy_pct"] == 46.67, str(g["quarters"][0]))
check("quarterly_derive: margen neto en puntos",
      g["quarters"][0]["net_margin_pct"] == 20.0, str(g["quarters"][0]["net_margin_pct"]))
check("quarterly_derive: sin trimestre base el interanual es null, no una division rara",
      g["quarters"][1]["revenue_yoy_pct"] is None and g["quarters"][2]["revenue_yoy_pct"] is None,
      str([q["revenue_yoy_pct"] for q in g["quarters"]]))

GAP = [
    {"period_end": "2026-04-30", "revenue": 100, "net_income": 10, "operating_income": 12,
     "diluted_eps": 0.90, "free_cash_flow": None, "currency": "EUR"},
    {"period_end": "2025-10-31", "revenue": 90, "net_income": 9, "operating_income": 11,
     "diluted_eps": 0.80, "free_cash_flow": None, "currency": "EUR"},
    {"period_end": "2025-07-31", "revenue": 85, "net_income": 8, "operating_income": 10,
     "diluted_eps": 0.70, "free_cash_flow": None, "currency": "EUR"},
    {"period_end": "2025-04-30", "revenue": 80, "net_income": 7, "operating_income": 9,
     "diluted_eps": 0.60, "free_cash_flow": None, "currency": "EUR"},
]
h = mer.quarterly_derive(GAP, "ITX.MC")
check("quarterly_derive: hueco de un trimestre -> available 2 y el par de fechas en gaps",
      h["available"] == 2 and h["gaps"] == [["2025-10-31", "2026-04-30"]], str(h)[:250])
check("quarterly_derive: elquarters no cruza el hueco hacia el cuarto trimestre",
      [q["period_end"] for q in h["quarters"]] == ["2026-04-30", "2025-10-31"],
      str([q["period_end"] for q in h["quarters"]]))
check("quarterly_derive: campo sin dato en los 3 va a missing_fields",
      h["missing_fields"] == ["free_cash_flow"], str(h["missing_fields"]))
check("quarterly_derive: note_es explica el hueco",
      "Solo 2 de 3" in h["note_es"] and "2025-10-31" in h["note_es"], h["note_es"])

OLDEST = [  # falta la ranura mas antigua: los tres ultimos son 2026-07, 2026-04, 2026-01
    {"period_end": "2026-07-31", "revenue": 100, "net_income": 10, "operating_income": 12,
     "diluted_eps": 0.90, "free_cash_flow": 5, "currency": "EUR"},
    {"period_end": "2026-04-30", "revenue": 95, "net_income": 9, "operating_income": 11,
     "diluted_eps": 0.80, "free_cash_flow": 4, "currency": "EUR"},
]
o = mer.quarterly_derive(OLDEST, "TEST")
check("quarterly_derive: hueco en la ranura mas antigua se reporta igual",
      o["available"] == 2 and o["gaps"] == [["2026-01-31", "2026-04-30"]], str(o)[:250])

e = mer.quarterly_derive([], "ZZZZ")
check("quarterly_derive: sin datos -> available 0 y nota legible",
      e["available"] == 0 and "Sin datos trimestrales" in e["note_es"], str(e))

LOSS = [
    {"period_end": "2026-06-30", "revenue": 100, "net_income": -5, "operating_income": -2,
     "diluted_eps": -0.10, "free_cash_flow": -3, "currency": "USD"},
    {"period_end": "2025-06-30", "revenue": 100, "net_income": -10, "operating_income": -4,
     "diluted_eps": -0.20, "free_cash_flow": -5, "currency": "USD"},
]
lo = mer.quarterly_derive(LOSS, "TEST")
check("quarterly_derive: no calcula crecimiento sobre base negativa (BPA y beneficio)",
      lo["quarters"][0]["eps_yoy_pct"] is None and lo["quarters"][0]["net_income_yoy_pct"] is None,
      str(lo["quarters"][0]))
check("quarterly_derive: con base positiva el crecimiento si se calcula, aunque el valor sea negativo",
      lo["quarters"][0]["revenue_yoy_pct"] == 0.0, str(lo["quarters"][0]["revenue_yoy_pct"]))

# ------------------------------------------------- preguntar a Claude (HU-32)
print("\n== contexto para Claude (app/llm.py) ==")
sys.path.insert(0, os.path.join(ROOT, "app"))
import llm  # noqa: E402

DETAIL = {
    "ticker": "ITX.MC",
    "quote": {"price": 53.26, "change_pct": -0.8, "currency": "EUR", "status": "CLOSED",
              "name": "Industria de Diseno Textil"},
    "fundamentals": {
        "name": "Industria de Diseno Textil", "sector": "Consumer Cyclical",
        "industry": "Apparel Retail", "country": "Spain",
        "valuation": {"per_trailing": 22.4, "per_forward": 19.1, "ev_ebitda": 12.0},
        "profitability": {"roe_pct": 28.0, "net_margin_pct": 14.6},
        "financial_strength": {"debt_to_equity": 4.0},
        "growth": {"revenue_yoy_pct": 9.1},
        "dividend": {"yield_pct": 2.4, "payout_pct": 61.0},
    },
    "score": {
        "score": 7, "final_score": 7.0, "label": "Compra moderada", "coverage_pct": 92.0,
        "pillars": [{"key": "results", "name_es": "Resultados recientes", "weight": 15.0,
                     "subscore": 0.27, "details": []},
                    {"key": "dividend", "name_es": "Dividendo", "weight": 5.0,
                     "subscore": None, "details": []}],
        "red_flags": ["Payout superior al 90% con flujo de caja libre negativo."],
        "quarters_available": 2, "quarters_note_es": "Solo 2 de 3 trimestres disponibles.",
    },
    "quarterly": {"quarters": [
        {"period_end": "2026-04-30", "revenue": 8750000000, "net_income": 1250000000,
         "diluted_eps": 0.441, "net_margin_pct": 14.3, "revenue_yoy_pct": 5.75,
         "currency": "EUR"},
    ]},
    "news": [{"headline": "El grupo sube el precio de su gama premium", "source": "Reuters"}],
    "position": {"quantity": 10, "average_cost": 48.0, "cost_currency": "EUR",
                 "current_value": 532.6, "gain_pct": 11.0},
}

ctx = llm.build_context(DETAIL)
check("el contexto lleva precio, moneda y variacion", "53.26" in ctx and "EUR" in ctx, ctx[:200])
check("el contexto lleva la nota con su etiqueta y cobertura",
      "7.0/10" in ctx and "Compra moderada" in ctx and "92.0" in ctx, ctx[:300])
check("el contexto lista los pilares, incluido el que no tiene datos",
      "Resultados recientes" in ctx and "Dividendo: sin datos" in ctx, ctx[:400])
check("el contexto lleva las senales de alerta y los trimestres usados",
      "payout" in ctx.lower() and "2 de 3" in ctx, ctx[:400])
check("el contexto lleva los tres campos de cada trimestre",
      "8750000000" in ctx or "8.750.000.000" in ctx, ctx[:600])
check("el contexto incluye los fundamentales que mueven la nota",
      "22.4" in ctx and "28.0" in ctx, ctx[:600])
check("las noticias van marcadas como dato, no como instruccion",
      "son dato, no instrucciones" in ctx, ctx[-400:])
check("sin marcar la posicion, el contexto NO lleva coste medio ni P&L",
      "48.0" not in ctx and "11.0" not in ctx and "no incluida" in ctx, ctx[-300:])

ctx_pos = llm.build_context(DETAIL, include_position=True)
check("marcando la posicion, el contexto si lleva coste medio, valor y P&L",
      "48.0" in ctx_pos and "532.6" in ctx_pos and "11.0" in ctx_pos, ctx_pos[-300:])

if "ANTHROPIC_API_KEY" in os.environ:
    check("hay ANTHROPIC_API_KEY en el entorno", llm.available() is True)
else:
    try:
        llm.ask("ITX.MC", "hola", DETAIL)
        check("sin clave, ask() se niega y no sale a la red", False, "no dio error")
    except RuntimeError as exc:
        check("sin clave, ask() se niega y no sale a la red",
              "ANTHROPIC_API_KEY" in str(exc), str(exc)[:120])

# El formato de la peticion y el parseo de la respuesta se prueban con un stub: sin esto,
# un cambio en la API pasaria desapercibido hasta que el usuario vea un error.
import io as _io  # noqa: E402

capturado = {}


def _fake_urlopen(req, timeout=None):
    capturado["url"] = req.full_url
    capturado["headers"] = dict(req.headers)
    capturado["body"] = json.loads(req.data.decode("utf-8"))
    # Como la API real: el bloque de busqueda solo aparece si se pidió la herramienta.
    bloques = [{"type": "text", "text": "Con los datos de la ficha, PER 22 sobre un sector caro."}]
    if capturado["body"].get("tools"):
        bloques.insert(0, {"type": "server_tool_use", "id": "x", "name": "web_search"})
        bloques.insert(1, {"type": "web_search_tool_result", "tool_use_id": "x", "content": []})
    return _io.BytesIO(json.dumps({"model": "claude-test", "content": bloques}).encode("utf-8"))


os.environ["ANTHROPIC_API_KEY"] = "sk-test-no-real"
real_urlopen, llm.urllib.request.urlopen = llm.urllib.request.urlopen, _fake_urlopen
try:
    res = llm.ask("ITX.MC", "¿Esta cara ya?", DETAIL, include_position=False, web_search=True)
    body = capturado["body"]
    check("la peticion va a la API de mensajes con la clave en la cabecera",
          capturado["url"].endswith("/v1/messages")
          and capturado["headers"].get("X-api-key") == "sk-test-no-real", capturado["url"])
    check("la peticion incluye el contexto con los numeros y la pregunta",
          "53.26" in body["messages"][0]["content"] and "¿Esta cara ya?" in body["messages"][0]["content"])
    check("la posicion NO viaja si no la pides",
          "48.0" not in body["messages"][0]["content"], "")
    check("la busqueda web se pide como herramienta del servidor",
          body["tools"][0]["type"].startswith("web_search"), str(body.get("tools")))
    check("la respuesta se arma con los bloques de texto y se marca que busco",
          res["answer"].startswith("Con los datos") and res["searched"] is True, str(res)[:200])

    res_sin = llm.ask("ITX.MC", "hola", DETAIL, web_search=False)
    check("sin busqueda no se pide la herramienta",
          "tools" not in capturado["body"] and res_sin["searched"] is False, "")
    check("con posicion marcada, el contexto si la lleva",
          "48.0" in llm.build_context(DETAIL, include_position=True), "")
finally:
    llm.urllib.request.urlopen = real_urlopen
    del os.environ["ANTHROPIC_API_KEY"]

# ---------------------------------------------------------------------- scoring
# Pure module, no network: exercised directly instead of through a subprocess.
print("\n== motor de puntuacion (scoring.py) ==")
sys.path.insert(0, os.path.join(ROOT, "mcp"))
import scoring  # noqa: E402
import finanzas_server as finsrv  # noqa: E402

# review_status es pura y decide el estado de una revision: se comprueba con fechas fijas.
check("review_status: caducada al pasar su fecha",
      finsrv.review_status({"expires_at": "2020-01-01", "last_quarter_seen": "2020-04-30"}) == "expired",
      str(finsrv.review_status({"expires_at": "2020-01-01"})))
check("review_status: obsoleta si hay trimestre mas nuevo que el visto",
      finsrv.review_status({"expires_at": "2099-01-01", "last_quarter_seen": "2026-03-31"},
                           latest_quarter_end="2026-09-30") == "stale")
check("review_status: vigente sin caducidad y sin trimestre nuevo",
      finsrv.review_status({"expires_at": "2099-01-01", "last_quarter_seen": "2026-09-30"},
                           latest_quarter_end="2026-09-30") == "active")
check("review_status: la caducidad gana a la obsolescencia",
      finsrv.review_status({"expires_at": "2020-01-01", "last_quarter_seen": "2020-04-30"},
                           latest_quarter_end="2026-09-30") == "expired")


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

# ------------------------------------------------- resultados recientes (HU-02)
print("\n== pilar de resultados recientes ==")

check("BASE_WEIGHTS incluye el pilar results en los tres perfiles",
      all("results" in w for w in scoring.BASE_WEIGHTS.values()),
      str(list(scoring.BASE_WEIGHTS)))
check("los pesos base suman 100 en los tres perfiles",
      all(sum(w.values()) == 100 for w in scoring.BASE_WEIGHTS.values()),
      str({k: sum(w.values()) for k, w in scoring.BASE_WEIGHTS.items()}))
check("BASE_WEIGHTS coincide con la tabla del plan (moderate 25/25/15/8/7/15/5, "
      "conservative 25/20/25/5/5/10/10, aggressive 20/20/10/15/20/15/0)",
      scoring.BASE_WEIGHTS == {
          "moderate": {"quality": 25, "valuation": 25, "health": 15, "growth": 8,
                       "momentum": 7, "results": 15, "dividend": 5},
          "conservative": {"quality": 25, "valuation": 20, "health": 25, "growth": 5,
                           "momentum": 5, "results": 10, "dividend": 10},
          "aggressive": {"quality": 20, "valuation": 20, "health": 10, "growth": 15,
                         "momentum": 20, "results": 15, "dividend": 0},
      },
      str(scoring.BASE_WEIGHTS))
check("la preferencia dividend sube el peso del dividendo y la growth lo quita",
      scoring._adjust_weights("moderate", "long", "dividend")["dividend"]
      > scoring.BASE_WEIGHTS["moderate"]["dividend"]
      and scoring._adjust_weights("moderate", "long", "growth")["dividend"] == 0,
      str({p: scoring._adjust_weights("moderate", "long", p)["dividend"] for p in ("dividend", "growth", "balanced")}))
check("el horizonte corto sube el momentum y el largo lo baja",
      scoring._adjust_weights("moderate", "short", "balanced")["momentum"]
      > scoring._adjust_weights("moderate", "long", "balanced")["momentum"],
      str({h: round(scoring._adjust_weights("moderate", h, "balanced")["momentum"], 1)
           for h in ("short", "medium", "long")}))

RISING = {
    "available": 3,
    "quarters": [
        {"period_end": "2026-06-30", "revenue": 110, "diluted_eps": 1.20, "net_income_yoy_pct": 40.0,
         "revenue_yoy_pct": 22.2, "eps_yoy_pct": 33.3, "net_margin_pct": 20.0},
        {"period_end": "2026-03-31", "revenue": 105, "diluted_eps": 1.10, "net_income_yoy_pct": 30.0,
         "revenue_yoy_pct": 16.7, "eps_yoy_pct": 22.2, "net_margin_pct": 19.0},
        {"period_end": "2025-12-31", "revenue": 100, "diluted_eps": 1.00, "net_income_yoy_pct": 25.0,
         "revenue_yoy_pct": 11.1, "eps_yoy_pct": 11.1, "net_margin_pct": 18.0},
    ],
    "earnings_surprise_pct": [4.0, 2.0, 6.0],
}
FALLING = {
    "available": 3,
    "quarters": [
        {"period_end": "2026-06-30", "revenue": 90, "diluted_eps": 0.60, "net_income_yoy_pct": -25.0,
         "revenue_yoy_pct": -10.0, "eps_yoy_pct": -33.3, "net_margin_pct": 8.0},
        {"period_end": "2026-03-31", "revenue": 95, "diluted_eps": 0.70, "net_income_yoy_pct": -15.0,
         "revenue_yoy_pct": -5.0, "eps_yoy_pct": -22.2, "net_margin_pct": 10.0},
        {"period_end": "2025-12-31", "revenue": 100, "diluted_eps": 0.90, "net_income_yoy_pct": -5.0,
         "revenue_yoy_pct": -2.0, "eps_yoy_pct": -10.0, "net_margin_pct": 12.0},
    ],
    "earnings_surprise_pct": [-4.0, -1.0],
}


def results_pillar(quarterly, fundamentals=None):
    res = scoring.score_stock({"price": 100}, fundamentals or HEALTHY["fundamentals"],
                              HEALTHY["historical"], quarterly=quarterly)
    return next(p for p in res["pillars"] if p["key"] == "results"), res


rising_pillar, rising_score = results_pillar(RISING)
falling_pillar, falling_score = results_pillar(FALLING)
check("empresa con trimestres crecientes saca pilar de resultados positivo",
      rising_pillar["subscore"] and rising_pillar["subscore"] > 0, str(rising_pillar["subscore"]))
check("empresa con trimestres cayendo saca pilar de resultados negativo",
      falling_pillar["subscore"] and falling_pillar["subscore"] < 0, str(falling_pillar["subscore"]))
check("el pilar positivo sube la nota y el negativo la baja",
      rising_score["score"] > falling_score["score"],
      f"{rising_score['score']} vs {falling_score['score']}")
check("cada metrica del pilar aparece en details con valor, subnota y unidad",
      {d["metric"] for d in rising_pillar["details"]} ==
      {"rev_yoy_avg", "eps_yoy_avg", "margin_trend", "consistency", "surprise"}
      and all(d["points"] is not None and d["reason_es"] for d in rising_pillar["details"]),
      str([d["metric"] for d in rising_pillar["details"]]))

TWO_OF_THREE = {"available": 2, "quarters": RISING["quarters"][:2]}
two_pillar, two_score = results_pillar(TWO_OF_THREE)
check("con 2 de 3 trimestres el pilar se calcula con lo que hay",
      two_pillar["subscore"] is not None and len(two_pillar["details"]) >= 3, str(two_pillar))
check("con 2 trimestres la consistencia compara solo el par que existe",
      next(d for d in two_pillar["details"] if d["metric"] == "consistency")["value"] == 1.0,
      str(two_pillar["details"]))
check("con 2 de 3 trimestres los details lo dicen, y no mueve la subnota",
      any(d["metric"] == "quarters_available" and d["points"] == 0.0 for d in two_pillar["details"])
      and two_pillar["subscore"] == results_pillar(
          {"available": 2, "quarters": RISING["quarters"][:2]})[0]["subscore"],
      str([d["metric"] for d in two_pillar["details"]]))

none_pillar, none_score = results_pillar({"available": 0, "quarters": []})
check("sin trimestres el pilar se excluye (subscore None)",
      none_pillar["subscore"] is None and none_pillar["details"] == [], str(none_pillar))
check("sin trimestres su peso se redistribuye y la cobertura baja",
      none_score["coverage_pct"] < rising_score["coverage_pct"]
      and abs(sum(none_score["weights"].values()) - 100) < 0.5,
      f"{none_score['coverage_pct']} vs {rising_score['coverage_pct']}")

LOSS_BASE = {"available": 3, "quarters": [
    {"period_end": "2026-06-30", "revenue": 100, "diluted_eps": -0.50, "net_income_yoy_pct": None,
     "revenue_yoy_pct": None, "eps_yoy_pct": None, "net_margin_pct": -5.0},
    {"period_end": "2026-03-31", "revenue": 110, "diluted_eps": -0.40, "net_income_yoy_pct": None,
     "revenue_yoy_pct": None, "eps_yoy_pct": None, "net_margin_pct": -4.0},
    {"period_end": "2025-12-31", "revenue": 120, "diluted_eps": -0.30, "net_income_yoy_pct": None,
     "revenue_yoy_pct": None, "eps_yoy_pct": None, "net_margin_pct": -3.0},
]}
loss_pillar, _ = results_pillar(LOSS_BASE)
loss_metrics = {d["metric"] for d in loss_pillar["details"]}
check("interanual null (base negativa) omite esas metricas sin division rara",
      loss_pillar["subscore"] is not None
      and not ({"rev_yoy_avg", "eps_yoy_avg"} & loss_metrics), str(loss_pillar))
check("con 1 solo trimestre y sin interanual no queda nada puntuable",
      results_pillar({"available": 1, "quarters": [LOSS_BASE["quarters"][0]]})[0]["subscore"] is None)

check("score_stock sigue siendo pura: results no aparece sin quarterly",
      next(p for p in scoring.score_stock({}, {}, {})["pillars"] if p["key"] == "results")["subscore"] is None)

# ---------------------------------------------- proyecciones de analistas (HU-33)
print("\n== proyecciones: parse_estimates ==")

TREND_PAYLOAD = {"quoteSummary": {"result": [{
    "earningsTrend": {"trend": [
        {"period": "0q", "endDate": "2026-09-30",
         "earningsEstimate": {"avg": {"raw": 1.98}, "low": {"raw": 1.9}, "high": {"raw": 2.1},
                              "numberOfAnalysts": {"raw": 27}},
         "revenueEstimate": {"avg": {"raw": 113624521680}, "growth": {"raw": 0.1089}},
         "epsRevisions": {"upLast30days": {"raw": 7}, "downLast30days": {"raw": 14}}},
        {"period": "+1q", "endDate": "2026-12-31",
         "earningsEstimate": {"avg": {"raw": 2.90}, "numberOfAnalysts": {"raw": 21}},
         "revenueEstimate": {"avg": {"raw": 154392842060}, "growth": {"raw": 0.074}},
         "epsRevisions": {"upLast30days": {"raw": 1}, "downLast30days": {"raw": 1}}},
        {"period": "0y", "endDate": "2026-09-30",
         "earningsEstimate": {"avg": {"raw": 8.82}, "numberOfAnalysts": {"raw": 39}},
         "revenueEstimate": {"avg": {"raw": 477832817030}, "growth": {"raw": 0.148}}},
        {"period": "+1y", "endDate": "2027-09-30",
         "earningsEstimate": {"avg": {"raw": 9.58}, "numberOfAnalysts": {"raw": 40}},
         "revenueEstimate": {"avg": {"raw": 527946029390}, "growth": {"raw": 0.105}}},
    ]},
    "financialData": {"targetMeanPrice": {"raw": 328.22}, "targetHighPrice": {"raw": 405.0},
                      "targetLowPrice": {"raw": 215.0}, "numberOfAnalystOpinions": {"raw": 39},
                      "recommendationKey": "buy"},
}]}}

e = mer.parse_estimates(TREND_PAYLOAD)
check("parse_estimates devuelve los 4 periodos con etiqueta en espanol",
      len(e["periods"]) == 4 and e["periods"][0]["period_es"] == "Trimestre actual",
      str([p["period_es"] for p in e["periods"]]))
check("parse_estimates pasa el BPA y el numero de analistas",
      e["periods"][0]["eps"] == 1.98 and e["periods"][0]["analysts"] == 27
      and e["periods"][0]["eps_low"] == 1.9, str(e["periods"][0]))
check("parse_estimates convierte el crecimiento a porcentaje",
      e["periods"][0]["revenue_growth_pct"] == 10.89, str(e["periods"][0]["revenue_growth_pct"]))
check("el balance de revisiones sale de los dos trimestres mas cercanos, no de los cuatro",
      e["revisions_up_30d"] == 8 and e["revisions_down_30d"] == 15
      and e["revisions_net_30d"] == -7, str(e["revisions_net_30d"]))
check("parse_estimates trae el precio objetivo con su rango",
      e["target"]["mean"] == 328.22 and e["target"]["low"] == 215.0
      and e["target"]["analysts"] == 39, str(e["target"]))
check("sin trend devuelve None en vez de inventar",
      mer.parse_estimates({"quoteSummary": {"result": [{}]}}) is None)
check("sin result devuelve None", mer.parse_estimates({}) is None)
check("las proyecciones avisan de que son consenso, no hechos",
      "sesgo" in e["note_es"], e["note_es"])

# El nivel del BPA estimado NO puntua: solo la revision, y con poco peso.
# Valoracion a media escala (PER 25, PEG 1.5) para que se note el efecto de las revisiones.
MEDIA = {
    "valuation": {"per_trailing": 25, "peg": 1.5},
    "financial_strength": {"debt_to_equity": 60},
    "dividend": {},
}
sin_est = scoring.score_stock({"price": 100}, MEDIA, HEALTHY["historical"])
con_est = scoring.score_stock({"price": 100}, MEDIA, HEALTHY["historical"],
                              estimates={"revisions_net_30d": 8, "periods": [
                                  {"eps": 99.0, "analysts": 40, "revenue": 5e11}]})
con_est_barato = scoring.score_stock({"price": 100}, MEDIA, HEALTHY["historical"],
                                     estimates={"revisions_net_30d": 8, "periods": [
                                         {"eps": 0.01, "analysts": 2, "revenue": 1e6}]})
val_base = next(p for p in sin_est["pillars"] if p["key"] == "valuation")
val_up = next(p for p in con_est["pillars"] if p["key"] == "valuation")
val_down = next(p for p in scoring.score_stock(
    {"price": 100}, MEDIA, HEALTHY["historical"],
    estimates={"revisions_net_30d": -8}).get("pillars") if p["key"] == "valuation")

check("el BPA estimado no puntua: mismo nivel de revisiones, nota igual",
      con_est["score"] == con_est_barato["score"] == sin_est["score"] + 1,
      f"{sin_est['score']} / {con_est['score']} / {con_est_barato['score']}")
check("sin estimaciones no aparece la metrica de revisiones",
      not any(d["metric"] == "estimate_revisions" for d in val_base["details"]),
      str([d["metric"] for d in val_base["details"]]))
check("revisiones al alza suben el pilar de valoracion y salen en sus detalles",
      val_up["subscore"] > val_base["subscore"]
      and any(d["metric"] == "estimate_revisions" for d in val_up["details"]),
      f"{val_base['subscore']} -> {val_up['subscore']}")
check("revisiones a la baja bajan el pilar de valoracion",
      val_down["subscore"] < val_base["subscore"], str(val_down["subscore"]))
check("el peso de las revisiones es debil: 15 puntos debia y la nota solo se mueve 1",
      val_up["subscore"] - val_base["subscore"] < 0.5
      and abs(sum(con_est["weights"].values()) - 100) < 0.5,
      f"subnota {val_base['subscore']} -> {val_up['subscore']}")

# ------------------------------------------- aviso de trimestres (HU-03)
print("\n== aviso de trimestres incompletos ==")

partial = results_pillar({"available": 2, "note_es": "Solo 2 de 3 trimestres disponibles: "
                         "hueco entre 2025-10-31 y 2026-04-30.", "quarters": RISING["quarters"][:2]})[1]
check("el resultado expone quarters_available y quarters_note_es",
      partial["quarters_available"] == 2 and "Solo 2 de 3" in partial["quarters_note_es"],
      str(partial["quarters_available"]))
check("el notice menciona los trimestres que faltan", "2 de 3 trimestres" in partial["notice"],
      partial["notice"][-120:])

full = results_pillar(RISING)[1]
check("con 3 trimestres el notice no avisa de quarters",
      full["quarters_available"] == 3 and "3 de 3 trimestres" not in full["notice"], full["notice"][-80:])

DECAY = {"available": 3, "quarters": [
    {"period_end": "2026-06-30", "revenue": 90, "diluted_eps": 0.60, "revenue_yoy_pct": -10.0,
     "eps_yoy_pct": -30.0, "net_margin_pct": 8.0},
    {"period_end": "2026-03-31", "revenue": 95, "diluted_eps": 0.70, "revenue_yoy_pct": -6.0,
     "eps_yoy_pct": -20.0, "net_margin_pct": 10.0},
    {"period_end": "2025-12-31", "revenue": 100, "diluted_eps": 0.90, "revenue_yoy_pct": -2.0,
     "eps_yoy_pct": -10.0, "net_margin_pct": 12.0},
]}
decay = results_pillar(DECAY)[1]
check("3 trimestres cayendo + margen a la baja salta la alerta de deterioro",
      any("deterioro constante" in f for f in decay["red_flags"]), str(decay["red_flags"]))
check("esa alerta limita la nota a 3", decay["score"] <= 3, str(decay["score"]))

no_margin_down = {"available": 3, "quarters": [
    {**DECAY["quarters"][0], "net_margin_pct": 14.0},
    {**DECAY["quarters"][1], "net_margin_pct": 12.0},
    DECAY["quarters"][2],
]}
check("ventas cayendo pero con margen al alza no salta la alerta",
      not any("deterioro" in f for f in results_pillar(no_margin_down)[1]["red_flags"]),
      str(results_pillar(no_margin_down)[1]["red_flags"]))
check("con 2 trimestres la alerta NO salta (falta evidencia)",
      not any("deterioro" in f for f in results_pillar(
          {"available": 2, "quarters": DECAY["quarters"][:2]})[1]["red_flags"]),
      str(results_pillar({"available": 2, "quarters": DECAY["quarters"][:2]})[1]["red_flags"]))

# ------------------------------------------------ nota final = + revision (HU-08)
print("\n== nota final: mecanica + revision ==")

ACTIVE = {"status": "active", "adjustment": 1.0, "rationale": "hecho", "sources": []}
base = results_pillar(RISING)[1]
# apply_review es pura sobre el dict que recibe: aqui se parte de una nota intermedia
# y sin alertas, para que los topes de 1 y 10 se puedan probar sin interferencias.
MECH6 = {"score": 6, "label": "Neutral / esperar", "sentiment": "ambar", "red_flags": []}

up = scoring.apply_review(MECH6, ACTIVE)
check("sin revision, final_score == score mecanica",
      scoring.apply_review(base, None)["final_score"] == base["score"])
check("revision positiva sube la nota final",
      up["score"] == 6 and up["final_score"] == 7 and up["review_applied"], f"{up['score']} -> {up['final_score']}")
check("revision negativa baja la nota final",
      scoring.apply_review(MECH6, {"status": "active", "adjustment": -1.0})["final_score"] == 5)

check("la nota final nunca pasa de 10",
      scoring.apply_review({**MECH6, "score": 9}, {"status": "active", "adjustment": 2})["final_score"] == 10)
check("la nota final nunca baja de 1",
      scoring.apply_review({**MECH6, "score": 1}, {"status": "active", "adjustment": -2})["final_score"] == 1)

flagged = results_pillar(DECAY)[1]
capped = scoring.apply_review(flagged, {"status": "active", "adjustment": 2})
check("una alerta vigente limita la nota final a 3 aunque la revision la suba",
      capped["final_score"] == 3, str(capped["final_score"]))

for status in ("expired", "stale"):
    skipped = scoring.apply_review(MECH6, {"status": status, "adjustment": 2})
    check(f"revision {status}: no se aplica pero se informa",
          skipped["final_score"] == 6 and skipped["review_applied"] is False
          and skipped["review"]["status"] == status, str(skipped))

sentiment_up = scoring.apply_review(MECH6, {"status": "active", "adjustment": 1.5})
check("label y sentiment se calculan sobre la nota final",
      sentiment_up["label"] == "Compra moderada" and sentiment_up["sentiment"] == "verde",
      f"{sentiment_up['label']} / {sentiment_up['sentiment']}")
check("la nota final conserva la mecanica a la vista",
      sentiment_up["score"] == 6 and sentiment_up["final_score"] == 7.5, str(sentiment_up))

combos_ok = True
for risk in ("conservative", "moderate", "aggressive"):
    for horizon in ("short", "medium", "long"):
        for preference in ("dividend", "growth", "balanced"):
            w = scoring._adjust_weights(risk, horizon, preference)
            if abs(sum(w.values()) - 100) > 0.01:
                combos_ok = False
check("los pesos del perfil suman 100 en las 27 combinaciones", combos_ok)

# ---------------------------------------------------------------- position_advice
print("\n== recomendaciones: position_advice ==")

MODERATE = {"risk_tolerance": "moderate", "horizon": "long", "preference": "balanced"}

advice = scoring.position_advice({"score": 2}, weight_pct=5, pnl_pct=-10, profile=MODERATE)
check("nota <= 3 sugiere vender", advice["action_key"] == "sell", str(advice))

advice = scoring.position_advice({"score": 4}, weight_pct=5, pnl_pct=-25, profile=MODERATE)
check("nota baja + perdida > 20% sugiere revisar tesis", advice["action_key"] == "review", str(advice))

advice = scoring.position_advice({"score": 6}, weight_pct=30, pnl_pct=5, profile=MODERATE)
check("peso sobre el limite moderado (25%) sugiere no ampliar", advice["action_key"] == "no_add", str(advice))

advice = scoring.position_advice({"score": 5}, weight_pct=30, pnl_pct=5, profile=MODERATE)
check("no ampliar + nota mediocre (<=5) anade razon de reducir",
      advice["action_key"] == "no_add" and any("reducir" in r for r in advice["reasons_es"]),
      str(advice))

advice = scoring.position_advice({"score": 9}, weight_pct=10, pnl_pct=15, profile=MODERATE)
check("nota alta y sin concentracion sugiere mantener o ampliar", advice["action_key"] == "add", str(advice))

advice = scoring.position_advice({"score": 6}, weight_pct=10, pnl_pct=2, profile=MODERATE)
check("caso neutro sugiere mantener", advice["action_key"] == "hold", str(advice))

advice_cons = scoring.position_advice({"score": 6}, weight_pct=22,
                                       pnl_pct=1, profile={"risk_tolerance": "conservative"})
advice_agre = scoring.position_advice({"score": 6}, weight_pct=22,
                                       pnl_pct=1, profile={"risk_tolerance": "aggressive"})
check("limite de concentracion depende del perfil (22% dispara en conservador, no en agresivo)",
      advice_cons["action_key"] == "no_add" and advice_agre["action_key"] != "no_add",
      f"{advice_cons['action_key']} / {advice_agre['action_key']}")

advice = scoring.position_advice({"score": 6, "final_score": 2}, weight_pct=5, pnl_pct=0, profile=MODERATE)
check("position_advice sigue la nota final, no la mecanica", advice["action_key"] == "sell", str(advice))
advice = scoring.position_advice({"score": 2, "final_score": 8.2}, weight_pct=5, pnl_pct=0, profile=MODERATE)
check("una revision positiva puede subir la accion sugerida a mantener/ampliar",
      advice["action_key"] == "add", str(advice))

# ------------------------------------------------------------------- select_ideas
print("\n== recomendaciones: select_ideas ==")

CANDIDATES = [
    {"ticker": "AAA", "score": 9, "coverage_pct": 80, "sector": "Technology"},
    {"ticker": "BBB", "score": 8, "coverage_pct": 90, "sector": "Healthcare"},
    {"ticker": "CCC", "score": 7, "coverage_pct": 60, "sector": "Technology"},
    {"ticker": "DDD", "score": 6, "coverage_pct": 90, "sector": "Energy"},       # nota < 7
    {"ticker": "EEE", "score": 9, "coverage_pct": 40, "sector": "Financials"},   # cobertura < 50
    {"ticker": "FFF", "score": 10, "coverage_pct": 70, "sector": "Technology"},  # ya en cartera
]
ideas = scoring.select_ideas(CANDIDATES, held_tickers=["fff"], held_sectors={"Technology"})
tickers_ideas = [i["ticker"] for i in ideas]
check("excluye valores ya en cartera", "FFF" not in tickers_ideas, tickers_ideas)
check("excluye nota por debajo de 7", "DDD" not in tickers_ideas, tickers_ideas)
check("excluye cobertura por debajo de 50%", "EEE" not in tickers_ideas, tickers_ideas)
check("ordena por nota desc, luego cobertura desc", tickers_ideas == ["AAA", "BBB", "CCC"], tickers_ideas)
check("diversifica cuando el sector no esta ya en cartera",
      next(i["diversifies"] for i in ideas if i["ticker"] == "BBB"), str(ideas))
check("no diversifica cuando el sector ya esta en cartera",
      not next(i["diversifies"] for i in ideas if i["ticker"] == "AAA"), str(ideas))
check("sin posiciones no marca diversifica",
      not any(i["diversifies"] for i in scoring.select_ideas([{"ticker": "BBB", "score": 8, "coverage_pct": 100, "sector": "Energy"}], [], set())))

ordered = scoring.select_ideas(
    [{"ticker": "AAA", "score": 9, "final_score": 7.1, "coverage_pct": 70},
     {"ticker": "BBB", "score": 7, "final_score": 7.0, "coverage_pct": 90}], [], set())
check("select_ideas ordena y filtra por la nota final, no por la mecanica",
      [i["ticker"] for i in ordered] == ["AAA", "BBB"]
      and not any(i["ticker"] for i in scoring.select_ideas(
          [{"ticker": "CCC", "score": 9, "final_score": 6.9, "coverage_pct": 90}], [], set())),
      str(ordered))

# ------------------------------------------------------------------- universo.json
print("\n== recomendaciones: universo.json ==")

with open(os.path.join(ROOT, "recursos", "universo.json"), encoding="utf-8") as fh:
    universo = json.load(fh)

stocks = universo.get("stocks", [])
tickers_universo = [s["ticker"] for s in stocks]
check("universo.json tiene al menos 90 tickers unicos",
      len(set(tickers_universo)) >= 90, str(len(set(tickers_universo))))
check("universo.json sin tickers duplicados",
      len(tickers_universo) == len(set(tickers_universo)),
      str([t for t in set(tickers_universo) if tickers_universo.count(t) > 1]))
check("cada entrada trae nombre e indices",
      all(s.get("name") and s.get("indices") for s in stocks),
      str([s for s in stocks if not s.get("name") or not s.get("indices")][:3]))
check("universo.json declara fuente y fecha", bool(universo.get("source")) and bool(universo.get("as_of")),
      str({"source": universo.get("source"), "as_of": universo.get("as_of")}))

shutil.rmtree(TEST_DIR, ignore_errors=True)

print("\n" + ("TODO CORRECTO" if not failures else f"FALLOS CRITICOS: {failures}"))
sys.exit(1 if failures else 0)
