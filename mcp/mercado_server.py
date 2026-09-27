"""MCP server 'market': quotes, fundamentals, news and valuation.

API-key-free data sources:
  - Yahoo Finance (chart v8, quoteSummary v10, search v1)  -> prices & fundamentals
  - Yahoo Finance RSS and Google News RSS                  -> news
  - Stooq CSV                                              -> price fallback

Optional keys via environment variables (improve reliability if Yahoo fails):
  FINNHUB_API_KEY, FMP_API_KEY
"""

import calendar
import email.utils
import http.cookiejar
import json
import math
import os
import statistics
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import scoring
import symbols
from mcp_common import MCPServer, log
import indicators
import metrics

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


def _round3(value):
    return round(value, 3) if value is not None else None


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


def _is_otc(market, ticker=None):
    """Pink sheets and grey markets: trade the same company with a fraction of the
    liquidity and enormous spreads. Never the default suggestion. The local catalog
    decides for any symbol it knows (IBDSF, LVMUY); the market name is only the
    fallback for symbols it doesn't."""
    row = symbols.lookup(ticker) if ticker else None
    if row:
        return symbols.is_otc(row)
    m = (market or "").strip().lower()
    return any(x in m for x in ("otc", "pink", "grey", "gray"))


def _market_rank(ticker, market=None):
    if _is_otc(market, ticker):
        return 98
    suffix = "." + ticker.split(".")[-1] if "." in ticker else ""
    return PRIMARY_SUFFIXES.index(suffix) if suffix in PRIMARY_SUFFIXES else 99


_tokens = symbols.tokens


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


def _catalog_result(row):
    return {
        "ticker": row["symbol"],
        "name": row["name"],
        "type": "EQUITY",
        "market": row["market"] or row["exchange"],
        "sector": row["sector"] or None,
        "country": row["country"] or None,
        "isin": row["isin"] or None,
        "mic": row["mic"] or None,
        "found_via": "local catalog",
    }


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
    # The local catalog answers first, without network. Yahoo is only asked about
    # names the catalog doesn't know ("Inditex" is registered as "Industria de Diseno
    # Textil"), and then the catalog still fills in the home listing.
    # Catalog hits come already ranked (exact symbol, home market, size); Yahoo's are
    # sorted by the primary-market suffix list.
    results = [_catalog_result(r) for r in symbols.search(query)]
    if not results:
        results, added = _yahoo_search(query, verify_primary)
        results = sorted(
            added + results, key=lambda r: _market_rank(r["ticker"], r.get("market"))
        )
    for r in results:
        row = symbols.lookup(r["ticker"])
        r.setdefault("isin", (row or {}).get("isin") or None)
        r.setdefault("mic", (row or {}).get("mic") or None)
        if _is_otc(r.get("market"), r["ticker"]):
            r["warning"] = "OTC market: low liquidity, avoid unless it's the only option"

    # The same company often trades on multiple exchanges. Exposed so it's
    # clear you need to choose, instead of silently assuming the first.
    equities = [r for r in results
                if r.get("type") == "EQUITY" and not _is_otc(r.get("market"), r["ticker"])]
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


