"""Paper trading, HTTP API and CLI, end to end on synthetic data."""

import asyncio
import json
import time

import pytest

from tradewise.cli import main
from tradewise.config import Settings, get_settings
from tradewise.data.ingest import ingest
from tradewise.data.providers import SyntheticProvider
from tradewise.data.store import PriceStore
from tradewise.data.universe import load_universe
from tradewise.paper import PaperAccounts


class ASGIClient:
    """Minimal in-process HTTP client (avoids an httpx dependency)."""

    def __init__(self, app):
        self.app = app
        self.loop = asyncio.new_event_loop()
        self.loop.run_until_complete(self._lifespan("startup"))

    async def _lifespan(self, kind):
        msgs = [{"type": f"lifespan.{kind}"}]

        async def receive():
            if msgs:
                return msgs.pop(0)
            await asyncio.Event().wait()  # stay "running" until the loop is closed

        async def send(_):
            pass

        if kind == "startup":
            self._task = self.loop.create_task(self.app({"type": "lifespan", "asgi": {"version": "3.0"}}, receive, send))
            await asyncio.sleep(0)

    def close(self):
        self._task.cancel()
        self.loop.run_until_complete(asyncio.gather(self._task, return_exceptions=True))
        self.loop.close()

    def request(self, method, path, body=None):
        return self.loop.run_until_complete(self._request(method, path, body))

    async def _request(self, method, path, body):
        raw = json.dumps(body).encode() if body is not None else b""
        path, _, query = path.partition("?")
        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method, "path": path,
                 "raw_path": path.encode(), "query_string": query.encode(), "root_path": "", "scheme": "http",
                 "headers": [(b"content-type", b"application/json"), (b"host", b"test")],
                 "client": ("127.0.0.1", 1), "server": ("test", 80)}
        sent = False
        out = {"status": None, "body": b""}

        async def receive():
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": raw, "more_body": False}
            await asyncio.sleep(3600)

        async def send(msg):
            if msg["type"] == "http.response.start":
                out["status"] = msg["status"]
            elif msg["type"] == "http.response.body":
                out["body"] += msg.get("body", b"")

        await self.app(scope, receive, send)
        data = None
        if out["body"]:
            try:
                data = json.loads(out["body"])
            except ValueError:
                data = out["body"].decode()
        return out["status"], data


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    root = tmp_path_factory.mktemp("tw")
    s = Settings(data_dir=root, auto_update=False)
    store = PriceStore(s.prices_db)
    prov = SyntheticProvider(seed=5, start="2015-01-01", end="2021-12-31")
    uni = load_universe()[:15]
    ingest(store, prov, uni, s.benchmark_symbol, start="2015-01-01", end="2021-06-30")
    return s, store, prov, uni


def test_paper_account_lifecycle(env, tmp_path):
    s, store, prov, uni = env
    accts = PaperAccounts(tmp_path / "paper.sqlite")
    data = store.load_market(s.benchmark_symbol)
    a = accts.create("p1", data, "trend_pullback", config={"initial_capital": 300_000})
    assert a["last_date"] == data.dates[-1].strftime("%Y-%m-%d") and a["summary"]["final_equity"] == 300_000
    with pytest.raises(ValueError):
        accts.create("p1", data, "trend_pullback")
    with pytest.raises(ValueError):
        accts.create("bad name!", data, "trend_pullback")

    # Same data again: nothing new to process.
    assert accts.update("p1", data)["days_processed"] == 0
    # Two months of new data arrive.
    more = PriceStore(tmp_path / "more.sqlite")
    ingest(more, prov, uni, s.benchmark_symbol, start="2015-01-01", end="2021-08-31")
    new = more.load_market(s.benchmark_symbol)
    u = accts.update("p1", new)
    assert u["days_processed"] == len(new.dates) - len(data.dates)
    assert u["last_date"] == new.dates[-1].strftime("%Y-%m-%d")
    assert [a["name"] for a in accts.list()] == ["p1"]
    accts.delete("p1")
    with pytest.raises(KeyError):
        accts.get("p1")


