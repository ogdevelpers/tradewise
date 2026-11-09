"""Automatic updates and background running, with a fake clock and a fake price server."""

import os
import socket
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from tradewise.auto import Updater
from tradewise.config import Settings
from tradewise.data.market import DataError
from tradewise.data.providers import SyntheticProvider
from tradewise.market_time import IST, last_complete_session, next_close_check
from tradewise.paper import PaperAccounts
from tradewise.services import MarketService


def ist(*a):
    return datetime(*a, tzinfo=IST)


@pytest.mark.parametrize("now,expected", [
    (ist(2026, 10, 12, 14, 19), "2026-10-09"),  # Monday before close -> Friday
    (ist(2026, 10, 12, 16, 30), "2026-10-12"),  # Monday after 4 pm -> Monday
    (ist(2026, 10, 10, 11, 0), "2026-10-09"),   # Saturday -> Friday
    (ist(2026, 10, 11, 20, 0), "2026-10-09"),   # Sunday -> Friday
    (ist(2026, 10, 13, 15, 59), "2026-10-12"),  # just before the cut-off
])
def test_last_complete_session(now, expected):
    assert last_complete_session(now) == pd.Timestamp(expected)


def test_next_close_check_and_utc_input():
    assert next_close_check(ist(2026, 10, 9, 17, 0)) == ist(2026, 10, 12, 16, 0)  # Fri evening -> Mon
    assert next_close_check(ist(2026, 10, 12, 9, 0)) == ist(2026, 10, 12, 16, 0)
    utc = datetime(2026, 10, 12, 11, 0, tzinfo=IST.utc)  # 16:30 IST
    assert last_complete_session(utc) == pd.Timestamp("2026-10-12")


class FakeYahoo:
    """Serves synthetic prices, but only up to ``available`` (what the 'server' has published)."""

    name = "yahoo"

    def __init__(self, available: str, fail: bool = False):
        self.available = pd.Timestamp(available)
        self.fail = fail
        self.calls = 0
        self._src = SyntheticProvider(seed=21, start="2018-01-01", end="2026-12-31")

    def fetch(self, symbol, start, end):
        self.calls += 1
        if self.fail:
            raise DataError("no internet")
        return self._src.fetch(symbol, start, min(pd.Timestamp(end), self.available))


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def small_universe(tmp_path):
    f = tmp_path / "u.csv"
    f.write_text("Symbol,Industry\nAAA,IT\nBBB,Banks\nCCC,Energy\n")
    return f


def make(tmp_path, universe, provider, clock):
    s = Settings(data_dir=tmp_path / "d", universe_file=universe, auto_update=False)
    market = MarketService(s)
    paper = PaperAccounts(s.paper_db)
    return Updater(market, paper, universe_file=universe, provider_factory=lambda: provider, clock=clock), market, paper


def test_first_run_offline_uses_demo_then_switches_to_real(tmp_path, small_universe):
    clock = Clock(ist(2026, 10, 14, 17, 0))
    yahoo = FakeYahoo("2026-10-14", fail=True)
    up, market, paper = make(tmp_path, small_universe, yahoo, clock)

    assert up.due() == "first-run"
    st = up.tick()
    assert st["source"] == "synthetic" and "DEMO" in st["message"] and market.meta()["is_demo"]
    paper.create("demo-acct", market.data(), "trend_pullback")

    assert up.due() is None                      # don't hammer the server
    clock.t += timedelta(minutes=61)
    assert up.due() == "try-real"
    yahoo.fail = False
    st = up.tick()
    assert st["source"] == "yahoo" and st["data_last_date"] == "2026-10-14"
    assert "Up to date" in st["message"]
    assert "removed" in st["notice"] and paper.list() == []   # demo-based practice accounts cleared
    assert not market.meta()["is_demo"]


def test_daily_cycle_retries_until_prices_are_published(tmp_path, small_universe):
    clock = Clock(ist(2026, 10, 14, 17, 0))
    yahoo = FakeYahoo("2026-10-14")
    up, market, paper = make(tmp_path, small_universe, yahoo, clock)
    up.tick()
    paper.create("p", market.data(), "trend_pullback")
    assert up.due() is None

    clock.t = ist(2026, 10, 15, 12, 0)           # next day, market still open
    assert up.due() is None                       # never fetch a half-finished day
    clock.t = ist(2026, 10, 15, 16, 5)            # after the close, but not published yet
    assert up.due() == "new-close"
    st = up.tick()
    assert st["data_last_date"] == "2026-10-14" and "aren't out yet" in st["message"]
    clock.t += timedelta(minutes=10)
    assert up.due() is None                       # waits 30 minutes between tries
    clock.t += timedelta(minutes=25)
    yahoo.available = pd.Timestamp("2026-10-15")  # published now
    st = up.tick()
    assert st["data_last_date"] == "2026-10-15" and "Up to date" in st["message"]
    assert paper.get("p")["last_date"] == "2026-10-15"   # practice account moved forward by itself


