# Investigacion de repositorios open source — agosto 2026

Busqueda de proyectos open source que SeaLock pueda integrar, vendorizar o forkear:
fuentes de datos de mercado, brokers, bots de trading y plataformas cuantitativas.

Todos los datos (estrellas, licencia, ultimo push, commits recientes, dependencias) estan
**verificados via API de GitHub y descarga directa de archivos el 2026-08-19**, no tomados
de listas de blogs. Varios proyectos muy citados resultaron estar muertos o dormidos pese
a tener decenas de miles de estrellas.

## Indice

- [Criterios de seleccion](#criterios-de-seleccion)
- [Parte 1 — Datos de mercado y simbolos](#parte-1--datos-de-mercado-y-simbolos)
- [Parte 2 — Analisis tecnico e indicadores](#parte-2--analisis-tecnico-e-indicadores)
- [Parte 3 — Plataformas cuantitativas y bots](#parte-3--plataformas-cuantitativas-y-bots)
- [Parte 4 — Brokers](#parte-4--brokers)
- [Parte 5 — Descartados](#parte-5--descartados)
- [Plan de accion detallado](#plan-de-accion-detallado)

## Criterios de seleccion

SeaLock impone restricciones duras que descartan a la mayoria de candidatos populares:

1. **Sin dependencias** — solo stdlib de Python (`CLAUDE.md` § Principios). Cualquier
   libreria que arrastre `pandas`/`numpy` solo sirve como *referencia de formulas o de
   arquitectura*, nunca como dependencia.
2. **Local primero** — el servidor `finance` no toca internet; `market` solo hace GET a
   fuentes publicas. Nada que exija cuenta obligatoria, clave de pago o servicio remoto.
3. **Licencia compatible** — MIT, Apache-2.0 y BSD permiten vendorizar con atribucion.
   GPL/AGPL son viricas: contaminarian SeaLock si se copia codigo. Commons Clause **no es
   open source**.
4. **Vivo de verdad** — no basta con que el repo no este archivado. Se contaron commits
   reales en ventanas de 90 y 180 dias. Varios proyectos famosos fallan aqui.
5. **Dominio correcto** — acciones y ETFs en EUR y mercados europeos. La inmensa mayoria
   de bots del ecosistema son de cripto y no aplican.

---

## Parte 1 — Datos de mercado y simbolos

### 1.1 FinanceDatabase — INTEGRAR

| Campo | Valor |
|---|---|
| Repo | **https://github.com/JerBouma/FinanceDatabase** |
| Licencia | **MIT** |
| Estrellas | 8.359 |
| Ultimo push | 2026-08-16 |
| Commits 90 dias | 30 |
| Datos | CSV plano, shardeado por mercado |

Base de datos de 300.000+ simbolos (acciones, ETFs, fondos, indices, divisas) con sector,
industria, pais, mercado, MIC e ISIN.

**Resuelve tres fallos documentados en `CLAUDE.md`:**

- *"Yahoo omite cotizacion principal europea"* — buscar "Inditex" devuelve Varsovia, Milan
  y Hamburgo, pero no Madrid.
- *"`ticker_search` intermitente; reintenta hasta 3 veces"*.
- *"ADR OTC se cuelan como primer resultado (`IBDSF`, `LVMUY`)"*.

**Verificacion hecha.** Se descargo `database/equities/MCE.csv` (Madrid, 216 KB) y se
comprobo contra los casos reales del proyecto:

```
ITX.MC | Industria de Diseno Textil, S.A. | EUR | MCE | XMAD | Spain
SAN.MC | Banco Santander, S.A.            | EUR | MCE | XMAD | Spain | ES0113900J37
IBE.MC | Iberdrola, S.A.                  | EUR | MCE | XMAD | Spain | ES0144580Y14
```

Columnas: `symbol, name, summary, currency, sector, industry_group, industry, exchange,
mic, market, country, state, city, zipcode, website, market_cap, isin, cusip, figi,
composite_figi, shareclass_figi, delisted`

**Encaje con las restricciones:**

- Los datos son **CSV plano** — se leen con el modulo `csv` de la stdlib. El paquete de pip
  necesita pandas; los CSV no necesitan nada.
- **Shardeado por mercado**: no hace falta clonar el repo (4,8 GB con historial). Solo los
  shards utiles.
- **Los codigos OTC que SeaLock hardcodea existen como shards propios**: `OID.csv`,
  `PNK.csv`, `OQB.csv`, `OQX.csv`, `OBB.csv`. La constante `OTC_CODES` de
  `mercado_server.py` deja de ser una lista adivinada.
- Busqueda **sin red** — encaja con "Local primero" y elimina reintentos.
- **MIT** — vendorizable conservando el aviso de copyright.

Shards europeos disponibles: `MCE` (Madrid), `PAR` (Paris), `AMS` (Amsterdam), `MIL`
(Milan), `LSE` (Londres), `GER`/`FRA`/`BER`/`DUS`/`HAM`/`MUN`/`STU` (Alemania), `EBS`
(Suiza), `LIS` (Lisboa), `BRU` (Bruselas), `STO` (Estocolmo), `CPH` (Copenhague), `HEL`
(Helsinki), `OSL` (Oslo), `VIE` (Viena), `ISE` (Dublin).

### 1.2 yfinance — ESTUDIAR, no depender

| Campo | Valor |
|---|---|
| Repo | **https://github.com/ranaroussi/yfinance** |
| Licencia | **Apache-2.0** |
| Estrellas | 25.021 |
| Ultimo push | 2026-08-13 |
| Commits 90 dias | 100+ (muy activo) |

Implementacion de referencia para los endpoints no oficiales de Yahoo. `mercado_server.py`
ya hace lo mismo (`_crumb()`, cookies, `quoteSummary`), pero yfinance parchea cada rotura
de Yahoo antes que nadie.

**Uso: leer su manejo de crumb/cookies cuando Yahoo rompa el acceso.** No anadir la
dependencia. Apache-2.0 permite copiar fragmentos con atribucion.

Yahoo lleva rompiendo integraciones desde el rediseno de febrero de 2025. Es el riesgo que
`CLAUDE.md` ya anticipa con el plan B de Finnhub/FMP.

### 1.3 FinanceToolkit — portar formulas

| Campo | Valor |
|---|---|
| Repo | **https://github.com/JerBouma/FinanceToolkit** |
| Licencia | **MIT** |
| Estrellas | 5.242 |
| Ultimo push | 2026-08-18 |

150+ ratios financieros calculados desde estados financieros crudos. Relevante para el
bloqueo del FCF europeo que impide el DCF en `ITX.MC`.

**No integrable**: requiere `pandas>=3.0`, `scikit-learn>=1.6`, `requests`, `yfinance`,
`openpyxl`, `pyyaml`. MIT permite **portar formulas concretas** a stdlib.

---

## Parte 2 — Analisis tecnico e indicadores

### 2.1 talipp — INTEGRAR

| Campo | Valor |
|---|---|
| Repo | **https://github.com/nardew/talipp** |
| Licencia | **MIT** |
| Estrellas | 534 |
| Ultimo push | 2025-09-09 |
| Commits 180 dias | 0 |
| **Dependencias** | **NINGUNA — `requirements.txt` es de 0 bytes** |

Analisis tecnico **incremental**: recalcula en O(1) al llegar un dato nuevo, frente a O(n)
recorriendo la serie entera. 50+ indicadores (RSI, MACD, ATR, ADX, Aroon, Bollinger,
Stochastic, familias completas de medias).

**Hallazgo decisivo, verificado descargando el archivo**: `requirements.txt` esta vacio
(0 bytes) y `setup.py` hace `install_requires=requirements`, es decir, lista vacia. **Cero
dependencias externas, Python stdlib puro.** Es el unico candidato de todo el ecosistema
de analisis tecnico que cumple la regla de SeaLock sin excepciones.

**Sobre los 0 commits en 180 dias.** Es matematica pura: calcular un RSI no cambia con el
tiempo. El riesgo de staleness aqui es cualitativamente distinto al de un scraper como
yfinance, que depende de que Yahoo no cambie. Ademas, MIT permite vendorizar solo los
indicadores necesarios y mantenerlos dentro del proyecto, con lo que el mantenimiento
upstream deja de importar.

Encaje inmediato: `historical()` en `mercado_server.py` ya calcula RSI 14, SMA 50 y SMA 200
a mano. talipp cubre eso y anade ATR, ADX, Bollinger y MACD sin coste de dependencias.

---

## Parte 3 — Plataformas cuantitativas y bots

### 3.1 microsoft/qlib — evaluado a fondo: SOLO IDEAS, no codigo

| Campo | Valor |
|---|---|
| Repo | **https://github.com/microsoft/qlib** |
| Licencia | **MIT** (permisiva, sin problema) |
| Estrellas | 47.731 (forks 7.566) |
| Ultimo push | 2026-07-23 |
| **Commits 90 dias** | **1** |
| **Commits 180 dias** | **6** |
| Issues abiertas | 479 |

Plataforma cuantitativa de Microsoft orientada a IA: pipeline completo de datos, modelos,
backtesting y ejecucion. El proyecto mas citado del sector y con licencia permisiva.

**Por que NO se integra, pese a MIT y 47.731 estrellas:**

**a) Esta practicamente dormido.** Un commit en 90 dias y seis en 180, con 479 issues
abiertas. Los ultimos commits reales son de abril de 2026. Uno de ellos arregla enlaces
rotos hacia [RD-Agent](https://github.com/microsoft/RD-Agent), que es donde Microsoft ha
movido el foco (14.275 estrellas, aunque tambien va lento: 3 commits en 90 dias). Un
proyecto en modo mantenimiento no es base para construir.

**b) Las dependencias son incompatibles a un nivel extremo.** Lista real de su
`pyproject.toml`:

```
pyyaml, numpy, pandas>=1.1, mlflow, filelock>=3.16.0, redis, dill, fire,
ruamel.yaml>=0.17.38, python-redis-lock, tqdm, pymongo, loguru, lightgbm,
gym, cvxpy, joblib, matplotlib, jupyter, nbconvert, pyarrow,
pydantic-settings, setuptools-scm
```

No son solo paquetes de Python: **exige Redis y MongoDB como infraestructura**. SeaLock es
una app local de un unico archivo SQLite lanzada con un `.bat`. La distancia es
insalvable, y no es cuestion de vendorizar un modulo suelto: el acoplamiento es total.

**c) Esta orientado al mercado chino.** Los commits recientes tocan `baostock` y la API de
Eastmoney para el calendario de negociacion. Los datasets de referencia son CSI300. La
cobertura europea no es su caso de uso.

**d) Resuelve otro problema.** qlib entrena modelos de machine learning para predecir
retornos. SeaLock registra gastos y valora empresas por fundamentales. No hay solape
funcional real.

**Lo que SI merece la pena robar (ideas, no codigo).** Su diseno de capa de datos es la
parte con valor transferible, y esta documentada:

1. **Separacion calendario / instrumentos / features.** qlib parte los datos en tres:
   `calendars/day.txt` (dias habiles), `instruments/` (universo de valores por pool) y
   `features/[ticker]/`. SeaLock mezcla hoy estos conceptos dentro de `historical()`. La
   separacion importa si algun dia hay backtesting.

2. **Prevencion de sesgo de anticipacion (lookahead bias)** — la idea mas valiosa. qlib
   usa la convencion `Ref($close, -2) / Ref($close, -1) - 1` para las etiquetas, no
   `T+1/T`. El motivo: al ver el cierre del dia T, la compra real ocurre en T+1 y la venta
   en T+2. Calcular retornos como si pudieras operar al cierre de T produce backtests
   optimistas y falsos. **Si SeaLock incorpora alguna vez senales o backtest, esta
   convencion hay que respetarla desde el primer dia**, porque reescribirla despues obliga
   a invalidar todos los resultados previos.

3. **Motor de expresiones para features.** Un DSL pequeno y elegante: `Ref($close, 60)`,
   `Mean($close, 3)`, `$rank($close) < 10`. Permite definir indicadores como cadenas de
   texto en vez de codigo. Sobredimensionado para SeaLock hoy, pero es la referencia si el
   glosario de metricas crece mucho.

4. **Alpha158** — conjunto documentado de 158 factores en
   `qlib.contrib.data.handler`. Util como catalogo de que factores existen y como se
   definen, aunque no se implementen.

**Veredicto**: MIT permite copiar, pero no hay nada copiable que encaje. El valor esta en
los cuatro conceptos anteriores, sobre todo el punto 2. Anotado como referencia
arquitectonica, no como dependencia ni como fork.

### 3.2 Bots de trading — el ecosistema no encaja

Busqueda especifica de bots de **acciones**, no de cripto. Resultado incomodo: el
ecosistema es abrumadoramente cripto.

| Repo | Estrellas | Licencia | Ultimo push | Veredicto |
|---|---|---|---|---|
| [Freqtrade](https://github.com/freqtrade/freqtrade) | 53.419 | GPL-3.0 | 2026-08-18 | Solo cripto (30+ exchanges via CCXT). Dominio equivocado + licencia virica |
| [Jesse](https://github.com/jesse-ai/jesse) | 8.343 | MIT | 2026-08-18 | Solo cripto |
| [Hummingbot](https://github.com/hummingbot/hummingbot) | 19.501 | Apache-2.0 | 2026-08-18 | Market making de cripto |
| [Nautilus Trader](https://github.com/nautechsystems/nautilus_trader) | 26.266 | LGPL-3.0 | 2026-08-19 | Nucleo en Rust, alta frecuencia. Sobredimensionado |
| [FinRL](https://github.com/AI4Finance-Foundation/FinRL) | 16.039 | MIT | 2026-07-13 | Reinforcement learning. Licencia OK pero exige torch/pandas; es investigacion, no producto |
| [pysystemtrade](https://github.com/pst-group/pysystemtrade) | 3.447 | GPL-3.0 | 2026-07-18 | Trading sistematico real de Rob Carver con IB. GPL virico |

### 3.3 Backtesting — todo problematico

| Repo | Estrellas | Licencia | Ultimo push | Veredicto |
|---|---|---|---|---|
| [backtesting.py](https://github.com/kernc/backtesting.py) | 8.866 | **AGPL-3.0** | 2026-08-05 | Activo y bueno, pero virico |
| [vectorbt](https://github.com/polakowo/vectorbt) | 8.720 | **Apache-2.0 + Commons Clause** | 2026-08-02 | **No es open source.** Prohibe vender software cuyo valor derive de el |
| [backtrader](https://github.com/mementum/backtrader) | 22.887 | GPL-3.0 | **2024-08-19** | Dos anos sin commits. Muerto |
| [zipline](https://github.com/quantopian/zipline) | 20.051 | Apache-2.0 | **2024-02-13** | Muerto tras cerrar Quantopian |
| [zipline-reloaded](https://github.com/stefan-jansen/zipline-reloaded) | 1.923 | Apache-2.0 | 2026-01-06 | Sucesor vivo del anterior. Licencia OK, pero pandas/numpy |

### 3.4 quantstats — portar formulas

| Campo | Valor |
|---|---|
| Repo | **https://github.com/ranaroussi/quantstats** |
| Licencia | **Apache-2.0** |
| Estrellas | 7.560 |
| Ultimo push | 2026-07-20 |

Analitica de carteras: Sharpe, Sortino, Calmar, drawdown, VaR, informes contra benchmark.
Mismo autor que yfinance. Requiere pandas/numpy, asi que **no es integrable**, pero las
formulas son portables a stdlib. SeaLock ya calcula volatilidad anualizada y max drawdown;
Sharpe y Sortino son la extension natural.

---

## Parte 4 — Brokers

Solo relevante si se decide implementar ejecucion de ordenes (ver aviso en el plan, fase E6).

| Repo | Estrellas | Licencia | Ultimo push | Nota |
|---|---|---|---|---|
| [ib_async](https://github.com/ib-api-reloaded/ib_async) | 1.715 | **BSD-2-Clause** | 2026-08-19 | Interactive Brokers. **Mejor opcion para mercados europeos** (`ITX.MC` incluido). Sucesor mantenido de `ib_insync`. Habla con TWS/IB Gateway en **localhost** |
| [alpaca-py](https://github.com/alpacahq/alpaca-py) | 1.463 | Apache-2.0 | 2026-08-14 | Cliente oficial de Alpaca. Paper trading gratuito. Cobertura sobre todo de EE.UU. |
| [tastytrade](https://github.com/tastyware/tastytrade) | 255 | MIT | 2026-08-07 | Cliente no oficial. Solo EE.UU. |
| [alpaca-trade-api-python](https://github.com/alpacahq/alpaca-trade-api-python) | 1.883 | Apache-2.0 | 2024-12-02 | **ARCHIVADO.** Sustituido por `alpaca-py` |

---

## Parte 5 — Descartados

| Repo | Estrellas | Licencia | Motivo |
|---|---|---|---|
| [OpenBB](https://github.com/OpenBB-finance/OpenBB) | 72.021 | **AGPL-3.0** | Verificado leyendo el LICENSE (GitHub lo reporta como NOASSERTION). Virico |
| [Ghostfolio](https://github.com/ghostfolio/ghostfolio) | 9.152 | **AGPL-3.0** | Producto par mas cercano (tracker self-hosted, muy activo). TypeScript + AGPL. Util para **ideas de interfaz**, no codigo |
| [maybe-finance](https://github.com/maybe-finance/maybe) | 54.322 | AGPL-3.0 | **ARCHIVADO** el 2025-07-24. Muerto pese a las estrellas |
| [Firefly III](https://github.com/firefly-iii/firefly-iii) | 24.368 | AGPL-3.0 | PHP + AGPL. Solo finanzas personales, sin inversion |
| [Portfolio Performance](https://github.com/portfolio-performance/portfolio) | 4.031 | EPL-1.0 | Java de escritorio. Buena referencia funcional europea, codigo no reutilizable |
| [beancount](https://github.com/beancount/beancount) | 5.915 | GPL-2.0 | Contabilidad de partida doble en texto plano. GPL |
| [ta (bukosabino)](https://github.com/bukosabino/ta) | 5.144 | MIT | Requiere pandas. talipp lo cubre sin dependencias |
| [TA-Lib](https://github.com/TA-Lib/ta-lib-python) | 12.194 | BSD-2-Clause | Wrapper Cython sobre libreria C: exige compilacion |
| [pandas-datareader](https://github.com/pydata/pandas-datareader) | 3.235 | BSD-3 | Requiere pandas por diseno |
| [yahooquery](https://github.com/dpguthrie/yahooquery) | 916 | MIT | Ultimo push 2025-05-15. Requiere pandas |

---

# Plan de accion detallado

Seis fases ordenadas por relacion valor/riesgo. **Las fases E1-E5 no anaden ninguna
dependencia de Python**: todo se vendoriza con su licencia y se lee con stdlib.

## Convenciones que aplican a todas las fases

- **Codigo en ingles** (`CLAUDE.md` § Convenciones): nombres de tool, parametros, claves de
  los dict devueltos y esquema de la base de datos. El copy de la interfaz sigue en espanol.
- **Codigo de terceros vendorizado** va con su LICENSE y un `FUENTE.md` que registre repo,
  commit exacto y fecha de descarga. Sin eso, dentro de un ano nadie sabe de donde salio.
- **Cada fase acaba con tests en verde**: `python pruebas/prueba_humo.py` (45 checks) y
  `python pruebas/prueba_web.py` (30 checks).
- **Cada tool MCP nueva es API web automatica** (`app/servidor.py` importa y despacha por
  nombre), asi que anadir una tool implica anadir tambien su test web.

---

## Fase E1 — Catalogo local de simbolos

**Fuente**: [FinanceDatabase](https://github.com/JerBouma/FinanceDatabase) (MIT)
**Ataca**: `ticker_search` intermitente, Yahoo omite cotizacion europea, `OTC_CODES` adivinada
**Riesgo**: bajo — indice de solo lectura; si falla, se cae a Yahoo como hoy

### E1a — Vendorizar los shards

- Crear `recursos/simbolos/` (**no** dentro de `datos/`, que esta en `.gitignore` y no debe
  versionarse).
- Descargar de `database/equities/` los shards: `MCE, PAR, AMS, MIL, LSE, GER, FRA, EBS,
  LIS, BRU, STO, CPH, HEL, OSL, VIE, ISE, NMS, NYQ`, mas los OTC `OID, PNK, OQB, OQX, OBB`.
- Anadir `recursos/simbolos/LICENSE` (MIT de FinanceDatabase, con su copyright) y
  `recursos/simbolos/FUENTE.md` (repo, commit SHA, fecha, lista de shards y por que esos).
- Tamano estimado total: < 15 MB. Verificar que no dispara el tamano del repo.

### E1b — Modulo de carga

- Nuevo `mcp/symbols.py`, sin dependencias, usando el modulo `csv` de stdlib.
- Carga **perezosa** (al primer uso, no al importar) y cacheada en memoria: arrancar el
  servidor MCP no debe leer 15 MB de CSV.
- Dos indices: por `symbol` exacto y por tokens normalizados del `name`. Reutilizar la
  funcion `_tokens()` que ya existe en `mercado_server.py` (quita acentos y formas
  juridicas como "SA", "Inc", "PLC") para que la normalizacion sea coherente.
- API interna: `lookup(symbol)`, `search(query, limit)`, `is_otc(exchange_code)`.

### E1c — `ticker_search` consulta local primero

- En `mercado_server.py`, `ticker_search()` busca en el catalogo local **antes** de salir a
  la red. Solo llama a Yahoo si no hay coincidencia local.
- Elimina los 3 reintentos con `time.sleep(0.4)` para el caso comun.
- Mantener el sondeo de mercados principales como respaldo para valores que no esten en el
  catalogo.
- Marcar el origen en la respuesta: `found_via: "catalogo local"` frente a `"yahoo"`.

### E1d — Sustituir `OTC_CODES`

- La constante hardcodeada `OTC_CODES = {"pnk", "oid", "obb", "oqb", "oqx", ...}` pasa a
  derivarse de la clasificacion real de mercado del catalogo.
- Conservar la funcion `_is_otc()` como interfaz, cambiando solo su implementacion, para no
  tocar a los llamantes.

### E1e — Exponer ISIN y MIC

- Anadir `isin` y `mic` a la respuesta de `ticker_search`.
- Motivo: identifican un valor sin ambiguedad, a diferencia del ticker. Resuelve el caso
  que `CLAUDE.md` marca como ambiguo: `SAN` (NYSE, USD) frente a `SAN.MC` (Madrid, EUR).
- Mostrar el ISIN en la ficha de la interfaz (`app/web/app.js`, cabecera de `viewStock`).

### E1f — Tests

En `pruebas/prueba_humo.py`:
- `ITX.MC`, `SAN.MC`, `IBE.MC` se resuelven **sin red** (test critico, no AVISO — es la
  gracia de la fase).
- Busqueda por nombre "Inditex" devuelve `ITX.MC` primero.
- `IBDSF` se degrada como OTC.
- El catalogo devuelve ISIN para `SAN.MC` (`ES0113900J37`).

En `pruebas/prueba_web.py`:
- `/api/market/ticker_search` devuelve `isin` y `mic`.

---

## Fase E2 — Indicadores tecnicos

**Fuente**: [talipp](https://github.com/nardew/talipp) (MIT, cero dependencias)
**Ataca**: `historical()` calcula RSI y SMA a mano; faltan ATR, Bollinger, MACD
**Riesgo**: bajo — matematica pura, verificable contra los valores actuales

### E2a — Vendorizar

- Crear `mcp/indicators.py` con solo los indicadores necesarios, no la libreria entera.
- Incluir `LICENSE` MIT de talipp y `FUENTE.md` con repo, commit y fecha.
- Mantener el caracter incremental (O(1) por dato nuevo): encaja con el auto-refresco de
  60 s que ya existe en la interfaz.

### E2b — Sustituir los calculos manuales

- En `historical()` de `mercado_server.py`, reemplazar el RSI 14 y las SMA 50/200 escritos a
  mano por los vendorizados.
- **Verificar equivalencia antes de borrar el codigo viejo**: mismos valores con los mismos
  datos de entrada, con tolerancia de redondeo. Si difieren, entender por que antes de
  seguir (puede haber una diferencia legitima en el suavizado del RSI: Wilder frente a
  media simple).

### E2c — Anadir indicadores nuevos

- ATR (volatilidad real, mejor que la desviacion tipica para stops).
- Bandas de Bollinger (posicion relativa dentro del rango).
- MACD (cruce de tendencia).
- Devolverlos en la respuesta de `historical()` con claves en ingles: `atr_14`,
  `bollinger_upper`, `bollinger_lower`, `macd`, `macd_signal`.

### E2d — Ampliar el veredicto

- `_verdict()` en `app/servidor.py` evalua hoy 5 dimensiones (valoracion, tendencia,
  solidez, dividendo, riesgo) con scores +1/0/-1.
- Incorporar los indicadores nuevos a la dimension de tendencia y riesgo.
- **Mantener los valores `verde`/`ambar`/`rojo` del campo `sentiment`**: `app.js` los usa
  directos como sufijo de clase CSS (`estilo.css` define `.veredicto-verde`).

### E2e — Glosario (obligatorio, no opcional)

- Anadir entradas en `app/web/glosario.js` para ATR, Bollinger y MACD, con `titulo`,
  `que_es`, `como_leerlo`, `trampa`, `rango`, `ideal` y `alias`.
- **El test B4 de `prueba_web.py` falla si una etiqueta con `data-glosario` no tiene
  entrada.** No es opcional: la fase no cierra sin esto.

### E2f — Tests

- Valores de RSI/SMA identicos a los actuales (test de no regresion).
- Los indicadores nuevos aparecen en la respuesta de `historical()`.
- B4 sigue en verde con las etiquetas nuevas.

---

## Fase E3 — Metricas de cartera

**Fuente**: formulas de [quantstats](https://github.com/ranaroussi/quantstats) (Apache-2.0), portadas a stdlib
**Riesgo**: bajo, pero exige una decision de parametro

### E3a — Ratios ajustados por riesgo

- Portar Sharpe, Sortino y Calmar a `app/servidor.py` o a un modulo nuevo.
- **Decision necesaria**: la tasa libre de riesgo. Debe ser un **parametro configurable**
  (variable de entorno `SEALOCK_RISK_FREE_RATE` con valor por defecto), nunca una constante
  escondida — cambia por ano y por divisa, y falsea el Sharpe si se deja fija.

### E3b — Comparativa contra benchmark

- Reutilizar `historical()` para traer `^IBEX` o `^GSPC`.
- Calcular alfa y beta de la cartera frente al indice.
- Exponerlo en `portfolio_valued()`.

### E3c — Interfaz y glosario

- Mostrar los ratios en las tarjetas de la pestana Cartera.
- Anadir sus entradas al glosario (mismo requisito del test B4).

---

## Fase E4 — Senales y alertas

Absorbe el punto de backlog *"Alertas precio — notificacion local cuando accion cruza
umbral"* y lo hace mas util sin cruzar la linea del asesoramiento.

**Riesgo**: bajo tecnicamente. El cuidado esta en la redaccion de los textos.

### E4a — Esquema

- Tabla `alerts` (ingles, como el resto del esquema): `id, ticker, metric, operator,
  threshold, active, created_at, last_triggered_at`.
- La migracion va en `_migrate_legacy_schema()` o como `CREATE TABLE IF NOT EXISTS` en
  `SCHEMA` — al ser tabla nueva, basta lo segundo.

### E4b — Tools MCP

- `alert_create`, `alerts_list`, `alert_delete`, `alert_toggle` en `finanzas_server.py`.
- Al ser tools MCP, quedan expuestas como API web automaticamente.

### E4c — Evaluacion

- Evaluar en el refresco de 60 s que ya existe en `app.js` (`startAutoRefresh`).
- Respetar el gate que ya hay: no evaluar si `document.hidden` ni si el mercado esta
  `CLOSED`.

### E4d — Presentacion (el punto delicado)

- **Los avisos describen hechos, nunca recomiendan.** "RSI de ITX.MC bajo 30" es
  aceptable; "momento de comprar ITX.MC" no lo es.
- Esto no es cosmetico: `CLAUDE.md` § Limites dice *"Sin asesoramiento financiero
  personalizado. Analisis si; 'compra esto' no."*
- Revisar tambien los textos del glosario asociados para que no deriven en recomendacion
  implicita.

### E4e — Tests

- Crear, listar, disparar y borrar una alerta.
- Una alerta desactivada no dispara.

---

## Fase E5 — Punto en el tiempo (idea tomada de qlib)

**Fuente**: concepto de [qlib](https://github.com/microsoft/qlib) (MIT). Ninguna linea de
codigo suya, solo la convencion.

**Hacer esto ANTES que cualquier backtest.** Es la fase mas barata ahora y la mas cara
despues: si se calculan retornos con la convencion equivocada, todos los resultados
historicos quedan invalidados y hay que rehacerlos.

### E5a — Documentar la convencion

- Escribir en `CLAUDE.md` la regla: al ver el cierre del dia T, la compra real ocurre en
  T+1 y la venta en T+2. Los retornos se calculan `T+2 / T+1 - 1`, no `T+1 / T`.
- Motivo: calcular como si pudieras operar al cierre de T produce backtests optimistas y
  falsos (sesgo de anticipacion, *lookahead bias*).

### E5b — Auditar lo que ya existe

- Revisar `period_return_pct` en `historical()` y los calculos de plusvalia en
  `portfolio_valued()`.
- Para valorar una posicion **ya abierta** la convencion no aplica (no hay decision futura
  simulada), pero conviene dejarlo escrito para que nadie lo confunda mas adelante.

### E5c — Separar calendario, universo y features

- Idea tomada del diseno de qlib: hoy `historical()` mezcla los tres conceptos.
- No hace falta refactorizar ya. Anotarlo como forma correcta si llega el backtesting.

---

## Fase E6 — Ejecucion via broker — REQUIERE DECISION PREVIA

**Fuente candidata**: [ib_async](https://github.com/ib-api-reloaded/ib_async) (BSD-2)

**No implementar sin decidir antes la cuestion de producto.** Ejecutar ordenes contradice
dos cosas escritas en `CLAUDE.md`:

- § Limites: *"Sin asesoramiento financiero personalizado. Analisis si; 'compra esto' no."*
- § Principios: *"Sin dependencias. Solo stdlib Python."* — `ib_async` no es stdlib.

Si aun asi se decide seguir:

- **Solo paper trading** al principio, sin excepcion.
- Actualizar antes la seccion Limites de `CLAUDE.md` para reflejar el cambio de alcance.
  Dejar el documento diciendo una cosa y el codigo haciendo otra es peor que cualquier bug.
- `ib_async` habla con TWS o IB Gateway en **localhost**, asi que "Local primero" se
  mantiene: no hay servicio de terceros en la nube.
- Aislarlo como **extra opcional**: SeaLock debe seguir arrancando y pasando los tests sin
  `ib_async` instalado.
- Es la unica opcion con cobertura europea real (`ITX.MC`, `SAN.MC`). Alpaca es sobre todo
  EE.UU.

---

## Orden recomendado y justificacion

```
E1  (simbolos)     -> mayor valor, menor riesgo. Ataca el fallo mas documentado
E2  (indicadores)  -> cero dependencias, mejora visible en la ficha
E5a (convencion)   -> barato ahora, caro despues. Solo documentar
E3  (metricas)     -> requiere decidir la tasa libre de riesgo
E4  (alertas)      -> cierra un punto del backlog
E6  (broker)       -> solo tras decision explicita de alcance
```

`E5a` se adelanta a proposito: es solo escribir la convencion en `CLAUDE.md`, cuesta
minutos, y evita tener que invalidar calculos si mas adelante llega el backtesting.

## Resumen ejecutivo

- **Integrar**: [FinanceDatabase](https://github.com/JerBouma/FinanceDatabase) (MIT,
  simbolos) y [talipp](https://github.com/nardew/talipp) (MIT, indicadores, cero
  dependencias). Ambos vendorizables, ambos atacan fallos ya documentados.
- **Estudiar sin integrar**: [yfinance](https://github.com/ranaroussi/yfinance) (parches de
  Yahoo), [quantstats](https://github.com/ranaroussi/quantstats) (formulas),
  [qlib](https://github.com/microsoft/qlib) (convencion de punto en el tiempo),
  [Ghostfolio](https://github.com/ghostfolio/ghostfolio) (ideas de interfaz).
- **qlib en concreto**: MIT y 47.731 estrellas, pero **1 commit en 90 dias**, 479 issues
  abiertas, y dependencias que incluyen **Redis y MongoDB**. Se toma la idea de prevencion
  de sesgo de anticipacion (fase E5), nada de codigo.
- **Evitar**: todo AGPL/GPL (OpenBB, Ghostfolio, Freqtrade, backtesting.py, pysystemtrade)
  y vectorbt (Commons Clause, no es open source).
- **Bots de trading**: el ecosistema es abrumadoramente cripto y no aplica a una app de
  acciones en EUR. El camino realista es senales (E4), no ejecucion automatica. Si se
  quiere ejecucion, `ib_async` (BSD-2) es la unica opcion con cobertura europea, y exige
  revisar antes los limites del proyecto.

Fecha de la investigacion: **2026-08-19**. Reverificar la actividad de los repos antes de
integrar si pasa mas de un semestre — este mismo estudio encontro tres proyectos muy
citados que estaban muertos y uno famoso que estaba dormido.
