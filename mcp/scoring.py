"""Pure scoring engine: turns quote/fundamentals/historical/valuation data into a
1-10 buy/sell score adjusted to an investor profile. No network, no I/O — every
input is a plain dict already fetched by the caller (mercado_server.stock_score or
app/servidor.py's stock_detail).

Sources behind the seven pillars: Buffett (ROE, low debt), Graham (defensive
criteria), Lynch (PEG), Greenblatt (earnings yield spirit via EV/EBITDA and ROIC
proxies), Piotroski (profitability + leverage signals), AQR quality-minus-junk
(margins, ROA), Jegadeesh-Titman (price momentum) and the last fiscal quarters
(recent results, not the last full year). Analyst consensus is used only
as a weak tiebreak in valuation because sell-side targets skew optimistic.

NOT investment advice: fixed rules over public data, nothing more.
"""

BASE_WEIGHTS = {
    "moderate":     {"quality": 25, "valuation": 25, "health": 15, "growth": 8,  "momentum": 7,  "results": 15, "dividend": 5},
    "conservative": {"quality": 25, "valuation": 20, "health": 25, "growth": 5,  "momentum": 5,  "results": 10, "dividend": 10},
    "aggressive":   {"quality": 20, "valuation": 20, "health": 10, "growth": 15, "momentum": 20, "results": 15, "dividend": 0},
}

PILLAR_NAMES_ES = {
    "quality": "Calidad",
    "valuation": "Valoracion",
    "health": "Solidez financiera",
    "growth": "Crecimiento",
    "momentum": "Momentum",
    "results": "Resultados recientes",
    "dividend": "Dividendo",
}

NOTICE = (
    "Puntuacion mecanica basada en reglas fijas sobre datos publicos; no es "
    "asesoramiento de un profesional con licencia. La decision es tuya."
)

# Max weight (% of portfolio base-currency value) a single position may reach before
# "No ampliar" kicks in, by risk tolerance. Used by position_advice() for Recomendaciones.
CONCENTRATION_LIMITS = {"conservative": 20, "moderate": 25, "aggressive": 35}

# Hidden Gems thresholds (HU-13), named so they're not magic numbers scattered in
# select_gems().
GEM_MIN_SCORE = 7
GEM_MIN_COVERAGE_PCT = 50
GEM_MIN_QUARTERS_AVAILABLE = 2
GEM_MIN_MARKET_CAP_EUR = 300_000_000
GEM_MAX_MARKET_CAP_EUR = 10_000_000_000


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


def _valuation(f, dcf, graham, price, estimates=None):
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

    # Only the revision balance, never the consensus level: how many analysts moved
    # their estimate up or down in 30 days is sentiment, and it carries much less of the
    # documented sell-side optimism than the absolute forecast does. Weight 0.5, same as
    # the target price, so it can never decide the note on its own.
    revisions = (estimates or {}).get("revisions_net_30d")
    if revisions is not None:
        s = _lin(revisions, -8, 8)
        pairs.append((s, 0.5))
        details.append(_detail("estimate_revisions", "Balance de revisiones (30 d)", revisions, s,
                                "", fmt="{:+d}"))

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