def test_holiday_stops_retrying(tmp_path, small_universe):
    clock = Clock(ist(2026, 10, 14, 17, 0))
    up, *_ = make(tmp_path, small_universe, FakeYahoo("2026-10-14"), clock)
    up.tick()
    clock.t = ist(2026, 10, 15, 16, 30)          # pretend the 15th is a holiday: nothing is ever published
    tries = 0
    while up.due() is not None and tries < 20:
        up.tick()
        tries += 1
        clock.t += timedelta(minutes=31)
    assert tries == Updater.MAX_TRIES
    assert "holiday" in up.status()["message"]


def test_catches_up_after_computer_was_off(tmp_path, small_universe):
    clock = Clock(ist(2026, 10, 7, 17, 0))
    yahoo = FakeYahoo("2026-10-07")
    up, market, paper = make(tmp_path, small_universe, yahoo, clock)
    up.tick()
    paper.create("p", market.data(), "trend_pullback")
    # A week later the app starts again (fresh updater, like after a reboot).
    clock.t = ist(2026, 10, 14, 9, 0)
    yahoo.available = pd.Timestamp("2026-10-14")
    up2 = Updater(market, paper, universe_file=small_universe, provider_factory=lambda: yahoo, clock=clock)
    assert up2.due() == "new-close"
    up2.tick()
    assert market.store.last_date("NIFTY50") == pd.Timestamp("2026-10-13")   # not the 14th: still trading
    assert paper.get("p")["last_date"] == "2026-10-13"


def test_offline_with_real_data_keeps_old_prices(tmp_path, small_universe):
    clock = Clock(ist(2026, 10, 14, 17, 0))
    yahoo = FakeYahoo("2026-10-14")
    up, market, _ = make(tmp_path, small_universe, yahoo, clock)
    up.tick()
    yahoo.fail = True
    clock.t = ist(2026, 10, 15, 17, 0)
    st = up.tick()
    assert st["source"] == "yahoo" and st["data_last_date"] == "2026-10-14" and "Couldn't reach" in st["message"]


def test_ingest_never_stores_todays_unfinished_bar(tmp_path, small_universe):
    from tradewise.data.ingest import ingest
    from tradewise.data.store import PriceStore
    from tradewise.data.universe import load_universe

    store = PriceStore(tmp_path / "p.sqlite")
    far = FakeYahoo("2026-12-31")
    ingest(store, far, load_universe(small_universe), "NIFTY50", start="2026-06-01")
    assert store.last_date("NIFTY50") <= last_complete_session()


def test_demo_lock_and_status(tmp_path, small_universe):
    clock = Clock(ist(2026, 10, 14, 17, 0))
    yahoo = FakeYahoo("2026-10-14", fail=True)
    up, market, _ = make(tmp_path, small_universe, yahoo, clock)
    up.tick()
    market.store.set_meta("demo_locked", "1")
    clock.t += timedelta(hours=5)
    assert up.due() is None
    up.tick()
    assert "as you chose" in up.status()["message"]
    calls = yahoo.calls
    assert up.update(manual=True)["source"] == "synthetic" and yahoo.calls == calls


# ------------------------------------------------------------- launcher
def test_install_and_uninstall_linux(tmp_path, monkeypatch):
    from tradewise import launcher

    if os.name == "nt" or sys.platform == "darwin":
        pytest.skip("Linux layout")
    s = Settings(data_dir=tmp_path / "d", api_port=8123)
    home, desk = tmp_path / "home", tmp_path / "Desktop"
    res = launcher.install(s, home=home, desktop=desk, load_now=False)
    auto = home / ".config" / "autostart" / "tradewise.desktop"
    icon = desk / "tradewise.desktop"
    assert set(res.created) == {auto, icon}
    assert "-m tradewise.cli serve" in auto.read_text() and "TRADEWISE_API_PORT=8123" in auto.read_text()
    assert "-m tradewise.cli start" in icon.read_text() and os.access(icon, os.X_OK)
    assert set(launcher.uninstall(s, home=home, desktop=desk)) == {auto, icon}
    assert not auto.exists()


def test_windows_launcher_quoting():
    from tradewise.launcher import _vbs

    v = _vbs([r"C:\Users\Asha Rao\Tradewise\.venv\Scripts\pythonw.exe", "-m", "tradewise.cli", "serve"])
    assert v.endswith(", 0, False\r\n")  # 0 = hidden window
    assert '""C:\\Users\\Asha Rao\\Tradewise\\.venv\\Scripts\\pythonw.exe"" -m tradewise.cli serve' in v


def _free_port():
    with socket.socket() as so:
        so.bind(("127.0.0.1", 0))
        return so.getsockname()[1]


def test_background_server_starts_once_and_stops(tmp_path, monkeypatch):
    from tradewise import launcher

    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).resolve().parents[1] / "src"))
    monkeypatch.setenv("TRADEWISE_AUTO_UPDATE", "false")
    s = Settings(data_dir=tmp_path / "bg", api_port=_free_port())
    assert launcher.ping(s) == ""
    try:
        assert launcher.ensure_running(s, wait_seconds=40)
        pid = s.pid_file.read_text()
        assert launcher.ensure_running(s)            # second call: reuses the running app
        assert s.pid_file.read_text() == pid
        assert launcher.ping(s) == "tradewise"
    finally:
        launcher.stop_server(s)
    import time
    for _ in range(40):
        if launcher.ping(s) == "":
            break
        time.sleep(0.25)
    assert launcher.ping(s) == ""
