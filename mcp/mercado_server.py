"""MCP server 'market': quotes, fundamentals, news and valuation.

API-key-free data sources:
  - Yahoo Finance (chart v8, quoteSummary v10, search v1)  -> prices & fundamentals
  - Yahoo Finance RSS and Google News RSS                  -> news
  - Stooq CSV                                              -> price fallback

Optional keys via environment variables (improve reliability if Yahoo fails):
  FINNHUB_API_KEY, FMP_API_KEY
"""

import http.cookiejar
import json
import math
import os
import statistics
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import scoring
from mcp_common import MCPServer, log

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
TIMEOUT = 20
BASE_CURRENCY = os.environ.get("SEALOCK_CURRENCY", "EUR")

_cookie_jar = http.cookiejar.CookieJar()
_opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_cookie_jar))
_crumb_cache = {"value": None, "ts": 0}
_market_state_cache = {}  # ticker -> (state, delay_data_min, ts)

# The chart v8 endpoint (used for price) never includes marketState, with any
# combination of range/interval: tested range 1d/2d/5d/1mo and interval
# 1m/5m/15m/1d, always absent. Only quoteSummary/price has it. Cached 180s
# because market state only changes at open/close, not every 60s UI refresh.
MARKET_STATES = {
    "REGULAR": "OPEN",
    "PRE": "PRE_MARKET",
    "PREPRE": "PRE_MARKET",
    "POST": "POST_MARKET",
    "POSTPOST": "POST_MARKET",
    "CLOSED": "CLOSED",
}


def _http_get(url, headers=None, binary=False):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    with _opener.open(request, timeout=TIMEOUT) as resp:
        data = resp.read()
    return data if binary else data.decode("utf-8", errors="replace")


def _get_json(url, headers=None):
    return json.loads(_http_get(url, headers))


def _crumb():
    """Obtain (and cache for 1h) the crumb that Yahoo requires for quoteSummary."""
    if _crumb_cache["value"] and time.time() - _crumb_cache["ts"] < 3600:
        return _crumb_cache["value"]
    try:
        _http_get("https://fc.yahoo.com/")
    except Exception:  # noqa: BLE001 - only needs to leave cookies
        pass
    try:
        value = _http_get("https://query1.finance.yahoo.com/v1/test/getcrumb").strip()
        if value and len(value) < 40:
            _crumb_cache.update(value=value, ts=time.time())
            return value
    except Exception as exc:  # noqa: BLE001
        log(f"[market] could not obtain crumb: {exc}")
    return None


def _num(value):
    """Yahoo sometimes returns numbers, other times {'raw': n, 'fmt': '...'."""
    if isinstance(value, dict):
        return value.get("raw")
    return value


def _market_state(ticker):
    """(state label, data delay minutes) for a ticker, via quoteSummary/price.

    Cached 180s per ticker: state only changes at session open/close, no need
    to query on every 60s UI refresh.
    """
    cache = _market_state_cache.get(ticker)
    if cache and time.time() - cache[2] < 180:
        return cache[0], cache[1]

    state, delay = None, None
    try:
        url = f"https://query1.finance.yahoo.com/v10/finance/quoteSummary/{urllib.parse.quote(ticker)}?modules=price"
        crumb = _crumb()
        if crumb:
            url += f"&crumb={urllib.parse.quote(crumb)}"
        r = _get_json(url)["quoteSummary"]["result"][0]["price"]
        raw_state = r.get("marketState")
        state = MARKET_STATES.get(raw_state, raw_state)
        delay = _num(r.get("exchangeDataDelayedBy"))
    except Exception as exc:  # noqa: BLE001 - unknown state doesn't block the quote
        log(f"[market] market state unavailable for {ticker}: {exc}")

    _market_state_cache[ticker] = (state, delay, time.time())
    return state, delay


def _pct(a, b):
    if a is None or b in (None, 0):
        return None
    return round(100 * (a - b) / b, 2)


def _fill_derived(f):
    """Fill Yahoo gaps with exact accounting identities.

    For many European stocks (ITX.MC, SAN.MC...) Yahoo doesn't publish marketCap
    or sharesOutstanding, but does publish enterpriseValue. Since EV = market cap
    + net debt, we derive market cap and, with price, the number of shares.

    Do NOT use floatShares as a substitute for total shares: it's only free float
    and in companies with a controlling shareholder it underestimates enormously
    (Inditex: 1,108M float vs 3,116M total).
    """
    derived = []
    price = f.get("current_price")
    ev = f["valuation"].get("enterprise_value")
    net_debt = f["financial_strength"].get("net_debt")

    if f.get("market_cap") is None and ev is not None and net_debt is not None:
        f["market_cap"] = ev - net_debt
        derived.append("market_cap = enterprise_value - net_debt")

    if f["per_share"].get("shares_outstanding") is None and f.get("market_cap") and price:
        f["per_share"]["shares_outstanding"] = round(f["market_cap"] / price)
        derived.append("shares_outstanding = market_cap / price")

    shares = f["per_share"].get("shares_outstanding")
    eps = f["per_share"].get("eps_trailing")
    if f["cash_flow"].get("net_income") is None and shares and eps:
        f["cash_flow"]["net_income"] = round(shares * eps)
        derived.append("net_income = shares_outstanding * eps_trailing")

    f["derived_fields"] = derived or None
    if derived:
        f["derived_notice"] = (
            "Yahoo didn't publish these fields for this stock; they were computed via "
            "accounting identity from the ones it did publish. Cross-check before valuing."
        )
    return f


