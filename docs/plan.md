# SeaLock — Plan de trabajo en historias de usuario

Fuente unica de lo pendiente. Sustituye a las secciones "Siguiente plan de accion",
"Backlog adicional" y al A6 suelto de `CLAUDE.md`/`AGENTS.md`, que ahora solo enlazan
aqui. Redactado 2026-09-25. Actualizado 2026-09-26: hechas HU-00, HU-01, HU-02, HU-03,
HU-04, HU-05, HU-06, HU-07, HU-08, HU-09, HU-10, HU-11, HU-12, HU-13, HU-14, HU-17, HU-18,
HU-20, HU-21, HU-28, HU-30, HU-31, HU-32, HU-33 y HU-34. Epicas 1, 2 y 3 cerradas; la 4
(Hidden Gems) cerrada entera.

Convencion: `[ ]` pendiente, `[x]` hecho. Una HU se marca hecha solo cuando cumple
todos sus criterios de aceptacion **y** la Definicion de Hecho global.

---

## 0. Decisiones ya tomadas

| # | Pregunta | Decision |
|---|---|---|
| D1 | Universo de Hidden Gems | **Mundial** (EE. UU., Europa, Asia-Pacifico, America Latina...), no solo blue chips |
| D2 | Revision de Claude | **Ajuste numerico acotado (+-2) mas nota escrita**, siempre visible junto a la nota mecanica |
| D3 | Peso del pilar trimestral | **~15 puntos**, restados de Crecimiento y Momentum (tabla exacta en HU-02) |

## 1. Hechos verificados antes de planificar (2026-09-25)

- `https://query1.finance.yahoo.com/ws/fundamentals-timeseries/v1/finance/timeseries/<TICKER>`
  devuelve trimestres (`quarterlyTotalRevenue`, `quarterlyNetIncome`,
  `quarterlyDilutedEPS`, `quarterlyFreeCashFlow`...):
  - `AAPL`: 3 ultimos trimestres completos (2025-12-31, 2026-03-31, 2026-06-30).
  - `ITX.MC`: hueco (2025-10-31 -> 2026-04-30, falta el cierre de enero) y **sin FCF**.
  - `SAN.MC`: ventas y beneficio completos, **sin FCF**, BPA desfasado un trimestre.
  - Conclusion: en Europa habra huecos; el plan obliga a detectarlos y avisarlos.
- `v1/finance/screener/predefined/saved?scrIds=undervalued_growth_stocks` funciona,
  pero solo devuelve valores de EE. UU. Para el resto del mundo hace falta catalogo propio.
- `news(ticker, query)` ya acepta texto libre; `fundamentals()` ya devuelve `sector`,
  `industry`, `market_cap` y `num_analysts`.

## 2. Definicion de Hecho (aplica a TODAS las HU)

- [ ] `python pruebas/prueba_humo.py` y `python pruebas/prueba_web.py` pasan (red = AVISO, no fallo).
- [ ] Toda logica no trivial nueva deja al menos un check que falla si la logica se rompe.
- [ ] Cero dependencias nuevas: solo stdlib Python y HTML/CSS/JS sin frameworks.
- [ ] Codigo, claves de dict, tools y esquema en ingles; copy de UI y textos `_es` en espanol.
- [ ] Toda metrica o etiqueta nueva con `data-glosario` tiene entrada en `glosario.js` (test B4).
- [ ] `CLAUDE.md` y `AGENTS.md` actualizados si cambia arquitectura, criterios de la nota o API.
- [ ] Pestana Ayuda actualizada si cambia algo visible para el usuario.
- [ ] Ningun dato del usuario (cartera, perfil, revisiones) sale en peticiones externas.
- [ ] Texto externo (noticias, nombres de empresa) se pinta como texto, nunca como HTML.
- [ ] Probado en la app real (`SeaLock.bat`), no solo en tests.

## 3. Orden y dependencias

```
HU-00
  -> EPICA 1 (HU-01 -> HU-02 -> HU-03 -> HU-04)
  -> EPICA 2 (HU-05)
  -> EPICA 3 (HU-06, HU-07 -> HU-08 -> HU-09)
  -> EPICA 4 (HU-10 -> HU-11 -> HU-12 -> HU-13 -> HU-14)
  -> EPICA 5 (HU-15 -> HU-16 ... HU-21)
  -> EPICA 6 (backlog heredado, sin orden fijo salvo lo indicado)
```

Motivo: las gemas (Epica 4) se ordenan por la nota final, que depende de la nota
trimestral (Epica 1) y del ajuste de Claude (Epica 3). La UI (Epica 5) va al final para
dar estilo una sola vez a todos los componentes nuevos.

Tamano: S = < medio dia, M = 1-2 dias, L = 3+ dias.

---

## EPICA 0 — Preparacion

### [x] HU-00 — Arranque unico del servidor (S)

**Como** usuario, **quiero** que un segundo doble clic en `SeaLock.bat` abra la app
ya en marcha, **para** no tener dos servidores peleando por el puerto.

Estado: hecho. `main()` en `app/servidor.py` sondea el puerto antes de arrancar, y
`SeaLock.bat` solo hace `pause` si el proceso sale con error (asi el segundo doble clic
no deja el consola esperando). Falta el commit propio (criterio 3).

Criterios de aceptacion:
- [x] Dado un SeaLock en marcha, cuando lanzo otro, entonces imprime "SeaLock is already
      running", abre el navegador (salvo `SEALOCK_NO_OPEN=1`) y sale con codigo 0.
      *(check en `prueba_web.py`: segunda copia del servidor sobre el mismo puerto ->
      "SeaLock is already running" y codigo 0. La rama de abrir el navegador solo se
      ejerce a mano, porque el test corre con `SEALOCK_NO_OPEN=1`.)*
- [x] Dado que no hay ninguno en marcha, cuando lanzo SeaLock, entonces arranca normal.
      *(el propio arranque del servidor en `prueba_web.py` es el check).*
- [x] El cambio queda en un commit propio, separado de cualquier otra HU.
      *(commit `7453dbc`: solo `main()` en `app/servidor.py`, `SeaLock.bat` y su test;
      aislado restaurando el fichero y reaplicando solo ese trozo.)*

---

## EPICA 1 — Nota basada en los 3 ultimos trimestres fiscales

### [x] HU-01 — Obtener los ultimos trimestres fiscales (M)

**Como** inversor, **quiero** que SeaLock descargue los resultados de los ultimos
trimestres fiscales de cada empresa, **para** que la nota refleje como va el negocio
ahora y no solo el ultimo ano.

Alcance: funcion y tool MCP `quarterly(ticker)` en `mcp/mercado_server.py` (y por tanto
`/api/market/quarterly` automatico). Fuente: Yahoo `fundamentals-timeseries`.

