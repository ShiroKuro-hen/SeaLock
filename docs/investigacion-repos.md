# Investigacion de repositorios open source — agosto 2026

Busqueda de proyectos open source **activos** que SeaLock pueda integrar, vendorizar o
forkear: fuentes de datos de mercado, brokers y bots de trading.

Todos los datos (estrellas, licencia, ultimo push, actividad) estan **verificados via API
de GitHub el 2026-08-19**, no tomados de listas de blogs. Los proyectos archivados o sin
commits recientes estan marcados como tales.

## Criterios de seleccion

SeaLock impone restricciones duras que descartan a la mayoria de candidatos populares:

1. **Sin dependencias** — solo stdlib de Python (`CLAUDE.md` § Principios). Cualquier
   libreria que arrastre `pandas`/`numpy` solo sirve como *referencia de formulas*, no
   como dependencia.
2. **Local primero** — el servidor `finance` no toca internet; `market` solo hace GET a
   fuentes publicas. Nada que exija cuenta, clave obligatoria o servicio remoto.
3. **Licencia compatible** — MIT, Apache-2.0 y BSD permiten vendorizar con atribucion.
   GPL/AGPL son viricas: contaminarian SeaLock si se copia codigo. Commons Clause **no
   es open source**.
4. **Activo** — descartado todo lo archivado o sin commits en ~12 meses.
5. **Dominio correcto** — acciones y ETFs en EUR/mercados europeos. Los bots de cripto
   (la inmensa mayoria del ecosistema) no aplican.

---

## Parte 1 — Datos de mercado y simbolos

### 1.1 FinanceDatabase — RECOMENDADO

| | |
|---|---|
| Repo | https://github.com/JerBouma/FinanceDatabase |
| Licencia | **MIT** |
| Estrellas | 8.359 |
| Ultimo push | 2026-08-16 |
| Actividad | 30 commits en 90 dias |
| Lenguaje | Python (los datos son CSV plano) |

Base de datos de 300.000+ simbolos (acciones, ETFs, fondos, indices, divisas) con sector,
industria, pais, mercado, MIC e ISIN.

**Por que resuelve un problema real de SeaLock.** `CLAUDE.md` documenta tres fallos
concretos que esta base corrige:

- *"Yahoo omite cotizacion principal europea"* — buscar "Inditex" en Yahoo devuelve
  Varsovia, Milan y Hamburgo, pero no Madrid.
- *"`ticker_search` intermitente; reintenta hasta 3 veces"*.
- *"ADR OTC se cuelan como primer resultado (`IBDSF`, `LVMUY`)"*.

Comprobado descargando el shard de Madrid (`database/equities/MCE.csv`, 216 KB) contra los
casos reales del proyecto:

```
ITX.MC | Industria de Diseno Textil, S.A. | EUR | MCE | XMAD | Spain
SAN.MC | Banco Santander, S.A.            | EUR | MCE | XMAD | Spain | ES0113900J37
IBE.MC | Iberdrola, S.A.                  | EUR | MCE | XMAD | Spain | ES0144580Y14
```

Columnas disponibles:
`symbol, name, summary, currency, sector, industry_group, industry, exchange, mic, market,
country, state, city, zipcode, website, market_cap, isin, cusip, figi, composite_figi,
shareclass_figi, delisted`

**Encaje tecnico con las restricciones de SeaLock:**

- Los datos son **CSV plano** — se leen con el modulo `csv` de la stdlib. El paquete de
  pip necesita pandas; los CSV no necesitan nada. Se respeta la regla de cero dependencias.
- Esta **shardeado por mercado**, asi que no hace falta clonar el repo entero (4,8 GB con
  historial). Solo los shards utiles: `MCE` (Madrid), `PAR`, `AMS`, `MIL`, `LSE`, `GER`,
  `EBS`, `LIS`, `BRU`, `STO`, `CPH`, `HEL`, `OSL`, `VIE`, `ISE`, mas `NMS`/`NYQ` para EE.UU.
