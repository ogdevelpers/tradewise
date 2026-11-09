import json

import pytest

from tradewise import strategies
from tradewise.backtest.engine import BacktestConfig, SimState, Simulator
from tradewise.costs import ZERO_COSTS, CostModel
from tradewise.strategies.base import Decision, EntrySignal, ExitSignal

from conftest import Scripted, bar, make_market

LOOSE = dict(initial_capital=100_000, slippage=0.0, costs=ZERO_COSTS, max_position_weight=1.0,
             max_sector_weight=1.0, max_monthly_drawdown=1.0, min_order_value=0)


def cfg(**kw):
    return BacktestConfig(**{**LOOSE, **kw})


def entry(sym="AAA", stop=95.0, target_r=2.0, score=1.0, weight=None):
    if weight is not None:
        return EntrySignal(sym, score, "test", weight=weight)
    return EntrySignal(sym, score, "test", stop=stop, target_r=target_r)


def run(prices, script, max_pos=10, **kw):
    data = make_market(prices, sectors=kw.pop("sectors", None))
    sim = Simulator(data, Scripted(script, max_pos), cfg(**kw))
    sim.state.next_index = 0
    return sim, sim.run()


def test_risk_sizing_target_then_breakeven_stop():
    prices = {"AAA": [bar(100), bar(100), bar(105, 110.5, 104, 108), bar(101, 101, 99, 100), bar(100)]}
    sim, st = run(prices, {0: Decision(entries=[entry()])})
    buy, part, rest = st.fills
    assert (buy.side, buy.qty, buy.price) == ("buy", 200, 100)  # 1% of 1L / (100 - 95)
    assert (part.qty, part.price, part.reason) == (100, 110, "Profit target reached")
    assert (rest.qty, rest.price, rest.reason) == (100, 100, "Stop-loss hit")  # moved to breakeven
    (t,) = st.trades
    assert t.pnl == 1000 and t.r_multiple == 1.0 and t.bars_held == 2
    assert st.equity[-1][1] == pytest.approx(101_000)


def test_gap_down_fills_at_open_and_stop_beats_target_same_bar():
    gap = {"AAA": [bar(100), bar(100), bar(90, 91, 89, 90)]}
    _, st = run(gap, {0: Decision(entries=[entry()])})
    assert st.fills[-1].price == 90 and st.fills[-1].reason == "Gap below stop-loss"
    assert st.trades[0].r_multiple == -2.0

    both = {"AAA": [bar(100), bar(100), bar(100, 111, 94, 100)]}
    _, st = run(both, {0: Decision(entries=[entry()])})
    assert [f.reason for f in st.fills[1:]] == ["Stop-loss hit"] and st.trades[0].pnl == -1000


def test_entry_skipped_when_open_is_below_stop():
    _, st = run({"AAA": [bar(100), bar(94), bar(94)]}, {0: Decision(entries=[entry()])})
    assert st.fills == [] and st.cash == 100_000


def test_slippage_and_costs_are_charged():
    prices = {"AAA": [bar(100), bar(100), bar(100), bar(100)]}
    _, st = run(prices, {0: Decision(entries=[entry()]), 1: Decision(exits=[ExitSignal("AAA", "x")])},
                slippage=0.001, costs=CostModel())
    buy, sell = st.fills
    assert buy.price == pytest.approx(100.1) and sell.price == pytest.approx(99.9)
    t = st.trades[0]
    assert t.pnl == pytest.approx(-(0.2 * buy.qty) - buy.cost - sell.cost, abs=0.02)
    assert st.cash == pytest.approx(100_000 + t.pnl, abs=0.02)


def test_cash_is_never_negative_and_orders_ranked_by_score():
    prices = {s: [bar(100)] * 4 for s in ("AAA", "BBB")}
    script = {0: Decision(entries=[entry("AAA", weight=0.6, score=1), entry("BBB", weight=0.6, score=2)])}
    _, st = run(prices, script, costs=CostModel())
    assert st.fills[0].symbol == "BBB" and st.fills[0].qty == 600
    assert st.fills[1].symbol == "AAA" and st.fills[1].qty < 400
    assert min(c for _, _, c in st.equity) >= 0


