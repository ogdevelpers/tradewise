"""Optional password protection (set APP_PASSWORD). Signed cookie, no user database needed."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import time
from html import escape
from urllib.parse import parse_qs

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

COOKIE = "tw_auth"
MAX_AGE = 30 * 24 * 3600
OPEN_PATHS = ("/login", "/logout", "/api/health", "/api/cron/")


def _secret(password: str, session_secret: str | None) -> bytes:
    base = session_secret or "tradewise-session:" + password
    return hashlib.sha256(base.encode()).digest()


def make_cookie(secret: bytes, now: float | None = None) -> str:
    expires = int((now or time.time()) + MAX_AGE)
    sig = hmac.new(secret, str(expires).encode(), hashlib.sha256).hexdigest()
    return f"{expires}.{sig}"


def valid_cookie(secret: bytes, value: str | None, now: float | None = None) -> bool:
    if not value or "." not in value:
        return False
    expires, sig = value.split(".", 1)
    if not expires.isdigit() or int(expires) < (now or time.time()):
        return False
    good = hmac.new(secret, expires.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(good, sig)


def _https(request: Request) -> bool:
    return request.url.scheme == "https" or request.headers.get("x-forwarded-proto", "") == "https"


LOGIN_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Tradewise · Log in</title>
<style>
body{margin:0;min-height:100vh;display:grid;place-items:center;background:#F5F5F1;color:#16181D;font:15px/1.5 Inter,system-ui,sans-serif}
form{background:#fff;border:1px solid #E3E4DE;border-radius:14px;padding:28px;width:min(360px,calc(100vw - 32px));box-sizing:border-box;display:flex;flex-direction:column;gap:14px}
h1{margin:0;font-size:24px}label{display:flex;flex-direction:column;gap:6px;font-weight:500;font-size:13px}
input{font:16px system-ui;padding:12px;border:1px solid #C9CAC4;border-radius:8px}
button{font:600 15px system-ui;padding:12px;border:0;border-radius:8px;background:#16181D;color:#fff;cursor:pointer;min-height:44px}
.err{background:#FBE5D8;color:#8A3306;border-radius:8px;padding:10px 12px;font-size:14px}p{margin:0;color:#5B5E66;font-size:13px}
</style></head><body><form method="post" action="/login"><h1>Tradewise</h1>
<p>This is a private app. Enter your password to continue.</p>{error}
<label>Password<input type="password" name="password" autocomplete="current-password" required autofocus></label>
<button type="submit">Log in</button></form></body></html>"""


class PasswordGate:
    """ASGI middleware: everything except the login page, health check and cron needs a valid cookie."""

    def __init__(self, app, password: str, session_secret: str | None = None):
        self.app = app
        self.password = password
        self.secret = _secret(password, session_secret)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        request = Request(scope, receive)
        path = scope["path"]
        if path == "/login":
            return await self._login(request)(scope, receive, send)
        if path == "/logout":
            resp = RedirectResponse("/login", status_code=303)
            resp.delete_cookie(COOKIE, path="/")
            return await resp(scope, receive, send)
        if path.startswith(OPEN_PATHS) or valid_cookie(self.secret, request.cookies.get(COOKIE)):
            return await self.app(scope, receive, send)
        if path.startswith("/api/"):
            return await JSONResponse({"error": "login required"}, status_code=401)(scope, receive, send)
        return await RedirectResponse("/login", status_code=303)(scope, receive, send)

    def _login(self, request: Request):
        async def app(scope, receive, send):
            if request.method == "POST":
                body = (await request.body()).decode("utf-8", "replace")
                given = parse_qs(body).get("password", [""])[0]
                if hmac.compare_digest(given.encode(), self.password.encode()):
                    resp: Response = RedirectResponse("/", status_code=303)
                    resp.set_cookie(COOKIE, make_cookie(self.secret), max_age=MAX_AGE, path="/", httponly=True,
                                    secure=_https(request), samesite="lax")
                    return await resp(scope, receive, send)
                await asyncio.sleep(1.0)  # slows down password guessing
                html = LOGIN_HTML.replace("{error}", '<div class="err" role="alert">Wrong password. Try again.</div>')
                return await HTMLResponse(html, status_code=401)(scope, receive, send)
            return await HTMLResponse(LOGIN_HTML.replace("{error}", ""))(scope, receive, send)
        return app


__all__ = ["PasswordGate", "make_cookie", "valid_cookie", "escape"]
