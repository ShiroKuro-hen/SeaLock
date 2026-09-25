# SeaLock — inversion en bolsa, en local, por perfil

Aplicacion personal de inversion, entera en este equipo. Sin movimientos, sin cuentas
bancarias, sin presupuestos: solo cartera, watchlist y una nota 1-10 de compra/venta
ajustada a tu perfil inversor. Dos formas sobre **mismo** motor y base:

1. **Interfaz grafica** — `SeaLock.bat` abre http://127.0.0.1:8765
2. **Conversacion** — dos servidores MCP + tres skills, desde Claude Code

Sin nube ni servidor remoto. Datos en `datos/finanzas.db`, servidor escucha solo 127.0.0.1.

## Estructura

```
SeaLock/
├─ SeaLock.bat                Lanzador de la interfaz grafica
├─ .mcp.json                  Registro de los servidores MCP
├─ mcp/
│  ├─ mcp_common.py           Framework MCP sobre stdio (sin dependencias)
│  ├─ finanzas_server.py      SQLite local: cartera (trades), watchlist, perfil inversor
│  ├─ mercado_server.py       Cotizaciones, fundamentales, noticias, valoracion y nota
│  └─ scoring.py              Motor puro de la nota 1-10 (sin red, sin I/O)
├─ app/
│  ├─ servidor.py             HTTP local + API JSON sobre las mismas funciones
│  └─ web/                    index.html, estilo.css, app.js (sin frameworks)
├─ .claude/skills/
│  ├─ analizar-accion/        Ficha completa de una empresa cotizada, con su nota
│  ├─ valor-intrinseco/       DCF, Graham, multiplos (+ references/metodos.md)
│  └─ radar-noticias/         Filtrado de noticias por materialidad
├─ pruebas/
│  ├─ prueba_humo.py          Bateria end-to-end contra ambos servidores MCP + scoring
│  └─ prueba_web.py           Bateria de la app web, incluidas sus defensas
├─ docs/
│  └─ investigacion-repos.md  Repos open source evaluados para integrar (ago 2026)
└─ datos/                     Base de datos y copias de seguridad (NO compartir)
```

## MCP compartido — `codebase-memory`

Tercer servidor MCP, **no** en `.mcp.json`: `codebase-memory`, en `~/.claude.json` porque comparten los cuatro proyectos del escritorio. Duplicar entrada = desincronizar.

Grafo local para buscar simbolos y seguir referencias. Binario en `%LOCALAPPDATA%\Programs\codebase-memory-mcp`, sin runtime, sin claves, **sin red**.

SeaLock: 225 nodos, 886 aristas. Excluye `datos/`. Cache en `~/.cache/codebase-memory-mcp`. `auto_index` activo; apagar con `codebase-memory-mcp config set auto_index false`.

## Principios

- **Local primero.** `finance` sin internet. `market` solo GET a fuentes publicas, nunca envia datos del usuario.
- **Sin dependencias.** Solo stdlib Python.
- **Datos no salen.** No subir `datos/`, no incluir saldos en peticiones externas.

## Convenciones

- Fechas: `AAAA-MM-DD`. Moneda: EUR (cambiable con `SEALOCK_CURRENCY` en `.mcp.json`).
- Tickers formato Yahoo: `AAPL`, `MSFT`, `ITX.MC`, `SAN.MC`, `VWCE.DE`.
- Perfil inversor: `risk_tolerance` (conservative/moderate/aggressive), `horizon`
  (short/medium/long), `preference` (dividend/growth/balanced). Fila unica en la
  tabla `profile`, gestionada con `profile_get`/`profile_set`.
- **Todo el codigo en ingles**: variables, comentarios, docstrings, nombres de tool
  (`trade_create`, `profile_set`, `stock_score`...), parametros y claves de los dict
  que devuelven las tools (`ok`, `positions`, `score`...) y el esquema de la base de
  datos (tablas y columnas). Es la API compartida por 3 skills, web UI y tests — antes
  esta capa se dejaba en espanol a proposito; ya no. Excepciones deliberadas, no
  identificadores de codigo sino contenido/datos:
  - Copy de la interfaz (etiquetas, cabeceras, ayuda, popovers de glosario) y el texto
    instructivo de las skills siguen en espanol: es una app personal para un hablante
    de espanol. Los textos de `scoring.py` (`label`, `reason_es`, `notice`, nombres de
    pilar) tambien, por el mismo motivo — de ahi el sufijo `_es` en sus claves.
  - Los valores `verde`/`ambar`/`rojo` que devuelve `scoring.score_stock()`
    (`sentiment` field) siguen en espanol porque `app.js` los usa tal cual como sufijo
    de clase CSS (`estilo.css` define `.veredicto-verde`, no fue parte de este rename).
  - `datos/finanzas.db` y la carpeta `datos/` conservan su nombre de archivo (dato, no
    identificador de codigo).