srv = MCPServer(
    "market",
    "1.0.0",
    instructions=(
        "Market data and valuation. Prices may be delayed ~15 min. "
        "Fundamentals come from public sources and may contain errors: "
        "verify before deciding. No output is investment advice."
    ),
)

# ----------------------------------------------------------------------- quote


def _yahoo_quote(ticker, with_state=False):
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(ticker)}"
        "?range=5d&interval=1d"
    )
    data = _get_json(url)
    result = (data.get("chart") or {}).get("result")
    if not result:
        raise ValueError(f"No data for '{ticker}'")
    meta = result[0]["meta"]
    price = meta.get("regularMarketPrice")
    prev_close = meta.get("chartPreviousClose") or meta.get("previousClose")

    # regularMarketTime = real time of the price according to Yahoo. NOT the time
    # we queried: without this field, a price from 2 days ago (market closed on
    # weekend) would appear as if it were from right now.
    timestamp = meta.get("regularMarketTime")
    price_at, age_seconds = None, None
    if timestamp:
        moment = datetime.fromtimestamp(timestamp, timezone.utc)
        price_at = moment.isoformat(timespec="seconds")
        age_seconds = round((datetime.now(timezone.utc) - moment).total_seconds())

    # meta.marketState from chart v8 is always absent (tested with range 1d/2d/5d/
    # 1mo x interval 1m/5m/15m/1d): only quoteSummary/price really has it. Fetched
    # separately and only when with_state=True, to avoid duplicating requests in
    # internal calls (market polling, exchange rate) that don't need it.
    state, delay_data_min = (None, None)
    if with_state:
        state, delay_data_min = _market_state(ticker)

    return {
        "ticker": meta.get("symbol", ticker),
        "name": meta.get("longName") or meta.get("shortName"),
        "price": price,
        "currency": meta.get("currency"),
        "previous_close": prev_close,
        "change_pct": _pct(price, prev_close),
        "high_52w": meta.get("fiftyTwoWeekHigh"),
        "low_52w": meta.get("fiftyTwoWeekLow"),
        "dist_from_high_52w_pct": _pct(price, meta.get("fiftyTwoWeekHigh")),
        "market": meta.get("fullExchangeName") or meta.get("exchangeName"),
        "status": state,
        "data_delay_min": delay_data_min,
        "price_as_of": price_at,
        "age_seconds": age_seconds,
        "source": "yahoo",
    }


def _stooq_quote(ticker):
    symbol = ticker.lower()
    if "." not in symbol:
        symbol += ".us"
    text = _http_get(f"https://stooq.com/q/l/?s={urllib.parse.quote(symbol)}&f=sd2t2ohlcv&h&e=csv")
    lines = [l for l in text.strip().splitlines() if l]
    if len(lines) < 2:
        raise ValueError("Stooq returned no data")
    fields = dict(zip(lines[0].split(","), lines[1].split(",")))
    if fields.get("Close") in (None, "N/D"):
        raise ValueError("Stooq has no quote for that symbol")
    # Stooq gives date and time of the data, but not the timezone: cannot compute a
    # reliable age in seconds like with Yahoo. Expose raw text instead of inventing
    # a figure that appears more precise than it is.
    datetime_str = None
    if fields.get("Date") not in (None, "N/D") and fields.get("Time") not in (None, "N/D"):
        datetime_str = f"{fields['Date']} {fields['Time']} (market timezone, unconfirmed)"

    return {
        "ticker": ticker.upper(),
        "name": None,
        "price": float(fields["Close"]),
        "currency": None,
        "previous_close": float(fields["Open"]) if fields.get("Open") not in (None, "N/D") else None,
        "price_as_of": datetime_str,
        "age_seconds": None,
        "source": "stooq (fallback)",
    }


@srv.tool(
    "quote",
    "Current price of one or more tickers, with day change and 52-week range.",
    {
        "tickers": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Yahoo symbols: AAPL, MSFT, ITX.MC, SAN.MC, VWCE.DE",
        }
    },
    required=["tickers"],
)
def quote(tickers):
    if isinstance(tickers, str):
        tickers = [tickers]
    output, failures = [], []
    for t in tickers[:25]:
        t = t.strip().upper()
        try:
            output.append(_yahoo_quote(t, with_state=True))
        except Exception as exc:  # noqa: BLE001
            try:
                output.append(_stooq_quote(t))
            except Exception as exc2:  # noqa: BLE001
                failures.append({"ticker": t, "error": f"{exc} / fallback: {exc2}"})
    return {
        "quotes": output,
        "failures": failures,
        "queried_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "notice": "Prices may be delayed ~15 min.",
    }