Criterios de aceptacion:
- [x] Pide unos 7 trimestres para poder comparar cada uno de los 3 ultimos con el
      **mismo trimestre del ano anterior** (interanual, evita la estacionalidad).
      *Pide 900 dias; el base interanual se busca a +-30 dias de hace 365.*
- [x] Campos por trimestre: `period_end` (AAAA-MM-DD), `revenue`, `net_income`,
      `operating_income`, `diluted_eps`, `free_cash_flow`, `currency`; `null` si falta.
- [x] Devuelve `quarters` (los 3 mas recientes, del mas nuevo al mas viejo), cada uno con
      `revenue_yoy_pct`, `eps_yoy_pct` (o `net_income_yoy_pct` si falta el BPA) y
      `net_margin_pct`; el interanual es `null` si falta el trimestre base o si el valor
      base es <= 0 (no se calcula crecimiento sobre perdidas).
- [x] Devuelve `available` (0-3), `missing_fields` (campos sin dato en los 3) y `gaps`.
      *Un hueco es una ranura fiscal que Yahoo no reporto, delimitada por los trimestres
      reportados de cada lado (o por el fin de mes de la ranura si no hay inferior). No
      se usa ">100 dias entre devueltos": hoy el hueco de ITX.MC es la ranura mas antigua
      y sus tres ultimos datos son contiguos.*
- [x] Devuelve `note_es` legible, p. ej. "Solo 2 de 3 trimestres disponibles: hueco entre
      2025-10-31 y 2026-04-30" o "Sin datos trimestrales publicos para XXX".
- [x] Con `AAPL` devuelve `available == 3` y `gaps == []`.
- [x] Con `ITX.MC` devuelve el hueco de enero en `gaps` y `free_cash_flow` en `missing_fields`.
- [x] Error de red o de Yahoo: devuelve `{"error": ...}` sin romper nada que lo llame.
- [x] Se cachea junto al resto de inputs en `_raw_inputs` de `servidor.py` (6 h).

Nota: `quarterly_derive()` es pura (sin red) y se testea con datos inventados en
`prueba_humo.py`; los checks contra Yahoo van como AVISO.

Aviso de auditoria (2026-09-25): el criterio de cache se cumple en el camino de
puntuacion, pero la ficha no lo usaba. **Resuelto en HU-06**: `stock_detail` ahora tira
de `_raw_inputs` y solo se refresca a peticion, asi que la ficha ya no hace ~7 peticiones
a Yahoo cada 60 s.

Fuera de alcance: estados financieros completos (balance trimestral). Si hace falta,
HU aparte.

### [x] HU-02 — Pilar "Resultados recientes" en la nota (M)

**Como** inversor, **quiero** que la nota 1-10 incluya un pilar con los 3 ultimos
trimestres, **para** premiar empresas que mejoran y penalizar las que se deterioran.

Alcance: `mcp/scoring.py` (`_results()`, `BASE_WEIGHTS`, `score_stock()`),
`stock_detail`/`_score_ticker` en `app/servidor.py`, tool `stock_score`.

Metricas (cada una en `[-1, 1]` con `_lin`, como el resto de pilares):

| Clave | Metrica | Malo -> Bueno |
|---|---|---|
| `rev_yoy_avg` | Media del crecimiento interanual de ventas de los trimestres disponibles | -5% -> +15% |
| `eps_yoy_avg` | Media del crecimiento interanual del BPA (o beneficio neto) | -10% -> +20% |
| `margin_trend` | Margen neto del trimestre mas nuevo menos el del mas viejo (puntos) | -3 -> +3 |
| `consistency` | Trimestres con ventas creciendo / trimestres con dato | 0 -> 1 |
| `surprise` | Sorpresa media del BPA frente al consenso (`earningsHistory`), ultimos 3 | -5% -> +5% |

Pesos nuevos (suman 100; el pilar se llama `results`, nombre visible "Resultados recientes"):

| Perfil | Calidad | Valoracion | Solidez | Crecimiento | Momentum | **Resultados** | Dividendo |
|---|---|---|---|---|---|---|---|
| moderate | 25 | 25 | 15 | 8 (antes 15) | 7 (antes 15) | **15** | 5 |
| conservative | 25 | 20 | 25 | 5 (antes 10) | 5 | **10** | 10 (antes 15) |
| aggressive | 20 | 20 | 10 | 15 (antes 25) | 20 (antes 25) | **15** | 0 |

Nota: en conservador el momentum ya era 5, asi que el pilar sale de crecimiento y
dividendo y pesa 10. Es la unica desviacion de D3.

Criterios de aceptacion:
- [x] `BASE_WEIGHTS` coincide con la tabla y cada perfil suma 100.
- [x] Los ajustes por `preference` y `horizon` siguen funcionando y renormalizando a 100.
- [x] Con `available == 0` el pilar se excluye, su peso se redistribuye y `coverage_pct` baja.
- [x] Con `available` 1 o 2 el pilar se calcula con lo que hay y lo dice en su `details`.
- [x] Cada metrica aparece en `pillars.results.details` con valor, subnota y unidad.
- [x] `score_stock()` sigue siendo pura (sin red, sin I/O): recibe `quarterly` como argumento.
- [x] Tests puros en `prueba_humo.py`: empresa creciendo (pilar > 0), empresa cayendo
      (pilar < 0), 2 de 3 trimestres, 0 trimestres (pilar excluido), base negativa
      (interanual `null`, sin division rara).
- [x] § Criterios de la nota de `CLAUDE.md`/`AGENTS.md` actualizado con el pilar y la tabla.
- [x] Entradas nuevas en `glosario.js`: "Resultados recientes" y cada metrica, con `rango` e `ideal`.

Nota: la sorpresa del BPA (`surprise`) llega en `quarterly().earnings_surprise_pct`, que
`quarterly()` pide a `earnings_calendar` (quoteSummary). Es una segunda peticion a Yahoo
por ticker, cacheada 6 h con el resto; si falla, la metrica se salta sola.

Correcciones de la auditoria (2026-09-25):
- Los `details` del pilar incluyen una linea `quarters_available` (con `points` 0, para no
  mover la subnota) que dice que se calcula con menos de 3 trimestres. Antes solo lo
  decia el `notice` global, y el criterio pedia que lo dijera el pilar.
- El test de `BASE_WEIGHTS` ahora comprueba los valores de la tabla, no solo que sumen
  100, y se anade que `preference`/`horizon` muevan los pesos y no solo los renormalicen.

### [x] HU-03 — Aviso de trimestres incompletos y senal de alerta (S)

**Como** inversor, **quiero** saber cuando la nota se apoya en menos de 3 trimestres y
que un deterioro claro limite la nota, **para** no fiarme de una nota con datos cojos.

Criterios de aceptacion:
- [x] `score_stock()` devuelve `quarters_available` y `quarters_note_es`
      (copiado de HU-01) en el resultado.
