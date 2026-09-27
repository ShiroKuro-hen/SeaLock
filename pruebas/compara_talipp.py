"""Compares `mcp/indicators.py` against the real talipp (optional, not part of the suites).

`talipp` is NOT a SeaLock dependency: this script only re-verifies the HU-23
equivalence against the original library whenever `indicators.py` changes. The
suites (`prueba_humo.py`, `prueba_web.py`) do not import it.

Usage:

    python -m pip install --target %TEMP%\\talipp_lib talipp==2.7.0
    python pruebas/compara_talipp.py
"""

import os
import random
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "mcp"))

try:
    import talipp.indicators as tal
    from talipp.ohlcv import OHLCV as TalippOHLCV
except ImportError:
    sys.exit("talipp not installed: `python -m pip install --target <dir> talipp==2.7.0` "
             "and add <dir> to PYTHONPATH.")

import indicators as mine  # noqa: E402

failures = 0


def random_series(n=300, seed=7):
    random.seed(seed)
    closes, px = [], 100.0
    for _ in range(n):
        px *= 1 + random.uniform(-0.04, 0.045)
        closes.append(px)
    return closes


def deterministic_series(n=260):
    # Integers only, no `math`: identical output on every platform.
    return [round(100 + ((i * 13) % 29) - 14 + (i % 5) * 0.8 + i * 0.12, 2)
            for i in range(n)]


def candles(closes):
    ours, theirs = [], []
    prev = closes[0]
    for c in closes:
        hi = round(max(prev, c) * 1.012, 4)
        lo = round(min(prev, c) * 0.988, 4)
        ours.append(mine.OHLCV(hi, lo, c))
        theirs.append(TalippOHLCV(open=prev, high=hi, low=lo, close=c, volume=1000))
        prev = c
    return ours, theirs


def compare(name, a, b):
    """Compare two output series, composite values included."""
    global failures
    if len(a) != len(b):
        print(f"FAIL {name}: lengths {len(a)} != {len(b)}")
        failures += 1
        return
    mismatches = 0
    for i, (x, y) in enumerate(zip(a, b)):
        if hasattr(x, "macd"):
            x, y = (x.macd, x.signal, x.histogram), (y.macd, y.signal, y.histogram)
        elif hasattr(x, "lb"):
            x, y = (x.lb, x.cb, x.ub), (y.lb, y.cb, y.ub)
        elif not isinstance(x, tuple):
            x, y = (x,), (y,)
        for u, v in zip(x, y):
            if u is None or v is None:
                mismatches += u is not None or v is not None
            elif abs(u - v) > 1e-9:
                mismatches += 1
                if mismatches < 4:
                    print(f"   [{i}] {u!r} != {v!r}")
    if mismatches:
        print(f"FAIL {name}: {mismatches} values differ out of {len(a)}")
        failures += 1
    else:
        print(f"OK    {name}: {len(a)} values identical")


def main():
    global failures
    for name, closes in (("random series", random_series()),
                         ("deterministic series", deterministic_series())):
        print(f"-- {name} --")
        ours, theirs = candles(closes)
        compare("SMA 50", mine.SMA(50, closes), tal.SMA(50, input_values=closes))
        compare("SMA 200", mine.SMA(200, closes), tal.SMA(200, input_values=closes))
        compare("EMA 26", mine.EMA(26, closes), tal.EMA(26, input_values=closes))
        compare("EMA 9", mine.EMA(9, closes), tal.EMA(9, input_values=closes))
        compare("RSI 14", mine.RSI(14, closes), tal.RSI(14, input_values=closes))
        compare("ATR 14", mine.ATR(14, ours), tal.ATR(14, input_values=theirs))
        compare("StdDev 20", mine.StdDev(20, closes), tal.StdDev(20, input_values=closes))
        compare("BB 20x2", mine.BB(20, 2.0, closes), tal.BB(20, 2.0, input_values=closes))
        compare("MACD 12/26/9", mine.MACD(12, 26, 9, closes),
                tal.MACD(12, 26, 9, input_values=closes))

        # SMA must still be exactly what historical() computes by hand.
        simple = [round(sum(closes[-n:]) / n, 4) for n in (50, 200)]
        averaged = [round(mine.SMA(n, closes).last(), 4) for n in (50, 200)]
        if simple == averaged:
            print(f"OK    SMA vs historical() simple mean: {averaged}")
        else:
            print(f"FAIL SMA vs simple mean: {simple} != {averaged}")
            failures += 1

        # RSI does change (Wilder vs simple mean): reported here, decided in prueba_humo.
        returns = [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes))]
        window = returns[-14:]
        gain = sum(r for r in window if r > 0) / 14
        loss = sum(-r for r in window if r < 0) / 14
        old = 100.0 if loss == 0 else round(100 - 100 / (1 + gain / loss), 1)
        new = round(mine.RSI(14, closes).last(), 1)
        print(f"     RSI: simple mean (old) = {old} | Wilder (talipp) = {new}")

    print(f"FAILURES: {failures}" if failures else "ALL CORRECT")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