# Primary markets, in preference order. Yahoo often returns secondary quotes
# first (Warsaw, Hamburg, Tradegate) with very low liquidity.
PRIMARY_SUFFIXES = [
    "",      # USA (no suffix)
    ".MC",   # Madrid
    ".PA",   # Paris
    ".DE",   # Xetra
    ".AS",   # Amsterdam
    ".MI",   # Milan
    ".L",    # London
    ".SW",   # Switzerland
    ".BR",   # Brussels
    ".LS",   # Lisbon
    ".CO",   # Copenhagen
    ".ST",   # Stockholm
    ".HE",   # Helsinki
    ".OL",   # Oslo
    ".VI",   # Vienna
    ".IR",   # Dublin
]


# Yahoo OTC market codes. 'OID' is used for unsponsored ADRs (Iberdrola trades
# there as IBDSF) and doesn't contain the string "otc", so they must be
# enumerated explicitly in addition to substring matching.
OTC_CODES = {"pnk", "oid", "obb", "oqb", "oqx", "otc", "otcm", "yhd", "pinx"}


def _is_otc(market):
    """Pink sheets and grey markets: trade the same company with a fraction of the
    liquidity and enormous spreads. Never the default suggestion."""
    m = (market or "").strip().lower()
    if not m:
        return False
    return m in OTC_CODES or any(x in m for x in ("otc", "pink", "grey", "gray"))


def _market_rank(ticker, market=None):
    if _is_otc(market):
        return 98
    suffix = "." + ticker.split(".")[-1] if "." in ticker else ""
    return PRIMARY_SUFFIXES.index(suffix) if suffix in PRIMARY_SUFFIXES else 99


# Words that don't identify a company (legal forms, articles, generics). Kept
# bilingual since Yahoo returns company names in whichever language/market they
# were registered in (Spanish companies especially).
STOP_WORDS = {
    "sa", "sab", "inc", "corp", "corporation", "plc", "nv", "ag", "se", "spa", "srl",
    "ltd", "limited", "co", "company", "group", "grupo", "holding", "holdings",
    "the", "and", "del", "las", "los", "una", "sus", "por", "con", "cia", "compania",
}


def _tokens(text):
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


def _symbol_name(ticker, quote=None):
    """Name of a quote. The Yahoo chart intermittently omits it,
    so the search endpoint is used, which returns it reliably."""
    if quote and quote.get("name"):
        return quote["name"]
    try:
        data = _get_json(
            "https://query1.finance.yahoo.com/v1/finance/search?q="
            f"{urllib.parse.quote(ticker)}&quotesCount=5&newsCount=0"
        )
        for q in data.get("quotes", []):
            if (q.get("symbol") or "").upper() == ticker.upper():
                return q.get("longname") or q.get("shortname")
    except Exception as exc:  # noqa: BLE001
        log(f"[market] could not resolve name for {ticker}: {exc}")
    return None


@srv.tool(
    "ticker_search",
    "Find the correct symbol from a company name. Sorts by primary market and "
    "verifies the primary quote exists even if Yahoo doesn't list it.",
    {
        "query": {"type": "string", "description": "e.g. 'Inditex' or 'Nvidia'"},
        "verify_primary": {
            "type": "boolean",
            "default": True,
            "description": "Probes the primary markets if the search only returns secondary ones",
        },
    },
    required=["query"],
)
def ticker_search(query, verify_primary=True):
    url = (
        "https://query1.finance.yahoo.com/v1/finance/search?q="
        f"{urllib.parse.quote(query)}&quotesCount=10&newsCount=0"
    )

    quotes = []
    for attempt in range(3):  # Yahoo search intermittently returns empty
        try:
            quotes = _get_json(url).get("quotes", [])
        except Exception as exc:  # noqa: BLE001
            log(f"[market] search failed (attempt {attempt + 1}): {exc}")
        if quotes:
            break
        time.sleep(0.4)

    results = [
        {
            "ticker": q["symbol"],
            "name": q.get("longname") or q.get("shortname"),
            "type": q.get("quoteType"),
            "market": q.get("exchDisp"),
            "sector": q.get("sector"),
        }
        for q in quotes
        if q.get("symbol")
    ]

    # Yahoo frequently omits the primary quote (searching "Inditex" returns
    # Warsaw, Milan and Hamburg, but never ITX.MC). The primary market probe
    # fetches it and places it first, verifying name identity to avoid
    # including a different company reusing those same letters.
    added = []
    if verify_primary and results:
        known = {r["ticker"] for r in results}
        roots = sorted(
            {r["ticker"].split(".")[0].lstrip("0123456789") for r in results[:6] if r["ticker"]},
            key=len,
        )
        expected = _tokens(query) | _tokens(results[0].get("name"))

        for root in roots[:2]:
            if not root:
                continue
            for suffix in PRIMARY_SUFFIXES[:8]:
                candidate = root + suffix
                if candidate in known:
                    continue
                try:
                    q = _yahoo_quote(candidate)
                except Exception:  # noqa: BLE001 - nonexistent symbol
                    continue
                if not q.get("price"):
                    continue
                name = _symbol_name(candidate, q)
                # Without name, identity cannot be verified: skip.
                if not (_tokens(name) & expected):
                    continue
                added.append({
                    "ticker": q["ticker"],
                    "name": name,
                    "type": "EQUITY",
                    "market": q.get("market"),
                    "sector": None,
                    "found_via": "primary market probe (Yahoo didn't list it)",
                })
                known.add(candidate)
                break
            if added:
                break

    results = sorted(
        added + results, key=lambda r: _market_rank(r["ticker"], r.get("market"))
    )
    for r in results:
        if _is_otc(r.get("market")):
            r["warning"] = "OTC market: low liquidity, avoid unless it's the only option"

    # The same company often trades on multiple exchanges. Exposed so it's
    # clear you need to choose, instead of silently assuming the first.
    equities = [r for r in results if r.get("type") == "EQUITY" and not _is_otc(r.get("market"))]
    markets = []
    for r in equities[:5]:
        if r.get("market") and r["market"] not in [m["market"] for m in markets]:
            markets.append({"ticker": r["ticker"], "market": r["market"]})

    return {
        "query": query,
        "results": results,
        "suggested": results[0]["ticker"] if results else None,
        "available_markets": markets if len(markets) > 1 else None,
        "notice": (
            "Sorted by primary market, OTC downranked. If the company trades on several "
            "markets, the primary one is usually its home country's: confirm it before "
            "using the symbol, since price and currency change with the market."
        ),
    }


