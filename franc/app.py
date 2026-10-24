"""
ASGI app factory for Franc MCP.

Usage:
    # Run with uvicorn (HTTP/SSE transport):
    uvicorn franc.app:app --host 0.0.0.0 --port 8001

    # Or via fastmcp CLI (stdio transport, default):
    fastmcp run franc/server.py

create_app() returns the FastMCP ASGI app wrapped with the full middleware stack.
The module-level `app` is the ready-to-serve instance.
"""

from starlette.types import ASGIApp

from franc.middleware import build_middleware_stack
from franc.server import mcp


def create_app() -> ASGIApp:
    """Return the Franc ASGI app with all middleware applied."""
    base: ASGIApp = mcp.http_app(transport="sse")  # type: ignore[arg-type]
    return build_middleware_stack(base)


# Module-level singleton — used by `uvicorn franc.app:app`
app: ASGIApp = create_app()
