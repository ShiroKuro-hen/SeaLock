# Catalogo de simbolos — fuente

- Repo: https://github.com/JerBouma/FinanceDatabase (MIT, ver `LICENSE`)
- Commit: `d0b95bd51f9c594c81bac0b20c7ab6cbd084c51e` (2026-09-20)
- Descargado: 2026-09-26, `database/equities/<SHARD>.csv`

`equities.csv` junta estos shards, sin las filas `delisted == True` y solo con las columnas
`symbol, name, country, sector, industry, exchange, market, mic, market_cap, isin, currency`
(el original trae descripciones largas, FIGI, web... que no usamos):

- EE. UU.: NYQ, NMS, NGM, NCM, ASE
- Canada TOR, Reino Unido LSE
- Europa: MCE, PAR, AMS, MIL, GER, EBS, LIS, BRU, STO, CPH, HEL, OSL, VIE, ISE
- Asia-Pacifico: JPX, HKG, KSC, TAI, NSE, NSI, ASX, SES
- Latinoamerica y Africa: SAO, MEX, JNB
- OTC, solo para clasificar (`mic == OTCM`), nunca como candidatos: PNK, OID, OQB, OQX, OBB

Fuera a proposito: **FRA** (Frankfurt). Son ~10.500 lineas secundarias de valores
extranjeros; las empresas alemanas ya estan en GER (Xetra). India va en NSE (el shard NSI
solo trae 25 valores).

Datos conocidos imperfectos: muchos valores sin ISIN (Inditex, Toyota) o con el de otra
linea; `market_cap` es una categoria ("Small Cap"...), no una cifra. Por eso la busqueda
agrupa por ISIN **o** por nombre registrado.

Para actualizar: cambiar el commit y re-ejecutar, desde una carpeta con los shards
descargados:

```python
import csv, glob
cols = ["symbol", "name", "country", "sector", "industry", "exchange", "market", "mic",
        "market_cap", "isin", "currency"]
with open("equities.csv", "w", newline="", encoding="utf-8") as fh:
    w = csv.writer(fh); w.writerow(cols)
    for f in sorted(glob.glob("*.csv")):
        if f in ("FRA.csv", "equities.csv"):
            continue
        for r in csv.DictReader(open(f, encoding="utf-8")):
            if r["delisted"] != "True":
                w.writerow([r[c] for c in cols])
```
