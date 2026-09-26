"""Local catalog of listed equities (FinanceDatabase, MIT): see recursos/simbolos/FUENTE.md.

Read-only and offline. Loaded lazily on first use, so starting an MCP server doesn't
parse 5 MB of CSV. Resolves symbols and company names without asking Yahoo.
"""

import csv
import os
import threading

CATALOG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "recursos", "simbolos", "equities.csv"
)
OTC_MIC = "OTCM"  # every OTC shard (PNK, OID, OQB, OQX, OBB) carries this MIC
CAP_RANK = {"Mega Cap": 0, "Large Cap": 1, "Mid Cap": 2, "Small Cap": 3, "Micro Cap": 4, "Nano Cap": 5}

# Words that don't identify a company (legal forms, articles, generics). Kept
# bilingual since company names come in whichever language they were registered in.
STOP_WORDS = {
    "sa", "sab", "inc", "corp", "corporation", "plc", "nv", "ag", "se", "spa", "srl",
    "ltd", "limited", "co", "company", "group", "grupo", "holding", "holdings",
    "the", "and", "del", "las", "los", "una", "sus", "por", "con", "cia", "compania",
}


def tokens(text):
    """Significant words from a name, without accents or legal forms."""
    if not text:
        return set()
    plain = (
        text.lower()
        .replace("á", "a").replace("é", "e").replace("í", "i")
        .replace("ó", "o").replace("ú", "u").replace("ñ", "n").replace("ü", "u")
    )
    raw = "".join(c if c.isalnum() else " " for c in plain).split()
    return {t for t in raw if len(t) >= 3 and t not in STOP_WORDS}


_lock = threading.Lock()
_data = None  # {"by_symbol", "by_root", "by_token", "by_name", "by_isin", "home_country"}


def _load():
    global _data
    with _lock:
        if _data is not None:
            return _data
        rows, by_symbol, by_root, by_token, by_name, by_isin = [], {}, {}, {}, {}, {}
        countries = {}  # exchange -> {country: count}
        try:
            with open(CATALOG_PATH, encoding="utf-8", newline="") as fh:
                rows = list(csv.DictReader(fh))
        except OSError:
            rows = []  # no catalog: every caller falls back to Yahoo as before
        for r in rows:
            by_symbol[r["symbol"].upper()] = r
            by_root.setdefault(r["symbol"].split(".")[0].upper(), []).append(r)
            by_name.setdefault(r["name"], []).append(r)
            if r["isin"]:
                by_isin.setdefault(r["isin"], []).append(r)
            for t in tokens(r["name"]):
                by_token.setdefault(t, set()).add(r["symbol"].upper())
            c = countries.setdefault(r["exchange"], {})
            c[r["country"]] = c.get(r["country"], 0) + 1
        # An exchange's home country is where most of its listings are from: derived from
        # the data itself, no hand-kept exchange -> country table.
        home = {ex: max(c, key=c.get) for ex, c in countries.items()}
        _data = {"by_symbol": by_symbol, "by_root": by_root, "by_token": by_token,
                 "by_name": by_name, "by_isin": by_isin, "home_country": home}
        return _data


def lookup(symbol):
    """Catalog row for an exact Yahoo symbol, or None."""
    return _load()["by_symbol"].get((symbol or "").upper().strip())


def rows():
    """Every catalog row, one per symbol. Used to build the Hidden Gems candidate
    universe (HU-11): no copy, no filtering, that's the caller's job."""
    return list(_load()["by_symbol"].values())


def is_otc(row):
    return bool(row) and row.get("mic") == OTC_MIC


def is_home(row):
    """True when the listing is on an exchange of the company's own country (ITX.MC,
    7203.T), not a foreign line or an ADR (IXD1.DE, TM)."""
    return bool(row) and _load()["home_country"].get(row["exchange"]) == row["country"]


def rank(row):
    """Sort key: listed before OTC, home market before foreign lines, bigger first."""
    return (is_otc(row), not is_home(row), CAP_RANK.get(row.get("market_cap"), 9), row["symbol"])


def siblings(row):
    """Other listings of the same company (same ISIN, or same registered name)."""
    d = _load()
    same = list(d["by_name"].get(row["name"], []))
    return same + [r for r in d["by_isin"].get(row.get("isin"), []) if r not in same]


def search(query, limit=10):
    """Symbol first (exact, then the root before the dot: 'SAN' -> SAN, SAN.MC...), then
    company names containing every significant word of the query. Each hit brings the
    company's other listings, so a foreign line still surfaces the home one. An exact
    symbol match always stays first: typing SAN means NYSE's SAN, not Madrid's."""
    d = _load()
    q = (query or "").strip().upper()
    if not q:
        return []
    hits = [d["by_symbol"][q]] if q in d["by_symbol"] else []
    hits += [r for r in d["by_root"].get(q.split(".")[0], []) if r not in hits]
    if not hits:
        words = tokens(query)
        found = set.intersection(*(d["by_token"].get(t, set()) for t in words)) if words else set()
        hits = [d["by_symbol"][s] for s in found]
    seen, out = set(), []
    for r in sorted(hits, key=rank)[:limit]:
        for s in sorted([r] + siblings(r), key=rank):
            if s["symbol"] not in seen:
                seen.add(s["symbol"])
                out.append(s)
    exact = d["by_symbol"].get(q)
    return ([exact] if exact else []) + sorted((r for r in out if r is not exact), key=rank)[: limit - bool(exact)]


if __name__ == "__main__":
    assert lookup("SAN.MC")["isin"] == "ES0113900J37"
    assert search("Santander")[0]["symbol"] == "SAN.MC", search("Santander")[:3]
    assert search("SAN")[0]["symbol"] == "SAN" and search("san.mc")[0]["symbol"] == "SAN.MC"
    assert search("Toyota Motor")[0]["symbol"] == "7203.T"
    assert is_otc(lookup("IBDSF")) and not is_otc(lookup("IBE.MC"))
    print("ok")
