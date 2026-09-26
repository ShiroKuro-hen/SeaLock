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
         "SEALOCK_NO_OPEN": "1", "SEALOCK_REFRESH_COOLDOWN": "0", "PYTHONIOENCODING": "utf-8"},
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

    # --- arranque unico: un segundo SeaLock no pelea por el puerto ---
    dup = subprocess.run(
        [sys.executable, "-u", os.path.join(ROOT, "app", "servidor.py")],
        capture_output=True, text=True, encoding="utf-8", cwd=ROOT, timeout=30,
        env={**os.environ, "SEALOCK_DATA": TEST_DIR, "SEALOCK_PORT": str(PORT),
             "SEALOCK_NO_OPEN": "1", "PYTHONIOENCODING": "utf-8"},
    )
    check("segunda copia avisa y sale con codigo 0 sin arrancar otro servidor",
          dup.returncode == 0 and "SeaLock is already running" in dup.stdout,
          f"codigo={dup.returncode} salida={dup.stdout[:120]!r}")

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
    code, _ = request("/api/finance/watchlist_list", {})
    check("rechaza API sin token", code == 403, f"codigo={code}")

    code, _ = request("/api/finance/watchlist_list", {}, token="equivocado")
    check("rechaza token incorrecto", code == 403, f"codigo={code}")

    code, _ = request("/api/finance/watchlist_list", {}, token=token,
                       headers={"Origin": "https://malicioso.example"})
    check("rechaza Origin externo (CSRF)", code == 403, f"codigo={code}")

    code, _ = request("/api/finance/watchlist_list", {}, token=token,
                       headers={"Host": "atacante.example"})
    check("rechaza Host externo (DNS rebinding)", code == 403, f"codigo={code}")

    code, _ = request("/api/finance/no_existe", {}, token=token)
    check("404 en herramienta inexistente", code == 404, f"codigo={code}")

    # --- flujo real ---
    def call(path, data):
        code, body = request(path, data, token=token)
        return code, json.loads(body)

    code, r = call("/api/finance/profile_get", {})
    check("profile_get via API devuelve por defecto", code == 200 and r["data"]["risk_tolerance"] == "moderate", str(r))

    code, r = call("/api/finance/profile_set", {"preference": "dividend"})
    check("profile_set via API actualiza", code == 200 and r["data"]["preference"] == "dividend", str(r))

    code, r = call("/api/finance/profile_set", {"horizon": "nunca"})
    check("profile_set via API rechaza enum invalido", code == 400, str(r))

    code, r = call("/api/finance/trade_create",
                    {"ticker": "AAPL", "action": "buy", "quantity": 5, "price": 150})
    check("registra operacion via API", code == 200 and r["data"]["ok"], str(r))

    code, r = call("/api/finance/trade_create",
                    {"ticker": "AAPL", "action": "sell", "quantity": 999, "price": 150})
    check("propaga error de negocio como 400 (venta en corto)", code == 400 and "error" in r, str(r))

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
    check("ficha completa con nota (red)",
          code == 200 and f["quote"].get("price") and "dcf" in f and "score" in f,
          str(r)[:200], critical=False)
    if code == 200 and "score" in f:
        check("la nota trae position para un valor en cartera", f.get("position") is not None,
              str(f.get("position")), critical=False)
    q = f.get("quarterly") or {}
    check("la ficha trae los trimestres fiscales (red)",
          bool(q.get("quarters")) and "available" in q, str(q)[:200], critical=False)
    if q.get("quarters"):
        primero = q["quarters"][0]
        check("cada trimestre trae cierre, ventas, beneficio, BPA, margen e interanual",
              all(k in primero for k in ("period_end", "revenue", "net_income", "diluted_eps",
                                          "net_margin_pct", "revenue_yoy_pct")),
              str(primero)[:200])
        check("el pilar de resultados recientes usa esos trimestres",
              next(p for p in f["score"]["pillars"]
                   if p["key"] == "results")["subscore"] is not None,
              str([(p["key"], p["subscore"]) for p in f["score"]["pillars"]]))
    check("la nota expone quarters_available y su aviso",
          "quarters_available" in f["score"], str(f["score"].get("quarters_available")))

    # --- actualizar nota a demanda (HU-06) ---
    check("la ficha dice cuantos datos tiene la nota",
          isinstance(f.get("score_age_seconds"), int) and f["score_age_seconds"] >= 0,
          str(f.get("score_age_seconds")))
    code, r = call("/api/app/stock_detail", {"ticker": "AAPL"})
    f2 = r["data"]
    check("la segunda llamada sirve la cache: la edad no se reinicia",
          f2["score_age_seconds"] >= f["score_age_seconds"],
          f"{f['score_age_seconds']} -> {f2['score_age_seconds']}")
    code, r = call("/api/app/stock_detail", {"ticker": "AAPL", "refresh": True})
    f3 = r["data"]
    check("refresh=true reinicia la edad de los datos",
          code == 200 and f3["score_age_seconds"] <= f2["score_age_seconds"],
          f"{f2['score_age_seconds']} -> {f3['score_age_seconds']}")
    with open(os.path.join(ROOT, "app", "web", "app.js"), encoding="utf-8") as fh:
        ficha_js = fh.read()
    check("el boton Actualizar nota esta en la ficha",
          "btn-actualizar-nota" in ficha_js, "")

    code, r = call("/api/app/portfolio_scores", {})
    check("portfolio_scores via API (red)",
          code == 200 and "AAPL" in r["data"]["scores"], str(r)[:200], critical=False)

    code, r = call("/api/finance/watchlist_add", {"ticker": "msft"})
    check("watchlist_add via API", code == 200 and r["data"]["ticker"] == "MSFT", str(r))
    code, r = call("/api/finance/watchlist_remove", {"ticker": "msft"})
    check("watchlist_remove via API", code == 200 and r["data"]["ok"], str(r))

    # --- recomendaciones ---
    code, r = call("/api/app/recommendation_universe", {})
    u = r["data"]
    check("recommendation_universe devuelve candidatos", code == 200 and len(u.get("candidates", [])) >= 90, str(r)[:200])
    check("recommendation_universe trae fuente y fecha",
          bool(u.get("source")) and bool(u.get("as_of")), str(u.get("source")))
    check("recommendation_universe incluye la posicion en cartera (AAPL)",
          "AAPL" in u.get("held", []), str(u.get("held")))

    code, r = call("/api/app/stock_scores", {"tickers": []})
    check("stock_scores con lista vacia devuelve ok", code == 200 and r["data"]["scores"] == {}, str(r))

    code, r = call("/api/app/stock_scores", {"tickers": ["AAPL"]})
    check("stock_scores puntua un ticker (red)",
          code == 200 and "AAPL" in r["data"]["scores"], str(r)[:200], critical=False)

    code, r = call("/api/app/recommendation_positions", {})
    check("recommendation_positions devuelve una fila con accion sugerida (red)",
          code == 200 and len(r["data"]["positions"]) == 1
          and "action_key" in r["data"]["positions"][0]["advice"], str(r)[:200], critical=False)

    code, _ = request("/api/app/recommendation_universe", {})
    check("recommendation_universe rechaza llamada sin token", code == 403, f"codigo={code}")

    # --- revision de Claude en la ficha (HU-08/HU-09) ---
    code, r = call("/api/finance/review_set", {
        "ticker": "AAPL", "adjustment": 1, "rationale": "Prueba e2e: trimestre mejor de lo esperado",
        "sources": ["https://example.com/nota"], "mechanical_score": 6.5,
        "last_quarter_seen": "2026-06-30"})
    check("review_set via API guarda la revision", code == 200 and r["data"]["ok"], str(r)[:200])

    code, r = call("/api/app/stock_detail", {"ticker": "AAPL"})
    sc = r["data"].get("score") or {}
    check("stock_detail trae score, final_score y review",
          code == 200 and "final_score" in sc and (sc.get("review") or {}).get("adjustment") == 1,
          str(sc)[:200])
    check("el ajuste mueve la nota final y la mecanica queda a la vista",
          sc.get("final_score") != sc.get("score") or sc.get("review_applied"),
          f"mecanica={sc.get('score')} final={sc.get('final_score')}")
    check("con revision vigente el veredicto queda aplicado",
          sc.get("review_applied") is True, str(sc.get("review_applied")))

    code, r = call("/api/app/stock_detail", {"ticker": "AAPL",
                                              "latest_quarter_end": "2026-09-30"})
    sc_stale = r["data"].get("score") or {}
    check("una revision obsoleta no se aplica pero se informa",
          sc_stale.get("review_applied") is False
          and (sc_stale.get("review") or {}).get("status") == "stale", str(sc_stale)[:200])

    code, r = call("/api/finance/review_get", {"ticker": "AAPL"})
    rid = r["data"]["review"]["id"]
    code, r = call("/api/finance/review_delete", {"id": rid})
    check("review_delete sin confirmacion no borra", r["data"].get("needs_confirmation") is True, str(r)[:200])
    call("/api/finance/review_delete", {"id": rid, "confirm": True})
    code, r = call("/api/finance/review_get", {"ticker": "AAPL"})
    check("tras confirmar, la revision ya no esta", r["data"].get("review") is None, str(r)[:200])

    # --- exportacion (HU-28) ---
    code, r = call("/api/app/export_all", {})
    exp = r["data"] if code == 200 else {}
    check("export_all devuelve trades, watchlist y reviews",
          code == 200 and set(exp) == {"trades", "watchlist", "reviews"}, str(r)[:200])
    check("export_all incluye las operaciones registradas",
          any(t["ticker"] == "AAPL" for t in exp.get("trades", [])), str(exp.get("trades"))[:200])
    code, _ = request("/api/app/export_all", {})
    check("export_all rechaza llamada sin token", code == 403, f"codigo={code}")

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

finally:
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
    shutil.rmtree(TEST_DIR, ignore_errors=True)

print("\n" + ("TODO CORRECTO" if not failures else f"FALLOS CRITICOS: {failures}"))
sys.exit(1 if failures else 0)
