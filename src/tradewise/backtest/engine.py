"""Day-by-day portfolio simulator shared by backtesting and paper trading.

Timeline for each trading day ``i``:

1. **Open** - fill orders queued at the previous close (sells first, then buys
   by score). Buys are sized here, from the actual fill price.
2. **Intraday** - check stops against the day's low (gap-downs fill at the
   open) and part-profit targets against the high. If both are touched on the
   same bar, the stop is assumed to hit first (the conservative choice).
3. **Close** - mark to market, apply risk guardrails, ask the strategy for
   decisions and queue orders for the next open.

Because signals are made at the close and filled at the next open, no decision
can use a price that was not yet known.
"""

from __future__ import annotations

import logging
import math
from dataclasses import asdict, dataclass, field
from typing import Any

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..costs import CostModel
from ..data.market import MarketData, as_array
from ..strategies.base import DayContext, Decision, Features, PositionView, Strategy

log = logging.getLogger(__name__)


class BacktestConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    initial_capital: float = Field(500_000, gt=0)
    start: pd.Timestamp | None = None
    end: pd.Timestamp | None = None
    risk_per_trade: float = Field(0.01, gt=0, le=0.05, description="Fraction of equity lost if a stop is hit")
    max_positions: int | None = Field(None, ge=1, le=100, description="Default comes from the strategy")
    max_position_weight: float = Field(0.20, gt=0, le=1)
    max_sector_weight: float = Field(0.25, gt=0, le=1)
    max_monthly_drawdown: float = Field(0.06, gt=0, le=1, description="Pause new entries after this monthly loss")
    slippage: float = Field(0.001, ge=0, le=0.05)
    min_order_value: float = Field(1_000, ge=0)
    costs: CostModel = Field(default_factory=CostModel)

    @model_validator(mode="before")
    @classmethod
    def _coerce(cls, values: Any) -> Any:
        if isinstance(values, dict):
            values = dict(values)
            for k in ("start", "end"):
                if values.get(k) not in (None, ""):
                    values[k] = pd.Timestamp(values[k])
                elif k in values:
                    values[k] = None
            if isinstance(values.get("costs"), dict):
                values["costs"] = CostModel(**values["costs"])
        return values

    @model_validator(mode="after")
    def _order(self):
        if self.start is not None and self.end is not None and self.start > self.end:
            raise ValueError("start must be on or before end")
        return self


# ---------------------------------------------------------------------- state
@dataclass
class Position:
    symbol: str
    qty: int
    initial_qty: int
    entry_price: float
    entry_index: int
    entry_date: str
    entry_value: float
    stop: float | None
    initial_stop: float | None
    target: float | None
    target_fraction: float
    breakeven_after_target: bool
    partial_done: bool = False
    last_price: float = 0.0
    costs: float = 0.0
    proceeds: float = 0.0  # gross value of shares already sold
    sold_qty: int = 0
    reason: str = ""

    def view(self) -> PositionView:
        return PositionView(self.symbol, self.qty, self.entry_price, self.entry_index, self.stop, self.partial_done)


@dataclass
class Order:
    symbol: str
    side: str  # "buy" | "sell"
    reason: str
    signal_date: str
    score: float = 0.0
    fraction: float = 1.0
    stop: float | None = None
    target_r: float | None = None
    target_fraction: float = 0.5
    weight: float | None = None
    breakeven_after_target: bool = True


@dataclass
class Trade:
    symbol: str
    entry_date: str
    exit_date: str
    entry_price: float
    exit_price: float
    qty: int
    pnl: float
    return_pct: float
    r_multiple: float | None
    bars_held: int
    costs: float
    entry_reason: str
    exit_reason: str


@dataclass
class Fill:
    date: str
    symbol: str
    side: str
    qty: int
    price: float
    cost: float
    reason: str


