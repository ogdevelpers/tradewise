"""Technical indicators, vectorised over Series or wide DataFrames (one column per symbol).

Definitions follow TradingView/Pine so values can be cross-checked on a chart:
RSI, ATR and ADX use Wilder's RMA seeded with a simple average; EMA is seeded
with an SMA of the first ``n`` values. Every function is causal: the value at
row *t* depends only on rows ``<= t``.
"""

from __future__ import annotations

from typing import TypeVar

import numpy as np
import pandas as pd

F = TypeVar("F", pd.Series, pd.DataFrame)


def _check(n: int) -> None:
    if not isinstance(n, (int, np.integer)) or n < 1:
        raise ValueError(f"period must be a positive integer, got {n!r}")


def sma(x: F, n: int) -> F:
    _check(n)
    return x.rolling(n, min_periods=n).mean()


def _seeded_ewm(x: F, n: int, alpha: float) -> F:
    """EWM with ``adjust=False`` whose first value is the SMA of the first ``n`` inputs."""
    _check(n)
    seed_vals = x.rolling(n, min_periods=n).mean()
    valid = seed_vals.notna()
    started = valid.cummax()
    seed = valid & ~started.shift(1, fill_value=False)
    y = x.where(started & ~seed)
    y = y.mask(seed, seed_vals)
    out = y.ewm(alpha=alpha, adjust=False, ignore_na=True).mean()
    return out.where(started)


def ema(x: F, n: int) -> F:
    return _seeded_ewm(x, n, 2.0 / (n + 1))


def rma(x: F, n: int) -> F:
    """Wilder's moving average."""
    return _seeded_ewm(x, n, 1.0 / n)


def rsi(close: F, n: int = 14) -> F:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = rma(gain, n)
    avg_loss = rma(loss, n)
    with np.errstate(divide="ignore", invalid="ignore"):
        rs = avg_gain / avg_loss
        out = 100 - 100 / (1 + rs)
    # No losses in the window -> RSI 100; flat window -> 50.
    out = out.mask((avg_loss == 0) & (avg_gain > 0), 100.0)
    out = out.mask((avg_loss == 0) & (avg_gain == 0), 50.0)
    return out.where(avg_gain.notna() & avg_loss.notna())


def true_range(high: F, low: F, close: F) -> F:
    prev = close.shift(1)
    hl = high - low
    hc = (high - prev).abs()
    lc = (low - prev).abs()
    if isinstance(hl, pd.DataFrame):
        tr = np.fmax(np.fmax(hl, hc), lc)  # fmax ignores the NaN prev close on row 0
    else:
        tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    return tr.where(high.notna() & low.notna())


def atr(high: F, low: F, close: F, n: int = 14) -> F:
    return rma(true_range(high, low, close), n)


def adx(high: F, low: F, close: F, n: int = 14) -> F:
    up = high.diff()
    down = -low.diff()
    plus_dm = up.where((up > down) & (up > 0), 0.0).where(up.notna())
    minus_dm = down.where((down > up) & (down > 0), 0.0).where(down.notna())
    tr = true_range(high, low, close).where(up.notna())
    atr_ = rma(tr, n)
    with np.errstate(divide="ignore", invalid="ignore"):
        plus_di = 100 * rma(plus_dm, n) / atr_
        minus_di = 100 * rma(minus_dm, n) / atr_
        dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di)
    dx = dx.replace([np.inf, -np.inf], np.nan)
    return rma(dx, n)


def macd(close: F, fast: int = 12, slow: int = 26, signal: int = 9) -> tuple[F, F, F]:
    if fast >= slow:
        raise ValueError("fast period must be shorter than slow period")
    line = ema(close, fast) - ema(close, slow)
    sig = ema(line, signal)
    return line, sig, line - sig


def pct_return(close: F, n: int) -> F:
    _check(n)
    return close / close.shift(n) - 1


def rolling_volatility(close: F, n: int = 252) -> F:
    """Annualised standard deviation of daily log returns."""
    _check(n)
    lr = np.log(close / close.shift(1))
    return lr.rolling(n, min_periods=int(n * 0.8)).std() * np.sqrt(252)
