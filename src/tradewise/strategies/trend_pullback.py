"""Trend pullback: buy strong stocks on a short dip inside an uptrend."""

from __future__ import annotations

import numpy as np
from pydantic import Field

from .. import indicators as ind
from ..data.market import MarketData
from .base import Decision, DayContext, EntrySignal, ExitSignal, Features, Strategy, StrategyParams


class TrendPullbackParams(StrategyParams):
    regime_ma: int = Field(200, ge=20, le=400, description="Benchmark must close above this SMA")
    trend_fast: int = Field(50, ge=10, le=200)
    trend_slow: int = Field(200, ge=50, le=400)
    ema_period: int = Field(20, ge=5, le=100)
    pullback_lookback: int = Field(3, ge=1, le=10, description="Bars in which the low must touch the EMA")
    pullback_tolerance: float = Field(0.01, ge=0, le=0.05)
    rsi_period: int = Field(14, ge=2, le=50)
    rsi_low: float = Field(40, ge=10, le=60)
    rsi_high: float = Field(55, ge=30, le=80)
    volume_period: int = Field(20, ge=5, le=100)
    atr_period: int = Field(14, ge=5, le=50)
    atr_stop_mult: float = Field(2.0, gt=0, le=6)
    target_r: float = Field(2.0, gt=0, le=10, description="Book part profit at this multiple of risk")
    target_fraction: float = Field(0.5, gt=0, le=1)
    min_turnover_cr: float = Field(5.0, ge=0, description="Median 20-day traded value, Rs crore")
    rs_period: int = Field(63, ge=10, le=252, description="Return period used to rank candidates")
    max_positions: int = Field(8, ge=1, le=50)