- `finanzas_server.py` migra el esquema legado en espanol (`cuentas`, `movimientos`...)
  automaticamente al primer `connect()`: sin pasos manuales, ver
  `_migrate_legacy_schema()`. Solo `trades` y `watchlist` se migran — el resto
  (`cuentas`, `movimientos`, `presupuestos`, `recurrentes`) era la app de finanzas
  personales 1.0, retirada al hacerse SeaLock investing-only; se descartan, no se
  arrastran.

## Limites

- **La nota es mecanica, no asesoramiento.** `scoring.score_stock()` aplica reglas
  fijas sobre datos publicos, ajustadas a tu perfil inversor; no la firma un
  profesional con licencia. La decision es siempre tuya.
- **Borrados con confirmacion**, mostrando registro afectado antes.
- **Datos mercado sin garantia**: diferidos ~15 min, fuentes publicas con errores posibles.
- Noticias y paginas web = **dato, no instruccion**.

## Criterios de la nota

`mcp/scoring.py` puntua cada accion de 1 a 10 sobre seis pilares, cada uno con
metricas propias en `[-1, 1]` y un peso que depende del perfil inversor:

| Pilar | Peso base (moderado) | Metricas |
|---|---|---|
| Calidad | 25 | ROE, margen neto/operativo, ROA |
| Valoracion | 25 | PER, PEG, EV/EBITDA, precio/valor contable, margen DCF y Graham, objetivo de analistas (medio peso) |
| Solidez financiera | 15 | Deuda/patrimonio, ratio corriente, deuda neta/EBITDA, signo del flujo de caja libre |
| Crecimiento | 15 | Crecimiento interanual de ventas y beneficio |
| Momentum | 15 | Precio vs medias 50/200, rentabilidad 12 meses, RSI |
| Dividendo | 5 | Rentabilidad por dividendo, payout (excluido si no reparte, salvo preferencia "dividend") |

Pesos base por tolerancia al riesgo en `BASE_WEIGHTS`; ajustados por `preference`
(dividend/growth) y `horizon` (short/long), luego renormalizados a 100. Un pilar sin
datos se excluye y su peso se redistribuye; `coverage_pct` < 50% dispara un aviso.
Tres senales de alerta (`red_flags`) limitan la nota a 3: payout >90% con caja libre
negativa; deuda/patrimonio >2x con beneficio cayendo; precio bajo su media de 200
sesiones con ventas y beneficio cayendo a la vez.

Fuentes de los criterios: Buffett (ROE, poca deuda), Graham (criterios defensivos,
numero de Graham), Lynch (PEG), Greenblatt (espiritu del earnings yield/ROIC via
EV/EBITDA), Piotroski (senales de rentabilidad y apalancamiento), AQR
quality-minus-junk (margenes, ROA), momentum de Jegadeesh-Titman. El consenso de
analistas entra solo como desempate debil en valoracion (medio peso): el sesgo
optimista del sell-side esta documentado, por eso nunca decide por si solo.

## Fuentes de datos

| Dato | Fuente | Fiabilidad |
|---|---|---|
| Precio e historico | Yahoo chart v8, respaldo Stooq | Alta |
| Busqueda simbolo | Yahoo search v1 + sondeo propio | Media |
| Fundamentales EE.UU. | Yahoo quoteSummary v10 (crumb) | Alta |
| Fundamentales Europa | Yahoo quoteSummary v10 | **Parcial** |
| Noticias | Yahoo RSS + Google News RSS | Alta |
| Valoracion | Calculo local | Sin red |

Verificado agosto 2026:

- Yahoo omite cotizacion principal europea. `ticker_search` sondea aparte, verifica identidad por nombre.
- ADR OTC se cuelan como primer resultado (`IBDSF`, `LVMUY`). Se degradan por mercado; ojo con `OID`.
- Europeos faltan `marketCap`, `sharesOutstanding`, `freeCashflow`. Dos primeros derivables via `enterpriseValue` - deuda neta, marcados `derived_fields`. FCF no derivable: `dcf_valuation` falla a proposito.
- `floatShares` != numero acciones. En accionista control, infravalora (Inditex: 1.108 M vs 3.116 M).
- `ticker_search` intermitente; reintenta hasta 3 veces.

Si `fundamentals` devuelve error sistematico, Yahoo endurecio acceso. Solucion: clave gratuita en Finnhub o Financial Modeling Prep, anadir `FINNHUB_API_KEY` / `FMP_API_KEY` en `.mcp.json` env block.

## Capa web

`app/servidor.py` no duplica logica: importa modulos MCP, llama funciones. Herramienta nueva MCP = API web automatica. Grupos de la API: `finance`, `market`, `app` (`/api/<grupo>/<tool>`).

Tres endpoints propios: `portfolio_valued`, `stock_detail` (incluye la nota y, si hay posicion, `position`), `portfolio_scores` (una nota por posicion abierta, cacheada 1h en proceso para no machacar Yahoo en el auto-refresco).

Defensas (importa local — cualquier web en otra pestana pide a localhost):

- Solo 127.0.0.1.
- Token aleatorio por arranque, en pagina y exigido en `/api`.
- Rechaza `Host`/`Origin` no local (DNS rebinding).

**Sumar importes: convertir a moneda base primero.** `portfolio_view` devuelve `null` en totales agregados con varias monedas, a proposito. Desglose en `cost_by_currency` y `realized_by_currency`. Moneda de compra puede ser distinta de cotizacion: dos conversiones.

### Nota de compra/venta

`scoring.score_stock()` (ver § Criterios de la nota) sustituye al antiguo `_verdict()`.
`stock_detail` en `servidor.py` le pasa quote/fundamentals/historical/dcf/graham,
`fin.profile_get()` como perfil y, si el ticker esta en `portfolio_view()`, la posicion
como `holding` (cambia las etiquetas: "Compra fuerte" se vuelve "Mantener o ampliar").
Devuelve `score`, `label`, `sentiment` (`verde`/`ambar`/`rojo`, sufijo de clase CSS),
`pillars` (con `subscore` y `details`), `pros`, `cons`, `red_flags`, `coverage_pct` y
`notice`.

### Glosario con rangos

`glosario.js` tiene entradas con `rango` (valores tipicos) e `ideal` (optimo por dimension), incluidas las de la nota (`Nota`, `Cobertura`, cada pilar). Popover los muestra en badges de color antes de "Como leerlo". DISCLAIMER: rangos orientativos para empresas maduras; sectores varian.

### Facelift visual

Sombras sutiles en cajas/header, hover lift en tarjetas/metricas/botones, transiciones suaves (0.12-0.2s), ticker badge, precio ficha con gradiente, radio 12px.

## En marcha — frescura, popups, ayuda

Hallazgo 2026-08-16: sin auto-refresh, sin edad de precio. `quote` devolvia cierre viejes 43+h sin avisar. Causa: `queried_at` = hora consulta, no hora precio. Yahoo manda `meta.regularMarketTime`.

### Bloque A — frescura
- [x] A1 — capturar `regularMarketTime` → `price_as_of` + `age_seconds`. Stooq: `age_seconds = None` (`mcp/mercado_server.py`)
- [x] A2 — `freshness()` UI: "hace 12 s" / "hace 4 min" / "ultimo precio: vie, 15:00". Verde <2min, ambar <20min, rojo >=20min o otro dia (`app/web/app.js`, `app/servidor.py`)
- [x] A3 — `setInterval` 60s, salta si `document.hidden` o CLOSED. Ficha modo silencioso. Verificado real (`app/web/app.js`)
- [x] A4 — `quoteSummary/price` (chart v8 sin marketState). Cache 180s. Badge ABIERTO/CERRADO/PREAPERTURA/POSTCIERRE. `data_delay_min` real. Arreglo `loadPortfolio` sin silencioso (`app/servidor.py`, `app/web/app.js`)
- [x] A5 — resuelto en A4
- [ ] A6 — medir delay real sesion (lunes). Yahoo ya expone `exchangeDataDelayedBy`: 0 US, 15 min ITX.MC en PREAPERTURA. Confirmar REGULAR

