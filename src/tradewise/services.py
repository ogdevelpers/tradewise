"""Application services used by both the API and the CLI."""

from __future__ import annotations

import logging
import math
import threading
from typing import Any, Mapping

import numpy as np
import pandas as pd

from . import indicators as ind
from . import strategies
from .backtest.engine import BacktestConfig
from .config import Settings
from .data.market import MarketData
from .data.store import PriceStore

log = logging.getLogger(__name__)


class MarketService:
    """Loads market data once and reloads only when the store changes."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.store = PriceStore(settings.prices_target)
        self._lock = threading.Lock()
        self._data: MarketData | None = None
        self._version: str | None = None
        self._features: dict[tuple, Any] = {}

    def data(self) -> MarketData:
        version = self.store.get_meta("version")
        with self._lock:
            if self._data is None or version != self._version:
                self._data = self.store.load_market(self.settings.benchmark_symbol)
                self._version = version
                self._features.clear()
                log.info("loaded %d symbols x %d days", len(self._data.symbols), len(self._data.dates))
            return self._data

    def features(self, strategy: strategies.Strategy):
        data = self.data()
        key = (strategy.name, strategy.params.model_dump_json(), self._version)
        with self._lock:
            if key not in self._features:
                if len(self._features) > 16:
                    self._features.clear()
                self._features[key] = strategy.prepare(data)
            return self._features[key]

    def meta(self) -> dict:
        try:
            data = self.data()
        except LookupError:
            return {"has_data": False, "provider": self.store.get_meta("provider"), "serverless": self.settings.serverless}
        return {
            "has_data": True,
            "provider": self.store.get_meta("provider"),
            "is_demo": self.store.get_meta("provider") == "synthetic",
            "version": self._version,
            "serverless": self.settings.serverless,
            "symbols": len(data.symbols),
            "first_date": data.dates[0].strftime("%Y-%m-%d"),
            "last_date": data.dates[-1].strftime("%Y-%m-%d"),
            "benchmark": self.settings.benchmark_symbol,
        }


def _f(x) -> float | None:
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return round(x, 4) if math.isfinite(x) else None


def market_context(data: MarketData) -> dict:
    b = data.benchmark
    sma200 = ind.sma(b, 200)
    last = b.iloc[-1]
    def chg(n: int):
        return _f(last / b.iloc[-1 - n] - 1) if len(b) > n else None
    above = bool(last > sma200.iloc[-1]) if pd.notna(sma200.iloc[-1]) else None
    breadth = (data.close.iloc[-1] > ind.sma(data.close, 200).iloc[-1])
    valid = ind.sma(data.close, 200).iloc[-1].notna()
    return {
        "date": data.dates[-1].strftime("%Y-%m-%d"),
        "benchmark": _f(last), "sma200": _f(sma200.iloc[-1]),
        "change_1d": chg(1), "change_1m": chg(21), "change_1y": chg(252),
        "regime": None if above is None else ("uptrend" if above else "downtrend"),
        "breadth_above_200dma": _f(breadth[valid].mean()) if valid.any() else None,
    }


def position_preview(close: float, stop: float | None, weight: float | None, cfg: BacktestConfig) -> dict:
    cap = cfg.initial_capital
    if stop is not None and close > stop:
        qty = math.floor(cap * cfg.risk_per_trade / (close - stop))
    elif weight is not None:
        qty = math.floor(cap * weight / close)
    else:
        return {"qty": 0}
    qty = min(qty, math.floor(cap * cfg.max_position_weight / close))
    return {"qty": int(qty), "value": round(qty * close, 2),
            "risk": round(qty * (close - stop), 2) if stop is not None else None}


def signals(svc: MarketService, strategy_name: str, params: Mapping[str, Any] | None = None,
            config: BacktestConfig | None = None) -> dict:
    """Entry candidates at the latest close (orders would go in at the next open)."""
    data = svc.data()
    strat = strategies.create(strategy_name, params)
    feats = svc.features(strat)
    cfg = config or BacktestConfig()
    i = len(data.dates) - 1
    ctx = strategies.DayContext(i=i, date=data.dates[i], features=feats, positions={}, pending=frozenset(),
                                can_enter=True)
    decision = strat.on_close(ctx)
    is_rebalance_strategy = "rebalance" in feats.series
    out = []
    for e in decision.entries[:50]:
        close = data.close[e.symbol].iloc[i]
        target = close + e.target_r * (close - e.stop) if e.stop is not None and e.target_r else None
        out.append({
            "symbol": e.symbol, "sector": data.sector_of(e.symbol), "action": "BUY", "reason": e.reason,
            "score": _f(e.score), "close": _f(close), "stop": _f(e.stop), "target": _f(target),
            **position_preview(close, e.stop, e.weight, cfg),
        })
    watch = []
    if not is_rebalance_strategy:
        for sym in data.symbols:
            if any(o["symbol"] == sym for o in out):
                continue
            checks = strat.explain(feats, i, sym)
            passed = sum(c["pass"] for c in checks)
            if checks and passed >= len(checks) - 2:
                watch.append({"symbol": sym, "sector": data.sector_of(sym), "passed": passed, "total": len(checks),
                              "missing": [c["rule"] for c in checks if not c["pass"]],
                              "missing_plain": [c.get("plain", c["rule"]) for c in checks if not c["pass"]]})
        watch.sort(key=lambda w: -w["passed"])
    next_rebalance = None
    ranking = []
    if is_rebalance_strategy:
        next_rebalance = (data.dates[-1] + pd.offsets.BMonthBegin(1)).strftime("%Y-%m-%d")  # first weekday of next month
        score = feats.row("score", i)
        valid = np.flatnonzero(np.isfinite(score))
        top_n = strat.max_positions
        weight = 1.0 / top_n
        for rank, j in enumerate(valid[np.argsort(-score[valid], kind="stable")][:top_n], start=1):
            sym = data.symbols[j]
            close = data.close[sym].iloc[i]
            ranking.append({"rank": rank, "symbol": sym, "sector": data.sector_of(sym), "close": _f(close),
                            **position_preview(close, None, weight, cfg)})
    return {"date": data.dates[i].strftime("%Y-%m-%d"), "strategy": strat.name, "label": strat.label,
            "capital": cfg.initial_capital, "risk_per_trade": cfg.risk_per_trade,
            "market_rising": bool(feats.series["regime"][i] > 0),
            "rebalance_today": bool(feats.series.get("rebalance", np.zeros(i + 1))[i] > 0) if is_rebalance_strategy else None,
            "next_rebalance": next_rebalance, "entries": out, "watch": watch[:20], "ranking": ranking}


def stock_analysis(svc: MarketService, symbol: str, strategy_name: str = "trend_pullback",
                   days: int = 260) -> dict:
    data = svc.data()
    symbol = symbol.upper()
    if symbol not in data.symbols:
        raise KeyError(f"unknown symbol {symbol!r}")
    bars = data.bars(symbol)
    if len(bars) < 30:
        raise LookupError(f"{symbol}: not enough history")
    c, h, l, v = bars["close"], bars["high"], bars["low"], bars["volume"]
    lines = {"ema20": ind.ema(c, 20), "sma50": ind.sma(c, 50), "sma200": ind.sma(c, 200)}
    macd_line, macd_sig, _ = ind.macd(c)
    rsi = ind.rsi(c, 14)
    atr = ind.atr(h, l, c, 14)
    adx = ind.adx(h, l, c, 14)
    tail = bars.iloc[-days:]
    chart = [{"date": d.strftime("%Y-%m-%d"), "open": _f(r.open), "high": _f(r.high), "low": _f(r.low),
              "close": _f(r.close), "volume": _f(r.volume),
              **{k: _f(s.loc[d]) for k, s in lines.items()}} for d, r in tail.iterrows()]
    last = c.iloc[-1]
    rel = (c.iloc[-1] / c.iloc[-64] - 1) - (data.benchmark.loc[bars.index[-1]] / data.benchmark.loc[bars.index[-64]] - 1) \
        if len(c) > 64 else float("nan")
    hi52 = h.iloc[-252:].max()
    technicals = {
        "rsi14": _f(rsi.iloc[-1]), "macd": _f(macd_line.iloc[-1]), "macd_signal": _f(macd_sig.iloc[-1]),
        "atr14": _f(atr.iloc[-1]), "atr_pct": _f(atr.iloc[-1] / last), "adx14": _f(adx.iloc[-1]),
        "volume_vs_20d": _f(v.iloc[-1] / v.iloc[-20:].mean()) if v.iloc[-20:].mean() > 0 else None,
        "rel_strength_3m": _f(rel), "from_52w_high": _f(last / hi52 - 1),
        "ema20": _f(lines["ema20"].iloc[-1]), "sma50": _f(lines["sma50"].iloc[-1]), "sma200": _f(lines["sma200"].iloc[-1]),
    }
    strat = strategies.create(strategy_name)
    feats = svc.features(strat)
    i = len(data.dates) - 1
    checklist = strat.explain(feats, i, symbol)
    ctx = strategies.DayContext(i=i, date=data.dates[i], features=feats, positions={}, pending=frozenset(),
                                can_enter=True)
    entry = next((e for e in strat.on_close(ctx).entries if e.symbol == symbol), None)
    signal = {"action": "WATCH" if not entry else "BUY", "reason": entry.reason if entry else None}
    if entry is not None:
        cfg = BacktestConfig()
        target = last + entry.target_r * (last - entry.stop) if entry.stop and entry.target_r else None
        signal.update({"score": _f(entry.score), "entry": _f(last), "stop": _f(entry.stop), "target": _f(target),
                       **position_preview(last, entry.stop, entry.weight, cfg)})
    prev = c.iloc[-2] if len(c) > 1 else last
    return {
        "symbol": symbol, "sector": data.sector_of(symbol), "date": bars.index[-1].strftime("%Y-%m-%d"),
        "close": _f(last), "change_1d": _f(last / prev - 1), "chart": chart, "technicals": technicals,
        "strategy": strat.name, "checklist": checklist, "signal": signal,
    }

