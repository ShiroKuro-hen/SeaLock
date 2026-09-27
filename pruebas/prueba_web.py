"""Web app test: launches the server and exercises the real API.

Also checks the defenses: no writes without a token, and requests with a
non-local Host or Origin are rejected.

Usage:  python pruebas/prueba_web.py
"""

import datetime
import json
import math
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


def _colors_outside_vars(css):
    """Hex or rgba used outside a `--custom-property:` definition. HU-15 wants the
    palette to live only in the variables block, so a loose color is a defect."""
    loose = []
    for line in css.splitlines():
        if not re.search(r"#[0-9a-fA-F]{3,8}\b|rgba?\(", line):
            continue
        head = line.split(":", 1)[0]
        if head.strip().startswith("--"):
            continue
        loose.append(line.strip())
    return loose


# The chart is client-side code, so the only way to check what it really draws is to run
# it. node is optional: without it the render checks report AVISO instead of pretending.
NODE = shutil.which("node") or shutil.which("nodejs")

_HARNESS_JS = r"""const fs = require("fs"), vm = require("vm");
const stub = () => ({
  style: {}, dataset: {}, textContent: "", classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
  addEventListener() {}, removeEventListener() {}, setAttribute() {}, getAttribute() { return null; },
  append() {}, appendChild() {}, focus() {}, contains() { return false; },
  querySelector() { return null; }, querySelectorAll() { return []; },
  getBoundingClientRect() { return { left: 0, top: 0, right: 0, bottom: 0, width: 0, height: 0 }; },
});
const ctx = {
  console,
  window: { SEALOCK: { currency: "EUR", token: "t" }, addEventListener() {}, innerWidth: 1280, innerHeight: 900 },
  document: {
    addEventListener() {}, removeEventListener() {}, querySelectorAll() { return []; },
    querySelector() { return null; }, getElementById() { return null; }, createElement: stub,
    body: stub(), documentElement: { dataset: {}, style: {} },
  },
  localStorage: { getItem() { return null; }, setItem() {}, removeItem() {} },
  setInterval() { return 0; }, requestAnimationFrame() { return 0; },
};
ctx.globalThis = ctx;
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[2], "utf8"), ctx, { filename: "app.js" });
ctx.FIXTURE = JSON.parse(fs.readFileSync(process.argv[4], "utf8"));
process.stdout.write(String(vm.runInContext(process.argv[3], ctx)));
"""


def _render_app_js(expr, fixture):
    """Runs an expression of app.js in node with a stub DOM and returns its output.

    Returns None when node is missing and a diagnostic string when the run failed, so
    the caller can fail or warn with something readable instead of an empty string.
    """
    if not NODE:
        return None
    harness = os.path.join(TEST_DIR, "render_app_js.js")
    if not os.path.exists(harness):
        with open(harness, "w", encoding="utf-8") as f:
            f.write(_HARNESS_JS)
    datos = os.path.join(TEST_DIR, "fixture-grafico.json")
    with open(datos, "w", encoding="utf-8") as f:
        json.dump(fixture, f)
    try:
        p = subprocess.run(
            [NODE, harness, os.path.join(ROOT, "app", "web", "app.js"), expr, datos],
            capture_output=True, text=True, encoding="utf-8", timeout=60)
    except subprocess.TimeoutExpired:
        return "TIMEOUT: node no termino en 60 s"
    if p.returncode != 0:
        return f"node fallo: {p.stderr.strip()[:300]}"
    return p.stdout