# ------------------------------------------------------------------- historical


@srv.tool(
    "historical",
    "Historical series with derived metrics: return, annualized volatility, max "
    "drawdown, moving averages and RSI.",
    {
        "ticker": {"type": "string"},
        "date_range": {
            "type": "string",
            "description": "1mo, 3mo, 6mo, 1y, 2y, 5y, 10y, max",
            "default": "1y",
        },
        "interval": {"type": "string", "description": "1d, 1wk, 1mo", "default": "1d"},
        "include_series": {
            "type": "boolean",
            "default": False,
            "description": "If true, also returns the last 60 closing prices",
        },
    },
    required=["ticker"],
)
def historical(ticker, date_range="1y", interval="1d", include_series=False):
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(ticker.upper())}"
        f"?range={urllib.parse.quote(date_range)}&interval={urllib.parse.quote(interval)}"
    )
    data = _get_json(url)
    result = (data.get("chart") or {}).get("result")
    if not result:
        raise ValueError(f"No historical data for '{ticker}'")
    res = result[0]
    timestamps = res.get("timestamp") or []
    raw_closes = res["indicators"]["quote"][0].get("close") or []

    series = [
        (datetime.fromtimestamp(t, timezone.utc).date().isoformat(), c)
        for t, c in zip(timestamps, raw_closes)
        if c is not None
    ]
    if len(series) < 3:
        raise ValueError("Insufficient historical data to calculate metrics")

    closes = [c for _, c in series]
    returns = [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes))]
    periods_per_year = {"1d": 252, "1wk": 52, "1mo": 12}.get(interval, 252)
    volatility = (
        statistics.pstdev(returns) * math.sqrt(periods_per_year) * 100 if len(returns) > 1 else None
    )

    peak, max_dd = closes[0], 0.0
    for c in closes:
        peak = max(peak, c)
        max_dd = min(max_dd, c / peak - 1)

    def sma(n):
        return round(sum(closes[-n:]) / n, 4) if len(closes) >= n else None

    rsi = None
    if len(returns) >= 14:
        window = returns[-14:]
        gains = [r for r in window if r > 0]
        losses = [-r for r in window if r < 0]
        avg_g = sum(gains) / 14
        avg_p = sum(losses) / 14
        rsi = 100.0 if avg_p == 0 else round(100 - 100 / (1 + avg_g / avg_p), 1)

    output = {
        "ticker": ticker.upper(),
        "range": date_range,
        "interval": interval,
        "observations": len(series),
        "date_from": series[0][0],
        "date_to": series[-1][0],
        "start_price": round(closes[0], 4),
        "end_price": round(closes[-1], 4),
        "period_return_pct": round(100 * (closes[-1] / closes[0] - 1), 2),
        "annualized_volatility_pct": round(volatility, 2) if volatility else None,
        "max_drawdown_pct": round(100 * max_dd, 2),
        "sma_50": sma(50),
        "sma_200": sma(200),
        "rsi_14": rsi,
        "period_high": round(max(closes), 4),
        "period_low": round(min(closes), 4),
    }
    if include_series:
        output["recent_closes"] = [
            {"date": f, "close": round(c, 4)} for f, c in series[-60:]
        ]
    return output


# ------------------------------------------------------------------ fundamentals

YAHOO_MODULES = ",".join(
    [
        "price",
        "summaryDetail",
        "defaultKeyStatistics",
        "financialData",
        "summaryProfile",
    ]
)


