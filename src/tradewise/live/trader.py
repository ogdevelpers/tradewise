"""Real-money trading through Zerodha, with safety rails.

How a day works
---------------
1. After the market closes, prices update (``auto.Updater``).
2. You log in to Zerodha from the app (the exchange requires a manual login
   every day). Tradewise then:
   * reconciles: reads your Zerodha orders, holdings and positions, and updates
     its own record of what it bought for you;
   * protects: makes sure every position has a GTT at Zerodha holding the
     safety exit (and the profit step), so exits work even if Tradewise is down;
   * plans: runs the strategy on today's close and lists tomorrow's orders.
3. You review the plan and press "Place these orders". They go to Zerodha as
   after-market orders (AMO) and execute at the next open:
   * buys are LIMIT orders a little above today's close (never chase a big gap-up),
   * sells are MARKET orders with Zerodha's automatic market protection.

Safety rails: real trading is off until you switch it on; a hard money limit;
a cap on new buys per day; an order is never placed twice; any Zerodha error
stops the run; an emergency stop cancels Tradewise's pending orders.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, time
from typing import Callable

import numpy as np
from pydantic import BaseModel, ConfigDict, Field

from .. import strategies
from ..broker.kite import Broker, BrokerError, KiteClient
from ..costs import CostModel
from ..db import Database
from ..market_time import IST, last_complete_session, now_ist
from ..risk import market_scale
from ..strategies.base import DayContext, PositionView

log = logging.getLogger(__name__)

TAG = "tradewise"
CONFIRM_PHRASE = "I UNDERSTAND THE RISK"
AMO_OPEN, AMO_CLOSE = time(16, 0), time(8, 55)  # window in which Tradewise places after-market orders
PENDING_STATUSES = {"AMO REQ RECEIVED", "OPEN", "OPEN PENDING", "PUT ORDER REQ RECEIVED", "VALIDATION PENDING",
                    "TRIGGER PENDING", "MODIFY PENDING", "MODIFY VALIDATION PENDING", "CANCEL PENDING"}
DONE_STATUSES = {"COMPLETE", "CANCELLED", "REJECTED"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS live_kv (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS live_positions (
    symbol TEXT PRIMARY KEY, qty INTEGER NOT NULL, entry_price {real} NOT NULL, entry_date TEXT NOT NULL,
    stop {real}, initial_stop {real}, target {real}, target_fraction {real}, partial_done INTEGER NOT NULL DEFAULT 0,
    gtt_id TEXT, gtt_qty INTEGER, strategy TEXT, reason TEXT
);
CREATE TABLE IF NOT EXISTS live_orders (
    order_id TEXT PRIMARY KEY, plan_id TEXT NOT NULL, symbol TEXT NOT NULL, side TEXT NOT NULL, qty INTEGER NOT NULL,
    order_type TEXT NOT NULL, limit_price {real}, stop {real}, target_r {real}, target_fraction {real}, reason TEXT,
    status TEXT NOT NULL, filled INTEGER NOT NULL DEFAULT 0, avg_price {real}, placed_at TEXT NOT NULL,
    settled INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS live_events (id TEXT PRIMARY KEY, at TEXT NOT NULL, level TEXT NOT NULL, message TEXT NOT NULL)
"""


class LiveConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    strategy: str = "trend_pullback"
    capital_limit: float = Field(0, ge=0, description="Most money (Rs) Tradewise may have invested at once")
    max_new_per_day: int = Field(2, ge=0, le=10, description="New buys per day, at most")
    risk_per_trade: float = Field(0.01, gt=0, le=0.02)
    max_position_weight: float = Field(0.20, gt=0, le=0.5)
    max_sector_weight: float = Field(0.30, gt=0, le=1)
    entry_buffer: float = Field(0.005, ge=0, le=0.03, description="Buy limit this much above the close")
    stop_buffer: float = Field(0.02, ge=0.005, le=0.05, description="Safety-exit sell limit this far below the trigger")
    safety_switch: bool = Field(True, description="Buy smaller when the market is jumpy")