def test_api_end_to_end(env):
    from tradewise.api import create_app

    s = env[0]
    c = ASGIClient(create_app(s))
    assert c.request("GET", "/api/health") == (200, {"status": "ok", "app": "tradewise"})
    code, meta = c.request("GET", "/api/meta")
    assert code == 200 and meta["has_data"] and meta["is_demo"] and meta["symbols"] == 15

    code, strats = c.request("GET", "/api/strategies")
    assert {x["name"] for x in strats} == {"trend_pullback", "momentum_rotation"}
    code, mk = c.request("GET", "/api/market")
    assert code == 200 and mk["regime"] in ("uptrend", "downtrend")
    for name in ("trend_pullback", "momentum_rotation"):
        code, sig = c.request("GET", f"/api/signals?strategy={name}")
        assert code == 200 and "entries" in sig
    code, st = c.request("GET", "/api/stocks/RELIANCE")
    assert code == 200 and len(st["chart"]) == 260 and len(st["checklist"]) == 7 and st["signal"]["action"] in ("BUY", "WATCH")

    assert c.request("GET", "/api/stocks/NOTREAL")[0] == 404
    assert c.request("GET", "/api/signals?strategy=bogus")[0] == 404
    assert c.request("GET", "/api/nothing")[0] == 404
    code, err = c.request("POST", "/api/backtests", {"strategies": []})
    assert code == 422 and err["details"]
    code, err = c.request("POST", "/api/backtests", {"strategies": [{"name": "trend_pullback", "params": {"atr_stop_mult": -2}}]})
    assert code == 422

    body = {"strategies": [{"name": "momentum_rotation", "weight": 0.7}, {"name": "trend_pullback", "weight": 0.3}],
            "config": {"start": "2016-06-01", "initial_capital": 500000}, "split": "2019-01-01"}
    code, job = c.request("POST", "/api/backtests", body)
    assert code == 202
    for _ in range(200):
        code, j = c.request("GET", f"/api/backtests/{job['id']}")
        if j["status"] in ("done", "failed"):
            break
        time.sleep(0.05)
    assert j["status"] == "done", j.get("error")
    assert [r["key"] for r in j["result"]["results"]] == ["momentum_rotation", "trend_pullback", "combined", "benchmark"]
    assert c.request("GET", "/api/backtests/nope")[0] == 404

    code, acct = c.request("POST", "/api/paper", {"name": "web1", "strategy": "momentum_rotation"})
    assert code == 201 and acct["name"] == "web1"
    assert c.request("POST", "/api/paper", {"name": "web1", "strategy": "momentum_rotation"})[0] == 422
    code, up = c.request("POST", "/api/paper/web1/update")
    assert code == 200 and up["days_processed"] == 0
    assert c.request("DELETE", "/api/paper/web1")[0] == 204
    assert c.request("GET", "/api/paper/web1")[0] == 404

    code, html = c.request("GET", "/")
    assert code == 200 and "Tradewise" in html
    assert c.request("GET", "/static/app.js")[0] == 200
    c.close()


def test_api_without_data(tmp_path):
    from tradewise.api import create_app

    c = ASGIClient(create_app(Settings(data_dir=tmp_path, auto_update=False)))
    assert c.request("GET", "/api/meta") == (200, {"has_data": False, "provider": None, "serverless": False})
    code, err = c.request("GET", "/api/market")
    assert code == 409 and "download" in err["error"]
    c.close()


