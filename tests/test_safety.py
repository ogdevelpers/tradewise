import numpy as np
import pandas as pd
import pytest

from tradewise import strategies
from tradewise.backtest import StrategySpec, compare
from tradewise.backtest.engine import BacktestConfig, Simulator
from tradewise.backtest.metrics import outcome_range
from tradewise.costs import ZERO_COSTS
from tradewise.risk import drawdown_scale, market_scale
from tradewise.strategies.base import Decision, EntrySignal

from conftest import Scripted, bar, make_market

LOOSE = dict(initial_capital=100_000, slippage=0.0, costs=ZERO_COSTS, max_position_weight=1.0,
             max_sector_weight=1.0, max_monthly_drawdown=1.0, min_order_value=0)


def jumpy_bench(n, swing=0.03):
    return list(100 * np.cumprod([1 + (swing if k % 2 else -swing) for k in range(n)]))


def first_buy_qty(bench, **kw):
    n = len(bench)
    data = make_market({"AAA": [bar(100)] * n}, bench=bench)
    script = {n - 3: Decision(entries=[EntrySignal("AAA", 1.0, "test", stop=95.0, target_r=2.0)])}
    sim = Simulator(data, Scripted(script), BacktestConfig(**{**LOOSE, **kw}))
    sim.state.next_index = 0
    st = sim.run()
    return st.fills[0].qty


def test_market_scale_shrinks_with_volatility_and_respects_floor():
    calm = market_scale(pd.Series(np.linspace(100, 110, 60)))
    assert calm.iloc[-1] == 1.0
    wild = market_scale(pd.Series(jumpy_bench(60, 0.03)))
    vol = 0.03 * np.sqrt(252)  # about 48% a year
    assert wild.iloc[-1] == pytest.approx(0.16 / vol, rel=0.05)
    assert market_scale(pd.Series(jumpy_bench(60, 0.2))).iloc[-1] == 0.25
    assert market_scale(pd.Series([100.0] * 5)).tolist() == [1.0] * 5  # unknown -> full size


def test_safety_switch_cuts_buys_in_a_jumpy_market():
    bench = jumpy_bench(40)
    full = first_buy_qty(bench, safety_switch=False)
    cut = first_buy_qty(bench)
    assert full == 200  # 1% of 1L / (100 - 95)
    assert 50 <= cut <= 80
    assert first_buy_qty([100.0] * 40) == 200  # calm market: no change


def test_safety_switch_uses_only_known_prices():
    bench = [100.0] * 38 + jumpy_bench(2, 0.2)  # chaos starts after the order was planned (close of day 37)
    assert first_buy_qty(bench) == first_buy_qty([100.0] * 40)


def test_drawdown_brake():
    assert drawdown_scale(90, 100, 0.12) == 1.0
    assert drawdown_scale(87, 100, 0.12) == 0.5
    assert drawdown_scale(50, 100, 0.0) == 1.0
    # In the simulator: a 20% loss on AAA, then a new buy on BBB is half size.
    prices = {"AAA": [bar(100), bar(100), bar(70), bar(70), bar(70), bar(70)], "BBB": [bar(100)] * 6}
    script = {0: Decision(entries=[EntrySignal("AAA", 1.0, "t", weight=0.5)]),
              3: Decision(entries=[EntrySignal("BBB", 1.0, "t", stop=90.0, target_r=2.0)])}
    for brake, qty in ((0.12, 42), (0.0, 85)):
        sim = Simulator(make_market(prices), Scripted(script), BacktestConfig(**{**LOOSE, "drawdown_brake": brake}))
        sim.state.next_index = 0
        st = sim.run()
        assert [f.qty for f in st.fills if f.symbol == "BBB"] == [qty]  # 1% of 85k / 10, halved when braking


def test_outcome_range():
    idx = pd.bdate_range("2015-01-01", periods=252 * 4)
    rng = np.random.default_rng(1)
    eq = pd.Series(100 * np.cumprod(1 + rng.normal(0.0005, 0.01, len(idx))), index=idx)
    o = outcome_range(eq)
    assert o["bad_year"] < o["typical_year"] < o["good_year"]
    assert 0 <= o["chance_of_loss"] <= 1 and o["bad_fall"] < 0
    assert outcome_range(eq) == o  # deterministic
    assert outcome_range(eq.iloc[:300]) is None  # too short


def test_low_volatility_prefers_calm_shares(synth):
    s = strategies.create("low_volatility", {"top_n": 4, "exit_rank": 6, "min_turnover_cr": 0})
    f = s.prepare(synth)
    i = len(synth.dates) - 1
    score, vol = f.row("score", i), f.row("vol", i)
    ok = np.isfinite(score)
    assert ok.sum() >= 3
    top = np.argmax(np.where(ok, score, -np.inf))
    assert vol[top] <= np.nanmedian(vol[ok]) + 1e-12
    st = Simulator(synth, s, BacktestConfig(initial_capital=500_000)).run()
    assert st.trades or st.positions
    assert all(f.reason.startswith("Steadiness rank") for f in st.fills if f.side == "buy")


def test_compare_can_show_the_effect_of_the_safety_switch(synth):
    res = compare(synth, [StrategySpec("momentum_rotation")], BacktestConfig(), without_safety=True)
    keys = [r["key"] for r in res["results"]]
    assert keys == ["momentum_rotation", "momentum_rotation:no_safety", "benchmark"]
    assert all("outcomes" in r for r in res["results"])
