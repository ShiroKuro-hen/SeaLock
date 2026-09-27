"""Portfolio metrics, stdlib only (formulas ported from quantstats, Apache-2.0).

Pure: no network, no I/O. The input is a list of simple period returns
(`close[i] / close[i-1] - 1`), never prices. Point-in-time by construction: the
series is whatever the caller built up to the observation date, so nothing here
can see the future (HU-26).

The risk-free rate is a knob, never a hidden constant (HU-24): it comes from
SEALOCK_RISK_FREE_RATE (annual, decimal; default 0) and is echoed back in
`summary()` so the number used is always visible next to the result.

Formulas and their convention (pinned by pruebas/prueba_humo.py):
  annualized_return  prod(1+r) ** (periods_per_year / n) - 1
  volatility         sample stdev of returns * sqrt(periods_per_year)
  max_drawdown       worst equity[i] / running_peak - 1
  sharpe             (annualized_return - risk_free) / volatility
  sortino            (annualized_return - risk_free) / downside_deviation,
                     downside over ALL observations, target = risk_free / ppy
  calmar             annualized_return / |max_drawdown|
"""

import math
import os
from datetime import date as _date

# Nominal periods per year, used when a series says nothing better about its own spacing.
PERIODS_PER_YEAR = {"1d": 252, "1wk": 52, "1mo": 12}


def _read_risk_free_rate():
    """SEALOCK_RISK_FREE_RATE is annual decimal. A typo must not read as 0.0: the rate
    is shown next to every result (HU-24), so a silent 0 would be a wrong number
    nobody could see. Fail loudly and name the variable instead."""
    raw = (os.environ.get("SEALOCK_RISK_FREE_RATE") or "").strip()
    if not raw:
        return 0.0
    try:
        return float(raw)
    except ValueError:
        raise ValueError(
            f"SEALOCK_RISK_FREE_RATE must be a number (annual, decimal), got {raw!r}"
        ) from None


RISK_FREE_RATE = _read_risk_free_rate()


def periods_per_year_from_dates(dates):
    """Annualization factor implied by the series itself: returns per calendar year.

    Yahoo downsamples long ranges, so a request for "daily" data can come back monthly
    or quarterly. Assuming 252 there would annualize a monthly series as if it were
    daily and invent absurd metrics — which is exactly what this prevents. Pure and
    testable offline. None when there is nothing to measure.

    Accepts ISO `AAAA-MM-DD` strings (the project convention) or `date` objects.
    """
    ds = []
    for value in dates:
        try:
            ds.append(_date.fromisoformat(str(value)[:10]) if isinstance(value, str) else value)
        except ValueError:
            continue
    ds = sorted(d for d in ds if d is not None)
    if len(ds) < 2:
        return None
    years = (ds[-1] - ds[0]).days / 365.25
    if years <= 0:
        return None
    return round((len(ds) - 1) / years, 2)


def _clean(returns):
    return [float(r) for r in returns if r is not None]


def _round(value, digits=3):
    return round(value, digits) if value is not None else None


def _pct(value):
    return round(value * 100, 2) if value is not None else None


def annualized_return(returns, periods_per_year=252):
    """Geometric annualized return. None when the series is empty or wiped out."""
    rs = _clean(returns)
    if not rs:
        return None
    growth = 1.0
    for r in rs:
        growth *= 1.0 + r
        if growth <= 0:
            return None
    return growth ** (periods_per_year / len(rs)) - 1.0


def annualized_volatility(returns, periods_per_year=252):
    """Annualized sample standard deviation. None with fewer than 2 observations."""
    rs = _clean(returns)
    if len(rs) < 2:
        return None
    mean = sum(rs) / len(rs)
    variance = sum((r - mean) ** 2 for r in rs) / (len(rs) - 1)
    return math.sqrt(variance) * math.sqrt(periods_per_year)


def max_drawdown(returns):
    """Worst peak-to-trough fall of the equity curve, as a negative fraction (0 = never)."""
    rs = _clean(returns)
    if not rs:
        return None
    equity, peak, worst = 1.0, 1.0, 0.0
    for r in rs:
        equity *= 1.0 + r
        peak = max(peak, equity)
        worst = min(worst, equity / peak - 1.0)
    return worst


def sharpe(returns, periods_per_year=252, risk_free_rate=None):
    """Excess annualized return per unit of total volatility. None with no volatility.

    The epsilon is deliberate: a constant series has a stdev that rounds to 0 but is
    not exactly 0 in floating point, and dividing by that noise would invent a Sharpe
    of 1e16 instead of saying "no volatility to compare against".
    """
    ann = annualized_return(returns, periods_per_year)
    vol = annualized_volatility(returns, periods_per_year)
    if ann is None or vol is None or vol < 1e-12:
        return None
    rf = RISK_FREE_RATE if risk_free_rate is None else risk_free_rate
    return (ann - rf) / vol


def sortino(returns, periods_per_year=252, risk_free_rate=None):
    """Excess annualized return per unit of downside deviation. None with no downside."""
    ann = annualized_return(returns, periods_per_year)
    rs = _clean(returns)
    if ann is None or not rs:
        return None
    rf = RISK_FREE_RATE if risk_free_rate is None else risk_free_rate
    target = rf / periods_per_year
    downside_sq = sum(min(r - target, 0.0) ** 2 for r in rs) / len(rs)
    if downside_sq <= 0:
        return None
    return (ann - rf) / (math.sqrt(downside_sq) * math.sqrt(periods_per_year))


def calmar(returns, periods_per_year=252):
    """Annualized return per unit of max drawdown. None when there was no drawdown."""
    ann = annualized_return(returns, periods_per_year)
    dd = max_drawdown(returns)
    if ann is None or not dd:
        return None
    return ann / abs(dd)


def summary(returns, periods_per_year=252, risk_free_rate=None):
    """Every metric at once, with the risk-free rate actually used (never hidden)."""
    rf = RISK_FREE_RATE if risk_free_rate is None else risk_free_rate
    return {
        "periods_per_year": periods_per_year,
        "risk_free_rate": rf,
        "observations": len(_clean(returns)),
        "annualized_return_pct": _pct(annualized_return(returns, periods_per_year)),
        "annualized_volatility_pct": _pct(annualized_volatility(returns, periods_per_year)),
        "max_drawdown_pct": _pct(max_drawdown(returns)),
        "sharpe": _round(sharpe(returns, periods_per_year, rf)),
        "sortino": _round(sortino(returns, periods_per_year, rf)),
        "calmar": _round(calmar(returns, periods_per_year)),
    }
