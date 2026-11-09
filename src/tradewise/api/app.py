"""HTTP API and web UI (Starlette)."""

from __future__ import annotations

import hmac
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from .. import strategies
from ..backtest import BacktestConfig, StrategySpec, compare
from ..config import Settings, get_settings
from ..paper import PaperAccounts
from ..auto import Updater
from ..broker import BrokerError
from ..live import LiveError, LiveTrader
from ..services import MarketService, market_context, signals, stock_analysis
from .auth import PasswordGate
from .jobs import JobManager

log = logging.getLogger(__name__)
STATIC = Path(__file__).with_name("static")


class StrategyChoice(BaseModel):
    name: str
    params: dict[str, Any] = Field(default_factory=dict)
    weight: float = Field(1.0, ge=0, le=1)


class BacktestRequest(BaseModel):
    strategies: list[StrategyChoice] = Field(min_length=1, max_length=5)
    config: dict[str, Any] = Field(default_factory=dict)
    split: str | None = None
    combined: bool = True
    without_safety: bool = False


class PaperCreate(BaseModel):
    name: str
    strategy: str
    params: dict[str, Any] = Field(default_factory=dict)
    config: dict[str, Any] = Field(default_factory=dict)


async def _json(request: Request, model: type[BaseModel]) -> BaseModel:
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(400, "body must be JSON") from None
    return model.model_validate(body)


