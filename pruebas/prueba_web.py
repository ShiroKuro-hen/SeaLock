"""Web app test: launches the server and exercises the real API.

Also checks the defenses: no writes without a token, and requests with a
non-local Host or Origin are rejected.

Usage:  python pruebas/prueba_web.py
"""

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEST_DIR = tempfile.mkdtemp(prefix="sealock-web-")


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


PORT = free_port()
BASE = f"http://127.0.0.1:{PORT}"

failures = []


def check(label, condition, detail="", critical=True):
    if condition:
        print(f"  OK    {label}")
    elif critical:
        print(f"  FALLO {label}  {detail}")
        failures.append(label)
    else:
        print(f"  AVISO {label}  {detail}")


def request(path, data=None, token=None, headers=None):
    body = json.dumps(data or {}).encode() if data is not None else None
    req = urllib.request.Request(BASE + path, data=body, method="POST" if body else "GET")
    if body:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("X-SeaLock-Token", token)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


proc = subprocess.Popen(
    [sys.executable, "-u", os.path.join(ROOT, "app", "servidor.py")],
    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", cwd=ROOT,
    env={**os.environ, "SEALOCK_DATA": TEST_DIR, "SEALOCK_PORT": str(PORT),
         "SEALOCK_NO_OPEN": "1", "PYTHONIOENCODING": "utf-8"},
)