def _fixture_grafico():
    """60 cierres con minimo, maximo y ultimo cierre conocidos, y las semillas de los
    indicadores como null (49 para la media 50, 19 para Bollinger, 25 para el MACD),
    igual que las devuelve historical()."""
    precios = [100 + 3 * math.sin(i / 7.0) + i * 0.28 for i in range(60)]
    precios[20], precios[40], precios[-1] = 95.0, 120.0, 112.0
    fechas = [(datetime.date(2025, 9, 26) + datetime.timedelta(days=i)).isoformat()
              for i in range(60)]

    def con_fecha(vals, semilla):
        return [{"date": f, "value": None if i < semilla else round(vals[i - semilla], 4)}
                for i, f in enumerate(fechas)]

    return {
        "closes": [{"date": f, "close": round(c, 4)} for f, c in zip(fechas, precios)],
        "sma_50_series": con_fecha(precios[49:], 49),
        "sma_200_series": con_fecha([p * 1.05 for p in precios[59:]], 59),
        "bollinger_upper_series": con_fecha([p * 1.06 for p in precios], 19),
        "bollinger_lower_series": con_fecha([p * 0.94 for p in precios], 19),
        "macd_series": [
            {"date": f,
             "macd": None if i < 25 else round(math.sin(i / 5.0) * 3, 4),
             "signal": None if i < 33 else round(math.sin(i / 5.0) * 2.4, 4),
             "histogram": None if i < 33 else round(math.cos(i / 5.0) * 1.2, 4)}
            for i, f in enumerate(fechas)
        ],
    }


