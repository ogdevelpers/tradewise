"""Strategy registry. Register a new strategy by adding its class to ``_REGISTRY``."""

from __future__ import annotations

from typing import Any, Mapping

from .base import Decision, DayContext, EntrySignal, ExitSignal, Features, PositionView, Strategy, StrategyParams
from .low_volatility import LowVolatility
from .momentum_rotation import MomentumRotation
from .trend_pullback import TrendPullback

_REGISTRY: dict[str, type[Strategy]] = {cls.name: cls for cls in (TrendPullback, MomentumRotation, LowVolatility)}


def available() -> list[dict]:
    return [cls.describe() for cls in _REGISTRY.values()]


def create(name: str, params: Mapping[str, Any] | None = None) -> Strategy:
    try:
        cls = _REGISTRY[name]
    except KeyError:
        raise KeyError(f"unknown strategy {name!r}; available: {sorted(_REGISTRY)}") from None
    return cls(params)


__all__ = [
    "Decision", "DayContext", "EntrySignal", "ExitSignal", "Features", "PositionView", "Strategy",
    "StrategyParams", "TrendPullback", "MomentumRotation", "LowVolatility", "available", "create",
]
