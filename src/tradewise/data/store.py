"""Price store on SQLite (local) or Postgres (Vercel). See ``tradewise.db``."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterable

import pandas as pd

from ..db import Database
from .market import OHLCV, MarketData, validate_ohlcv

log = logging.getLogger(__name__)


def _schema(db: Database) -> str:
    real = db.real
    without_rowid = " WITHOUT ROWID" if db.dialect == "sqlite" else ""
    return f"""
CREATE TABLE IF NOT EXISTS prices (
    symbol TEXT NOT NULL,
    date   TEXT NOT NULL,
    open {real} NOT NULL, high {real} NOT NULL, low {real} NOT NULL, close {real} NOT NULL,
    volume {real} NOT NULL,
    PRIMARY KEY (symbol, date)
){without_rowid};
CREATE TABLE IF NOT EXISTS instruments (
    symbol TEXT PRIMARY KEY,
    name   TEXT,
    sector TEXT,
    is_benchmark INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)
"""


class PriceStore:
    def __init__(self, target: str | Path):
        self.db = Database(target)
        self.path = self.db.path
        self.db.ensure_schema(_schema(self.db))

    # ---------------------------------------------------------------- writes
    def upsert_bars(self, symbol: str, df: pd.DataFrame) -> int:
        clean = validate_ohlcv(df, symbol)
        rows = [
            (symbol, d.strftime("%Y-%m-%d"), *map(float, r))
            for d, r in zip(clean.index, clean[list(OHLCV)].itertuples(index=False, name=None))
        ]
        with self.db.connect() as c:
            c.executemany(
                "INSERT INTO prices(symbol,date,open,high,low,close,volume) VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT(symbol,date) DO UPDATE SET open=excluded.open, high=excluded.high, "
                "low=excluded.low, close=excluded.close, volume=excluded.volume",
                rows,
            )
        return len(rows)

    def upsert_instruments(self, items: Iterable[tuple[str, str | None, str | None, bool]]) -> None:
        with self.db.connect() as c:
            c.executemany(
                "INSERT INTO instruments(symbol,name,sector,is_benchmark) VALUES (?,?,?,?) "
                "ON CONFLICT(symbol) DO UPDATE SET name=excluded.name, sector=excluded.sector, "
                "is_benchmark=excluded.is_benchmark",
                [(s, n, sec, int(b)) for s, n, sec, b in items],
            )

    def clear_prices(self) -> None:
        """Remove all prices (used when switching data source, so demo and real prices never mix)."""
        with self.db.connect() as c:
            c.execute("DELETE FROM prices")
            c.execute("DELETE FROM meta WHERE key IN ('provider','version')")

    def set_meta(self, key: str, value: str) -> None:
        with self.db.connect() as c:
            c.execute("INSERT INTO meta(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                      (key, value))

    # ----------------------------------------------------------------- reads
    def get_meta(self, key: str, default: str | None = None) -> str | None:
        with self.db.connect() as c:
            row = c.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def get_meta_many(self, keys: list[str]) -> dict[str, str | None]:
        if not keys:
            return {}
        with self.db.connect() as c:
            rows = c.execute(f"SELECT key,value FROM meta WHERE key IN ({','.join('?' * len(keys))})", keys).fetchall()
        found = dict(rows)
        return {k: found.get(k) for k in keys}

    def symbols(self, include_benchmarks: bool = False) -> list[str]:
        q = "SELECT symbol FROM instruments" + ("" if include_benchmarks else " WHERE is_benchmark=0")
        with self.db.connect() as c:
            return sorted(r[0] for r in c.execute(q).fetchall())

    def instruments(self) -> list[dict]:
        with self.db.connect() as c:
            rows = c.execute("SELECT symbol,name,sector,is_benchmark FROM instruments ORDER BY symbol").fetchall()
        return [dict(symbol=s, name=n, sector=sec, is_benchmark=bool(b)) for s, n, sec, b in rows]

    def last_date(self, symbol: str) -> pd.Timestamp | None:
        with self.db.connect() as c:
            row = c.execute("SELECT MAX(date) FROM prices WHERE symbol=?", (symbol,)).fetchone()
        return pd.Timestamp(row[0]) if row and row[0] else None

    def last_dates(self) -> dict[str, pd.Timestamp]:
        with self.db.connect() as c:
            rows = c.execute("SELECT symbol, MAX(date) FROM prices GROUP BY symbol").fetchall()
        return {s: pd.Timestamp(d) for s, d in rows if d}

    def _query_bars(self, symbols: list[str] | None, start, end) -> pd.DataFrame:
        q = "SELECT symbol,date,open,high,low,close,volume FROM prices WHERE 1=1"
        args: list = []
        if symbols is not None:
            q += f" AND symbol IN ({','.join('?' * len(symbols))})"
            args += symbols
        if start is not None:
            q += " AND date>=?"
            args.append(pd.Timestamp(start).strftime("%Y-%m-%d"))
        if end is not None:
            q += " AND date<=?"
            args.append(pd.Timestamp(end).strftime("%Y-%m-%d"))
        q += " ORDER BY symbol,date"
        with self.db.connect() as c:
            rows = c.execute(q, args).fetchall()
        df = pd.DataFrame(rows, columns=["symbol", "date", *OHLCV])
        if not df.empty:
            df["date"] = pd.to_datetime(df["date"])
            df[list(OHLCV)] = df[list(OHLCV)].astype("float64")
        return df

    def load_bars(self, symbol: str, start=None, end=None) -> pd.DataFrame:
        df = self._query_bars([symbol], start, end)
        return df.drop(columns="symbol").set_index("date") if not df.empty else df

    def load_market(self, benchmark: str, symbols: list[str] | None = None, start=None, end=None) -> MarketData:
        """Everything in one query: fast even when the database is across the network."""
        wanted = None if symbols is None else [*symbols, benchmark]
        df = self._query_bars(wanted, start, end)
        if df.empty or not (df["symbol"] == benchmark).any():
            raise LookupError(f"benchmark {benchmark} has no data; prices have not been downloaded yet")
        groups = {s: g.drop(columns="symbol").set_index("date") for s, g in df.groupby("symbol", sort=False)}
        bench = groups.pop(benchmark)
        stock_syms = set(symbols) if symbols is not None else set(self.symbols())
        bars = {s: g for s, g in groups.items() if s in stock_syms}
        if not bars:
            raise LookupError("no stock data found; prices have not been downloaded yet")
        sectors = {i["symbol"]: i["sector"] or "Unknown" for i in self.instruments()}
        return MarketData.from_bars(bars, bench, sectors)