- **Los codigos OTC que SeaLock ya tiene hardcodeados existen como shards propios**:
  `OID.csv`, `PNK.csv`, `OQB.csv`, `OQX.csv`, `OBB.csv`. La constante `OTC_CODES` de
  `mercado_server.py` deja de ser una lista adivinada y pasa a ser verificable.
- Busqueda **sin red** — encaja con el principio "Local primero" y elimina la
  intermitencia y los reintentos.
- **MIT** — se puede vendorizar conservando el aviso de copyright.

### 1.2 yfinance — estudiar, no depender

| | |
|---|---|
| Repo | https://github.com/ranaroussi/yfinance |
| Licencia | **Apache-2.0** |
| Estrellas | 25.021 |
| Ultimo push | 2026-08-13 |
| Actividad | 100+ commits en 90 dias (muy activo) |

Es la implementacion de referencia para hablar con los endpoints no oficiales de Yahoo.
`mercado_server.py` ya hace lo mismo (`_crumb()`, cookies, `quoteSummary`), pero yfinance
parchea cada rotura de Yahoo antes que nadie.

**Uso recomendado: leer su manejo de crumb/cookies cuando Yahoo rompa el acceso.** No
anadir la dependencia. Apache-2.0 permite copiar fragmentos con atribucion.

Aviso relevante del ecosistema: Yahoo lleva rompiendo integraciones desde el rediseno de
febrero de 2025, y la recomendacion general es no depender de estos endpoints para
sistemas criticos. Es exactamente el riesgo que `CLAUDE.md` ya anticipa con el plan B de
Finnhub/FMP.

### 1.3 FinanceToolkit — solo portar formulas

| | |
|---|---|
| Repo | https://github.com/JerBouma/FinanceToolkit |
| Licencia | **MIT** |
| Estrellas | 5.242 |
| Ultimo push | 2026-08-18 |

150+ ratios financieros calculados a partir de estados financieros crudos. Relevante para
el bloqueo del FCF europeo que impide el DCF en valores como `ITX.MC`.

**No integrable como dependencia**: requiere `pandas>=3.0`, `scikit-learn>=1.6`,
`requests`, `yfinance`, `openpyxl`, `pyyaml`. MIT permite **portar formulas concretas** a
stdlib en lugar de instalarlo.

---

## Parte 2 — Bots de trading y senales

### Aviso de alcance previo

`CLAUDE.md` § Limites dice: *"Sin asesoramiento financiero personalizado. Analisis si;
'compra esto' no."* Un bot que **ejecuta ordenes automaticamente** contradice ese limite y
tambien el principio "Local primero" (exige cuenta de broker y red).

Hay tres niveles posibles, de menor a mayor conflicto:

| Nivel | Que hace | Conflicto con los limites |
|---|---|---|
| **A. Senales** | Calcula indicadores y marca condiciones (RSI < 30, cruce de medias) | Ninguno — SeaLock ya calcula RSI y medias moviles |
| **B. Backtest** | Prueba una estrategia contra historico y da metricas | Ninguno — es analisis, no recomendacion |
| **C. Ejecucion** | Manda ordenes reales o de paper trading a un broker | **Directo** — requiere revisar la seccion Limites |

La recomendacion es empezar por A y B, que aportan valor sin romper nada. El nivel C es
una decision de producto, no tecnica.

### 2.1 talipp — RECOMENDADO para el nivel A

| | |
|---|---|
| Repo | https://github.com/nardew/talipp |
| Licencia | **MIT** |
| Estrellas | 534 |
| Ultimo push | 2025-09-09 |
| Actividad | 0 commits en 180 dias (estable, no abandonado) |

Libreria de analisis tecnico **incremental**: recalcula en O(1) al llegar un dato nuevo,
en vez de O(n) recorriendo toda la serie. 50+ indicadores (RSI, MACD, ATR, ADX, Aroon,
Bollinger, Stochastic, medias de todo tipo).

**Hallazgo decisivo: `requirements.txt` esta vacio (0 bytes). Cero dependencias
externas — Python stdlib puro.** Es el unico candidato de todo el ecosistema de analisis
tecnico que cumple la regla de SeaLock sin excepciones.

