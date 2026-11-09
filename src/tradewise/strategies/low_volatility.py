"""Steady shares: once a month, hold the calmest large shares that are still in an uptrend.

The "low-volatility anomaly" is one of the best-documented patterns in stock markets, India included (the NSE
publishes a Nifty100 Low Volatility 30 index built on it): shares with small daily swings have historically
earned about as much as the market with noticeably smaller falls. This plan ranks liquid shares above their
200-day average by *low* one-year volatility, with a smaller tilt towards a positive one-year return, holds the
top 15 equally and swaps on the first trading day of each month. It reuses the rotation mechanics (rank bands,
regime filter) of :class:`MomentumRotation`.
"""

from __future__ import annotations

import pandas as pd
from pydantic import Field, model_validator

from .. import indicators as ind
from ..data.market import MarketData
from .base import Features, StrategyParams
from .momentum_rotation import MomentumRotation, _zscore


class LowVolatilityParams(StrategyParams):
    top_n: int = Field(15, ge=3, le=50, description="Number of stocks to hold")
    exit_rank: int = Field(30, ge=3, le=200, description="Sell a holding only when it falls below this rank")
    vol_period: int = Field(252, ge=60, le=504, description="Days used to measure how much a share swings")
    lookback_long: int = Field(252, ge=60, le=504, description="Days for the return tilt")
    return_tilt: float = Field(0.5, ge=0, le=2, description="Weight of the 1-year return next to calmness")
    min_turnover_cr: float = Field(5.0, ge=0)
    trend_ma: int = Field(200, ge=0, le=400, description="Stock must be above this SMA (0 = off)")
    regime_ma: int = Field(200, ge=0, le=400, description="No new buys while benchmark is below this SMA (0 = off)")
    exit_all_in_bear: bool = Field(False, description="Sell everything when the regime filter turns off")
    skip_recent: int = Field(0, ge=0, le=63)

    @model_validator(mode="after")
    def _check(self):
        if self.exit_rank < self.top_n:
            raise ValueError("exit_rank must be >= top_n")
        return self


class LowVolatility(MomentumRotation):
    name = "low_volatility"
    label = "Steady shares (low volatility)"
    description = ("Monthly: among liquid shares above their 200-day average, hold the 15 with the smallest "
                   "one-year price swings (with a tilt to positive 1-year returns), equally weighted; sell a holding "
                   "when it drops out of the top 30.")
    Params = LowVolatilityParams  # type: ignore[assignment]
    params: LowVolatilityParams  # type: ignore[assignment]
    rank_reason = "Steadiness rank"

    @property
    def warmup(self) -> int:
        p = self.params
        return max(p.lookback_long, p.vol_period, p.trend_ma, p.regime_ma) + 1

    def prepare(self, data: MarketData) -> Features:
        p = self.params
        c, v = data.close, data.volume
        r_long = c / c.shift(p.lookback_long) - 1
        vol = ind.rolling_volatility(c, p.vol_period).where(lambda x: x > 0)
        turnover = (c * v).rolling(20, min_periods=20).median()
        eligible = r_long.notna() & vol.notna() & (turnover >= p.min_turnover_cr * 1e7)
        if p.trend_ma:
            eligible &= c > ind.sma(c, p.trend_ma)
        score = _zscore(-vol, eligible)
        if p.return_tilt:
            score = score + p.return_tilt * _zscore(r_long, eligible)
        score = score.where(eligible)

        bench = data.benchmark
        regime = (bench > ind.sma(bench, p.regime_ma)).astype("float64") if p.regime_ma else pd.Series(1.0, bench.index)
        months = pd.Series(data.dates.to_period("M"), index=data.dates)
        rebalance = (months != months.shift(1)).astype("float64")
        rebalance.iloc[0] = 0.0

        frames = {"score": score, "r_long": r_long, "vol": vol, "turnover": turnover,
                  "eligible": eligible.astype("float64")}
        return Features.from_frames(data, frames, series={"regime": regime, "rebalance": rebalance})

    def explain(self, features: Features, i: int, symbol: str) -> list[dict]:
        p, f = self.params, features
        j = f.col[symbol]
        score = f.arrays["score"][i]
        valid = score == score
        rank = int((score[valid] > score[j]).sum()) + 1 if score[j] == score[j] else None
        vol = f.arrays["vol"][i, j]
        return [
            {"key": "eligible", "rule": "Eligible: enough history, liquidity" + (f" and above {p.trend_ma} DMA" if p.trend_ma else ""),
             "plain": "A large, actively traded share in a long-term uptrend",
             "why": "We only rank shares that are easy to trade and not falling.",
             "pass": bool(f.arrays["eligible"][i, j] > 0)},
            {"key": "rank", "rule": f"Ranked in top {p.top_n} by low volatility" + (f" (now #{rank})" if rank else ""),
             "plain": f"Among the {p.top_n} calmest shares" + (f" (now #{rank}; swings about {vol:.0%} a year)" if rank and vol == vol else ""),
             "why": "Calm shares have historically fallen less in bad times while still growing.",
             "pass": bool(rank is not None and rank <= p.top_n)},
            {"key": "market", "rule": "Market regime allows new buys", "plain": "The overall market is rising",
             "why": "No new buys while the whole market is falling.", "pass": bool(f.series["regime"][i] > 0)},
        ]
