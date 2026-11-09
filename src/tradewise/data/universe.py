"""Universe lists. Accepts NSE's index constituent CSV format
(``Company Name, Industry, Symbol, Series, ISIN Code``) as published on niftyindices.com."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from .market import DataError

SAMPLE_UNIVERSE = Path(__file__).with_name("sample_universe.csv")


@dataclass(frozen=True)
class Instrument:
    symbol: str
    name: str
    sector: str


def load_universe(path: str | Path | None = None) -> list[Instrument]:
    path = Path(path) if path else SAMPLE_UNIVERSE
    if not path.exists():
        raise DataError(f"universe file not found: {path}")
    df = pd.read_csv(path)
    cols = {c.strip().lower(): c for c in df.columns}
    if "symbol" not in cols:
        raise DataError(f"{path}: needs a Symbol column")
    sym_col = cols["symbol"]
    name_col = cols.get("company name") or cols.get("name")
    sec_col = cols.get("industry") or cols.get("sector")
    if "series" in cols:
        df = df[df[cols["series"]].astype(str).str.strip().str.upper() == "EQ"]
    out: list[Instrument] = []
    seen: set[str] = set()
    for _, row in df.iterrows():
        sym = str(row[sym_col]).strip().upper()
        if not sym or sym == "NAN" or sym in seen:
            continue
        seen.add(sym)
        out.append(Instrument(
            symbol=sym,
            name=str(row[name_col]).strip() if name_col else sym,
            sector=str(row[sec_col]).strip() if sec_col else "Unknown",
        ))
    if not out:
        raise DataError(f"{path}: no symbols found")
    return out