@dataclass
class PlannedOrder:
    symbol: str
    side: str  # BUY / SELL
    qty: int
    order_type: str  # LIMIT / MARKET
    price: float | None
    reason: str
    stop: float | None = None
    target_r: float | None = None
    target_fraction: float = 0.5
    close: float | None = None
    value: float | None = None

    def key(self) -> str:
        return f"{self.side}:{self.symbol}"


@dataclass
class Plan:
    plan_id: str
    data_date: str
    created_at: str
    strategy: str
    orders: list[PlannedOrder] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    executed_at: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Plan":
        d = dict(d)
        d["orders"] = [PlannedOrder(**o) for o in d.get("orders", [])]
        return cls(**d)


class LiveError(RuntimeError):
    """A clear, user-facing reason why something can't be done right now."""


def round_tick(price: float, tick: float, up: bool) -> float:
    tick = tick if tick and tick > 0 else 0.05
    steps = price / tick
    steps = math.ceil(steps - 1e-9) if up else math.floor(steps + 1e-9)
    return round(steps * tick, 2)


def session_expiry(created: datetime) -> datetime:
    """Kite access tokens stop working at about 6 am IST the next morning."""
    c = created.astimezone(IST)
    day = c.date() if c.time() < time(6, 0) else c.date() + timedelta(days=1)
    return datetime.combine(day, time(6, 0), tzinfo=IST)