def _yahoo_search(query, verify_primary):
    """Yahoo search for names the catalog doesn't know. Returns (results, added)."""
    url = (
        "https://query1.finance.yahoo.com/v1/finance/search?q="
        f"{urllib.parse.quote(query)}&quotesCount=10&newsCount=0"
    )
    try:
        quotes = _get_json(url).get("quotes", [])
    except Exception as exc:  # noqa: BLE001
        log(f"[market] search failed: {exc}")
        quotes = []

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
    # Warsaw, Milan and Hamburg, but never ITX.MC). The catalog knows the other
    # listings of any symbol Yahoo did return, so the home one comes from there.
    known = {r["ticker"] for r in results}
    added = []
    for r in results:
        row = symbols.lookup(r["ticker"])
        for s in symbols.siblings(row) if row else []:
            if s["symbol"] not in known and symbols.is_home(s) and not symbols.is_otc(s):
                added.append(_catalog_result(s))
                known.add(s["symbol"])
    if added:
        return results, added

    # Not in the catalog either: probe the primary markets, verifying name
    # identity to avoid including a different company reusing those same letters.
    if verify_primary and results:
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
    return results, added


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
    quote = res["indicators"]["quote"][0]
    raw_closes = quote.get("close") or []
    # ATR needs the true range, so the same row must carry high and low too.
    raw_highs = quote.get("high") or []
    raw_lows = quote.get("low") or []

    series, candles = [], []
    for i, (t, c) in enumerate(zip(timestamps, raw_closes)):
        if c is None:
            continue
        series.append(
            (datetime.fromtimestamp(t, timezone.utc).date().isoformat(), c)
        )
        h = raw_highs[i] if i < len(raw_highs) else None
        lo = raw_lows[i] if i < len(raw_lows) else None
        if h is not None and lo is not None:
            candles.append(indicators.OHLCV(h, lo, c))
    if len(series) < 3:
        raise ValueError("Insufficient historical data to calculate metrics")

    closes = [c for _, c in series]
    returns = [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes))]
    # Annualize from the data, not from the interval we asked for: Yahoo downsamples
    # long ranges, so "1d" can come back monthly or quarterly (see metrics.py).
    periods_per_year = metrics.periods_per_year_from_dates(
        [d for d, _ in series]
    ) or metrics.PERIODS_PER_YEAR.get(interval, 252)
    volatility = (
        statistics.pstdev(returns) * math.sqrt(periods_per_year) * 100 if len(returns) > 1 else None
    )

    peak, max_dd = closes[0], 0.0
    for c in closes:
        peak = max(peak, c)
        max_dd = min(max_dd, c / peak - 1)

    # HU-23: the maths comes from `mcp/indicators.py` (vendored from talipp,
    # see `mcp/FUENTE.md`). SMA keeps the exact value the hand-written mean had;
    # RSI now follows Wilder's smoothing instead of the plain average of the
    # last 14 returns — the difference is documented and pinned in the tests.
    # The instances are kept, not just their `.last()`: HU-36 also returns the
    # per-point series, which is the same computation read at every index.
    sma50_ind = indicators.SMA(50, closes)
    sma200_ind = indicators.SMA(200, closes)
    sma50 = sma50_ind.last()
    sma200 = sma200_ind.last()
    rsi = indicators.RSI(14, closes).last()
    atr = indicators.ATR(14, candles).last()
    bb_ind = indicators.BB(20, 2.0, closes)
    macd_ind = indicators.MACD(12, 26, 9, closes)
    bb = bb_ind.last()
    macd = macd_ind.last()

    def r4(v):
        return round(v, 4) if v is not None else None

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
        # Risk-adjusted metrics (HU-24). The risk-free rate is a knob
        # (SEALOCK_RISK_FREE_RATE), echoed in every portfolio summary, never hidden.
        "sharpe": _round3(metrics.sharpe(returns, periods_per_year)),
        "sortino": _round3(metrics.sortino(returns, periods_per_year)),
        "calmar": _round3(metrics.calmar(returns, periods_per_year)),
        "risk_free_rate": metrics.RISK_FREE_RATE,
        # HU-23: trend/volatility indicators (talipp semantics, FUENTE.md).
        "sma_50": r4(sma50),
        "sma_200": r4(sma200),
        "rsi_14": round(rsi, 1) if rsi is not None else None,
        "atr_14": r4(atr),
        "bollinger_upper": r4(bb.ub) if bb else None,
        "bollinger_lower": r4(bb.lb) if bb else None,
        "macd": r4(macd.macd) if macd else None,
        "macd_signal": r4(macd.signal) if macd and macd.signal is not None else None,
        "period_high": round(max(closes), 4),
        "period_low": round(min(closes), 4),
    }
    if include_series:
        # `closes` is the whole requested window (the sparkline and the portfolio
        # history need a year, not a month); `recent_closes` stays the last 60.
        output["closes"] = [{"date": f, "close": round(c, 4)} for f, c in series]
        output["recent_closes"] = [
            {"date": f, "close": round(c, 4)} for f, c in series[-60:]
        ]
        # HU-36: the indicators as series, index-aligned with `series` so the UI can
        # draw them over the price. Seeds stay `None` (never 0) and the leading rows
        # stay too: the chart skips a `None`, but it needs the index to match `closes`.
        # Emitted only when every output is aligned: a shorter list would draw an
        # indicator on the wrong date.
        if all(len(x) == len(series) for x in (sma50_ind, sma200_ind, bb_ind, macd_ind)):
            dates = [f for f, _ in series]
            output["sma_50_series"] = [
                {"date": f, "value": r4(v)} for f, v in zip(dates, sma50_ind)
            ]
            output["sma_200_series"] = [
                {"date": f, "value": r4(v)} for f, v in zip(dates, sma200_ind)
            ]
            output["bollinger_upper_series"] = [
                {"date": f, "value": r4(v.ub) if v else None} for f, v in zip(dates, bb_ind)
            ]
            output["bollinger_lower_series"] = [
                {"date": f, "value": r4(v.lb) if v else None} for f, v in zip(dates, bb_ind)
            ]
            output["macd_series"] = [
                {
                    "date": f,
                    "macd": r4(v.macd) if v else None,
                    "signal": r4(v.signal) if v else None,
                    "histogram": r4(v.histogram) if v else None,
                }
                for f, v in zip(dates, macd_ind)
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


def _news_key(article):
    return article["headline"].lower()[:80]


def news_dedupe(articles, seen=None):
    """Drop repeated headlines, keeping the first. `seen` (a set of keys) is updated in
    place, so a second list can be deduped against the first one."""
    seen = set() if seen is None else seen
    unique = []
    for a in articles:
        key = _news_key(a)
        if key in seen:
            continue
        seen.add(key)
        unique.append(a)
    return unique


def news_sorted(articles, limit=10):
    """Newest first, with `published` as ISO 8601 (RSS pubDate is RFC 2822). Undated
    items go last. Pure: no network."""
    out = []
    for a in articles:
        try:
            published = email.utils.parsedate_to_datetime(a.get("date") or "")
            if published.tzinfo is None:
                published = published.replace(tzinfo=timezone.utc)
        except Exception:  # noqa: BLE001 - one malformed feed date must not drop the list
            published = None
        # UTC so the ISO strings sort correctly as text.
        out.append({**a, "published": (published.astimezone(timezone.utc).isoformat()
                                       if published else None)})
    out.sort(key=lambda a: a["published"] or "", reverse=True)
    return out[: int(limit)]


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
        "https://news.google.com/rss/search?q=" + urllib.parse.quote_plus(
            f"{term} {'accion OR bolsa OR resultados' if language == 'es' else 'stock OR earnings'}"
        ) + f"&{suffix}",
        limit,
    )

    unique = news_dedupe(articles)

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