- [x] Si `quarters_available < 3`, el `notice` de la nota lo menciona.
- [x] Nueva `red_flag`: con 3 trimestres, ventas cayendo interanualmente en los 3 **y**
      `margin_trend < 0` -> la nota queda limitada a 3, igual que las otras tres alertas.
- [x] La alerta **no** salta con menos de 3 trimestres (falta evidencia).
- [x] Test puro para la alerta (salta con 3 malos, no salta con 2).
- [x] § Criterios de la nota lista cuatro senales de alerta en vez de tres.

Nota: la alerta exige los 3 trimestres **con datos de ventas** (si a uno le falta
`revenue_yoy_pct` no hay evidencia suficiente). Reutiliza `_results()` para el
`margin_trend`, asi que la definicion de "margen a la baja" vive en un solo sitio.

Correccion de la auditoria (2026-09-25): la pestana Ayuda de `index.html` seguia
diciendo "seis pilares" y listando tres alertas, con lo que incumpedia la Definicion de
Hecho. Corregida: siete pilares, cuatro alertas, la nota de que el ajuste de Claude
tampoco es asesoramiento y la fuente de datos de trimestres.

### [x] HU-04 — Ver los trimestres en la ficha (S)

**Como** inversor, **quiero** ver en la ficha de la empresa sus 3 ultimos trimestres,
**para** entender de donde sale el pilar sin abrir otra web.

Criterios de aceptacion:
- [x] Caja "Resultados trimestrales" en la ficha: tabla con fecha de cierre, ventas,
      beneficio, BPA, margen y crecimiento interanual; celdas sin dato muestran "—".
- [x] Mini grafico de barras SVG inline (ventas por trimestre), sin librerias.
- [x] Si hay aviso (`note_es`), se muestra encima de la tabla en ambar.
- [x] Importes en la moneda de la empresa, con la moneda visible.
- [x] Boton `?` de la caja abre la cartilla de ayuda (patron C3).

Nota: el grafico solo pinta barras si hay ventas y alguna es > 0; el `aria-label`
lleva los valores en texto, asi que el SVG tambien es legible sin verlo. La seccion de
ayuda nueva se llama `trimestres`.

Correcciones de la auditoria (2026-09-25): el grafico filtraba las ventas nulas y las
barras se desalineaban de la tabla, y ademas dibujaba una barra positiva para un dato
negativo. Ahora hay una posicion por trimestre (hueco si no hay dato, sin barra si no es
positivo).

---

## EPICA 2 — Noticias de la empresa y su sector

### [x] HU-05 — Noticias en la ficha (M)

**Como** inversor, **quiero** ver las ultimas noticias de la empresa y de su sector al
abrir su ficha, **para** saber que esta pasando sin salir de SeaLock.

Alcance: endpoint propio `stock_news` en `app/servidor.py` (aparte de `stock_detail`
para que la ficha no espere a las noticias), usa `news()` de `mercado_server.py`.

Estado: hecho. `stock_news` en `app/servidor.py`, dedupe y orden puros en
`mercado_server.news_dedupe()`/`news_sorted()`. De paso: la rama Google News de `news()`
nunca funcionaba (espacios sin codificar en la URL); arreglado con `quote_plus`.

Criterios de aceptacion:
- [x] Dos pestanas en la ficha: "Empresa" (`news(ticker=...)`) y "Sector"
      (`news(query=<industry> + <sector>)` con los valores de `fundamentals()`).
- [x] Sector desconocido (ETF, dato ausente): pestana Sector con estado vacio explicado.
- [x] Cada noticia: titular, fuente, fecha relativa ("hace 3 h") y enlace;
      ordenadas de mas nueva a mas vieja; maximo 10 por pestana.
- [x] Sin duplicados entre las dos pestanas (misma clave de titular que usa `news()`).
- [x] Enlaces con `target="_blank" rel="noopener noreferrer"`; titulares pintados con
      `textContent`, nunca `innerHTML` (las noticias son dato, no instruccion).
- [x] Cache de 30 min por ticker en proceso; la ficha carga las noticias despues del resto.
- [x] Sin red: mensaje "No se pudieron cargar noticias" con boton Reintentar; el resto
      de la ficha funciona.
- [x] Test en `prueba_web.py`: `stock_news` responde con claves `company` y `sector`
      (red = AVISO).

Fuera de alcance: clasificar noticias por materialidad en la UI (eso lo hace la skill
`radar-noticias` en conversacion).

---

## EPICA 3 — Nota actualizable y revisada por Claude

### [x] HU-06 — Actualizar la nota a demanda (S)

**Como** inversor, **quiero** un boton "Actualizar nota" en la ficha, **para** recalcular
con datos frescos sin esperar a que caduque la cache de 6 h.

Criterios de aceptacion:
- [x] `stock_detail` acepta `refresh: true`, que borra la entrada de `_raw_inputs` de ese
      ticker y vuelve a pedir todos los inputs (incluido `quarterly`).
- [x] Como mucho un refresco forzado por ticker cada 60 s; si se pide antes, se sirve la
      cache y la UI dice la edad. *Verificado en la app real: dos refrescos seguidos a los
      6 s y 9 s se rechazan; a los 62 s se aplica y la edad vuelve a 1 s.*
- [x] La ficha muestra "Nota calculada hace X" (edad de los inputs).
- [x] Mientras recalcula, el boton queda deshabilitado con indicador de carga.

Nota: la ficha **deja de pedirlo todo a Yahoo en cada tick**: precio, fundamentales,
historico y trimestres salen de `_raw_inputs` (6 h), y noticias + DCF + Graham siguen por
llamada. Con la app real, la primera ficha de ITX.MC tardo 6,9 s y la segunda 1,5 s. Esto
tambien cierra el aviso de auditoria de HU-01.

`FORCED_REFRESH_COOLDOWN` tiene mando `SEALOCK_REFRESH_COOLDOWN` solo para que
`prueba_web.py` pueda ejercitar el enfriamiento; en la app vale 60. El boton guarda
ademas los supuestos del DCF, asi que "Recalcular" DCF y "Actualizar nota" no se pisan.

Bug que cazaron los tests: la primera version hacia `if refresh and enfriamiento` y luego
`if TTL`, con lo que un refresco permitido caia igualmente en la cache de 6 h. Ahora
`refresh` se salta el TTL salvo que este enfriando.

### [x] HU-07 — Revision de Claude guardada en local (M)

**Como** inversor, **quiero** que Claude, al analizar una empresa en conversacion, pueda
dejar un ajuste de la nota con su motivo, **para** incorporar lo que las reglas fijas no
ven (noticias, contexto sectorial, calidad de los resultados).

Alcance: tabla `score_reviews` y tools en `mcp/finanzas_server.py` (sin red).

