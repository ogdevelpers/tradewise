"""Where prices come from. Add a new source by implementing ``DataProvider``."""

from __future__ import annotations

import logging
import zlib
from pathlib import Path
from typing import Protocol, runtime_checkable

import numpy as np
import pandas as pd

from .market import DataError, validate_ohlcv

log = logging.getLogger(__name__)


@runtime_checkable
class DataProvider(Protocol):
    name: str

    def fetch(self, symbol: str, start: str | pd.Timestamp, end: str | pd.Timestamp) -> pd.DataFrame:
        """Daily OHLCV for ``symbol`` (split/bonus adjusted), index = date."""
        ...


class CSVProvider:
    """Reads ``<folder>/<SYMBOL>.csv`` with Date, Open, High, Low, Close, Volume columns."""

    name = "csv"

    def __init__(self, folder: str | Path):
        self.folder = Path(folder)
        if not self.folder.is_dir():
            raise DataError(f"CSV folder not found: {self.folder}")

    def available(self) -> list[str]:
        return sorted(p.stem.upper() for p in self.folder.glob("*.csv"))

    def fetch(self, symbol, start, end) -> pd.DataFrame:
        path = self.folder / f"{symbol}.csv"
        if not path.exists():
            matches = [p for p in self.folder.glob("*.csv") if p.stem.upper() == symbol.upper()]
            if not matches:
                raise DataError(f"{symbol}: no CSV file in {self.folder}")
            path = matches[0]
        df = pd.read_csv(path)
        df.columns = [str(c).strip().lower() for c in df.columns]
        if "adj close" in df.columns:
            df = df.drop(columns=["adj close"])
        df = validate_ohlcv(df, symbol)
        return df.loc[pd.Timestamp(start): pd.Timestamp(end)]


# Yahoo Finance tickers for the indices we use as benchmarks.
YAHOO_INDEX = {"NIFTY50": "^NSEI", "BANKNIFTY": "^NSEBANK", "SENSEX": "^BSESN"}


class YahooProvider:
    """Free daily data via the optional ``yfinance`` package (``pip install tradewise[yahoo]``).

    Prices are auto-adjusted for splits and bonuses. Good enough for research;
    for live trading prefer your broker's historical API.
    """

    name = "yahoo"

    def __init__(self):
        try:
            import yfinance as yf
        except ImportError as exc:  # pragma: no cover - depends on env
            raise DataError("yfinance is not installed: pip install 'tradewise[yahoo]'") from exc
        # yfinance keeps a small cache in the home folder; serverless hosts only allow writing to /tmp.
        try:
            import tempfile

            cache = Path(tempfile.gettempdir()) / "tradewise-yf-cache"
            cache.mkdir(parents=True, exist_ok=True)
            if hasattr(yf, "set_tz_cache_location"):
                yf.set_tz_cache_location(str(cache))
        except OSError:  # pragma: no cover
            pass

    @staticmethod
    def ticker(symbol: str) -> str:
        return YAHOO_INDEX.get(symbol.upper(), f"{symbol.upper()}.NS")

    def fetch(self, symbol, start, end) -> pd.DataFrame:
        import yfinance as yf

        end_excl = pd.Timestamp(end) + pd.Timedelta(days=1)  # yfinance end is exclusive
        raw = yf.download(
            self.ticker(symbol), start=pd.Timestamp(start), end=end_excl,
            auto_adjust=True, progress=False, threads=False,
        )
        if raw is None or raw.empty:
            raise DataError(f"{symbol}: Yahoo returned no data")
        if isinstance(raw.columns, pd.MultiIndex):
            raw.columns = raw.columns.get_level_values(0)
        return validate_ohlcv(raw, symbol)

    def fetch_many(self, symbols: list[str], start, end) -> dict[str, pd.DataFrame]:
        """All symbols in one request: far fewer calls, so Yahoo is much less likely to rate-limit us."""
        import yfinance as yf

        tickers = {self.ticker(s): s for s in symbols}
        raw = yf.download(
            list(tickers), start=pd.Timestamp(start), end=pd.Timestamp(end) + pd.Timedelta(days=1),
            auto_adjust=True, progress=False, threads=True, group_by="ticker",
        )
        out: dict[str, pd.DataFrame] = {}
        if raw is None or raw.empty:
            return out
        for tk, sym in tickers.items():
            try:
                if isinstance(raw.columns, pd.MultiIndex):
                    if tk not in raw.columns.get_level_values(0):
                        continue
                    df = raw[tk]
                else:
                    df = raw
                df = df.dropna(how="all")
                if len(df):
                    out[sym] = validate_ohlcv(df, sym)
            except DataError as exc:
                log.warning("%s: %s", sym, exc)
        return out


