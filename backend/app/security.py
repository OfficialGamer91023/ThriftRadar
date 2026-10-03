"""Request guards. Spec: DESIGN.md §4.3 (client_ip, local_guard, BodySizeLimitMiddleware, demo_denylist)."""

import hmac
import ipaddress
import logging

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

log = logging.getLogger(__name__)

LOCAL_HOSTS = frozenset({"127.0.0.1:8000", "localhost:8000", "[::1]:8000"})
TOKEN_PREFIXES = ("/ingest", "/listener/", "/admin/")
DEMO_DENY_PREFIXES = ("/ingest", "/admin", "/listener", "/api/listener")
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
DEFAULT_BODY_LIMIT = 64 * 1024


def _header(scope: Scope, name: bytes) -> str | None:
    for k, v in scope.get("headers", []):
        if k == name:
            return v.decode("latin-1")
    return None


def _is_token_path(path: str) -> bool:
    return path == "/ingest" or any(path.startswith(p) for p in TOKEN_PREFIXES)


def client_ip(request: Request, hops: int) -> str:
    """The address the N-th trusted proxy from the right saw; the client can't spoof it."""
    direct = request.client.host if request.client else "unknown"
    if hops == 0:
        return direct
    parts = [p.strip() for p in request.headers.get("x-forwarded-for", "").split(",") if p.strip()]
    ip = parts[-hops] if len(parts) >= hops else direct
    try:
        ipaddress.ip_address(ip)
        return ip
    except ValueError:
        return "unknown"


class LocalGuard:
    """Local mode only: Host allowlist (DNS rebinding), token paths, custom header on mutations (CSRF)."""

    def __init__(self, app: ASGIApp, ingest_token: str, allowed_hosts: frozenset[str] = LOCAL_HOSTS):
        self.app = app
        self.token = ingest_token.encode()
        self.allowed_hosts = allowed_hosts

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        if _header(scope, b"host") not in self.allowed_hosts:
            return await JSONResponse({"detail": "misdirected_request"}, 421)(scope, receive, send)
        path, method = scope["path"], scope["method"]
        if _is_token_path(path):
            given = (_header(scope, b"x-ingest-token") or "").encode("latin-1")
            if not hmac.compare_digest(given, self.token):
                return await JSONResponse({"detail": "unauthorized"}, 401)(scope, receive, send)
        elif method in UNSAFE_METHODS and _header(scope, b"x-thriftradar") != "1":
            return await JSONResponse({"detail": "missing_x_thriftradar"}, 403)(scope, receive, send)
        await self.app(scope, receive, send)


class _BodyTooLarge(Exception):
    pass


class BodySizeLimitMiddleware:
    """413 before reading when Content-Length is over; otherwise count bytes as they arrive."""

    def __init__(self, app: ASGIApp, limits: dict[str, int], default: int = DEFAULT_BODY_LIMIT):
        self.app = app
        self.limits = sorted(limits.items(), key=lambda kv: -len(kv[0]))  # longest prefix wins
        self.default = default

    def limit_for(self, path: str) -> int:
        for prefix, limit in self.limits:
            if path == prefix or path.startswith(prefix.rstrip("/") + "/"):
                return limit
        return self.default

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        limit = self.limit_for(scope["path"])
        too_large = JSONResponse({"detail": "body_too_large"}, 413)
        declared = _header(scope, b"content-length")
        if declared is not None:
            try:
                if int(declared) > limit:
                    return await too_large(scope, receive, send)
            except ValueError:
                return await JSONResponse({"detail": "bad_content_length"}, 400)(scope, receive, send)

        received = 0
        started = False

        async def counting_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise _BodyTooLarge
            return message

        async def tracking_send(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, counting_receive, tracking_send)
        except _BodyTooLarge:
            if not started:
                await too_large(scope, receive, send)


class DemoDenylist:
    """Demo only. Belt-and-braces: these routers aren't mounted in demo either."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["path"].startswith(DEMO_DENY_PREFIXES):
            return await JSONResponse({"detail": "Not Found"}, 404)(scope, receive, send)
        await self.app(scope, receive, send)


class SecurityHeaders:
    HEADERS = [
        (b"x-content-type-options", b"nosniff"),
        (b"referrer-policy", b"no-referrer"),
        (b"x-frame-options", b"DENY"),
    ]

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                message["headers"] = list(message.get("headers", [])) + self.HEADERS
            await send(message)

        await self.app(scope, receive, with_headers)