Esquema `score_reviews`: `id`, `ticker`, `created_at` (AAAA-MM-DD), `adjustment` (REAL),
`rationale` (TEXT), `sources` (JSON, lista de URLs), `mechanical_score` (nota al revisar),
`last_quarter_seen` (AAAA-MM-DD), `expires_at` (AAAA-MM-DD), `author` (por defecto `claude`).

Criterios de aceptacion:
- [x] Tool `review_set(ticker, adjustment, rationale, sources, mechanical_score, last_quarter_seen, expires_days=30)`:
      rechaza `adjustment` fuera de `[-2, 2]`, `rationale` vacio o > 2000 caracteres,
      `sources` que no sean URLs http(s).
- [x] Tool `review_get(ticker)`: devuelve la revision vigente mas reciente o `null`.
- [x] Tool `reviews_list()`: todas las revisiones, con `status` `active`/`expired`/`stale`.
- [x] Tool `review_delete(id)`: muestra el registro afectado antes de borrar (§ Limites).
      *Sin `confirm` devuelve el registro y no borra nada; con `confirm: true` lo borra.*
- [x] Una revision caduca (`expired`) al pasar `expires_at`, y queda obsoleta (`stale`)
      si `quarterly()` ya tiene un trimestre mas nuevo que `last_quarter_seen`.
      *`finance` no tiene red, asi que el trimestre mas nuevo entra por
      `latest_quarter_end` en `review_get`/`reviews_list`; `review_status()` (pura)
      decide el estado.*
- [x] Una revision nueva del mismo ticker sustituye a la anterior como vigente (la
      antigua queda en historico).
- [x] La migracion de la tabla es automatica en `connect()`, como el resto del esquema.
- [x] La skill `analizar-accion` (en `.claude/skills` y `.agents/skills`) termina
      proponiendo el ajuste con su motivo y fuentes, lo muestra y lo guarda con `review_set`.
      El motivo debe citar hechos concretos que la nota mecanica no recoge.
- [x] Tests en `prueba_humo.py`: limites de `adjustment`, vigente/caducada/obsoleta,
      sustitucion por revision nueva.

Nota: `review_status()` es pura y admite cualquier fecha. Por tools, `expired` sale
comparando `expires_at` con hoy; no hay forma de crear una revision ya caducada con
`review_set` (nace siempre vigente), asi que ese estado se comprueba sobre `review_status()`
con fechas fijas.

Correccion de la auditoria (2026-09-25): el estado `expired` no tenia ningun test real
(solo un dict literal en la parte de scoring) y `mechanical_score` se guardaba sin que
nadie lo leyera. Anadidos tres checks de `review_status()` (caducada, obsoleta, vigente,
y que la caducidad gana). `mechanical_score` queda como registro: la UI no lo muestra
porque es la mecanica del momento de la revision y puede no ser la de hoy.

### [x] HU-08 — Nota final = nota mecanica + ajuste (S)

**Como** inversor, **quiero** ver a la vez la nota mecanica, el ajuste de Claude y la
nota final, **para** saber siempre que parte son reglas fijas y que parte es criterio.

Criterios de aceptacion:
- [x] Funcion pura `scoring.apply_review(score_result, review)` ->
      `final_score = clamp(round(score + adjustment, 1), 1, 10)`; si hay `red_flags`,
      el tope de 3 se aplica **despues** del ajuste (el ajuste no salta una alerta).
- [x] Revision `expired` o `stale`: no se aplica; se muestra tachada con el motivo.
- [x] `label` y `sentiment` se calculan sobre `final_score`.
- [x] `stock_detail`, `portfolio_scores`, `stock_scores` y `recommendation_positions`
      devuelven `score` (mecanica), `review` y `final_score`.
- [x] `position_advice()`, `select_ideas()` y el futuro `select_gems()` usan `final_score`.
- [x] Tests puros: ajuste positivo, negativo, limites 1 y 10, tope por alerta.

Nota: `_label_for()` salio de `score_stock()` y la reusan `apply_review()` y el propio
motor, para que la etiqueta de una posicion con revision no se calcule con otro criterio.
`score_stock()` devuelve ademas `is_holding` para eso. `servidor._with_review()` es el
unico sitio que junta mecanica + revision, y se usa en los cuatro endpoints.

Correcciones de la auditoria (2026-09-25):
- `_label_for` estaba definido dos veces (la segunda, sin docstring, sombreaba a la
  primera). Borrada la copia muerta.
- `stale` era codigo muerto: nadie pasaba `latest_quarter_end`. Ahora `stock_detail` lo
  deduce del trimestre mas nuevo que acaba de traer, asi que la revision obsoleta se ve
  tachada de verdad.
- El test del tope superior usaba `6 + 2 = 8`, que nunca ejercitaba `min(final, 10)`.

### [x] HU-09 — Mostrar la revision en la interfaz (S)

**Como** inversor, **quiero** ver la revision de Claude en la ficha y en las listas,
**para** saber que empresas tienen criterio humano/IA encima de las reglas.

Criterios de aceptacion:
- [x] Ficha: "Nota mecanica 6 · Revision Claude +1,0 -> 7,0", con motivo, fuentes
      enlazadas, fecha y estado (vigente/caducada/obsoleta). *La mecanica es un entero
      1-10 por diseno: se muestra sin decimales. El `mechanical_score` que guarda la
      revision es la mecanica del momento en que Claude reviso, que puede no coincidir
      con la de hoy; no se muestra para no confundir.*
- [x] Listas (cartera, recomendaciones): la nota final lleva una marca pequena si
      hay revision vigente, con tooltip del ajuste. *Hidden Gemas (HU-14) todavia no
      existe; su lista usara el mismo `marcaRevision()`.*
- [x] Sin revision: la ficha dice "Sin revision de Claude. Pidele 'analiza <ticker>' en
      Claude Code para anadirla."
- [x] Entradas de glosario para "Nota mecanica", "Ajuste de Claude" y "Nota final".
- [x] § Limites de `CLAUDE.md`/`AGENTS.md` aclara que el ajuste tampoco es asesoramiento.

Nota: `stock_detail` acepta `latest_quarter_end`, pero si no se le pasa deduce el
trimestre mas nuevo de los datos que acaba de traer: sin eso, `stale` era codigo muerto
en la app (solo lo disparaba un test que lo pasaba a mano). El popover de glosario del
visor "una a una" de Recomendaciones tampoco estaba conectado; arreglado con el mismo
`attachGlossary` que usa la ficha.

---

## EPICA 4 — Hidden Gems mundiales

### [x] HU-10 — Catalogo local de simbolos mundial (L) — absorbe E1a-E1f

**Como** inversor, **quiero** un catalogo local de empresas cotizadas de todo el mundo,
**para** buscar valores y alimentar las gemas sin depender de la busqueda de Yahoo.