def _results(q):
    """Recent results: the last fiscal quarters, not the last full year.

    `q` is the quarterly() result (or {}). Every metric skips itself when its data
    is missing, so a company with 2 of 3 quarters still gets a partial pillar.
    """
    q = q or {}
    quarters = [x for x in (q.get("quarters") or []) if x]
    details, pairs = [], []
    if not quarters:
        return None, details  # available == 0: pillar excluded, weight redistributed

    rev = [x["revenue_yoy_pct"] for x in quarters if x.get("revenue_yoy_pct") is not None]
    if rev:
        v = round(sum(rev) / len(rev), 2)
        s = _lin(v, -5, 15)
        pairs.append((s, 1.0))
        details.append(_detail("rev_yoy_avg", "Ventas (interanual)", v, s, "%"))

    # BPA when reported, net income otherwise: same idea, one metric either way.
    eps = [x.get("eps_yoy_pct") if x.get("eps_yoy_pct") is not None else x.get("net_income_yoy_pct")
           for x in quarters]
    eps = [v for v in eps if v is not None]
    if eps:
        v = round(sum(eps) / len(eps), 2)
        s = _lin(v, -10, 20)
        pairs.append((s, 1.0))
        details.append(_detail("eps_yoy_avg", "Beneficio (interanual)", v, s, "%"))

    margins = [x["net_margin_pct"] for x in quarters if x.get("net_margin_pct") is not None]
    if len(margins) >= 2:  # newest minus oldest, quarters arrive newest first
        v = round(margins[0] - margins[-1], 2)
        s = _lin(v, -3, 3)
        pairs.append((s, 1.0))
        details.append(_detail("margin_trend", "Margen neto: del mas nuevo al mas viejo", v, s, " p.p."))

    revs = [x.get("revenue") for x in quarters]
    steps = [(new, old) for new, old in zip(revs, revs[1:]) if new is not None and old is not None]
    if steps:
        v = round(sum(1 for new, old in steps if new > old) / len(steps), 2)
        s = _lin(v, 0, 1)
        pairs.append((s, 1.0))
        details.append(_detail("consistency", "Trimestres con ventas al alza", v, s, fmt="{:.0%}"))

    surprise = [v for v in (q.get("earnings_surprise_pct") or []) if v is not None]
    if surprise:
        v = round(sum(surprise) / len(surprise), 2)
        s = _lin(v, -5, 5)
        pairs.append((s, 1.0))
        details.append(_detail("surprise", "Sorpresa del BPA", v, s, "%"))

    if (q.get("available") or len(quarters)) < 3:
        # points 0: this line explains the pillar, it must not move it.
        details.insert(0, {
            "metric": "quarters_available",
            "value": q.get("available") or len(quarters),
            "points": 0.0,
            "reason_es": "Datos de menos de 3 trimestres: el pilar se calcula con lo que hay",
        })
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


def _label_for(score, holding=False):
    """UI label for a 1-10 score. A position already owned reads differently: the
    question is no longer "should I buy" but "should I keep it"."""
    if holding:
        if score >= 8:
            return "Mantener o ampliar"
        if score >= 5:
            return "Mantener"
        if score >= 3:
            return "Vigilar / reducir"
        return "Considerar vender"
    if score >= 9:
        return "Compra fuerte"
    if score >= 7:
        return "Compra moderada"
    if score >= 5:
        return "Neutral / esperar"
    if score >= 3:
        return "Evitar"
    return "Evitar claramente"


def _score_from_subscores(pillar_data, weights, red_flags, holding=False):
    """Weighting/scoring step shared by score_stock() (fresh computation) and
    recompute_from_payload() (profile change on a stored payload, no network): given
    each pillar's (subscore, details) and the profile's weights, combine them into a
    score, coverage and pros/cons. There is exactly one implementation of this step.

    `pillar_data`: {key: (subscore_or_None, details)}.
    """
    pillars, weighted_sum, weight_total, covered_weight = [], 0.0, 0.0, 0.0
    for key, (subscore, details) in pillar_data.items():
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
            "subscore": subscore, "details": details or [],
        })

    x = weighted_sum / weight_total if weight_total else 0.0
    score = int(max(1, min(10, round(1 + (x + 1) * 4.5))))
    coverage_pct = round(covered_weight, 1)
    if red_flags:
        score = min(score, 3)

    pros = [d["reason_es"] for p in pillars for d in p["details"] if d["points"] > 0]
    cons = [d["reason_es"] for p in pillars for d in p["details"] if d["points"] < 0]
    sentiment = "verde" if score >= 7 else "rojo" if score <= 3 else "ambar"
    label = _label_for(score, holding=holding)
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
        "coverage_pct": coverage_pct,
        "coverage_warning": coverage_warning,
    }


def dividend_variants(fundamentals):
    """Both dividend-pillar variants (preference == "dividend" vs. anything else): the
    Hidden Gems cache writes a payload before it knows which profile will read it back,
    so both are stored once at scan time and recompute_from_payload() picks the right
    one with no network call."""
    d_pref, det_pref = _dividend(fundamentals, "dividend")
    d_other, det_other = _dividend(fundamentals, "balanced")
    return {
        "dividend": {"subscore": d_pref, "details": det_pref},
        "other": {"subscore": d_other, "details": det_other},
    }


