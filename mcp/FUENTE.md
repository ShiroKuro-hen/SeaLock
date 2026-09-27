# Indicadores tecnicos — fuente

- Repo: https://github.com/nardew/talipp (MIT, ver `LICENSE.talipp` en esta carpeta)
- Version: 2.7.0, commit `3ec88b40c59bde6ec6b77518428a1214b4a17d55` (2025-09-09)
- Descargado: 2026-09-26 (`git clone --depth 50 https://github.com/nardew/talipp`
  y checkout de la etiqueta `2.7.0`)
- Adaptado a: `mcp/indicators.py` (HU-23)

De talipp se toman **solo los indicadores que usa SeaLock**: `SMA`, `EMA`, `RSI`,
`ATR`, `StdDev`, `BB` (Bollinger) y `MACD`, con sus formulas y su semantica
incremental tal cual (primera salida en `None` hasta tener datos suficientes,
semillas y suavizado incluidos). Los ficheros de origen son
`talipp/indicators/{SMA,EMA,RSI,ATR,StdDev,BB,MACD}.py` y, para la parte
compuesta, como `BB` y `MACD` montan sus partes.

**Lo que se deja fuera a proposito**: el framework generico (`Indicator` base con
sampling de entradas, modificadores, `remove`/`purge`, oyentes de salida), la
fabrica `MAType` con sus 14 medias, `ohlcv` completo, el resto de ~90
indicadores y los paquetes `talipp.*` tal cual. SeaLock no tiene dependencias
de terceros: solo stdlib, y `historical()` calcula una ventana entera de una
pasada, asi que nada de eso se usa.

## Verificacion de equivalencia (HU-23)

Antes de sustituir los calculos hechos a mano, `mcp/indicators.py` se comparo
contra **talipp 2.7.0 real** (instalado aparte, no como dependencia del
proyecto) sobre dos series de 300 valores: una aleatoria y otra determinista.
Los siete indicadores devolvieron **los mismos valores** (tolerancia 1e-9),
incluidos los campos de `BBVal` y `MACDVal`.

Esa comparacion queda ademas fijada en `pruebas/prueba_humo.py` con valores
golden obtenidos de la propia talipp sobre una serie determinista (enteros, sin
`math`, para que salgan iguales en cualquier plataforma): un cambio en
cualquier formula hace fallar el check.

- **SMA**: identico a la media simple que ya usaba `historical()`
  (`sum(closes[-n:]) / n`, redondeada a 4 decimales). Sustitucion sin cambio
  de valor.
- **RSI**: **si difiere**, y es esperado. La version anterior promediaba los
  ultimos 14 retornos (media simple del periodo); talipp usa el suavizado de
  Wilder `(media_previa * 13 + cambio) / 14` sobre toda la historia, que es el
  RSI canonico. En la serie determinista del test: 49.0 (antigua) frente a
  47.9 (Wilder). El pilar Momentum lee `rsi_14` con los mismos umbrales 30/70,
  asi que solo cambian las notas en los valores que estaban en la frontera.
  `FUENTE.md` y el plan dejan constancia de que el cambio es deliberado.

Para repetir la comparacion contra la libreria real (no forma parte de las baterias,
que no instalan paquetes):

```bash
python -m pip install --target %TEMP%\talipp_lib talipp==2.7.0
set PYTHONPATH=%TEMP%\talipp_lib
python pruebas/compara_talipp.py
```
