---
name: valor-intrinseco
description: Estimar el valor real (intrinseco) de una empresa cotizada mediante DCF, formulas de Graham, descuento de dividendos y multiplos, con supuestos justificados y analisis de sensibilidad. Usar cuando el usuario pregunte cuanto vale realmente una accion, si esta cara o barata, cual es su valor justo o su margen de seguridad, o pida un DCF o una valoracion.
---

# Valor intrinseco

El precio es lo que se paga; el valor es lo que se recibe. Este flujo estima lo segundo.

**La cifra unica es la parte menos importante del resultado.** Un DCF con supuestos
apenas distintos da valores que difieren en un 200 %. Lo que aporta valor es el rango
y saber que supuestos habria que creerse para justificar el precio de hoy.

## Elegir metodo

| Tipo de empresa | Metodo principal | Motivo |
|---|---|---|
| Madura, flujo de caja estable | DCF (`dcf_valuation`) | El FCF es predecible |
| Value clasica, con beneficios | Graham (`graham_valuation`) | Rapido y conservador |
| Alta rentabilidad por dividendo | Gordon (dentro de `graham_valuation`) | El dividendo es el retorno |
| Alto crecimiento, sin beneficios | Multiplos + escenarios | El DCF se vuelve ciencia-ficcion |
| Banca y aseguradoras | P/VC y ROE, **no** DCF | El FCF no es interpretable |
| Ciclicas (materias primas, auto) | Beneficio medio del ciclo | Un ano suelto enganа |

Usa **al menos dos** metodos. Si convergen, aumenta la confianza; si divergen mucho,
eso mismo es el hallazgo y hay que decirlo.

## Supuestos: de donde salen

Nunca inventes el crecimiento. Anclalo y explica el ancla:

- **Crecimiento fase 1**: parte del crecimiento historico de ingresos/FCF
  (`fundamentals` → `growth`, e `historical` a 5 anos). Recortalo: casi ninguna
  empresa sostiene 20 % anual durante una decada.
- **Crecimiento terminal**: 2–2,5 %. Nunca por encima del crecimiento del PIB nominal
  a largo plazo; una empresa que crece perpetuamente mas que la economia acabaria
  siendo la economia.
- **Tasa de descuento**: 8–10 % para empresas grandes y estables; 11–14 % para
  pequenas, endeudadas o volatiles. Sube la tasa con la beta y con la deuda neta.
- **FCF base**: si el ultimo ano fue atipico (extraordinarios, un capex puntual), usa
  la media de 3 anos y explicalo.

Consulta `references/metodos.md` para las formulas, el detalle de cada metodo y los
errores tipicos.

## Cuando faltan datos (habitual en valores europeos)

Yahoo publica bastante menos de las empresas europeas que de las estadounidenses. En
la practica:

- **Capitalizacion y numero de acciones**: `fundamentals` los deriva por identidad
  contable (`market_cap = enterprise_value - net_debt`, y `shares_outstanding =
  market_cap / price`) y los enumera en `derived_fields`. Son exactos, pero **di que
  son derivados** al presentarlos.
- **Nunca uses `floatShares` como numero de acciones.** Es solo el capital flotante. En
  Inditex son 1.108 M frente a 3.116 M totales, porque Amancio Ortega controla ~59 %:
  usarlo triplicaria el valor por accion.
- **Flujo de caja libre**: Yahoo no lo da para muchos valores europeos, y sin el no hay
  DCF. La herramienta falla a proposito, enumerando que datos si hay. Las salidas son:
  1. Pedir al usuario el FCF del informe anual (flujo operativo − capex). Es lo correcto.
  2. Cambiar a `graham_valuation`, que solo necesita BPA y valor contable.
  3. Valorar por multiplos contra comparables del sector.

  **No sustituyas el FCF por el beneficio neto en silencio.** No son lo mismo: difieren
  en amortizaciones, circulante e inversiones. Si haces esa aproximacion por falta de
  alternativa, dilo en la misma frase en que das el resultado.

## Presentacion

1. **Rango, no punto**: "valor estimado entre 120 y 165 €, centro 138 €".
2. **Tabla de sensibilidad** siempre. La devuelve `dcf_valuation`; incluyela.
3. **Margen de seguridad**: cuanto descuento hay frente al valor central. Por debajo de
   un 20-30 % el margen no compensa el error del propio modelo.
4. **Valoracion inversa**, cuando sea revelador: "a este precio, el mercado descuenta un
   14 % de crecimiento anual durante 10 anos. ¿Es creible?". Esto suele decir mas que
   el propio DCF: pruebalo variando `growth_pct` hasta que el valor iguale al precio.
5. **Los supuestos, visibles**. Nunca escondas los inputs de los que sale la cifra.

## Limites que hay que declarar

- Empresas en perdidas: el DCF sobre FCF negativo no significa nada.
- Deuda neta: si es enorme frente a la capitalizacion, el equity value es hipersensible
  a la tasa de descuento.
- Los fundamentales vienen de fuentes publicas y pueden ser erroneos. Ante cifras raras
  (margenes > 60 %, PER negativo, FCF que no cuadra con el EBITDA), avisa y sugiere
  contrastar con el informe anual de la empresa.
- Esto es analisis, no asesoramiento financiero. Que quede dicho al cerrar la valoracion.
