# SeaLock

Finanzas personales e inversion, en local. Todo corre en este equipo: no hay cuenta,
no hay nube, y los datos no salen de `datos/finanzas.db`.

## Abrir la aplicacion

Doble clic en **`SeaLock.bat`**. Se abre el navegador en http://127.0.0.1:8765 con
cinco pantallas: Panel, Movimientos, Cartera, Acciones y Ayuda.

Para cerrarla, `Ctrl+C` en la ventana negra que queda abierta.

Ademas de la interfaz, puedes hablar con Claude Code desde esta carpeta ("apunta 40 de
gasolina", "analiza Inditex") y usar los mismos datos. Interfaz y conversacion trabajan
sobre la misma base de datos.

## Estado

Instalado y verificado. Python 3.12.10 puesto, y las dos baterias de pruebas en verde
(45 comprobaciones del motor + 30 de la interfaz), incluidas las que salen a internet.

```bash
python pruebas/prueba_humo.py
python pruebas/prueba_web.py
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

## Las cinco pantallas

- **Panel** — patrimonio total (efectivo + cartera a precio de mercado), ahorro del mes,
  grafico de 12 meses, saldos por cuenta y presupuestos con barras de progreso.
- **Movimientos** — alta rapida, historial con filtros, recurrentes y traspasos.
- **Cartera** — posiciones con precio en vivo, plusvalia latente y peso de cada una.
- **Acciones** — buscador, ficha con fundamentales, valoracion (DCF editable con tabla
  de sensibilidad y formulas de Graham) y noticias.
- **Ayuda** — primeros pasos, glosario buscable, origen de los datos, limites y backup.

## Uso por conversacion

Se habla en lenguaje natural; las skills se activan solas.

| Lo que dices | Que ocurre |
|---|---|
| "he gastado 42 en la compra" | Se registra el gasto en `alimentacion` |
| "¿cuánto llevo gastado este mes?" | Resumen por categorias con comparativa |
| "he pasado 500 al ahorro" | Traspaso: mueve saldos sin contar como gasto |
| "mi nómina son 2100 el día 28" | Alta como recurrente; se genera cada mes |
| "importa el CSV del banco de Descargas" | Simulacion, revision y despues importacion |
| "he comprado 10 acciones de AAPL a 178" | Se registra en la cartera |
| "analiza Inditex" | Ficha: precio, fundamentales, noticias y valoracion |
| "¿cuánto vale realmente Nvidia?" | DCF + Graham con tabla de sensibilidad |
| "¿algo nuevo de mis acciones?" | Noticias filtradas por materialidad |
| "dame el informe del mes" | Patrimonio total a valor de mercado |

## Que hay dentro

**Dos servidores MCP** (`mcp/`):

- `finance` — 25 herramientas sobre SQLite local. Movimientos, cuentas (crear, editar,
  eliminar), traspasos entre cuentas propias, recurrentes, presupuestos, cartera con
  coste medio, watchlist, importacion de CSV, consultas SQL de solo lectura y copias de
  seguridad. **No accede a internet.**
- `market` — 10 herramientas. Cotizaciones, historico con volatilidad y drawdown,
  fundamentales, noticias, calendario de resultados, DCF, formulas de Graham y tipo de
  cambio.

**Cinco skills** (`.claude/skills/`): `registro-finanzas`, `analizar-accion`,
`valor-intrinseco`, `radar-noticias`, `informe-patrimonio`.

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
cifra, la herramienta falla diciendo exactamente que le falta.

Los fundamentales de Yahoo requieren ademas un token rotatorio y pueden dejar de
funcionar en cualquier momento. Si eso pasa, da de alta una clave gratuita en
[Finnhub](https://finnhub.io) o [FMP](https://financialmodelingprep.com) y anadela al
bloque `env` del servidor `market` en `.mcp.json`.

## Aviso

Herramienta de uso personal. Los precios pueden ir con ~15 minutos de retraso y los
fundamentales proceden de fuentes publicas que pueden contener errores. Nada de lo que
produce es asesoramiento financiero.