### Bloque B — popups explicativos
- [x] B1 — `app/web/glosario.js`: 28 entradas, `buscarGlosario()`
- [x] B2 — popover: clic + hover, ESC cierra, accesible teclado y tap movil. CSS + JS
      nuevo. (La version original enganchaba clic pero no tabindex/role/teclado: los
      elementos `[data-glosario]` no eran alcanzables con Tab ni respondian a
      Enter/Espacio. Corregido con `makeFocusable()` + listener `keydown` delegado
      antes de verificar. Probado real: foco por Tab, apertura con Enter, cierre con
      Espacio sin scroll de pagina, en `.metrica` y en `th`/`.etiqueta`.)
- [x] B3 — `metrica()` engancha glosario via `data-*` + handler. Tambien cabeceras tabla cartera y tarjetas panel
- [x] B4 — test: todo label `metrica()` + `data-glosario` tiene entrada glosario (`prueba_web.py`)

### Bloque C — centro de ayuda
- [x] C1 — 5a pestana "Ayuda"
- [x] C2 — secciones: primeros pasos, pantallas, glosario buscable, origen datos, limites, backup, privacidad, FAQ
- [x] C3 — `?` cabeceras de cajas → cartilla emergente (popup card, no navega)
- [x] C4 — estados vacios enlazan ayuda relevante

Orden: A -> B1+B2+B3 -> C -> A6+B4.

## Pruebas

```bash
python pruebas/prueba_humo.py    # ~30 checks: finance (9 tools) + market (11 tools) + scoring.py puro
python pruebas/prueba_web.py     # ~23 checks: web API + defensas + B4 glosario
```

Subprocesos con DB temporal, sin tocar `datos/finanzas.db`. Red = AVISO, no fallo.

## Siguiente plan de accion

SeaLock se hizo investing-only: se elimino la gestion de cuentas/movimientos/
presupuestos/recurrentes (antiguos bloques D1/D2 de este plan) junto con las skills
`registro-finanzas` e `informe-patrimonio`. En su lugar: perfil inversor (`profile`)
y nota 1-10 (`scoring.py`, `stock_score`). Ver § Criterios de la nota.

### D3 — Presentacion y pulido
- [ ] D3a — mobile: nav tabs con scroll horizontal, forms responsive sin overflow
- [ ] D3b — focus-visible en botones y links, disabled states
- [ ] D3c — skeleton loading en vez de "Cargando..." texto plano
- [ ] D3d — retry buttons en estados de error
- [ ] D3e — modal de confirmacion custom (sustituir `confirm()` del browser)

### E — Integrar open source externo

Investigacion completa, con sub-tareas y justificacion, en
**[`docs/investigacion-repos.md`](docs/investigacion-repos.md)** (agosto 2026, datos
verificados via API de GitHub y descarga directa de archivos, no de blogs).

E1-E5 **no anaden ninguna dependencia**: todo se vendoriza con su licencia y se lee con
stdlib. Codigo de terceros va siempre con su `LICENSE` + `FUENTE.md` (repo, commit, fecha).

Orden recomendado: `E1 -> E2 -> E5a -> E3 -> E4 -> E6`.