@srv.tool(
    "fundamentals",
    "Fundamental ratios and data: P/E, P/B, margins, ROE, debt, free cash flow, "
    "dividend and growth. Basis for valuing the company.",
    {"ticker": {"type": "string"}},
    required=["ticker"],
)
def fundamentals(ticker):
    ticker = ticker.upper().strip()
    crumb = _crumb()
    url = (
        f"https://query1.finance.yahoo.com/v10/finance/quoteSummary/{urllib.parse.quote(ticker)}"
        f"?modules={YAHOO_MODULES}"
    )
    if crumb:
        url += f"&crumb={urllib.parse.quote(crumb)}"

    try:
        data = _get_json(url)
    except Exception as exc:  # noqa: BLE001
        return {
            "ticker": ticker,
            "error": f"Yahoo rejected the request ({exc}).",
            "suggestion": (
                "Set FINNHUB_API_KEY or FMP_API_KEY in .mcp.json for an alternative source, "
                "or enter the fundamentals by hand into dcf_valuation."
            ),
        }

    result = (data.get("quoteSummary") or {}).get("result")
    if not result:
        return {"ticker": ticker, "error": "Yahoo returned no fundamentals for that symbol."}

    m = result[0]
    price = m.get("price", {})
    detail = m.get("summaryDetail", {})
    stats = m.get("defaultKeyStatistics", {})
    fin = m.get("financialData", {})
    profile = m.get("summaryProfile", {})

    def g(d, field):
        return _num(d.get(field))

    def pct_val(d, field):
        v = g(d, field)
        return round(v * 100, 2) if isinstance(v, (int, float)) else None

    return _fill_derived({
        "ticker": ticker,
        "name": price.get("longName") or price.get("shortName"),
        "sector": profile.get("sector"),
        "industry": profile.get("industry"),
        "country": profile.get("country"),
        "employees": profile.get("fullTimeEmployees"),
        "currency": price.get("currency"),
        "current_price": g(price, "regularMarketPrice"),
        "market_cap": g(price, "marketCap"),
        "valuation": {
            "per_trailing": g(detail, "trailingPE"),
            "per_forward": g(detail, "forwardPE"),
            "peg": g(stats, "pegRatio"),
            "price_to_book": g(stats, "priceToBook"),
            "price_to_sales": g(stats, "priceToSalesTrailing12Months")
            or g(detail, "priceToSalesTrailing12Months"),
            "ev_ebitda": g(stats, "enterpriseToEbitda"),
            "ev_to_sales": g(stats, "enterpriseToRevenue"),
            "enterprise_value": g(stats, "enterpriseValue"),
        },
        "profitability": {
            "roe_pct": pct_val(fin, "returnOnEquity"),
            "roa_pct": pct_val(fin, "returnOnAssets"),
            "gross_margin_pct": pct_val(fin, "grossMargins"),
            "operating_margin_pct": pct_val(fin, "operatingMargins"),
            "net_margin_pct": pct_val(fin, "profitMargins"),
        },
        "growth": {
            "revenue_yoy_pct": pct_val(fin, "revenueGrowth"),
            "earnings_yoy_pct": pct_val(fin, "earningsGrowth"),
        },
        "financial_strength": {
            "total_cash": g(fin, "totalCash"),
            "total_debt": g(fin, "totalDebt"),
            "net_debt": (
                g(fin, "totalDebt") - g(fin, "totalCash")
                if g(fin, "totalDebt") is not None and g(fin, "totalCash") is not None
                else None
            ),
            "debt_to_equity": g(fin, "debtToEquity"),
            "current_ratio": g(fin, "currentRatio"),
        },
        "cash_flow": {
            "free_cash_flow": g(fin, "freeCashflow"),
            "operating_cash_flow": g(fin, "operatingCashflow"),
            "ebitda": g(fin, "ebitda"),
            "revenue_ttm": g(fin, "totalRevenue"),
            "net_income": g(stats, "netIncomeToCommon"),
        },
        "per_share": {
            "eps_trailing": g(stats, "trailingEps"),
            "eps_forward": g(stats, "forwardEps"),
            "book_value_per_share": g(stats, "bookValue"),
            "shares_outstanding": g(stats, "sharesOutstanding")
            or g(stats, "impliedSharesOutstanding"),
        },
        "dividend": {
            "yield_pct": pct_val(detail, "dividendYield"),
            "payout_pct": pct_val(stats, "payoutRatio"),
            "annual_dividend": g(detail, "dividendRate"),
        },
        "risk": {"beta": g(stats, "beta") or g(detail, "beta")},
        "analyst_consensus": {
            "mean_target_price": g(fin, "targetMeanPrice"),
            "recommendation": fin.get("recommendationKey"),
            "num_analysts": g(fin, "numberOfAnalystOpinions"),
        },
        "source": "Yahoo Finance",
        "notice": "Public data with no guarantee. Cross-check against the company's official reports.",
    })


@srv.tool(
    "compare",
    "Compare several tickers side by side on key valuation and quality metrics.",
    {
        "tickers": {"type": "array", "items": {"type": "string"}},
    },
    required=["tickers"],
)
def compare(tickers):
    if isinstance(tickers, str):
        tickers = [tickers]
    rows_out = []
    for t in tickers[:10]:
        f = fundamentals(t)
        if f.get("error"):
            rows_out.append({"ticker": t.upper(), "error": f["error"]})
            continue
        rows_out.append(
            {
                "ticker": f["ticker"],
                "name": f["name"],
                "price": f["current_price"],
                "market_cap": f["market_cap"],
                "per": f["valuation"]["per_trailing"],
                "per_forward": f["valuation"]["per_forward"],
                "pbv": f["valuation"]["price_to_book"],
                "ev_ebitda": f["valuation"]["ev_ebitda"],
                "net_margin_pct": f["profitability"]["net_margin_pct"],
                "roe_pct": f["profitability"]["roe_pct"],
                "revenue_growth_pct": f["growth"]["revenue_yoy_pct"],
                "net_debt": f["financial_strength"]["net_debt"],
                "dividend_pct": f["dividend"]["yield_pct"],
            }
        )
    return {"comparison": rows_out}


