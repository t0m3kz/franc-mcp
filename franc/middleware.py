"""
ASGI middleware stack for Franc MCP.

Layers (outermost → innermost):
  RequestIdMiddleware      — attaches a unique X-Request-ID to every request/response
  StructuredLoggingMiddleware — logs method, path, status, duration, request-id
  ErrorHandlingMiddleware  — catches unhandled exceptions and returns JSON 500
  PingMiddleware           — short-circuits GET /ping → 200 {"status": "ok"} without
                             touching the MCP server (useful for load-balancer health checks)

Usage:
    from franc.app import create_app
    app = create_app()   # returns the full ASGI app with middleware applied
"""

import logging
import time
import uuid
from collections.abc import Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

logger = logging.getLogger("franc.middleware")


class RequestIdMiddleware(BaseHTTPMiddleware):
    """
    Attach a unique request ID to each request.

    - Reads X-Request-ID header if provided by the caller; otherwise generates one.
    - Sets X-Request-ID on the response.
    - Stores the value in request.state.request_id for downstream middleware/handlers.
    """

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response


class StructuredLoggingMiddleware(BaseHTTPMiddleware):
    """
    Log every request with method, path, status code, duration, and request ID.

    Format:
        [franc] GET /sse 200 42ms req=abc123
    """

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        start = time.monotonic()
        request_id = getattr(request.state, "request_id", "-")

        response = await call_next(request)

        duration_ms = int((time.monotonic() - start) * 1000)
        logger.info(
            "[franc] %s %s %d %dms req=%s",
            request.method,
            request.url.path,
            response.status_code,
            duration_ms,
            request_id,
        )
        return response


class ErrorHandlingMiddleware(BaseHTTPMiddleware):
    """
    Catch any unhandled exception from the MCP server and return a structured JSON 500.

    Without this, unhandled errors surface as empty/malformed responses which confuse
    MCP clients. A JSON envelope with {"error": ...} gives the LLM something to reason about.
    """

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        request_id = getattr(request.state, "request_id", "-")
        try:
            return await call_next(request)
        except Exception as exc:
            logger.exception(
                "[franc] Unhandled exception on %s %s req=%s",
                request.method,
                request.url.path,
                request_id,
            )
            return JSONResponse(
                status_code=500,
                content={
                    "error": "Internal server error",
                    "detail": str(exc),
                    "request_id": request_id,
                },
                headers={"X-Request-ID": request_id},
            )


class PingMiddleware:
    """
    Short-circuit GET /ping and GET /health without hitting the MCP stack.

    Returns immediately with {"status": "ok"} so container orchestrators and
    load balancers can verify the process is alive without triggering full MCP init.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http" and scope["method"] == "GET" and scope["path"] in ("/ping", "/health"):
            response = JSONResponse({"status": "ok"})
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


def build_middleware_stack(app: ASGIApp) -> ASGIApp:
    """
    Wrap an ASGI app with the full Franc middleware stack.

    Order matters — outermost runs first on request, last on response:
      PingMiddleware (pure ASGI, wraps everything)
        RequestIdMiddleware
          StructuredLoggingMiddleware
            ErrorHandlingMiddleware
              <app>
    """
    # BaseHTTPMiddleware wrappers (innermost first, because each wraps the previous)
    app = ErrorHandlingMiddleware(app)  # type: ignore[assignment]
    app = StructuredLoggingMiddleware(app)  # type: ignore[assignment]
    app = RequestIdMiddleware(app)  # type: ignore[assignment]
    # Pure ASGI wrapper — outermost, runs before Starlette parses the request
    app = PingMiddleware(app)  # type: ignore[assignment]
    return app
