"""Performance statistics. Every function copes with empty or flat inputs."""

from __future__ import annotations

import math
from typing import Iterable

import numpy as np
import pandas as pd

from .engine import SimState, Trade

TRADING_DAYS = 252


def equity_series(state: SimState) -> pd.Series:
    if not state.equity:
        return pd.Series(dtype="float64")
    dates, eq, _ = zip(*state.equity)
    return pd.Series(eq, index=pd.DatetimeIndex(pd.to_datetime(list(dates))), name="equity", dtype="float64")


def _num(x) -> float | None:
    if x is None:
        return None
    x = float(x)
    return round(x, 6) if math.isfinite(x) else None


def curve_stats(equity: pd.Series, risk_free: float = 0.065) -> dict:
    """CAGR, drawdown, volatility, Sharpe and Sortino from a daily equity curve."""
    eq = equity.dropna()
    if len(eq) < 2 or eq.iloc[0] <= 0:
        return {"start": None, "end": None, "total_return": None, "cagr": None, "max_drawdown": None,
                "volatility": None, "sharpe": None, "sortino": None, "calmar": None}
    rets = eq.pct_change().dropna()
    years = max((eq.index[-1] - eq.index[0]).days / 365.25, 1 / 365.25)
    total = eq.iloc[-1] / eq.iloc[0] - 1
    cagr = (eq.iloc[-1] / eq.iloc[0]) ** (1 / years) - 1 if eq.iloc[-1] > 0 else -1.0
    dd = eq / eq.cummax() - 1
    vol = rets.std(ddof=1) * math.sqrt(TRADING_DAYS) if len(rets) > 1 else float("nan")
    excess = rets - risk_free / TRADING_DAYS
    sd = excess.std(ddof=1)
    sharpe = excess.mean() / sd * math.sqrt(TRADING_DAYS) if sd and sd > 0 else float("nan")
    downside = excess.clip(upper=0)
    dsd = math.sqrt((downside ** 2).mean()) if len(downside) else 0.0
    sortino = excess.mean() / dsd * math.sqrt(TRADING_DAYS) if dsd > 0 else float("nan")
    mdd = dd.min()
    return {
        "start": eq.index[0].strftime("%Y-%m-%d"),
        "end": eq.index[-1].strftime("%Y-%m-%d"),
        "total_return": _num(total),
        "cagr": _num(cagr),
        "max_drawdown": _num(mdd),
        "volatility": _num(vol),
        "sharpe": _num(sharpe),
        "sortino": _num(sortino),
        "calmar": _num(cagr / abs(mdd)) if mdd < 0 else None,
    }


def trade_stats(trades: Iterable[Trade], years: float | None = None) -> dict:
    tr = list(trades)
    n = len(tr)
    empty = {"trades": n, "win_rate": None, "avg_win_pct": None, "avg_loss_pct": None, "payoff_ratio": None,
             "profit_factor": None, "expectancy_pct": None, "expectancy_r": None, "avg_bars_held": None,
             "trades_per_year": None, "net_pnl": 0.0}
    if n == 0:
        return empty
    pnl = np.array([t.pnl for t in tr])
    ret = np.array([t.return_pct for t in tr])
    wins, losses = ret[pnl > 0], ret[pnl <= 0]
    gross_win, gross_loss = pnl[pnl > 0].sum(), -pnl[pnl <= 0].sum()
    rs = [t.r_multiple for t in tr if t.r_multiple is not None]
    avg_win = wins.mean() if len(wins) else None
    avg_loss = losses.mean() if len(losses) else None
    return {
        "trades": n,
        "win_rate": _num(len(wins) / n),
        "avg_win_pct": _num(avg_win),
        "avg_loss_pct": _num(avg_loss),
        "payoff_ratio": _num(avg_win / abs(avg_loss)) if avg_win is not None and avg_loss not in (None, 0) else None,
        "profit_factor": _num(gross_win / gross_loss) if gross_loss > 0 else None,
        "expectancy_pct": _num(ret.mean()),
        "expectancy_r": _num(np.mean(rs)) if rs else None,
        "avg_bars_held": _num(np.mean([t.bars_held for t in tr])),
        "trades_per_year": _num(n / years) if years and years > 0 else None,
        "net_pnl": round(float(pnl.sum()), 2),
    }


def summarize(state: SimState, risk_free: float = 0.065) -> dict:
    eq = equity_series(state)
    curve = curve_stats(eq, risk_free)
    years = (eq.index[-1] - eq.index[0]).days / 365.25 if len(eq) > 1 else None
    out = {**curve, **trade_stats(state.trades, years)}
    out["total_costs"] = round(state.total_costs, 2)
    out["final_equity"] = round(float(eq.iloc[-1]), 2) if len(eq) else None
    out["open_positions"] = len(state.positions)
    if state.equity:
        cash = np.array([c for _, _, c in state.equity])
        equity = np.array([e for _, e, _ in state.equity])
        out["exposure"] = _num(np.mean(1 - cash / np.where(equity > 0, equity, np.nan)))
    else:
        out["exposure"] = None
    return out
