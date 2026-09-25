"""Pure scoring engine: turns quote/fundamentals/historical/valuation data into a
1-10 buy/sell score adjusted to an investor profile. No network, no I/O — every
input is a plain dict already fetched by the caller (mercado_server.stock_score or
app/servidor.py's stock_detail).

Sources behind the six pillars: Buffett (ROE, low debt), Graham (defensive
criteria), Lynch (PEG), Greenblatt (earnings yield spirit via EV/EBITDA and ROIC
proxies), Piotroski (profitability + leverage signals), AQR quality-minus-junk
(margins, ROA), Jegadeesh-Titman (price momentum). Analyst consensus is used only
as a weak tiebreak in valuation because sell-side targets skew optimistic.

NOT investment advice: fixed rules over public data, nothing more.
"""

BASE_WEIGHTS = {
    "moderate":     {"quality": 25, "valuation": 25, "health": 15, "growth": 15, "momentum": 15, "dividend": 5},
    "conservative": {"quality": 25, "valuation": 20, "health": 25, "growth": 10, "momentum": 5,  "dividend": 15},
    "aggressive":   {"quality": 20, "valuation": 20, "health": 10, "growth": 25, "momentum": 25, "dividend": 0},
}

PILLAR_NAMES_ES = {
    "quality": "Calidad",
    "valuation": "Valoracion",
    "health": "Solidez financiera",
    "growth": "Crecimiento",
    "momentum": "Momentum",
    "dividend": "Dividendo",
}

NOTICE = (
    "Puntuacion mecanica basada en reglas fijas sobre datos publicos; no es "
    "asesoramiento de un profesional con licencia. La decision es tuya."
)

# Max weight (% of portfolio base-currency value) a single position may reach before
# "No ampliar" kicks in, by risk tolerance. Used by position_advice() for Recomendaciones.
CONCENTRATION_LIMITS = {"conservative": 20, "moderate": 25, "aggressive": 35}


def _lin(value, bad, good):
    """Score in [-1, 1]: linear from `bad` (-1) to `good` (+1), clamped.

    Works for metrics where lower is better too (pass good < bad), e.g.
    _lin(per, bad=30, good=15).
    """
    if value is None:
        return None
    span = good - bad
    if span == 0:
        return 0.0
    t = max(0.0, min(1.0, (value - bad) / span))
    return round(2 * t - 1, 4)


def _detail(key, label, value, score, unit="", fmt="{:.1f}"):
    grade = "bueno" if score >= 0.5 else "malo" if score <= -0.5 else "neutral"
    try:
        vtxt = fmt.format(value)
    except (TypeError, ValueError):
        vtxt = str(value)
    return {
        "metric": key,
        "value": value,
        "points": score,
        "reason_es": f"{label}: {vtxt}{unit} ({grade})",
    }


def _combine(pairs):
    """Weighted mean of (score, weight) pairs, or None if there's nothing to average."""
    pairs = [p for p in pairs if p[0] is not None]
    total_w = sum(w for _, w in pairs)
    if not pairs or total_w == 0:
        return None
    return round(sum(s * w for s, w in pairs) / total_w, 4)


# --------------------------------------------------------------------- pillars


def _quality(f):
    prof = (f or {}).get("profitability") or {}
    details, pairs = [], []

    v = prof.get("roe_pct")
    s = _lin(v, 8, 15)
    if s is not None:
        pairs.append((s, 1.0)); details.append(_detail("roe_pct", "ROE", v, s, "%"))

    net = prof.get("net_margin_pct")
    if net is not None:
        s = _lin(net, 5, 15)
        pairs.append((s, 1.0)); details.append(_detail("net_margin_pct", "Margen neto", net, s, "%"))
    else:
        op = prof.get("operating_margin_pct")
        s = _lin(op, 5, 15)
        if s is not None:
            pairs.append((s, 1.0)); details.append(_detail("operating_margin_pct", "Margen operativo", op, s, "%"))

    v = prof.get("roa_pct")
    s = _lin(v, 2, 7)
    if s is not None:
        pairs.append((s, 1.0)); details.append(_detail("roa_pct", "ROA", v, s, "%"))

    return _combine(pairs), details


