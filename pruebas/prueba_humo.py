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
    check(f"tools/list ({len(tools)} herramientas)", len(tools) >= 25)
    check("herramientas clave presentes", {"transaction_create", "portfolio_view", "account_update", "account_delete"} <= names)

    created = c.request(
        "tools/call",
        {
            "name": "transaction_create",
            "arguments": {
                "type": "expense",
                "amount": 12.5,
                "category": "prueba-humo",
                "description": "registro de prueba automatica",
            },
        },
    )
    data = json.loads(text(created))
    check("registrar movimiento", data.get("ok") is True, text(created))
    test_id = data.get("id")

    summary = json.loads(text(c.request("tools/call", {"name": "period_summary", "arguments": {}})))
    check("period_summary", "expenses" in summary, str(summary))

    # Validacion: un importe negativo debe rechazarse.
    bad = c.request(
        "tools/call",
        {
            "name": "transaction_create",
            "arguments": {"type": "expense", "amount": -5, "category": "prueba-humo"},
        },
    )
    check("rechaza importe negativo", bad["result"].get("isError") is True, text(bad))

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
        "tools/call", {"name": "sql_query", "arguments": {"sql": "DELETE FROM transactions"}}
    )
    check("sql_query bloquea escritura", bad_sql["result"].get("isError") is True)

    deleted = c.request("tools/call", {"name": "transaction_delete", "arguments": {"id": test_id}})
    check("eliminar movimiento devuelve el registro", "deleted" in json.loads(text(deleted)))

    # --- D2b: transaction_update ---
    created2 = json.loads(text(c.request("tools/call", {
        "name": "transaction_create",
        "arguments": {"type": "expense", "amount": 20, "category": "prueba-humo"},
    })))
    updated = json.loads(text(c.request("tools/call", {
        "name": "transaction_update",
        "arguments": {"id": created2["id"], "amount": 33.5, "category": "prueba-editada"},
    })))
    check(
        "transaction_update cambia importe y categoria",
        updated["after"]["amount"] == 33.5 and updated["after"]["category"] == "prueba-editada",
        str(updated),
    )
    c.request("tools/call", {"name": "transaction_delete", "arguments": {"id": created2["id"]}})

    # --- D2c: transactions_list pagination (offset/has_more) ---
    page_ids = []
    for i in range(5):
        r = json.loads(text(c.request("tools/call", {
            "name": "transaction_create",
            "arguments": {"type": "expense", "amount": 1 + i, "category": "prueba-paginacion"},
        })))
        page_ids.append(r["id"])
    page1 = json.loads(text(c.request("tools/call", {
        "name": "transactions_list",
        "arguments": {"category": "prueba-paginacion", "limit": 2, "offset": 0},
    })))
    check("pagina 1 devuelve 2 filas con has_more", page1["shown"] == 2 and page1["has_more"] is True, str(page1))
    page3 = json.loads(text(c.request("tools/call", {
        "name": "transactions_list",
        "arguments": {"category": "prueba-paginacion", "limit": 2, "offset": 4},
    })))
    check("ultima pagina sin has_more", page3["shown"] == 1 and page3["has_more"] is False, str(page3))
    for i in page_ids:
        c.request("tools/call", {"name": "transaction_delete", "arguments": {"id": i}})

    # --- transferencias entre cuentas propias ---
    for name in ("Prueba Corriente", "Prueba Ahorro"):
        c.request(
            "tools/call",
            {"name": "account_create", "arguments": {"name": name, "initial_balance": 1000}},
        )
    tr = c.request(
        "tools/call",
        {
            "name": "transfer_create",
            "arguments": {
                "from_account": "Prueba Corriente",
                "to_account": "Prueba Ahorro",
                "amount": 300,
            },
        },
    )
    check("registrar transferencia", json.loads(text(tr)).get("ok") is True, text(tr))

    balances = {
        x["name"]: x["balance"]
        for x in json.loads(text(c.request("tools/call", {"name": "accounts_list", "arguments": {}})))[
            "accounts"
        ]
    }
    check(
        "traspaso mueve ambos saldos",
        balances.get("Prueba Corriente") == 700 and balances.get("Prueba Ahorro") == 1300,
        str(balances),
    )

    res = json.loads(text(c.request("tools/call", {"name": "period_summary", "arguments": {}})))
    check(
        "traspaso NO cuenta como ingreso ni gasto",
        res["income"] == 0 and res["expenses"] == 0,
        f"income={res['income']} expenses={res['expenses']}",
    )

    same = c.request(
        "tools/call",
        {
            "name": "transfer_create",
            "arguments": {"from_account": "Prueba Ahorro", "to_account": "Prueba Ahorro", "amount": 10},
        },
    )
    check("rechaza traspaso a la misma cuenta", same["result"].get("isError") is True)

    # --- movimientos recurrentes ---
    rec = c.request(
        "tools/call",
        {
            "name": "recurring_create",
            "arguments": {
                "concept": "Nomina prueba",
                "type": "income",
                "amount": 2000,
                "category": "nomina",
                "frequency": "monthly",
                "day": 31,
                "start_date": "2026-01-31",
            },
        },
    )
    check("definir recurrente", json.loads(text(rec)).get("ok") is True, text(rec))

    sim = json.loads(
        text(
            c.request(
                "tools/call",
                {"name": "recurring_apply", "arguments": {"end_date": "2026-04-30"}},
            )
        )
    )
    check("simula 4 ocurrencias (ene-abr)", sim["pending"] == 4, str(sim["pending"]))
    check(
        "ajusta dia 31 a meses cortos",
        any(d["date"] == "2026-02-28" for d in sim["detail"]),
        str([d["date"] for d in sim["detail"]]),
    )
    check("simular no escribe nada", sim["simulation"] is True)

    applied = json.loads(
        text(
            c.request(
                "tools/call",
                {
                    "name": "recurring_apply",
                    "arguments": {"end_date": "2026-04-30", "simulate": False},
                },
            )
        )
    )
    check("aplica 4 movimientos", applied["pending"] == 4)

    repeated = json.loads(
        text(
            c.request(
                "tools/call",
                {
                    "name": "recurring_apply",
                    "arguments": {"end_date": "2026-04-30", "simulate": False},
                },
            )
        )
    )
    check("idempotente: no duplica", repeated["pending"] == 0, str(repeated["pending"]))

    # --- account_update ---
    accounts = json.loads(text(c.request("tools/call", {"name": "accounts_list", "arguments": {}})))["accounts"]
    acc_id = next(a["id"] for a in accounts if a["name"] == "Prueba Corriente")

    edited = json.loads(text(c.request("tools/call", {
        "name": "account_update",
        "arguments": {"id": acc_id, "name": "PC Editada", "type": "investment"},
    })))
    check("account_update cambia nombre y tipo", edited["after"]["name"] == "PC Editada" and edited["after"]["type"] == "investment", str(edited))

    after_edit = json.loads(text(c.request("tools/call", {"name": "accounts_list", "arguments": {}})))["accounts"]
    check("account_update persiste", next(a["name"] for a in after_edit if a["id"] == acc_id) == "PC Editada")

    # --- account_delete (borrado fisico) ---
    # Crear cuenta temporal con un movimiento para probar cascada
    c.request("tools/call", {"name": "account_create", "arguments": {"name": "Para Borrar", "initial_balance": 50}})
    c.request("tools/call", {"name": "transaction_create", "arguments": {
        "type": "expense", "amount": 1, "category": "prueba-borrado", "account": "Para Borrar"}})
    tmp = next(a["id"] for a in json.loads(text(
        c.request("tools/call", {"name": "accounts_list", "arguments": {}})))["accounts"] if a["name"] == "Para Borrar")

    deleted_account = json.loads(text(c.request("tools/call", {"name": "account_delete", "arguments": {"id": tmp}})))
    check("account_delete borra la cuenta", deleted_account["account"]["name"] == "Para Borrar", str(deleted_account))

    after_delete = json.loads(text(c.request("tools/call", {"name": "accounts_list", "arguments": {}})))["accounts"]
    check("account_delete desaparece de listar", all(a["id"] != tmp for a in after_delete))

    txs = json.loads(text(c.request("tools/call", {
        "name": "transactions_list", "arguments": {"account": "Para Borrar"}})))
    check("account_delete cascada: borra movimientos", txs["shown"] == 0)
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

shutil.rmtree(TEST_DIR, ignore_errors=True)

print("\n" + ("TODO CORRECTO" if not failures else f"FALLOS CRITICOS: {failures}"))
sys.exit(1 if failures else 0)