def test_max_positions_and_sector_cap():
    prices = {s: [bar(100)] * 3 for s in ("AAA", "BBB", "CCC")}
    script = {0: Decision(entries=[entry(s, weight=0.2) for s in ("AAA", "BBB", "CCC")])}
    _, st = run(prices, script, max_pos=2)
    assert len(st.positions) == 2

    sectors = {"AAA": "IT", "BBB": "IT", "CCC": "Banks"}
    _, st = run(prices, script, sectors=sectors, max_sector_weight=0.25)
    qty = {s: p.qty for s, p in st.positions.items()}
    assert qty["AAA"] == 200 and qty["BBB"] == 50 and qty["CCC"] == 200


def test_exit_carried_over_when_stock_does_not_trade():
    prices = {"AAA": [bar(100), bar(100), bar(100), None, bar(102), bar(102)], "BBB": [bar(50)] * 6}
    _, st = run(prices, {0: Decision(entries=[entry(weight=0.5)]), 2: Decision(exits=[ExitSignal("AAA", "x")])})
    sell = [f for f in st.fills if f.side == "sell"]
    assert len(sell) == 1 and sell[0].price == 102 and sell[0].date == st.equity[4][0]


def test_monthly_drawdown_pauses_entries():
    rows = [bar(100)] * 3 + [bar(80)] * 4
    prices = {"AAA": rows, "BBB": [bar(10)] * 7}
    data = make_market(prices, start="2024-03-01")
    strat = Scripted({0: Decision(entries=[entry(weight=1.0)])})
    sim = Simulator(data, strat, cfg(max_monthly_drawdown=0.06))
    sim.state.next_index = 0
    sim.run()
    assert [c.can_enter for c in strat.seen][:3] == [True, True, True]
    assert not strat.seen[3].can_enter


def test_accounting_identity_on_full_backtest(synth):
    strat = strategies.create("trend_pullback")
    st = Simulator(synth, strat, BacktestConfig(start="2016-06-01")).run()
    assert st.trades, "expected some trades on synthetic data"
    open_value = sum(p.qty * p.last_price for p in st.positions.values())
    assert st.equity[-1][1] == pytest.approx(st.cash + open_value, abs=0.05)
    realised = sum(t.pnl for t in st.trades)
    open_pnl = sum(p.proceeds + p.qty * p.last_price - p.entry_value - p.costs for p in st.positions.values())
    assert st.equity[-1][1] - 500_000 == pytest.approx(realised + open_pnl, abs=1.0)
    assert all(c >= -1e-6 for _, _, c in st.equity)


@pytest.mark.parametrize("name", ["trend_pullback", "momentum_rotation"])
def test_resume_from_saved_state_matches_single_run(synth, name):
    conf = BacktestConfig(start="2016-06-01")
    full = Simulator(synth, strategies.create(name), conf).run()
    half = Simulator(synth, strategies.create(name), conf)
    half.run(until=1000)
    saved = json.loads(json.dumps(half.state.to_dict()))
    resumed = Simulator(synth, strategies.create(name), conf, state=SimState.from_dict(saved)).run()
    assert resumed.equity == full.equity
    assert [t.__dict__ for t in resumed.trades] == [t.__dict__ for t in full.trades]


def test_steps_must_be_in_order(synth):
    sim = Simulator(synth, strategies.create("trend_pullback"))
    with pytest.raises(ValueError):
        sim.step(sim.state.next_index + 1)


def test_config_validation():
    with pytest.raises(ValueError):
        BacktestConfig(start="2020-01-01", end="2019-01-01")
    with pytest.raises(ValueError):
        BacktestConfig(risk_per_trade=0.5)
    c = BacktestConfig(**{"start": "", "costs": {"stt_rate": 0.002}})
    assert c.start is None and c.costs.stt_rate == 0.002
