"""MCP server 'finance': investor profile and portfolio (trades, watchlist) in
local SQLite.

Everything lives in datos/finanzas.db inside this folder. Nothing goes to the internet.
"""

import json
import os
import sqlite3
from contextlib import closing, contextmanager
from datetime import date, datetime, timedelta

import scoring
import symbols
from mcp_common import MCPServer, log

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.environ.get("SEALOCK_DATA", os.path.join(ROOT, "datos"))
DB_PATH = os.path.join(DATA_DIR, "finanzas.db")
BASE_CURRENCY = os.environ.get("SEALOCK_CURRENCY", "EUR")

PROFILE_ENUMS = {
    "risk_tolerance": ("conservative", "moderate", "aggressive"),
    "horizon": ("short", "medium", "long"),
    "preference": ("dividend", "growth", "balanced"),
}

# Where a trade was executed. Fixed list on purpose: no broker accounts, no integrations,
# this is just a label so the same trade isn't searched in the wrong place. Add values
# here when you need them.
BROKERS = ("HapiBroker", "ETF", "Fondo mutuo", "Otro")

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    ticker TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('buy','sell')),
    quantity REAL NOT NULL CHECK (quantity > 0),
    price REAL NOT NULL CHECK (price >= 0),
    commission REAL NOT NULL DEFAULT 0,
    currency TEXT NOT NULL,
    note TEXT,
    broker TEXT,
    uploaded_at TEXT
);

