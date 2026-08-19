---
name: registro-finanzas
description: Registrar gastos e ingresos en lenguaje natural, revisar en que se va el dinero, controlar presupuestos e importar extractos del banco. Usar cuando el usuario diga cosas como "he gastado 40 en la compra", "apunta la nomina", "cuanto llevo gastado este mes", "como voy de presupuesto", "importa este CSV del banco", o pregunte por sus ingresos, gastos, ahorro o categorias de gasto.
---

# Registro de finanzas personales

Convierte frases sueltas en registros limpios en la base de datos local. La clave es
la **consistencia de categorias**: si hoy se apunta "comida" y manana "alimentacion",
los resumenes dejan de servir.

## Flujo de trabajo

### 1. Apuntar un movimiento

Cuando el usuario describa un gasto o ingreso, llama directamente a
`finance.transaction_create`. No preguntes lo que puedes deducir:

| Lo que dice el usuario | Como se registra |
|---|---|
| "gasté 42€ en el súper" | gasto, 42, alimentacion, fecha=hoy |
| "40 de gasolina ayer" | gasto, 40, transporte, fecha=ayer |
| "me ha entrado la nómina, 2100" | ingreso, 2100, nomina |
| "cena con Marta 65" | gasto, 65, ocio, descripcion="cena con Marta" |

Pregunta **solo** si falta el importe o si la categoria es genuinamente ambigua.

### 2. Mantener el catalogo de categorias

Antes de inventar una categoria nueva, mira las que ya existen:

```
finance.sql_query: SELECT category, COUNT(*) n FROM transactions GROUP BY category ORDER BY n DESC
```

Reutiliza la existente si encaja. Catalogo base recomendado:

`vivienda` · `alimentacion` · `transporte` · `suministros` · `salud` · `ocio` ·
`restauracion` · `compras` · `educacion` · `seguros` · `impuestos` · `suscripciones` ·
`nomina` · `inversion` · `otros-ingresos`

Usa `subcategory` para el detalle (`suministros` / `luz`), no una categoria nueva.

### 3. Traspasos entre cuentas propias

Mover dinero de la cuenta corriente al ahorro **no es un gasto ni un ingreso**. Si se
registra como dos movimientos normales, los resumenes se inflan por ambos lados y la
tasa de ahorro deja de significar nada.

Usa `finance.transfer_create`. Ajusta los dos saldos y queda excluido de
ingresos, gastos y presupuestos. Senales de que se trata de un traspaso: "he pasado",
"he movido", "transferí", "al ahorro", "a la cuenta de inversion".

Si el traspaso cruza monedas, la herramienta lo rechaza a proposito: registra dos
movimientos separados y deja escrito el tipo de cambio aplicado.

### 4. Gastos e ingresos recurrentes

Nomina, alquiler, hipoteca, seguros y suscripciones no deberian teclearse cada mes.
Definelos una vez con `finance.recurring_create` y despues genera los pendientes con
`finance.recurring_apply`.

`recurring_apply` simula por defecto: ensena lo que crearia y solo escribe al
llamarlo con `simulate=false`. Es idempotente, asi que puede ejecutarse cuantas veces
haga falta sin duplicar nada. El dia se ajusta solo a los meses cortos (dia 31 →
28 de febrero).

`recurring_list` da ademas el **compromiso mensual fijo**: cuanto sale cada mes
pase lo que pase. Es la cifra que de verdad limita la capacidad de ahorro.

### 5. Responder a "¿cómo voy?"

- Mes en curso o un periodo concreto → `finance.period_summary`
- Tendencia de varios meses → `finance.monthly_evolution`
- Control de limites → `finance.budget_status`

Al presentar resultados: **primero la conclusion, luego la tabla**. Ejemplo:

> Llevas 1.340 € de gasto este mes, un 12 % por encima de tu media. Casi todo viene de
> `restauracion` (310 €, frente a 180 € habituales).

No sermonees sobre los habitos de gasto salvo que te lo pidan. Senala hechos
—desviaciones, presupuestos excedidos, gastos duplicados— y calla.

### 6. Importar extractos del banco

`finance.csv_import` va en modo simulacion por defecto. **Siempre** revisa primero
la simulacion con el usuario (cuantas filas, que errores, muestra de 10) y solo despues
ejecuta con `simulate=false`. Si el CSV usa comas como separador, pasa `delimiter=","`.

Tras importar, propon categorizar los movimientos que quedaron en `uncategorized`
usando `transaction_update`.

## Reglas

- **Importes siempre positivos.** El signo lo marca `type` (income/expense).
- **Nunca borres sin confirmar.** `transaction_delete` es irreversible; pide confirmacion
  y muestra el registro afectado antes de llamarla.
- Ante fechas relativas ("el martes", "hace dos semanas"), calcula la fecha real y
  registrala explicitamente en formato AAAA-MM-DD.
- Sugiere `finance.backup_create` despues de una importacion grande o si han pasado
  muchas semanas desde la ultima.
