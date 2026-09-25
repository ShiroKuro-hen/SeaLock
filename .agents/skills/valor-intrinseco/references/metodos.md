# Metodos de valoracion: formulas y trampas

## 1. DCF en dos fases

**Fase 1 (crecimiento explicito, N anos):**

```
VP_fase1 = Σ [ FCF₀ · (1+g)ᵗ / (1+r)ᵗ ]   para t = 1..N
```

**Valor terminal (perpetuidad de Gordon), al final del ano N:**

```
VT = FCF_N · (1 + g_t) / (r − g_t)
VP_terminal = VT / (1+r)^N
```

**Valor del equity y por accion:**

```
Enterprise Value = VP_fase1 + VP_terminal
Equity Value     = Enterprise Value − Deuda neta
Valor por accion = Equity Value / Acciones en circulacion
```

Donde `g` = crecimiento fase 1, `g_t` = crecimiento perpetuo, `r` = tasa de descuento.
Requisito ineludible: **`g_t < r`**, o el denominador se vuelve negativo.

### Trampas del DCF

- **El valor terminal domina.** Suele ser el 60-80 % del total. Si el resultado depende
  casi por completo de lo que pasa a partir del ano 10, la precision es ilusoria.
- **Deuda neta con signo.** `deuda_total − caja`. Si es negativa (mas caja que deuda)
  *suma* al equity value. Comprueba el signo antes de fiarte del resultado.
- **FCF vs. beneficio.** Usa flujo de caja libre (operativo − capex), no el beneficio
  contable. Yahoo lo da en `cash_flow.free_cash_flow`.
- **Acciones diluidas.** Si la empresa reparte muchas stock options, las acciones
  crecen cada ano y el valor por accion cae. Usa el numero diluido cuando exista.
- **Recompras.** Reducen las acciones, elevando el valor por accion. No se recogen en
  el modelo basico.

### Coste de capital orientativo (WACC aproximado)

```
r ≈ tipo libre de riesgo + beta · prima de riesgo de mercado
```

Con tipo libre de riesgo ~3-4 % y prima ~5 %:

| Perfil | beta | r sugerida |
|---|---|---|
| Utility, consumo defensivo | 0,6-0,8 | 7-8 % |
| Gran industrial, gran consumo | 1,0 | 8-9 % |
| Tecnologica consolidada | 1,2-1,4 | 10-11 % |
| Small cap o alta deuda | > 1,5 | 12-15 % |

## 2. Numero de Graham

```
Valor = √(22,5 × BPA × Valor contable por accion)
```

El 22,5 sale de PER 15 × P/VC 1,5. Es una criba conservadora, pensada para
industriales de los anos 70. Requiere BPA y valor contable **positivos**. Inutil para
empresas asset-light (software, servicios), donde el valor contable no captura los
intangibles.

## 3. Formula de Graham revisada

```
Valor = BPA × (8,5 + 2g) × 4,4 / Y
```

`g` = crecimiento esperado a 7-10 anos (en %), `Y` = rentabilidad exigida (en %).
Extremadamente sensible a `g`: con g = 20 el multiplicador es 48,5. Limita `g` a un
maximo razonable (10-12) o el resultado deja de tener sentido.

## 4. Descuento de dividendos (Gordon)

```
Valor = D₁ / (r − g) = D₀ · (1+g) / (r − g)
```

Solo aplicable a empresas con dividendo estable y sostenible. Verifica primero el
**payout**: por encima del 80 % del beneficio, el dividendo esta en riesgo y el modelo
sobrevalora.

## 5. Multiplos comparables

Para cada multiplo (PER, EV/EBITDA, P/VC, P/Ventas):

```
Valor implicito = Multiplo del sector × Metrica de la empresa
```

Reglas de uso:

- Compara solo dentro del mismo sector y tamano similar.
- Usa la **mediana** del grupo, no la media: un solo comparable disparatado la arrastra.
- Compara tambien contra la **propia historia** de la empresa (PER actual vs. su media
  de 5 anos). Con `market.compare` construyes el grupo en una llamada.
- EV/EBITDA es preferible al PER cuando las estructuras de deuda difieren mucho.

## 6. Valoracion inversa (la mas util)

En vez de preguntar "¿cuanto vale?", pregunta "¿que hay que creerse para que el precio
de hoy sea justo?". Fija el valor por accion igual al precio de mercado y despeja el
crecimiento. En la practica: llama a `dcf_valuation` variando `growth_pct` hasta que
`margin_of_safety_pct` ≈ 0.

Ese crecimiento implicito es contrastable con la realidad: si el mercado exige un 25 %
anual durante 10 anos a una empresa que crece al 8 %, ya tienes tu conclusion sin
depender de la precision del modelo.

## 7. Sectores con reglas propias

- **Bancos**: valora por P/VC contra el ROE. Regla practica: P/VC justo ≈ (ROE − g) /
  (r − g). El FCF no es interpretable; **no uses DCF**.
- **Aseguradoras**: valor contable ajustado y ratio combinado.
- **REIT / inmobiliarias**: FFO y AFFO en lugar de beneficio; NAV frente a capitalizacion.
- **Ciclicas**: usa beneficio medio del ciclo completo. Un PER bajo en el pico del ciclo
  es la senal de venta clasica, no de compra.
- **Biotech sin ingresos**: valoracion por probabilidad de exito de la cartera de
  ensayos. Fuera del alcance de estas herramientas; dilo abiertamente.