def _puntos(svg, clase):
    """Points of the first polyline or polygon with that class, as (x, y) tuples."""
    m = re.search(rf'class="{clase}"[^>]*points="([^"]+)"', svg or "")
    return [tuple(float(v) for v in q.split(",")) for q in m.group(1).split()] if m else []


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

    # HU-24: metricas de cartera contra ^IBEX. Una operacion antigua deja la serie con
    # mas de un punto, que es lo minimo para que las metricas tengan algo que medir.
    code, r = call("/api/finance/trade_create",
                   {"ticker": "MSFT", "action": "buy", "quantity": 2, "price": 300,
                    "date": "2024-01-02", "currency": "USD"})
    check("registra una operacion antigua para la serie de cartera", code == 200, str(r)[:150])

    code, r = call("/api/app/portfolio_metrics", {})
    pm = r["data"]
    check("HU-24: portfolio_metrics responde con cartera y benchmark",
          code == 200 and "portfolio" in pm and "benchmark" in pm and "not_converted" in pm,
          str(r)[:250])
    p = pm.get("portfolio")
    check("HU-24: la tasa libre de riesgo sale junto a las metricas, nunca escondida",
          p is None or "risk_free_rate" in p, str(p)[:200], critical=False)
    if p:
        check("HU-24: la cartera trae sharpe/sortino/calmar y max drawdown (red)",
              all(k in p for k in ("sharpe", "sortino", "calmar", "max_drawdown_pct")),
              str(p)[:250], critical=False)
        check("HU-24: el benchmark se compara en la misma ventana (red)",
              (pm.get("benchmark") or {}).get("date_from") is not None,
              str(pm.get("benchmark"))[:200], critical=False)
    else:
        check("HU-24: sin serie medible lo dice en vez de devolver un numero inventado",
              "note" in pm and "risk_free_rate" not in pm, str(pm)[:200], critical=False)

    # HU-29: la misma serie, en crudo, para la grafica de la pestana Cartera.
    check("HU-29: portfolio_metrics trae la serie para la grafica",
          isinstance(pm.get("series"), list), str(type(pm.get("series"))))
    if pm.get("series"):
        pt = pm["series"][0]
        check("HU-29: cada punto trae fecha, valor, coste y plusvalia",
              all(k in pt for k in ("date", "value", "cost", "gain"))
              and abs((pt["value"] - pt["cost"]) - pt["gain"]) < 0.05, str(pt))
        hoy = time.strftime("%Y-%m-%d")
        check("HU-29: la serie no mira hacia delante: ninguna fecha posterior a hoy",
              all(p["date"] <= hoy for p in pm["series"]),
              str([p["date"] for p in pm["series"] if p["date"] > hoy][:3]))
    else:
        check("HU-29: sin serie medible lo dice, no inventa puntos",
              "note" in pm, str(pm)[:200], critical=False)

    code, _ = request("/api/app/portfolio_metrics", {})
    check("rechaza portfolio_metrics sin token", code == 403, f"codigo={code}")

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

    hist_web = f.get("historical") or {}
    check("HU-23: la ficha trae ATR, Bollinger y MACD ademas de medias y RSI (red)",
          all(k in hist_web for k in ("atr_14", "bollinger_upper", "bollinger_lower",
                                      "macd", "macd_signal")),
          str({k: hist_web.get(k) for k in ("atr_14", "bollinger_upper", "macd")}),
          critical=False)

    est = f.get("estimates") or {}
    check("la ficha trae las proyecciones de analistas (red)",
          "error" not in est and bool(est.get("periods")), str(est)[:200], critical=False)
    if est.get("periods"):
        p0 = est["periods"][0]
        check("cada proyeccion trae periodo, BPA, analistas, ventas y crecimiento",
              all(k in p0 for k in ("period_es", "eps", "analysts", "revenue",
                                    "revenue_growth_pct")), str(p0)[:200])
        check("el precio objetivo llega con su rango",
              "mean" in (est.get("target") or {}), str(est.get("target"))[:150])

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

    # --- comparador de acciones (HU-30) ---
    code, r = call("/api/app/stock_compare", {"tickers": ["AAPL"]})
    check("stock_compare con 1 ticker rechaza (necesita 2-4)", code == 400, str(r)[:200])

    code, r = call("/api/app/stock_compare", {"tickers": ["AAPL", "MSFT"]})
    check("stock_compare con 2 tickers devuelve 2 filas (red)",
          code == 200 and len(r["data"]["rows"]) == 2, str(r)[:200], critical=False)

    code, r = call("/api/app/recommendation_positions", {})
    check("recommendation_positions devuelve una fila con accion sugerida (red)",
          code == 200 and len(r["data"]["positions"]) == 1
          and "action_key" in r["data"]["positions"][0]["advice"], str(r)[:200], critical=False)

    code, r = call("/api/app/stock_news", {"ticker": "AAPL"})
    check("stock_news responde con company y sector aparte de la ficha",
          code == 200 and "company" in r["data"] and "sector" in r["data"], str(r)[:200])
    check("stock_news trae noticias de empresa y sector (red)",
          bool(r["data"].get("company")) and bool(r["data"].get("sector")),
          str(r)[:200], critical=False)
    code, r = call("/api/app/stock_detail", {"ticker": "AAPL"})
    check("stock_detail ya no espera a las noticias", "news" not in r["data"], str(list(r["data"]))[:200])

    code, _ = request("/api/app/recommendation_universe", {})
    check("recommendation_universe rechaza llamada sin token", code == 403, f"codigo={code}")

    # --- Hidden Gems (HU-11/HU-12/HU-14): universo y cache son sin red; el escaneo real
    # se comprueba solo con cancelar de inmediato, para no lanzar un escaneo pesado aqui ---
    code, r = call("/api/app/gems_universe", {})
    check("gems_universe responde con candidatos y mercados excluidos (sin red)",
          code == 200 and "candidates" in r["data"] and "excluded_markets" in r["data"], str(r)[:200])

    code, _ = request("/api/app/gems_universe", {})
    check("gems_universe rechaza llamada sin token", code == 403, f"codigo={code}")

    code, r = call("/api/finance/gems_list", {})
    check("gems_list responde (sin red, cache vacia al empezar)",
          code == 200 and "gems" in r["data"] and "scanned" in r["data"], str(r)[:200])

    code, _ = request("/api/finance/gems_list", {})
    check("gems_list rechaza llamada sin token", code == 403, f"codigo={code}")

    code, r = call("/api/app/gems_scan_status", {})
    check("gems_scan_status responde sin escaneo en curso", code == 200 and r["data"]["running"] is False, str(r)[:200])

    code, r = call("/api/app/gems_scan_cancel", {})
    check("gems_scan_cancel sin escaneo en curso avisa en vez de fallar",
          code == 200 and r["data"]["ok"] is False, str(r)[:200])

    code, _ = request("/api/app/gems_scan_start", {})
    check("gems_scan_start rechaza llamada sin token", code == 403, f"codigo={code}")

    # Arranca un escaneo real (limitado a 1 candidato) y lo cancela enseguida: prueba el
    # endpoint sin someter la app a un escaneo de cientos de tickers.
    code, r = call("/api/app/gems_scan_start", {"limit": 1})
    check("gems_scan_start arranca con un limite bajo", code == 200 and r["data"]["ok"], str(r)[:200])
    code, r = call("/api/app/gems_scan_cancel", {})
    check("gems_scan_cancel detiene un escaneo en curso", code == 200 and r["data"]["ok"], str(r)[:200])

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

    # --- broker: donde se ejecuto y pendientes de subir (HapiBroker) ---
    code, r = call("/api/finance/trade_create",
                   {"ticker": "VOO", "action": "buy", "quantity": 1, "price": 500,
                    "currency": "USD", "broker": "HapiBroker"})
    check("trade_create con broker via API", code == 200 and r["data"]["broker"] == "HapiBroker",
          str(r)[:200])
    code, r = call("/api/finance/trade_create",
                   {"ticker": "AAPL", "action": "buy", "quantity": 1, "price": 150,
                    "broker": "NoExiste"})
    check("broker invalido via API devuelve 400", code == 400, str(r)[:200])

    code, r = call("/api/finance/trades_pending", {})
    check("trades_pending responde con el desglose por broker",
          code == 200 and "HapiBroker" in r["data"]["by_broker"], str(r)[:200])

    code, r = call("/api/app/config", {})
    cfg = r["data"]
    check("config expone la lista de brokers y los pendientes de cada uno",
          "HapiBroker" in cfg.get("brokers", []) and "HapiBroker" in cfg.get("pending_by_broker", {}),
          str(cfg)[:200])

    code, r = call("/api/finance/trades_mark_uploaded", {"broker": "HapiBroker"})
    check("trades_mark_uploaded via API", code == 200 and r["data"]["marked"] >= 1, str(r)[:200])

    # --- preguntar a Claude (HU-32) ---
    code, r = call("/api/app/config", {})
    cfg2 = r["data"]
    check("config dice si hay clave de Claude y que modelo se usa",
          "claude_available" in cfg2 and bool(cfg2.get("claude_model")), str(cfg2)[:200])

    code, r = call("/api/app/ask", {"ticker": "AAPL", "question": ""})
    check("pregunta vacia se rechaza sin llamar a la API", code == 400, str(r)[:200])

    if not cfg2.get("claude_available"):
        code, r = call("/api/app/ask", {"ticker": "AAPL", "question": "esta cara?"})
        check("sin clave, ask explica que falta y no envia nada",
              code == 400 and "ANTHROPIC_API_KEY" in json.dumps(r), str(r)[:200])
    else:
        code, r = call("/api/app/ask",
                       {"ticker": "AAPL", "question": "esta cara? responde en una frase"})
        check("ask responde con texto (red)", code == 200 and bool(r["data"].get("answer")),
              str(r)[:200], critical=False)

    code, _ = request("/api/app/ask", {"ticker": "AAPL", "question": "hola"})
    check("ask rechaza llamada sin token", code == 403, f"codigo={code}")

    code, r = call("/api/app/build_prompt", {"ticker": "AAPL"})
    pr = r["data"].get("text", "")
    check("build_prompt devuelve un prompt con los datos y las URLs (red)",
          code == 200 and "finance.yahoo.com/quote/AAPL" in pr and "Mi perfil inversor" in pr,
          str(r)[:200], critical=False)
    check("el prompt lleva los numeros de la ficha y las proyecciones",
          "Nota de la app" in pr and "Proyecciones del consenso" in pr, "", critical=False)
    code, _ = request("/api/app/build_prompt", {"ticker": "AAPL"})
    check("build_prompt rechaza llamada sin token", code == 403, f"codigo={code}")

    code, html = request("/")
    check("la clave de la API nunca llega al HTML",
          "ANTHROPIC_API_KEY" not in html and "x-api-key" not in html, "")

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

    # HU-23: los tres indicadores nuevos se pintan en la ficha y el glosario
    # los explica (B4 ya exigiria la entrada; aqui se comprueba lo contrario).
    js_busca = ('metrica("ATR 14"', 'metrica("Bollinger %B"', 'metrica("MACD"')
    gl_busca = ('titulo: "ATR"', 'titulo: "Bollinger"', 'titulo: "MACD"')
    check("HU-23: la ficha muestra ATR, Bollinger %B y MACD, con su glosario",
          all(s in app_js for s in js_busca) and all(s in glosario_js for s in gl_busca),
          "falta alguna etiqueta en app.js o su entrada en glosario.js")

    # HU-19: las cajas de carga dibujan un esqueleto con la forma del contenido, no
    # un "Cargandoâ€¦" suelto. Si alguien reintroduce el placeholder, esto falla.
    css = open(os.path.join(ROOT, "app", "web", "estilo.css"), encoding="utf-8").read()
    check("HU-19: helper de esqueleto usado en las cajas de carga",
          "function esqueleto(" in app_js and app_js.count("esqueleto(") >= 6,
          f"usos={app_js.count('esqueleto(')}")
    check("HU-19: no queda ninguna caja con el placeholder 'Cargandoâ€¦'",
          'class="cargando"' not in app_js,
          "quedan placeholders class=cargando")
    check("HU-19: esqueleto con estilo y prefers-reduced-motion",
          ".esqueleto" in css and "prefers-reduced-motion" in css,
          "falta .esqueleto o prefers-reduced-motion")

    # HU-16: nota como anillo, pilares ordenados por peso, sparkline y SVG accesible.
    check("HU-16: hay anillo SVG de la nota y sparkline",
          "function anilloSVG(" in app_js and "function sparklineSVG(" in app_js,
          "faltan anilloSVG o sparklineSVG")
    check("HU-16: los pilares se ordenan por peso, no por orden de llegada",
          ".sort((a, b) => (b.weight || 0) - (a.weight || 0))" in app_js,
          "no hay ordenacion de pilares por peso")
    check("HU-16: la ficha pinta la serie del historico y las metricas de riesgo",
          "precioSVG(h" in app_js and 'metrica("Sharpe"' in app_js,
          "la ficha no dibuja el historico o no muestra Sharpe")
    svg_imgs = app_js.count('role="img"')
    check("HU-16: toda grafica SVG trae role=img y texto alternativo",
          svg_imgs >= 3 and 'aria-label="Nota' in app_js,
          f"role=img x{svg_imgs}")

    code, r = call("/api/app/sparklines", {"tickers": ["AAPL"]})
    series = (r.get("data") or {}).get("series") or {}
    check("HU-16: sparklines devuelve la serie de AAPL (red)",
          code == 200 and len(series.get("AAPL") or []) > 2,
          str(r)[:200], critical=False)
    code, _ = request("/api/app/sparklines", {"tickers": ["AAPL"]})
    check("rechaza sparklines sin token", code == 403, f"codigo={code}")


    # HU-15: sistema de diseno y modo oscuro.
    with open(os.path.join(ROOT, "app", "web", "index.html"), encoding="utf-8") as f:
        index_html = f.read()
    check("HU-15: el tema se fija antes del primer pintado y el interruptor esta en la cabecera",
          "dataset.tema" in index_html and 'id="btn-tema"' in index_html,
          "falta el script de arranque del tema o el boton")
    check("HU-15: la eleccion de tema persiste en localStorage",
          'localStorage.setItem("sealock-tema"' in app_js and 'localStorage.getItem("sealock-tema")' in index_html,
          "no hay persistencia del tema")
    check("HU-15: el tema por defecto sigue prefers-color-scheme",
          "prefers-color-scheme" in index_html, "el arranque no mira el sistema")
    check("HU-15: los colores viven solo en variables (ningun hex ni rgba suelto)",
          _colors_outside_vars(css) == [], str(_colors_outside_vars(css)))
    check("HU-15: escala tipografica de 6 tamanos, sin px sueltos",
          len(re.findall(r"--t-[a-z0-9]+:", css)) >= 5
          and not re.search(r"font(-size)?:\s*[0-9.]+px", css),
          "quedan font-size en px o faltan tokens")
    check("HU-15: cifras tabulares en importes y notas",
          css.count("tabular-numeric") + css.count("tabular-nums") >= 5,
          f"tabular-nums x{css.count('tabular-nums')}")

    # HU-29: la grafica de evolucion vive en la pestana Cartera.
    check("HU-29: caja de Evolucion en Cartera con su funcion de carga",
          'id="evolucion-cartera"' in index_html
          and "function loadEvolucion(" in app_js and "function evolucionHTML(" in app_js,
          "falta la caja o las funciones")
    check("HU-29: la grafica es SVG con role=img y texto alternativo",
          "function evolucionSVG(" in app_js
          and 'aria-label="Evolucion de la cartera' in app_js
          and app_js.count('role="img"') >= 4,
          f"role=img x{app_js.count('role=\"img\"')}")
    check("HU-29: dibuja valor total y plusvalia latente con su linea de cero",
          "yGanancia(0)" in app_js and "ev-leyenda" in app_js and "ev-alza" in app_js,
          "falta la linea de cero o la leyenda")
    check("HU-29: la curva usa la serie punto-en-el-tiempo y lo explica en la ayuda",
          "punto en el tiempo" in app_js and "evolucion: {" in app_js
          and 'data-section="evolucion"' in index_html,
          "falta la ayuda de la serie")
    bloque_evo = app_js.split("function evolucionSVG(")[1] if "function evolucionSVG(" in app_js else ""
    check("HU-29: la grafica se pinta con variables de color, no con hex sueltos",
          all(v in bloque_evo for v in ("var(--acento)", "var(--borde)"))
          and _colors_outside_vars(css) == [], str(_colors_outside_vars(css)))

    # --- HU-35/HU-36: el grafico de precio de la ficha (epica 7) ---
    bloque_precio = app_js.split("function precioSVG(")[1].split("function pintarPrecio(")[0] \
        if "function precioSVG(" in app_js else ""
    bloque_pintar = app_js.split("function pintarPrecio(")[1].split("function sparklineSVG(")[0] \
        if "function pintarPrecio(" in app_js else ""
    check("HU-35: existe precioSVG y la ficha ya no usa el sparkline de 280x56",
          "function precioSVG(" in app_js and "sparklineSVG(h.closes" not in app_js
          and "280, 56" not in app_js and "function sparklineSVG(" in app_js,
          "falta precioSVG, la ficha sigue con el sparkline, o se borro la funcion")
    check("HU-35: la caja de Comportamiento pinta el grafico y tiene su boton de ayuda",
          'id="precio-grafico"' in app_js and "pintarPrecio(h, c)" in app_js
          and 'data-section="precio"' in app_js and "precio: {" in app_js,
          "falta la caja, la llamada a pintarPrecio o la ayuda")
    caja_comp = app_js.split('id="precio-grafico"')[1].split("Valor intrinseco")[0] \
        if 'id="precio-grafico"' in app_js else ""
    check("HU-35: las 12 metricas siguen debajo del grafico, ahora como texto secundario",
          caja_comp.count("${metrica(") == 12,
          f"metricas={caja_comp.count('${metrica(')}")

    fix = _fixture_grafico()
    svg = _render_app_js("precioSVG(FIXTURE, {ancho: 517, alto: 210, moneda: 'EUR'})", fix)
    critico = NODE is not None
    check("HU-35: el SVG dibujado trae role=img y un aria-label con las cifras",
          bool(svg) and 'role="img"' in svg
          and re.search(r'aria-label="Grafico de precio: [^"]{30,}"', svg),
          (svg or "sin node: no se pudo renderizar")[:200], critical=critico)
    etiquetas = re.findall(r'<text class="p-y[^"]*"[^>]*>([^<]+)</text>', svg or "")
    check("HU-35: el eje Y rotula minimo, maximo y ultimo cierre con el numero real",
          {"95", "120", "112"} <= set(etiquetas), f"etiquetas={etiquetas}", critical=critico)
    fechas_x = re.findall(r'<text class="p-x"[^>]*>([^<]+)</text>', svg or "")
    check("HU-35: el eje X rotula la primera y la ultima fecha, en espanol y no en ISO",
          len(fechas_x) == 2
          and all(re.search(r"[a-záéíóú]{3,}\s\d{4}$", f) for f in fechas_x),
          f"fechas={fechas_x}", critical=critico)
    check("HU-35: responsive sin libreria (viewBox + width:100% + preserveAspectRatio)",
          "viewBox=" in (svg or "") and 'preserveAspectRatio="xMidYMid meet"' in (svg or "")
          and ".precio-svg, .macd-svg { display: block; width: 100%; height: auto; }" in css
          and re.findall(r'<script src="([^"]+)"', index_html) == ["/glosario.js", "/app.js"],
          "falta viewBox, preserveAspectRatio, el ancho al 100% o aparece una libreria")

    # Una semilla null pintada como 0 caeria fuera del panel (con esta escala y(0) sale
    # negativo): la linea tiene que empezar donde hay dato y ningun punto salirse.
    cierre = _puntos(svg, "p-cierre")
    sma = _puntos(svg, "p-sma50")
    check("HU-36: la media 50 no se dibuja antes de su semilla (49 nulls), ni como 0",
          bool(cierre) and len(sma) == 11 and sma[0][0] / cierre[0][0] > 0.7
          and all(y > 0 for _, y in sma),
          f"puntos={len(sma)} inicio={sma[0][0] if sma else 0}", critical=critico)
    banda = _puntos(svg, "p-banda")
    check("HU-36: la banda de Bollinger tampoco arranca en el primer dia (19 nulls)",
          bool(banda) and banda[0][0] / cierre[0][0] > 0.2
          and all(y > 0 for _, y in banda),
          f"puntos={len(banda)} inicio={banda[0][0] if banda else 0}", critical=critico)
    check("HU-36: banda y lineas con variables de color y relleno translucido, no rgba",
          'class="p-banda"' in (svg or "") and "fill-opacity" in css
          and "rgba(" not in (svg or "") and "#" not in (svg or "")
          and all(c in bloque_precio for c in ("p-sma50", "p-sma200", "p-cierre")),
          "el SVG trae color suelto o la banda no va con fill-opacity", critical=critico)

    macd = _render_app_js("macdSVG(FIXTURE, {ancho: 517, moneda: 'EUR'})", fix)
    check("HU-36: el panel de MACD tiene histograma, senal y linea de cero, con escala propia",
          bool(macd) and 'class="p-hist-alza"' in macd and 'class="p-hist-baja"' in macd
          and 'class="p-cero"' in macd and 'class="p-macd"' in macd and 'class="p-senal"' in macd
          and macd.count("<svg") == 1
          and re.search(r'aria-label="Panel de MACD[^"]{10,}"', macd),
          (macd or "sin node: no se pudo renderizar")[:200], critical=critico)

    check("HU-35: la cruceta se alcanza sin raton (foco + flechas) y con el dedo",
          'tabindex="0"' in bloque_precio and "pointermove" in bloque_pintar
          and "ArrowLeft" in bloque_pintar and "ArrowRight" in bloque_pintar
          and "getBoundingClientRect" in bloque_pintar
          and 'class="precio-lectura"' in app_js,
          "falta el foco, las flechas o el puntero")
    check("HU-35: los numeros existen tambien como texto, no solo dentro del SVG",
          'class="precio-lectura"' in app_js and "Min." in bloque_pintar
          and 'class="p-y' in bloque_precio and 'metrica("Bollinger %B"' in caja_comp,
          "la lectura no se pinta fuera del SVG")
    check("HU-36: las lineas del grafico tienen entrada de glosario (lo exige B4)",
          all(s in app_js for s in ('data-glosario="Media 50"', 'data-glosario="Media 200"',
                                    'data-glosario="Bandas de Bollinger"', 'data-glosario="Señal MACD"'))
          and all(s in glosario_js for s in ('"Bandas de Bollinger"', 'titulo: "Señal MACD"')),
          "falta una etiqueta del grafico o su entrada en el glosario")
    bloque_css = css.split("grafico de precio de la ficha")[1].split(".veredicto {")[0] \
        if "grafico de precio de la ficha" in css else ""
    check("HU-35: la paleta del grafico vive en variables, sin hex ni rgba sueltos",
          bool(bloque_css) and _colors_outside_vars(bloque_css) == []
          and not re.search(r"font(-size)?:\s*[0-9.]+px", bloque_css)
          and _colors_outside_vars(css) == [],
          str(_colors_outside_vars(bloque_css)))
    check("HU-15: la cruceta respeta prefers-reduced-motion",
          "transition" in bloque_css and "prefers-reduced-motion" in css,
          "falta la transicion o el permiso para quien pide menos movimiento")

finally:
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
    shutil.rmtree(TEST_DIR, ignore_errors=True)

print("\n" + ("TODO CORRECTO" if not failures else f"FALLOS CRITICOS: {failures}"))
sys.exit(1 if failures else 0)
