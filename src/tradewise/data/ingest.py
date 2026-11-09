"""Download prices from a provider into the store, incrementally and in parallel."""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone

import pandas as pd

from ..market_time import last_complete_session
from .market import DataError
from .providers import DataProvider
from .store import PriceStore
from .universe import Instrument

log = logging.getLogger(__name__)


@dataclass
class IngestReport:
    updated: dict[str, int] = field(default_factory=dict)
    failed: dict[str, str] = field(default_factory=dict)
    replaced_source: str | None = None  # previous provider, if its prices were wiped

    @property
    def ok(self) -> bool:
        return not self.failed


def ingest(
    store: PriceStore,
    provider: DataProvider,
    instruments: list[Instrument],
    benchmark: str,
    start: str = "2014-01-01",
    end: str | None = None,
    incremental: bool = True,
    workers: int = 4,
) -> IngestReport:
    # Never store today's bar before the close is final: a half-day candle would corrupt every signal.
    end_ts = min(pd.Timestamp(end) if end else pd.Timestamp.max, last_complete_session())
    report = IngestReport()
    previous = store.get_meta("provider")
    new = getattr(provider, "name", "unknown")
    if previous and previous != new:
        log.warning("switching data source %s -> %s: removing old prices so they never mix", previous, new)
        store.clear_prices()
        report.replaced_source = previous
        incremental = False
    store.upsert_instruments([(i.symbol, i.name, i.sector, False) for i in instruments])
    store.upsert_instruments([(benchmark, benchmark, "Index", True)])

    known = store.last_dates() if incremental else {}

    def since_for(sym: str) -> pd.Timestamp:
        since = pd.Timestamp(start)
        if incremental:
            last = known.get(sym)
            if last is not None:
                since = last - pd.Timedelta(days=7)  # re-fetch a week to pick up late corrections
        return since

    symbols = [benchmark] + [i.symbol for i in instruments]

    def save(sym: str, df: pd.DataFrame, since: pd.Timestamp) -> None:
        df = df.loc[since:end_ts]
        report.updated[sym] = store.upsert_bars(sym, df) if len(df) else 0

    if hasattr(provider, "fetch_many"):
        # One batched request for every symbol (used for Yahoo).
        sinces = {s: since_for(s) for s in symbols}
        todo = [s for s in symbols if sinces[s] <= end_ts]
        for s in symbols:
            if s not in todo:
                report.updated[s] = 0
        if todo:
            try:
                got = provider.fetch_many(todo, min(sinces[s] for s in todo), end_ts)
            except Exception as exc:  # network errors, provider bugs: report, don't crash
                got = {}
                for s in todo:
                    report.failed[s] = f"{type(exc).__name__}: {exc}"
                log.warning("batched download failed: %s", exc)
            for s in todo:
                if s in report.failed:
                    continue
                if s not in got:
                    report.failed[s] = "no data returned"
                    continue
                try:
                    save(s, got[s], sinces[s])
                except (DataError, ValueError) as exc:
                    report.failed[s] = str(exc)
    else:
        def job(sym: str) -> int:
            since = since_for(sym)
            if since > end_ts:
                return 0
            df = provider.fetch(sym, since, end_ts)
            return store.upsert_bars(sym, df) if len(df) else 0

        with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
            futs = {ex.submit(job, s): s for s in symbols}
            for fut in as_completed(futs):
                sym = futs[fut]
                try:
                    report.updated[sym] = fut.result()
                except (DataError, OSError, ValueError, KeyError) as exc:
                    report.failed[sym] = str(exc)
                    log.warning("download failed for %s: %s", sym, exc)
                except Exception as exc:  # provider libraries raise all sorts of things
                    report.failed[sym] = f"{type(exc).__name__}: {exc}"
                    log.exception("unexpected error downloading %s", sym)
    if benchmark in report.failed:
        log.error("benchmark %s failed to download; signals need it", benchmark)
    store.set_meta("provider", new)
    store.set_meta("version", datetime.now(timezone.utc).isoformat())
    return report
