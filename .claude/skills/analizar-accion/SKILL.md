---
name: analizar-accion
description: Analisis completo de una accion o ETF combinando cotizacion, historico, fundamentales, noticias y valoracion intrinseca. Usar cuando el usuario pregunte por una empresa cotizada ("que tal esta Nvidia", "analiza Inditex", "esta cara Apple?", "merece la pena entrar en X"), pida comparar varias acciones, o quiera revisar su watchlist.
---

# Analisis de una accion

Produce una ficha util en una sola pasada, sin ir preguntando dato a dato.

## Secuencia

**1. Resolver el simbolo.** Si el usuario da un nombre y no un ticker, usa
`market.ticker_search` y **fijate en `available_markets`**, no solo en `suggested`.

Esto no es un detalle menor: la misma empresa cotiza en muchos sitios con precios y
monedas distintos, y elegir mal contamina toda la valoracion.

- La cotizacion principal suele ser la del **pais de origen**: Inditex `ITX.MC`,
  Iberdrola `IBE.MC`, LVMH `MC.PA`, Nvidia `NVDA`.
- Los resultados marcados con `warning` son **ADR de mercado OTC** (`IBDSF`,
  `LVMUY`, `LVMHF`): misma empresa, liquidez irrisoria y horquillas enormes. No los uses.
- Yahoo a veces **no lista la cotizacion principal**: buscar "Inditex" devuelve Varsovia,
  Milan y Hamburgo, pero no Madrid. La herramienta lo sondea aparte y lo marca con
  `found_via`.
- Santander es el caso ambiguo tipico: `SAN` (NYSE, en USD) y `SAN.MC` (Madrid, en EUR)
  son ambas reales. Con moneda base EUR, la de Madrid es la que evita ruido de divisa.
  Si hay duda, **di cual has elegido y por que**.

**2. Recoger datos en paralelo.** Lanza en el mismo turno:

- `market.quote` → precio, variacion, rango 52 semanas
- `market.fundamentals` → ratios, margenes, deuda, flujo de caja
- `market.historical` (rango `1y` y, si interesa la foto larga, `5y`)
- `market.news` → contexto reciente

**3. Valorar.** Ver la skill `valor-intrinseco`. Como minimo, un `market.dcf_valuation`
con supuestos justificados y su tabla de sensibilidad.

**4. Cruzar con la cartera.** Comprueba si ya se tiene la posicion
(`finance.portfolio_view`) o esta en seguimiento (`finance.watchlist_list`). Si ya hay
posicion, compara el precio actual con el coste medio y menciona la tesis original
guardada en la watchlist: es lo que permite ver si la tesis sigue viva o se rompio.

## Formato de salida

```
## NVDA — NVIDIA Corp · 178,20 USD (+1,4 %)

**En una frase:** <veredicto honesto en una linea>

### Situacion
Precio vs. rango 52s, tendencia (medias 50/200), volatilidad, drawdown maximo.

### Fundamentales
Tabla compacta: PER, PER fwd, EV/EBITDA, margen neto, ROE, crecimiento, deuda neta.
Cada cifra necesita contexto: "PER 45" no dice nada; "PER 45 frente a 28 de su
media de 5 anos" si.

### Valoracion
Valor intrinseco estimado, margen de seguridad y el rango de la sensibilidad.

### Noticias que mueven la aguja (ultimos dias)
3-5 titulares con fecha y por que importan. Descarta el ruido.

### Riesgos
2-4 riesgos concretos y especificos de esta empresa.
```

## Reglas de honestidad

- **Distingue dato de opinion.** Los ratios son datos; "esta barata" es una
  interpretacion sujeta a supuestos, y debe ir marcada como tal.
- **Si un dato no esta disponible, dilo.** No rellenes huecos con estimaciones propias
  presentadas como hechos. `fundamentals` puede devolver `error` si Yahoo bloquea la
  peticion: en ese caso indica que falta y sigue con lo que si tengas.
- **Nunca des una recomendacion personalizada de inversion.** Puedes exponer el analisis,
  los escenarios y los riesgos; la decision es del usuario. No eres asesor financiero
  y hay que decirlo cuando la pregunta sea "¿compro o no?".
- **Ni PER ni RSI deciden nada por si solos.** Un PER bajo suele senalar un problema, no
  una ganga. Busca la explicacion antes de celebrar el numero.
- Los precios pueden ir con ~15 min de retraso: irrelevante para invertir a anos,
  importante si el usuario habla de operar hoy. Avisa solo en ese segundo caso.

## Comparativas

Para varias empresas a la vez, `market.compare` da la tabla en una llamada. Compara
siempre dentro del mismo sector: un PER de 12 es caro en banca y barato en software.
