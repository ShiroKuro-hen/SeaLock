# SeaLock

Inversion en bolsa, en local, por perfil. Todo corre en este equipo: no hay cuenta,
no hay nube, y los datos no salen de `datos/finanzas.db`. Sin bookkeeping personal:
solo cartera, watchlist y una nota 1-10 de compra/venta ajustada a tu perfil inversor.

## Abrir la aplicacion

Doble clic en **`SeaLock.bat`**. Se abre el navegador en http://127.0.0.1:8765 con
tres pantallas: Cartera, Acciones y Ayuda.

Para cerrarla, `Ctrl+C` en la ventana negra que queda abierta.

Ademas de la interfaz, puedes hablar con Claude Code desde esta carpeta ("analiza
Inditex", "he comprado 10 acciones de AAPL a 178") y usar los mismos datos. Interfaz y
conversacion trabajan sobre la misma base de datos.

## Estado

Instalado y verificado. Python 3.12.10 puesto, y las dos baterias de pruebas en verde,
incluidas las que salen a internet.

```bash
python pruebas/prueba_humo.py    # ~30 checks: finance (9 tools) + market (11 tools) + scoring.py puro
python pruebas/prueba_web.py     # ~23 checks: web API + defensas + B4 glosario
```

Usan bases de datos temporales, asi que no tocan tus datos reales.

## Instalacion (en otro equipo)

**Requisito unico: Python 3.9 o superior.** No hace falta instalar ningun paquete.

```bash
winget install -e --id Python.Python.3.12
```

Cierra y reabre la terminal. Si `python` abre la Microsoft Store en vez de ejecutarse:
Configuracion → Aplicaciones → Configuracion avanzada de aplicaciones → Alias de
ejecucion de aplicaciones → desactiva `python.exe` y `python3.exe`.

Por ultimo, reinicia Claude Code en esta carpeta para que cargue `.mcp.json`.

## Las tres pantallas

- **Cartera** — tu perfil inversor (riesgo, horizonte, preferencia), posiciones con
  precio en vivo, plusvalia latente, peso de cada una y su Nota; watchlist.
- **Acciones** — buscador, ficha con fundamentales, valoracion (DCF editable con tabla
  de sensibilidad y formulas de Graham), noticias y la **Nota 1-10** de compra/venta.
- **Ayuda** — primeros pasos, como se calcula la nota, glosario buscable, origen de
  los datos, limites y backup.

## Uso por conversacion

Se habla en lenguaje natural; las skills se activan solas.

| Lo que dices | Que ocurre |
|---|---|
| "he comprado 10 acciones de AAPL a 178" | Se registra en la cartera |
| "analiza Inditex" | Ficha: precio, fundamentales, noticias, valoracion y nota |
| "¿cuánto vale realmente Nvidia?" | DCF + Graham con tabla de sensibilidad |
| "¿algo nuevo de mis acciones?" | Noticias filtradas por materialidad |
| "pon mi perfil como agresivo, largo plazo" | Actualiza el perfil que pondera la nota |

## Que hay dentro

**Dos servidores MCP** (`mcp/`):

- `finance` — 9 herramientas sobre SQLite local. Cartera (`trade_create`,
  `portfolio_view`), watchlist, perfil inversor (`profile_get`/`profile_set`),
  consultas SQL de solo lectura y copias de seguridad. **No accede a internet.**
- `market` — 11 herramientas. Cotizaciones, historico con volatilidad y drawdown,
  fundamentales, noticias, calendario de resultados, DCF, formulas de Graham, tipo de
  cambio y `stock_score` (la nota 1-10, via `mcp/scoring.py`, un modulo puro sin red).

**Tres skills** (`.claude/skills/`): `analizar-accion`, `valor-intrinseco`,
`radar-noticias`.

## Datos

Todo vive en `datos/finanzas.db`. Copia de seguridad: pide "haz una copia de
seguridad" y se guarda en `datos/copias/`. **No subas `datos/` a ningun repositorio.**

## Fuentes de mercado

Yahoo Finance (precios, fundamentales, noticias), Google News (noticias) y Stooq
(respaldo de precios). Todas gratuitas y sin clave.

**Limitacion conocida, verificada contra el mercado real:** Yahoo cubre mucho peor las
empresas europeas. Para valores como `ITX.MC` no publica capitalizacion, numero de
acciones ni flujo de caja libre. Los dos primeros se derivan por identidad contable y
se marcan como derivados; el FCF no es derivable, asi que el DCF automatico no funciona
en esos valores y hay que darle el dato del informe anual. Antes que inventarse una
cifra, la herramienta falla diciendo exactamente que le falta. La nota lo refleja con
menor `coverage_pct`.

Los fundamentales de Yahoo requieren ademas un token rotatorio y pueden dejar de
funcionar en cualquier momento. Si eso pasa, da de alta una clave gratuita en
[Finnhub](https://finnhub.io) o [FMP](https://financialmodelingprep.com) y anadela al
bloque `env` del servidor `market` en `.mcp.json`.

## Como se calcula la nota

`mcp/scoring.py` puntua cada accion de 1 a 10 sobre seis pilares (calidad, valoracion,
solidez financiera, crecimiento, momentum, dividendo), cada uno con un peso que depende
de tu perfil inversor. Tres senales de alerta limitan la nota a 3 (payout insostenible,
deuda alta con beneficio cayendo, tendencia bajista de fondo con fundamentales
deteriorandose). Detalle completo, pesos y fuentes (Buffett, Graham, Lynch, Greenblatt,
Piotroski, AQR, Jegadeesh-Titman) en `CLAUDE.md`, seccion "Criterios de la nota".

**Es una puntuacion mecanica sobre reglas fijas y datos publicos, no asesoramiento de
un profesional con licencia. La decision es siempre tuya.**

## Aviso

Herramienta de uso personal. Los precios pueden ir con ~15 minutos de retraso y los
fundamentales proceden de fuentes publicas que pueden contener errores. Nada de lo que
produce es asesoramiento financiero.
