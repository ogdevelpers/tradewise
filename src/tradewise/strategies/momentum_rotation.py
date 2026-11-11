"""Momentum rotation: once a month, hold the stocks with the strongest risk-adjusted 6 and 12 month returns."""

from __future__ import annotations

import numpy as np
import pandas as pd
from pydantic import Field, model_validator

from .. import indicators as ind
from ..data.market import MarketData
from .base import Decision, DayContext, EntrySignal, ExitSignal, Features, Strategy, StrategyParams


class MomentumRotationParams(StrategyParams):
    top_n: int = Field(15, ge=3, le=50, description="Number of stocks to hold")
    exit_rank: int = Field(30, ge=3, le=200, description="Sell a holding only when it falls below this rank")
    lookback_long: int = Field(252, ge=60, le=504)
    lookback_short: int = Field(126, ge=20, le=252)
    start_immediately: bool = Field(True, description="With nothing held yet, buy at the next close instead of waiting for the month to turn")
    skip_recent: int = Field(21, ge=0, le=63, description="Ignore the latest days (short-term reversal)")
    vol_period: int = Field(252, ge=60, le=504)
    min_turnover_cr: float = Field(5.0, ge=0)
    trend_ma: int = Field(200, ge=0, le=400, description="Stock must be above this SMA (0 = off)")
    regime_ma: int = Field(200, ge=0, le=400, description="No new buys while benchmark is below this SMA (0 = off)")
    exit_all_in_bear: bool = Field(False, description="Sell everything when the regime filter turns off")

    @model_validator(mode="after")
    def _check(self):
        if self.exit_rank < self.top_n:
            raise ValueError("exit_rank must be >= top_n")
        if self.lookback_short >= self.lookback_long:
            raise ValueError("lookback_short must be shorter than lookback_long")
        return self


class MomentumRotation(Strategy):
    name = "momentum_rotation"
    label = "Momentum rotation"
    description = ("Monthly: rank stocks by volatility-adjusted 6 and 12 month returns (skipping the last month), "
                   "hold the top 15 equally weighted, sell a holding when it drops out of the top 30.")
    Params = MomentumRotationParams
    params: MomentumRotationParams
    rank_reason = "Momentum rank"

    @property
    def max_positions(self) -> int:
        return self.params.top_n

    @property
    def warmup(self) -> int:
        p = self.params
        return max(p.lookback_long + p.skip_recent, p.vol_period, p.trend_ma, p.regime_ma) + 1

    def prepare(self, data: MarketData) -> Features:
        p = self.params
        c, v = data.close, data.volume
        base = c.shift(p.skip_recent) if p.skip_recent else c
        r_long = base / c.shift(p.skip_recent + p.lookback_long) - 1
        r_short = base / c.shift(p.skip_recent + p.lookback_short) - 1
        vol = ind.rolling_volatility(c, p.vol_period).where(lambda x: x > 0)
        turnover = (c * v).rolling(20, min_periods=20).median()
        eligible = r_long.notna() & r_short.notna() & vol.notna() & (turnover >= p.min_turnover_cr * 1e7)
        if p.trend_ma:
            eligible &= c > ind.sma(c, p.trend_ma)
        score = (_zscore(r_long / vol, eligible) + _zscore(r_short / vol, eligible)) / 2
        score = score.where(eligible)

        bench = data.benchmark
        regime = (bench > ind.sma(bench, p.regime_ma)).astype("float64") if p.regime_ma else pd.Series(1.0, bench.index)
        months = pd.Series(data.dates.to_period("M"), index=data.dates)
        rebalance = (months != months.shift(1)).astype("float64")
        rebalance.iloc[0] = 0.0  # never rebalance on the first row: no prior month to compare

        frames = {"score": score, "r_long": r_long, "r_short": r_short, "vol": vol, "turnover": turnover,
                  "eligible": eligible.astype("float64")}
        return Features.from_frames(data, frames, series={"regime": regime, "rebalance": rebalance})

    def on_close(self, ctx: DayContext) -> Decision:
        f, i, p = ctx.features, ctx.i, self.params
        out = Decision()
        starting = getattr(p, "start_immediately", False) and not ctx.positions and not ctx.pending
        if f.series["rebalance"][i] <= 0 and not starting:
            return out
        regime_on = f.series["regime"][i] > 0
        score = f.row("score", i)
        valid = np.flatnonzero(np.isfinite(score))
        order = valid[np.argsort(-score[valid], kind="stable")]
        rank = {f.symbols[j]: r + 1 for r, j in enumerate(order)}

        keep = set()
        for sym in ctx.positions:
            if not regime_on and p.exit_all_in_bear:
                out.exits.append(ExitSignal(sym, "Market regime turned bearish"))
            elif rank.get(sym, 10**9) > p.exit_rank:
                out.exits.append(ExitSignal(sym, f"Dropped out of top {p.exit_rank}"))
            else:
                keep.add(sym)

        if not (ctx.can_enter and regime_on):
            return out
        slots = p.top_n - len(keep)
        weight = 1.0 / p.top_n
        for j in order:
            if slots <= 0:
                break
            sym = f.symbols[j]
            if sym in ctx.positions or sym in ctx.pending:
                continue
            r = rank[sym]
            if r > p.top_n:
                break
            out.entries.append(EntrySignal(symbol=sym, score=float(score[j]), weight=weight,
                                           reason=f"{self.rank_reason} {r}"))
            slots -= 1
        return out

    def explain(self, features: Features, i: int, symbol: str) -> list[dict]:
        p, f = self.params, features
        j = f.col[symbol]
        score = f.arrays["score"][i]
        valid = np.isfinite(score)
        rank = int((score[valid] > score[j]).sum()) + 1 if np.isfinite(score[j]) else None
        top = bool(rank is not None and rank <= p.top_n)
        return [
            {"key": "eligible", "rule": "Eligible: enough history, liquidity" + (f" and above {p.trend_ma} DMA" if p.trend_ma else ""),
             "plain": "A large, actively traded share in a long-term uptrend",
             "why": "We only rank shares that are easy to trade and already rising.",
             "pass": bool(f.arrays["eligible"][i, j] > 0)},
            {"key": "rank", "rule": f"Ranked in top {p.top_n}" + (f" (now #{rank})" if rank else ""),
             "plain": f"Among the {p.top_n} strongest performers" + (f" (now #{rank})" if rank else ""),
             "why": "Shares that rose steadily over the last year tend to keep doing well for a while.", "pass": top},
            {"key": "market", "rule": "Market regime allows new buys", "plain": "The overall market is rising",
             "why": "No new buys while the whole market is falling.", "pass": bool(f.series["regime"][i] > 0)},
        ]


def _zscore(x: pd.DataFrame, mask: pd.DataFrame) -> pd.DataFrame:
    """Cross-sectional z-score per day over eligible stocks only."""
    xm = x.where(mask)
    mu = xm.mean(axis=1)
    sd = xm.std(axis=1).where(lambda s: s > 0)
    return xm.sub(mu, axis=0).div(sd, axis=0)
