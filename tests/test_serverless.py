"""The hosted (Vercel) setup: settings, login, scheduled updates, shared state, locks and Postgres."""

import asyncio
import json
import os
import shutil
from datetime import datetime

import pandas as pd
import pytest

from tradewise.auto import Updater
from tradewise.config import Settings
from tradewise.data.ingest import ingest
from tradewise.data.providers import SyntheticProvider
from tradewise.data.store import PriceStore
from tradewise.data.universe import load_universe
from tradewise.db import Database
from tradewise.market_time import IST
from tradewise.paper import PaperAccounts
from tradewise.services import MarketService

PG_URL = os.environ.get("TRADEWISE_TEST_PG_URL")


def call(app, method, path, body=b"", headers=None):
    """Tiny ASGI client with headers and raw bodies (for forms and cookies)."""
    async def go():
        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method,
                 "path": path.split("?")[0], "raw_path": path.encode(), "query_string": path.partition("?")[2].encode(),
                 "root_path": "", "scheme": "https", "client": ("1.2.3.4", 1), "server": ("x", 443),
                 "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]}
        sent, out = False, {"status": 0, "headers": [], "body": b""}

        async def receive():
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            await asyncio.Event().wait()

        async def send(msg):
            if msg["type"] == "http.response.start":
                out["status"], out["headers"] = msg["status"], msg.get("headers", [])
            elif msg["type"] == "http.response.body":
                out["body"] += msg.get("body", b"")
        await app(scope, receive, send)
        return out
    return asyncio.new_event_loop().run_until_complete(go())


@pytest.fixture(scope="module")
def demo_db(tmp_path_factory):
    root = tmp_path_factory.mktemp("srv")
    store = PriceStore(root / "prices.sqlite")
    prov = SyntheticProvider(seed=5, start="2016-01-01", end="2021-06-30")
    ingest(store, prov, load_universe()[:10], "NIFTY50", start="2016-01-01", end="2021-06-30")
    return root / "prices.sqlite"


def test_settings_from_vercel_environment():
    s = Settings.from_env({"VERCEL": "1", "DATABASE_URL": "postgresql://u:p@h/db", "APP_PASSWORD": "pw",
                           "CRON_SECRET": "cs"})
    assert s.serverless and not s.auto_update and s.database_url.startswith("postgresql://")
    assert s.app_password == "pw" and s.cron_secret == "cs"
    assert s.prices_target == s.paper_target == "postgresql://u:p@h/db"
    assert Settings.from_env({"POSTGRES_URL": "postgres://x"}).database_url == "postgres://x"
    local = Settings.from_env({})
    assert not local.serverless and local.prices_target == local.prices_db


def _app(tmp_path, demo_db, **kw):
    from tradewise.api import create_app

    shutil.copy(demo_db, tmp_path / "prices.sqlite")
    # database_url may also be a SQLite path; on Vercel it is the Neon Postgres URL.
    s = Settings(data_dir=tmp_path, auto_update=False, serverless=True,
                 database_url=str(tmp_path / "prices.sqlite"), **kw)
    return create_app(s), s


def test_vercel_without_database_shows_setup_page(tmp_path):
    from tradewise.api import create_app

    app = create_app(Settings(data_dir=tmp_path, serverless=True, auto_update=False))
    r = call(app, "GET", "/")
    assert r["status"] == 503 and b"Storage" in r["body"]
    assert call(app, "GET", "/api/status")["status"] == 503


def test_password_gate(tmp_path, demo_db):
    app, _ = _app(tmp_path, demo_db, app_password="s3cret pass")
    assert call(app, "GET", "/api/health")["status"] == 200
    assert call(app, "GET", "/api/status")["status"] == 401
    r = call(app, "GET", "/")
    assert r["status"] == 303 and dict(r["headers"])[b"location"] == b"/login"
    assert b"password" in call(app, "GET", "/login")["body"]
    bad = call(app, "POST", "/login", b"password=nope", {"content-type": "application/x-www-form-urlencoded"})
    assert bad["status"] == 401 and b"Wrong password" in bad["body"]
    ok = call(app, "POST", "/login", b"password=s3cret+pass", {"content-type": "application/x-www-form-urlencoded"})
    assert ok["status"] == 303
    cookie = [v.decode() for k, v in ok["headers"] if k == b"set-cookie"][0]
    assert "HttpOnly" in cookie and "Secure" in cookie and "SameSite=lax" in cookie
    value = cookie.split(";")[0]
    r = call(app, "GET", "/api/status", headers={"cookie": value})
    assert r["status"] == 200 and json.loads(r["body"])["login"] is True
    tampered = value[:-1] + ("0" if value[-1] != "0" else "1")
    assert call(app, "GET", "/api/status", headers={"cookie": tampered})["status"] == 401


def test_cookie_expiry():
    from tradewise.api.auth import make_cookie, valid_cookie

    secret = b"k" * 32
    c = make_cookie(secret, now=1000)
    assert valid_cookie(secret, c, now=1001)
    assert not valid_cookie(secret, c, now=1000 + 31 * 24 * 3600)
    assert not valid_cookie(secret, "garbage") and not valid_cookie(b"other" * 7, c, now=1001)


def test_cron_endpoint_requires_secret(tmp_path, demo_db):
    app, _ = _app(tmp_path, demo_db)
    assert call(app, "GET", "/api/cron/evening-1")["status"] == 403  # not configured
    app, _ = _app(tmp_path, demo_db, cron_secret="abc", app_password="pw")
    assert call(app, "GET", "/api/cron/evening-1", headers={"authorization": "Bearer nope"})["status"] == 401
    r = call(app, "GET", "/api/cron/evening-1", headers={"authorization": "Bearer abc"})
    assert r["status"] == 200 and "status" in json.loads(r["body"])  # cron works without the login cookie


