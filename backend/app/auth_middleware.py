"""ASGI middleware that protects all non-public API routes."""

from urllib.parse import urlparse

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.auth import AuthService

_PUBLIC_API_PATHS = frozenset({"/api/health", "/api/auth/login"})
_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if not request.app.state.settings.auth_required:
            return await call_next(request)
        if not request.url.path.startswith("/api/") or request.url.path in _PUBLIC_API_PATHS:
            return await call_next(request)

        raw_token = request.cookies.get("demo_session")
        with request.app.state.session_factory() as session:
            principal = AuthService(session, request.app.state.settings).authenticate(
                raw_token
            )
        if principal is None:
            return JSONResponse(
                status_code=401, content={"detail": "\u8bf7\u5148\u767b\u5f55"}
            )

        request.state.principal = principal
        if request.method in _MUTATING_METHODS and not _same_origin(request):
            return JSONResponse(
                status_code=403,
                content={"detail": "\u975e\u6cd5\u6765\u6e90\u8bf7\u6c42"},
            )
        return await call_next(request)


def _same_origin(request: Request) -> bool:
    origin = request.headers.get("origin")
    if origin is None:
        return True
    parsed = urlparse(origin)
    return bool(parsed.scheme and parsed.netloc) and parsed.netloc.lower() == request.headers.get(
        "host", ""
    ).lower()
