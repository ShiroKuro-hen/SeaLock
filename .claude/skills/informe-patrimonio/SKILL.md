---
name: informe-patrimonio
description: Informe periodico que une finanzas personales y cartera de inversion en una foto unica del patrimonio, con valor de mercado real, plusvalias latentes, tasa de ahorro y concentracion de riesgo. Usar cuando el usuario pida un informe, un resumen del mes, como va su cartera, cuanto ha ganado o perdido, cual es su patrimonio total, o un cierre mensual o anual.
---

# Informe de patrimonio

Une las dos mitades de la aplicacion: lo que entra y sale (servidor `finanzas`) y lo
que valen las inversiones ahora mismo (servidor `mercado`).

## Construccion

**1. Situacion en efectivo** — `finance.accounts_list` da los saldos por cuenta.

**2. Cartera a valor de mercado.** `finance.portfolio_view` devuelve cantidades y coste
medio, pero **no** precio actual. Toma los tickers de esa respuesta y llama a
`market.quote` con todos a la vez. Luego calcula, por posicion:

```
market_value     = quantity × current_price
gain             = market_value − total_cost
gain_pct         = 100 × gain / total_cost
portfolio_weight = market_value / total_portfolio_value
```

**3. Divisas.** Si hay posiciones en moneda distinta a la base, convierte con
`market.exchange_rate` **antes** de sumar. Sumar euros y dolares sin convertir es el
error mas comun y el mas caro. Indica el tipo de cambio usado.

**4. Flujos del periodo** — `finance.period_summary` y `finance.monthly_evolution`
para ingresos, gastos, tasa de ahorro y tendencia.

**5. Presupuestos** — `finance.budget_status`, solo si hay presupuestos definidos.

## Formato

```
# Patrimonio — agosto 2026

**Total: 84.320 €**  (+2.140 € en el mes, +2,6 %)

| Bloque | Valor | Peso |
|---|---|---|
| Efectivo | 18.400 € | 22 % |
| Cartera | 65.920 € | 78 % |

## Cartera

| Ticker | Cant. | Coste medio | Precio | Valor | P&L | P&L % | Peso |
|---|---|---|---|---|---|---|---|
...
**Latente: +8.210 € (+14,2 %)** · **Realizado acumulado: +1.030 €**

## Flujo del mes
Ingresos 2.400 € · Gastos 1.680 € · Ahorro 720 € (tasa 30 %)
Tendencia: <subiendo/estable/bajando> frente a los 3 meses anteriores.

## Observaciones
- <concentracion, desviaciones, presupuestos excedidos, posiciones que rompen tesis>
```

## Que senalar en observaciones

Hechos, no consejos:

- **Concentracion**: una posicion por encima del 25 % de la cartera, o un sector por
  encima del 40 %. Enuncia el dato y su implicacion de riesgo.
- **Divergencias**: una posicion con caida grande frente al resto de la cartera merece
  un vistazo a `radar-noticias`.
- **Tesis rotas**: compara el estado actual con la `thesis` guardada en la watchlist o en
  la nota de la operacion. Si el motivo de compra ya no aplica, dilo.
- **Tasa de ahorro** desviada de su media reciente, con la categoria responsable.
- **Coherencia de datos**: si un precio no se pudo obtener, marca la posicion como
  "sin valorar" en vez de asumir el coste como valor. Nunca infles el total con
  supuestos silenciosos.

## Reglas

- **Plusvalia latente ≠ realizada.** Mantenlas separadas: solo la segunda tributa.
- **No calcules impuestos ni des consejo fiscal.** Puedes senalar que una venta
  generaria una ganancia realizada de X, nada mas.
- **Sin recomendaciones de compra o venta.** El informe describe; decide el usuario.
- Ofrece `finance.backup_create` al cerrar un informe mensual o anual.