try:
    print(f"\n== servidor web (puerto {PORT}) ==")
    for _ in range(60):
        try:
            with socket.create_connection(("127.0.0.1", PORT), timeout=0.5):
                break
        except OSError:
            if proc.poll() is not None:
                print("El servidor murio al arrancar:\n", proc.stdout.read())
                sys.exit(1)
            time.sleep(0.25)

    # --- pagina y token ---
    code, html = request("/")
    check("sirve la pagina principal", code == 200 and "SeaLock" in html)
    m = re.search(r'token:\s*"([^"]+)"', html)
    check("inyecta el token en la pagina", bool(m))
    token = m.group(1) if m else ""
    check("no queda el marcador sin sustituir", "{{TOKEN}}" not in html)

    for file, mark in (("/estilo.css", "--acento"), ("/app.js", "async function api")):
        code, body = request(file)
        check(f"sirve {file}", code == 200 and mark in body)

    # --- defensas ---
    code, _ = request("/api/finance/accounts_list", {})
    check("rechaza API sin token", code == 403, f"codigo={code}")

    code, _ = request("/api/finance/accounts_list", {}, token="equivocado")
    check("rechaza token incorrecto", code == 403, f"codigo={code}")

    code, _ = request("/api/finance/accounts_list", {}, token=token,
                       headers={"Origin": "https://malicioso.example"})
    check("rechaza Origin externo (CSRF)", code == 403, f"codigo={code}")

    code, _ = request("/api/finance/accounts_list", {}, token=token,
                       headers={"Host": "atacante.example"})
    check("rechaza Host externo (DNS rebinding)", code == 403, f"codigo={code}")

    code, _ = request("/api/finance/no_existe", {}, token=token)
    check("404 en herramienta inexistente", code == 404, f"codigo={code}")

    # --- flujo real ---
    def call(path, data):
        code, body = request(path, data, token=token)
        return code, json.loads(body)

    code, r = call("/api/finance/account_create", {"name": "Banco", "initial_balance": 2000})
    check("crea cuenta via API", code == 200 and r["data"]["ok"], str(r))

    code, r = call("/api/finance/transaction_create",
                    {"type": "expense", "amount": 55.4, "category": "alimentacion",
                     "account": "Banco", "description": "compra semanal"})
    check("registra movimiento via API", code == 200 and r["data"]["ok"], str(r))

    code, r = call("/api/finance/transaction_create",
                    {"type": "expense", "amount": -5, "category": "x"})
    check("propaga error de validacion como 400", code == 400 and "error" in r, str(r))

    code, r = call("/api/app/dashboard", {})
    p = r["data"]
    check("dashboard agrega patrimonio", code == 200 and p["net_worth"] == 1944.6,
          f"net_worth={p.get('net_worth')}")
    check("dashboard trae serie de evolucion", isinstance(p["evolution"], list))
    check("dashboard trae resumen del mes", p["month"]["expenses"] == 55.4, str(p["month"]["expenses"]))

    code, r = call("/api/finance/trade_create",
                    {"ticker": "AAPL", "action": "buy", "quantity": 5, "price": 150})
    check("registra operacion via API", code == 200 and r["data"]["ok"], str(r))

    code, r = call("/api/app/portfolio_valued", {})
    pos = r["data"]["positions"]
    check("cartera valorada devuelve la posicion", code == 200 and len(pos) == 1, str(r)[:200])
    has_price = pos and pos[0].get("price")
    check("cartera trae precio de mercado (red)", bool(has_price),
          str(pos[0] if pos else ""), critical=False)
    if has_price:
        check("calcula peso y P&L", pos[0]["weight_pct"] == 100.0 and pos[0]["gain"] is not None,
              str(pos[0]), critical=False)

    code, r = call("/api/app/stock_detail", {"ticker": "AAPL"})
    f = r["data"]
    check("ficha completa (red)",
          code == 200 and f["quote"].get("price") and "dcf" in f,
          str(r)[:200], critical=False)

    code, r = call("/api/finance/transfer_create",
                    {"from_account": "Banco", "to_account": "Banco", "amount": 10})
    check("errores de negocio llegan al cliente", code == 400, str(r))

    # B4: todo label con data-glosario en app.js tiene entrada en glosario.js
    with open(os.path.join(ROOT, "app", "web", "app.js"), encoding="utf-8") as f:
        app_js = f.read()
    with open(os.path.join(ROOT, "app", "web", "glosario.js"), encoding="utf-8") as f:
        glosario_js = f.read()
    ui_labels = set(re.findall(r'data-glosario="([^{"]+)"', app_js))
    ui_labels.update(re.findall(r"metrica\(\s*[\"']([^\"'$]+)[\"']", app_js))
    glossary_aliases = set()
    for m in re.finditer(r"alias:\s*\[(.*?)\]", glosario_js):
        glossary_aliases.update(re.findall(r'"([^"]+)"', m.group(1)))
    for m in re.finditer(r"titulo:\s*\"([^\"]+)\"", glosario_js):
        glossary_aliases.add(m.group(1))
    missing = {e.lower() for e in ui_labels} - {a.lower() for a in glossary_aliases}
    check("B4: todo label data-glosario tiene entrada en glosario",
          not missing, f"Faltan: {missing}" if missing else "")

    # --- account_update ---
    listed = call("/api/finance/accounts_list", {})[1]["data"]
    bank_id = next(a["id"] for a in listed["accounts"] if a["name"] == "Banco")

    code, r = call("/api/finance/account_update", {"id": bank_id, "name": "Banco Editado", "type": "investment"})
    check("account_update via API", code == 200 and r["data"]["ok"], str(r))

    listed2 = call("/api/finance/accounts_list", {})[1]["data"]
    check("account_update persiste", next(a["name"] for a in listed2["accounts"] if a["id"] == bank_id) == "Banco Editado")

    # --- account_delete ---
    call("/api/finance/account_create", {"name": "TMP", "initial_balance": 1})
    tmp_id = next(a["id"] for a in call("/api/finance/accounts_list", {})[1]["data"]["accounts"] if a["name"] == "TMP")
    code, r = call("/api/finance/account_delete", {"id": tmp_id})
    check("account_delete via API", code == 200 and r["data"]["ok"], str(r))
    after = call("/api/finance/accounts_list", {})[1]["data"]
    check("account_delete desaparece", all(a["id"] != tmp_id for a in after["accounts"]))

    # --- D2b/D2c: transaction_update + transactions_list pagination via API ---
    code, r = call("/api/finance/transaction_create",
                    {"type": "expense", "amount": 9, "category": "prueba-web-editar"})
    tx_id = r["data"]["id"]
    code, r = call("/api/finance/transaction_update", {"id": tx_id, "amount": 42})
    check("transaction_update via API", code == 200 and r["data"]["after"]["amount"] == 42, str(r))

    for _ in range(3):
        call("/api/finance/transaction_create", {"type": "expense", "amount": 3, "category": "prueba-web-paginacion"})
    code, r = call("/api/finance/transactions_list",
                    {"category": "prueba-web-paginacion", "limit": 2, "offset": 0})
    check("transactions_list pagina 1 via API (has_more)", code == 200 and r["data"]["shown"] == 2 and r["data"]["has_more"] is True, str(r))
    code, r = call("/api/finance/transactions_list",
                    {"category": "prueba-web-paginacion", "limit": 2, "offset": 2})
    check("transactions_list pagina 2 via API", code == 200 and r["data"]["shown"] == 1 and r["data"]["has_more"] is False, str(r))

finally:
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
    shutil.rmtree(TEST_DIR, ignore_errors=True)

print("\n" + ("TODO CORRECTO" if not failures else f"FALLOS CRITICOS: {failures}"))
sys.exit(1 if failures else 0)
