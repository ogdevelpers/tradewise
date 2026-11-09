"""Run several strategies on the same data, a combined portfolio, a benchmark and a train/test split."""

from __future__ import annotations

import logging
import multiprocessing
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

import pandas as pd

from .. import strategies
from ..data.market import MarketData
from .engine import BacktestConfig, SimState, Simulator
from .metrics import curve_stats, equity_series, outcome_range, summarize

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class StrategySpec:
    name: str
    params: Mapping[str, Any] | None = None
    weight: float = 1.0  # share of capital in the combined portfolio


def _run_one(data: MarketData, spec: StrategySpec, cfg: BacktestConfig) -> SimState:
    strat = strategies.create(spec.name, spec.params)
    return Simulator(data, strat, cfg).run()


def _split_stats(eq: pd.Series, split: pd.Timestamp | None) -> dict | None:
    if split is None or eq.empty:
        return None
    train, test = eq[eq.index < split], eq[eq.index >= split]
    if len(train) < 20 or len(test) < 20:
        return None
    a, b = curve_stats(train), curve_stats(test)
    ratio = None
    if a["cagr"] and b["cagr"] is not None and a["cagr"] > 0:
        ratio = round(b["cagr"] / a["cagr"], 3)
    return {"train": a, "test": b, "test_vs_train": ratio}


def downsample(eq: pd.Series, max_points: int = 600) -> list[list]:
    if eq.empty:
        return []
    step = max(1, -(-len(eq) // max_points))  # ceil division
    s = eq.iloc[::step]
    if s.index[-1] != eq.index[-1]:
        s = pd.concat([s, eq.iloc[-1:]])
    return [[d.strftime("%Y-%m-%d"), round(float(v), 2)] for d, v in s.items()]


def compare(
    data: MarketData,
    specs: Sequence[StrategySpec],
    config: BacktestConfig | None = None,
    split: str | pd.Timestamp | None = None,
    combined: bool = True,
    workers: int = 1,
    without_safety: bool = False,
) -> dict:
    """Backtest each spec on full capital, plus a combined portfolio that splits capital by ``weight``.

    ``without_safety`` also runs every spec with the safety switch off (keys ``<name>:no_safety``), so the
    effect of the switch can be seen side by side.
    """
    if not specs:
        raise ValueError("pick at least one strategy")
    cfg = config or BacktestConfig()
    split_ts = pd.Timestamp(split) if split else None

    if workers > 1 and len(specs) > 1:
        # spawn (not fork): safe when called from a multi-threaded process such as the web server
        ctx = multiprocessing.get_context("spawn")
        with ProcessPoolExecutor(max_workers=min(workers, len(specs)), mp_context=ctx) as ex:
            states = list(ex.map(_run_one, [data] * len(specs), specs, [cfg] * len(specs)))
    else:
        states = [_run_one(data, s, cfg) for s in specs]

    extra = []
    if without_safety and cfg.safety_switch:
        off = cfg.model_copy(update={"safety_switch": False})
        for spec in specs:
            st = _run_one(data, spec, off)
            eq = equity_series(st)
            extra.append({
                "key": f"{spec.name}:no_safety",
                "label": strategies.create(spec.name, spec.params).label + " (safety switch off)",
                "metrics": summarize(st), "split": _split_stats(eq, split_ts), "outcomes": outcome_range(eq),
                "equity": downsample(eq), "trades": [],
            })
    results = []
    curves: dict[str, pd.Series] = {}
    for spec, st in zip(specs, states):
        eq = equity_series(st)
        curves[spec.name] = eq
        results.append({
            "key": spec.name,
            "label": strategies.create(spec.name, spec.params).label,
            "metrics": summarize(st),
            "split": _split_stats(eq, split_ts),
            "outcomes": outcome_range(eq),
            "equity": downsample(eq),
            "trades": [t.__dict__ for t in st.trades[-500:]],
        })

    if combined and len(specs) > 1:
        total_w = sum(max(s.weight, 0) for s in specs)
        if total_w <= 0:
            raise ValueError("combined weights must be positive")
        # Each sleeve trades independently on its own share of capital.
        sleeve_cfgs = [cfg.model_copy(update={"initial_capital": cfg.initial_capital * max(s.weight, 0) / total_w})
                       for s in specs]
        sleeves = [_run_one(data, s, c) for s, c in zip(specs, sleeve_cfgs) if c.initial_capital > 0]
        comb = pd.concat([equity_series(st) for st in sleeves], axis=1).ffill().dropna().sum(axis=1)
        label = "Combined " + " / ".join(f"{round(100 * s.weight / total_w)}" for s in specs)
        results.append({
            "key": "combined", "label": label, "metrics": {**curve_stats(comb), "trades": sum(len(st.trades) for st in sleeves),
                                                           "total_costs": round(sum(st.total_costs for st in sleeves), 2)},
            "split": _split_stats(comb, split_ts), "outcomes": outcome_range(comb), "equity": downsample(comb), "trades": [],
        })

    results += extra
    ref = next((c for c in curves.values() if not c.empty), None)
    bench = data.benchmark
    if ref is not None:
        b = bench.loc[ref.index[0]: ref.index[-1]].dropna()
        if len(b):
            beq = b / b.iloc[0] * cfg.initial_capital
            results.append({
                "key": "benchmark", "label": "Benchmark buy & hold", "metrics": curve_stats(beq),
                "split": _split_stats(beq, split_ts), "outcomes": outcome_range(beq), "equity": downsample(beq),
                "trades": [],
            })
    return {"config": cfg.model_dump(mode="json"), "split": split_ts.strftime("%Y-%m-%d") if split_ts else None,
            "results": results}
