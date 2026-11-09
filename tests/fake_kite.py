"""An in-memory stand-in for Zerodha, good enough to exercise the full live-trading cycle."""

from __future__ import annotations

import itertools

from tradewise.broker.kite import BrokerError


class FakeKite:
    def __init__(self, cash: float = 1_000_000):
        self.cash = cash
        self.holdings_qty: dict[str, int] = {}   # settled + T1
        self.day_qty: dict[str, int] = {}        # today's delivery net (positions)
        self.orders: dict[str, dict] = {}
        self.gtt: dict[str, dict] = {}
        self.prices: dict[str, float] = {}
        self.ticks: dict[str, float] = {}
        self.token_valid = True
        self.calls: list[tuple] = []
        self.fail_next: BrokerError | None = None
        self._ids = itertools.count(1)

    # ---- session
    def login_url(self):
        return "https://kite.example/login"

    def create_session(self, request_token, api_secret):
        if request_token == "bad":
            raise BrokerError("Invalid request token", "TokenException", 403)
        return {"access_token": "acc-" + request_token, "user_id": "AB1234", "user_name": "Test User"}

    def _check(self, name, *a):
        self.calls.append((name, *a))
        if self.fail_next:
            e, self.fail_next = self.fail_next, None
            raise e
        if not self.token_valid:
            raise BrokerError("Incorrect `api_key` or `access_token`.", "TokenException", 403)

    # ---- reads
    def profile(self):
        self._check("profile")
        return {"user_id": "AB1234"}

    def holdings(self):
        self._check("holdings")
        return [{"tradingsymbol": s, "exchange": "NSE", "quantity": q, "t1_quantity": 0}
                for s, q in self.holdings_qty.items() if q]

    def positions(self):
        self._check("positions")
        net = [{"tradingsymbol": s, "product": "CNC", "quantity": q} for s, q in self.day_qty.items() if q]
        return {"net": net, "day": net}

    def order_history(self, order_id):
        self._check("order_history", order_id)
        o = self.orders[order_id]
        return [{"status": o["status"], "filled_quantity": o.get("filled", 0), "average_price": o.get("avg", 0),
                 "status_message": o.get("msg")}]

    def margins(self):
        self._check("margins")
        return {"net": self.cash, "available": {"live_balance": self.cash}}

    def ltp(self, symbols):
        self._check("ltp", tuple(symbols))
        return {s: self.prices[s] for s in symbols if s in self.prices}

    def tick_sizes(self, symbols):
        return {s: self.ticks.get(s, 0.05) for s in symbols}

    # ---- orders
    def place_order(self, **p):
        self._check("place_order", p)
        assert p["variety"] == "amo" and p["product"] == "CNC" and p["exchange"] == "NSE"
        if p["order_type"] == "MARKET":
            assert p.get("market_protection") == -1, "market orders need market protection"
        else:
            assert p.get("price"), "limit orders need a price"
        oid = f"ORD{next(self._ids)}"
        self.orders[oid] = {**p, "status": "AMO REQ RECEIVED"}
        return oid

    def cancel_order(self, order_id, variety="amo"):
        self._check("cancel_order", order_id)
        o = self.orders[order_id]
        if o["status"] != "AMO REQ RECEIVED":
            raise BrokerError("Order cannot be cancelled", "OrderException", 400)
        o["status"] = "CANCELLED"

    def place_gtt(self, symbol, trigger_type, trigger_values, last_price, orders):
        self._check("place_gtt", symbol, trigger_type, tuple(trigger_values))
        if trigger_type == "two-leg":
            assert len(trigger_values) == 2 and trigger_values[0] < last_price < trigger_values[1]
        else:
            assert len(trigger_values) == 1
        held = self.holdings_qty.get(symbol, 0) + self.day_qty.get(symbol, 0)
        assert all(o["quantity"] <= held for o in orders), "GTT for more shares than held"
        gid = str(next(self._ids))
        self.gtt[gid] = {"id": gid, "symbol": symbol, "type": trigger_type, "triggers": list(trigger_values),
                         "orders": orders, "status": "active"}
        return gid

    def gtts(self):
        self._check("gtts")
        return [{"id": g["id"], "status": g["status"]} for g in self.gtt.values()]

    def delete_gtt(self, trigger_id):
        self._check("delete_gtt", trigger_id)
        g = self.gtt.get(str(trigger_id))
        if not g or g["status"] != "active":
            raise BrokerError("GTT not found", "InputException", 400)
        g["status"] = "deleted"

    # ---- market simulation
    def open_market(self, opens: dict[str, float]):
        """Settle yesterday, then execute after-market orders at today's open."""
        for s, q in self.day_qty.items():
            self.holdings_qty[s] = self.holdings_qty.get(s, 0) + q
        self.day_qty = {}
        for o in self.orders.values():
            if o["status"] != "AMO REQ RECEIVED":
                continue
            s, px = o["tradingsymbol"], opens[o["tradingsymbol"]]
            if o["transaction_type"] == "BUY":
                if px > o["price"]:
                    o["status"] = "CANCELLED"  # limit never reached; DAY order expires
                    continue
                self._fill(o, px, +1)
            else:
                self._fill(o, px, -1)

    def _fill(self, o, px, sign):
        s, q = o["tradingsymbol"], o["quantity"]
        o.update(status="COMPLETE", filled=q, avg=px)
        self.day_qty[s] = self.day_qty.get(s, 0) + sign * q
        self.cash -= sign * q * px

    def intraday(self, symbol: str, low: float, high: float):
        """Fire GTTs whose triggers are crossed (the stop is checked first)."""
        for g in self.gtt.values():
            if g["symbol"] != symbol or g["status"] != "active":
                continue
            leg = None
            if low <= g["triggers"][0]:
                leg = g["orders"][0]
            elif g["type"] == "two-leg" and high >= g["triggers"][1]:
                leg = g["orders"][1]
            if leg:
                g["status"] = "triggered"
                o = {"tradingsymbol": symbol, "quantity": leg["quantity"]}
                self._fill(o, leg["price"], -1)
