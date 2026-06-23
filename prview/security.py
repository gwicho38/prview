"""Localhost-only security middleware (spec Security section, items 2-3).

Two gates on every API request:
  1. Origin/Host must be the server's own loopback identity — blocks
     cross-origin drivers and DNS-rebinding (a foreign Host header means the
     request was resolved through a non-loopback name).
  2. A per-session token (header `X-Prview-Token` or `?token=`) must match the
     one minted at launch. Missing/wrong → 401.

Initial HTML/asset GETs are exempt from the token gate so the browser can load
the page that *carries* the token; they still pass the Origin/Host gate. The
token is injected at startup via set_token() on app.state — the G6 launcher
mints it and calls server.set_session_token().

Rejections return structured {error, hint?} JSON — never a stack trace.
"""
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

TOKEN_HEADER = "X-Prview-Token"
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1"}
_EXEMPT_PREFIXES = ("/static",)
_EXEMPT_PATHS = {"/", "/index.html", "/favicon.ico"}


def _host_is_loopback(host_header: str) -> bool:
    if not host_header:
        return True  # TestClient / direct loopback connections omit Host:port host
    host = host_header.rsplit(":", 1)[0] if host_header.count(":") == 1 else host_header
    host = host.strip("[]")
    return host in _LOOPBACK_HOSTS or host.rsplit(":", 1)[0] in _LOOPBACK_HOSTS


def _origin_is_loopback(origin: str) -> bool:
    if not origin:
        return True
    # origin = scheme://host[:port]
    rest = origin.split("://", 1)[-1]
    host = rest.split("/", 1)[0]
    host = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
    host = host.strip("[]")
    return host in _LOOPBACK_HOSTS


def _reject(status: int, error: str, hint: str | None = None) -> JSONResponse:
    body = {"error": error}
    if hint:
        body["hint"] = hint
    return JSONResponse(body, status_code=status)


def _is_exempt(path: str) -> bool:
    return path in _EXEMPT_PATHS or path.startswith(_EXEMPT_PREFIXES)


class SecurityMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        host = request.headers.get("host", "")
        origin = request.headers.get("origin", "")

        if not _host_is_loopback(host):
            return _reject(403, "Forbidden Host header", "requests must target 127.0.0.1")
        if not _origin_is_loopback(origin):
            return _reject(403, "Forbidden Origin header", "cross-origin requests are blocked")

        if not _is_exempt(request.url.path):
            expected = getattr(request.app.state, "session_token", None)
            supplied = request.headers.get(TOKEN_HEADER) or request.query_params.get("token")
            if not expected or supplied != expected:
                return _reject(401, "Missing or invalid session token",
                               "reopen the URL printed by `uv run prview`")

        return await call_next(request)
