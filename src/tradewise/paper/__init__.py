"""Paper trading: the backtest simulator run forward one day at a time, with state saved in SQLite.

Using the same simulator guarantees paper results follow exactly the rules
that were backtested. A live broker can later replace the simulated fills
behind the ``Broker`` protocol in ``broker.py``.
"""

from .account import PaperAccounts

__all__ = ["PaperAccounts"]
