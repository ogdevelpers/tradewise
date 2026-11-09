"""Real-money trading, end to end against a simulated Zerodha."""

import http.server
import json
import threading
import urllib.parse
from datetime import datetime, timedelta

import numpy as np
import pytest

from fake_kite import FakeKite
from tradewise import strategies
from tradewise.broker.kite import BrokerError, KiteClient, checksum
from tradewise.config import Settings
from tradewise.data.ingest import ingest
from tradewise.data.providers import SyntheticProvider
from tradewise.data.store import PriceStore
from tradewise.data.universe import Instrument
from tradewise.live import CONFIRM_PHRASE, LiveError, LiveTrader
from tradewise.live.trader import round_tick, session_expiry
from tradewise.market_time import IST
from tradewise.services import MarketService
from tradewise.strategies.base import Decision, EntrySignal, ExitSignal, Features, Strategy, StrategyParams


# ----------------------------------------------------------- a steerable strategy
class LiveScriptParams(StrategyParams):
    pass


class LiveScript(Strategy):
    """Buys/sells whatever the test asks for, with a 5% stop and a 2R profit step."""

    name = "live_script"
    label = "Live script"
    description = "test"
    Params = LiveScriptParams
    want_buy: list = []
    want_sell: list = []

    @property
    def max_positions(self):
        return 5

    def prepare(self, data):
        return Features.from_frames(data, {"close": data.close}, series={})

    def on_close(self, ctx):
        d = Decision()
        for s in self.want_sell:
            if s in ctx.positions:
                d.exits.append(ExitSignal(s, "Closed below 50 DMA"))
        if ctx.can_enter:
            for s in self.want_buy:
                if s not in ctx.positions and s not in ctx.pending:
                    c = ctx.features.value("close", ctx.i, s)
                    d.entries.append(EntrySignal(s, 1.0, "Pullback to 20 EMA in uptrend", stop=c * 0.95, target_r=2.0))
        return d


@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.setitem(strategies._REGISTRY, "live_script", LiveScript)
    LiveScript.want_buy, LiveScript.want_sell = [], []
    s = Settings(data_dir=tmp_path, auto_update=False)
    store = PriceStore(s.prices_target)
    prov = SyntheticProvider(seed=4, start="2020-01-01", end="2021-06-30")
    inst = [Instrument("AAA", "A", "IT"), Instrument("BBB", "B", "Banks"), Instrument("CCC", "C", "IT")]
    ingest(store, prov, inst, "NIFTY50", start="2020-01-01", end="2021-06-30")
    market = MarketService(s)
    kite = FakeKite(cash=500_000)
    clock = {"t": datetime(2021, 6, 30, 18, 0, tzinfo=IST)}  # Wednesday evening, after the close
    trader = LiveTrader(s.prices_target, market, "key", "secret", broker_factory=lambda tok: kite,
                        clock=lambda: clock["t"])
    close = market.data().close.iloc[-1]
    for sym in inst:
        kite.prices[sym.symbol] = float(close[sym.symbol])
    return trader, kite, clock, market


def _login_and_enable(trader, limit=200_000):
    trader.complete_login("tok")
    trader.set_config({"enabled": True, "strategy": "live_script", "capital_limit": limit}, confirm=CONFIRM_PHRASE)


def test_round_tick_and_session_expiry():
    assert round_tick(101.03, 0.05, up=True) == 101.05 and round_tick(101.03, 0.05, up=False) == 101.0
    assert round_tick(250.0, 0.05, up=True) == 250.0 and round_tick(2501.37, 0.1, up=False) == 2501.3
    assert session_expiry(datetime(2026, 11, 9, 18, 0, tzinfo=IST)) == datetime(2026, 11, 10, 6, 0, tzinfo=IST)
    assert session_expiry(datetime(2026, 11, 10, 2, 0, tzinfo=IST)) == datetime(2026, 11, 10, 6, 0, tzinfo=IST)


def test_turning_on_needs_confirmation_and_limits(rig):
    trader, *_ = rig
    with pytest.raises(LiveError, match="type"):
        trader.set_config({"enabled": True, "capital_limit": 100000})
    with pytest.raises(LiveError, match="money limit"):
        trader.set_config({"enabled": True, "capital_limit": 500}, confirm=CONFIRM_PHRASE)
    cfg = trader.set_config({"enabled": True, "capital_limit": 100000, "strategy": "live_script"},
                            confirm=CONFIRM_PHRASE.lower())
    assert cfg.enabled
    no_keys = LiveTrader(trader.db.path, trader.market, None, None)
    with pytest.raises(LiveError, match="API key"):
        no_keys.set_config({"enabled": False, "capital_limit": 0})
        no_keys.set_config({"enabled": True, "capital_limit": 100000}, confirm=CONFIRM_PHRASE)