@dataclass
class SimState:
    cash: float
    next_index: int = 0  # first day not yet processed
    positions: dict[str, Position] = field(default_factory=dict)
    pending: list[Order] = field(default_factory=list)
    trades: list[Trade] = field(default_factory=list)
    fills: list[Fill] = field(default_factory=list)
    equity: list[tuple[str, float, float]] = field(default_factory=list)  # date, equity, cash
    month: str = ""
    month_start_equity: float = 0.0
    paused_month: str = ""
    total_costs: float = 0.0
    last_decision_date: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "SimState":
        d = dict(d)
        d["positions"] = {k: Position(**v) for k, v in d.get("positions", {}).items()}
        d["pending"] = [Order(**o) for o in d.get("pending", [])]
        d["trades"] = [Trade(**t) for t in d.get("trades", [])]
        d["fills"] = [Fill(**f) for f in d.get("fills", [])]
        d["equity"] = [tuple(e) for e in d.get("equity", [])]
        return cls(**d)


# ------------------------------------------------------------------ simulator
class Simulator:
    def __init__(self, data: MarketData, strategy: Strategy, config: BacktestConfig | None = None,
                 state: SimState | None = None, features: Features | None = None):
        self.data = data
        self.strategy = strategy
        self.cfg = config or BacktestConfig()
        self.features = features if features is not None else strategy.prepare(data)
        if list(self.features.dates) != list(data.dates):
            raise ValueError("features were prepared on different dates")
        self.dates = data.dates
        self.symbols = data.symbols
        self.col = {s: j for j, s in enumerate(self.symbols)}
        self.o, self.h = as_array(data.open), as_array(data.high)
        self.l, self.c = as_array(data.low), as_array(data.close)
        self.max_positions = self.cfg.max_positions or strategy.max_positions
        self.state = state or SimState(cash=float(self.cfg.initial_capital))
        if state is None:
            self.state.next_index = self._first_index()

    # ----------------------------------------------------------- public API
    def _first_index(self) -> int:
        start = self.cfg.start
        if start is None:
            return min(self.strategy.warmup, max(len(self.dates) - 1, 0))
        return int(self.dates.searchsorted(start))

    def last_index(self) -> int:
        if self.cfg.end is None:
            return len(self.dates) - 1
        return int(self.dates.searchsorted(self.cfg.end, side="right")) - 1

    def run(self, until: int | None = None) -> SimState:
        stop = self.last_index() if until is None else min(until, len(self.dates) - 1)
        while self.state.next_index <= stop:
            self.step(self.state.next_index)
        return self.state

    def step(self, i: int) -> None:
        if i != self.state.next_index:
            raise ValueError(f"days must be processed in order (expected {self.state.next_index}, got {i})")
        date = self.dates[i]
        prev_equity = self._equity_at(i - 1) if i > 0 else self.state.cash
        self._open(i, date, prev_equity)
        self._intraday(i, date)
        equity = self._mark(i)
        self._close(i, date, equity)
        self.state.next_index = i + 1

    # ---------------------------------------------------------- helpers
    def _ds(self, d: pd.Timestamp) -> str:
        return d.strftime("%Y-%m-%d")

    def _price(self, arr: np.ndarray, i: int, sym: str) -> float:
        return float(arr[i, self.col[sym]])

    def _equity_at(self, i: int) -> float:
        if self.state.equity:
            return self.state.equity[-1][1]
        return self.state.cash + sum(p.qty * p.last_price for p in self.state.positions.values())

    def _sector_value(self, sector: str, i: int) -> float:
        total = 0.0
        for p in self.state.positions.values():
            if self.data.sector_of(p.symbol) == sector:
                px = self._price(self.c, i - 1, p.symbol) if i > 0 else p.last_price
                total += p.qty * (px if math.isfinite(px) else p.last_price)
        return total

    def _record_fill(self, date, sym, side, qty, price, cost, reason) -> None:
        self.state.fills.append(Fill(self._ds(date), sym, side, int(qty), round(price, 4), round(cost, 2), reason))
        self.state.total_costs += cost

    # ------------------------------------------------------------- phases
    def _open(self, i: int, date: pd.Timestamp, prev_equity: float) -> None:
        st, cfg = self.state, self.cfg
        if not st.pending:
            return
        carry: list[Order] = []
        sells = [o for o in st.pending if o.side == "sell"]
        buys = sorted((o for o in st.pending if o.side == "buy"), key=lambda o: o.score, reverse=True)
        for o in sells:
            pos = st.positions.get(o.symbol)
            if pos is None:
                continue
            px = self._price(self.o, i, o.symbol)
            if not math.isfinite(px):
                carry.append(o)  # no trading today (halt/holiday for this stock): try again tomorrow
                continue
            qty = pos.qty if o.fraction >= 1 else max(1, int(pos.qty * o.fraction))
            self._sell(pos, min(qty, pos.qty), px * (1 - cfg.slippage), i, date, o.reason)
        for o in buys:
            if o.symbol in st.positions:
                continue
            if len(st.positions) >= self.max_positions:
                continue
            px = self._price(self.o, i, o.symbol)
            if not math.isfinite(px):
                continue  # entries are not carried over: the setup is stale
            self._buy(o, px * (1 + cfg.slippage), i, date, prev_equity)
        st.pending = carry

    def _buy(self, o: Order, fill: float, i: int, date: pd.Timestamp, equity: float) -> None:
        st, cfg = self.state, self.cfg
        if o.stop is not None:
            risk_ps = fill - o.stop
            if risk_ps <= 0:
                log.debug("%s %s: opened below stop, entry skipped", self._ds(date), o.symbol)
                return
            qty = math.floor(equity * cfg.risk_per_trade / risk_ps)
        else:
            qty = math.floor(equity * float(o.weight) / fill)
        qty = min(qty, math.floor(equity * cfg.max_position_weight / fill))
        sector = self.data.sector_of(o.symbol)
        room = equity * cfg.max_sector_weight - self._sector_value(sector, i)
        qty = min(qty, math.floor(max(room, 0.0) / fill))
        # Cash: shrink until value + charges fit.
        while qty > 0 and qty * fill + cfg.costs.buy_cost(qty * fill) > st.cash:
            qty = min(qty - 1, math.floor(st.cash / (fill * (1 + 0.0015))))
        if qty <= 0 or qty * fill < cfg.min_order_value:
            return
        value = qty * fill
        cost = cfg.costs.buy_cost(value)
        st.cash -= value + cost
        target = None
        if o.stop is not None and o.target_r is not None:
            target = fill + o.target_r * (fill - o.stop)
        st.positions[o.symbol] = Position(
            symbol=o.symbol, qty=qty, initial_qty=qty, entry_price=fill, entry_index=i, entry_date=self._ds(date),
            entry_value=value, stop=o.stop, initial_stop=o.stop, target=target, target_fraction=o.target_fraction,
            breakeven_after_target=o.breakeven_after_target, last_price=fill, costs=cost, reason=o.reason,
        )
        self._record_fill(date, o.symbol, "buy", qty, fill, cost, o.reason)

    def _sell(self, pos: Position, qty: int, fill: float, i: int, date: pd.Timestamp, reason: str) -> None:
        st = self.state
        value = qty * fill
        cost = self.cfg.costs.sell_cost(value)
        st.cash += value - cost
        pos.qty -= qty
        pos.sold_qty += qty
        pos.proceeds += value
        pos.costs += cost
        self._record_fill(date, pos.symbol, "sell", qty, fill, cost, reason)
        if pos.qty == 0:
            self._close_trade(pos, i, date, reason)

    def _close_trade(self, pos: Position, i: int, date: pd.Timestamp, reason: str) -> None:
        pnl = pos.proceeds - pos.entry_value - pos.costs
        r = None
        if pos.initial_stop is not None:
            risk = (pos.entry_price - pos.initial_stop) * pos.initial_qty
            r = round(pnl / risk, 3) if risk > 0 else None
        self.state.trades.append(Trade(
            symbol=pos.symbol, entry_date=pos.entry_date, exit_date=self._ds(date),
            entry_price=round(pos.entry_price, 4), exit_price=round(pos.proceeds / pos.sold_qty, 4),
            qty=pos.initial_qty, pnl=round(pnl, 2), return_pct=round(pnl / pos.entry_value, 6), r_multiple=r,
            bars_held=i - pos.entry_index, costs=round(pos.costs, 2), entry_reason=pos.reason, exit_reason=reason,
        ))
        del self.state.positions[pos.symbol]

    def _intraday(self, i: int, date: pd.Timestamp) -> None:
        slip = self.cfg.slippage
        for sym in list(self.state.positions):
            pos = self.state.positions[sym]
            o, h, l = self._price(self.o, i, sym), self._price(self.h, i, sym), self._price(self.l, i, sym)
            if not (math.isfinite(o) and math.isfinite(l) and math.isfinite(h)):
                continue
            if pos.stop is not None and l <= pos.stop:
                fill = min(o, pos.stop) * (1 - slip)
                why = "Gap below stop-loss" if o <= pos.stop else "Stop-loss hit"
                self._sell(pos, pos.qty, fill, i, date, why)
                continue
            if pos.target is not None and not pos.partial_done and h >= pos.target:
                qty = pos.qty if pos.target_fraction >= 1 else max(1, int(pos.qty * pos.target_fraction))
                fill = max(o, pos.target)  # resting limit order: no slippage
                pos.partial_done = True
                if pos.breakeven_after_target and pos.stop is not None:
                    pos.stop = max(pos.stop, pos.entry_price)
                self._sell(pos, min(qty, pos.qty), fill, i, date, "Profit target reached")

    def _mark(self, i: int) -> float:
        st = self.state
        invested = 0.0
        for sym, pos in st.positions.items():
            px = self._price(self.c, i, sym)
            if math.isfinite(px):
                pos.last_price = px
            invested += pos.qty * pos.last_price
        equity = st.cash + invested
        st.equity.append((self._ds(self.dates[i]), round(equity, 2), round(st.cash, 2)))
        return equity

    def _close(self, i: int, date: pd.Timestamp, equity: float) -> None:
        st = self.state
        month = date.strftime("%Y-%m")
        if month != st.month:
            prev = st.equity[-2][1] if len(st.equity) >= 2 else self.cfg.initial_capital
            st.month, st.month_start_equity = month, prev
        if st.month_start_equity > 0 and equity / st.month_start_equity - 1 <= -self.cfg.max_monthly_drawdown:
            if st.paused_month != month:
                log.info("%s: monthly drawdown limit hit, pausing new entries", self._ds(date))
            st.paused_month = month
        can_enter = st.paused_month != month

        ctx = DayContext(
            i=i, date=date, features=self.features,
            positions={s: p.view() for s, p in st.positions.items()},
            pending=frozenset(o.symbol for o in st.pending),
            can_enter=can_enter,
        )
        decision: Decision = self.strategy.on_close(ctx)
        self._apply(decision, i, date, equity)
        st.last_decision_date = self._ds(date)

    def _apply(self, d: Decision, i: int, date: pd.Timestamp, equity: float) -> None:
        st, ds = self.state, self._ds(date)
        for sym, new_stop in d.stop_updates.items():
            pos = st.positions.get(sym)
            if pos is not None and pos.stop is not None and math.isfinite(new_stop) and new_stop > pos.stop:
                pos.stop = float(new_stop)
        exiting = set()
        queued_sells = {o.symbol for o in st.pending if o.side == "sell" and o.fraction >= 1}
        for e in d.exits:
            if e.symbol in st.positions and e.symbol not in queued_sells:
                st.pending.append(Order(e.symbol, "sell", e.reason, ds, fraction=e.fraction))
                if e.fraction >= 1:
                    exiting.add(e.symbol)
        slots = self.max_positions - (len(st.positions) - len(exiting)) - sum(1 for o in st.pending if o.side == "buy")
        queued = {o.symbol for o in st.pending}
        for e in d.entries:
            if slots <= 0:
                break
            if e.symbol in st.positions or e.symbol in queued:
                continue
            st.pending.append(Order(
                e.symbol, "buy", e.reason, ds, score=e.score, stop=e.stop, target_r=e.target_r,
                target_fraction=e.target_fraction, weight=e.weight, breakeven_after_target=e.breakeven_after_target,
            ))
            queued.add(e.symbol)
            slots -= 1


def run_backtest(data: MarketData, strategy: Strategy, config: BacktestConfig | None = None) -> SimState:
    return Simulator(data, strategy, config).run()