def _valuation(f, dcf, graham, price):
    val = (f or {}).get("valuation") or {}
    details, pairs = [], []

    per, per_label = val.get("per_trailing"), "PER"
    if per is None:
        per, per_label = val.get("per_forward"), "PER futuro"
    if per is not None:
        s = -1.0 if per < 0 else _lin(per, 30, 15)
        pairs.append((s, 1.0)); details.append(_detail("per", per_label, per, s))

    peg = val.get("peg")
    if peg is not None:
        s = -1.0 if peg <= 0 else _lin(peg, 2, 1)
        pairs.append((s, 1.0)); details.append(_detail("peg", "PEG", peg, s))

    ev = val.get("ev_ebitda")
    if ev is not None:
        s = -1.0 if ev < 0 else _lin(ev, 15, 10)
        pairs.append((s, 1.0)); details.append(_detail("ev_ebitda", "EV/EBITDA", ev, s))

    pb = val.get("price_to_book")
    if pb is not None:
        s = -1.0 if pb < 0 else _lin(pb, 3, 1.5)
        pairs.append((s, 0.5)); details.append(_detail("price_to_book", "Precio/Valor contable", pb, s))

    margin = (dcf or {}).get("margin_of_safety_pct")
    # ponytail: a DCF value under ~1/4 of price usually means bad inputs (Yahoo's
    # levered FCF), not a real signal; skip it rather than let it drag the score.
    if margin is not None and margin >= -300:
        s = _lin(margin, -20, 20)
        pairs.append((s, 1.0)); details.append(_detail("dcf_margin", "Margen de seguridad DCF", margin, s, "%"))

    gmargin = (graham or {}).get("margin_vs_median_pct")
    if gmargin is not None:
        s = _lin(gmargin, -20, 20)
        pairs.append((s, 1.0)); details.append(_detail("graham_margin", "Margen vs. Graham", gmargin, s, "%"))

    target = ((f or {}).get("analyst_consensus") or {}).get("mean_target_price")
    if target is not None and price:
        tpct = round(100 * (target - price) / price, 2)
        s = _lin(tpct, -20, 20)
        pairs.append((s, 0.5)); details.append(_detail("analyst_target", "Objetivo analistas vs. precio", tpct, s, "%"))

    return _combine(pairs), details


def _health(f):
    strength = (f or {}).get("financial_strength") or {}
    cash = (f or {}).get("cash_flow") or {}
    details, pairs = [], []

    de = strength.get("debt_to_equity")
    if de is not None:
        # Yahoo's debtToEquity is always a percentage (150 means 1.5x).
        de_norm = de / 100.0
        s = _lin(de_norm, 2, 0.5)
        pairs.append((s, 1.0)); details.append(_detail("debt_to_equity", "Deuda/Patrimonio", round(de_norm, 2), s))

    cr = strength.get("current_ratio")
    if cr is not None:
        s = _lin(cr, 1, 2)
        pairs.append((s, 1.0)); details.append(_detail("current_ratio", "Ratio corriente", cr, s))

    nd, ebitda = strength.get("net_debt"), cash.get("ebitda")
    if nd is not None and ebitda:
        ratio = nd / ebitda
        s = _lin(ratio, 3, 1)
        pairs.append((s, 1.0)); details.append(_detail("net_debt_ebitda", "Deuda neta/EBITDA", round(ratio, 2), s))

    fcf = cash.get("free_cash_flow")
    if fcf is not None:
        s = 1.0 if fcf > 0 else (-1.0 if fcf < 0 else 0.0)
        pairs.append((s, 1.0)); details.append(_detail("free_cash_flow", "Flujo de caja libre", round(fcf / 1e6), s, " M", fmt="{:,.0f}"))

    return _combine(pairs), details


def _growth(f):
    g = (f or {}).get("growth") or {}
    details, pairs = [], []
    for key, label in (("revenue_yoy_pct", "Crecimiento ventas"), ("earnings_yoy_pct", "Crecimiento beneficio")):
        v = g.get(key)
        s = _lin(v, 0, 10)
        if s is not None:
            pairs.append((s, 1.0)); details.append(_detail(key, label, v, s, "%"))
    return _combine(pairs), details


def _momentum(h, price):
    h = h or {}
    details, pairs = [], []

    sma50, sma200 = h.get("sma_50"), h.get("sma_200")
    p = price if price is not None else h.get("end_price")
    if p and sma50 and sma200:
        if p > sma50 > sma200:
            s = 1.0
        elif p < sma50 < sma200:
            s = -1.0
        else:
            s = 0.0
        pairs.append((s, 1.0)); details.append(_detail("trend", "Precio vs medias 50/200", p, s, fmt="{:.2f}"))

    ret = h.get("period_return_pct")
    s = _lin(ret, -10, 10)
    if s is not None:
        pairs.append((s, 1.0)); details.append(_detail("period_return_pct", "Rentabilidad 12 meses", ret, s, "%"))

    rsi = h.get("rsi_14")
    if rsi is not None:
        s = -0.5 if rsi > 70 else (0.5 if rsi < 30 else 0.0)
        pairs.append((s, 1.0)); details.append(_detail("rsi_14", "RSI", rsi, s))

    return _combine(pairs), details