# ------------------------------------------------------------------- quarterly

QUARTERLY_TYPES = (
    "quarterlyTotalRevenue,quarterlyNetIncome,quarterlyOperatingIncome,"
    "quarterlyDilutedEPS,quarterlyFreeCashFlow"
)
QUARTERLY_FIELDS = {
    "quarterlyTotalRevenue": "revenue",
    "quarterlyNetIncome": "net_income",
    "quarterlyOperatingIncome": "operating_income",
    "quarterlyDilutedEPS": "diluted_eps",
    "quarterlyFreeCashFlow": "free_cash_flow",
}


def _quarter_rows(payload):
    """Yahoo fundamentals-timeseries payload -> one row per period, newest first."""
    rows = {}
    for item in (payload.get("timeseries") or {}).get("result") or []:
        for yahoo_key, field in QUARTERLY_FIELDS.items():
            for entry in item.get(yahoo_key) or []:
                if not entry or not entry.get("asOfDate"):
                    continue
                row = rows.setdefault(
                    entry["asOfDate"],
                    dict({"period_end": entry["asOfDate"]},
                         **{f: None for f in QUARTERLY_FIELDS.values()}),
                )
                row[field] = _num(entry.get("reportedValue"))
                row["currency"] = entry.get("currencyCode") or row.get("currency")
    return [rows[d] for d in sorted(rows, reverse=True)]


def _yoy_pct(value, base):
    """Year-over-year in percent. None when the base quarter is missing or not positive:
    growth over a loss is not a number."""
    if value is None or base is None or base <= 0:
        return None
    return round((value / base - 1) * 100, 2)


def _day(period_end):
    return datetime.strptime(period_end, "%Y-%m-%d").date()


def _month_end(ym):
    """'2026-01' -> '2026-01-31': the last day of a slot Yahoo never reported."""
    return f"{ym}-{calendar.monthrange(int(ym[:4]), int(ym[5:7]))[1]:02d}"


def _prior_year(rows, period_end):
    """The same fiscal quarter a year earlier, within 30 days: retail calendars move
    the closing date a few days every year."""
    target = _day(period_end) - timedelta(days=365)
    best, best_gap = None, 31
    for row in rows:
        gap = abs((_day(row["period_end"]) - target).days)
        if gap < best_gap:
            best, best_gap = row, gap
    return best


