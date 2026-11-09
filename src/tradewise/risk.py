"""The safety switch: buy smaller when the market is jumpy or the account is in a deep dip.

Two independent brakes, multiplied together, applied only to the size of *new* buys:

* **Market calm.** When the Nifty's recent daily swings (20-day volatility, annualised) are above
  ``target_vol``, sizes shrink in proportion: at twice the normal swing, buys are half size. Big swings
  cluster in crashes, so this trims risk exactly when losses are most likely. Never below ``floor``.
* **Drawdown brake.** While the account is more than ``drawdown_brake`` below its highest value, new buys
  are half size, so a losing streak can't snowball.

Everything uses closing prices already known when the order is planned, so there's no lookahead.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from . import indicators as ind

VOL_WINDOW = 20


def market_scale(benchmark: pd.Series, target_vol: float = 0.16, floor: float = 0.25) -> pd.Series:
    """Per-day size multiplier in [floor, 1] from the benchmark's recent volatility (1 while unknown)."""
    vol = ind.rolling_volatility(benchmark, VOL_WINDOW)
    scale = (target_vol / vol.where(vol > 0)).clip(lower=floor, upper=1.0)
    return scale.fillna(1.0)


def market_vol(benchmark: pd.Series) -> pd.Series:
    return ind.rolling_volatility(benchmark, VOL_WINDOW)


def drawdown_scale(equity: float, peak: float, brake: float) -> float:
    if brake <= 0 or peak <= 0 or not math.isfinite(equity):
        return 1.0
    return 0.5 if equity / peak - 1 <= -brake else 1.0


def describe(scale: float, vol: float | None, target_vol: float) -> dict:
    """Plain-language status for the UI."""
    vol_ok = vol is not None and np.isfinite(vol)
    if scale >= 0.999:
        msg = "The market is calm, so buys are full size."
    else:
        msg = (f"The market is jumpy (swinging about {vol:.0%} a year, against a normal {target_vol:.0%}), "
               f"so new buys are cut to {scale:.0%} of normal size.") if vol_ok else \
              f"New buys are cut to {scale:.0%} of normal size."
    return {"scale": round(float(scale), 3), "market_vol": round(float(vol), 4) if vol_ok else None,
            "target_vol": target_vol, "message": msg}