def test_cli_flow(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TRADEWISE_DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    try:
        assert main(["data", "--provider", "synthetic", "--start", "2015-01-01", "--end", "2020-12-31"]) == 0
        assert main(["backtest", "--start", "2016-06-01", "--split", "2019-01-01", "--weights", "0.3,0.7",
                     "--output", str(tmp_path / "r.json")]) == 0
        out = capsys.readouterr().out
        assert "Trend pullback" in out and "Combined 30 / 70" in out and "Benchmark" in out
        assert json.loads((tmp_path / "r.json").read_text())["results"]
        assert main(["signals", "--strategy", "momentum_rotation"]) == 0
        assert main(["paper", "create", "cli1", "--strategy", "trend_pullback"]) == 0
        assert main(["paper", "update"]) == 0
        assert main(["paper", "list"]) == 0
        assert main(["paper", "create"]) == 2
        assert main(["paper", "create", "cli1"]) == 1  # duplicate
        assert main(["backtest", "--weights", "1"]) == 2
        assert main(["data", "--provider", "csv"]) == 2
    finally:
        get_settings.cache_clear()


def test_settings_from_env():
    s = Settings.from_env({"TRADEWISE_API_PORT": "9001", "TRADEWISE_LOG_LEVEL": "debug", "TRADEWISE_UNIVERSE_FILE": ""})
    assert s.api_port == 9001 and s.log_level == "DEBUG" and s.universe_file is None
    with pytest.raises(ValueError):
        Settings.from_env({"TRADEWISE_LOG_LEVEL": "LOUD"})


def test_switching_source_never_mixes_prices(tmp_path):
    store = PriceStore(tmp_path / "p.sqlite")
    inst = [load_universe()[0]]
    ingest(store, SyntheticProvider(seed=1, start="2020-01-01", end="2020-12-31"), inst, "NIFTY50", start="2020-01-01",
           end="2020-12-31")

    class Real:  # stands in for Yahoo: different prices on a different calendar
        name = "yahoo"

        def fetch(self, symbol, start, end):
            df = SyntheticProvider(seed=99, start="2020-06-01", end="2020-12-31").fetch(symbol, start, end)
            return df.iloc[::2]

    rep = ingest(store, Real(), inst, "NIFTY50", start="2020-01-01", end="2020-12-31")
    assert rep.replaced_source == "synthetic"
    bars = store.load_bars(inst[0].symbol)
    assert bars.index.min() >= __import__("pandas").Timestamp("2020-06-01")  # no demo rows left behind
    assert store.get_meta("provider") == "yahoo"


def test_beginner_endpoints(env, tmp_path):
    from tradewise.api import create_app

    s = Settings(data_dir=tmp_path, auto_update=False)
    # copy the module's demo prices into a fresh data dir so this test owns its paper accounts
    import shutil
    shutil.copy(env[0].prices_db, s.prices_db)
    from tradewise.auto import Updater
    from tradewise.data.market import DataError
    from tradewise.services import MarketService

    def offline():
        raise DataError("no internet")

    market = MarketService(s)
    up = Updater(market, PaperAccounts(s.paper_db), provider_factory=offline)
    c = ASGIClient(create_app(s, updater=up))
    code, sig = c.request("GET", "/api/signals?strategy=momentum_rotation&capital=100000")
    assert code == 200 and sig["capital"] == 100000 and 1 <= len(sig["ranking"]) <= 15
    assert all(r["value"] <= 100000 / 15 + 1e-6 for r in sig["ranking"] if r["qty"] >= 1)
    assert isinstance(sig["market_rising"], bool)
    assert c.request("GET", "/api/signals?capital=abc")[0] == 422
    assert c.request("GET", "/api/signals?capital=-5")[0] == 422

    code, st = c.request("GET", "/api/stocks/RELIANCE")
    assert all({"plain", "why"} <= set(r) for r in st["checklist"])

    c.request("POST", "/api/paper", {"name": "practice-x", "strategy": "trend_pullback"})
    code, job = c.request("POST", "/api/refresh")
    assert code == 202
    for _ in range(200):
        code, j = c.request("GET", f"/api/jobs/{job['id']}")
        if j["status"] in ("done", "failed"):
            break
        time.sleep(0.05)
    assert j["status"] == "done", j.get("error")
    assert j["result"]["source"] == "synthetic" and "demo prices" in j["result"]["message"]
    code, st = c.request("GET", "/api/status")
    assert code == 200 and {"busy", "message", "data_last_date", "expected_date", "next_update"} <= set(st)
    c.close()