def quarterly_derive(rows, ticker=None):
    """Pure shaping of raw quarters into the quarterly() result, no network.

    'quarters' are the last three fiscal slots, stepping back three months from the
    newest close. A slot Yahoo never reported is simply absent: that is what 'available'
    counts and 'gaps' explains.
    """
    rows = sorted(rows or [], key=lambda r: r["period_end"], reverse=True)
    by_month, slots = {}, []
    if rows:
        for row in rows:
            by_month.setdefault(row["period_end"][:7], row)
        year, month = int(rows[0]["period_end"][:4]), int(rows[0]["period_end"][5:7])
        for back in range(3):
            total = year * 12 + (month - 1) - 3 * back
            slots.append(f"{total // 12:04d}-{total % 12 + 1:02d}")
    expected = [(slot, by_month.get(slot)) for slot in slots]

    quarters = []
    for _slot, row in expected:
        if not row:
            continue
        base = _prior_year(rows, row["period_end"]) or {}
        quarter = dict(row)
        quarter["revenue_yoy_pct"] = _yoy_pct(row.get("revenue"), base.get("revenue"))
        quarter["eps_yoy_pct"] = _yoy_pct(row.get("diluted_eps"), base.get("diluted_eps"))
        quarter["net_income_yoy_pct"] = _yoy_pct(row.get("net_income"), base.get("net_income"))
        revenue = row.get("revenue")
        quarter["net_margin_pct"] = (
            round(row["net_income"] / revenue * 100, 2)
            if revenue and row.get("net_income") is not None
            else None
        )
        quarters.append(quarter)

    # A hole is a fiscal slot Yahoo never reported: bracket it with the reported
    # quarters on each side, or with the slot's own month end when it has none below.
    gaps, above = [], None
    for i, (slot, row) in enumerate(expected):
        if row is None:
            below = next((r for _, r in expected[i + 1:] if r), None)
            if above:
                gaps.append([below["period_end"] if below else _month_end(slot), above["period_end"]])
        else:
            above = row
    available = len(quarters)
    missing = [f for f in QUARTERLY_FIELDS.values() if all(q.get(f) is None for q in quarters)]
    if available == 0:
        note = f"Sin datos trimestrales publicos para {ticker}."
    elif available < 3:
        detail = f": hueco entre {gaps[0][0]} y {gaps[0][1]}" if gaps else ""
        note = f"Solo {available} de 3 trimestres disponibles{detail}."
    else:
        note = "3 de 3 trimestres disponibles."
    return {
        "ticker": ticker,
        "quarters": quarters,
        "available": available,
        "missing_fields": missing,
        "gaps": gaps,
        "note_es": note,
    }


@srv.tool(
    "quarterly",
    "Last fiscal quarters: revenue, net income, operating income, diluted EPS and free "
    "cash flow, with year-over-year comparisons. Holes and missing fields are reported, "
    "never filled.",
    {"ticker": {"type": "string"}},
    required=["ticker"],
)
def quarterly(ticker):
    ticker = ticker.upper().strip()
    now = int(time.time())
    url = (
        "https://query1.finance.yahoo.com/ws/fundamentals-timeseries/v1/finance/timeseries/"
        f"{urllib.parse.quote(ticker)}?symbol={urllib.parse.quote(ticker)}"
        f"&type={QUARTERLY_TYPES}&period1={now - 900 * 86400}&period2={now}"
    )
    try:
        payload = _get_json(url)
    except Exception as exc:  # noqa: BLE001 - a hole in the data never breaks the caller
        return {"ticker": ticker, "error": str(exc)}
    result = quarterly_derive(_quarter_rows(payload), ticker)
    # EPS surprise vs consensus, last 3. Separate quoteSummary call; the scoring pillar
    # skips the metric when it's missing, so a failure here is not fatal.
    try:
        result["earnings_surprise_pct"] = [
            h["surprise_pct"] for h in earnings_calendar(ticker).get("history", [])[:3]
            if h.get("surprise_pct") is not None
        ]
    except Exception:  # noqa: BLE001
        result["earnings_surprise_pct"] = []
    return result


# ------------------------------------------------------------------ estimates

ESTIMATE_PERIODS_ES = {
    "0q": "Trimestre actual",
    "+1q": "Proximo trimestre",
    "0y": "Ano actual",
    "+1y": "Ano siguiente",
}


