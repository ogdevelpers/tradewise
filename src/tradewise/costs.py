"""Indian equity delivery (CNC) transaction costs.

Defaults match Zerodha's published equity-delivery charges (checked Nov 2026):
STT 0.1% on buy and sell, NSE transaction charge 0.00307%, SEBI fee Rs 10/crore,
stamp duty 0.015% on buys, GST 18% on (brokerage + exchange + SEBI), DP charge
Rs 15.34 per scrip on the sell day, zero brokerage. Rates change: verify with
your broker and override any field.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict


@dataclass(frozen=True)
class CostModel:
    stt_rate: float = 0.001
    exchange_rate: float = 0.0000307
    sebi_rate: float = 10 / 1e7
    stamp_rate_buy: float = 0.00015
    gst_rate: float = 0.18
    brokerage_rate: float = 0.0
    brokerage_cap: float = 0.0  # per order; 0 = no cap
    dp_charge_per_sell: float = 15.34

    def __post_init__(self) -> None:
        for k, v in asdict(self).items():
            if v < 0:
                raise ValueError(f"{k} cannot be negative")

    def brokerage(self, value: float) -> float:
        b = value * self.brokerage_rate
        return min(b, self.brokerage_cap) if self.brokerage_cap > 0 else b

    def buy_cost(self, value: float) -> float:
        if value <= 0:
            return 0.0
        brk = self.brokerage(value)
        exch = value * self.exchange_rate
        sebi = value * self.sebi_rate
        return value * self.stt_rate + exch + sebi + value * self.stamp_rate_buy + brk + self.gst_rate * (brk + exch + sebi)

    def sell_cost(self, value: float) -> float:
        if value <= 0:
            return 0.0
        brk = self.brokerage(value)
        exch = value * self.exchange_rate
        sebi = value * self.sebi_rate
        return value * self.stt_rate + exch + sebi + brk + self.gst_rate * (brk + exch + sebi) + self.dp_charge_per_sell

    def round_trip(self, buy_value: float, sell_value: float) -> float:
        return self.buy_cost(buy_value) + self.sell_cost(sell_value)


ZERO_COSTS = CostModel(stt_rate=0, exchange_rate=0, sebi_rate=0, stamp_rate_buy=0, gst_rate=0, dp_charge_per_sell=0)
