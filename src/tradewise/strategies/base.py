"""Strategy contract.

A strategy does two things:

1. ``prepare(data)`` computes every feature it needs, vectorised over the
   whole history. Features must be causal (row *t* uses rows ``<= t`` only);
   the test-suite enforces this by re-running on truncated data.
2. ``on_close(ctx)`` runs after each day's close and returns exits for held
   stocks and ranked entry candidates. The simulator decides quantities and
   fills orders at the next day's open, so a strategy can never trade on a
   price it could not have seen.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, ClassVar, Mapping

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict

from ..data.market import MarketData, as_array


class StrategyParams(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


@dataclass(frozen=True)
class EntrySignal:
    symbol: str
    score: float
    reason: str
    stop: float | None = None  # absolute stop -> risk-based sizing
    target_r: float | None = None  # partial profit at entry + target_r * initial risk
    target_fraction: float = 0.5
    weight: float | None = None  # target weight of equity -> weight-based sizing
    breakeven_after_target: bool = True

    def __post_init__(self) -> None:
        if (self.stop is None) == (self.weight is None):
            raise ValueError("entry needs exactly one of stop or weight")
        if self.weight is not None and not 0 < self.weight <= 1:
            raise ValueError("weight must be in (0, 1]")
        if not 0 < self.target_fraction <= 1:
            raise ValueError("target_fraction must be in (0, 1]")


@dataclass(frozen=True)
class ExitSignal:
    symbol: str
    reason: str
    fraction: float = 1.0


@dataclass(frozen=True)
class PositionView:
    symbol: str
    qty: int
    entry_price: float
    entry_index: int
    stop: float | None
    partial_done: bool


@dataclass
class Features:
    """Prepared feature panels: ``arrays[name]`` has shape (days, symbols)."""

    dates: pd.DatetimeIndex
    symbols: list[str]
    arrays: dict[str, np.ndarray]
    series: dict[str, np.ndarray] = field(default_factory=dict)  # per-day, shape (days,)

    def __post_init__(self) -> None:
        self.col = {s: j for j, s in enumerate(self.symbols)}
        shape = (len(self.dates), len(self.symbols))
        for k, a in self.arrays.items():
            if a.shape != shape:
                raise ValueError(f"feature {k} has shape {a.shape}, expected {shape}")
        for k, a in self.series.items():
            if a.shape != (len(self.dates),):
                raise ValueError(f"series {k} has wrong shape {a.shape}")

    @classmethod
    def from_frames(cls, data: MarketData, frames: Mapping[str, pd.DataFrame],
                    series: Mapping[str, pd.Series] | None = None) -> "Features":
        arrays = {k: as_array(v.reindex(index=data.dates, columns=data.symbols)) for k, v in frames.items()}
        ser = {k: np.asarray(v.reindex(data.dates), dtype="float64") for k, v in (series or {}).items()}
        return cls(dates=data.dates, symbols=data.symbols, arrays=arrays, series=ser)

    def row(self, name: str, i: int) -> np.ndarray:
        return self.arrays[name][i]

    def value(self, name: str, i: int, symbol: str) -> float:
        return float(self.arrays[name][i, self.col[symbol]])


@dataclass(frozen=True)
class DayContext:
    i: int
    date: pd.Timestamp
    features: Features
    positions: Mapping[str, PositionView]
    pending: frozenset[str]  # symbols with orders already queued
    can_enter: bool


@dataclass
class Decision:
    exits: list[ExitSignal] = field(default_factory=list)
    entries: list[EntrySignal] = field(default_factory=list)
    stop_updates: dict[str, float] = field(default_factory=dict)  # stops can only move up


class Strategy(ABC):
    name: ClassVar[str]
    label: ClassVar[str]
    description: ClassVar[str]
    Params: ClassVar[type[StrategyParams]]

    def __init__(self, params: StrategyParams | Mapping[str, Any] | None = None):
        if isinstance(params, StrategyParams):
            if not isinstance(params, self.Params):
                raise TypeError(f"expected {self.Params.__name__}")
            self.params = params
        else:
            self.params = self.Params(**(params or {}))

    @property
    def max_positions(self) -> int:
        """Default cap on simultaneous holdings (the backtest config can override it)."""
        return 10

    @property
    def warmup(self) -> int:
        """Bars of history needed before signals are meaningful."""
        return 0

    @abstractmethod
    def prepare(self, data: MarketData) -> Features: ...

    @abstractmethod
    def on_close(self, ctx: DayContext) -> Decision: ...

    def explain(self, features: Features, i: int, symbol: str) -> list[dict]:
        """Human-readable rule checklist for one stock on one day (for the UI)."""
        return []

    @classmethod
    def describe(cls) -> dict:
        return {
            "name": cls.name,
            "label": cls.label,
            "description": cls.description,
            "params": cls.Params.model_json_schema(),
            "defaults": cls.Params().model_dump(),
        }


def finite(x: float) -> bool:
    return x is not None and bool(np.isfinite(x))