CREATE TABLE IF NOT EXISTS watchlist (
    ticker TEXT PRIMARY KEY,
    name TEXT,
    thesis TEXT,
    target_price REAL,
    added_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS profile (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    risk_tolerance TEXT NOT NULL DEFAULT 'moderate',
    horizon TEXT NOT NULL DEFAULT 'long',
    preference TEXT NOT NULL DEFAULT 'balanced',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS score_reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    created_at TEXT NOT NULL,
    adjustment REAL NOT NULL,
    rationale TEXT NOT NULL,
    sources TEXT NOT NULL DEFAULT '[]',
    mechanical_score REAL,
    last_quarter_seen TEXT,
    expires_at TEXT NOT NULL,
    author TEXT NOT NULL DEFAULT 'claude'
);

CREATE TABLE IF NOT EXISTS score_cache (
    ticker TEXT PRIMARY KEY,
    scored_at TEXT NOT NULL,
    score INTEGER,
    final_score REAL,
    coverage_pct REAL,
    red_flags TEXT NOT NULL DEFAULT '[]',
    market_cap_eur REAL,
    num_analysts INTEGER,
    sector TEXT,
    country TEXT,
    region TEXT,
    quarters_available INTEGER,
    payload TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_trades_ticker ON trades(ticker);
CREATE INDEX IF NOT EXISTS idx_score_reviews_ticker ON score_reviews(ticker);
CREATE INDEX IF NOT EXISTS idx_score_cache_region ON score_cache(region);
"""


def _needs_migration(cx):
    row = cx.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='operaciones'"
    ).fetchone()
    return row is not None


def _migrate_legacy_schema(cx):
    """One-time upgrade from the original Spanish schema to the current one.

    Only trades and watchlist carry forward. Legacy bookkeeping tables (cuentas,
    movimientos, presupuestos, recurrentes) are left untouched: the app no longer
    uses them, but user data is never deleted by a migration.
    """
    cx.execute("PRAGMA foreign_keys = OFF")
    cx.execute("ALTER TABLE watchlist RENAME TO watchlist_legacy")
    cx.executescript(SCHEMA)

    cx.execute(
        """INSERT INTO trades (id, date, ticker, action, quantity, price, commission, currency, note)
           SELECT id, fecha, ticker,
                  CASE accion WHEN 'compra' THEN 'buy' ELSE 'sell' END,
                  cantidad, precio, comision, moneda, nota
           FROM operaciones"""
    )
    cx.execute(
        """INSERT INTO watchlist (ticker, name, thesis, target_price, added_at)
           SELECT ticker, nombre, tesis, precio_objetivo, anadido FROM watchlist_legacy"""
    )

    cx.execute("DROP TABLE operaciones")
    cx.execute("DROP TABLE watchlist_legacy")
    cx.commit()
    cx.execute("PRAGMA foreign_keys = ON")


def connect():
    os.makedirs(DATA_DIR, exist_ok=True)
    cx = sqlite3.connect(DB_PATH)
    cx.row_factory = sqlite3.Row
    cx.execute("PRAGMA foreign_keys = ON")
    if _needs_migration(cx):
        _migrate_legacy_schema(cx)
    else:
        cx.executescript(SCHEMA)
    _add_missing_columns(cx)
    return cx


# Columns added to an existing trades table. CREATE TABLE IF NOT EXISTS does not add
# columns to a table that already exists, so this runs on every connect() and is a no-op
# once applied. No version table: two columns do not earn one.
MIGRATIONS = (("trades", "broker", "broker TEXT"), ("trades", "uploaded_at", "uploaded_at TEXT"))


def _add_missing_columns(cx):
    for table, column, ddl in MIGRATIONS:
        cols = {r["name"] for r in cx.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            cx.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")
    cx.commit()


@contextmanager
def db():
    """Connection with automatic commit/rollback AND guaranteed close.

    'with sqlite3.connect(...)' only commits; it does not close. In a long-lived
    server that eventually exhausts file descriptors.
    """
    cx = connect()
    try:
        with cx:
            yield cx
    finally:
        cx.close()


def rows(cursor):
    return [dict(r) for r in cursor.fetchall()]


def _now():
    return datetime.now().isoformat(timespec="seconds")


def validate_date(value, default_today=True):
    if not value:
        if default_today:
            return date.today().isoformat()
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date().isoformat()
    except ValueError:
        raise ValueError(f"Invalid date '{value}'. Expected format YYYY-MM-DD.")


srv = MCPServer(
    "finance",
    "1.0.0",
    instructions=(
        "Personal investing tracking in local SQLite: trades, watchlist and investor "
        "profile. Dates in YYYY-MM-DD. "
        f"Base currency: {BASE_CURRENCY}."
    ),
)

# ----------------------------------------------------------------------- profile


@srv.tool(
    "profile_get",
    "Get the investor profile (risk tolerance, horizon, preference). Returns "
    "sensible defaults when it has never been set.",
    {},
)
def profile_get():
    with db() as cx:
        row = cx.execute(
            "SELECT risk_tolerance, horizon, preference, updated_at FROM profile WHERE id = 1"
        ).fetchone()
    if row is None:
        return {"risk_tolerance": "moderate", "horizon": "long", "preference": "balanced", "updated_at": None}
    return dict(row)


@srv.tool(
    "profile_set",
    "Update the investor profile used to weight the buy/sell score. Partial "
    "update: only the given fields change.",
    {
        "risk_tolerance": {"type": "string", "enum": list(PROFILE_ENUMS["risk_tolerance"])},
        "horizon": {"type": "string", "enum": list(PROFILE_ENUMS["horizon"])},
        "preference": {"type": "string", "enum": list(PROFILE_ENUMS["preference"])},
    },
)
def profile_set(risk_tolerance=None, horizon=None, preference=None):
    given = {"risk_tolerance": risk_tolerance, "horizon": horizon, "preference": preference}
    for name, value in given.items():
        if value is not None and value not in PROFILE_ENUMS[name]:
            raise ValueError(f"{name} must be one of {PROFILE_ENUMS[name]}")

    current = profile_get()
    merged = {k: (given[k] if given[k] is not None else current[k]) for k in PROFILE_ENUMS}
    with db() as cx:
        cx.execute(
            """INSERT INTO profile (id, risk_tolerance, horizon, preference, updated_at)
               VALUES (1,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                 risk_tolerance = excluded.risk_tolerance,
                 horizon = excluded.horizon,
                 preference = excluded.preference,
                 updated_at = excluded.updated_at""",
            (merged["risk_tolerance"], merged["horizon"], merged["preference"],
             datetime.now().isoformat(timespec="seconds")),
        )
    return {"ok": True, **merged}


# ---------------------------------------------------------------------- portfolio


@srv.tool(
    "trade_create",
    "Record a buy or sell of a stock/ETF in the portfolio.",
    {
        "ticker": {"type": "string", "description": "Symbol, e.g. AAPL or ITX.MC"},
        "action": {"type": "string", "enum": ["buy", "sell"]},
        "quantity": {"type": "number"},
        "price": {"type": "number", "description": "Price per share"},
        "date": {"type": "string"},
        "commission": {"type": "number", "default": 0},
        "currency": {"type": "string"},
        "note": {"type": "string", "description": "Reason for the trade"},
        "broker": {
            "type": "string",
            "enum": list(BROKERS),
            "description": "Where you executed it; None when unknown",
        },
        "uploaded": {
            "type": "boolean",
            "default": False,
            "description": "True when you already put it in the broker yourself",
        },
    },
    required=["ticker", "action", "quantity", "price"],
)
def trade_create(ticker, action, quantity, price, date=None, commission=0, currency=None,
                 note=None, broker=None, uploaded=False):
    if action not in ("buy", "sell"):
        raise ValueError("action must be 'buy' or 'sell'")
    ticker = ticker.upper().strip()
    if broker is not None and broker not in BROKERS:
        raise ValueError(f"broker must be one of {', '.join(BROKERS)}")

    with db() as cx:
        if action == "sell":
            row = cx.execute(
                """SELECT COALESCE(SUM(CASE WHEN action='buy' THEN quantity ELSE -quantity END),0) AS pos
                   FROM trades WHERE ticker = ?""",
                (ticker,),
            ).fetchone()
            if float(quantity) > row["pos"] + 1e-9:
                raise ValueError(
                    f"Cannot sell {quantity} of {ticker}: current position is {row['pos']}"
                )

        cur = cx.execute(
            """INSERT INTO trades (date,ticker,action,quantity,price,commission,currency,note,
                                    broker,uploaded_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                validate_date(date),
                ticker,
                action,
                float(quantity),
                float(price),
                float(commission or 0),
                (currency or BASE_CURRENCY).upper(),
                note,
                broker,
                _now() if uploaded else None,
            ),
        )
    return {
        "ok": True,
        "id": cur.lastrowid,
        "ticker": ticker,
        "broker": broker,
        "uploaded": bool(uploaded),
        "total_amount": round(float(quantity) * float(price) + float(commission or 0), 2),
    }


@srv.tool(
    "portfolio_view",
    "Open positions with average cost and realized gains (average-cost method). "
    "Does not include market price: use the 'market' server for that.",
    {"include_closed": {"type": "boolean", "default": False}},
)
def portfolio_view(include_closed=False):
    with db() as cx:
        trades = rows(cx.execute("SELECT * FROM trades ORDER BY date, id"))

    positions = {}
    for t in trades:
        p = positions.setdefault(
            t["ticker"],
            {
                "ticker": t["ticker"],
                "quantity": 0.0,
                "total_cost": 0.0,
                "realized": 0.0,
                "commissions": 0.0,
                "currency": t["currency"],
                "trades": 0,
                "brokers": set(),
            },
        )
        p["trades"] += 1
        if t.get("broker"):
            p["brokers"].add(t["broker"])
        p["commissions"] += t["commission"]
        if t["action"] == "buy":
            p["total_cost"] += t["quantity"] * t["price"] + t["commission"]
            p["quantity"] += t["quantity"]
        else:
            avg_cost = p["total_cost"] / p["quantity"] if p["quantity"] else 0.0
            cost_of_sold = avg_cost * t["quantity"]
            p["realized"] += t["quantity"] * t["price"] - cost_of_sold - t["commission"]
            p["total_cost"] -= cost_of_sold
            p["quantity"] -= t["quantity"]

    output = []
    for p in positions.values():
        open_pos = p["quantity"] > 1e-9
        if not open_pos and not include_closed:
            continue
        output.append(
            {
                "ticker": p["ticker"],
                "quantity": round(p["quantity"], 6),
                "average_cost": round(p["total_cost"] / p["quantity"], 4) if open_pos else None,
                "total_cost": round(p["total_cost"], 2),
                "realized_gain": round(p["realized"], 2),
                "commissions_paid": round(p["commissions"], 2),
                "currency": p["currency"],
                "brokers": ", ".join(sorted(p["brokers"])) or None,
                "status": "open" if open_pos else "closed",
            }
        )
    output.sort(key=lambda x: -(x["total_cost"] or 0))

    # Summing amounts across different currencies without conversion produces a
    # meaningless figure, so totals are broken down by currency and only
    # aggregated when there is a single one.
    realized_by_currency, cost_by_currency = {}, {}
    for x in output:
        c = x["currency"]
        realized_by_currency[c] = round(realized_by_currency.get(c, 0) + x["realized_gain"], 2)
        if x["quantity"] > 0:
            cost_by_currency[c] = round(cost_by_currency.get(c, 0) + x["total_cost"], 2)
    multi_currency = len(set(list(realized_by_currency) + list(cost_by_currency))) > 1

    return {
        "positions": output,
        "currencies": sorted(set(x["currency"] for x in output)),
        "cost_by_currency": cost_by_currency,
        "realized_by_currency": realized_by_currency,
        "total_cost_invested": None if multi_currency else round(sum(cost_by_currency.values()), 2),
        "accumulated_realized": None if multi_currency else round(sum(realized_by_currency.values()), 2),
        "note": (
            "There are positions in multiple currencies: aggregate totals are null on "
            "purpose. Convert with market.exchange_rate before summing."
            if multi_currency
            else "For market value and unrealized P&L, call market.quote with these tickers."
        ),
    }


# -------------------------------------------------------------------- watchlist


@srv.tool(
    "watchlist_add",
    "Add a stock to follow, with your investment thesis and target price.",
    {
        "ticker": {"type": "string"},
        "name": {"type": "string"},
        "thesis": {"type": "string", "description": "Why this company is interesting"},
        "target_price": {"type": "number"},
    },
    required=["ticker"],
)
def watchlist_add(ticker, name=None, thesis=None, target_price=None):
    with db() as cx:
        cx.execute(
            """INSERT INTO watchlist (ticker,name,thesis,target_price,added_at) VALUES (?,?,?,?,?)
               ON CONFLICT(ticker) DO UPDATE SET
                 name = COALESCE(excluded.name, watchlist.name),
                 thesis = COALESCE(excluded.thesis, watchlist.thesis),
                 target_price = COALESCE(excluded.target_price, watchlist.target_price)""",
            (
                ticker.upper().strip(),
                name,
                thesis,
                target_price,
                date.today().isoformat(),
            ),
        )
    return {"ok": True, "ticker": ticker.upper().strip()}


@srv.tool("watchlist_list", "List the tracked stocks with their thesis.", {})
def watchlist_list():
    with db() as cx:
        return {"watchlist": rows(cx.execute("SELECT * FROM watchlist ORDER BY ticker"))}


@srv.tool(
    "watchlist_remove",
    "Remove a ticker from tracking.",
    {"ticker": {"type": "string"}},
    required=["ticker"],
)
def watchlist_remove(ticker):
    with db() as cx:
        cur = cx.execute("DELETE FROM watchlist WHERE ticker = ?", (ticker.upper().strip(),))
    return {"ok": cur.rowcount > 0, "ticker": ticker.upper().strip()}


# ------------------------------------------------------------------ broker sync


@srv.tool(
    "trades_pending",
    "Trades you have not yet entered in the broker, grouped by broker. SeaLock never "
    "sends anything anywhere: this is the list of what YOU still have to copy over.",
    {"broker": {"type": "string", "enum": list(BROKERS)}},
)
def trades_pending(broker=None):
    if broker is not None and broker not in BROKERS:
        raise ValueError(f"broker must be one of {', '.join(BROKERS)}")
    with db() as cx:
        data = rows(cx.execute(
            """SELECT * FROM trades WHERE uploaded_at IS NULL
               AND (? IS NULL OR broker = ?)
               ORDER BY date, id""",
            (broker, broker),
        ))
    grouped = {}
    for t in data:
        grouped.setdefault(t["broker"] or "sin broker", []).append(
            {"id": t["id"], "date": t["date"], "ticker": t["ticker"],
             "action": t["action"], "quantity": t["quantity"], "price": t["price"]}
        )
    return {
        "pending": data and len(data) or 0,
        "by_broker": {k: {"count": len(v), "trades": v} for k, v in sorted(grouped.items())},
    }


@srv.tool(
    "trades_mark_uploaded",
    "Mark trades as already entered in the broker. No ids means every pending trade of "
    "that broker. Records when, nothing else.",
    {
        "broker": {"type": "string", "enum": list(BROKERS)},
        "ids": {"type": "array", "items": {"type": "integer"}},
    },
)
def trades_mark_uploaded(broker=None, ids=None):
    if broker is not None and broker not in BROKERS:
        raise ValueError(f"broker must be one of {', '.join(BROKERS)}")
    stamp = _now()
    with db() as cx:
        if ids:
            marks = ",".join("?" * len(ids))
            cur = cx.execute(
                f"UPDATE trades SET uploaded_at=? WHERE id IN ({marks})", [stamp, *ids])
        else:
            cur = cx.execute(
                """UPDATE trades SET uploaded_at=?
                   WHERE uploaded_at IS NULL AND (? IS NULL OR broker=?)""",
                (stamp, broker, broker),
            )
    return {"ok": True, "marked": cur.rowcount, "uploaded_at": stamp}


# --------------------------------------------------------------------- reviews

MAX_ADJUSTMENT = 2.0
MAX_RATIONALE = 2000


def review_status(review, latest_quarter_end=None):
    """'active', 'expired' or 'stale'. Pure: the newest quarter is passed in because
    this server has no network to fetch it itself."""
    if review.get("expires_at") and review["expires_at"] < date.today().isoformat():
        return "expired"
    seen, latest = review.get("last_quarter_seen"), latest_quarter_end
    if seen and latest and latest > seen:
        return "stale"
    return "active"


def _review_row(row, latest_quarter_end=None):
    review = dict(row)
    review["sources"] = json.loads(review["sources"] or "[]")
    review["status"] = review_status(review, latest_quarter_end)
    return review


@srv.tool(
    "review_set",
    "Store a Claude review of a stock's mechanical score: a bounded adjustment (+-"
    f"{MAX_ADJUSTMENT:g}) with its rationale and sources. A newer review for the same "
    "ticker becomes the current one; the previous stays as history.",
    {
        "ticker": {"type": "string"},
        "adjustment": {"type": "number", "description": f"-{MAX_ADJUSTMENT:g}..{MAX_ADJUSTMENT:g}"},
        "rationale": {"type": "string", "description": "Why, citing concrete facts"},
        "sources": {"type": "array", "items": {"type": "string"}},
        "mechanical_score": {"type": "number", "description": "Score before adjusting"},
        "last_quarter_seen": {
            "type": "string",
            "description": "Newest quarter close the review looked at (YYYY-MM-DD)",
        },
        "expires_days": {"type": "integer", "default": 30},
    },
    required=["ticker", "adjustment", "rationale"],
)
def review_set(ticker, adjustment, rationale, sources=None, mechanical_score=None,
               last_quarter_seen=None, expires_days=30):
    ticker = ticker.upper().strip()
    try:
        adjustment = float(adjustment)
    except (TypeError, ValueError):
        raise ValueError(f"adjustment must be a number between -{MAX_ADJUSTMENT:g} and {MAX_ADJUSTMENT:g}")
    if not -MAX_ADJUSTMENT <= adjustment <= MAX_ADJUSTMENT:
        raise ValueError(f"adjustment must be between -{MAX_ADJUSTMENT:g} and {MAX_ADJUSTMENT:g}")
    rationale = (rationale or "").strip()
    if not rationale or len(rationale) > MAX_RATIONALE:
        raise ValueError(f"rationale must be 1-{MAX_RATIONALE} characters")
    sources = list(sources or [])
    for source in sources:
        if not isinstance(source, str) or not source.startswith(("http://", "https://")):
            raise ValueError(f"every source must be an http(s) URL, got: {source!r}")
    days = int(expires_days)
    if days < 0:
        raise ValueError("expires_days must be 0 or more")
    today = date.today()
    with db() as cx:
        cur = cx.execute(
            """INSERT INTO score_reviews (ticker, created_at, adjustment, rationale,
                                          sources, mechanical_score, last_quarter_seen, expires_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (ticker, today.isoformat(), adjustment, rationale, json.dumps(sources),
             None if mechanical_score is None else float(mechanical_score),
             validate_date(last_quarter_seen, default_today=False),
             (today + timedelta(days=days)).isoformat()),
        )
        row = cx.execute("SELECT * FROM score_reviews WHERE id=?", (cur.lastrowid,)).fetchone()
    return {"ok": True, "review": _review_row(row)}


@srv.tool(
    "review_get",
    "The current review for a ticker (its most recent one) with its status, or null.",
    {
        "ticker": {"type": "string"},
        "latest_quarter_end": {
            "type": "string",
            "description": "Newest quarter close available, to flag stale reviews",
        },
    },
    required=["ticker"],
)
def review_get(ticker, latest_quarter_end=None):
    ticker = ticker.upper().strip()
    with db() as cx:
        row = cx.execute(
            "SELECT * FROM score_reviews WHERE ticker=? ORDER BY created_at DESC, id DESC LIMIT 1",
            (ticker,),
        ).fetchone()
    return {
        "ticker": ticker,
        "review": _review_row(row, latest_quarter_end) if row else None,
    }


@srv.tool(
    "reviews_list",
    "Every stored score review, newest first, each with its status.",
    {"latest_quarter_end": {"type": "string"}},
)
def reviews_list(latest_quarter_end=None):
    with db() as cx:
        data = rows(cx.execute("SELECT * FROM score_reviews ORDER BY created_at DESC, id DESC"))
    return {"reviews": [_review_row(r, latest_quarter_end) for r in data]}


@srv.tool(
    "review_delete",
    "Delete a review by id. Without confirm it only returns the record, so it can be "
    "shown before anything is removed.",
    {"id": {"type": "integer"}, "confirm": {"type": "boolean", "default": False}},
    required=["id"],
)
def review_delete(id, confirm=False):
    with db() as cx:
        row = cx.execute("SELECT * FROM score_reviews WHERE id=?", (int(id),)).fetchone()
        if row is None:
            return {"ok": False, "error": f"No review with id {id}"}
        if not confirm:
            return {"ok": False, "needs_confirmation": True, "review": _review_row(row)}
        cx.execute("DELETE FROM score_reviews WHERE id=?", (int(id),))
    return {"ok": True, "deleted": _review_row(row)}


# ----------------------------------------------------------------- hidden gems
#
# score_cache holds market data (not user data): the Hidden Gems scan results, written
# by app/servidor.py's background scanner (it has network access; this server does
# not). These are plain functions, not MCP tools: the scanner calls them directly in
# the same process. gems_list below IS a tool, so Claude can read the current gems.

GEM_FRESH_DAYS = 14  # HU-12/HU-13: gems are chosen among entries scored within this window


def score_cache_upsert(ticker, scored_at, score, coverage_pct, red_flags, market_cap_eur,
                       num_analysts, sector, country, region, quarters_available, payload):
    with db() as cx:
        cx.execute(
            """INSERT INTO score_cache (ticker, scored_at, score, final_score, coverage_pct,
                                         red_flags, market_cap_eur, num_analysts, sector,
                                         country, region, quarters_available, payload)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(ticker) DO UPDATE SET
                 scored_at = excluded.scored_at,
                 score = excluded.score,
                 final_score = excluded.final_score,
                 coverage_pct = excluded.coverage_pct,
                 red_flags = excluded.red_flags,
                 market_cap_eur = excluded.market_cap_eur,
                 num_analysts = excluded.num_analysts,
                 sector = excluded.sector,
                 country = excluded.country,
                 region = excluded.region,
                 quarters_available = excluded.quarters_available,
                 payload = excluded.payload""",
            (ticker, scored_at, score, score, coverage_pct, json.dumps(red_flags or []),
             market_cap_eur, num_analysts, sector, country, region, quarters_available,
             json.dumps(payload or {})),
        )
    return {"ok": True, "ticker": ticker}


def score_cache_all():
    """Every cached ticker, with red_flags/payload already decoded."""
    with db() as cx:
        data = rows(cx.execute("SELECT * FROM score_cache"))
    for r in data:
        r["red_flags"] = json.loads(r["red_flags"] or "[]")
        r["payload"] = json.loads(r["payload"] or "{}")
    return data


@srv.tool(
    "gems_list",
    "Current Hidden Gems (HU-13 rule) from the local score cache: no network. Combines "
    "each cached mechanical score with any active Claude review (final_score), the same "
    "way the rest of the app does, so Claude can comment on what's already showing.",
    {},
)
def gems_list():
    cache = score_cache_all()
    profile = profile_get()
    cutoff = (date.today() - timedelta(days=GEM_FRESH_DAYS)).isoformat()
    fresh = [r for r in cache if r["scored_at"][:10] >= cutoff]

    try:
        held_tickers = [p["ticker"] for p in portfolio_view()["positions"]]
    except Exception:  # noqa: BLE001 - gems still work without portfolio context
        held_tickers = []
    # Held sectors come from the local catalog (offline, same GICS names the scanner
    # stores), so `diversifies` works without network.
    held_sectors = {(symbols.lookup(t) or {}).get("sector") for t in held_tickers} - {None, ""}

    candidates = []
    for r in fresh:
        mech = scoring.recompute_from_payload(r["payload"], profile)
        review = None
        try:
            review = review_get(r["ticker"])["review"]
        except Exception:  # noqa: BLE001 - a missing review must never break the list
            review = None
        applied = scoring.apply_review(mech, review)
        candidates.append({
            "ticker": r["ticker"], "name": (r["payload"] or {}).get("name"),
            "scored_at": r["scored_at"],
            "score": mech["score"], "final_score": applied["final_score"],
            "label": applied["label"], "sentiment": applied["sentiment"],
            "review": review, "review_applied": applied["review_applied"],
            "coverage_pct": mech["coverage_pct"], "coverage_warning": mech["coverage_warning"],
            "red_flags": mech["red_flags"], "pros": mech["pros"], "cons": mech["cons"],
            "notice": mech["notice"], "pillars": mech["pillars"],
            "quarters_available": r["quarters_available"],
            "market_cap_eur": r["market_cap_eur"], "num_analysts": r["num_analysts"],
            "sector": r["sector"], "country": r["country"], "region": r["region"],
        })

    gems = scoring.select_gems(candidates, held_tickers, held_sectors)
    return {
        "gems": gems,
        "scanned": len(cache),
        "fresh_for_gems": len(fresh),
        "candidates_considered": len(candidates),
    }


# ------------------------------------------------------------------------- SQL


@srv.tool(
    "sql_query",
    "Run a read-only SELECT query against the database, for analysis not covered "
    "by the other tools. Tables: trades, watchlist, profile, score_reviews, score_cache.",
    {
        "sql": {"type": "string", "description": "Must start with SELECT or WITH"},
        "limit": {"type": "integer", "default": 200},
    },
    required=["sql"],
)
def sql_query(sql, limit=200):
    clean = sql.strip().rstrip(";")
    if not clean.lower().startswith(("select", "with")):
        raise ValueError("Only SELECT or WITH queries are allowed")
    if ";" in clean:
        raise ValueError("Multiple statements are not allowed")
    with db() as cx:
        cx.execute("PRAGMA query_only = ON")
        data = rows(cx.execute(clean))
    return {"rows": data[: int(limit)], "total": len(data)}


@srv.tool(
    "backup_create",
    "Create a timestamped copy of the database in datos/copias/.",
    {},
)
def backup_create():
    dest_dir = os.path.join(DATA_DIR, "copias")
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, f"finanzas-{datetime.now():%Y%m%d-%H%M%S}.db")
    with closing(connect()) as origin, closing(sqlite3.connect(dest)) as copy:
        origin.backup(copy)
    return {"ok": True, "file": dest, "size_kb": round(os.path.getsize(dest) / 1024, 1)}


if __name__ == "__main__":
    log(f"[finance] database: {DB_PATH}")
    srv.run()