def test_credentials_from_the_page(rig):
    trader, *_ = rig
    t2 = LiveTrader(trader.db.path.parent / "other.sqlite", trader.market, None, None)
    assert t2.api_key is None and not t2.state()["configured"]
    with pytest.raises(LiveError):
        t2.set_credentials("bad key!", "x")
    t2.set_credentials("abcdef123456", "secret987654")
    assert t2.api_key == "abcdef123456" and t2.state()["configured"]
    assert t2.login_url().endswith("api_key=abcdef123456")
    with pytest.raises(LiveError, match="on the server"):
        trader.set_credentials("abcdef123456", "secret987654")  # env-provided keys win


def test_needs_login_and_session_expires(rig):
    trader, kite, clock, _ = rig
    with pytest.raises(LiveError, match="Log in"):
        trader.plan()
    trader.complete_login("tok")
    assert trader.session_info()["user_name"] == "Test User"
    clock["t"] = datetime(2021, 7, 1, 6, 1, tzinfo=IST)
    assert trader.session_info() is None
    with pytest.raises(LiveError, match="Log in"):
        trader.broker()


def test_full_cycle_buy_protect_profit_step_and_stop(rig):
    trader, kite, clock, market = rig
    _login_and_enable(trader)
    LiveScript.want_buy = ["AAA"]
    plan = trader.plan()
    (buy,) = plan.orders
    close = kite.prices["AAA"]
    assert buy.side == "BUY" and buy.order_type == "LIMIT"
    assert buy.price == round_tick(close * 1.005, 0.05, up=True)
    stop = close * 0.95
    expected_qty = min(int(200_000 * 0.01 / (buy.price - stop)), int(200_000 * 0.2 / buy.price))
    assert buy.qty == expected_qty and buy.value <= 200_000 * 0.2

    res = trader.execute(plan.plan_id)
    assert res == {"placed": ["BUY:AAA"], "errors": []}
    assert trader.execute(plan.plan_id)["placed"] == []  # pressing twice never duplicates
    assert len([c for c in kite.calls if c[0] == "place_order"]) == 1

    # Thursday: the buy fills at the open; evening sync records it and sets the GTT.
    kite.open_market({"AAA": close * 1.002, "BBB": 1, "CCC": 1})
    clock["t"] = datetime(2021, 7, 1, 18, 0, tzinfo=IST)
    trader.complete_login("tok2")
    trader.sync()
    (pos,) = trader.positions()
    assert pos["qty"] == buy.qty and abs(pos["entry_price"] - close * 1.002) < 1e-6
    g = kite.gtt[pos["gtt_id"]]
    assert g["type"] == "two-leg" and g["orders"][0]["quantity"] == buy.qty
    assert g["orders"][1]["quantity"] == buy.qty // 2
    assert g["triggers"][0] == round_tick(stop, 0.05, up=False)

    # Friday: the profit step fires.
    kite.prices["AAA"] = pos["target"] * 1.01
    kite.intraday("AAA", low=pos["entry_price"], high=pos["target"] * 1.01)
    trader.sync()
    (pos2,) = trader.positions()
    assert pos2["partial_done"] == 1 and pos2["qty"] == buy.qty - buy.qty // 2
    assert pos2["stop"] == pos["entry_price"]                       # moved to break-even
    g2 = kite.gtt[pos2["gtt_id"]]
    assert g2["type"] == "single" and g2["orders"][0]["quantity"] == pos2["qty"]

    # Monday: the safety exit fires for the rest.
    kite.prices["AAA"] = pos["entry_price"] * 0.97
    kite.intraday("AAA", low=pos["entry_price"] * 0.97, high=pos["entry_price"])
    trader.sync()
    assert trader.positions() == []
    assert any("no longer in your account" in e["message"] for e in trader.events())


def test_strategy_exit_cancels_gtt_and_sells_at_market(rig):
    trader, kite, clock, market = rig
    _login_and_enable(trader)
    LiveScript.want_buy = ["AAA"]
    trader.execute(trader.plan().plan_id)
    kite.open_market({"AAA": kite.prices["AAA"], "BBB": 1, "CCC": 1})
    trader.sync()
    (pos,) = trader.positions()
    LiveScript.want_buy, LiveScript.want_sell = [], ["AAA"]
    plan = trader.plan()
    (sell,) = plan.orders
    assert sell.side == "SELL" and sell.order_type == "MARKET" and sell.qty == pos["qty"]
    trader.execute(plan.plan_id)
    assert kite.gtt[pos["gtt_id"]]["status"] == "deleted"
    placed = [c[1] for c in kite.calls if c[0] == "place_order"][-1]
    assert placed["transaction_type"] == "SELL" and placed["market_protection"] == -1
    kite.open_market({"AAA": kite.prices["AAA"] * 0.99, "BBB": 1, "CCC": 1})
    trader.sync()
    assert trader.positions() == []


