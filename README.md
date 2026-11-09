# Tradewise

Systematic swing-trading research for NSE stocks: stock analysis, buy/sell signals,
backtesting with Indian delivery costs, strategy comparison and paper trading.

> Research tool, not investment advice. No strategy avoids losses; the goal is
> small losses and larger wins, proven on history and in paper trading first.

## Easiest way to start (no commands, ever)

1. Install Python 3.10 or newer from python.org. On Windows, tick **"Add python.exe to PATH"** during setup.
2. Unzip this folder somewhere permanent, such as Documents. Don't run it from inside the zip or from Downloads,
   because the app runs from this folder from now on.
3. Double-click **`start.bat`** (Windows) or **`start.command`** (Mac; the first time, right-click it and choose Open).
   On Linux, run `./start.sh`.

That's the only time you need the start file. From then on:
- **Tradewise starts by itself** whenever you log in to your computer, and runs quietly in the background.
- **A "Tradewise" icon on your desktop** opens it in your browser.
- **Prices update by themselves.** After 4 pm on each weekday, Tradewise fetches the day's closing prices. If they
  aren't published yet, it retries every 30 minutes. If your computer was off, it catches up on every missed day
  the next time it starts. Practice accounts trade automatically after each update.
- **The open page refreshes itself** when new prices arrive. A line at the top always says how fresh the prices are.

If there's no internet the first time, Tradewise shows clearly marked **demo prices**. It keeps trying every hour
and switches to real prices by itself.

To stop starting at login: `tradewise install --remove` (from the `.venv` folder), or delete the "Tradewise" item
from your Startup folder (Windows) or Login Items (Mac).

## Real trading on a free server in India

Real buy and sell orders go through Zerodha's Kite Connect API, which only accepts orders from a registered
fixed IP address. **DEPLOY-INDIA.md** sets up a free Oracle Cloud server in Mumbai with a fixed IP, HTTPS and the
**Real trading** page. The flow:
- you log in to Zerodha each evening (the exchange requires a daily manual login)
- you review the plan and place it with one click
- orders go as after-market orders, with safety exits and profit steps held at Zerodha as GTT orders

Code: `tradewise/broker/kite.py` (API client) and `tradewise/live/trader.py` (reconcile, protect, plan,
execute, emergency stop). The tests run the full cycle against a simulated Zerodha (`tests/fake_kite.py`).

## Put it online (Vercel, without real trading)

Tradewise can run as a private website on Vercel's free plan, with a Neon Postgres database, password login and
automatic evening price updates. Follow **DEPLOY.md**: about 15 minutes of clicking, no commands.

On Vercel, `api/index.py` serves the app and `vercel.json` schedules the updates (Vercel Cron calls
`/api/cron/*` with `CRON_SECRET`). The app detects Vercel by itself:
- it stores data in Postgres (`DATABASE_URL`)
- it runs updates only when the scheduled calls arrive, since serverless has no background threads
- it finishes backtests within the request
- it requires `APP_PASSWORD`

