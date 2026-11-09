import numpy as np
import pandas as pd
import pytest

from tradewise import strategies
from tradewise.backtest import BacktestConfig, StrategySpec, compare, summarize
from tradewise.backtest.engine import Simulator

from conftest import nan_equal


@pytest.mark.parametrize("name", ["trend_pullback", "momentum_rotation"])
@pytest.mark.parametrize("cut", ["2017-03-15", "2019-08-01", "2021-12-31"])
def test_no_lookahead(synth, name, cut):
    """Features on day t must be identical whether or not later data exists."""
    strat = strategies.create(name)
    full = strat.prepare(synth)
    part = strat.prepare(synth.truncate(cut))
    n = len(part.dates)
    for k, arr in part.arrays.items():
        assert nan_equal(arr, full.arrays[k][:n]), f"{name}.{k} changes when future data is added"
    for k, arr in part.series.items():
        assert nan_equal(arr, full.series[k][:n]), f"{name}.{k} changes when future data is added"


@pytest.mark.parametrize("name", ["trend_pullback", "momentum_rotation"])
def test_decisions_identical_on_truncated_data(synth, name):
    conf = BacktestConfig(start="2016-06-01")
    full = Simulator(synth, strategies.create(name), conf).run()
    cut = synth.truncate("2019-12-31")
    part = Simulator(cut, strategies.create(name), conf).run()
    n = len(part.equity)
    assert part.equity == full.equity[:n]


def test_trend_pullback_rules(synth):
    s = strategies.create("trend_pullback")
    st = Simulator(synth, s, BacktestConfig(start="2016-06-01")).run()
    m = summarize(st)
    assert m["trades"] > 20
    assert max(len(set(f.symbol for f in st.fills)) for _ in [0]) >= 1
    reasons = {t.exit_reason for t in st.trades}
    assert reasons <= {"Stop-loss hit", "Gap below stop-loss", "Profit target reached", "Closed below 50 DMA",
                       "Closed below 20 EMA after target"}
    assert all(t.r_multiple is not None for t in st.trades)
    f = s.prepare(synth)
    i = len(synth.dates) - 1
    checks = s.explain(f, i, "AAA")
    assert len(checks) == 7 and all(set(c) == {"key", "rule", "plain", "why", "pass"} for c in checks)


def test_momentum_trades_only_after_month_start(synth):
    s = strategies.create("momentum_rotation", {"top_n": 4, "exit_rank": 6})
    st = Simulator(synth, s, BacktestConfig(start="2016-06-01")).run()
    assert st.fills
    dates = synth.dates
    first_of_month = {d for d, p in zip(dates[1:], dates[:-1]) if d.month != p.month}
    fill_days = {pd.Timestamp(f.date) for f in st.fills}
    allowed = {dates[dates.get_loc(d) + 1] for d in first_of_month if dates.get_loc(d) + 1 < len(dates)}
    assert fill_days <= allowed
    assert max(len(p) for p in [st.positions]) <= 4


def test_param_validation():
    with pytest.raises(ValueError):
        strategies.create("momentum_rotation", {"top_n": 20, "exit_rank": 10})
    with pytest.raises(ValueError):
        strategies.create("trend_pullback", {"atr_stop_mult": -1})
    with pytest.raises(ValueError):
        strategies.create("trend_pullback", {"not_a_param": 1})
    with pytest.raises(KeyError):
        strategies.create("nope")
    names = {s["name"] for s in strategies.available()}
    assert names == {"trend_pullback", "momentum_rotation"}


def test_compare_with_combined_and_split(synth):
    specs = [StrategySpec("momentum_rotation", weight=0.7), StrategySpec("trend_pullback", weight=0.3)]
    res = compare(synth, specs, BacktestConfig(start="2016-06-01"), split="2019-06-01")
    keys = [r["key"] for r in res["results"]]
    assert keys == ["momentum_rotation", "trend_pullback", "combined", "benchmark"]
    comb = res["results"][2]
    assert comb["label"] == "Combined 70 / 30"
    start_val = comb["equity"][0][1]
    assert start_val == pytest.approx(500_000, rel=0.01)
    for r in res["results"]:
        assert r["split"] is None or {"train", "test", "test_vs_train"} <= set(r["split"])
        assert len(r["equity"]) <= 610


def test_compare_parallel_matches_sequential(synth):
    specs = [StrategySpec("momentum_rotation"), StrategySpec("trend_pullback")]
    conf = BacktestConfig(start="2016-06-01")
    a = compare(synth, specs, conf, combined=False, workers=1)
    b = compare(synth, specs, conf, combined=False, workers=2)
    assert [r["metrics"] for r in a["results"]] == [r["metrics"] for r in b["results"]]


def test_metrics_handle_empty_state():
    from tradewise.backtest.engine import SimState

    m = summarize(SimState(cash=1000))
    assert m["cagr"] is None and m["trades"] == 0 and m["win_rate"] is None
    assert np.isnan(np.nan)  # sanity
