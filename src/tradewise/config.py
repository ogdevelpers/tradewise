"""Application settings.

Every value can be overridden with an environment variable prefixed with
``TRADEWISE_`` (for example ``TRADEWISE_DATA_DIR=/srv/tradewise``).
"""

from __future__ import annotations

import logging
import os
import sys
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel, Field, field_validator

_PREFIX = "TRADEWISE_"


def default_data_dir() -> Path:
    """A fixed per-user folder, so the app finds its data however it was started."""
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        return base / "Tradewise"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Tradewise"
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "tradewise"


class Settings(BaseModel):
    data_dir: Path = Field(default_factory=default_data_dir, description="Where the databases and log live")
    universe_file: Path | None = Field(default=None, description="CSV with a Symbol column (NSE index list format)")
    benchmark_symbol: str = "NIFTY50"
    api_host: str = "127.0.0.1"
    api_port: int = 8000
    auto_update: bool = Field(default=True, description="Fetch new prices automatically while running")
    database_url: str | None = Field(default=None, description="postgresql://... (used on Vercel); default SQLite")
    serverless: bool = Field(default=False, description="Running as serverless functions (Vercel)")
    app_password: str | None = Field(default=None, description="Require this password to open the app")
    session_secret: str | None = Field(default=None, description="Signs login cookies; derived from the password if unset")
    cron_secret: str | None = Field(default=None, description="Vercel sends it to the scheduled update endpoint")
    kite_api_key: str | None = Field(default=None, description="Zerodha Kite Connect API key (real trading)")
    kite_api_secret: str | None = Field(default=None, description="Zerodha Kite Connect API secret")
    max_backtest_workers: int = Field(default=2, ge=1, le=32)
    log_level: str = "INFO"

    @field_validator("log_level")
    @classmethod
    def _valid_level(cls, v: str) -> str:
        v = v.upper()
        if v not in {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}:
            raise ValueError(f"unknown log level {v!r}")
        return v

    @property
    def prices_target(self) -> str | Path:
        return self.database_url or self.prices_db

    @property
    def paper_target(self) -> str | Path:
        return self.database_url or self.paper_db

    @property
    def prices_db(self) -> Path:
        return self.data_dir / "prices.sqlite"

    @property
    def log_file(self) -> Path:
        return self.data_dir / "tradewise.log"

    @property
    def pid_file(self) -> Path:
        return self.data_dir / "server.pid"

    @property
    def url(self) -> str:
        return f"http://{self.api_host}:{self.api_port}/"

    @property
    def paper_db(self) -> Path:
        return self.data_dir / "paper.sqlite"

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> "Settings":
        env = os.environ if environ is None else environ
        values: dict[str, str] = {}
        for name in cls.model_fields:
            key = _PREFIX + name.upper()
            if key in env and env[key] != "":
                values[name] = env[key]
        # Standard names set by Vercel and its Neon/Postgres integration.
        if "database_url" not in values:
            for key in ("DATABASE_URL", "POSTGRES_URL"):
                if env.get(key):
                    values["database_url"] = env[key]
                    break
        if env.get("VERCEL") == "1":
            values.setdefault("serverless", "true")
            values["auto_update"] = "false"  # no background threads on serverless; Vercel Cron triggers updates
            values.setdefault("data_dir", "/tmp/tradewise")
        for name, key in (("app_password", "APP_PASSWORD"), ("cron_secret", "CRON_SECRET"),
                          ("kite_api_key", "KITE_API_KEY"), ("kite_api_secret", "KITE_API_SECRET")):
            if name not in values and env.get(key):
                values[name] = env[key]
        return cls(**values)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings.from_env()


def configure_logging(level: str = "INFO", log_file: Path | None = None) -> None:
    from logging.handlers import RotatingFileHandler

    fmt = "%(asctime)s %(levelname)s %(name)s: %(message)s"
    logging.basicConfig(level=level, format=fmt)
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        root = logging.getLogger()
        if not any(isinstance(h, RotatingFileHandler) for h in root.handlers):
            h = RotatingFileHandler(log_file, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
            h.setFormatter(logging.Formatter(fmt))
            root.addHandler(h)