class TrendPullback(Strategy):
    name = "trend_pullback"
    label = "Trend pullback"
    description = ("Buys stocks in a confirmed uptrend when price dips to the 20 EMA and bounces on volume. "
                   "Stop at 2x ATR, half booked at 2R, rest trailed under the 20 EMA.")
    Params = TrendPullbackParams
    params: TrendPullbackParams

    @property
    def max_positions(self) -> int:
        return self.params.max_positions

    @property
    def warmup(self) -> int:
        p = self.params
        return max(p.trend_slow, p.regime_ma) + p.pullback_lookback + 1

    def prepare(self, data: MarketData) -> Features:
        p = self.params
        c, h, l, v = data.close, data.high, data.low, data.volume
        sma_f = ind.sma(c, p.trend_fast)
        sma_s = ind.sma(c, p.trend_slow)
        ema_ = ind.ema(c, p.ema_period)
        rsi_ = ind.rsi(c, p.rsi_period)
        atr_ = ind.atr(h, l, c, p.atr_period)
        vol_ma = ind.sma(v, p.volume_period)
        turnover = (c * v).rolling(20, min_periods=20).median()
        rs = ind.pct_return(c, p.rs_period)
        bench = data.benchmark
        regime = (bench > ind.sma(bench, p.regime_ma)).astype("float64")

        low_min = l.rolling(p.pullback_lookback, min_periods=p.pullback_lookback).min()
        rsi_min = rsi_.rolling(p.pullback_lookback, min_periods=p.pullback_lookback).min()

        cond = {
            "trend": (c > sma_s) & (sma_f > sma_s),
            "pullback": low_min <= ema_ * (1 + p.pullback_tolerance),
            "bounce": (c > ema_) & (c > c.shift(1)),
            "rsi_turn": (rsi_ > rsi_.shift(1)) & (rsi_min >= p.rsi_low) & (rsi_min <= p.rsi_high),
            "volume": v > vol_ma,
            "liquidity": turnover >= p.min_turnover_cr * 1e7,
        }
        entry = atr_.notna() & (atr_ > 0)
        for m in cond.values():
            entry &= m
        entry = entry & (regime.to_numpy()[:, None] > 0)

        frames = {
            "close": c, "sma_fast": sma_f, "sma_slow": sma_s, "ema": ema_, "rsi": rsi_, "atr": atr_,
            "volume": v, "vol_ma": vol_ma, "turnover": turnover, "rs": rs,
            "score": rs.rank(axis=1, pct=True) * 100,
            "stop": c - p.atr_stop_mult * atr_,
            "entry": entry.astype("float64"),
            "exit_hard": (c < sma_f).astype("float64"),
            "exit_trail": (c < ema_).astype("float64"),
        }
        frames.update({f"c_{k}": m.astype("float64") for k, m in cond.items()})
        return Features.from_frames(data, frames, series={"regime": regime})

    def on_close(self, ctx: DayContext) -> Decision:
        f, i, p = ctx.features, ctx.i, self.params
        out = Decision()
        exit_hard, exit_trail = f.row("exit_hard", i), f.row("exit_trail", i)
        for sym, pos in ctx.positions.items():
            j = f.col[sym]
            if exit_hard[j] > 0:
                out.exits.append(ExitSignal(sym, f"Closed below {p.trend_fast} DMA"))
            elif pos.partial_done and exit_trail[j] > 0:
                out.exits.append(ExitSignal(sym, f"Closed below {p.ema_period} EMA after target"))
        if not ctx.can_enter:
            return out
        entry, score, stop = f.row("entry", i), f.row("score", i), f.row("stop", i)
        for j in np.flatnonzero(entry > 0):
            sym = f.symbols[j]
            if sym in ctx.positions or sym in ctx.pending or not np.isfinite(stop[j]) or stop[j] <= 0:
                continue
            out.entries.append(EntrySignal(
                symbol=sym, score=float(np.nan_to_num(score[j])), reason="Pullback to 20 EMA in uptrend",
                stop=float(stop[j]), target_r=p.target_r, target_fraction=p.target_fraction,
            ))
        out.entries.sort(key=lambda e: e.score, reverse=True)
        return out

    def explain(self, features: Features, i: int, symbol: str) -> list[dict]:
        p, f = self.params, features
        j = f.col[symbol]

        def ok(name: str) -> bool:
            return bool(f.arrays[name][i, j] > 0)

        def rule(key, technical, plain, why, passed):
            return {"key": key, "rule": technical, "plain": plain, "why": why, "pass": passed}

        regime = bool(f.series["regime"][i] > 0)
        return [
            rule("market", f"Market regime: benchmark above {p.regime_ma}-day average",
                 "The overall market is rising",
                 "When the whole market falls, most shares fall with it, so we only buy in a rising market.", regime),
            rule("trend", f"Trend: price above {p.trend_slow} DMA and {p.trend_fast} DMA above {p.trend_slow} DMA",
                 "This share has been rising for months",
                 "Its price is above its long-term average price, a sign of a steady uptrend.", ok("c_trend")),
            rule("dip", f"Pullback: low touched the {p.ema_period} EMA in the last {p.pullback_lookback} days",
                 "It dipped a little in the last few days",
                 "Buying after a small dip gets a better price than buying after a big jump.", ok("c_pullback")),
            rule("bounce", f"Bounce: closed up and above the {p.ema_period} EMA",
                 "It started rising again today",
                 "Shows buyers are coming back after the dip.", ok("c_bounce")),
            rule("rsi", f"RSI({p.rsi_period}) turned up from the {p.rsi_low:g}–{p.rsi_high:g} zone",
                 "The dip was mild and momentum is turning up",
                 "A calm dip that recovers is healthy. A crash is not.", ok("c_rsi_turn")),
            rule("volume", f"Volume above {p.volume_period}-day average",
                 "More shares than usual were traded today",
                 "Busy trading means many people support the move.", ok("c_volume")),
            rule("liquid", f"Liquidity: median traded value at least Rs {p.min_turnover_cr:g} cr",
                 "Easy to buy and sell",
                 "Shares that trade a lot every day can be bought and sold quickly at a fair price.", ok("c_liquidity")),
        ]
