"""Vercel entry point: Vercel serves the ASGI ``app`` defined here."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from tradewise.api import create_app  # noqa: E402
from tradewise.config import Settings, configure_logging  # noqa: E402

settings = Settings.from_env()
configure_logging(settings.log_level)
app = create_app(settings)
