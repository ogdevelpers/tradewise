"""Minimal Zerodha Kite Connect v3 client (standard library only).

Only what Tradewise needs: login, account reads, delivery (CNC) orders placed as
after-market orders (AMO), and GTT triggers that hold the safety exit and the
profit step at Zerodha's end, so they work even when Tradewise is offline.

Docs: https://kite.trade/docs/connect/v3/
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import io
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Protocol

log = logging.getLogger(__name__)

API = "https://api.kite.trade"
LOGIN = "https://kite.zerodha.com/connect/login"


class BrokerError(RuntimeError):
    def __init__(self, message: str, error_type: str = "GeneralException", status: int | None = None):
        super().__init__(message)
        self.error_type = error_type
        self.status = status

    @property
    def session_expired(self) -> bool:
        return self.error_type == "TokenException" or self.status == 403


class Broker(Protocol):
    """What the live trader needs. ``KiteClient`` and the test fake both implement it."""

    def profile(self) -> dict: ...
    def holdings(self) -> list[dict]: ...
    def positions(self) -> dict: ...
    def order_history(self, order_id: str) -> list[dict]: ...
    def margins(self) -> dict: ...
    def ltp(self, symbols: list[str]) -> dict[str, float]: ...
    def tick_sizes(self, symbols: list[str]) -> dict[str, float]: ...
    def place_order(self, **params) -> str: ...
    def cancel_order(self, order_id: str, variety: str = "amo") -> None: ...
    def place_gtt(self, symbol: str, trigger_type: str, trigger_values: list[float], last_price: float,
                  orders: list[dict]) -> str: ...
    def gtts(self) -> list[dict]: ...
    def delete_gtt(self, trigger_id: str) -> None: ...


def checksum(api_key: str, request_token: str, api_secret: str) -> str:
    return hashlib.sha256((api_key + request_token + api_secret).encode()).hexdigest()


class KiteClient:
    def __init__(self, api_key: str, access_token: str | None = None, base: str = API, timeout: float = 15):
        if not api_key:
            raise BrokerError("Zerodha API key is not set (KITE_API_KEY)", "InputException")
        self.api_key = api_key
        self.access_token = access_token
        self.base = base.rstrip("/")
        self.timeout = timeout
        self._ticks: dict[str, float] | None = None

    # ------------------------------------------------------------ plumbing
    def login_url(self) -> str:
        return f"{LOGIN}?{urllib.parse.urlencode({'v': 3, 'api_key': self.api_key})}"

    def _request(self, method: str, path: str, params: dict | None = None, raw: bool = False) -> Any:
        url = self.base + path
        data = None
        if params and method in ("GET", "DELETE"):
            url += "?" + urllib.parse.urlencode(params, doseq=True)
        elif params:
            data = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None}).encode()
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("X-Kite-Version", "3")
        req.add_header("User-Agent", "tradewise/0.1")
        req.add_header("Accept-Encoding", "gzip")
        if self.access_token:
            req.add_header("Authorization", f"token {self.api_key}:{self.access_token}")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    body = gzip.decompress(body)
        except urllib.error.HTTPError as exc:
            body = exc.read()
            try:
                err = json.loads(body.decode() or "{}")
            except ValueError:
                err = {}
            raise BrokerError(err.get("message") or f"Zerodha error {exc.code}", err.get("error_type", "GeneralException"),
                              exc.code) from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise BrokerError(f"Could not reach Zerodha: {exc}", "NetworkException") from None
        if raw:
            return body.decode("utf-8", "replace")
        try:
            payload = json.loads(body.decode())
        except ValueError:
            raise BrokerError("Zerodha sent an unreadable reply", "DataException") from None
        if payload.get("status") == "error":
            raise BrokerError(payload.get("message", "Zerodha error"), payload.get("error_type", "GeneralException"))
        return payload.get("data")

    # ------------------------------------------------------------ session
    def create_session(self, request_token: str, api_secret: str) -> dict:
        data = self._request("POST", "/session/token", {
            "api_key": self.api_key, "request_token": request_token,
            "checksum": checksum(self.api_key, request_token, api_secret)})
        self.access_token = data["access_token"]
        return data

    # -------------------------------------------------------------- reads
    def profile(self) -> dict:
        return self._request("GET", "/user/profile")

    def holdings(self) -> list[dict]:
        return self._request("GET", "/portfolio/holdings") or []

    def positions(self) -> dict:
        return self._request("GET", "/portfolio/positions") or {"net": [], "day": []}

    def order_history(self, order_id: str) -> list[dict]:
        return self._request("GET", f"/orders/{urllib.parse.quote(order_id)}") or []

    def margins(self) -> dict:
        return self._request("GET", "/user/margins/equity") or {}

    def ltp(self, symbols: list[str]) -> dict[str, float]:
        if not symbols:
            return {}
        data = self._request("GET", "/quote/ltp", {"i": [f"NSE:{s}" for s in symbols]}) or {}
        return {k.split(":", 1)[1]: float(v["last_price"]) for k, v in data.items()}

    def tick_sizes(self, symbols: list[str]) -> dict[str, float]:
        if self._ticks is None:
            text = self._request("GET", "/instruments/NSE", raw=True)
            self._ticks = {}
            for row in csv.DictReader(io.StringIO(text)):
                if row.get("segment") == "NSE" and row.get("instrument_type") == "EQ":
                    try:
                        self._ticks[row["tradingsymbol"]] = float(row["tick_size"])
                    except (KeyError, ValueError):
                        pass
        return {s: self._ticks.get(s, 0.05) for s in symbols}

    # ------------------------------------------------------------- orders
    def place_order(self, **params) -> str:
        variety = params.pop("variety", "amo")
        data = self._request("POST", f"/orders/{variety}", params)
        return str(data["order_id"])

    def cancel_order(self, order_id: str, variety: str = "amo") -> None:
        self._request("DELETE", f"/orders/{variety}/{urllib.parse.quote(order_id)}")

    def place_gtt(self, symbol: str, trigger_type: str, trigger_values: list[float], last_price: float,
                  orders: list[dict]) -> str:
        condition = {"exchange": "NSE", "tradingsymbol": symbol, "trigger_values": trigger_values,
                     "last_price": last_price}
        legs = [{"exchange": "NSE", "tradingsymbol": symbol, "product": "CNC", "order_type": "LIMIT", **o}
                for o in orders]
        data = self._request("POST", "/gtt/triggers", {
            "type": trigger_type, "condition": json.dumps(condition), "orders": json.dumps(legs)})
        return str(data["trigger_id"])

    def gtts(self) -> list[dict]:
        return self._request("GET", "/gtt/triggers") or []

    def delete_gtt(self, trigger_id: str) -> None:
        self._request("DELETE", f"/gtt/triggers/{urllib.parse.quote(str(trigger_id))}")