Fuente: [FinanceDatabase](https://github.com/JerBouma/FinanceDatabase) (MIT). Detalle
tecnico original en `docs/investigacion-repos.md` § Fase E1.

Estado: hecho. `recursos/simbolos/equities.csv` (44.627 valores, 5,5 MB, un solo CSV en
vez de shards) y `mcp/symbols.py`. Cambios sobre lo previsto: FRA fuera (duplicados de
extranjeros, ya en GER), India en NSE, `_tokens()` se muda a `symbols.tokens` para no
crear un import circular, y el sondeo de Yahoo se queda como ultimo recurso para nombres
que el catalogo no conoce. El "mercado de origen" sale del propio catalogo (pais
mayoritario de cada bolsa), sin tabla a mano.

Criterios de aceptacion:
- [x] Vendorizado en `recursos/simbolos/` (no en `datos/`) con `LICENSE` y `FUENTE.md`
      (repo, commit, fecha).
- [x] Mercados incluidos (codigos a verificar al vendorizar): EE. UU. (NYQ, NMS, NGM,
      NCM, ASE), Canada (TOR), Reino Unido (LSE), Europa continental (MCE, PAR, AMS,
      MIL, GER, FRA, EBS, LIS, BRU, STO, CPH, HEL, OSL, VIE, ISE), Japon (JPX), Hong Kong
      (HKG), Corea (KSC), Taiwan (TAI), India (NSI), Australia (ASX), Singapur (SES),
      Brasil (SAO), Mexico (MEX), Sudafrica (JNB); mas OTC (OID, PNK, OQB, OQX, OBB)
      solo para clasificar, nunca como candidatos.
- [x] Solo columnas necesarias (`symbol`, `name`, `country`, `sector`, `industry`,
      `exchange`, `market_cap`, `isin`, `mic`); tamano total en repo **< 25 MB**.
- [x] `mcp/symbols.py`: carga perezosa con `csv` de stdlib, indices por simbolo y por
      tokens del nombre (reutiliza `_tokens()`).
- [x] `ticker_search` consulta el catalogo primero y Yahoo solo si no hay coincidencia;
      desaparecen los 3 reintentos.
- [x] `OTC_CODES` sustituido por la clasificacion del catalogo.
- [x] `ticker_search` expone `isin` y `mic` (desambigua `SAN` NYSE de `SAN.MC`).
- [x] Test **critico, no AVISO**: `ITX.MC`, `SAN.MC`, `IBE.MC`, `7203.T` y `0700.HK`
      se resuelven sin red.

### [x] HU-11 — Universo de candidatos a gema (M)

**Como** inversor, **quiero** que SeaLock sepa que empresas del mundo son candidatas a
gema, **para** no puntuar decenas de miles de valores ni solo los de siempre.

Estado: hecho, con simplificaciones deliberadas frente al enunciado original:

1. **Sin screeners predefinidos de Yahoo** (`undervalued_growth_stocks` etc.): son
   solo EE. UU. y anaden red extra sin aportar al resto del mundo. Candidatos =
   filas del catalogo (`symbols.rows()`) con `is_home()` (cotizacion de origen, no
   ADR/linea extranjera), no OTC, `market_cap` "Small Cap" o "Mid Cap", fuera de los
   111 tickers de `recursos/universo.json`. El catalogo es solo acciones, asi que
   ETF/fondos ya quedan fuera sin filtro aparte.
2. **Sondeo de cobertura sin tabla ni job de 30 dias**: se deriva de `score_cache`
   (`app/servidor.py:_excluded_markets()`). Por bolsa, con 5 o mas valores puntuados,
   si menos de la mitad llega a `coverage_pct >= 50` la bolsa se excluye de futuros
   escaneos. Se recalcula solo con cada escaneo, sin fecha guardada aparte.
3. `region` sale de un mapa fijo `exchange -> region` (`EXCHANGE_REGION` en
   `app/servidor.py`) sobre las bolsas ya vendorizadas en HU-10.
4. Los tickers en cartera se marcan `held: true` en `gems_universe` y se excluyen del
   escaneo real (`_select_scan_candidates`), tal como pedia el criterio original.

Criterios de aceptacion:
- [x] Candidatos = empresas del catalogo con `market_cap` "Small Cap" o "Mid Cap"
      (sin los screeners de Yahoo, ver nota 1).
- [x] Excluidos: OTC, ETF/fondos (el catalogo ya es solo acciones), los 111 blue chips
      de `recursos/universo.json` y los tickers en cartera (se marcan, no se sugieren).
- [x] Sondeo de cobertura (ver nota 2: derivado de `score_cache`, no de una tabla aparte
      ni de un job de 30 dias): mercados sin cobertura suficiente se listan aparte.
- [x] Cada candidato lleva `region` (Norteamerica, Europa, Asia-Pacifico, Latinoamerica, Africa).
- [x] Endpoint `gems_universe`: devuelve candidatos y mercados excluidos, sin red.

### [x] HU-12 — Cache persistente de notas y escaneo en segundo plano (M)

**Como** inversor, **quiero** que el escaneo de gemas trabaje en segundo plano y
recuerde lo ya puntuado, **para** abrir la pestana y ver resultados al instante.

Alcance: tabla `score_cache` en `datos/finanzas.db` (datos de mercado, no del usuario),
hilo de escaneo en `app/servidor.py`, tool de solo lectura `gems_list` en `finanzas_server.py`
para que Claude pueda comentar las gemas en conversacion.

Estado: hecho. `payload` guarda, por pilar, subnota y detalles, mas **las dos variantes
del pilar Dividendo** (`scoring.dividend_variants()`), para poder recalcular la nota al
cambiar de perfil sin red (`scoring.recompute_from_payload()`, que comparte con
`score_stock()` el mismo paso de ponderacion, `_score_from_subscores()`: una sola
implementacion). El escaneo corre en un hilo daemon, con `threading.Lock` para permitir
solo uno a la vez, y `gems_scan_start` acepta un `limit` opcional (pensado para pruebas,
no para el escaneo real de 300).

Criterios de aceptacion:
- [x] `score_cache`: `ticker` (PK), `scored_at`, `score`, `final_score`, `coverage_pct`,
      `red_flags` (JSON), `market_cap_eur`, `num_analysts`, `sector`, `country`, `region`,
      `quarters_available`, `payload` (JSON con pilares, variantes de Dividendo y pros/contras).
- [x] Cada escaneo puntua como mucho 300 candidatos, repartidos entre regiones y
      priorizando los nunca puntuados y luego los mas antiguos (rotacion: con el tiempo
      se cubre todo el universo).
- [x] Una entrada de cache vale 7 dias para no volver a pedirla; las gemas se eligen
      entre entradas de menos de 14 dias.
- [x] Maximo 4 hilos; pausa entre tandas; ante errores seguidos, espera creciente
      (5/15/45 s) y, si persiste (3 tandas fallidas seguidas), el escaneo se detiene con
      estado `paused_rate_limited`.
- [x] Endpoints `gems_scan_start`, `gems_scan_status` (progreso `done/total`, errores,
      inicio, estado) y `gems_scan_cancel`. Solo un escaneo a la vez.
- [x] El escaneo no bloquea la UI ni el resto de la API; un fallo en un ticker nunca
      tumba el escaneo.
- [x] Un cambio de perfil inversor recalcula `score`/`final_score` desde `payload` sin red.
- [x] Tests: rotacion (prioriza nunca puntuados), caducidad 7/14 dias, recalculo por
      cambio de perfil (con datos inventados, sin red), sondeo de cobertura.

### [x] HU-13 — Seleccion de gemas (S)

**Como** inversor, **quiero** una regla clara de que es una "gema", **para** entender por
que aparece cada empresa.

Estado: hecho tal como estaba escrito, sin desviaciones.

Criterios de aceptacion:
- [x] Funcion pura `scoring.select_gems(candidates, held_tickers, held_sectors)`; reutiliza
      la logica de `select_ideas()` en vez de duplicarla.
- [x] Una gema cumple todo:
      `final_score >= 7`; `coverage_pct >= 50`; sin `red_flags`;
      `quarters_available >= 2`; `market_cap_eur` entre 300 M y 10.000 M (sin dato -> fuera);
      subnota del pilar Valoracion > 0 (no esta cara).
- [x] Orden: `final_score` desc, luego subnota de Valoracion desc, luego menos analistas
      (`num_analysts` bajo = menos seguida = mas "oculta"; sin dato va al final).
- [x] Marca `diversifies: true` si el sector no esta en la cartera.
- [x] Devuelve para cada gema su motivo principal: el pilar con mejor subnota ponderada,
      en espanol.
- [x] Umbrales como constantes con nombre en `scoring.py` (`GEM_MIN_SCORE`,
      `GEM_MIN_COVERAGE_PCT`, `GEM_MIN_QUARTERS_AVAILABLE`, `GEM_MIN_MARKET_CAP_EUR`,
      `GEM_MAX_MARKET_CAP_EUR`), no numeros sueltos.
- [x] Tests puros: cada criterio de exclusion por separado y el orden.

### [x] HU-14 — Pestana "Hidden Gems" (M)

**Como** inversor, **quiero** una pestana con las empresas mas atractivas del momento en
todo el mundo, **para** descubrir ideas fuera de los indices grandes.

Estado: hecho. `diversifies` se calcula en `finanzas_server.py:gems_list()`, sin red: el
sector de cada posicion sale del catalogo local (HU-10), y el escaner guarda tambien el
sector del catalogo (GICS), asi que los nombres coinciden. Tambien se omitieron las banderas emoji de pais (solo texto plano): no
aportaban frente al coste de mantener una tabla pais -> emoji.

Criterios de aceptacion:
- [x] Pestana "Hidden Gems" entre Recomendaciones y Cartera.
- [x] Al abrirla muestra al instante las gemas de la cache, con "Ultimo escaneo: fecha" y
      "X de Y candidatos analizados".
- [x] Boton "Escanear ahora" con barra de progreso y boton cancelar; la lista se repinta
      al terminar el escaneo (sondeo cada 2 s mientras esta en marcha).
- [x] Filtros: region, sector, nota minima (7-10); se combinan y persisten en `localStorage`.
- [x] Tarjeta por gema: nombre, ticker, pais (texto, sin bandera — ver nota), sector, nota
      final (con marca de revision si la hay), capitalizacion en EUR, motivo principal,
      etiqueta "Diversifica".
- [x] Acciones por tarjeta: "Ver ficha" (con boton de vuelta a Gems, patron de
      Recomendaciones) y "Anadir a watchlist" (deshabilitado si ya esta).
- [x] Navegacion una a una con `location.hash` (`#gem/<indice>`), el boton Atras funciona.
- [x] Mercados excluidos por falta de datos listados en un desplegable informativo.
- [x] Estado vacio (sin escaneos): explica que hacer y enlaza a Ayuda.
- [x] Aviso visible: "Seleccion mecanica sobre datos publicos, no es asesoramiento."
- [x] Ayuda: seccion nueva "Hidden Gems" con la regla de HU-13 en lenguaje llano.
- [x] Test en `prueba_web.py`: endpoints de gemas responden y exigen el token.
- [ ] Test en `prueba_web.py`: endpoints de gemas responden y exigen el token.

---

## EPICA 5 — Interfaz y experiencia mas atractivas

Metodo: antes de tocar nada, auditoria con la skill `better-interface` (accesibilidad,
layout, tipografia, color, copy, pulido) sobre las 6 pestanas; sus hallazgos se anaden
como criterios a estas HU. Verificacion visual con capturas antes/despues.

### [ ] HU-15 — Sistema de diseno y modo oscuro (M)

**Como** usuario, **quiero** una interfaz coherente y moderna, con modo oscuro,
**para** que dé gusto usarla a cualquier hora.

Criterios de aceptacion:
- [ ] `estilo.css` usa variables CSS para color, espaciado, radios, sombras y tipografia;
      cero colores sueltos fuera de las variables.
- [ ] Escala tipografica definida (5-6 tamanos) y cifras tabulares en importes y notas.
- [ ] Modo oscuro: sigue `prefers-color-scheme` y tiene interruptor en la cabecera que
      persiste en `localStorage`.
- [ ] Contraste AA (>= 4,5:1 texto, >= 3:1 elementos graficos) en ambos temas, incluidos
      `.veredicto-verde/ambar/rojo` (se mantienen esos nombres de clase).
- [ ] `prefers-reduced-motion` desactiva transiciones y animaciones.

### [ ] HU-16 — Visualizacion de la nota y del precio (M)

**Como** inversor, **quiero** ver la nota y sus pilares de un vistazo, **para** entender
la calificacion sin leer tablas.

Criterios de aceptacion:
- [ ] Nota como anillo SVG (0-10) coloreado por `sentiment`, con el numero en el centro.
- [ ] Barras horizontales por pilar (subnota y peso), ordenadas por peso.
- [ ] Sparkline de precio a 1 ano en la ficha y en las tarjetas de gemas.
- [ ] Todo SVG inline, sin librerias; cada grafico tiene texto alternativo con los valores.

### [x] HU-17 — Movil (S) — antes D3a
- [x] Pestanas con scroll horizontal; ningun formulario ni tabla desborda a 360 px de ancho.
- [x] Zonas tactiles >= 44 px.

Estado: hecho. `.pestanas` con `overflow-x: auto` bajo 600px; todas las tablas ya
vivian en `.tabla-scroll` salvo la de "Fuentes de datos" en Ayuda, envuelta ahora.
`@media (pointer: coarse)` sube `min-height` a 44px en botones, `.pestana` y `.mini`.

### [x] HU-18 — Foco y estados deshabilitados (S) — antes D3b
- [x] `:focus-visible` visible en botones, enlaces, pestanas y tarjetas.
- [x] Estados `disabled` visibles y no clicables mientras una accion esta en curso.
- [x] Todo accionable por teclado (Tab, Enter, Espacio, ESC en popups).

Estado: hecho. La regla generica `button:focus-visible, [tabindex]:focus-visible` de
`estilo.css` ya cubria pestanas, `.mini`, tarjetas/filas de Recomendaciones (con
`tabindex=0` y su propio manejador de teclado); se amplio a `a:focus-visible`. Se anadio
`button:disabled { opacity:.5; cursor:not-allowed }`. El popover de glosario y la
tarjeta de ayuda ya cerraban con ESC (comprobado, sin cambios).

### [ ] HU-19 — Esqueletos de carga (S) — antes D3c
- [ ] Toda caja que hoy dice "Cargando..." muestra un esqueleto con la forma del contenido.

### [x] HU-20 — Reintentar en errores (S) — antes D3d
- [x] Todo estado de error tiene boton "Reintentar" que repite solo esa carga.

Estado: hecho. `renderError(id, e, retry)` acepta una funcion opcional y pinta un
boton "Reintentar" dentro de la caja de error, siempre escapado con `esc()`. Todas
las llamadas (cartera, watchlist, busqueda, ficha, recomendaciones) pasan la funcion
que repite solo esa carga.

### [x] HU-21 — Modal de confirmacion propio (S) — antes D3e
- [x] `confirm()` del navegador sustituido por un modal con el registro afectado,
      foco atrapado, ESC cancela, foco devuelto al boton de origen.

Estado: hecho. `confirmDialog(mensaje)` en `app.js`, sobre `<dialog>` nativo
(`showModal()`): foco atrapado y ESC (evento `cancel`) gratis del navegador; al
cerrar, devuelve el foco a quien abrio el dialogo. Unico borrado destructivo en la UI
era "Quitar" de seguimiento (`watchlist_remove`); ahora pide confirmacion mostrando
ticker y nombre. Estilos en `estilo.css` con las variables `--fondo-caja`/`--borde`.

---

## EPICA 6 — Backlog heredado (sin cambios de alcance)

Detalle tecnico de E2-E6 en `docs/investigacion-repos.md`.

### [ ] HU-22 — Medir el retraso real de cotizacion (S) — antes A6
**Como** inversor, **quiero** saber cuanto retraso real tienen los precios en sesion,
**para** fiarme de la etiqueta de frescura.
- [ ] Medido en sesion REGULAR (lunes) para un valor EE. UU. y uno de Madrid.
- [ ] `data_delay_min` coincide con lo medido o se corrige; resultado anotado en `CLAUDE.md`.

### [ ] HU-23 — Indicadores tecnicos (M) — antes E2
**Como** inversor, **quiero** ATR, Bollinger y MACD, **para** que el momentum sea mas fino.
- [ ] `mcp/indicators.py` vendorizado de talipp con licencia y `FUENTE.md`.
- [ ] RSI/SMA de `historical()` sustituidos **tras verificar equivalencia** (Wilder vs media simple).
- [ ] ATR/Bollinger/MACD en `historical()` y como metricas del pilar Momentum.
- [ ] Entradas de glosario (el test B4 falla sin ellas).

### [ ] HU-24 — Metricas de cartera (M) — antes E3
**Como** inversor, **quiero** Sharpe, Sortino, Calmar y comparacion con `^IBEX`, **para**
medir mi cartera con estandares del sector.
- [ ] Formulas portadas de quantstats a stdlib, con tests contra valores conocidos.
- [ ] Tasa libre de riesgo en `SEALOCK_RISK_FREE_RATE`, nunca constante escondida.

### [ ] HU-25 — Alertas (M) — antes E4, absorbe "Alertas precio"
**Como** inversor, **quiero** alertas sobre precio y senales, **para** no vigilar a mano.
- [ ] Tabla `alerts` y tools `alert_create`/`alerts_list`/`alert_delete`/`alert_toggle`.
- [ ] Evaluacion en el refresco de 60 s; aviso en la UI.
- [ ] Describe hechos ("RSI de ITX.MC bajo 30"), nunca recomienda (§ Limites).

### [ ] HU-26 — Convencion de punto en el tiempo (S) — antes E5
- [ ] Convencion documentada antes de cualquier backtest: se ve el cierre de T, se compra
      en T+1, se vende en T+2, retorno = `T+2/T+1 - 1`.
- [ ] `period_return_pct` y plusvalias auditadas con esa convencion.

### [ ] HU-27 — Ejecucion via broker (L) — antes E6, **REQUIERE DECISION PREVIA**
- [ ] Decision explicita del usuario (contradice § Limites y § Principios).
- [ ] Solo paper trading y como extra opcional; § Limites actualizado antes.

### [x] HU-28 — Exportar datos (S)
**Como** inversor, **quiero** exportar cartera, watchlist y revisiones a CSV/JSON, **para**
tener copia fuera de SeaLock.
- [x] Boton en Cartera; descarga local, sin red. *Dos botones, "Exportar JSON" y
      "Exportar CSV", junto a "Actualizar precios". El endpoint `app/export_all` devuelve
      `{trades, watchlist, reviews}` y el navegador arma el fichero (Blob + `<a download>`);
      el CSV lleva una columna `tabla` para distinguir las tres. Nada sale a la red.
- [x] Test en `prueba_web.py`: `export_all` responde con las 3 claves y exige el token.

### [ ] HU-29 — Grafico de evolucion de la cartera (M)
**Como** inversor, **quiero** ver la evolucion del valor total y la plusvalia, **para** ver
mi progreso.
- [ ] Linea temporal SVG con valor total en moneda base y plusvalia; respeta HU-26.

### [x] HU-31 — Broker por operacion y pendientes de subir (S)
**Como** inversor, **quiero** anotar en que broker he hecho cada operacion y ver lo que me
falta por copiar a la plataforma, **para** no buscar una operacion donde no esta.
- [x] `trades.broker` con lista fija `BROKERS` (`HapiBroker`, `ETF`, `Fondo mutuo`,
      `Otro`) y `<select>` en el formulario; `portfolio_view` devuelve `brokers` por
      posicion y la tabla de Cartera lo muestra.
- [x] `trades.uploaded_at` + casilla "Ya esta en el broker" al registrar, tools
      `trades_pending()` (agrupado por broker) y `trades_mark_uploaded()`.
- [x] Al abrir la app, `app/config` expone los pendientes y Cartera avisa con un boton
      "Marcar todo como subido".
- [x] Migracion en sitio: `_add_missing_columns()` anade las columnas a las bases
      existentes en cada `connect()` (no hay tabla de versiones; son dos columnas).
- [x] Tests en `prueba_humo.py` (8) y `prueba_web.py` (4).

Nota: pedida fuera del plan. **SeaLock no manda nada a Hapi ni a ninguna plataforma**
(§ Principios): `uploaded_at` es lo que tu marcas a mano, no un envio. Si algun dia se
quiere sincronizar de verdad, es HU-27 y requiere decision expresa.

### [x] HU-33 — Proyecciones de analistas (M)
**Como** inversor, **quiero** ver lo que espera el consenso de analistas para los proximos
periodos, **para** contrastar mi lectura con la del mercado.
- [x] Tool `earnings_estimates(ticker)` con los 4 periodos (trimestre actual, siguiente,
      ano actual, siguiente): BPA, numero de analistas, ventas y crecimiento esperado,
      mas el precio objetivo con su rango.
- [x] `parse_estimates()` es pura: el shape de Yahoo se testea sin red.
- [x] Cache 6 h en `_raw_inputs` como `estimates`; llega a la ficha y a la nota.
- [x] Caja "Proyecciones" en la ficha con la tabla, el balance de revisiones 30 d y el
      rango del objetivo, mas cartilla de ayuda y entrada de glosario.
- [x] De las proyecciones **solo puntua el balance de revisiones** (peso 0.5, sobre los dos
      trimestres mas cercanos). Los niveles del BPA esperado no entran: medirian el
      optimismo del analysta, no el negocio.
- [x] Tests: 10 puros (parseo, y que el nivel no mueva la nota) + 2 de red + 3 e2e.

Nota: el sesgo al alza del sell-side ya estaba documentado para el precio objetivo; aqui
se aplica el mismo criterio al resto del consenso. Con menos de 3 analistas la cobertura
es tan baja que la cifra se lee como orientativa: se muestra, pero el balance de
revisiones de un solo analista no dice mucho.

### [x] HU-34 — Copiar prompt del valor con perfil, cartera y URLs (S)
**Como** inversor, **quiero** copiarme los datos del valor, las URLs, mi perfil y mi cartera
en un texto, **para** pegarlo en cualquier IA y que me lo analice con mi contexto.
- [x] `llm.build_prompt()` (puro) arma el prompt: instruccion de "analiza, no me digas que
      compre", perfil inversor, posicion de este valor, el resto de la cartera, todos los
      numeros (`build_context`) y las URLs (cotizacion + titulares).
- [x] Endpoint `app/build_prompt` y boton "Copiar prompt para otra IA" en la ficha, que
      usa `navigator.clipboard` (nativo, sin librerias).
- [x] **La cartera si viaja entera en el prompt**, a diferencia de la caja de preguntar:
      aqui la pegas tu a mano y eliges el sitio, que es justo lo que se pidio. La caja de
      preguntar mantiene la posicion como opt-in.
- [x] El prompt lleva las **proyecciones** (consenso + balance de revisiones) y avisa de
      que son prevision.
- [x] Tests: 6 puros del prompt y 3 del contexto con proyecciones, + 3 e2e.

Nota: las proyecciones tambien entran en la caja de preguntar a Claude (faltaban en
HU-32, corregido aqui).

### [x] HU-30 — Comparador de acciones (M)
**Como** inversor, **quiero** comparar varias acciones lado a lado, **para** elegir entre ellas.
- [x] Tabla con fundamentales, pilares, trimestres (HU-01) y nota final (HU-08) de 2-4 tickers.

Estado: hecho. `stock_compare(tickers)` en `app/servidor.py`: acepta lista o texto
separado por comas, exige 2-4 tickers, puntua en paralelo con `_score_many` y anade
desde `_raw_inputs` el subconjunto de fundamentales (PER, EV/EBITDA, ROE, margen
neto, crecimiento de ventas, dividendo) y el ultimo trimestre (ventas y margen
interanual). Fallos por ticker vuelven como `{"ticker","error"}` sin tumbar el lote.
Caja "Comparar acciones" en Acciones (`index.html`/`app.js`, `renderComparar`), con
las mismas etiquetas de fila que la ficha para no necesitar entradas nuevas de
glosario. Test en `prueba_web.py`: 1 ticker rechaza, 2 tickers devuelven 2 filas.

### [x] HU-32 — Preguntar a Claude desde la ficha, con busqueda web (M)
**Como** inversor, **quiero** preguntar sobre un valor desde la ficha, con los numeros de
la app como contexto y buscando en internet, **para** contrastar sin salir de SeaLock.
- [x] `app/llm.py`: `build_context()` (puro, sin red ni clave) arma el contexto desde la
      respuesta de `stock_detail`; `ask()` llama a la API de mensajes de Anthropic.
- [x] Busqueda web con la herramienta del servidor `web_search_20250305` (tope 3 por
      pregunta). Sin credenciales de busquador.
- [x] La posicion es **opt-in** por pregunta (`include_position`): sin la casilla no
      viajan coste medio, valor ni P&L. Las noticias van marcadas como dato, no
      instruccion.
- [x] La clave solo se lee de `ANTHROPIC_API_KEY`; no se guarda ni llega al navegador
      (test en `prueba_web.py`). Modelo con mando `SEALOCK_CLAUDE_MODEL`.
- [x] La respuesta se pinta con `textContent`, nunca como HTML.
- [x] Tests: 10 de contexto puro, 5 de formato de peticion/parseo con stub, 5 e2e.
- [x] Aviso visible en la caja y cartilla de ayuda `preguntar`.

Nota: rompe "Datos no salen" a proposito, y por eso queda escrito en § Principios como la
unica excepcion. Con una *suscripcion* de Claude o ChatGPT **no** vale: hace falta una
clave de API, que se cobra aparte. Ojo con el dato fiscal: no se envia nada
automaticamente a Hapi ni a ningun broker (HU-27 sigue necesitando decision expresa).

---

## 4. Riesgos conocidos

| Riesgo | Mitigacion |
|---|---|
| Yahoo limita peticiones al escanear cientos de valores | Tope 300/escaneo, 4 hilos, pausas, backoff, estado `paused_rate_limited` (HU-12) |
| Datos trimestrales incompletos fuera de EE. UU. | Aviso explicito y pilar parcial o excluido (HU-01, HU-03); mercados sin cobertura fuera del escaneo (HU-11) |
| Yahoo cambia o cierra `fundamentals-timeseries` | `quarterly` devuelve error y la nota excluye el pilar; alternativa con clave gratuita Finnhub/FMP (§ Fuentes de datos) |
| Catalogo mundial demasiado grande | Solo columnas necesarias, sin micro/nano caps, tope de 25 MB (HU-10) |
| El ajuste de Claude se toma como asesoramiento | Nota mecanica siempre visible, ajuste acotado a +-2, motivo y fuentes obligatorios (HU-07 a HU-09) |