def parse_estimates(payload):
    """Pure: quoteSummary earningsTrend + financialData -> analyst projections.

    This is the sell-side consensus (BPA and revenue expected for the next periods,
    how many analysts, and how many of them moved their estimate up or down). Levels are
    kept out of the score on purpose: consensus skews optimistic. What the score uses is
    the *revision balance*, which is sentiment and not a level.
    """
    result = (payload.get("quoteSummary") or {}).get("result") or []
    if not result:
        return None
    block = result[0]
    trend = (block.get("earningsTrend") or {}).get("trend") or []
    periods = []
    for row in trend:
        eps = row.get("earningsEstimate") or {}
        rev = row.get("revenueEstimate") or {}
        up30, down30 = row.get("epsRevisions") or {}, {}
        down30 = up30.get("downLast30days")
        up30 = up30.get("upLast30days")
        periods.append({
            "period": row.get("period"),
            "period_es": ESTIMATE_PERIODS_ES.get(row.get("period"), row.get("period")),
            "end_date": row.get("endDate"),
            "eps": _num(eps.get("avg")),
            "eps_low": _num(eps.get("low")),
            "eps_high": _num(eps.get("high")),
            "analysts": _num(eps.get("numberOfAnalysts")),
            "revenue": _num(rev.get("avg")),
            "revenue_growth_pct": round(_num(rev.get("growth")) * 100, 2)
            if _num(rev.get("growth")) is not None else None,
            "revisions_up_30d": _num(up30),
            "revisions_down_30d": _num(down30),
        })
    if not periods:
        return None

    fd = block.get("financialData") or {}
    # Revision balance over the two nearest quarters: a level, not a price, so the
    # documented optimism bias of the consensus hits it much less.
    near = [p for p in periods if p["period"] in ("0q", "+1q")][:2] or periods[:2]
    net = sum((p["revisions_up_30d"] or 0) - (p["revisions_down_30d"] or 0) for p in near)
    return {
        "periods": periods,
        "revisions_net_30d": net,
        "revisions_up_30d": sum(p["revisions_up_30d"] or 0 for p in near),
        "revisions_down_30d": sum(p["revisions_down_30d"] or 0 for p in near),
        "target": {
            "mean": _num(fd.get("targetMeanPrice")),
            "high": _num(fd.get("targetHighPrice")),
            "low": _num(fd.get("targetLowPrice")),
            "analysts": _num(fd.get("numberOfAnalystOpinions")),
            "recommendation": fd.get("recommendationKey"),
        },
        "note_es": (
            "Consenso de analistas, no un hecho: el sesgo del sell-side esta documentado, "
            "por eso solo la revision de estimaciones entra en la nota y con poco peso."
        ),
    }


@srv.tool(
    "earnings_estimates",
    "Analyst projections: consensus EPS and revenue for the current and next quarter and "
    "year, how many analysts, and how many moved their estimate up or down in 30 days. "
    "Forecasts, not facts.",
    {"ticker": {"type": "string"}},
    required=["ticker"],
)
def earnings_estimates(ticker):
    ticker = ticker.upper().strip()
    crumb = _crumb()
    url = (f"https://query1.finance.yahoo.com/v10/finance/quoteSummary/{urllib.parse.quote(ticker)}"
           "?modules=earningsTrend,financialData")
    if crumb:
        url += f"&crumb={urllib.parse.quote(crumb)}"
    try:
        payload = _get_json(url)
    except Exception as exc:  # noqa: BLE001 - a missing projection is not fatal
        return {"ticker": ticker, "error": str(exc)}
    estimates = parse_estimates(payload)
    if estimates is None:
        return {"ticker": ticker, "error": "Sin proyecciones publicas para este valor"}
    return {"ticker": ticker, **estimates}


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
    try:
        recent = quarterly(ticker)
    except Exception:  # noqa: BLE001 - results pillar just skips
        recent = {}
    try:
        estimates = earnings_estimates(ticker)
    except Exception:  # noqa: BLE001 - projections just skip
        estimates = {}

    profile = {k: v for k, v in {
        "risk_tolerance": risk_tolerance, "horizon": horizon, "preference": preference,
    }.items() if v}
    return scoring.score_stock(q, f, h, dcf=dcf, graham=graham, profile=profile or None,
                               quarterly=recent, estimates=estimates)


if __name__ == "__main__":
    srv.run()