class SyntheticProvider:
    """Deterministic, realistic-looking fake prices for demos and tests.

    Each symbol gets its own seed from its name, so results are reproducible.
    The market has bull, bear and sideways regimes so strategies see all three.
    Never use these numbers for real decisions.
    """

    name = "synthetic"

    def __init__(self, seed: int = 7, start: str = "2014-01-01", end: str = "2026-10-30"):
        self.seed = seed
        self.calendar = pd.bdate_range(start, end)
        self._market = self._make_market()

    def _rng(self, key: str) -> np.random.Generator:
        return np.random.default_rng([self.seed, zlib.crc32(key.encode())])

    def _make_market(self) -> np.ndarray:
        rng = self._rng("__market__")
        n = len(self.calendar)
        rets = np.empty(n)
        i = 0
        while i < n:
            length = int(rng.integers(60, 300))
            regime = rng.choice(["bull", "bear", "flat"], p=[0.55, 0.2, 0.25])
            mu, sigma = {"bull": (0.0009, 0.009), "bear": (-0.0011, 0.016), "flat": (0.0001, 0.010)}[regime]
            rets[i: i + length] = rng.normal(mu, sigma, size=min(length, n - i))
            i += length
        return rets

    def _ohlcv(self, closes: np.ndarray, rng: np.random.Generator, base_volume: float) -> pd.DataFrame:
        n = len(closes)
        prev = np.concatenate([[closes[0]], closes[:-1]])
        opens = prev * (1 + rng.normal(0, 0.004, n))
        body_hi = np.maximum(opens, closes)
        body_lo = np.minimum(opens, closes)
        highs = body_hi * (1 + np.abs(rng.normal(0, 0.006, n)))
        lows = body_lo * (1 - np.abs(rng.normal(0, 0.006, n)))
        vol = base_volume * np.exp(rng.normal(0, 0.35, n)) * (1 + 8 * np.abs(closes / prev - 1))
        return pd.DataFrame(
            {"open": opens, "high": highs, "low": lows, "close": closes, "volume": np.round(vol)},
            index=pd.DatetimeIndex(self.calendar, name="date"),
        )

    def fetch(self, symbol, start, end) -> pd.DataFrame:
        sym = symbol.upper()
        rng = self._rng(sym)
        if sym in YAHOO_INDEX or sym.startswith("NIFTY"):
            rets = self._market
            price0 = 8000.0
        else:
            n = len(self.calendar)
            beta = rng.uniform(0.6, 1.4)
            alpha = np.zeros(n)
            i = 0
            while i < n:  # stock-specific trends: some stocks lead, some lag
                length = int(rng.integers(80, 400))
                alpha[i: i + length] = rng.normal(0.0002, 0.0007)
                i += length
            rets = beta * self._market + alpha + rng.normal(0, 0.014, n)
            price0 = float(rng.uniform(150, 3000))
        closes = price0 * np.exp(np.cumsum(rets))
        df = self._ohlcv(closes, rng, base_volume=float(rng.uniform(2e5, 4e6)))
        return validate_ohlcv(df, sym).loc[pd.Timestamp(start): pd.Timestamp(end)]


def get_provider(name: str, **kwargs) -> DataProvider:
    name = name.lower()
    if name == "csv":
        return CSVProvider(kwargs["folder"])
    if name == "yahoo":
        return YahooProvider()
    if name == "synthetic":
        return SyntheticProvider(**{k: v for k, v in kwargs.items() if k in {"seed", "start", "end"}})
    raise DataError(f"unknown provider {name!r} (choose csv, yahoo or synthetic)")