# ---------------------------------------------------------------------- news


def _rss(url, limit):
    try:
        root = ET.fromstring(_http_get(url, binary=True))
    except Exception as exc:  # noqa: BLE001
        log(f"[market] RSS failed {url}: {exc}")
        return []
    items = []
    for item in root.iter("item"):
        title = item.findtext("title")
        if not title:
            continue
        source = item.findtext("source") or ""
        items.append(
            {
                "headline": title.strip(),
                "link": (item.findtext("link") or "").strip(),
                "date": (item.findtext("pubDate") or "").strip(),
                "source": source.strip() or urllib.parse.urlparse(url).netloc,
            }
        )
        if len(items) >= limit:
            break
    return items


@srv.tool(
    "news",
    "Recent news about a stock or topic, combining Yahoo Finance and Google News. "
    "Returns headlines with a link; use WebFetch on the link to read the content.",
    {
        "ticker": {"type": "string", "description": "Symbol, e.g. NVDA"},
        "query": {
            "type": "string",
            "description": "Alternative or additional free text, e.g. 'Inditex earnings'",
        },
        "language": {"type": "string", "description": "es | en", "default": "es"},
        "limit": {"type": "integer", "default": 15},
    },
)
def news(ticker=None, query=None, language="es", limit=15):
    if not ticker and not query:
        raise ValueError("Provide at least 'ticker' or 'query'")

    articles = []
    if ticker:
        articles += _rss(
            "https://feeds.finance.yahoo.com/rss/2.0/headline?s="
            f"{urllib.parse.quote(ticker.upper())}&region=US&lang=en-US",
            limit,
        )

    term = query or ticker
    if language == "es":
        suffix = "hl=es-419&gl=ES&ceid=ES:es"
    else:
        suffix = "hl=en-US&gl=US&ceid=US:en"
    articles += _rss(
        f"https://news.google.com/rss/search?q={urllib.parse.quote(term)}+"
        f"{'accion OR bolsa OR resultados' if language == 'es' else 'stock OR earnings'}&{suffix}",
        limit,
    )

    seen, unique = set(), []
    for a in articles:
        key = a["headline"].lower()[:80]
        if key in seen:
            continue
        seen.add(key)
        unique.append(a)

    return {
        "query": {"ticker": ticker, "text": query, "language": language},
        "news": unique[: int(limit)],
        "total": len(unique[: int(limit)]),
        "note": "Headlines from public sources; verify before acting on them.",
    }


@srv.tool(
    "earnings_calendar",
    "Next earnings release date and data from the latest report.",
    {"ticker": {"type": "string"}},
    required=["ticker"],
)
def earnings_calendar(ticker):
    crumb = _crumb()
    url = (
        f"https://query1.finance.yahoo.com/v10/finance/quoteSummary/"
        f"{urllib.parse.quote(ticker.upper())}?modules=calendarEvents,earningsHistory"
    )
    if crumb:
        url += f"&crumb={urllib.parse.quote(crumb)}"
    try:
        data = _get_json(url)
    except Exception as exc:  # noqa: BLE001
        return {"ticker": ticker.upper(), "error": str(exc)}

    result = (data.get("quoteSummary") or {}).get("result")
    if not result:
        return {"ticker": ticker.upper(), "error": "No calendar data"}

    events = result[0].get("calendarEvents", {}).get("earnings", {})
    dates = [
        datetime.fromtimestamp(_num(d), timezone.utc).date().isoformat()
        for d in events.get("earningsDate", [])
        if _num(d)
    ]
    history = [
        {
            "quarter": h.get("quarter", {}).get("fmt"),
            "eps_estimate": _num(h.get("epsEstimate")),
            "eps_actual": _num(h.get("epsActual")),
            "surprise_pct": (
                round(_num(h.get("surprisePercent")) * 100, 2)
                if _num(h.get("surprisePercent")) is not None
                else None
            ),
        }
        for h in result[0].get("earningsHistory", {}).get("history", [])
    ]
    return {"ticker": ticker.upper(), "upcoming_earnings_dates": dates, "history": history}


# ------------------------------------------------------------------- valuation


