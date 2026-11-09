"""Indicators are checked against straightforward loop implementations of the TradingView formulas."""

import math

import numpy as np
import pandas as pd
import pytest

from tradewise import indicators as ind


def _rand_ohlc(n=400, seed=1):
    rng = np.random.default_rng(seed)
    c = 100 * np.exp(np.cumsum(rng.normal(0, 0.015, n)))
    o = c * (1 + rng.normal(0, 0.003, n))
    h = np.maximum(o, c) * (1 + np.abs(rng.normal(0, 0.005, n)))
    l = np.minimum(o, c) * (1 - np.abs(rng.normal(0, 0.005, n)))
    idx = pd.bdate_range("2020-01-01", periods=n)
    return pd.Series(o, idx), pd.Series(h, idx), pd.Series(l, idx), pd.Series(c, idx)


def ref_rma(x, n):
    out, start = [math.nan] * len(x), None
    for i in range(len(x)):
        if start is None:
            window = x[i - n + 1: i + 1] if i >= n - 1 else []
            if window and not any(math.isnan(v) for v in window):
                out[i], start = sum(window) / n, i
        else:
            out[i] = (out[i - 1] * (n - 1) + x[i]) / n
    return out


def ref_ema(x, n):
    a = 2 / (n + 1)
    out = [math.nan] * len(x)
    for i in range(n - 1, len(x)):
        out[i] = sum(x[:n]) / n if i == n - 1 else a * x[i] + (1 - a) * out[i - 1]
    return out


def ref_rsi(c, n):
    ch = [math.nan] + [c[i] - c[i - 1] for i in range(1, len(c))]
    g = [math.nan if math.isnan(v) else max(v, 0) for v in ch]
    l_ = [math.nan if math.isnan(v) else max(-v, 0) for v in ch]
    ag, al = ref_rma(g, n), ref_rma(l_, n)
    return [math.nan if math.isnan(a) else (100.0 if b == 0 else 100 - 100 / (1 + a / b)) for a, b in zip(ag, al)]


def ref_atr(h, l, c, n):
    tr = [h[0] - l[0]] + [max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])) for i in range(1, len(c))]
    return ref_rma(tr, n)


def test_sma_simple():
    s = pd.Series([1.0, 2, 3, 4, 5])
    assert ind.sma(s, 3).tolist()[2:] == [2.0, 3.0, 4.0]
    assert ind.sma(s, 3).isna().sum() == 2


@pytest.mark.parametrize("n", [5, 14, 20])
def test_ema_matches_reference(n):
    _, _, _, c = _rand_ohlc()
    np.testing.assert_allclose(ind.ema(c, n).to_numpy(), ref_ema(c.tolist(), n), rtol=1e-10, equal_nan=True)


@pytest.mark.parametrize("n", [2, 14])
def test_rsi_matches_reference(n):
    _, _, _, c = _rand_ohlc()
    np.testing.assert_allclose(ind.rsi(c, n).to_numpy(), ref_rsi(c.tolist(), n), rtol=1e-10, equal_nan=True)
    assert ind.rsi(c, n).iloc[: n].isna().all() and ind.rsi(c, n).iloc[n:].notna().all()


def test_atr_matches_reference():
    _, h, l, c = _rand_ohlc()
    np.testing.assert_allclose(ind.atr(h, l, c, 14).to_numpy(), ref_atr(h.tolist(), l.tolist(), c.tolist(), 14),
                               rtol=1e-10, equal_nan=True)


def test_rsi_edge_cases():
    up = pd.Series(np.arange(1.0, 40.0))
    assert ind.rsi(up, 14).dropna().eq(100).all()
    flat = pd.Series(np.full(40, 5.0))
    assert ind.rsi(flat, 14).dropna().eq(50).all()


def test_wide_frame_equals_per_column_with_staggered_listing():
    _, h, l, c = _rand_ohlc()
    late = c.copy()
    late.iloc[:120] = np.nan  # stock listed later
    wide = pd.DataFrame({"A": c, "B": late})
    for fn in (lambda x: ind.ema(x, 20), lambda x: ind.rsi(x, 14), lambda x: ind.sma(x, 50)):
        w = fn(wide)
        np.testing.assert_allclose(w["A"], fn(c), equal_nan=True)
        np.testing.assert_allclose(w["B"], fn(late.dropna()).reindex(c.index), equal_nan=True)
    hw = pd.DataFrame({"A": h, "B": h.where(late.notna())})
    lw = pd.DataFrame({"A": l, "B": l.where(late.notna())})
    aw = ind.atr(hw, lw, wide, 14)
    np.testing.assert_allclose(aw["A"], ind.atr(h, l, c, 14), equal_nan=True)
    np.testing.assert_allclose(aw["B"], ind.atr(h[late.notna()], l[late.notna()], late.dropna(), 14).reindex(c.index),
                               equal_nan=True)


@pytest.mark.parametrize("cut", [60, 150, 399])
def test_indicators_are_causal(cut):
    _, h, l, c = _rand_ohlc()
    full = {"ema": ind.ema(c, 20), "rsi": ind.rsi(c, 14), "atr": ind.atr(h, l, c, 14), "adx": ind.adx(h, l, c, 14),
            "macd": ind.macd(c)[0]}
    part = {"ema": ind.ema(c[:cut], 20), "rsi": ind.rsi(c[:cut], 14), "atr": ind.atr(h[:cut], l[:cut], c[:cut], 14),
            "adx": ind.adx(h[:cut], l[:cut], c[:cut], 14), "macd": ind.macd(c[:cut])[0]}
    for k in full:
        np.testing.assert_allclose(full[k].iloc[:cut], part[k], equal_nan=True, err_msg=k)


def test_adx_range_and_macd_validation():
    _, h, l, c = _rand_ohlc()
    a = ind.adx(h, l, c, 14).dropna()
    assert len(a) > 300 and a.between(0, 100).all()
    with pytest.raises(ValueError):
        ind.macd(c, 26, 12)
    with pytest.raises(ValueError):
        ind.sma(c, 0)
