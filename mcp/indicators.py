"""Technical indicators — vendored from talipp (MIT), see `FUENTE.md`.

Only the indicators SeaLock needs: SMA, EMA, RSI, ATR, Bollinger and MACD. The
formulas and the incremental semantics follow talipp 2.7.0, but its framework
(the generic `Indicator` base with sampling/input modifiers, the `MAType`
factory, OHLCV bookkeeping, remove/purge) is trimmed away: `historical()`
computes a whole window in one pass, so the only thing we keep from that
framework is what the math needs.

Semantics kept from talipp (they are what makes the values comparable):

  - An indicator is fed one input at a time and always appends an output,
    `None` until it has enough data (`period` inputs for SMA/EMA/StdDev,
    `period + 1` for RSI — it needs `period` changes — and `period` for ATR).
  - `SMA` after the seed uses the rolling form
    `prev - (x[n-period] - x[n]) / period`, which is algebraically the same
    as the plain mean.
  - `RSI` seeds the average gain/loss with the first `period - 1` changes and
    then applies Wilder's smoothing `(avg * (period - 1) + change) / period`.
    That is the standard Wilder RSI, and it is **not** the plain average of
    the last 14 returns that `historical()` used to compute (see FUENTE.md).
  - `EMA` seeds with the SMA of its first `period` values, then
    `2/(period+1) * x + (1 - 2/(period+1)) * prev`.
  - `ATR` uses the true range `max(high-low, |high-close_prev|, |low-close_prev|)`
    and Wilder smoothing, fed with `OHLCV` records (we keep only the three
    fields we use: high, low, close).
  - `Bollinger` = SMA(period) ± mult * StdDev(period) (population deviation,
    divided by `period`, as talipp does).
  - `MACD` = EMA(fast) - EMA(slow), signal = EMA(signal) of the MACD line.

Standard parameters used by `historical()`: RSI 14, ATR 14, Bollinger 20 x 2,
MACD 12/26/9.

Pure and offline: stdlib only, no I/O, no network.
"""

from collections import namedtuple

__all__ = ["OHLCV", "SMA", "EMA", "RSI", "ATR", "StdDev", "BB", "MACD"]


OHLCV = namedtuple("OHLCV", ["high", "low", "close"])
BBVal = namedtuple("BBVal", ["lb", "cb", "ub"])
MACDVal = namedtuple("MACDVal", ["macd", "signal", "histogram"])


def _has(values, window, exact=False):
    """talipp's `has_valid_values`: `values[-window]` exists and is not None.

    With `exact=True` the value right before the window must be None (or the
    window must fill the whole sequence), i.e. the window has just been
    completed. Inputs in SeaLock never contain None, so `exact` degenerates
    to `len(values) == window`.
    """
    if not exact:
        return len(values) >= window and values[-window] is not None
    if len(values) == window:
        return values[-window] is not None
    return (
        len(values) > window
        and values[-window] is not None
        and values[-window - 1] is None
    )


class Indicator:
    """Incremental base: `add()` appends one input and one output.

    Sub-indicators (Bollinger's SMA and StdDev, MACD's EMAs) are fed the same
    input *before* the own output is computed, which is what talipp does and
    what makes a composed indicator see the current input in its parts.
    """

    def __init__(self, input_values=None):
        self.input_values = []
        self.output_values = []
        self._sub_indicators = []
        self._init_components()
        if input_values is not None:
            self.add_all(input_values)

    def _init_components(self):
        """Hook for subclasses to set their period and sub-indicators."""

    def add_sub_indicator(self, indicator):
        self._sub_indicators.append(indicator)

    def add(self, value):
        for sub in self._sub_indicators:
            sub.add(value)
        self.input_values.append(value)
        new = self._calculate_new_value()
        if new is None and self.output_values:
            # talipp repeats the previous output instead of going back to None
            # once a value has been produced.
            new = self.output_values[-1]
        self.output_values.append(new)
        return new

    def add_all(self, values):
        for value in values:
            self.add(value)
        return self

    def _calculate_new_value(self):
        raise NotImplementedError

    def last(self):
        """Latest output, or None while there is not enough data."""
        return self.output_values[-1] if self.output_values else None

    def __len__(self):
        return len(self.output_values)

    def __getitem__(self, index):
        return self.output_values[index]

    def __iter__(self):
        return iter(self.output_values)

    def __str__(self):
        return str(self.output_values)


class SMA(Indicator):
    """Simple moving average."""

    def __init__(self, period, input_values=None):
        self.period = period
        super().__init__(input_values)

    def _calculate_new_value(self):
        if _has(self.input_values, self.period + 1):
            return (
                self.output_values[-1]
                - (self.input_values[-self.period - 1] - self.input_values[-1])
                / float(self.period)
            )
        if _has(self.input_values, self.period, exact=True):
            return float(sum(self.input_values[-self.period :])) / self.period
        return None