@srv.tool(
    "dcf_valuation",
    "Intrinsic value via two-phase discounted cash flow (DCF), with a sensitivity "
    "table. If financial inputs are omitted, they're pulled from fundamentals().",
    {
        "ticker": {"type": "string"},
        "free_cash_flow": {
            "type": "number",
            "description": "Current annual FCF in absolute currency. Pulled from Yahoo if omitted.",
        },
        "growth_pct": {
            "type": "number",
            "description": "Annual FCF growth in phase 1 (%)",
            "default": 8,
        },
        "phase1_years": {"type": "integer", "default": 10},
        "terminal_growth_pct": {
            "type": "number",
            "description": "Perpetual growth; must be < discount rate. Typically 2-2.5",
            "default": 2.5,
        },
        "discount_rate_pct": {
            "type": "number",
            "description": "Cost of capital / required return (%)",
            "default": 9,
        },
        "net_debt": {"type": "number", "description": "Debt - cash. Pulled from Yahoo if omitted."},
        "shares_outstanding": {"type": "number", "description": "Shares outstanding. Pulled from Yahoo if omitted."},
        "current_price": {"type": "number"},
    },
    required=["ticker"],
)
def dcf_valuation(
    ticker,
    free_cash_flow=None,
    growth_pct=8,
    phase1_years=10,
    terminal_growth_pct=2.5,
    discount_rate_pct=9,
    net_debt=None,
    shares_outstanding=None,
    current_price=None,
):
    ticker = ticker.upper().strip()
    origin, f = {}, {}
    if None in (free_cash_flow, shares_outstanding, current_price, net_debt):
        f = fundamentals(ticker)
        if not f.get("error"):
            derived_fields = f.get("derived_fields") or []
            def field_source(field):
                return "Yahoo (derived)" if any(field in d for d in derived_fields) else "Yahoo"

            if free_cash_flow is None:
                free_cash_flow = f["cash_flow"]["free_cash_flow"]
                origin["free_cash_flow"] = "Yahoo"
            if shares_outstanding is None:
                shares_outstanding = f["per_share"]["shares_outstanding"]
                origin["shares_outstanding"] = field_source("shares_outstanding")
            if current_price is None:
                current_price = f["current_price"]
                origin["current_price"] = "Yahoo"
            if net_debt is None:
                net_debt = f["financial_strength"]["net_debt"]
                origin["net_debt"] = "Yahoo"

    if not free_cash_flow or not shares_outstanding:
        missing = [
            n for n, v in (("free_cash_flow", free_cash_flow), ("shares_outstanding", shares_outstanding)) if not v
        ]
        available = {}
        if f and not f.get("error"):
            available = {
                k: v
                for k, v in {
                    "operating_cash_flow": f["cash_flow"].get("operating_cash_flow"),
                    "ebitda": f["cash_flow"].get("ebitda"),
                    "net_income": f["cash_flow"].get("net_income"),
                    "shares_outstanding": f["per_share"].get("shares_outstanding"),
                    "market_cap": f.get("market_cap"),
                }.items()
                if v
            }
        def fmt_num(n):
            for threshold, suffix in ((1e12, " B"), (1e9, " MM"), (1e6, " M")):
                if abs(n) >= threshold:
                    return f"{n / threshold:,.2f}{suffix}".replace(",", " ")
            return f"{n:,.0f}".replace(",", " ")

        detail = ", ".join(f"{k.replace('_', ' ')} {fmt_num(v)}" for k, v in available.items())
        raise ValueError(
            f"Yahoo doesn't publish {' or '.join(missing)} for {ticker}, so the DCF can't be "
            f"calculated on its own. Available: {detail or 'no useful data'}. "
            "Options: (a) enter the free cash flow by hand, taken from the annual report's "
            "cash flow statement (operating cash flow minus capex); or (b) use the Graham "
            "valuation, which only needs earnings per share and book value. Don't substitute "
            "free cash flow with net income without saying so: they aren't the same."
        )
    r = discount_rate_pct / 100.0
    g_t = terminal_growth_pct / 100.0
    if g_t >= r:
        raise ValueError(
            "Terminal growth must be lower than the discount rate; "
            "otherwise the model gives an infinite value."
        )

    def calculate(g_pct, rate_pct):
        g, disc = g_pct / 100.0, rate_pct / 100.0
        if g_t >= disc:
            return None
        fcf, pv = float(free_cash_flow), 0.0
        for year in range(1, int(phase1_years) + 1):
            fcf *= 1 + g
            pv += fcf / (1 + disc) ** year
        terminal_value = fcf * (1 + g_t) / (disc - g_t)
        pv += terminal_value / (1 + disc) ** int(phase1_years)
        equity = pv - (net_debt or 0)
        return equity / shares_outstanding

    value_per_share = calculate(growth_pct, discount_rate_pct)
    margin = (
        round(100 * (value_per_share - current_price) / value_per_share, 1)
        if current_price and value_per_share
        else None
    )

    sensitivity = []
    for g in (growth_pct - 4, growth_pct - 2, growth_pct, growth_pct + 2, growth_pct + 4):
        row = {"growth_pct": round(g, 1)}
        for d in (discount_rate_pct - 2, discount_rate_pct, discount_rate_pct + 2):
            v = calculate(g, d)
            row[f"disc_{round(d,1)}%"] = round(v, 2) if v else None
        sensitivity.append(row)

    return {
        "ticker": ticker,
        "method": "Two-phase DCF (explicit growth + Gordon perpetuity)",
        "assumptions": {
            "base_free_cash_flow": free_cash_flow,
            "phase1_growth_pct": growth_pct,
            "phase1_years": phase1_years,
            "terminal_growth_pct": terminal_growth_pct,
            "discount_rate_pct": discount_rate_pct,
            "net_debt": net_debt,
            "shares_outstanding": shares_outstanding,
            "data_source": origin or "all provided manually",
        },
        "intrinsic_value_per_share": round(value_per_share, 2),
        "current_price": current_price,
        "margin_of_safety_pct": margin,
        "reading": (
            "Undervalued under these assumptions"
            if margin and margin > 0
            else "Overvalued under these assumptions"
            if margin is not None
            else "No current price to compare against"
        ),
        "sensitivity": sensitivity,
        "notice": (
            "The DCF is extremely sensitive to the assumptions: look at the sensitivity "
            "table, not the single figure. This is not investment advice."
        ),
    }


