from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from tradewise.data.market import MarketData
from tradewise.data.providers import SyntheticProvider
from tradewise.strategies.base import Decision, DayContext, Features, Strategy, StrategyParams

SYMBOLS = ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "GGG", "HHH", "III", "JJJ", "KKK", "LLL"]
SECTORS = {s: ("Banks" if i % 3 == 0 else "IT" if i % 3 == 1 else "Energy") for i, s in enumerate(SYMBOLS)}


@pytest.fixture(scope="session")
def synth() -> MarketData:
    p = SyntheticProvider(seed=11, start="2015-01-01", end="2021-12-31")
    bars = {s: p.fetch(s, "2015-01-01", "2021-12-31") for s in SYMBOLS}
    return MarketData.from_bars(bars, p.fetch("NIFTY50", "2015-01-01", "2021-12-31"), SECTORS)


def make_market(prices: dict[str, list[tuple[float, float, float, float]]], start="2024-01-01",
                sectors: dict[str, str] | None = None) -> MarketData:
    """Build MarketData from per-symbol lists of (open, high, low, close); None = no bar that day."""
    n = max(len(v) for v in prices.values())
    idx = pd.bdate_range(start, periods=n)
    bars = {}
    for sym, rows in prices.items():
        recs = [(d, *r, 1e6) for d, r in zip(idx, rows) if r is not None]
        bars[sym] = pd.DataFrame(recs, columns=["date", "open", "high", "low", "close", "volume"]).set_index("date")
    bench = pd.Series(100.0, index=idx)
    return MarketData.from_bars(bars, bench, sectors or {})


class ScriptedParams(StrategyParams):
    pass


class Scripted(Strategy):
    """Strategy that returns pre-written decisions on given day indexes (for engine tests)."""

    name = "scripted"
    label = "Scripted"
    description = "test helper"
    Params = ScriptedParams

    def __init__(self, script: dict[int, Decision], max_pos: int = 10):
        super().__init__()
        self.script = script
        self._max = max_pos
        self.seen: list[DayContext] = []

    @property
    def max_positions(self) -> int:
        return self._max

    def prepare(self, data: MarketData) -> Features:
        return Features(dates=data.dates, symbols=data.symbols, arrays={})

    def on_close(self, ctx: DayContext) -> Decision:
        self.seen.append(ctx)
        return self.script.get(ctx.i, Decision())


def bar(o, h=None, l=None, c=None):
    """Convenience: flat bar at o unless high/low/close given."""
    return (o, h if h is not None else max(o, c or o), l if l is not None else min(o, c or o), c if c is not None else o)


def nan_equal(a: np.ndarray, b: np.ndarray) -> bool:
    return bool(np.allclose(a, b, equal_nan=True, rtol=1e-9, atol=1e-9))
