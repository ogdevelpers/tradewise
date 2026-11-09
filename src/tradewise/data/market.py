"""Aligned, validated market data in "wide" form (rows = dates, columns = symbols).

Wide frames let every indicator run vectorised across the whole universe at
once, and let the simulator read one row per day as a NumPy array, which is
what keeps backtests on hundreds of stocks fast.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Mapping

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

OHLCV = ("open", "high", "low", "close", "volume")


class DataError(ValueError):
    """Raised when market data cannot be used safely."""


def validate_ohlcv(df: pd.DataFrame, symbol: str = "?") -> pd.DataFrame:
    """Return a clean OHLCV frame or raise :class:`DataError`.

    Cleans: lower-cases column names, parses/sorts the date index, removes
    duplicate dates (keeps last), and drops rows with missing or impossible
    prices (non-positive, high below low, open/close outside the high-low range).
    """
    if df is None or len(df) == 0:
        raise DataError(f"{symbol}: no rows")
    out = df.copy()
    out.columns = [str(c).strip().lower().replace(" ", "_") for c in out.columns]
    if "date" in out.columns:
        out = out.set_index("date")
    missing = [c for c in OHLCV if c not in out.columns]
    if missing:
        raise DataError(f"{symbol}: missing columns {missing}")
    out = out[list(OHLCV)]
    try:
        idx = pd.to_datetime(out.index)
    except (TypeError, ValueError) as exc:
        raise DataError(f"{symbol}: unparseable dates") from exc
    if getattr(idx, "tz", None) is not None:
        idx = idx.tz_localize(None)
    out.index = pd.DatetimeIndex(idx).normalize()
    out.index.name = "date"
    out = out.apply(pd.to_numeric, errors="coerce").astype("float64")
    out = out[~out.index.duplicated(keep="last")].sort_index()

    o, h, l, c, v = (out[k] for k in OHLCV)
    bad = (
        out[["open", "high", "low", "close"]].isna().any(axis=1)
        | (o <= 0) | (h <= 0) | (l <= 0) | (c <= 0)
        | (h < l)
        | (o > h * (1 + 1e-9)) | (o < l * (1 - 1e-9))
        | (c > h * (1 + 1e-9)) | (c < l * (1 - 1e-9))
    )
    if bad.any():
        log.warning("%s: dropping %d invalid rows", symbol, int(bad.sum()))
        out = out[~bad]
    out["volume"] = out["volume"].fillna(0.0).clip(lower=0.0)
    if out.empty:
        raise DataError(f"{symbol}: no valid rows")
    return out


@dataclass(frozen=True)
class MarketData:
    """Prices for a universe aligned to the benchmark's trading calendar."""

    open: pd.DataFrame
    high: pd.DataFrame
    low: pd.DataFrame
    close: pd.DataFrame
    volume: pd.DataFrame
    benchmark: pd.Series
    sectors: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        idx = self.close.index
        cols = self.close.columns
        for name in OHLCV:
            frame = getattr(self, name)
            if not frame.index.equals(idx) or not frame.columns.equals(cols):
                raise DataError(f"{name} frame is not aligned with close")
        if not self.benchmark.index.equals(idx):
            raise DataError("benchmark is not aligned with the price calendar")
        if not idx.is_monotonic_increasing or idx.has_duplicates:
            raise DataError("calendar must be sorted and unique")

    @classmethod
    def from_bars(
        cls,
        bars: Mapping[str, pd.DataFrame],
        benchmark: pd.DataFrame | pd.Series,
        sectors: Mapping[str, str] | None = None,
    ) -> "MarketData":
        if not bars:
            raise DataError("no symbols supplied")
        if isinstance(benchmark, pd.DataFrame):
            bench = validate_ohlcv(benchmark, "benchmark")["close"]
        else:
            bench = benchmark.dropna().astype("float64")
            bench.index = pd.DatetimeIndex(pd.to_datetime(bench.index)).normalize()
            bench = bench[~bench.index.duplicated(keep="last")].sort_index()
        if bench.empty:
            raise DataError("benchmark has no data")
        calendar = bench.index
        clean = {}
        for sym, df in bars.items():
            try:
                clean[sym] = validate_ohlcv(df, sym)
            except DataError as exc:
                log.warning("skipping %s", exc)
        if not clean:
            raise DataError("no symbol has usable data")
        symbols = sorted(clean)
        frames = {}
        for name in OHLCV:
            wide = pd.DataFrame({s: clean[s][name] for s in symbols})
            extra = wide.index.difference(calendar)
            if len(extra):
                log.debug("dropping %d bars outside benchmark calendar (%s)", len(extra), name)
            frames[name] = wide.reindex(calendar).astype("float64")
        frames["volume"] = frames["volume"].where(frames["close"].notna())
        bench.name = "benchmark"
        return cls(**frames, benchmark=bench, sectors=dict(sectors or {}))

    # ------------------------------------------------------------------ views
    @property
    def dates(self) -> pd.DatetimeIndex:
        return self.close.index  # type: ignore[return-value]

    @property
    def symbols(self) -> list[str]:
        return list(self.close.columns)

    def slice(self, start=None, end=None) -> "MarketData":
        """Rows between ``start`` and ``end`` inclusive."""
        sl = slice(pd.Timestamp(start) if start is not None else None,
                   pd.Timestamp(end) if end is not None else None)
        return MarketData(
            **{name: getattr(self, name).loc[sl] for name in OHLCV},
            benchmark=self.benchmark.loc[sl],
            sectors=self.sectors,
        )

    def truncate(self, end) -> "MarketData":
        """Data as it would have been known at the close of ``end``."""
        return self.slice(None, end)

    def select(self, symbols: list[str]) -> "MarketData":
        keep = [s for s in symbols if s in self.close.columns]
        if not keep:
            raise DataError("none of the requested symbols are available")
        return MarketData(
            **{name: getattr(self, name)[keep] for name in OHLCV},
            benchmark=self.benchmark,
            sectors=self.sectors,
        )

    def bars(self, symbol: str) -> pd.DataFrame:
        if symbol not in self.close.columns:
            raise KeyError(symbol)
        df = pd.DataFrame({name: getattr(self, name)[symbol] for name in OHLCV})
        return df.dropna(subset=["close"])

    def sector_of(self, symbol: str) -> str:
        return self.sectors.get(symbol, "Unknown")

    def nbytes(self) -> int:
        return int(sum(getattr(self, n).to_numpy().nbytes for n in OHLCV))


def as_array(frame: pd.DataFrame) -> np.ndarray:
    """Contiguous float64 view used by the simulator."""
    return np.ascontiguousarray(frame.to_numpy(dtype="float64"))