class EMA(Indicator):
    """Exponential moving average (SMA seed, then recursive smoothing)."""

    def __init__(self, period, input_values=None):
        self.period = period
        super().__init__(input_values)

    def _calculate_new_value(self):
        if not _has(self.input_values, self.period):
            return None
        if _has(self.input_values, self.period, exact=True):
            return sum(self.input_values[-self.period :]) / self.period
        mult = 2.0 / (self.period + 1.0)
        return mult * self.input_values[-1] + (1.0 - mult) * self.output_values[-1]


class RSI(Indicator):
    """Relative Strength Index with Wilder smoothing (talipp's seeding)."""

    def __init__(self, period, input_values=None):
        self.period = period
        self.avg_gain = []
        self.avg_loss = []
        super().__init__(input_values)

    def _calculate_new_value(self):
        if not _has(self.input_values, self.period + 1):
            return None

        if _has(self.input_values, self.period + 1, exact=True):
            changes = [
                self.input_values[i] - self.input_values[i - 1]
                for i in range(1, self.period)
            ]
            self.avg_gain.append(sum(c for c in changes if c > 0) / (self.period - 1))
            self.avg_loss.append(sum(-c for c in changes if c < 0) / (self.period - 1))

        change = self.input_values[-1] - self.input_values[-2]
        gain = change if change > 0 else 0.0
        loss = -change if change < 0 else 0.0
        self.avg_gain.append((self.avg_gain[-1] * (self.period - 1) + gain) / self.period)
        self.avg_loss.append((self.avg_loss[-1] * (self.period - 1) + loss) / self.period)

        if self.avg_loss[-1] == 0:
            return 100.0
        rs = self.avg_gain[-1] / self.avg_loss[-1]
        return 100.0 - (100.0 / (1.0 + rs))


class ATR(Indicator):
    """Average True Range (Wilder smoothing) over `OHLCV` inputs."""

    def __init__(self, period, input_values=None):
        self.period = period
        self.tr = []
        super().__init__(input_values)

    def _calculate_new_value(self):
        high = self.input_values[-1].high
        low = self.input_values[-1].low

        if _has(self.input_values, 1, exact=True):
            self.tr.append(high - low)
        else:
            prev_close = self.input_values[-2].close
            self.tr.append(
                max(high - low, abs(high - prev_close), abs(low - prev_close))
            )

        if len(self.input_values) < self.period:
            return None
        if len(self.input_values) == self.period:
            return sum(self.tr) / self.period
        return (self.output_values[-1] * (self.period - 1) + self.tr[-1]) / self.period


class StdDev(Indicator):
    """Population standard deviation (divided by `period`, as talipp)."""

    def __init__(self, period, input_values=None):
        self.period = period
        super().__init__(input_values)

    def _calculate_new_value(self):
        if not _has(self.input_values, self.period):
            return None
        window = self.input_values[-self.period :]
        mean = sum(window) / self.period
        return (sum((item - mean) ** 2 for item in window) / self.period) ** 0.5


class BB(Indicator):
    """Bollinger Bands: SMA(period) ± std_dev_mult * StdDev(period)."""

    def __init__(self, period, std_dev_mult=2.0, input_values=None):
        self.period = period
        self.std_dev_mult = std_dev_mult
        super().__init__(input_values)

    def _init_components(self):
        # Called by the base class before `input_values` is fed.
        self.central_band = SMA(self.period)
        self.std_dev = StdDev(self.period)
        self.add_sub_indicator(self.central_band)
        self.add_sub_indicator(self.std_dev)

    def _calculate_new_value(self):
        if not _has(self.input_values, self.period):
            return None
        mid = self.central_band[-1]
        dev = self.std_dev_mult * self.std_dev[-1]
        return BBVal(mid - dev, mid, mid + dev)


class MACD(Indicator):
    """MACD: EMA(fast) - EMA(slow), signal = EMA(signal) over the MACD line."""

    def __init__(self, fast_period=12, slow_period=26, signal_period=9, input_values=None):
        self.fast_period = fast_period
        self.slow_period = slow_period
        self.signal_period = signal_period
        super().__init__(input_values)

    def _init_components(self):
        self.ma_fast = EMA(self.fast_period)
        self.ma_slow = EMA(self.slow_period)
        self.signal_line = EMA(self.signal_period)
        self.add_sub_indicator(self.ma_fast)
        self.add_sub_indicator(self.ma_slow)

    def _calculate_new_value(self):
        if not _has(self.ma_fast, 1) or not _has(self.ma_slow, 1):
            return None
        macd = self.ma_fast[-1] - self.ma_slow[-1]
        self.signal_line.add(macd)
        signal = self.signal_line[-1] if _has(self.signal_line, 1) else None
        histogram = macd - signal if (macd is not None and signal is not None) else None
        return MACDVal(macd, signal, histogram)
