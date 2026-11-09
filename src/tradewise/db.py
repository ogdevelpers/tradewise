"""One small database layer for both places Tradewise runs.

* On your computer: a SQLite file (nothing to install).
* On Vercel: Postgres (for example Neon), because serverless functions have no
  lasting disk. Selected by passing a ``postgresql://`` URL.

All SQL in the app is written once with ``?`` placeholders and portable
``INSERT ... ON CONFLICT`` upserts; this layer adapts it per database.
"""

from __future__ import annotations

import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

PG_PREFIXES = ("postgres://", "postgresql://")


def is_postgres(target: str | Path) -> bool:
    return isinstance(target, str) and target.startswith(PG_PREFIXES)


class Conn:
    def __init__(self, raw, dialect: str):
        self.raw = raw
        self.dialect = dialect

    def _q(self, sql: str) -> str:
        return sql.replace("?", "%s") if self.dialect == "postgres" else sql

    def execute(self, sql: str, params: Sequence[Any] = ()):
        cur = self.raw.cursor()
        cur.execute(self._q(sql), tuple(params))
        return cur

    def executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> None:
        rows = [tuple(r) for r in rows]
        if rows:
            self.raw.cursor().executemany(self._q(sql), rows)

    def script(self, sql: str) -> None:
        for stmt in (s.strip() for s in sql.split(";")):
            if stmt:
                self.execute(stmt)


class Database:
    def __init__(self, target: str | Path):
        if is_postgres(target):
            self.dialect = "postgres"
            self.url = str(target)
            self.path = None
        else:
            self.dialect = "sqlite"
            self.url = None
            self.path = Path(target)
            self.path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def real(self) -> str:
        """Column type for prices: Postgres REAL is only 4 bytes, so use double precision."""
        return "DOUBLE PRECISION" if self.dialect == "postgres" else "REAL"

    @contextmanager
    def connect(self) -> Iterator[Conn]:
        if self.dialect == "postgres":
            try:
                import psycopg
            except ImportError as exc:  # pragma: no cover - depends on environment
                raise RuntimeError("Postgres needs the psycopg package: pip install 'psycopg[binary]'") from exc
            # prepare_threshold=None: required behind connection poolers such as Neon's pgbouncer.
            raw = psycopg.connect(self.url, connect_timeout=15, prepare_threshold=None)
        else:
            raw = sqlite3.connect(self.path, timeout=30)
            raw.execute("PRAGMA journal_mode=WAL")
            raw.execute("PRAGMA synchronous=NORMAL")
        try:
            yield Conn(raw, self.dialect)
            raw.commit()
        except Exception:
            raw.rollback()
            raise
        finally:
            raw.close()

    def ensure_schema(self, ddl: str) -> None:
        """Create tables if missing. Two serverless instances starting at the same moment can race on
        Postgres' CREATE TABLE IF NOT EXISTS, so retry once."""
        for attempt in range(3):
            try:
                with self.connect() as c:
                    c.script(ddl)
                return
            except Exception:
                if self.dialect != "postgres" or attempt == 2:
                    raise
                time.sleep(0.5 * (attempt + 1))

    # ------------------------------------------------------------ locking
    def try_lock(self, name: str, ttl_seconds: int) -> str | None:
        """Cross-process lock stored in the ``meta`` table (works across serverless instances).
        Returns a token to pass to :meth:`unlock`, or None if someone else holds a fresh lock."""
        now = datetime.now(timezone.utc)
        token = f"{now.isoformat()}|{uuid.uuid4().hex}"
        cutoff = (now - timedelta(seconds=ttl_seconds)).isoformat()
        with self.connect() as c:
            cur = c.execute(
                "INSERT INTO meta(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value "
                "WHERE meta.value < ?", (f"lock:{name}", token, cutoff))
            return token if cur.rowcount == 1 else None

    def unlock(self, name: str, token: str) -> None:
        with self.connect() as c:
            c.execute("DELETE FROM meta WHERE key=? AND value=?", (f"lock:{name}", token))
