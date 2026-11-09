"""Automatic price updates.

While the app is running, a background thread wakes up every few minutes and
decides whether new prices should exist:

* First run: download real prices (or fall back to demo prices if that fails).
* Real prices: once a weekday's closing prices should be out (after 4 pm IST),
  fetch them. If Yahoo hasn't published them yet, retry every 30 minutes. If
  the computer was off, the next check catches up on every missed day.
* Demo prices (because the internet was down): try real prices again every hour
  and switch over automatically.

After every successful update, all practice accounts are brought up to date.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timedelta
from typing import Callable

import pandas as pd

from .data.ingest import ingest
from .data.market import DataError
from .data.providers import DataProvider, SyntheticProvider, get_provider
from .data.universe import Instrument, load_universe
from .market_time import last_complete_session, next_close_check, now_ist

log = logging.getLogger(__name__)

class Updater:
    RETRY = timedelta(minutes=30)
    MAX_TRIES = 6  # per expected day; after that it's most likely a market holiday
    REAL_RETRY = timedelta(hours=1)

    def __init__(self, market, paper, universe_file=None,
                 provider_factory: Callable[[], DataProvider] | None = None,
                 clock: Callable[[], datetime] = now_ist, interval_seconds: int = 300, serverless: bool = False):
        self.market = market
        self.paper = paper
        self.universe_file = universe_file
        self.provider_factory = provider_factory or (lambda: get_provider("yahoo"))
        self.clock = clock
        self.interval = interval_seconds
        self.serverless = serverless
        self.after_update: list[Callable[[], None]] = []  # e.g. prepare tonight's real-trading plan
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._tries: dict[pd.Timestamp, int] = {}
        self._last_attempt: datetime | None = None
        self.state = {"busy": False, "busy_since": None, "message": "Starting…", "last_check": None,
                      "last_success": None, "last_error": None, "notice": None}

    # ------------------------------------------------------- persistence
    # State lives in the database so every serverless instance (and every restart) sees the same picture.
    STATE_KEY = "updater_state"

    def _load(self) -> None:
        raw = self.store.get_meta(self.STATE_KEY)
        if not raw:
            return
        try:
            d = json.loads(raw)
        except ValueError:
            return
        self.state.update({k: d.get(k, self.state.get(k)) for k in self.state})
        self._tries = {pd.Timestamp(k): int(v) for k, v in d.get("tries", {}).items()}
        la = d.get("last_attempt")
        self._last_attempt = datetime.fromisoformat(la) if la else None

    def _save(self) -> None:
        d = {**self.state, "tries": {k.strftime("%Y-%m-%d"): v for k, v in self._tries.items()
                                     if k >= pd.Timestamp(self.clock().date()) - pd.Timedelta(days=10)},
             "last_attempt": self._last_attempt.isoformat() if self._last_attempt else None}
        self.store.set_meta(self.STATE_KEY, json.dumps(d))

    def _busy_now(self) -> bool:
        since = self.state.get("busy_since")
        if not self.state.get("busy") or not since:
            return False
        try:  # an update killed half-way (timeout, redeploy) must not look busy forever
            return self.clock() - datetime.fromisoformat(since) < timedelta(minutes=10)
        except ValueError:
            return False

    def _retry_words(self) -> str:
        return "at the next scheduled update this evening" if self.serverless else "in 30 minutes"

    def _when(self, nxt: datetime) -> str:
        return f"{nxt:%a %d %b} evening" if self.serverless else f"{nxt:%a %d %b} after 4 pm"

    # ------------------------------------------------------------ thread
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="auto-update", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:  # the loop must survive anything
                log.exception("automatic update failed")
                self.state.update(busy=False, message="Automatic update hit an error; will try again shortly.")
            self._stop.wait(self.interval)

    # ---------------------------------------------------------- decision
    @property
    def store(self):
        return self.market.store

    @property
    def benchmark(self) -> str:
        return self.market.settings.benchmark_symbol

    def due(self, now: datetime | None = None) -> str | None:
        """Why an update should run now, or None."""
        now = now or self.clock()
        self._load()
        source = self.store.get_meta("provider")
        recently = self._last_attempt is not None
        if source is None:
            return "first-run"
        if source == "csv":
            return None
        if source == "synthetic":
            if self.store.get_meta("demo_locked") == "1":
                return None
            if recently and now - self._last_attempt < self.REAL_RETRY:
                return None
            return "try-real"
        expected = last_complete_session(now)
        have = self.store.last_date(self.benchmark)
        if have is not None and have >= expected:
            if self._last_attempt is None:
                return "startup"  # make sure practice accounts have caught up
            return None
        if self._tries.get(expected, 0) >= self.MAX_TRIES:
            return None
        if recently and now - self._last_attempt < self.RETRY:
            return None
        return "new-close"

    def tick(self, now: datetime | None = None) -> dict | None:
        reason = self.due(now)
        if reason is None:
            if not self._busy_now():
                self._set_idle_message(now)
                self._save()
            return None
        log.info("automatic update (%s)", reason)
        return self.update(now=now)

    # ------------------------------------------------------------ update
    def _instruments(self, first: bool) -> list[Instrument]:
        if not first:
            inst = [Instrument(i["symbol"], i["name"] or i["symbol"], i["sector"] or "Unknown")
                    for i in self.store.instruments() if not i["is_benchmark"]]
            if inst:
                return inst
        return load_universe(self.universe_file)

    def update(self, now: datetime | None = None, manual: bool = False) -> dict:
        """Fetch new prices if possible, then update practice accounts. Safe from any thread or instance."""
        if not self._lock.acquire(timeout=600 if manual and not self.serverless else 0):
            return {**self.status(), "message": "An update is already running."}
        token = None
        try:
            token = self.store.db.try_lock("update", ttl_seconds=600)
            if token is None:
                return {**self.status(), "message": "An update is already running."}
            now = now or self.clock()
            self._load()
            self._last_attempt = now
            self.state.update(busy=True, busy_since=now.isoformat(), last_check=now.isoformat(timespec="minutes"),
                              message="Getting the latest prices…")
            self._save()
            source = self.store.get_meta("provider")
            demo_locked = self.store.get_meta("demo_locked") == "1"
            if source == "csv" or (source == "synthetic" and demo_locked):
                self.state["message"] = "Using fixed prices (demo or your own files); nothing to download."
            else:
                self._download(source, now)
            self._update_practice()
            for hook in self.after_update:
                try:
                    hook()
                except Exception:
                    log.exception("after-update step failed")
        finally:
            self.state.update(busy=False, busy_since=None)
            try:
                self._save()
            finally:
                if token:
                    self.store.db.unlock("update", token)
                self._lock.release()
        return self.status()

    def _download(self, source: str | None, now: datetime) -> None:
        expected = last_complete_session(now)
        try:
            provider = self.provider_factory()
            report = ingest(self.store, provider, self._instruments(source is None), self.benchmark,
                            end=str(expected.date()), incremental=source == "yahoo")
            if self.benchmark in report.failed:
                raise DataError(f"Nifty 50 download failed: {report.failed[self.benchmark]}")
        except (DataError, OSError, ValueError) as exc:
            self.state["last_error"] = str(exc)
            log.warning("price download failed: %s", exc)
            if source is None:
                ingest(self.store, SyntheticProvider(), self._instruments(True), self.benchmark, incremental=False)
                self.state["message"] = ("Real prices couldn't be downloaded. Showing DEMO prices for now. "
                                         f"Tradewise tries again {'at the next scheduled update' if self.serverless else 'every hour'} "
                                         "and switches automatically.")
            elif source == "synthetic":
                self.state["message"] = ("Still on demo prices: real prices aren't reachable yet. Next try "
                                         f"{'at the next scheduled update' if self.serverless else 'in an hour'}.")
            else:
                self.state["message"] = f"Couldn't reach the price server. Trying again {self._retry_words()}."
            return

        if report.replaced_source == "synthetic":
            removed = 0
            for a in self.paper.list():
                self.paper.delete(a["name"])
                removed += 1
            self.state["notice"] = ("Real prices are now loaded, replacing the demo prices." +
                                    (f" {removed} practice account(s) started on demo prices were removed; "
                                     "start a new one on the Practice page." if removed else ""))
        self.state.update(last_error=None, last_success=now.isoformat(timespec="minutes"))
        have = self.store.last_date(self.benchmark)
        if have is not None and have < expected:
            self._tries[expected] = self._tries.get(expected, 0) + 1
        self._set_idle_message(now, force=True)
        if report.failed:
            self.state["message"] += f" ({len(report.failed)} share(s) could not be downloaded.)"

    def _update_practice(self) -> None:
        try:
            data = self.market.data()
        except LookupError:
            return
        for a in self.paper.list():
            try:
                self.paper.update(a["name"], data)
            except Exception:
                log.exception("could not update practice account %s", a["name"])

    def _set_idle_message(self, now: datetime | None, force: bool = False) -> None:
        if self.state["busy"] and not force:
            return
        now = now or self.clock()
        source = self.store.get_meta("provider")
        if source == "csv":
            self.state["message"] = "Using prices from your own CSV files."
            return
        if source == "synthetic" and self.store.get_meta("demo_locked") == "1":
            self.state["message"] = "Showing DEMO prices, as you chose. Start Tradewise normally to use real prices."
            return
        if source != "yahoo":
            return
        expected = last_complete_session(now)
        have = self.store.last_date(self.benchmark)
        if have is None:
            return
        nxt = next_close_check(now)
        if have >= expected:
            self.state["message"] = (f"Up to date with the market close on {have:%a %d %b}. "
                                     f"Next automatic update: {self._when(nxt)}.")
        elif self._tries.get(expected, 0) >= self.MAX_TRIES:
            self.state["message"] = (f"No prices were published for {expected:%a %d %b} (probably a market "
                                     f"holiday). Next automatic update: {self._when(nxt)}.")
        else:
            self.state["message"] = (f"Prices are up to {have:%a %d %b}. The {expected:%a %d %b} closing prices "
                                     f"aren't out yet; checking again {self._retry_words()}.")

    # ------------------------------------------------------------ status
    def status(self) -> dict:
        now = self.clock()
        if not self._lock.locked():
            self._load()
        have = self.store.last_date(self.benchmark)
        meta = self.store.get_meta_many(["provider", "version"])
        return {
            **self.state,
            "busy": self._busy_now() if not self._lock.locked() else True,
            "serverless": self.serverless,
            "source": meta["provider"],
            "version": meta["version"],
            "data_last_date": have.strftime("%Y-%m-%d") if have is not None else None,
            "expected_date": last_complete_session(now).strftime("%Y-%m-%d"),
            "next_update": next_close_check(now).isoformat(timespec="minutes"),
            "running": bool(self._thread and self._thread.is_alive()),
        }
