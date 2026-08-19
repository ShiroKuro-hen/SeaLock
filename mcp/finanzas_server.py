"""MCP server 'finance': personal finance tracking in local SQLite.

Everything lives in datos/finanzas.db inside this folder. Nothing goes to the internet.
"""

import calendar
import csv
import os
import sqlite3
from contextlib import closing, contextmanager
from datetime import date, datetime

# Reserved category: transfers between own accounts move balance but are neither
# income nor a real expense, so they are excluded from all summaries.
TRANSFER_CATEGORY = "transfer"

from mcp_common import MCPServer, log

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.environ.get("SEALOCK_DATA", os.path.join(ROOT, "datos"))
DB_PATH = os.path.join(DATA_DIR, "finanzas.db")
BASE_CURRENCY = os.environ.get("SEALOCK_CURRENCY", "EUR")

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    type TEXT NOT NULL DEFAULT 'checking',
    currency TEXT NOT NULL,
    initial_balance REAL NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    type TEXT NOT NULL CHECK (type IN ('income','expense')),
    amount REAL NOT NULL CHECK (amount > 0),
    currency TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'uncategorized',
    subcategory TEXT,
    account_id INTEGER REFERENCES accounts(id),
    description TEXT,
    tags TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS budgets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category TEXT NOT NULL,
    limit_amount REAL NOT NULL CHECK (limit_amount > 0),
    currency TEXT NOT NULL,
    period TEXT NOT NULL DEFAULT 'monthly',
    UNIQUE (category, period)
);

CREATE TABLE IF NOT EXISTS recurring_transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    concept TEXT NOT NULL,
    type TEXT NOT NULL CHECK (type IN ('income','expense')),
    amount REAL NOT NULL CHECK (amount > 0),
    category TEXT NOT NULL,
    account_id INTEGER REFERENCES accounts(id),
    frequency TEXT NOT NULL CHECK (frequency IN ('monthly','yearly')),
    day INTEGER NOT NULL DEFAULT 1,
    month INTEGER,
    currency TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    start_date TEXT NOT NULL,
    end_date TEXT
);

CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    ticker TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('buy','sell')),
    quantity REAL NOT NULL CHECK (quantity > 0),
    price REAL NOT NULL CHECK (price >= 0),
    commission REAL NOT NULL DEFAULT 0,
    currency TEXT NOT NULL,
    account_id INTEGER REFERENCES accounts(id),
    note TEXT
);