def test_jobs_finish_inside_the_request_when_serverless(tmp_path, demo_db):
    app, _ = _app(tmp_path, demo_db)
    body = json.dumps({"strategies": [{"name": "momentum_rotation"}], "config": {"start": "2018-01-01"}}).encode()
    r = call(app, "POST", "/api/backtests", body, {"content-type": "application/json"})
    job = json.loads(r["body"])
    assert r["status"] == 200 and job["status"] == "done" and job["result"]["results"]


def test_updater_state_is_shared_between_instances(tmp_path):
    s = Settings(data_dir=tmp_path, auto_update=False, universe_file=None)
    market, paper = MarketService(s), PaperAccounts(s.paper_target)

    class Fail:
        name = "yahoo"

        def fetch(self, *a):
            from tradewise.data.market import DataError
            raise DataError("blocked")

    clock = lambda: datetime(2026, 10, 14, 17, 30, tzinfo=IST)  # noqa: E731
    a = Updater(market, paper, provider_factory=Fail, clock=clock, serverless=True)
    a.tick()  # first run fails -> demo prices
    b = Updater(MarketService(s), paper, provider_factory=Fail, clock=clock, serverless=True)  # another instance
    st = b.status()
    assert st["source"] == "synthetic" and "scheduled update" in st["message"] and st["last_error"]
    assert b.due() is None  # it knows an attempt just happened
    assert not st["busy"]


def test_stale_busy_flag_expires(tmp_path):
    s = Settings(data_dir=tmp_path, auto_update=False)
    market = MarketService(s)
    t = {"now": datetime(2026, 10, 14, 17, 0, tzinfo=IST)}
    up = Updater(market, PaperAccounts(s.paper_target), clock=lambda: t["now"], serverless=True)
    up.state.update(busy=True, busy_since=t["now"].isoformat())
    up._save()
    assert up.status()["busy"]
    t["now"] = t["now"].replace(minute=20)  # killed by a timeout 20 minutes ago
    assert not up.status()["busy"]


def test_batched_provider_path(tmp_path):
    class Batched:
        name = "yahoo"

        def __init__(self):
            self.calls = 0
            self.src = SyntheticProvider(seed=3, start="2020-01-01", end="2020-12-31")

        def fetch_many(self, symbols, start, end):
            self.calls += 1
            return {s: self.src.fetch(s, start, end) for s in symbols if s != "MISSING"}

    store = PriceStore(tmp_path / "p.sqlite")
    prov = Batched()
    from tradewise.data.universe import Instrument
    inst = [Instrument("AAA", "A", "IT"), Instrument("MISSING", "M", "IT")]
    rep = ingest(store, prov, inst, "NIFTY50", start="2020-01-01", end="2020-06-30")
    assert prov.calls == 1 and rep.updated["AAA"] > 100 and rep.failed == {"MISSING": "no data returned"}
    rep2 = ingest(store, prov, inst, "NIFTY50", start="2020-01-01", end="2020-12-31")
    assert prov.calls == 2 and 0 < rep2.updated["AAA"] < 140  # only the new months (+1 week overlap)


# --------------------------------------------------------- both databases
TARGETS = ["sqlite"] + (["postgres"] if PG_URL else [])


@pytest.fixture(params=TARGETS)
def target(request, tmp_path):
    if request.param == "sqlite":
        return tmp_path / "db.sqlite"
    import psycopg

    with psycopg.connect(PG_URL, autocommit=True) as c:
        c.execute("DROP TABLE IF EXISTS prices, instruments, meta, accounts")
    return PG_URL


def test_lock(target):
    PriceStore(target)  # creates the meta table
    db = Database(target)
    t1 = db.try_lock("update", ttl_seconds=600)
    assert t1 and db.try_lock("update", ttl_seconds=600) is None
    db.unlock("update", t1)
    t2 = db.try_lock("update", ttl_seconds=600)
    assert t2
    assert db.try_lock("update", ttl_seconds=0)  # a stale lock (crashed holder) can be taken over


def test_store_and_practice_roundtrip(target):
    store = PriceStore(target)
    prov = SyntheticProvider(seed=8, start="2017-01-01", end="2020-12-31")
    rep = ingest(store, prov, load_universe()[:6], "NIFTY50", start="2017-01-01", end="2020-12-31")
    assert rep.ok
    expect = prov.fetch("RELIANCE", "2017-01-01", "2020-12-31")
    got = store.load_bars("RELIANCE")
    assert len(got) == len(expect)
    pd.testing.assert_series_equal(got["close"], expect["close"], check_names=False, check_freq=False)  # full precision
    md = store.load_market("NIFTY50")
    assert len(md.symbols) == 6 and store.last_dates()["NIFTY50"] == md.dates[-1]
    assert store.get_meta_many(["provider", "nope"]) == {"provider": "synthetic", "nope": None}

    paper = PaperAccounts(target)
    paper.create("p1", md.truncate("2020-06-30"), "trend_pullback")
    with pytest.raises(ValueError):
        paper.create("p1", md, "trend_pullback")
    assert paper.update("p1", md)["days_processed"] > 100
    assert [a["name"] for a in paper.list()] == ["p1"]
    paper.delete("p1")
    assert paper.list() == []