def recompute_from_payload(payload, profile=None):
    """Recompute score/label/sentiment for a different investor profile from a stored
    payload (mcp/finanzas_server.py score_cache.payload), no network. Pillar subscores
    and details are fixed data already computed at scan time; only the weights (and
    which dividend variant applies) depend on the profile. Reuses
    _score_from_subscores, so there is one scoring implementation either way.
    """
    profile = profile or {}
    risk_tolerance = profile.get("risk_tolerance", "moderate")
    horizon = profile.get("horizon", "long")
    preference = profile.get("preference", "balanced")
    weights = _adjust_weights(risk_tolerance, horizon, preference)

    variant = "dividend" if preference == "dividend" else "other"
    pillar_data = {}
    for p in payload.get("pillars") or []:
        if p["key"] == "dividend":
            dv = (payload.get("dividend_variants") or {}).get(variant) or {}
            pillar_data["dividend"] = (dv.get("subscore"), dv.get("details") or [])
        else:
            pillar_data[p["key"]] = (p.get("subscore"), p.get("details") or [])

    red_flags = payload.get("red_flags") or []
    result = _score_from_subscores(pillar_data, weights, red_flags, holding=False)
    quarters_available = payload.get("quarters_available")

    notice = NOTICE
    if quarters_available is not None and quarters_available < 3:
        notice += (
            f" Ademas, la nota se apoya en {quarters_available} de 3 trimestres fiscales"
            + (f" ({payload['quarters_note_es']})" if payload.get("quarters_note_es") else "")
            + "."
        )

    return {
        **result,
        "red_flags": red_flags,
        "is_holding": False,
        "quarters_available": quarters_available,
        "quarters_note_es": payload.get("quarters_note_es"),
        "weights": {k: round(v, 1) for k, v in weights.items()},
        "profile": {"risk_tolerance": risk_tolerance, "horizon": horizon, "preference": preference},
        "notice": notice,
    }


def score_stock(quote, fundamentals, historical, dcf=None, graham=None, profile=None,
                holding=None, quarterly=None, estimates=None):
    profile = profile or {}
    risk_tolerance = profile.get("risk_tolerance", "moderate")
    horizon = profile.get("horizon", "long")
    preference = profile.get("preference", "balanced")
    weights = _adjust_weights(risk_tolerance, horizon, preference)

    price = (quote or {}).get("price")
    f = fundamentals if not (fundamentals or {}).get("error") else {}

    pillar_data = {
        "quality": _quality(f),
        "valuation": _valuation(f, dcf, graham, price, estimates),
        "health": _health(f),
        "growth": _growth(f),
        "momentum": _momentum(historical, price),
        "results": _results(quarterly),
        "dividend": _dividend(f, preference),
    }

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

    q = quarterly or {}
    quarters = [x for x in (q.get("quarters") or []) if x]
    quarters_available = q.get("available")
    if quarters_available is None:
        quarters_available = len(quarters)
    rev_yoy = [x.get("revenue_yoy_pct") for x in quarters]
    rev_yoy_known = [v for v in rev_yoy if v is not None]
    # Needs all three quarters with revenue data: a hole is not evidence of decline.
    if len(quarters) == 3 and len(rev_yoy_known) == 3:
        _results_details = _results(q)[1]
        margin_trend = next(
            (d["value"] for d in _results_details if d["metric"] == "margin_trend"), None
        )
        if all(v < 0 for v in rev_yoy_known) and margin_trend is not None and margin_trend < 0:
            red_flags.append(
                "Ventas cayendo interanualmente en los tres trimestres y con el margen neto a la baja: "
                "deterioro constante."
            )

    notice = NOTICE
    if quarters_available < 3:
        notice += (
            f" Ademas, la nota se apoya en {quarters_available} de 3 trimestres fiscales"
            + (f" ({q['note_es']})" if q.get("note_es") else "")
            + "."
        )

    combined = _score_from_subscores(pillar_data, weights, red_flags, holding=bool(holding))

    return {
        **combined,
        "red_flags": red_flags,
        "is_holding": bool(holding),
        "quarters_available": quarters_available,
        "quarters_note_es": q.get("note_es"),
        "weights": {k: round(v, 1) for k, v in weights.items()},
        "profile": {"risk_tolerance": risk_tolerance, "horizon": horizon, "preference": preference},
        "notice": notice,
    }


# ------------------------------------------------------------------- revisions