## For developers

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e ".[yahoo,dev]"
tradewise start            # background app + start at login + opens browser (--foreground, --demo)
tradewise status           # is it running, how fresh are the prices
tradewise stop
pytest                     # 97 tests, about 20 seconds (set TRADEWISE_TEST_PG_URL to also test Postgres)
```

Other commands: `tradewise data`, `tradewise backtest`, `tradewise signals`, `tradewise paper ...`, `tradewise serve`.

## How automatic updates work

`tradewise/auto.py` runs inside the app and checks every 5 minutes:
- **Time check:** it uses the Indian market clock (IST) to work out which day's closing prices should exist.
  That's today after 4 pm on a weekday, otherwise the previous weekday.
- **Fetching:** if the saved prices are older, it fetches the missing days, then updates every practice account.
- **Never half a day:** today's prices are never stored before 4 pm, so a half-finished trading day can't distort
  a signal.
- **Holidays:** if no prices appear after 6 tries, it treats the day as a market holiday and waits for the next day.

Data, logs and practice accounts live in one per-user folder:
- **Windows:** `%LOCALAPPDATA%\Tradewise`
- **Mac:** `~/Library/Application Support/Tradewise`
- **Linux:** `~/.local/share/tradewise`

Override it with `TRADEWISE_DATA_DIR`.

## Strategies

**Trend pullback** (`trend_pullback`) buys a stock only when all of these hold:
- Nifty 50 is above its 200-day average.
- The stock is above its 200 DMA, and its 50 DMA is above its 200 DMA.
- Its low touched the 20 EMA in the last 3 days, and it closed up above the EMA.
- RSI(14) turned up from the 40–55 zone, on above-average volume.
- It has enough liquidity.

Each trade has a stop at 2×ATR and risks 1% of capital. Half is booked at 2R, the
stop then moves to breakeven, and the rest exits on a close below the 20 EMA. The
whole position exits on a close below the 50 DMA.

**Momentum rotation** (`momentum_rotation`) runs on the first trading day of each month:
- Ranks liquid stocks above their 200 DMA by volatility-adjusted 6- and 12-month
  returns, skipping the latest month.
- Holds the top 15 at equal weight.
- Sells a holding only when it falls out of the top 30.
- Makes no new buys while Nifty is below its 200 DMA.

Every parameter can be changed. `GET /api/strategies` lists them with their limits.

## How the backtest stays honest

- **Signal timing:** signals are generated at the close and filled at the **next
  open**, with slippage.
- **Stops and targets:** a stop fills at the stop price, or at the open on a gap
  down. A target fills at the target. If both are hit on the same bar, the stop is
  assumed to hit first.
- **No lookahead:** a test re-runs every strategy on data cut off at several dates
  and checks that nothing changes. A signal can never use future data.
- **Costs** (Zerodha delivery rates, Nov 2026; edit them in `costs.py`): STT 0.1%
  on both sides, NSE 0.00307%, SEBI ₹10/crore, stamp duty 0.015% on buys, 18% GST,
  and ₹15.34 DP charge per sell.
- **Guardrails:**
  - risk per trade
  - max positions
  - max 20% in one stock and 25% in one sector
  - new entries pause after a 6% monthly drawdown
- **Walk-forward:** the train/test split shows whether results hold up on years the
  rules were never looked at.
- **Paper trading** runs the *same simulator* one day at a time. A test proves that
  stopping and resuming gives identical results to one long run.

## Architecture

```
src/tradewise/
  data/          providers (Yahoo, CSV, synthetic) → validation → SQLite store → MarketData (wide, aligned)
  indicators.py  EMA/RSI/ATR/ADX/MACD matching TradingView, vectorised over all stocks
  strategies/    Strategy base + registry; add a class to strategies/__init__.py to plug one in
  backtest/      day-by-day Simulator, metrics, compare (parallel, combined sleeves, benchmark)
  paper/         paper accounts persisted in SQLite; Broker protocol for a future live adapter
  services.py    signals, stock analysis, market context (shared by API and CLI)
  api/           Starlette JSON API + background job queue + web UI (plain JS, no build step)
  cli.py         `tradewise` command
```

**Scaling notes:**
- Indicators run on whole matrices, so the simulator reads one NumPy row per day.
  A 40-stock, 12-year backtest takes about 1 second, and Nifty 500 stays in seconds.
- Strategy comparisons run in separate processes.
- The API runs backtests as background jobs. Swap `api/jobs.py` for Celery or RQ
  when running several servers.
- The price store is plain SQL behind one class. Move it to Postgres or TimescaleDB
  when you outgrow SQLite.
- Configuration comes from environment variables: `TRADEWISE_DATA_DIR`,
  `TRADEWISE_BENCHMARK_SYMBOL`, `TRADEWISE_UNIVERSE_FILE`, `TRADEWISE_API_PORT`,
  `TRADEWISE_MAX_BACKTEST_WORKERS`, `TRADEWISE_LOG_LEVEL`.

## Using your own universe

Download an index constituent list (for example the Nifty 200 CSV) from
niftyindices.com, then:

```bash
tradewise data --universe ind_nifty200list.csv --full
```

CSV data from any source also works: `tradewise data --provider csv --folder ./my_csvs`,
with one `SYMBOL.csv` file per stock containing Date, Open, High, Low, Close and Volume columns.

## Not built yet (next phases)

- **Fundamentals filter** (ROE, debt, pledging) needs a fundamentals data source.
- **Results/earnings calendar** to skip trades just before results.
- **India VIX position scaling.**
- **Alerts** on Telegram or email.
- **Live broker adapter** (Kite, Upstox and others) behind `paper/broker.py`. Add it
  only after months of paper results match the backtest. SEBI's retail algo rules
  (in force from 1 April 2026) require a broker-registered strategy ID and a static IP.