def test_limits_cash_daily_cap_and_off_switch(rig):
    trader, kite, *_ = rig
    trader.complete_login("tok")
    LiveScript.want_buy = ["AAA", "BBB", "CCC"]
    trader.set_config({"strategy": "live_script"})
    assert trader.plan().orders == []  # trading off: no buys
    trader.set_config({"enabled": True, "capital_limit": 150_000, "max_new_per_day": 2}, confirm=CONFIRM_PHRASE)
    plan = trader.plan()
    assert len(plan.orders) == 2  # daily cap
    assert any("limit of new buys" in n for n in plan.notes)
    assert sum(o.value for o in plan.orders) <= 150_000
    kite.cash = 5_000
    small = trader.plan()
    assert sum(o.value for o in small.orders) <= 5_000  # never more than the cash in the account
    assert small.notes  # it says why the other buys were skipped


def test_emergency_stop_cancels_pending_orders(rig):
    trader, kite, *_ = rig
    _login_and_enable(trader)
    LiveScript.want_buy = ["AAA"]
    trader.execute(trader.plan().plan_id)
    res = trader.stop()
    assert res["cancelled"] == ["AAA"] and not trader.config().enabled
    assert all(o["status"] == "CANCELLED" for o in kite.orders.values())


def test_refuses_outside_order_window_and_on_stale_data(rig):
    trader, kite, clock, _ = rig
    _login_and_enable(trader)
    LiveScript.want_buy = ["AAA"]
    plan = trader.plan()
    clock["t"] = datetime(2021, 6, 30, 11, 0, tzinfo=IST)  # market hours (session still valid)
    trader._put("session", {**trader._get("session"), "expires_at": "2099-01-01T00:00:00+05:30"})
    with pytest.raises(LiveError, match="4 pm"):
        trader.execute(plan.plan_id)
    clock["t"] = datetime(2021, 7, 2, 18, 0, tzinfo=IST)  # two days later, no new prices loaded
    with pytest.raises(LiveError, match="only up to"):
        trader.plan()


def test_broker_errors_stop_the_run_and_expired_token_logs_out(rig):
    trader, kite, *_ = rig
    _login_and_enable(trader)
    LiveScript.want_buy = ["AAA", "BBB"]
    plan = trader.plan()
    kite.fail_next = BrokerError("Insufficient funds", "MarginException", 400)
    res = trader.execute(plan.plan_id)
    assert res["placed"] == [] and "Insufficient funds" in res["errors"][0]
    res = trader.execute(plan.plan_id)  # retry places what's left, once
    assert len(res["placed"]) == 2
    kite.token_valid = False
    with pytest.raises(LiveError, match="expired"):
        trader.sync()
    assert trader.session_info() is None


def test_price_below_stop_without_protection_is_sold_next_open(rig):
    trader, kite, clock, _ = rig
    _login_and_enable(trader)
    LiveScript.want_buy = ["AAA"]
    trader.execute(trader.plan().plan_id)
    kite.open_market({"AAA": kite.prices["AAA"], "BBB": 1, "CCC": 1})
    kite.prices["AAA"] *= 0.90  # crashed through the stop before protection was set
    LiveScript.want_buy = []
    plan = trader.plan()
    assert [(o.side, o.symbol, o.reason) for o in plan.orders] == [("SELL", "AAA", "Below the safety exit")]


# ------------------------------------------------------------- API wiring
def test_api_real_trading_flow(rig, tmp_path):
    from test_serverless import call
    from tradewise.api import create_app

    trader, kite, *_ = rig
    app = create_app(Settings(data_dir=trader.db.path.parent, auto_update=False), live_trader=trader)
    st = json.loads(call(app, "GET", "/api/live")["body"])
    assert st["available"] and st["session"] is None and not st["config"]["enabled"]
    r = call(app, "GET", "/api/broker/callback?status=success&request_token=abc")
    assert r["status"] == 303 and dict(r["headers"])[b"location"] == b"/#/live"
    assert trader.session_info()["user_name"] == "Test User"
    r = call(app, "POST", "/api/live/config", json.dumps({"enabled": True, "capital_limit": 100000}).encode(),
             {"content-type": "application/json"})
    assert r["status"] == 409 and CONFIRM_PHRASE in json.loads(r["body"])["error"]
    r = call(app, "POST", "/api/live/execute", json.dumps({"plan_id": "nope"}).encode(),
             {"content-type": "application/json"})
    assert r["status"] == 409

    serverless = create_app(Settings(data_dir=tmp_path / "s", serverless=True, auto_update=False,
                                     database_url=str(tmp_path / "s.sqlite")))
    assert json.loads(call(serverless, "GET", "/api/live")["body"])["available"] is False
    assert call(serverless, "POST", "/api/live/plan")["status"] == 409