def _dividend(f, preference):
    d = (f or {}).get("dividend") or {}
    y = d.get("yield_pct")
    payout = d.get("payout_pct")
    details = []

    if not y:  # non-payer
        if preference == "dividend":
            details.append(_detail("dividend_yield", "Dividendo", 0, -0.5, "%"))
            return -0.5, details
        return None, details  # excluded, not penalised

    s = 1.0 if 2 <= y <= 6 else (_lin(y, 0, 2) if y < 2 else max(0.0, 1 - (y - 6) / 6))
    pairs = [(s, 1.0)]
    details.append(_detail("dividend_yield", "Dividendo", y, s, "%"))

    if payout is not None:
        s2 = _lin(payout, 90, 60)
        pairs.append((s2, 1.0)); details.append(_detail("payout_pct", "Payout", payout, s2, "%"))

    return _combine(pairs), details


# --------------------------------------------------------------------- weights


def _adjust_weights(risk_tolerance, horizon, preference):
    w = dict(BASE_WEIGHTS.get(risk_tolerance, BASE_WEIGHTS["moderate"]))

    if preference == "dividend" and w["dividend"] < 15:
        remaining = 15 - w["dividend"]
        for src in ("momentum", "growth"):
            take = min(remaining, w[src])
            w[src] -= take
            remaining -= take
            if remaining <= 0:
                break
        w["dividend"] = 15 - remaining
    elif preference == "growth":
        w["growth"] += w["dividend"]
        w["dividend"] = 0

    if horizon == "short":
        w["momentum"] += 10
        w["valuation"] -= 10
    elif horizon == "long":
        w["momentum"] -= 5
        w["valuation"] += 5

    for k in w:
        w[k] = max(0.0, w[k])
    total = sum(w.values()) or 1.0
    return {k: v * 100.0 / total for k, v in w.items()}


# ----------------------------------------------------------------------- main


def score_stock(quote, fundamentals, historical, dcf=None, graham=None, profile=None, holding=None):
    profile = profile or {}
    risk_tolerance = profile.get("risk_tolerance", "moderate")
    horizon = profile.get("horizon", "long")
    preference = profile.get("preference", "balanced")
    weights = _adjust_weights(risk_tolerance, horizon, preference)

    price = (quote or {}).get("price")
    f = fundamentals if not (fundamentals or {}).get("error") else {}

    pillar_results = {
        "quality": _quality(f),
        "valuation": _valuation(f, dcf, graham, price),
        "health": _health(f),
        "growth": _growth(f),
        "momentum": _momentum(historical, price),
        "dividend": _dividend(f, preference),
    }

    pillars, weighted_sum, weight_total, covered_weight = [], 0.0, 0.0, 0.0
    for key, (subscore, details) in pillar_results.items():
        w = weights.get(key, 0.0)
        if subscore is None:
            pillars.append({
                "key": key, "name_es": PILLAR_NAMES_ES[key], "weight": round(w, 1),
                "subscore": None, "details": [],
            })
            continue
        covered_weight += w
        weighted_sum += subscore * w
        weight_total += w
        pillars.append({
            "key": key, "name_es": PILLAR_NAMES_ES[key], "weight": round(w, 1),
            "subscore": subscore, "details": details,
        })

    x = weighted_sum / weight_total if weight_total else 0.0
    score = int(max(1, min(10, round(1 + (x + 1) * 4.5))))
    coverage_pct = round(covered_weight, 1)

    red_flags = []
    strength = f.get("financial_strength") or {}
    cash = f.get("cash_flow") or {}
    growth = f.get("growth") or {}
    dividend = f.get("dividend") or {}
    hist = historical or {}

    payout, fcf = dividend.get("payout_pct"), cash.get("free_cash_flow")
    if payout is not None and payout > 90 and fcf is not None and fcf <= 0:
        red_flags.append("Payout superior al 90% con flujo de caja libre negativo: dividendo en riesgo real.")

    de = strength.get("debt_to_equity")
    de_norm = de / 100.0 if de is not None else None
    earn_growth = growth.get("earnings_yoy_pct")
    if de_norm is not None and de_norm > 2 and earn_growth is not None and earn_growth < 0:
        red_flags.append("Deuda/patrimonio superior a 2x con beneficio cayendo: apalancamiento peligroso.")

    rev_growth = growth.get("revenue_yoy_pct")
    price_below_200 = price is not None and hist.get("sma_200") is not None and price < hist["sma_200"]
    if price_below_200 and rev_growth is not None and rev_growth < 0 and earn_growth is not None and earn_growth < 0:
        red_flags.append("Precio bajo la media de 200 sesiones con ventas y beneficio cayendo: tendencia bajista de fondo.")

    if red_flags:
        score = min(score, 3)

    pros = [d["reason_es"] for p in pillars for d in p["details"] if d["points"] > 0]
    cons = [d["reason_es"] for p in pillars for d in p["details"] if d["points"] < 0]

    sentiment = "verde" if score >= 7 else "rojo" if score <= 3 else "ambar"

    if holding:
        if score >= 8:
            label = "Mantener o ampliar"
        elif score >= 5:
            label = "Mantener"
        elif score >= 3:
            label = "Vigilar / reducir"
        else:
            label = "Considerar vender"
    else:
        if score >= 9:
            label = "Compra fuerte"
        elif score >= 7:
            label = "Compra moderada"
        elif score >= 5:
            label = "Neutral / esperar"
        elif score >= 3:
            label = "Evitar"
        else:
            label = "Evitar claramente"

    coverage_warning = (
        f"Cobertura de datos baja ({coverage_pct:.0f}%): la nota se apoya en pocos "
        "indicadores disponibles, tomala con cautela."
        if coverage_pct < 50 else None
    )

    return {
        "score": score,
        "label": label,
        "sentiment": sentiment,
        "pillars": pillars,
        "pros": pros,
        "cons": cons,
        "red_flags": red_flags,
        "coverage_pct": coverage_pct,
        "coverage_warning": coverage_warning,
        "weights": {k: round(v, 1) for k, v in weights.items()},
        "profile": {"risk_tolerance": risk_tolerance, "horizon": horizon, "preference": preference},
        "notice": NOTICE,
    }