- [ ] **E1 — Catalogo local de simbolos** — [FinanceDatabase](https://github.com/JerBouma/FinanceDatabase) (MIT, push 2026-08-16, 30 commits/90d)
  - [ ] E1a — vendorizar shards en `recursos/simbolos/` (**no** en `datos/`, que esta gitignored): `MCE PAR AMS MIL LSE GER FRA EBS LIS BRU STO CPH HEL OSL VIE ISE NMS NYQ` + OTC `OID PNK OQB OQX OBB`. <15 MB
  - [ ] E1b — `mcp/symbols.py`: carga perezosa con `csv` de stdlib, indice por symbol y por tokens del name (reutilizar `_tokens()` que ya existe)
  - [ ] E1c — `ticker_search` consulta local primero, Yahoo solo si no hay match. Elimina los 3 reintentos
  - [ ] E1d — sustituir la constante adivinada `OTC_CODES` por clasificacion real del catalogo
  - [ ] E1e — exponer `isin` y `mic` (desambigua `SAN` NYSE vs `SAN.MC` Madrid)
  - [ ] E1f — tests: `ITX.MC`/`SAN.MC`/`IBE.MC` resueltos **sin red** (critico, no AVISO)
- [ ] **E2 — Indicadores tecnicos** — [talipp](https://github.com/nardew/talipp) (MIT, **`requirements.txt` de 0 bytes = cero dependencias**)
  - [ ] E2a — vendorizar solo lo necesario en `mcp/indicators.py`
  - [ ] E2b — sustituir RSI/SMA manuales de `historical()`, **verificando equivalencia antes de borrar** (ojo: RSI de Wilder vs media simple)
  - [ ] E2c — anadir ATR, Bollinger, MACD
  - [ ] E2d — anadir ATR/Bollinger/MACD como metricas de `momentum` en `scoring.py` (mantener `verde`/`ambar`/`rojo`: son sufijos de clase CSS)
  - [ ] E2e — entradas nuevas en `glosario.js`. **El test B4 falla sin esto**
- [ ] **E3 — Metricas de cartera** — formulas de [quantstats](https://github.com/ranaroussi/quantstats) (Apache-2.0) portadas a stdlib. Sharpe, Sortino, Calmar + benchmark `^IBEX`. **Decidir**: tasa libre de riesgo como parametro (`SEALOCK_RISK_FREE_RATE`), nunca constante escondida
- [ ] **E4 — Senales y alertas** (absorbe "Alertas precio" del backlog). Tabla `alerts`, tools `alert_create`/`alerts_list`/`alert_delete`/`alert_toggle`, evaluacion en el refresco de 60s. **Describe hechos ("RSI de ITX.MC bajo 30"), nunca recomienda** — § Limites
- [ ] **E5 — Punto en el tiempo** — concepto de [qlib](https://github.com/microsoft/qlib) (MIT), solo la idea
  - [ ] E5a — **documentar la convencion antes de cualquier backtest**: al ver el cierre de T, se compra en T+1 y se vende en T+2, luego retorno = `T+2/T+1 - 1`, no `T+1/T`. Calcularlo mal produce backtests optimistas y falsos (lookahead bias). Barato ahora, caro despues: invalidaria resultados previos
  - [ ] E5b — auditar `period_return_pct` y las plusvalias con esa convencion
- [ ] **E6 — Ejecucion via broker** — [ib_async](https://github.com/ib-api-reloaded/ib_async) (BSD-2, unica con cobertura europea real, habla con TWS en localhost). **REQUIERE DECISION PREVIA**: contradice § Limites ("la nota es mecanica, no asesoramiento": ejecutar automaticamente es otra cosa) y § Principios (no es stdlib). Solo paper trading, actualizando antes esa seccion, y como extra opcional

**Sobre qlib** (evaluado a fondo porque es el mas citado del sector): MIT y 47.731
estrellas, pero **1 commit en 90 dias**, 6 en 180, 479 issues abiertas, y dependencias que
incluyen **Redis y MongoDB** ademas de 23 paquetes. Orientado al mercado chino (baostock,
CSI300). Se toma su convencion de punto en el tiempo (E5) y nada de codigo.

**Evitar por licencia**: OpenBB y Ghostfolio (AGPL-3.0), Freqtrade y pysystemtrade
(GPL-3.0), backtesting.py (AGPL-3.0), vectorbt (**Commons Clause: no es open source**).
**Muertos pese a las estrellas**: maybe-finance (archivado 2025-07), backtrader (sin
commits desde 2024-08), zipline (desde 2024-02).

**Bots de trading**: el ecosistema es abrumadoramente cripto (Freqtrade, Jesse, Hummingbot)
y no aplica a acciones en EUR. El camino realista es E4 (senales), no ejecucion automatica.

### Backlog adicional
- [ ] **A6** — medir delay real en sesion
- [ ] **Dark mode** — solo si se pide
- [ ] **Exportar datos** — CSV/JSON de la cartera
- [ ] **Grafico evolucion cartera** — linea temporal con valor total + plusvalia
- [ ] ~~**Alertas precio**~~ — absorbido por E4
- [ ] **Comparador acciones** — tabla lado a lado de fundamentales y notas