Sobre la falta de commits recientes: es una libreria de **matematica pura** (calcular un
RSI no cambia con el tiempo). El riesgo de staleness aqui es mucho menor que en un scraper
como yfinance, que depende de que Yahoo no cambie. Ademas, MIT permite vendorizar solo los
indicadores necesarios y mantenerlos dentro del proyecto.

Encaje inmediato: `historical()` en `mercado_server.py` ya calcula RSI 14, SMA 50 y SMA 200
a mano. talipp cubre eso y anade ATR, ADX, Bollinger y MACD sin coste de dependencias.

### 2.2 microsoft/qlib — referencia arquitectonica

| | |
|---|---|
| Repo | https://github.com/microsoft/qlib |
| Licencia | **MIT** |
| Estrellas | 47.731 |
| Ultimo push | 2026-07-23 |

Plataforma cuantitativa de Microsoft orientada a IA: pipeline completo de datos, modelos,
backtesting y ejecucion. Es el proyecto mas serio y mejor mantenido del sector, con
licencia permisiva.

**Demasiado pesado para integrar** (pandas, numpy, pytorch). Util como **referencia de
arquitectura** si algun dia SeaLock crece hacia backtesting: como separan datos, señales,
estrategia y ejecucion.

### 2.3 quantstats — referencia de metricas de cartera

| | |
|---|---|
| Repo | https://github.com/ranaroussi/quantstats |
| Licencia | **Apache-2.0** |
| Estrellas | 7.560 |
| Ultimo push | 2026-07-20 |

Analitica de carteras: Sharpe, Sortino, Calmar, drawdown, VaR, informes comparados contra
un benchmark. Del mismo autor que yfinance.

Requiere pandas/numpy, asi que **no es integrable**, pero las formulas son portables a
stdlib y encajarian en el `informe-patrimonio`. SeaLock ya calcula volatilidad anualizada
y max drawdown; Sharpe y Sortino serian la extension natural.

### 2.4 Brokers — solo si se decide el nivel C