def apply_review(score_result, review):
    """Final score = mechanical score + Claude's bounded adjustment.

    Pure. `review` is a score_reviews row (or None). An expired or stale review is not
    applied, only reported, so a decision made on old data can't move today's score.
    The red_flags cap applies AFTER the adjustment: a review never buys its way out
    of a warning.

    Returns a new dict: {score, final_score, label, sentiment, review, review_applied}.
    """
    out = {
        "score": score_result["score"],
        "final_score": score_result["score"],
        "label": score_result["label"],
        "sentiment": score_result["sentiment"],
        "review": review,
        "review_applied": False,
    }
    if not review:
        return out

    out["review_applied"] = review.get("status") == "active"
    if not out["review_applied"]:
        return out

    final = max(1.0, min(10.0, round(score_result["score"] + float(review["adjustment"]), 1)))
    if score_result.get("red_flags"):
        final = min(final, 3.0)
    rating = int(max(1, min(10, round(final))))
    out["final_score"] = final
    out["label"] = _label_for(rating, holding=score_result.get("is_holding"))
    out["sentiment"] = "verde" if rating >= 7 else "rojo" if rating <= 3 else "ambar"
    return out


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
    # final_score = mechanical + Claude's review, so decisions follow what the user sees.
    score = (score_result or {}).get("final_score")
    if score is None:
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

    `candidates`: iterable of dicts with at least ticker/final_score (or score) and
    coverage_pct, optionally sector (as scored by mercado_server.stock_score /
    app.servidor._score_ticker). Excludes held tickers, coverage_pct < 50 and
    final_score < min_score. Sorted by final_score desc, then coverage_pct desc. Tags
    `diversifies: true` when the sector isn't among a non-empty `held_sectors` (with no
    holdings every idea would "diversify").
    """
    held_tickers = {t.upper() for t in (held_tickers or []) if t}
    held_sectors = {s for s in (held_sectors or []) if s}

    ideas = []
    for c in candidates or []:
        ticker = (c.get("ticker") or "").upper()
        score = c.get("final_score")
        if score is None:
            score = c.get("score")
        coverage = c.get("coverage_pct")
        if not ticker or ticker in held_tickers:
            continue
        if score is None or score < min_score:
            continue
        if coverage is None or coverage < 50:
            continue
        idea = dict(c)
        idea["ticker"] = ticker
        idea["final_score"] = score
        idea["diversifies"] = bool(held_sectors) and bool(c.get("sector")) and c.get("sector") not in held_sectors
        ideas.append(idea)

    ideas.sort(key=lambda c: (-c["final_score"], -(c.get("coverage_pct") or 0)))
    return ideas


# ------------------------------------------------------------------ hidden gems


def _main_reason_es(pillars):
    """Pillar with the best weighted subscore (weight * subscore), in Spanish: the
    headline reason a gem made the cut."""
    scored = [(p["weight"] * p["subscore"], p["name_es"]) for p in (pillars or []) if p.get("subscore") is not None]
    if not scored:
        return None
    return max(scored, key=lambda x: x[0])[1]


def select_gems(candidates, held_tickers, held_sectors):
    """Hidden Gems (HU-13): reuses select_ideas() for the held/coverage/min-score
    filter and the `diversifies` flag, then narrows to the gem-specific rules and
    re-sorts. Pure, no I/O.

    `candidates`: iterable of dicts like select_ideas() plus `red_flags` (list),
    `quarters_available` (int or None), `market_cap_eur` (float or None), `pillars`
    (score_stock()-shaped list) and optionally `num_analysts`.

    A gem satisfies all of: final_score >= GEM_MIN_SCORE; coverage_pct >=
    GEM_MIN_COVERAGE_PCT (already enforced by select_ideas); no red_flags;
    quarters_available >= GEM_MIN_QUARTERS_AVAILABLE; market_cap_eur within
    [GEM_MIN_MARKET_CAP_EUR, GEM_MAX_MARKET_CAP_EUR] (missing -> excluded); Valuation
    pillar subscore > 0 (not expensive).

    Order: final_score desc, then Valuation subscore desc, then num_analysts asc
    (missing goes last: fewer analysts means less followed, more "hidden"). Each gem
    carries `main_reason_es`: the pillar with the best weighted subscore.
    """
    ideas = select_ideas(candidates, held_tickers, held_sectors, min_score=GEM_MIN_SCORE)

    gems = []
    for c in ideas:
        if c.get("red_flags"):
            continue
        qa = c.get("quarters_available")
        if qa is None or qa < GEM_MIN_QUARTERS_AVAILABLE:
            continue
        mcap = c.get("market_cap_eur")
        if mcap is None or not (GEM_MIN_MARKET_CAP_EUR <= mcap <= GEM_MAX_MARKET_CAP_EUR):
            continue
        valuation = next((p for p in (c.get("pillars") or []) if p.get("key") == "valuation"), None)
        valuation_subscore = valuation["subscore"] if valuation else None
        if valuation_subscore is None or valuation_subscore <= 0:
            continue

        gem = dict(c)
        gem["valuation_subscore"] = valuation_subscore
        gem["main_reason_es"] = _main_reason_es(c.get("pillars"))
        gems.append(gem)

    def sort_key(g):
        analysts = g.get("num_analysts")
        return (-g["final_score"], -g["valuation_subscore"], analysts is None, analysts or 0)

    gems.sort(key=sort_key)
    return gems