class LiveTrader:
    def __init__(self, target, market, api_key: str | None = None, api_secret: str | None = None,
                 broker_factory: Callable[[str | None], Broker] | None = None,
                 clock: Callable[[], datetime] = now_ist, costs: CostModel | None = None):
        self.db = Database(target)
        self.db.ensure_schema(_SCHEMA.replace("{real}", self.db.real))
        self.market = market
        self._env_key, self._env_secret = api_key, api_secret  # from the server's environment, if set
        self.clock = clock
        self.costs = costs or CostModel()
        self._factory = broker_factory or (lambda token: KiteClient(self.api_key or "", token))
        self._lock = threading.Lock()

    # =========================================================== storage
    def _get(self, key: str, default=None):
        with self.db.connect() as c:
            row = c.execute("SELECT value FROM live_kv WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def _put(self, key: str, value) -> None:
        with self.db.connect() as c:
            c.execute("INSERT INTO live_kv(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                      (key, json.dumps(value)))

    def event(self, message: str, level: str = "info") -> None:
        log.log(logging.WARNING if level != "info" else logging.INFO, "live: %s", message)
        with self.db.connect() as c:
            c.execute("INSERT INTO live_events(id,at,level,message) VALUES (?,?,?,?)",
                      (uuid.uuid4().hex, self.clock().isoformat(timespec="seconds"), level, message))

    def events(self, limit: int = 50) -> list[dict]:
        with self.db.connect() as c:
            rows = c.execute("SELECT at,level,message FROM live_events ORDER BY at DESC, id DESC LIMIT ?",
                             (limit,)).fetchall()
        return [{"at": a, "level": lv, "message": m} for a, lv, m in rows]

    def positions(self) -> list[dict]:
        cols = ["symbol", "qty", "entry_price", "entry_date", "stop", "initial_stop", "target", "target_fraction",
                "partial_done", "gtt_id", "gtt_qty", "strategy", "reason"]
        with self.db.connect() as c:
            rows = c.execute(f"SELECT {','.join(cols)} FROM live_positions ORDER BY symbol").fetchall()
        return [dict(zip(cols, r)) for r in rows]

    def _save_position(self, p: dict) -> None:
        cols = ["symbol", "qty", "entry_price", "entry_date", "stop", "initial_stop", "target", "target_fraction",
                "partial_done", "gtt_id", "gtt_qty", "strategy", "reason"]
        with self.db.connect() as c:
            c.execute(
                f"INSERT INTO live_positions({','.join(cols)}) VALUES ({','.join('?' * len(cols))}) "
                "ON CONFLICT(symbol) DO UPDATE SET " + ",".join(f"{k}=excluded.{k}" for k in cols[1:]),
                [p.get(k) for k in cols])

    def _delete_position(self, symbol: str) -> None:
        with self.db.connect() as c:
            c.execute("DELETE FROM live_positions WHERE symbol=?", (symbol,))

    def orders(self, only_open: bool = False) -> list[dict]:
        cols = ["order_id", "plan_id", "symbol", "side", "qty", "order_type", "limit_price", "stop", "target_r",
                "target_fraction", "reason", "status", "filled", "avg_price", "placed_at", "settled"]
        q = f"SELECT {','.join(cols)} FROM live_orders" + (" WHERE settled=0" if only_open else "") + \
            " ORDER BY placed_at DESC"
        with self.db.connect() as c:
            rows = c.execute(q).fetchall()
        return [dict(zip(cols, r)) for r in rows]

    def _record_order(self, order_id: str, plan_id: str, o: PlannedOrder) -> None:
        with self.db.connect() as c:
            c.execute(
                "INSERT INTO live_orders(order_id,plan_id,symbol,side,qty,order_type,limit_price,stop,target_r,"
                "target_fraction,reason,status,filled,avg_price,placed_at,settled) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (order_id, plan_id, o.symbol, o.side, o.qty, o.order_type, o.price, o.stop, o.target_r,
                 o.target_fraction, o.reason, "PLACED", 0, None, self.clock().isoformat(timespec="seconds"), 0))

    def _update_order(self, order_id: str, **kw) -> None:
        sets = ",".join(f"{k}=?" for k in kw)
        with self.db.connect() as c:
            c.execute(f"UPDATE live_orders SET {sets} WHERE order_id=?", [*kw.values(), order_id])

    # ======================================================= credentials
    @property
    def api_key(self) -> str | None:
        return self._env_key or (self._get("kite_app") or {}).get("api_key")

    @property
    def api_secret(self) -> str | None:
        return self._env_secret or (self._get("kite_app") or {}).get("api_secret")

    def set_credentials(self, api_key: str, api_secret: str) -> None:
        """Save the Kite Connect app's key and secret (only used when they aren't set on the server)."""
        api_key, api_secret = (api_key or "").strip(), (api_secret or "").strip()
        ok = lambda v: 6 <= len(v) <= 64 and v.isalnum()  # noqa: E731
        if not (ok(api_key) and ok(api_secret)):
            raise LiveError("That doesn't look like a Kite Connect API key and secret (letters and numbers only).")
        if self._env_key:
            raise LiveError("The API key is set on the server; change it there.")
        self._put("kite_app", {"api_key": api_key, "api_secret": api_secret})
        self._put("session", None)
        self.event("Zerodha app details saved.")

    # ============================================================ config
    def config(self) -> LiveConfig:
        return LiveConfig(**self._get("config", {}))

    def set_config(self, changes: dict, confirm: str | None = None) -> LiveConfig:
        cfg = self.config().model_dump()
        turning_on = bool(changes.get("enabled")) and not cfg["enabled"]
        cfg.update(changes)
        new = LiveConfig(**cfg)
        strategies.create(new.strategy)  # validates the name
        if turning_on:
            if (confirm or "").strip().upper() != CONFIRM_PHRASE:
                raise LiveError(f'To turn on real trading, type "{CONFIRM_PHRASE}".')
            if new.capital_limit < 10_000:
                raise LiveError("Set a money limit of at least Rs 10,000 before turning on real trading.")
            if not self.api_key or not self.api_secret:
                raise LiveError("Zerodha API key and secret are not set on the server yet.")
        was_on = self.config().enabled
        self._put("config", new.model_dump())
        if turning_on:
            self.event(f"Real trading turned ON (limit Rs {new.capital_limit:,.0f}, {new.strategy}).", "warn")
        elif was_on and not new.enabled:
            self.event("Real trading turned OFF. Safety exits for shares you own stay in place at Zerodha.", "warn")
        return new

    # =========================================================== session
    def login_url(self) -> str:
        if not self.api_key:
            raise LiveError("Zerodha API key is not set on the server (KITE_API_KEY).")
        return KiteClient(self.api_key).login_url()

    def complete_login(self, request_token: str) -> dict:
        if not (self.api_key and self.api_secret):
            raise LiveError("Zerodha API key and secret are not set on the server.")
        if not request_token or len(request_token) > 128:
            raise LiveError("Zerodha did not send a valid login token. Please try logging in again.")
        client = self._factory(None)
        data = client.create_session(request_token, self.api_secret)
        now = self.clock()
        sess = {"access_token": data["access_token"], "user_id": data.get("user_id"),
                "user_name": data.get("user_name") or data.get("user_shortname"),
                "created_at": now.isoformat(), "expires_at": session_expiry(now).isoformat()}
        self._put("session", sess)
        self.event(f"Logged in to Zerodha as {sess['user_name'] or sess['user_id']}.")
        return self.session_info()

    def _session(self) -> dict | None:
        s = self._get("session")
        if not s:
            return None
        if datetime.fromisoformat(s["expires_at"]) <= self.clock():
            return None
        return s

    def session_info(self) -> dict | None:
        s = self._session()
        if not s:
            return None
        return {"user_name": s.get("user_name"), "user_id": s.get("user_id"), "expires_at": s["expires_at"]}

    def logout(self) -> None:
        self._put("session", None)

    def broker(self) -> Broker:
        s = self._session()
        if not s:
            raise LiveError("Log in to Zerodha first (needed once a day).")
        return self._factory(s["access_token"])

    def _call(self, fn, *a, **kw):
        try:
            return fn(*a, **kw)
        except BrokerError as exc:
            if exc.session_expired:
                self._put("session", None)
                raise LiveError("Your Zerodha login has expired. Please log in again.") from None
            raise

    # ========================================================= reconcile
    def sync(self) -> list[str]:
        """Bring Tradewise's records in line with Zerodha and make sure every position is protected."""
        with self._lock:
            return self._sync()

    def _sync(self) -> list[str]:
        b = self.broker()
        notes: list[str] = []
        today = self.clock().strftime("%Y-%m-%d")
        positions = {p["symbol"]: p for p in self.positions()}

        # 1. Orders Tradewise placed: did they fill?
        for o in self.orders(only_open=True):
            hist = self._call(b.order_history, o["order_id"])
            if not hist:
                continue
            last = hist[-1]
            status = str(last.get("status", "")).upper()
            if status not in DONE_STATUSES:
                self._update_order(o["order_id"], status=status)
                continue
            filled = int(last.get("filled_quantity") or 0)
            avg = float(last.get("average_price") or 0) or None
            self._update_order(o["order_id"], status=status, filled=filled, avg_price=avg, settled=1)
            if status == "REJECTED":
                why = last.get("status_message") or "no reason given"
                self.event(f"Zerodha rejected the order to {o['side'].lower()} {o['symbol']}: {why}", "warn")
            if filled <= 0 or avg is None:
                if status != "REJECTED":
                    notes.append(f"{o['side'].title()} order for {o['symbol']} did not fill ({status.lower()}).")
                continue
            if o["side"] == "BUY":
                p = positions.get(o["symbol"])
                if p:  # topped up (should be rare)
                    total = p["qty"] + filled
                    p["entry_price"] = (p["entry_price"] * p["qty"] + avg * filled) / total
                    p["qty"] = total
                else:
                    target = None
                    if o["stop"] and o["target_r"]:
                        target = avg + o["target_r"] * (avg - o["stop"])
                    p = {"symbol": o["symbol"], "qty": filled, "entry_price": avg, "entry_date": today,
                         "stop": o["stop"], "initial_stop": o["stop"], "target": target,
                         "target_fraction": o["target_fraction"] or 0.5, "partial_done": 0, "gtt_id": None,
                         "gtt_qty": None, "strategy": self.config().strategy, "reason": o["reason"]}
                positions[o["symbol"]] = p
                self._save_position(p)
                self.event(f"Bought {filled} {o['symbol']} at Rs {avg:,.2f}.")
            else:
                p = positions.get(o["symbol"])
                if p:
                    pnl = (avg - p["entry_price"]) * min(filled, p["qty"])
                    p["qty"] -= filled
                    self.event(f"Sold {filled} {o['symbol']} at Rs {avg:,.2f} ({o['reason']}). "
                               f"Result about Rs {pnl:,.0f} before charges.")
                    if p["qty"] <= 0:
                        self._delete_position(o["symbol"])
                        positions.pop(o["symbol"])
                    else:
                        self._save_position(p)

        # 2. Holdings: shares sold by a GTT (or by you, by hand).
        held = self._effective_holdings(b)
        for sym, p in list(positions.items()):
            have = held.get(sym, 0)
            if have >= p["qty"]:
                continue
            sold = p["qty"] - have
            if have <= 0:
                self.event(f"{sym}: all {p['qty']} shares are no longer in your account "
                           "(safety exit hit, or sold outside Tradewise).", "warn")
                self._delete_position(sym)
                positions.pop(sym)
                continue
            if not p["partial_done"] and p["target"]:
                p["partial_done"] = 1
                p["stop"] = max(p["stop"] or 0, p["entry_price"])  # rest is now protected at the buy price
                self.event(f"{sym}: profit step reached, {sold} shares sold. Safety exit for the rest moved "
                           f"to the buy price (Rs {p['entry_price']:,.2f}).")
            else:
                self.event(f"{sym}: {sold} shares are no longer in your account.", "warn")
            p["qty"] = have
            p["gtt_id"] = None if p.get("gtt_qty") != have else p["gtt_id"]
            self._save_position(p)

        # 3. Protection: every position needs an active GTT that matches its quantity.
        if positions:
            active = {str(g.get("id")): g for g in self._call(b.gtts) if str(g.get("status", "")).lower() == "active"}
            ltp = self._call(b.ltp, list(positions))
            ticks = self._call(b.tick_sizes, list(positions))
            for sym, p in positions.items():
                if p["gtt_id"] and str(p["gtt_id"]) in active and p.get("gtt_qty") == p["qty"]:
                    continue
                if p["gtt_id"] and str(p["gtt_id"]) in active:
                    self._call(b.delete_gtt, str(p["gtt_id"]))
                notes += self._protect(b, p, ltp.get(sym), ticks.get(sym, 0.05))
        self._put("last_sync", self.clock().isoformat(timespec="seconds"))
        return notes

    def _effective_holdings(self, b: Broker) -> dict[str, int]:
        """Shares you own now: settled + unsettled holdings, plus today's delivery buys/sells."""
        out: dict[str, int] = {}
        for h in self._call(b.holdings):
            if h.get("exchange", "NSE") not in ("NSE", "BSE"):
                continue
            out[h["tradingsymbol"]] = out.get(h["tradingsymbol"], 0) + int(h.get("quantity") or 0) + \
                int(h.get("t1_quantity") or 0)
        for p in (self._call(b.positions) or {}).get("net", []):
            if p.get("product") == "CNC":
                out[p["tradingsymbol"]] = out.get(p["tradingsymbol"], 0) + int(p.get("quantity") or 0)
        return out

    def _protect(self, b: Broker, p: dict, last_price: float | None, tick: float) -> list[str]:
        sym, cfg = p["symbol"], self.config()
        if p["stop"] is None:
            return []  # rotation holdings are reviewed monthly, no fixed safety exit
        if last_price is None:
            return [f"{sym}: no live price, could not place the safety exit. Will retry next time."]
        stop_trig = round_tick(p["stop"], tick, up=False)
        stop_px = round_tick(p["stop"] * (1 - cfg.stop_buffer), tick, up=False)
        if last_price <= stop_trig:
            self._put("forced_exits", sorted(set(self._get("forced_exits", [])) | {sym}))
            return [f"{sym} is already below its safety exit; it will be sold at the next open."]
        legs = [{"transaction_type": "SELL", "quantity": p["qty"], "price": stop_px}]
        if not p["partial_done"] and p["target"] and last_price < p["target"]:
            part = max(1, int(p["qty"] * (p["target_fraction"] or 0.5)))
            tgt = round_tick(p["target"], tick, up=True)
            legs.append({"transaction_type": "SELL", "quantity": part, "price": tgt})
            gid = self._call(b.place_gtt, sym, "two-leg", [stop_trig, tgt], last_price, legs)
            msg = f"{sym}: safety exit at Rs {stop_trig:,.2f} and profit step at Rs {tgt:,.2f} are now set at Zerodha."
        else:
            gid = self._call(b.place_gtt, sym, "single", [stop_trig], last_price, legs)
            msg = f"{sym}: safety exit at Rs {stop_trig:,.2f} is now set at Zerodha."
        p["gtt_id"], p["gtt_qty"] = str(gid), p["qty"]
        self._save_position(p)
        self.event(msg)
        return []

    # ============================================================== plan
    def plan(self) -> Plan:
        """Reconcile, then work out tomorrow's orders. Nothing is sent to Zerodha here."""
        with self._lock:
            cfg = self.config()
            data = self.market.data()
            last = data.dates[-1]
            expected = last_complete_session(self.clock())
            if last < expected:
                raise LiveError(f"Prices are only up to {last:%a %d %b}. Wait for today's closing prices "
                                "before planning real orders.")
            notes = self._sync()
            b = self.broker()
            strat = strategies.create(cfg.strategy)
            feats = self.market.features(strat)
            i = len(data.dates) - 1
            held = {p["symbol"]: p for p in self.positions()}
            views = {}
            for s, p in held.items():
                if s not in feats.col:
                    continue
                entry_i = int(np.searchsorted(data.dates.values, np.datetime64(p["entry_date"])))
                views[s] = PositionView(s, p["qty"], p["entry_price"], min(entry_i, i), p["stop"], bool(p["partial_done"]))
            pending = frozenset(o["symbol"] for o in self.orders(only_open=True))
            ctx = DayContext(i=i, date=data.dates[i], features=feats, positions=views, pending=pending,
                             can_enter=cfg.enabled)
            decision = strat.on_close(ctx)
            close = data.close.iloc[i]
            orders: list[PlannedOrder] = []

            exits = {e.symbol: e for e in decision.exits}
            for sym in self._get("forced_exits", []):
                if sym in held and sym not in exits:
                    exits[sym] = strategies.ExitSignal(sym, "Below the safety exit")
            for sym, e in exits.items():
                p = held.get(sym)
                if not p or sym in pending:
                    continue
                qty = p["qty"] if e.fraction >= 1 else max(1, int(p["qty"] * e.fraction))
                orders.append(PlannedOrder(sym, "SELL", qty, "MARKET", None, e.reason, close=float(close[sym]),
                                           value=round(qty * float(close[sym]), 2)))

            if cfg.enabled and cfg.max_new_per_day > 0 and decision.entries:
                orders += self._size_entries(b, cfg, decision.entries, held, exits, close, data, notes)
            elif not cfg.enabled:
                notes.append("Real trading is off, so no new buys are planned.")
            digest = hashlib.sha256(json.dumps([asdict(o) for o in orders], sort_keys=True).encode()).hexdigest()[:10]
            plan = Plan(plan_id=f"{last:%Y%m%d}-{digest}", data_date=last.strftime("%Y-%m-%d"),
                        created_at=self.clock().isoformat(timespec="seconds"), strategy=cfg.strategy,
                        orders=orders, notes=notes)
            prev = self._get("plan")
            if prev and prev.get("plan_id") == plan.plan_id and prev.get("executed_at"):
                plan.executed_at = prev["executed_at"]  # same plan already placed: keep it marked
            self._put("plan", plan.to_dict())
            return plan

    def _size_entries(self, b: Broker, cfg: LiveConfig, entries, held, exits, close, data,
                      notes: list[str]) -> list[PlannedOrder]:
        margins = self._call(b.margins) or {}
        cash = float((margins.get("available") or {}).get("live_balance") or margins.get("net") or 0)
        invested = sum(p["qty"] * float(close.get(s, p["entry_price"])) for s, p in held.items() if s not in exits)
        room = max(0.0, min(cfg.capital_limit - invested, cash))
        strat = strategies.create(cfg.strategy)
        slots = strat.max_positions - (len(held) - len(exits))
        ticks = self._call(b.tick_sizes, [e.symbol for e in entries[: cfg.max_new_per_day * 3]])
        sector_value: dict[str, float] = {}
        for s, p in held.items():
            sector_value[data.sector_of(s)] = sector_value.get(data.sector_of(s), 0) + p["qty"] * float(close.get(s, 0))
        scale = 1.0
        if cfg.safety_switch:
            scale = float(market_scale(data.benchmark).iloc[-1])
            if scale < 0.999:
                notes.append(f"Safety switch: the market is jumpy, so new buys are {scale:.0%} of normal size.")
        out: list[PlannedOrder] = []
        for e in entries:
            if len(out) >= min(cfg.max_new_per_day, slots):
                notes.append(f"{e.symbol} also qualified, but today's limit of new buys is reached.")
                continue
            px = float(close[e.symbol])
            limit = round_tick(px * (1 + cfg.entry_buffer), ticks.get(e.symbol, 0.05), up=True)
            if e.stop is not None:
                if limit <= e.stop:
                    continue
                qty = math.floor(cfg.capital_limit * cfg.risk_per_trade * scale / (limit - e.stop))
            else:
                qty = math.floor(cfg.capital_limit * float(e.weight) * scale / limit)
            qty = min(qty, math.floor(cfg.capital_limit * cfg.max_position_weight / limit))
            sec = data.sector_of(e.symbol)
            qty = min(qty, math.floor(max(0.0, cfg.capital_limit * cfg.max_sector_weight - sector_value.get(sec, 0)) / limit))
            while qty > 0 and qty * limit + self.costs.buy_cost(qty * limit) > room:
                qty -= 1
            if qty < 1:
                if limit > cfg.capital_limit * cfg.max_position_weight:
                    notes.append(f"{e.symbol} qualified, but one share (Rs {limit:,.0f}) costs more than "
                                 f"{cfg.max_position_weight:.0%} of your limit. Skipped.")
                elif room < limit:
                    notes.append(f"{e.symbol} qualified, but there isn't enough money left within your limit "
                                 "or in your Zerodha account.")
                else:
                    notes.append(f"{e.symbol} qualified, but it would put too much in one sector. Skipped.")
                continue
            value = qty * limit
            room -= value + self.costs.buy_cost(value)
            sector_value[sec] = sector_value.get(sec, 0) + value
            out.append(PlannedOrder(e.symbol, "BUY", qty, "LIMIT", limit, e.reason, stop=e.stop, target_r=e.target_r,
                                    target_fraction=e.target_fraction, close=px, value=round(value, 2)))
        return out

    # =========================================================== execute
    def in_amo_window(self) -> bool:
        t = self.clock().astimezone(IST)
        return t.weekday() >= 5 or t.time() >= AMO_OPEN or t.time() <= AMO_CLOSE

    def execute(self, plan_id: str) -> dict:
        """Send the reviewed plan to Zerodha. Safe to press twice: placed orders are never repeated."""
        with self._lock:
            cfg = self.config()
            raw = self._get("plan")
            if not raw or raw.get("plan_id") != plan_id:
                raise LiveError("This plan is out of date. Refresh the page to see the latest plan.")
            plan = Plan.from_dict(raw)
            if not plan.orders:
                raise LiveError("There is nothing to place.")
            if any(o.side == "BUY" for o in plan.orders) and not cfg.enabled:
                raise LiveError("Real trading is off. Turn it on first.")
            if not self.in_amo_window():
                raise LiveError("Orders can be placed between 4 pm and 8:55 am (they execute at the next open).")
            if self.market.data().dates[-1].strftime("%Y-%m-%d") != plan.data_date:
                raise LiveError("New prices arrived since this plan was made. Refresh to see the new plan.")
            b = self.broker()
            already = {(o["side"], o["symbol"]) for o in self.orders() if o["plan_id"] == plan_id}
            placed, errors = [], []
            # Sells first (they free up money), then buys.
            for o in sorted(plan.orders, key=lambda x: x.side != "SELL"):
                if (o.side, o.symbol) in already:
                    continue
                try:
                    if o.side == "SELL":
                        pos = next((p for p in self.positions() if p["symbol"] == o.symbol), None)
                        if pos and pos.get("gtt_id"):
                            try:
                                self._call(b.delete_gtt, str(pos["gtt_id"]))
                            except BrokerError:
                                pass  # already triggered or gone
                            pos["gtt_id"], pos["gtt_qty"] = None, None
                            self._save_position(pos)
                        oid = self._call(b.place_order, variety="amo", exchange="NSE", tradingsymbol=o.symbol,
                                         transaction_type="SELL", quantity=o.qty, product="CNC", order_type="MARKET",
                                         market_protection=-1, validity="DAY", tag=TAG)
                    else:
                        oid = self._call(b.place_order, variety="amo", exchange="NSE", tradingsymbol=o.symbol,
                                         transaction_type="BUY", quantity=o.qty, product="CNC", order_type="LIMIT",
                                         price=o.price, validity="DAY", tag=TAG)
                except BrokerError as exc:
                    errors.append(f"{o.side.title()} {o.symbol}: {exc}")
                    self.event(f"Order to {o.side.lower()} {o.qty} {o.symbol} failed: {exc}", "error")
                    break  # stop on the first problem; nothing half-understood gets sent
                self._record_order(oid, plan_id, o)
                placed.append(o.key())
                verb = "buy" if o.side == "BUY" else "sell"
                price = f"up to Rs {o.price:,.2f}" if o.price else "at market"
                self.event(f"Placed order to {verb} {o.qty} {o.symbol} {price} at the next open ({o.reason}).")
            if placed and not errors:
                raw["executed_at"] = self.clock().isoformat(timespec="seconds")
                self._put("plan", raw)
                self._put("forced_exits", [s for s in self._get("forced_exits", [])
                                           if f"SELL:{s}" not in placed])
            return {"placed": placed, "errors": errors}

    # ======================================================== emergency
    def stop(self) -> dict:
        """Turn real trading off and cancel Tradewise's orders that haven't executed yet.
        Safety exits (GTTs) stay in place to protect shares you already own."""
        with self._lock:
            cfg = self.config()
            cfg.enabled = False
            self._put("config", cfg.model_dump())
            cancelled, failed = [], []
            try:
                b = self.broker()
            except LiveError:
                b = None
            for o in self.orders(only_open=True):
                if b is None:
                    failed.append(o["symbol"])
                    continue
                try:
                    self._call(b.cancel_order, o["order_id"], "amo")
                    cancelled.append(o["symbol"])
                    self._update_order(o["order_id"], status="CANCELLED", settled=1)
                except (BrokerError, LiveError):
                    failed.append(o["symbol"])
            msg = "Real trading turned OFF."
            if cancelled:
                msg += f" Cancelled pending orders: {', '.join(cancelled)}."
            if failed:
                msg += f" Could not cancel (log in to Zerodha and cancel in Kite): {', '.join(failed)}."
            self.event(msg, "warn")
            return {"cancelled": cancelled, "failed": failed}

    # ============================================================ status
    def state(self) -> dict:
        cfg = self.config()
        plan = self._get("plan")
        return {
            "configured": bool(self.api_key and self.api_secret),
            "config": cfg.model_dump(),
            "confirm_phrase": CONFIRM_PHRASE,
            "session": self.session_info(),
            "positions": self.positions(),
            "open_orders": self.orders(only_open=True),
            "plan": plan,
            "last_sync": self._get("last_sync"),
            "events": self.events(30),
            "in_order_window": self.in_amo_window(),
        }

    def after_price_update(self) -> None:
        """Called after the daily price update: if you're logged in, prepare tonight's plan automatically."""
        if not self._session():
            return
        try:
            self.plan()
        except (LiveError, BrokerError) as exc:
            self.event(f"Could not prepare tonight's plan automatically: {exc}", "warn")