CREATE TABLE IF NOT EXISTS watchlist (
    ticker TEXT PRIMARY KEY,
    name TEXT,
    thesis TEXT,
    target_price REAL,
    added_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tx_date ON transactions(date);
CREATE INDEX IF NOT EXISTS idx_tx_category ON transactions(category);
CREATE INDEX IF NOT EXISTS idx_trades_ticker ON trades(ticker);
"""


def _needs_migration(cx):
    row = cx.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='cuentas'"
    ).fetchone()
    return row is not None


def _migrate_legacy_schema(cx):
    """One-time upgrade from the original Spanish schema to the current English one.

    SQLite can't ALTER a CHECK constraint in place, so every table is rebuilt: the
    new English tables are created fresh, rows are copied over with their values
    translated, and the old Spanish tables are dropped. Runs once per database --
    afterwards `cuentas` no longer exists, so `_needs_migration` returns False.
    """
    cx.execute("PRAGMA foreign_keys = OFF")
    cx.execute("ALTER TABLE watchlist RENAME TO watchlist_legacy")
    cx.executescript(SCHEMA)

    cx.execute(
        """INSERT INTO accounts (id, name, type, currency, initial_balance, active)
           SELECT id, nombre,
                  CASE tipo
                    WHEN 'corriente' THEN 'checking'
                    WHEN 'ahorro' THEN 'savings'
                    WHEN 'efectivo' THEN 'cash'
                    WHEN 'tarjeta' THEN 'card'
                    ELSE tipo
                  END,
                  moneda, saldo_inicial, activa
           FROM cuentas"""
    )
    cx.execute(
        """INSERT INTO transactions
           (id, date, type, amount, currency, category, subcategory, account_id,
            description, tags, created_at)
           SELECT id, fecha,
                  CASE tipo WHEN 'ingreso' THEN 'income' ELSE 'expense' END,
                  importe, moneda,
                  CASE categoria WHEN 'transferencia' THEN 'transfer' ELSE categoria END,
                  subcategoria, cuenta_id, descripcion,
                  REPLACE(REPLACE(etiquetas, 'recurrente:', 'recurring:'), 'traspaso:', 'transfer:'),
                  creado
           FROM movimientos"""
    )
    cx.execute(
        """INSERT INTO budgets (id, category, limit_amount, currency, period)
           SELECT id, categoria, limite, moneda,
                  CASE periodo WHEN 'mensual' THEN 'monthly' ELSE periodo END
           FROM presupuestos"""
    )
    cx.execute(
        """INSERT INTO recurring_transactions
           (id, concept, type, amount, category, account_id, frequency, day, month,
            currency, active, start_date, end_date)
           SELECT id, concepto,
                  CASE tipo WHEN 'ingreso' THEN 'income' ELSE 'expense' END,
                  importe, categoria, cuenta_id,
                  CASE frecuencia WHEN 'mensual' THEN 'monthly' ELSE 'yearly' END,
                  dia, mes, moneda, activo, desde, hasta
           FROM recurrentes"""
    )
    cx.execute(
        """INSERT INTO trades
           (id, date, ticker, action, quantity, price, commission, currency,
            account_id, note)
           SELECT id, fecha, ticker,
                  CASE accion WHEN 'compra' THEN 'buy' ELSE 'sell' END,
                  cantidad, precio, comision, moneda, cuenta_id, nota
           FROM operaciones"""
    )
    cx.execute(
        """INSERT INTO watchlist (ticker, name, thesis, target_price, added_at)
           SELECT ticker, nombre, tesis, precio_objetivo, anadido FROM watchlist_legacy"""
    )

    cx.execute("DROP TABLE cuentas")
    cx.execute("DROP TABLE movimientos")
    cx.execute("DROP TABLE presupuestos")
    cx.execute("DROP TABLE recurrentes")
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
    return cx


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
        "Personal finance tracking in local SQLite. Amounts always positive; "
        "the sign is determined by the 'type' field (income/expense). Dates in YYYY-MM-DD. "
        f"Base currency: {BASE_CURRENCY}."
    ),
)

# --------------------------------------------------------------------- accounts


@srv.tool(
    "account_create",
    "Create an account (bank, cash, card, broker...) to record transactions against.",
    {
        "name": {"type": "string", "description": "Unique name, e.g. 'BBVA payroll'"},
        "type": {
            "type": "string",
            "description": "checking | savings | cash | card | broker",
            "default": "checking",
        },
        "currency": {"type": "string", "description": f"ISO 4217. Defaults to {BASE_CURRENCY}"},
        "initial_balance": {"type": "number", "default": 0},
    },
    required=["name"],
)
def account_create(name, type="checking", currency=None, initial_balance=0):
    with db() as cx:
        cur = cx.execute(
            "INSERT INTO accounts (name, type, currency, initial_balance) VALUES (?,?,?,?)",
            (name, type, (currency or BASE_CURRENCY).upper(), float(initial_balance)),
        )
        return {"ok": True, "id": cur.lastrowid, "name": name}


@srv.tool(
    "account_update",
    "Edit an existing account: rename, change type, or adjust initial balance.",
    {
        "id": {"type": "integer"},
        "name": {"type": "string", "description": "New unique name"},
        "type": {
            "type": "string",
            "description": "checking | savings | cash | card | broker",
        },
        "initial_balance": {"type": "number"},
    },
    required=["id"],
)
def account_update(id, name=None, type=None, initial_balance=None):
    changes, args = [], []
    if name is not None:
        changes.append("name = ?")
        args.append(name)
    if type is not None:
        changes.append("type = ?")
        args.append(type)
    if initial_balance is not None:
        changes.append("initial_balance = ?")
        args.append(float(initial_balance))
    if not changes:
        raise ValueError("No field specified to modify")

    with db() as cx:
        before = cx.execute("SELECT * FROM accounts WHERE id = ?", (id,)).fetchone()
        if before is None:
            raise ValueError(f"Account {id} does not exist")
        cx.execute(f"UPDATE accounts SET {', '.join(changes)} WHERE id = ?", args + [id])
        after = cx.execute("SELECT * FROM accounts WHERE id = ?", (id,)).fetchone()
    return {"ok": True, "before": dict(before), "after": dict(after)}


@srv.tool(
    "account_delete",
    "Delete an account and all its transactions permanently. Cannot be undone.",
    {"id": {"type": "integer"}},
    required=["id"],
)
def account_delete(id):
    with db() as cx:
        account = cx.execute("SELECT * FROM accounts WHERE id = ?", (id,)).fetchone()
        if account is None:
            raise ValueError(f"Account {id} does not exist")
        txs = cx.execute("SELECT COUNT(*) AS n FROM transactions WHERE account_id = ?", (id,)).fetchone()
        trades = cx.execute("SELECT COUNT(*) AS n FROM trades WHERE account_id = ?", (id,)).fetchone()
        recurring = cx.execute(
            "SELECT COUNT(*) AS n FROM recurring_transactions WHERE account_id = ?", (id,)
        ).fetchone()
        tx_count = txs["n"]
        cx.execute("DELETE FROM transactions WHERE account_id = ?", (id,))
        cx.execute("DELETE FROM trades WHERE account_id = ?", (id,))
        cx.execute("DELETE FROM recurring_transactions WHERE account_id = ?", (id,))
        cx.execute("DELETE FROM accounts WHERE id = ?", (id,))
    return {
        "ok": True,
        "account": dict(account),
        "transactions_deleted": tx_count,
        "trades_deleted": trades["n"],
        "recurring_deleted": recurring["n"],
    }


@srv.tool(
    "accounts_list",
    "List accounts with their current balance (initial balance + income - expenses).",
    {"include_inactive": {"type": "boolean", "default": False}},
)
def accounts_list(include_inactive=False):
    filter_sql = "" if include_inactive else "WHERE a.active = 1"
    with db() as cx:
        data = rows(
            cx.execute(
                f"""
            SELECT a.id, a.name, a.type, a.currency, a.initial_balance, a.active,
                   ROUND(a.initial_balance
                     + COALESCE(SUM(CASE WHEN t.type='income'  THEN t.amount END),0)
                     - COALESCE(SUM(CASE WHEN t.type='expense' THEN t.amount END),0), 2) AS balance
            FROM accounts a LEFT JOIN transactions t ON t.account_id = a.id
            {filter_sql}
            GROUP BY a.id ORDER BY a.name
        """
            )
        )
    return {"accounts": data, "total_accounts": len(data)}


# ----------------------------------------------------------------- transactions


@srv.tool(
    "transaction_create",
    "Record income or an expense. The main everyday tool.",
    {
        "type": {"type": "string", "enum": ["income", "expense"]},
        "amount": {"type": "number", "description": "Always positive"},
        "category": {
            "type": "string",
            "description": "e.g. food, housing, transport, leisure, payroll, investment",
        },
        "date": {"type": "string", "description": "YYYY-MM-DD. Defaults to today."},
        "subcategory": {"type": "string"},
        "account": {"type": "string", "description": "Name of an existing account"},
        "description": {"type": "string"},
        "currency": {"type": "string"},
        "tags": {"type": "string", "description": "Comma-separated list"},
    },
    required=["type", "amount", "category"],
)
def transaction_create(
    type,
    amount,
    category,
    date=None,
    subcategory=None,
    account=None,
    description=None,
    currency=None,
    tags=None,
):
    if type not in ("income", "expense"):
        raise ValueError("type must be 'income' or 'expense'")
    if float(amount) <= 0:
        raise ValueError("amount must be positive; use 'type' to indicate the sign")

    with db() as cx:
        account_id = None
        if account:
            row = cx.execute("SELECT id FROM accounts WHERE name = ?", (account,)).fetchone()
            if row is None:
                raise ValueError(f"Account '{account}' does not exist. Create it with account_create.")
            account_id = row["id"]

        cur = cx.execute(
            """INSERT INTO transactions
               (date,type,amount,currency,category,subcategory,account_id,description,tags,created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                validate_date(date),
                type,
                round(float(amount), 2),
                (currency or BASE_CURRENCY).upper(),
                category.lower().strip(),
                subcategory,
                account_id,
                description,
                tags,
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        return {"ok": True, "id": cur.lastrowid, "type": type, "amount": round(float(amount), 2)}


@srv.tool(
    "transactions_list",
    "Query transactions with filters. Also returns totals for the matched subset.",
    {
        "date_from": {"type": "string", "description": "YYYY-MM-DD"},
        "date_to": {"type": "string", "description": "YYYY-MM-DD"},
        "type": {"type": "string", "enum": ["income", "expense"]},
        "category": {"type": "string"},
        "account": {"type": "string"},
        "search": {"type": "string", "description": "Searches description and tags"},
        "limit": {"type": "integer", "default": 100},
        "offset": {"type": "integer", "default": 0},
    },
)
def transactions_list(
    date_from=None, date_to=None, type=None, category=None, account=None,
    search=None, limit=100, offset=0,
):
    conditions, args = [], []
    if date_from:
        conditions.append("t.date >= ?")
        args.append(validate_date(date_from, False))
    if date_to:
        conditions.append("t.date <= ?")
        args.append(validate_date(date_to, False))
    if type:
        conditions.append("t.type = ?")
        args.append(type)
    if category:
        conditions.append("t.category = ?")
        args.append(category.lower())
    if account:
        conditions.append("a.name = ?")
        args.append(account)
    if search:
        conditions.append("(t.description LIKE ? OR t.tags LIKE ?)")
        args += [f"%{search}%", f"%{search}%"]

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    with db() as cx:
        total = cx.execute(
            f"""SELECT COUNT(*) AS n FROM transactions t
                LEFT JOIN accounts a ON a.id = t.account_id {where}""",
            args,
        ).fetchone()["n"]
        data = rows(
            cx.execute(
                f"""SELECT t.id, t.date, t.type, t.amount, t.currency, t.category,
                           t.subcategory, a.name AS account, t.description, t.tags
                    FROM transactions t LEFT JOIN accounts a ON a.id = t.account_id
                    {where} ORDER BY t.date DESC, t.id DESC LIMIT ? OFFSET ?""",
                args + [int(limit), int(offset)],
            )
        )
    income = round(sum(d["amount"] for d in data if d["type"] == "income"), 2)
    expenses = round(sum(d["amount"] for d in data if d["type"] == "expense"), 2)
    return {
        "transactions": data,
        "shown": len(data),
        "total": total,
        "has_more": offset + len(data) < total,
        "income": income,
        "expenses": expenses,
        "net": round(income - expenses, 2),
    }


@srv.tool(
    "transaction_update",
    "Fix fields on an existing transaction. Only the given fields are touched.",
    {
        "id": {"type": "integer"},
        "amount": {"type": "number"},
        "category": {"type": "string"},
        "date": {"type": "string"},
        "description": {"type": "string"},
        "type": {"type": "string", "enum": ["income", "expense"]},
    },
    required=["id"],
)
def transaction_update(id, amount=None, category=None, date=None, description=None, type=None):
    changes, args = [], []
    if amount is not None:
        changes.append("amount = ?")
        args.append(round(float(amount), 2))
    if category is not None:
        changes.append("category = ?")
        args.append(category.lower())
    if date is not None:
        changes.append("date = ?")
        args.append(validate_date(date, False))
    if description is not None:
        changes.append("description = ?")
        args.append(description)
    if type is not None:
        changes.append("type = ?")
        args.append(type)
    if not changes:
        raise ValueError("No field specified to modify")

    with db() as cx:
        before = cx.execute("SELECT * FROM transactions WHERE id = ?", (id,)).fetchone()
        if before is None:
            raise ValueError(f"Transaction {id} does not exist")
        cx.execute(f"UPDATE transactions SET {', '.join(changes)} WHERE id = ?", args + [id])
        after = cx.execute("SELECT * FROM transactions WHERE id = ?", (id,)).fetchone()
    return {"ok": True, "before": dict(before), "after": dict(after)}


@srv.tool(
    "transaction_delete",
    "Delete a transaction by id. Returns the deleted record in case it needs restoring.",
    {"id": {"type": "integer"}},
    required=["id"],
)
def transaction_delete(id):
    with db() as cx:
        row = cx.execute("SELECT * FROM transactions WHERE id = ?", (id,)).fetchone()
        if row is None:
            raise ValueError(f"Transaction {id} does not exist")
        cx.execute("DELETE FROM transactions WHERE id = ?", (id,))
    return {"ok": True, "deleted": dict(row)}


@srv.tool(
    "transfer_create",
    "Move money between two of your own accounts. Adjusts both balances but does NOT "
    "count as income or expense in summaries.",
    {
        "from_account": {"type": "string", "description": "Name of the source account"},
        "to_account": {"type": "string", "description": "Name of the destination account"},
        "amount": {"type": "number"},
        "date": {"type": "string"},
        "description": {"type": "string"},
        "commission": {"type": "number", "default": 0, "description": "Recorded as a real expense"},
    },
    required=["from_account", "to_account", "amount"],
)
def transfer_create(from_account, to_account, amount, date=None, description=None, commission=0):
    if from_account == to_account:
        raise ValueError("Source and destination accounts cannot be the same")
    if float(amount) <= 0:
        raise ValueError("amount must be positive")

    date = validate_date(date)
    now = datetime.now().isoformat(timespec="seconds")
    with db() as cx:
        accounts = {}
        for role, name in (("source", from_account), ("destination", to_account)):
            row = cx.execute("SELECT id, currency FROM accounts WHERE name = ?", (name,)).fetchone()
            if row is None:
                raise ValueError(f"{role.capitalize()} account '{name}' does not exist")
            accounts[role] = row

        if accounts["source"]["currency"] != accounts["destination"]["currency"]:
            raise ValueError(
                "Transfer between different currencies: record it as two separate "
                "transactions with the applied exchange rate."
            )

        currency = accounts["source"]["currency"]
        tag = f"transfer:{date}:{round(float(amount), 2)}"
        text = description or f"Transfer {from_account} -> {to_account}"

        cx.execute(
            """INSERT INTO transactions
               (date,type,amount,currency,category,account_id,description,tags,created_at)
               VALUES (?,'expense',?,?,?,?,?,?,?)""",
            (date, round(float(amount), 2), currency, TRANSFER_CATEGORY,
             accounts["source"]["id"], text, tag, now),
        )
        cx.execute(
            """INSERT INTO transactions
               (date,type,amount,currency,category,account_id,description,tags,created_at)
               VALUES (?,'income',?,?,?,?,?,?,?)""",
            (date, round(float(amount), 2), currency, TRANSFER_CATEGORY,
             accounts["destination"]["id"], text, tag, now),
        )
        if commission and float(commission) > 0:
            cx.execute(
                """INSERT INTO transactions
                   (date,type,amount,currency,category,account_id,description,created_at)
                   VALUES (?,'expense',?,?,'commissions',?,?,?)""",
                (date, round(float(commission), 2), currency, accounts["source"]["id"],
                 f"Transfer commission {from_account} -> {to_account}", now),
            )

    return {
        "ok": True,
        "date": date,
        "from_account": from_account,
        "to_account": to_account,
        "amount": round(float(amount), 2),
        "commission": round(float(commission or 0), 2),
        "note": "Excluded from income and expenses; only affects account balances.",
    }


# ------------------------------------------------------------ recurring_transactions


def _occurrences(rec, until):
    """Dates on which a recurring transaction should have occurred."""
    start = datetime.strptime(rec["start_date"], "%Y-%m-%d").date()
    end = min(until, datetime.strptime(rec["end_date"], "%Y-%m-%d").date()) if rec["end_date"] else until
    dates = []

    if rec["frequency"] == "monthly":
        year, month = start.year, start.month
        while (year, month) <= (end.year, end.month):
            day = min(int(rec["day"]), calendar.monthrange(year, month)[1])
            d = date(year, month, day)
            if start <= d <= end:
                dates.append(d.isoformat())
            month += 1
            if month > 12:
                year, month = year + 1, 1
    else:  # yearly
        month = int(rec["month"] or start.month)
        for year in range(start.year, end.year + 1):
            day = min(int(rec["day"]), calendar.monthrange(year, month)[1])
            d = date(year, month, day)
            if start <= d <= end:
                dates.append(d.isoformat())
    return dates


@srv.tool(
    "recurring_create",
    "Register a transaction that repeats (payroll, rent, subscription, insurance). "
    "Creates nothing by itself: transactions are generated with recurring_apply.",
    {
        "concept": {"type": "string", "description": "e.g. 'Payroll' or 'Apartment rent'"},
        "type": {"type": "string", "enum": ["income", "expense"]},
        "amount": {"type": "number"},
        "category": {"type": "string"},
        "frequency": {"type": "string", "enum": ["monthly", "yearly"], "default": "monthly"},
        "day": {
            "type": "integer",
            "description": "Day of month (1-31). Adjusted for short months.",
            "default": 1,
        },
        "month": {"type": "integer", "description": "Only for yearly frequency (1-12)"},
        "account": {"type": "string"},
        "start_date": {"type": "string", "description": "First valid date. Defaults to today."},
        "end_date": {"type": "string", "description": "Last valid date (optional)"},
        "currency": {"type": "string"},
    },
    required=["concept", "type", "amount", "category"],
)
def recurring_create(
    concept, type, amount, category, frequency="monthly", day=1, month=None,
    account=None, start_date=None, end_date=None, currency=None,
):
    if not 1 <= int(day) <= 31:
        raise ValueError("day must be between 1 and 31")
    if frequency == "yearly" and month is None:
        raise ValueError("Yearly frequency requires a month (1-12)")

    with db() as cx:
        account_id = None
        if account:
            row = cx.execute("SELECT id FROM accounts WHERE name = ?", (account,)).fetchone()
            if row is None:
                raise ValueError(f"Account '{account}' does not exist")
            account_id = row["id"]
        cur = cx.execute(
            """INSERT INTO recurring_transactions
               (concept,type,amount,category,account_id,frequency,day,month,currency,start_date,end_date)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                concept, type, round(float(amount), 2), category.lower().strip(), account_id,
                frequency, int(day), int(month) if month else None,
                (currency or BASE_CURRENCY).upper(),
                validate_date(start_date), validate_date(end_date, False),
            ),
        )
    return {"ok": True, "id": cur.lastrowid, "concept": concept, "frequency": frequency}


@srv.tool(
    "recurring_list",
    "List defined recurring transactions and how many times each has been generated.",
    {"include_inactive": {"type": "boolean", "default": False}},
)
def recurring_list(include_inactive=False):
    filter_sql = "" if include_inactive else "WHERE r.active = 1"
    with db() as cx:
        data = rows(
            cx.execute(
                f"""SELECT r.*, a.name AS account,
                       (SELECT COUNT(*) FROM transactions t
                        WHERE t.tags LIKE 'recurring:' || r.id || ':%') AS generated_count
                    FROM recurring_transactions r LEFT JOIN accounts a ON a.id = r.account_id
                    {filter_sql} ORDER BY r.type DESC, r.amount DESC"""
            )
        )
    monthly_income = sum(
        d["amount"] for d in data if d["type"] == "income" and d["frequency"] == "monthly"
    )
    monthly_expenses = sum(
        d["amount"] for d in data if d["type"] == "expense" and d["frequency"] == "monthly"
    )
    return {
        "recurring": data,
        "fixed_monthly_commitment": round(monthly_expenses, 2),
        "fixed_monthly_income": round(monthly_income, 2),
        "fixed_monthly_margin": round(monthly_income - monthly_expenses, 2),
    }


@srv.tool(
    "recurring_apply",
    "Generate the pending transactions for every recurring rule up to a date. It's "
    "idempotent: it never duplicates a transaction already generated. Simulates by default.",
    {
        "end_date": {"type": "string", "description": "YYYY-MM-DD. Defaults to today."},
        "simulate": {"type": "boolean", "default": True},
    },
)
def recurring_apply(end_date=None, simulate=True):
    limit_date = datetime.strptime(validate_date(end_date), "%Y-%m-%d").date()
    now = datetime.now().isoformat(timespec="seconds")
    pending = []

    with db() as cx:
        for rec in rows(cx.execute("SELECT * FROM recurring_transactions WHERE active = 1")):
            existing = {
                r["date"]
                for r in cx.execute(
                    "SELECT date FROM transactions WHERE tags LIKE ?",
                    (f"recurring:{rec['id']}:%",),
                ).fetchall()
            }
            for d in _occurrences(rec, limit_date):
                if d not in existing:
                    pending.append({
                        "recurring_id": rec["id"], "concept": rec["concept"], "date": d,
                        "type": rec["type"], "amount": rec["amount"],
                        "category": rec["category"], "account_id": rec["account_id"],
                        "currency": rec["currency"],
                    })

        if not simulate and pending:
            cx.executemany(
                """INSERT INTO transactions
                   (date,type,amount,currency,category,account_id,description,tags,created_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                [
                    (p["date"], p["type"], p["amount"], p["currency"], p["category"],
                     p["account_id"], p["concept"],
                     f"recurring:{p['recurring_id']}:{p['date']}", now)
                    for p in pending
                ],
            )

    total = round(
        sum(p["amount"] if p["type"] == "income" else -p["amount"] for p in pending), 2
    )
    return {
        "simulation": bool(simulate),
        "pending": len(pending),
        "net_effect": total,
        "detail": pending[:40],
        "next_step": (
            "Call again with simulate=false to record them."
            if simulate and pending
            else None
        ),
    }


@srv.tool(
    "recurring_deactivate",
    "Deactivate a recurring rule so it stops generating transactions. Does not delete history.",
    {"id": {"type": "integer"}},
    required=["id"],
)
def recurring_deactivate(id):
    with db() as cx:
        row = cx.execute("SELECT * FROM recurring_transactions WHERE id = ?", (id,)).fetchone()
        if row is None:
            raise ValueError(f"Recurring rule {id} does not exist")
        cx.execute("UPDATE recurring_transactions SET active = 0 WHERE id = ?", (id,))
    return {"ok": True, "deactivated": dict(row)}


# --------------------------------------------------------------------- analysis


@srv.tool(
    "period_summary",
    "Summary of income, expenses, savings and category breakdown for a period.",
    {
        "date_from": {"type": "string", "description": "YYYY-MM-DD"},
        "date_to": {"type": "string", "description": "YYYY-MM-DD"},
        "month": {"type": "string", "description": "YYYY-MM shortcut; ignores date_from/date_to"},
    },
)
def period_summary(date_from=None, date_to=None, month=None):
    if month:
        if len(month) != 7 or month[4] != "-":
            raise ValueError("month must be in YYYY-MM format")
        date_from, date_to = f"{month}-01", f"{month}-31"
    if not date_from:
        date_from = date.today().replace(day=1).isoformat()
    if not date_to:
        date_to = date.today().isoformat()

    with db() as cx:
        totals = cx.execute(
            """SELECT
                 COALESCE(SUM(CASE WHEN type='income'  THEN amount END),0) AS income,
                 COALESCE(SUM(CASE WHEN type='expense' THEN amount END),0) AS expenses,
                 COUNT(*) AS n
               FROM transactions WHERE date BETWEEN ? AND ? AND category <> ?""",
            (date_from, date_to, TRANSFER_CATEGORY),
        ).fetchone()
        by_category = rows(
            cx.execute(
                """SELECT category, type, ROUND(SUM(amount),2) AS total, COUNT(*) AS n
                   FROM transactions WHERE date BETWEEN ? AND ? AND category <> ?
                   GROUP BY category, type ORDER BY total DESC""",
                (date_from, date_to, TRANSFER_CATEGORY),
            )
        )

    income = round(totals["income"], 2)
    expenses = round(totals["expenses"], 2)
    savings = round(income - expenses, 2)
    return {
        "period": {"date_from": date_from, "date_to": date_to},
        "income": income,
        "expenses": expenses,
        "savings": savings,
        "savings_rate_pct": round(100 * savings / income, 1) if income else None,
        "transaction_count": totals["n"],
        "by_category": by_category,
    }


@srv.tool(
    "monthly_evolution",
    "Monthly series of income, expenses and savings. Useful for spotting trends.",
    {"months": {"type": "integer", "default": 12, "description": "Last N months"}},
)
def monthly_evolution(months=12):
    with db() as cx:
        data = rows(
            cx.execute(
                """SELECT substr(date,1,7) AS month,
                          ROUND(COALESCE(SUM(CASE WHEN type='income'  THEN amount END),0),2) AS income,
                          ROUND(COALESCE(SUM(CASE WHEN type='expense' THEN amount END),0),2) AS expenses
                   FROM transactions WHERE category <> ?
                   GROUP BY month ORDER BY month DESC LIMIT ?""",
                (TRANSFER_CATEGORY, int(months)),
            )
        )
    for d in data:
        d["savings"] = round(d["income"] - d["expenses"], 2)
    return {"series": list(reversed(data))}


@srv.tool(
    "budget_set",
    "Set a monthly spending cap for a category.",
    {
        "category": {"type": "string"},
        "limit_amount": {"type": "number"},
        "currency": {"type": "string"},
    },
    required=["category", "limit_amount"],
)
def budget_set(category, limit_amount, currency=None):
    with db() as cx:
        cx.execute(
            """INSERT INTO budgets (category, limit_amount, currency, period) VALUES (?,?,?,'monthly')
               ON CONFLICT(category, period) DO UPDATE SET limit_amount = excluded.limit_amount""",
            (category.lower(), float(limit_amount), (currency or BASE_CURRENCY).upper()),
        )
    return {"ok": True, "category": category.lower(), "limit_amount": float(limit_amount)}


@srv.tool(
    "budget_status",
    "Compare actual spending this month against the defined budgets.",
    {"month": {"type": "string", "description": "YYYY-MM. Defaults to the current month."}},
)
def budget_status(month=None):
    month = month or date.today().strftime("%Y-%m")
    today = date.today().isoformat()

    # A future-dated expense is recorded but hasn't happened yet. Counting it as
    # spent would misalign this view against period_summary, which only goes up
    # to today. Keep it separate under 'committed_future' instead of mixing or
    # silently discarding it.
    with db() as cx:
        budgets = rows(cx.execute("SELECT category, limit_amount, currency FROM budgets"))
        txs = rows(
            cx.execute(
                """SELECT category,
                          ROUND(SUM(CASE WHEN date <= ? THEN amount ELSE 0 END),2) AS spent,
                          ROUND(SUM(CASE WHEN date >  ? THEN amount ELSE 0 END),2) AS future
                   FROM transactions
                   WHERE type='expense' AND substr(date,1,7) = ? AND category <> ?
                   GROUP BY category""",
                (today, today, month, TRANSFER_CATEGORY),
            )
        )
    spent = {t["category"]: t["spent"] for t in txs}
    future = {t["category"]: t["future"] for t in txs}

    lines = []
    for b in budgets:
        real = spent.get(b["category"], 0.0)
        pending = future.get(b["category"], 0.0)
        projected = round(real + pending, 2)
        lines.append(
            {
                "category": b["category"],
                "limit_amount": b["limit_amount"],
                "spent": real,
                "committed_future": pending,
                "projected_month_end": projected,
                "remaining": round(b["limit_amount"] - real, 2),
                "consumed_pct": round(100 * real / b["limit_amount"], 1),
                "status": (
                    "EXCEEDED" if real > b["limit_amount"]
                    else "WILL_EXCEED" if projected > b["limit_amount"]
                    else "ok"
                ),
            }
        )
    no_budget = sorted(set(spent) - {b["category"] for b in budgets})
    return {
        "month": month,
        "date_to": today if month == date.today().strftime("%Y-%m") else f"{month}-31",
        "budgets": lines,
        "categories_without_budget": no_budget,
        "note": (
            "'spent' goes up to today, same as period_summary. Later-dated entries "
            "are counted separately under 'committed_future'."
        ),
    }


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
        "account": {"type": "string", "description": "Name of the broker account"},
        "note": {"type": "string", "description": "Reason for the trade"},
    },
    required=["ticker", "action", "quantity", "price"],
)
def trade_create(
    ticker,
    action,
    quantity,
    price,
    date=None,
    commission=0,
    currency=None,
    account=None,
    note=None,
):
    if action not in ("buy", "sell"):
        raise ValueError("action must be 'buy' or 'sell'")
    ticker = ticker.upper().strip()

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

        account_id = None
        if account:
            row = cx.execute("SELECT id FROM accounts WHERE name = ?", (account,)).fetchone()
            if row is None:
                raise ValueError(f"Account '{account}' does not exist")
            account_id = row["id"]

        cur = cx.execute(
            """INSERT INTO trades
               (date,ticker,action,quantity,price,commission,currency,account_id,note)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                validate_date(date),
                ticker,
                action,
                float(quantity),
                float(price),
                float(commission or 0),
                (currency or BASE_CURRENCY).upper(),
                account_id,
                note,
            ),
        )
    return {
        "ok": True,
        "id": cur.lastrowid,
        "ticker": ticker,
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
            },
        )
        p["trades"] += 1
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


# ------------------------------------------------------------- import / SQL


@srv.tool(
    "csv_import",
    "Import transactions from a CSV (e.g. a bank export). Accepted headers: "
    "date, amount, concept/description, category. Negative amounts = expense.",
    {
        "path": {"type": "string", "description": "Absolute path to the CSV file"},
        "account": {"type": "string", "description": "Account to assign everything to"},
        "delimiter": {"type": "string", "default": ";"},
        "simulate": {
            "type": "boolean",
            "default": True,
            "description": "If true, only shows what would be imported without writing anything",
        },
    },
    required=["path"],
)
def csv_import(path, account=None, delimiter=";", simulate=True):
    if not os.path.isfile(path):
        raise ValueError(f"File not found: {path}")

    date_aliases = ("fecha", "date", "fecha operacion", "fecha_operacion", "f. valor")
    amount_aliases = ("importe", "amount", "cantidad", "monto")
    desc_aliases = ("concepto", "descripcion", "description", "detalle", "movimiento")
    category_aliases = ("categoria", "category")

    def find(row, aliases):
        for k, v in row.items():
            if k and k.strip().lower() in aliases:
                return v
        return None

    prepared, errors = [], []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for n, row in enumerate(csv.DictReader(fh, delimiter=delimiter), start=2):
            raw_amount = find(row, amount_aliases)
            raw_date = find(row, date_aliases)
            if not raw_amount or not raw_date:
                errors.append({"line": n, "reason": "missing date or amount"})
                continue
            try:
                amount = float(str(raw_amount).replace(".", "").replace(",", ".").replace(" ", ""))
            except ValueError:
                errors.append({"line": n, "reason": f"unreadable amount '{raw_amount}'"})
                continue
            try:
                norm_date = validate_date(raw_date.strip()[:10], False)
            except ValueError:
                try:
                    norm_date = datetime.strptime(raw_date.strip()[:10], "%d/%m/%Y").date().isoformat()
                except ValueError:
                    errors.append({"line": n, "reason": f"unreadable date '{raw_date}'"})
                    continue
            prepared.append(
                {
                    "date": norm_date,
                    "type": "expense" if amount < 0 else "income",
                    "amount": round(abs(amount), 2),
                    "category": (find(row, category_aliases) or "uncategorized").lower(),
                    "description": find(row, desc_aliases),
                }
            )

    if simulate:
        return {
            "simulation": True,
            "would_import": len(prepared),
            "errors": errors,
            "sample": prepared[:10],
            "next_step": "Call again with simulate=false to write to the database.",
        }

    with db() as cx:
        account_id = None
        if account:
            row = cx.execute("SELECT id FROM accounts WHERE name = ?", (account,)).fetchone()
            if row is None:
                raise ValueError(f"Account '{account}' does not exist")
            account_id = row["id"]
        now = datetime.now().isoformat(timespec="seconds")
        cx.executemany(
            """INSERT INTO transactions
               (date,type,amount,currency,category,account_id,description,created_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            [
                (
                    p["date"],
                    p["type"],
                    p["amount"],
                    BASE_CURRENCY,
                    p["category"],
                    account_id,
                    p["description"],
                    now,
                )
                for p in prepared
            ],
        )
    return {"ok": True, "imported": len(prepared), "errors": errors}


@srv.tool(
    "sql_query",
    "Run a read-only SELECT query against the database, for analysis not covered by "
    "the other tools. Tables: accounts, transactions, budgets, recurring_transactions, "
    "trades, watchlist.",
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