@srv.tool(
    "graham_valuation",
    "Quick value-style valuations: Graham number, revised Graham formula, and "
    "dividend discount value (Gordon).",
    {
        "ticker": {"type": "string"},
        "eps": {"type": "number", "description": "Earnings per share; pulled from Yahoo if omitted"},
        "book_value_per_share": {"type": "number"},
        "growth_pct": {"type": "number", "default": 5},
        "annual_dividend": {"type": "number"},
        "required_return_pct": {"type": "number", "default": 9},
    },
    required=["ticker"],
)
def graham_valuation(
    ticker,
    eps=None,
    book_value_per_share=None,
    growth_pct=5,
    annual_dividend=None,
    required_return_pct=9,
):
    ticker = ticker.upper().strip()
    price = None
    if None in (eps, book_value_per_share, annual_dividend):
        f = fundamentals(ticker)
        if not f.get("error"):
            price = f["current_price"]
            eps = eps if eps is not None else f["per_share"]["eps_trailing"]
            book_value_per_share = (
                book_value_per_share
                if book_value_per_share is not None
                else f["per_share"]["book_value_per_share"]
            )
            annual_dividend = (
                annual_dividend if annual_dividend is not None else f["dividend"]["annual_dividend"]
            )

    results = {}
    if eps and book_value_per_share and eps > 0 and book_value_per_share > 0:
        results["graham_number"] = round(
            math.sqrt(22.5 * eps * book_value_per_share), 2
        )
    if eps and eps > 0:
        results["graham_revised_formula"] = round(
            eps * (8.5 + 2 * growth_pct) * 4.4 / max(required_return_pct, 0.1), 2
        )
    r = required_return_pct / 100.0
    g = min(growth_pct, required_return_pct - 1) / 100.0
    if annual_dividend and r > g:
        results["gordon_dividend_discount"] = round(annual_dividend * (1 + g) / (r - g), 2)

    if not results:
        raise ValueError(
            "Not enough data (eps/book value/dividend). Enter them manually."
        )

    values = list(results.values())
    return {
        "ticker": ticker,
        "current_price": price,
        "valuations": results,
        "estimated_range": {"min": round(min(values), 2), "max": round(max(values), 2)},
        "margin_vs_median_pct": (
            round(100 * (statistics.median(values) - price) / statistics.median(values), 1)
            if price
            else None
        ),
        "inputs": {
            "eps": eps,
            "book_value_per_share": book_value_per_share,
            "growth_pct": growth_pct,
            "annual_dividend": annual_dividend,
            "required_return_pct": required_return_pct,
        },
        "notice": (
            "Graham's formulas assume mature companies with stable earnings; "
            "they distort heavily for high-growth tech or loss-making companies."
        ),
    }


@srv.tool(
    "exchange_rate",
    "Current exchange rate between two currencies.",
    {
        "from_currency": {"type": "string", "description": "ISO 4217, e.g. USD"},
        "to_currency": {"type": "string", "description": f"ISO 4217, e.g. {BASE_CURRENCY}"},
    },
    required=["from_currency"],
)
def exchange_rate(from_currency, to_currency=None):
    to_currency = (to_currency or BASE_CURRENCY).upper()
    from_currency = from_currency.upper()
    if from_currency == to_currency:
        return {"from_currency": from_currency, "to_currency": to_currency, "rate": 1.0}
    pair = f"{from_currency}{to_currency}=X"
    data = _yahoo_quote(pair)
    return {
        "from_currency": from_currency,
        "to_currency": to_currency,
        "rate": data["price"],
        "change_pct": data.get("change_pct"),
    }


@srv.tool(
    "stock_score",
    "1-10 buy/sell score from quote, fundamentals, historical and valuation data, "
    "weighted by investor profile. Never sends profile data over the network.",
    {
        "ticker": {"type": "string"},
        "risk_tolerance": {"type": "string", "enum": ["conservative", "moderate", "aggressive"]},
        "horizon": {"type": "string", "enum": ["short", "medium", "long"]},
        "preference": {"type": "string", "enum": ["dividend", "growth", "balanced"]},
    },
    required=["ticker"],
)
def stock_score(ticker, risk_tolerance=None, horizon=None, preference=None):
    ticker = ticker.upper().strip()
    q = quote(tickers=[ticker])["quotes"]
    q = q[0] if q else {}
    f = fundamentals(ticker)
    try:
        h = historical(ticker=ticker, date_range="1y")
    except Exception:  # noqa: BLE001 - momentum pillar just skips
        h = {}
    try:
        dcf = dcf_valuation(ticker=ticker)
    except Exception:  # noqa: BLE001 - optional input
        dcf = None
    try:
        graham = graham_valuation(ticker=ticker)
    except Exception:  # noqa: BLE001 - optional input
        graham = None

    profile = {k: v for k, v in {
        "risk_tolerance": risk_tolerance, "horizon": horizon, "preference": preference,
    }.items() if v}
    return scoring.score_stock(q, f, h, dcf=dcf, graham=graham, profile=profile or None)


if __name__ == "__main__":
    srv.run()
