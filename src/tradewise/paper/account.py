from __future__ import annotations

import json
import logging
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .. import strategies
from ..backtest.engine import BacktestConfig, SimState, Simulator
from ..backtest.metrics import summarize
from ..data.market import MarketData
from ..db import Database

log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    name TEXT PRIMARY KEY,
    strategy TEXT NOT NULL,
    params TEXT NOT NULL,
    config TEXT NOT NULL,
    state TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""
_NAME = re.compile(r"^[A-Za-z0-9_-]{1,40}$")


class PaperAccounts:
    def __init__(self, target: str | Path):
        self.db = Database(target)
        self._lock = threading.Lock()  # one update at a time per process
        self.db.ensure_schema(_SCHEMA)

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    def create(self, name: str, data: MarketData, strategy: str, params: Mapping[str, Any] | None = None,
               config: Mapping[str, Any] | None = None) -> dict:
        if not _NAME.match(name):
            raise ValueError("account name: 1-40 letters, digits, - or _")
        strat = strategies.create(strategy, params)  # validates params
        cfg = BacktestConfig(**{**(config or {}), "start": None, "end": None})
        state = SimState(cash=cfg.initial_capital)
        # Start on the latest close: today's signals are queued for the next open.
        state.next_index = len(data.dates) - 1
        sim = Simulator(data, strat, cfg, state=state)
        sim.run()
        with self._lock, self.db.connect() as c:
            cur = c.execute(
                "INSERT INTO accounts(name,strategy,params,config,state,created_at,updated_at) VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT(name) DO NOTHING",
                (name, strategy, json.dumps(strat.params.model_dump()), cfg.model_dump_json(),
                 json.dumps(sim.state.to_dict()), self._now(), self._now()),
            )
            if cur.rowcount != 1:
                raise ValueError(f"paper account {name!r} already exists")
        return self.get(name)

    def _row(self, name: str) -> tuple:
        with self.db.connect() as con:
            row = con.execute("SELECT name,strategy,params,config,state,created_at,updated_at FROM accounts WHERE name=?",
                              (name,)).fetchone()
        if row is None:
            raise KeyError(f"no paper account named {name!r}")
        return row

    def list(self) -> list[dict]:
        with self.db.connect() as con:
            names = [r[0] for r in con.execute("SELECT name FROM accounts ORDER BY created_at")]
        return [self.get(n) for n in names]

    def get(self, name: str) -> dict:
        n, strategy, params, config, state, created, updated = self._row(name)
        st = SimState.from_dict(json.loads(state))
        return {
            "name": n, "strategy": strategy, "params": json.loads(params), "config": json.loads(config),
            "created_at": created, "updated_at": updated, "last_date": st.last_decision_date,
            "summary": summarize(st),
            "positions": [p.__dict__ for p in st.positions.values()],
            "pending": [o.__dict__ for o in st.pending],
            "recent_fills": [f.__dict__ for f in st.fills[-50:]],
            "equity": [[d, e] for d, e, _ in st.equity[-750:]],
        }

    def update(self, name: str, data: MarketData) -> dict:
        """Process every new day of data since the last update."""
        with self._lock:
            _, strategy, params, config, state_json, _, _ = self._row(name)
            strat = strategies.create(strategy, json.loads(params))
            cfg = BacktestConfig(**json.loads(config))
            st = SimState.from_dict(json.loads(state_json))
            if st.last_decision_date:
                st.next_index = int(data.dates.searchsorted(st.last_decision_date, side="right"))
            sim = Simulator(data, strat, cfg, state=st)
            before = st.next_index
            sim.run(until=len(data.dates) - 1)
            processed = sim.state.next_index - before
            with self.db.connect() as con:
                con.execute("UPDATE accounts SET state=?, updated_at=? WHERE name=?",
                            (json.dumps(sim.state.to_dict()), self._now(), name))
        log.info("paper %s: processed %d new day(s)", name, processed)
        out = self.get(name)
        out["days_processed"] = processed
        return out

    def delete(self, name: str) -> None:
        with self._lock, self.db.connect() as con:
            cur = con.execute("DELETE FROM accounts WHERE name=?", (name,))
            if cur.rowcount == 0:
                raise KeyError(f"no paper account named {name!r}")