# ------------------------------------------------------------ recomendaciones


def position_advice(score_result, weight_pct, pnl_pct, profile):
    """Buy/hold/sell/trim guidance for one held position: pure rules over an existing
    score_stock() result plus portfolio weight and P&L. No network, no I/O.

    Returns {"action_key": <english code>, "action_es": <copy>, "reasons_es": [...]}.
    action_key is a stable identifier for the UI (e.g. a badge color); action_es is
    what gets shown.
    """
    profile = profile or {}
    risk_tolerance = profile.get("risk_tolerance", "moderate")
    limit = CONCENTRATION_LIMITS.get(risk_tolerance, CONCENTRATION_LIMITS["moderate"])
    score = (score_result or {}).get("score")
    over_limit = weight_pct is not None and weight_pct > limit

    reasons = []
    if score is not None:
        reasons.append(f"Nota {score}/10.")
    if weight_pct is not None:
        reasons.append(f"Peso en cartera {weight_pct:.1f} % (limite {limit} % para tu perfil {risk_tolerance}).")
    if pnl_pct is not None:
        reasons.append(f"P/L {pnl_pct:+.1f} %.")

    if score is not None and score <= 3:
        action_key, action_es = "sell", "Considerar vender"
    elif score is not None and score <= 4 and pnl_pct is not None and pnl_pct <= -20:
        action_key, action_es = "review", "Revisar tesis (perdida y nota baja)"
    elif over_limit:
        action_key, action_es = "no_add", "No ampliar: concentracion alta"
        if score is not None and score <= 5:
            reasons.append("Nota mediocre y ya por encima del limite de concentracion: valorar reducir.")
    elif score is not None and score >= 8:
        action_key, action_es = "add", "Mantener o ampliar"
    else:
        action_key, action_es = "hold", "Mantener"

    return {"action_key": action_key, "action_es": action_es, "reasons_es": reasons}


def select_ideas(candidates, held_tickers, held_sectors, min_score=7):
    """Rank not-held candidates into "new ideas": pure filter + sort, no I/O.

    `candidates`: iterable of dicts with at least ticker/score/coverage_pct, optionally
    sector (as scored by mercado_server.stock_score / app.servidor._score_ticker).
    Excludes held tickers, coverage_pct < 50 and score < min_score. Sorted by score
    desc, then coverage_pct desc. Tags `diversifies: true` when the sector isn't
    among a non-empty `held_sectors` (with no holdings every idea would "diversify").
    """
    held_tickers = {t.upper() for t in (held_tickers or []) if t}
    held_sectors = {s for s in (held_sectors or []) if s}

    ideas = []
    for c in candidates or []:
        ticker = (c.get("ticker") or "").upper()
        score, coverage = c.get("score"), c.get("coverage_pct")
        if not ticker or ticker in held_tickers:
            continue
        if score is None or score < min_score:
            continue
        if coverage is None or coverage < 50:
            continue
        idea = dict(c)
        idea["ticker"] = ticker
        idea["diversifies"] = bool(held_sectors) and bool(c.get("sector")) and c.get("sector") not in held_sectors
        ideas.append(idea)

    ideas.sort(key=lambda c: (-c["score"], -(c.get("coverage_pct") or 0)))
    return ideas
