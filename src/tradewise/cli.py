"""Command line: ``tradewise <command>``. Run ``tradewise --help`` for the list."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from . import strategies
from .config import configure_logging, get_settings

log = logging.getLogger("tradewise")


def _pct(x) -> str:
    return "—" if x is None else f"{x * 100:.1f}%"


def _num(x, nd=2) -> str:
    return "—" if x is None else f"{x:.{nd}f}"


def cmd_data(args) -> int:
    from .data.ingest import ingest
    from .data.providers import get_provider
    from .data.store import PriceStore
    from .data.universe import load_universe

    s = get_settings()
    store = PriceStore(s.prices_target)
    instruments = load_universe(args.universe or s.universe_file)
    kwargs = {"folder": args.folder} if args.provider == "csv" else {}
    if args.provider == "synthetic":
        kwargs = {"end": args.end or "2026-10-30"}
    provider = get_provider(args.provider, **kwargs)
    report = ingest(store, provider, instruments, s.benchmark_symbol, start=args.start, end=args.end,
                    incremental=not args.full, workers=args.workers)
    print(f"Updated {len(report.updated)} symbols, {sum(report.updated.values())} rows.")
    for sym, err in sorted(report.failed.items()):
        print(f"  FAILED {sym}: {err}", file=sys.stderr)
    return 0 if s.benchmark_symbol not in report.failed else 1


def _load():
    from .data.store import PriceStore

    s = get_settings()
    return PriceStore(s.prices_target).load_market(s.benchmark_symbol)


def cmd_backtest(args) -> int:
    from .backtest import BacktestConfig, StrategySpec, compare

    data = _load()
    names = args.strategy or ["trend_pullback", "momentum_rotation"]
    weights = [float(w) for w in args.weights.split(",")] if args.weights else [1.0] * len(names)
    if len(weights) != len(names):
        print("--weights needs one value per strategy", file=sys.stderr)
        return 2
    cfg = BacktestConfig(initial_capital=args.capital, start=args.start, end=args.end,
                         risk_per_trade=args.risk, slippage=args.slippage)
    res = compare(data, [StrategySpec(n, None, w) for n, w in zip(names, weights)], cfg, split=args.split,
                  workers=args.workers)
    head = f"{'Strategy':28} {'CAGR':>7} {'MaxDD':>7} {'Sharpe':>7} {'Win%':>6} {'Trades':>7} {'Test/Train':>10}"
    print(head)
    print("-" * len(head))
    for r in res["results"]:
        m, sp = r["metrics"], r["split"]
        print(f"{r['label'][:28]:28} {_pct(m.get('cagr')):>7} {_pct(m.get('max_drawdown')):>7} "
              f"{_num(m.get('sharpe')):>7} {_pct(m.get('win_rate')):>6} {str(m.get('trades') or '—'):>7} "
              f"{_num(sp and sp['test_vs_train']):>10}")
    if args.output:
        Path(args.output).write_text(json.dumps(res, indent=2, default=str))
        print(f"Full results written to {args.output}")
    return 0


def cmd_signals(args) -> int:
    from .services import MarketService, signals

    res = signals(MarketService(get_settings()), args.strategy)
    print(f"{res['label']} signals at close of {res['date']} (orders for next open)")
    if not res["entries"]:
        print("  No new entries today.")
    for e in res["entries"]:
        print(f"  BUY {e['symbol']:12} close {_num(e['close'])}  stop {_num(e['stop'])}  qty {e.get('qty', 0)}  "
              f"score {_num(e['score'], 1)}  — {e['reason']}")
    return 0


def cmd_paper(args) -> int:
    from .paper import PaperAccounts

    s = get_settings()
    accts = PaperAccounts(s.paper_target)
    if args.action == "create":
        a = accts.create(args.name, _load(), args.strategy, config={"initial_capital": args.capital})
        print(f"Created paper account {a['name']} ({a['strategy']}), {len(a['pending'])} order(s) queued.")
    elif args.action == "update":
        names = [args.name] if args.name else [a["name"] for a in accts.list()]
        data = _load()
        for n in names:
            a = accts.update(n, data)
            m = a["summary"]
            print(f"{n}: +{a['days_processed']} day(s), equity {_num(m['final_equity'])}, "
                  f"{len(a['positions'])} open, {len(a['pending'])} queued")
    elif args.action == "show":
        print(json.dumps(accts.get(args.name), indent=2, default=str))
    elif args.action == "list":
        for a in accts.list():
            print(f"{a['name']:20} {a['strategy']:20} last {a['last_date']}  equity {_num(a['summary']['final_equity'])}")
    elif args.action == "delete":
        accts.delete(args.name)
        print(f"Deleted {args.name}")
    return 0


def _load_demo(settings) -> None:
    from .data.ingest import ingest
    from .data.providers import SyntheticProvider
    from .data.store import PriceStore
    from .data.universe import load_universe

    store = PriceStore(settings.prices_target)
    if store.get_meta("provider") != "synthetic":
        print("Loading DEMO prices (made up, for exploring only)...")
        ingest(store, SyntheticProvider(), load_universe(settings.universe_file), settings.benchmark_symbol,
               incremental=False)
    store.set_meta("demo_locked", "1")


def cmd_start(args) -> int:
    """What the start file and the desktop icon run: make sure Tradewise runs in the background,
    register it to start at login (first time only) and open it in the browser."""
    from . import launcher
    from .data.store import PriceStore

    s = get_settings()
    if args.port:
        s = s.model_copy(update={"api_port": args.port})
    s.data_dir.mkdir(parents=True, exist_ok=True)
    if args.demo:
        _load_demo(s)
    else:
        PriceStore(s.prices_target).set_meta("demo_locked", "0")
    if args.foreground:
        if not args.no_browser:
            import threading

            threading.Timer(2.0, launcher.open_browser, [s]).start()
        return cmd_serve(args)
    if not launcher.is_installed(s) and not args.no_install:
        res = launcher.install(s)
        print("Tradewise will now start by itself whenever you log in to this computer,")
        print("and there is a 'Tradewise' icon on your desktop to open it.")
        for n in res.notes:
            print(n)
    try:
        ok = launcher.ensure_running(s)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if not ok:
        print(f"Tradewise did not start. Details are in {s.log_file}", file=sys.stderr)
        return 1
    if not args.no_browser:
        launcher.open_browser(s)
    print(f"Tradewise is running in the background at {s.url}")
    print("It fetches new prices by itself after the market closes. You can close this window.")
    return 0


def cmd_serve(args) -> int:
    import os

    import uvicorn

    from .api import create_app

    s = get_settings()
    configure_logging(s.log_level, s.log_file)
    s.data_dir.mkdir(parents=True, exist_ok=True)
    s.pid_file.write_text(str(os.getpid()))
    try:
        uvicorn.run(create_app(s), host=getattr(args, "host", None) or s.api_host,
                    port=getattr(args, "port", None) or s.api_port, log_level="warning")
    finally:
        try:
            if s.pid_file.read_text().strip() == str(os.getpid()):
                s.pid_file.unlink()
        except OSError:
            pass
    return 0


def cmd_stop(_args) -> int:
    from . import launcher

    s = get_settings()
    print("Tradewise stopped." if launcher.stop_server(s) else "Tradewise was not running.")
    return 0


def cmd_install(args) -> int:
    from . import launcher

    s = get_settings()
    if args.remove:
        removed = launcher.uninstall(s)
        launcher.stop_server(s)
        print("Tradewise will no longer start at login." if removed else "Nothing to remove.")
        return 0
    res = launcher.install(s)
    for p in res.created:
        print(f"Created {p}")
    for n in res.notes:
        print(n)
    return 0


def cmd_status(_args) -> int:
    import urllib.request

    from . import launcher

    s = get_settings()
    if launcher.ping(s) != "tradewise":
        print("Tradewise is not running. Start it with: tradewise start")
        return 1
    with urllib.request.urlopen(s.url + "api/status", timeout=5) as r:
        st = json.loads(r.read().decode())
    print(f"Running at {s.url}")
    print(f"Prices up to: {st['data_last_date']} ({st['source']})")
    print(st["message"])
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="tradewise", description="Systematic swing trading for NSE stocks")
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("data", help="download prices into the local store")
    d.add_argument("--provider", choices=["yahoo", "csv", "synthetic"], default="yahoo")
    d.add_argument("--folder", help="folder of <SYMBOL>.csv files (csv provider)")
    d.add_argument("--universe", help="CSV with a Symbol column (default: bundled sample of 40 large caps)")
    d.add_argument("--start", default="2014-01-01")
    d.add_argument("--end")
    d.add_argument("--full", action="store_true", help="re-download everything instead of only new days")
    d.add_argument("--workers", type=int, default=4)
    d.set_defaults(fn=cmd_data)

    names = [s["name"] for s in strategies.available()]
    b = sub.add_parser("backtest", help="backtest and compare strategies")
    b.add_argument("--strategy", action="append", choices=names, help="repeat to compare several")
    b.add_argument("--weights", help="capital split for the combined row, e.g. 0.3,0.7")
    b.add_argument("--capital", type=float, default=500_000)
    b.add_argument("--risk", type=float, default=0.01)
    b.add_argument("--slippage", type=float, default=0.001)
    b.add_argument("--start", default="2016-01-01")
    b.add_argument("--end")
    b.add_argument("--split", default="2022-01-01", help="walk-forward split date")
    b.add_argument("--workers", type=int, default=2)
    b.add_argument("--output", help="write full JSON results here")
    b.set_defaults(fn=cmd_backtest)

    s = sub.add_parser("signals", help="entry signals at the latest close")
    s.add_argument("--strategy", choices=names, default="trend_pullback")
    s.set_defaults(fn=cmd_signals)

    pp = sub.add_parser("paper", help="paper trading accounts")
    pp.add_argument("action", choices=["create", "update", "show", "list", "delete"])
    pp.add_argument("name", nargs="?")
    pp.add_argument("--strategy", choices=names, default="trend_pullback")
    pp.add_argument("--capital", type=float, default=500_000)
    pp.set_defaults(fn=cmd_paper)

    st = sub.add_parser("start", help="start Tradewise in the background and open it (what the desktop icon does)")
    st.add_argument("--demo", action="store_true", help="use made-up demo prices (no internet needed)")
    st.add_argument("--foreground", action="store_true", help="run in this window instead of the background")
    st.add_argument("--no-browser", action="store_true")
    st.add_argument("--no-install", action="store_true", help="don't register start-at-login")
    st.add_argument("--port", type=int)
    st.set_defaults(fn=cmd_start)

    sv = sub.add_parser("serve", help="run the web app in this window (used by the background launcher)")
    sv.add_argument("--host")
    sv.add_argument("--port", type=int)
    sv.set_defaults(fn=cmd_serve)

    sub.add_parser("stop", help="stop the background app").set_defaults(fn=cmd_stop)
    sub.add_parser("status", help="show whether the app runs and how fresh the prices are").set_defaults(fn=cmd_status)
    ins = sub.add_parser("install", help="start Tradewise at login and add a desktop icon")
    ins.add_argument("--remove", action="store_true", help="undo: stop starting at login, remove the icon")
    ins.set_defaults(fn=cmd_install)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(get_settings().log_level)
    if args.cmd == "paper" and args.action in ("create", "show", "delete") and not args.name:
        print(f"paper {args.action} needs an account name", file=sys.stderr)
        return 2
    if args.cmd == "data" and args.provider == "csv" and not args.folder:
        print("--folder is required with --provider csv", file=sys.stderr)
        return 2
    try:
        return int(args.fn(args) or 0)
    except (LookupError, ValueError) as exc:
        msg = exc.args[0] if isinstance(exc, KeyError) and exc.args else exc
        print(f"error: {msg}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