SETUP_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Tradewise · one more step</title>
<style>body{margin:0;min-height:100vh;display:grid;place-items:center;background:#F5F5F1;color:#16181D;
font:16px/1.5 system-ui,sans-serif}main{max-width:560px;margin:16px;background:#fff;border:1px solid #E3E4DE;
border-radius:14px;padding:28px}h1{margin-top:0}</style></head><body><main><h1>One more step</h1>
<p>Tradewise is deployed, but it needs a database to remember prices and practice accounts.</p>
<ol><li>In your Vercel project, open the <strong>Storage</strong> tab.</li>
<li>Choose <strong>Create Database</strong>, pick <strong>Neon (Serverless Postgres)</strong>, the free plan, and
connect it to this project.</li><li>Open <strong>Deployments</strong> and choose <strong>Redeploy</strong> on the latest one.</li></ol>
</main></body></html>"""


def _setup_needed_app():
    async def app(scope, receive, send):
        if scope["type"] != "http":
            return
        resp = JSONResponse({"error": "database not connected"}, status_code=503) \
            if scope["path"].startswith("/api/") else HTMLResponse(SETUP_HTML, status_code=503)
        await resp(scope, receive, send)
    return app


def create_app(settings: Settings | None = None, updater: Updater | None = None,
               live_trader: LiveTrader | None = None) -> Starlette:
    settings = settings or get_settings()
    if settings.serverless and not settings.database_url:
        return _setup_needed_app()  # type: ignore[return-value]
    market = MarketService(settings)
    paper = PaperAccounts(settings.paper_target)
    jobs = JobManager(workers=settings.max_backtest_workers, inline=settings.serverless)
    updater = updater or Updater(market, paper, universe_file=settings.universe_file, serverless=settings.serverless)
    trader = live_trader or (None if settings.serverless else LiveTrader(
        settings.prices_target, market, settings.kite_api_key, settings.kite_api_secret))
    if trader is not None:
        updater.after_update.append(trader.after_price_update)

    def need_trader() -> LiveTrader:
        if trader is None:
            raise LiveError("Real trading isn't available on this host: Zerodha needs a fixed (static) IP address, "
                            "which serverless hosting can't provide. Use the India server setup instead.")
        return trader

    async def submit(kind: str, fn) -> JSONResponse:
        job_id = await run_in_threadpool(jobs.submit, kind, fn)
        if jobs.inline:  # serverless: the work is already finished, return it directly
            return JSONResponse(jobs.get(job_id))
        return JSONResponse({"id": job_id, "status": "queued"}, status_code=202)

    async def health(_: Request):
        return JSONResponse({"status": "ok", "app": "tradewise"})

    async def meta(_: Request):
        return JSONResponse(await run_in_threadpool(market.meta))

    async def list_strategies(_: Request):
        return JSONResponse(strategies.available())

    async def market_view(_: Request):
        data = await run_in_threadpool(market.data)
        return JSONResponse(await run_in_threadpool(market_context, data))

    async def list_stocks(_: Request):
        data = await run_in_threadpool(market.data)
        return JSONResponse([{"symbol": s, "sector": data.sector_of(s)} for s in data.symbols])

    async def signal_view(request: Request):
        name = request.query_params.get("strategy", "trend_pullback")
        try:
            capital = float(request.query_params.get("capital", 500_000))
        except ValueError:
            raise HTTPException(422, "capital must be a number") from None
        cfg = BacktestConfig(initial_capital=capital)
        return JSONResponse(await run_in_threadpool(signals, market, name, None, cfg))

    async def start_refresh(_: Request):
        return await submit("refresh", lambda: updater.update(manual=True))

    async def stock_view(request: Request):
        sym = request.path_params["symbol"]
        name = request.query_params.get("strategy", "trend_pullback")
        return JSONResponse(await run_in_threadpool(stock_analysis, market, sym, name))

    async def start_backtest(request: Request):
        req: BacktestRequest = await _json(request, BacktestRequest)  # type: ignore[assignment]
        for s in req.strategies:  # validate now so the client gets a 422 immediately
            strategies.create(s.name, s.params)
        cfg = BacktestConfig(**req.config)
        specs = [StrategySpec(s.name, s.params, s.weight) for s in req.strategies]
        data = await run_in_threadpool(market.data)
        return await submit("backtest", lambda: compare(data, specs, cfg, split=req.split, combined=req.combined,
                                                              without_safety=req.without_safety))

    async def status(_: Request):
        st = await run_in_threadpool(updater.status)
        return JSONResponse({**st, "login": bool(settings.app_password)})

    async def cron(request: Request):
        """Called by Vercel Cron (or any scheduler) with 'Authorization: Bearer <CRON_SECRET>'."""
        if not settings.cron_secret:
            return JSONResponse({"error": "set CRON_SECRET to enable scheduled updates"}, status_code=403)
        given = request.headers.get("authorization", "")
        if not hmac.compare_digest(given.encode(), f"Bearer {settings.cron_secret}".encode()):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        result = await run_in_threadpool(updater.tick)
        st = await run_in_threadpool(updater.status)
        return JSONResponse({"ran": result is not None, "status": st})

    # ------------------------------------------------------- real trading
    async def live_state(_: Request):
        if trader is None:
            return JSONResponse({"available": False, "reason": "Real trading needs the India server (static IP)."})
        return JSONResponse({"available": True, **(await run_in_threadpool(trader.state))})

    async def live_config(request: Request):
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(400, "body must be a JSON object")
        confirm = body.pop("confirm", None)
        cfg = await run_in_threadpool(need_trader().set_config, body, confirm)
        return JSONResponse(cfg.model_dump())

    async def live_credentials(request: Request):
        body = await request.json()
        await run_in_threadpool(need_trader().set_credentials, str(body.get("api_key", "")),
                                str(body.get("api_secret", "")))
        return JSONResponse({"ok": True})

    async def live_plan(_: Request):
        plan = await run_in_threadpool(need_trader().plan)
        return JSONResponse(plan.to_dict())

    async def live_execute(request: Request):
        body = await request.json()
        return JSONResponse(await run_in_threadpool(need_trader().execute, str(body.get("plan_id", ""))))

    async def live_stop(_: Request):
        return JSONResponse(await run_in_threadpool(need_trader().stop))

    async def broker_login(_: Request):
        return RedirectResponse(need_trader().login_url(), status_code=303)

    async def broker_callback(request: Request):
        q = request.query_params
        if q.get("status") != "success" or not q.get("request_token"):
            return RedirectResponse("/#/live", status_code=303)
        try:
            await run_in_threadpool(need_trader().complete_login, q["request_token"])
        except (LiveError, BrokerError) as exc:
            log.warning("Zerodha login failed: %s", exc)
            need_trader().event(f"Zerodha login failed: {exc}", "error")
            return RedirectResponse("/#/live", status_code=303)
        try:  # straight away: reconcile, protect, and prepare tonight's plan
            await run_in_threadpool(need_trader().plan)
        except (LiveError, BrokerError) as exc:
            need_trader().event(f"Logged in, but the plan could not be prepared yet: {exc}", "warn")
        return RedirectResponse("/#/live", status_code=303)

    async def broker_logout(_: Request):
        await run_in_threadpool(need_trader().logout)
        return JSONResponse({"ok": True})

    async def get_job(request: Request):
        return JSONResponse(jobs.get(request.path_params["job_id"]))

    async def paper_list(_: Request):
        return JSONResponse(await run_in_threadpool(paper.list))

    async def paper_create(request: Request):
        req: PaperCreate = await _json(request, PaperCreate)  # type: ignore[assignment]
        data = await run_in_threadpool(market.data)
        acct = await run_in_threadpool(paper.create, req.name, data, req.strategy, req.params, req.config)
        return JSONResponse(acct, status_code=201)

    async def paper_get(request: Request):
        return JSONResponse(await run_in_threadpool(paper.get, request.path_params["name"]))

    async def paper_update(request: Request):
        data = await run_in_threadpool(market.data)
        return JSONResponse(await run_in_threadpool(paper.update, request.path_params["name"], data))

    async def paper_delete(request: Request):
        await run_in_threadpool(paper.delete, request.path_params["name"])
        return Response(status_code=204)

    async def index(_: Request):
        return FileResponse(STATIC / "index.html")

    # ------------------------------------------------------------ errors
    async def on_validation(_: Request, exc: Exception):
        assert isinstance(exc, ValidationError)
        errors = [{"field": ".".join(map(str, e["loc"])), "message": e["msg"]} for e in exc.errors()]
        return JSONResponse({"error": "invalid request", "details": errors}, status_code=422)

    async def on_value(_: Request, exc: Exception):
        return JSONResponse({"error": str(exc)}, status_code=422)

    async def on_key(_: Request, exc: Exception):
        msg = exc.args[0] if exc.args else "not found"
        return JSONResponse({"error": str(msg)}, status_code=404)

    async def on_live(_: Request, exc: Exception):
        return JSONResponse({"error": str(exc)}, status_code=409)

    async def on_broker(_: Request, exc: Exception):
        return JSONResponse({"error": f"Zerodha: {exc}"}, status_code=502)

    async def on_lookup(_: Request, exc: Exception):
        return JSONResponse({"error": str(exc)}, status_code=409)

    async def on_http(_: Request, exc: Exception):
        assert isinstance(exc, HTTPException)
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code)

    async def on_error(_: Request, exc: Exception):
        log.exception("unhandled error")
        return JSONResponse({"error": "internal error"}, status_code=500)

    @asynccontextmanager
    async def lifespan(_app):
        if settings.auto_update:
            updater.start()
        yield
        updater.stop()
        jobs.shutdown()

    routes = [
        Route("/", index),
        Route("/api/health", health),
        Route("/api/meta", meta),
        Route("/api/strategies", list_strategies),
        Route("/api/market", market_view),
        Route("/api/stocks", list_stocks),
        Route("/api/stocks/{symbol}", stock_view),
        Route("/api/signals", signal_view),
        Route("/api/backtests", start_backtest, methods=["POST"]),
        Route("/api/backtests/{job_id}", get_job),
        Route("/api/jobs/{job_id}", get_job),
        Route("/api/refresh", start_refresh, methods=["POST"]),
        Route("/api/status", status),
        Route("/api/live", live_state),
        Route("/api/live/config", live_config, methods=["POST"]),
        Route("/api/live/plan", live_plan, methods=["POST"]),
        Route("/api/live/credentials", live_credentials, methods=["POST"]),
        Route("/api/live/execute", live_execute, methods=["POST"]),
        Route("/api/live/stop", live_stop, methods=["POST"]),
        Route("/api/broker/login", broker_login),
        Route("/api/broker/callback", broker_callback),
        Route("/api/broker/logout", broker_logout, methods=["POST"]),
        Route("/api/cron/{slot}", cron),
        Route("/api/paper", paper_list),
        Route("/api/paper", paper_create, methods=["POST"]),
        Route("/api/paper/{name}", paper_get),
        Route("/api/paper/{name}", paper_delete, methods=["DELETE"]),
        Route("/api/paper/{name}/update", paper_update, methods=["POST"]),
        Mount("/static", StaticFiles(directory=STATIC), name="static"),
    ]
    app = Starlette(
        routes=routes,
        lifespan=lifespan,
        exception_handlers={
            LiveError: on_live, BrokerError: on_broker,
            ValidationError: on_validation, ValueError: on_value, KeyError: on_key, LookupError: on_lookup,
            HTTPException: on_http, Exception: on_error,
        },
    )
    app.state.market = market
    app.state.jobs = jobs
    app.state.updater = updater
    app.state.trader = trader
    if settings.app_password:
        return PasswordGate(app, settings.app_password, settings.session_secret)  # type: ignore[return-value]
    return app