# -------------------------------------------------- the real HTTP client
class _KiteHandler(http.server.BaseHTTPRequestHandler):
    seen: list = []

    def log_message(self, *a):
        pass

    def _reply(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.seen.append(("GET", self.path, dict(self.headers), b""))
        if self.path.startswith("/portfolio/holdings"):
            if "expired" in self.headers.get("Authorization", ""):
                return self._reply(403, {"status": "error", "message": "Token expired", "error_type": "TokenException"})
            return self._reply(200, {"status": "success", "data": [{"tradingsymbol": "INFY", "quantity": 3}]})
        if self.path.startswith("/quote/ltp"):
            return self._reply(200, {"status": "success", "data": {"NSE:INFY": {"last_price": 1500.5}}})
        if self.path.startswith("/instruments/NSE"):
            body = ("instrument_token,exchange_token,tradingsymbol,name,last_price,expiry,strike,tick_size,lot_size,"
                    "instrument_type,segment,exchange\n1,1,INFY,INFOSYS,0,,0,0.1,1,EQ,NSE,NSE\n").encode()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body)
            return
        self._reply(404, {"status": "error", "message": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        self.seen.append(("POST", self.path, dict(self.headers), body))
        if self.path == "/session/token":
            return self._reply(200, {"status": "success", "data": {"access_token": "AT", "user_id": "AB1"}})
        if self.path == "/orders/amo":
            return self._reply(200, {"status": "success", "data": {"order_id": "250101000001"}})
        if self.path == "/gtt/triggers":
            return self._reply(200, {"status": "success", "data": {"trigger_id": 123}})
        self._reply(400, {"status": "error", "message": "bad", "error_type": "InputException"})


@pytest.fixture
def kite_server():
    _KiteHandler.seen = []
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _KiteHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def test_kite_client_requests(kite_server):
    k = KiteClient("apikey", base=kite_server)
    assert k.login_url() == "https://kite.zerodha.com/connect/login?v=3&api_key=apikey"
    assert k.create_session("rt", "sec")["access_token"] == "AT" and k.access_token == "AT"
    _, path, headers, body = _KiteHandler.seen[-1]
    form = urllib.parse.parse_qs(body.decode())
    assert form["checksum"] == [checksum("apikey", "rt", "sec")] and headers["X-Kite-Version"] == "3"

    assert k.holdings()[0]["tradingsymbol"] == "INFY"
    assert _KiteHandler.seen[-1][2]["Authorization"] == "token apikey:AT"
    assert k.ltp(["INFY"]) == {"INFY": 1500.5}
    assert "i=NSE%3AINFY" in _KiteHandler.seen[-1][1]
    assert k.tick_sizes(["INFY", "OTHER"]) == {"INFY": 0.1, "OTHER": 0.05}

    oid = k.place_order(variety="amo", exchange="NSE", tradingsymbol="INFY", transaction_type="SELL", quantity=3,
                        product="CNC", order_type="MARKET", market_protection=-1, validity="DAY", tag="tradewise")
    assert oid == "250101000001"
    form = urllib.parse.parse_qs(_KiteHandler.seen[-1][3].decode())
    assert form["market_protection"] == ["-1"] and form["order_type"] == ["MARKET"] and "variety" not in form

    gid = k.place_gtt("INFY", "two-leg", [1400.0, 1700.0], 1500.5,
                      [{"transaction_type": "SELL", "quantity": 3, "price": 1372.0},
                       {"transaction_type": "SELL", "quantity": 1, "price": 1700.0}])
    assert gid == "123"
    form = urllib.parse.parse_qs(_KiteHandler.seen[-1][3].decode())
    cond, legs = json.loads(form["condition"][0]), json.loads(form["orders"][0])
    assert cond == {"exchange": "NSE", "tradingsymbol": "INFY", "trigger_values": [1400.0, 1700.0], "last_price": 1500.5}
    assert legs[0]["product"] == "CNC" and legs[0]["order_type"] == "LIMIT" and legs[1]["quantity"] == 1

    expired = KiteClient("apikey", "expired", base=kite_server)
    with pytest.raises(BrokerError) as ei:
        expired.holdings()
    assert ei.value.session_expired
    with pytest.raises(BrokerError, match="reach Zerodha"):
        KiteClient("k", base="http://127.0.0.1:9").holdings()
    assert np.isfinite(1.0)