| Repo | Licencia | Estrellas | Ultimo push | Nota |
|---|---|---|---|---|
| [alpaca-py](https://github.com/alpacahq/alpaca-py) | Apache-2.0 | 1.463 | 2026-08-14 | Cliente oficial de Alpaca. **Paper trading gratuito.** Cobertura sobre todo de EE.UU. |
| [ib_async](https://github.com/ib-api-reloaded/ib_async) | BSD-2-Clause | 1.715 | 2026-08-19 | Interactive Brokers. **La mejor opcion para mercados europeos** (`ITX.MC` incluido). Sucesor mantenido de `ib_insync` |
| [tastytrade](https://github.com/tastyware/tastytrade) | MIT | 255 | 2026-08-07 | Cliente no oficial. Solo EE.UU. |

Si algun dia se implementa el nivel C, **`ib_async` es el candidato correcto** por
cobertura europea y licencia BSD. Exige TWS o IB Gateway corriendo en local, lo cual es
compatible con "Local primero" (no hay nube de terceros, el broker se habla en localhost).

---

## Parte 3 — Descartados y por que

| Repo | Estrellas | Licencia | Motivo del descarte |
|---|---|---|---|
| [OpenBB](https://github.com/OpenBB-finance/OpenBB) | 72.021 | **AGPL-3.0** | Verificado leyendo el LICENSE (GitHub lo reporta como NOASSERTION). Virico: copiar codigo obligaria a publicar SeaLock bajo AGPL |
| [Ghostfolio](https://github.com/ghostfolio/ghostfolio) | 9.152 | **AGPL-3.0** | Producto par mas cercano (tracker de cartera self-hosted, muy activo). TypeScript + AGPL. Util para **robar ideas de interfaz**, no codigo |
| [Freqtrade](https://github.com/freqtrade/freqtrade) | 53.419 | GPL-3.0 | Solo cripto, 30+ exchanges via CCXT. Dominio equivocado + licencia virica |
| [Nautilus Trader](https://github.com/nautechsystems/nautilus_trader) | 26.266 | LGPL-3.0 | Nucleo en Rust, orientado a alta frecuencia. Sobredimensionado |
| [backtrader](https://github.com/mementum/backtrader) | 22.887 | GPL-3.0 | **Ultimo push 2024-08-19** — dos anos sin tocar. Efectivamente sin mantenimiento |
| [zipline](https://github.com/quantopian/zipline) | 20.051 | Apache-2.0 | **Ultimo push 2024-02-13.** Muerto tras el cierre de Quantopian. Existe [zipline-reloaded](https://github.com/stefan-jansen/zipline-reloaded) (1.923, push 2026-01-06) como sucesor |
| [maybe-finance](https://github.com/maybe-finance/maybe) | 54.322 | AGPL-3.0 | **ARCHIVADO** el 2025-07-24. Muerto pese a las estrellas |
| [vectorbt](https://github.com/polakowo/vectorbt) | 8.720 | **Apache-2.0 + Commons Clause** | **No es open source.** La Commons Clause prohibe vender software cuyo valor derive de el. Uso personal permitido; incompatible con un proyecto que quiera mantener libertad de licencia |
| [backtesting.py](https://github.com/kernc/backtesting.py) | 8.866 | AGPL-3.0 | Activo (push 2026-08-05) y bueno, pero AGPL |
| [pysystemtrade](https://github.com/pst-group/pysystemtrade) | 3.447 | GPL-3.0 | Trading sistematico real de Rob Carver, con IB. GPL virico |
| [FinRL](https://github.com/AI4Finance-Foundation/FinRL) | 16.039 | MIT | Reinforcement learning para trading. Licencia OK pero exige torch/pandas y es investigacion, no producto |
| [jesse](https://github.com/jesse-ai/jesse) | 8.343 | MIT | Solo cripto |
| [alpaca-trade-api-python](https://github.com/alpacahq/alpaca-trade-api-python) | 1.883 | Apache-2.0 | **ARCHIVADO.** Sustituido por `alpaca-py` |
| [ta (bukosabino)](https://github.com/bukosabino/ta) | 5.144 | MIT | Requiere pandas. talipp lo cubre sin dependencias |
| [TA-Lib](https://github.com/TA-Lib/ta-lib-python) | 12.194 | BSD-2-Clause | Wrapper Cython sobre una libreria C: exige compilacion. Rompe "sin dependencias" de forma agresiva |

---

## Plan de accion propuesto

Ordenado por relacion valor/riesgo. Ninguna fase anade dependencias de Python.

### Fase E1 — Catalogo local de simbolos (FinanceDatabase, MIT)

Ataca el fallo mas documentado del proyecto: la resolucion de tickers europeos.

- **E1a** — Vendorizar los shards de `FinanceDatabase/database/equities/` relevantes en
  `datos/simbolos/` (o mejor `recursos/simbolos/`, para que no caiga bajo el `.gitignore`
  de `datos/`): `MCE, PAR, AMS, MIL, LSE, GER, EBS, LIS, BRU, STO, CPH, HEL, OSL, VIE,
  ISE, NMS, NYQ`. Total estimado < 15 MB. Incluir el aviso de copyright MIT y un
  `FUENTE.md` con repo, commit y fecha de descarga.
- **E1b** — Modulo `mcp/simbolos.py`: carga perezosa de los CSV con el modulo `csv` de
  stdlib, indice en memoria por `symbol` y por tokens del `name`. Sin red.
- **E1c** — `ticker_search` consulta **primero el catalogo local**; solo cae a Yahoo si no
  hay coincidencia. Elimina los 3 reintentos y la intermitencia documentada.
- **E1d** — Sustituir la constante `OTC_CODES` adivinada por la clasificacion real de
  mercado del catalogo (los shards `OID/PNK/OQB/OQX/OBB` identifican OTC de forma
  verificable).
- **E1e** — Exponer `isin` y `mic` en la respuesta de `ticker_search`: identifican un valor
  sin ambiguedad, a diferencia del ticker (el caso `SAN` NYSE vs `SAN.MC` Madrid que
  `CLAUDE.md` marca como ambiguo).
- **E1f** — Tests: `ITX.MC`, `SAN.MC`, `IBE.MC` resueltos **sin red**; `IBDSF` degradado
  como OTC; busqueda por nombre ("Inditex") devuelve Madrid primero.

**Riesgo**: bajo. Es un indice de solo lectura; si falla, se cae a Yahoo como hoy.

### Fase E2 — Indicadores tecnicos (talipp, MIT)

- **E2a** — Vendorizar de talipp solo los indicadores necesarios en `mcp/indicadores.py`,
  conservando la licencia MIT y la atribucion.
- **E2b** — Sustituir los calculos manuales de RSI/SMA de `historical()` por los
  vendorizados, y anadir ATR, Bollinger y MACD.
- **E2c** — Ampliar `_verdict()` con las nuevas dimensiones (hoy evalua 5).
- **E2d** — Anadir las entradas nuevas a `glosario.js` — hay un test (B4) que exige que
  toda etiqueta con `data-glosario` tenga entrada, asi que esto no es opcional.

**Riesgo**: bajo. Matematica pura, verificable contra los valores actuales.

### Fase E3 — Metricas de cartera (formulas de quantstats, Apache-2.0)

- **E3a** — Portar Sharpe, Sortino y Calmar a stdlib dentro de `portfolio_valued()`.
- **E3b** — Comparativa contra benchmark (`^IBEX`, `^GSPC`) reutilizando `historical()`.

**Riesgo**: bajo, pero requiere decidir la tasa libre de riesgo (parametro, no constante).

### Fase E4 — Senales y alertas (sin broker)

Convierte el backlog *"Alertas precio — notificacion local cuando accion cruza umbral"* en
algo mas util, sin cruzar la linea del asesoramiento.

- **E4a** — Tabla `alerts` (ticker, condicion, umbral, activa).
- **E4b** — Evaluacion en el refresco de 60 s que ya existe.
- **E4c** — Aviso en la interfaz. **Describe el hecho ("RSI de ITX.MC bajo 30"), nunca
  recomienda una accion.** Asi se respeta el limite documentado.

**Riesgo**: bajo tecnicamente. Conviene redactar los textos con cuidado para no derivar en
recomendaciones implicitas.

### Fase E5 — Ejecucion via broker (`ib_async`, BSD-2) — REQUIERE DECISION

**No implementar sin decidir antes la cuestion de producto.** Ejecutar ordenes contradice
`CLAUDE.md` § Limites. Si se decide seguir:

- Empezar **solo con paper trading**.
- Actualizar la seccion Limites de `CLAUDE.md` para reflejar el cambio de alcance.
- `ib_async` habla con TWS/IB Gateway en **localhost**, lo cual mantiene el principio
  "Local primero" (no hay servicio de terceros en la nube).
- Rompe la regla de cero dependencias: `ib_async` no es stdlib. Habria que aislarlo como
  extra opcional, de modo que SeaLock siga funcionando sin el instalado.

---

## Resumen ejecutivo

- **Integrar ya**: FinanceDatabase (MIT) para simbolos, talipp (MIT) para indicadores.
  Ambos vendorizables, ambos sin dependencias, ambos atacan fallos ya documentados.
- **Estudiar sin integrar**: yfinance (parches de Yahoo), quantstats (formulas), qlib
  (arquitectura), Ghostfolio (ideas de interfaz).
- **Evitar**: todo lo AGPL/GPL (OpenBB, Ghostfolio, Freqtrade, backtesting.py,
  pysystemtrade) y vectorbt (Commons Clause, no es open source).
- **Bot de trading**: el ecosistema es abrumadoramente de cripto y no aplica. Para
  acciones, el camino realista es senales + backtest (niveles A y B), no ejecucion
  automatica. Si se quiere ejecucion, `ib_async` (BSD-2) es la unica opcion con buena
  cobertura europea, y exige revisar los limites del proyecto primero.

Fecha de la investigacion: 2026-08-19. Conviene reverificar la actividad de los repos
antes de integrar si pasa mas de un semestre.
