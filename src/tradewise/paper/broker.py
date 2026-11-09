"""Interface a live broker adapter (Zerodha Kite, Upstox, Angel One, Fyers) must implement.

Live order placement is deliberately not built yet: it should only be added
after months of paper results match the backtest. Under SEBI's retail algo
framework (fully in force from 1 April 2026) automated orders must go through
the broker's compliant API with a registered strategy ID and a whitelisted
static IP, so adapters must pass those through.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol


@dataclass(frozen=True)
class OrderRequest:
    symbol: str
    side: Literal["BUY", "SELL"]
    qty: int
    order_type: Literal["MARKET", "LIMIT", "SL", "SL-M"] = "MARKET"
    price: float | None = None
    trigger_price: float | None = None
    product: Literal["CNC"] = "CNC"  # delivery only
    strategy_id: str | None = None  # exchange-assigned algo ID

    def __post_init__(self) -> None:
        if self.qty <= 0:
            raise ValueError("qty must be positive")
        if self.order_type in ("LIMIT", "SL") and self.price is None:
            raise ValueError(f"{self.order_type} orders need a price")
        if self.order_type in ("SL", "SL-M") and self.trigger_price is None:
            raise ValueError("stop orders need a trigger price")


class Broker(Protocol):
    def place_order(self, order: OrderRequest) -> str: ...
    def cancel_order(self, order_id: str) -> None: ...
    def positions(self) -> dict[str, int]: ...
    def cash(self) -> float: ...
