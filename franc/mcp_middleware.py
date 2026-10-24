"""
MCP-level middleware for Franc (FastMCP hooks — works for stdio *and* HTTP transports).

Not to be confused with ``franc.middleware``, which is the ASGI stack and only exists for HTTP.

Layers (outermost → innermost), each opt-in/out through environment variables:
  OTelTracingMiddleware     — span per tool call            (FRANC_OTEL=true, needs opentelemetry-api)
  MetricsMiddleware         — per-tool call/error/latency   (FRANC_METRICS=true, scraped at GET /metrics)
  AuditMiddleware           — one structured log line/call  (FRANC_AUDIT=true, default on)
  TokenPassthroughMiddleware— per-caller Infrahub token     (FRANC_AUTH_MODE=token-passthrough, HTTP only)
  ReadOnlyMiddleware        — hide + reject write tools     (FRANC_READ_ONLY=true)
  SafeRetryMiddleware       — retry transient errors, reads only (FRANC_RETRIES, default 2; 0 disables)
  ResponseSizeGuardMiddleware — refuse oversized responses  (FRANC_MAX_RESPONSE_BYTES, default 1000000; 0 disables)
"""

from __future__ import annotations

import logging
import os
import time
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import httpx
import pydantic_core
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_http_headers
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.server.middleware.error_handling import RetryMiddleware
from fastmcp.tools.base import ToolResult
from mcp.types import TextContent
from starlette.requests import Request
from starlette.responses import PlainTextResponse

from franc.utils import request_token

logger = logging.getLogger("franc.mcp")
audit_logger = logging.getLogger("franc.audit")

# Tools carrying any of these tags change Infrahub state (branches, objects, proposed changes).
# `reset_session` is deliberately not one of them: it only forgets a local branch reference.
WRITE_TAGS = frozenset({"write", "create"})

AUTH_MODE_SHARED = "shared"
AUTH_MODE_TOKEN_PASSTHROUGH = "token-passthrough"

DEFAULT_MAX_RESPONSE_BYTES = 1_000_000
DEFAULT_RETRIES = 2

_TRUTHY = {"1", "true", "yes", "on"}


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    return default if raw is None else raw.strip().lower() in _TRUTHY


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


async def _resolve_tool(context: MiddlewareContext[Any]) -> Any | None:
    """Look up the tool a tools/call request targets; None when it cannot be resolved."""
    fmcp = context.fastmcp_context
    if fmcp is None:
        return None
    return await fmcp.fastmcp.get_tool(context.message.name)


def _is_write(tool: Any | None) -> bool:
    """Fail closed: a tool that cannot be resolved is treated as a write."""
    return tool is None or bool(WRITE_TAGS & set(tool.tags or ()))


def _is_read_only(tool: Any | None) -> bool:
    """True only when the tool explicitly declares ``read_only_hint``."""
    annotations = getattr(tool, "annotations", None)
    return bool(getattr(annotations, "read_only_hint", False))


# ---------------------------------------------------------------------------
# Read-only mode
# ---------------------------------------------------------------------------


class ReadOnlyMiddleware(Middleware):
    """Hide write tools from discovery and reject calls to them, so a read-only deployment is enforced twice."""

    async def on_list_tools(self, context: MiddlewareContext[Any], call_next: CallNext[Any, Any]) -> Sequence[Any]:
        tools = await call_next(context)
        return [t for t in tools if not (WRITE_TAGS & set(t.tags or ()))]

    async def on_call_tool(self, context: MiddlewareContext[Any], call_next: CallNext[Any, Any]) -> Any:
        if _is_write(await _resolve_tool(context)):
            logger.warning("read_only_blocked tool=%s", context.message.name)
            raise ToolError(
                f"Tool '{context.message.name}' is not available: Franc is running in read-only mode "
                "(FRANC_READ_ONLY=true). Write operations are disabled."
            )
        return await call_next(context)


# ---------------------------------------------------------------------------
# Per-caller Infrahub token
# ---------------------------------------------------------------------------


def extract_request_token() -> str | None:
    """Infrahub token from the current HTTP request: ``X-INFRAHUB-KEY`` or ``Authorization: Bearer``."""
    headers = get_http_headers(include={"authorization", "x-infrahub-key"})
    if token := headers.get("x-infrahub-key"):
        return token.strip() or None
    auth = headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip() or None
    return None


class TokenPassthroughMiddleware(Middleware):
    """Run every tool call with the *caller's* Infrahub token instead of the server's shared one.

    Fail-closed: a call without a token is rejected, so a misconfigured gateway can never fall back to
    the server credential. Needs an HTTP transport — stdio has no headers.
    """

    async def on_call_tool(self, context: MiddlewareContext[Any], call_next: CallNext[Any, Any]) -> Any:
        token = extract_request_token()
        if not token:
            raise ToolError(
                "This Franc instance requires your own Infrahub token: send it as "
                "'X-INFRAHUB-KEY: <token>' or 'Authorization: Bearer <token>' (FRANC_AUTH_MODE=token-passthrough)."
            )
        reset = request_token.set(token)
        try:
            return await call_next(context)
        finally:
            request_token.reset(reset)


# ---------------------------------------------------------------------------
# Audit, metrics, tracing
# ---------------------------------------------------------------------------


class AuditMiddleware(Middleware):
    """One structured log line per tool call. Logs argument *names* only — values may hold secrets."""

    async def on_call_tool(self, context: MiddlewareContext[Any], call_next: CallNext[Any, Any]) -> Any:
        user = get_http_headers().get("x-forwarded-user", "-")
        arg_keys = ",".join(sorted((context.message.arguments or {}).keys()))
        start = time.perf_counter()
        status = "ok"
        try:
            return await call_next(context)
        except Exception:
            status = "error"
            raise
        finally:
            audit_logger.info(
                "tool=%s user=%s status=%s duration_ms=%.1f args=[%s]",
                context.message.name,
                user,
                status,
                (time.perf_counter() - start) * 1000,
                arg_keys,
            )


@dataclass
class _ToolStats:
    calls: int = 0
    errors: int = 0
    duration_s: float = 0.0


class MetricsMiddleware(Middleware):
    """In-process per-tool counters, exposed in Prometheus text format."""

    def __init__(self) -> None:
        self._stats: dict[str, _ToolStats] = defaultdict(_ToolStats)

    async def on_call_tool(self, context: MiddlewareContext[Any], call_next: CallNext[Any, Any]) -> Any:
        stats = self._stats[context.message.name]
        start = time.perf_counter()
        try:
            return await call_next(context)
        except Exception:
            stats.errors += 1
            raise
        finally:
            stats.calls += 1
            stats.duration_s += time.perf_counter() - start

    def snapshot(self) -> dict[str, dict[str, float]]:
        return {
            name: {"calls": s.calls, "errors": s.errors, "duration_seconds": round(s.duration_s, 6)}
            for name, s in self._stats.items()
        }

    def prometheus_text(self) -> str:
        lines = [
            "# HELP franc_tool_calls_total Tool calls by tool name.",
            "# TYPE franc_tool_calls_total counter",
        ]
        snap = self.snapshot()
        lines += [f'franc_tool_calls_total{{tool="{n}"}} {int(v["calls"])}' for n, v in sorted(snap.items())]
        lines += [
            "# HELP franc_tool_errors_total Tool calls that raised.",
            "# TYPE franc_tool_errors_total counter",
        ]
        lines += [f'franc_tool_errors_total{{tool="{n}"}} {int(v["errors"])}' for n, v in sorted(snap.items())]
        lines += [
            "# HELP franc_tool_duration_seconds_total Cumulative tool wall time.",
            "# TYPE franc_tool_duration_seconds_total counter",
        ]
        lines += [
            f'franc_tool_duration_seconds_total{{tool="{n}"}} {v["duration_seconds"]}' for n, v in sorted(snap.items())
        ]
        return "\n".join(lines) + "\n"


class OTelTracingMiddleware(Middleware):
    """Span per tool call. Requires ``opentelemetry-api``; the exporter/SDK is configured by the host process."""

    def __init__(self, tracer: Any) -> None:
        self._tracer = tracer

    async def on_call_tool(self, context: MiddlewareContext[Any], call_next: CallNext[Any, Any]) -> Any:
        with self._tracer.start_as_current_span(f"franc.tool.{context.message.name}") as span:
            span.set_attribute("franc.tool", context.message.name)
            try:
                return await call_next(context)
            except Exception as exc:
                span.record_exception(exc)
                raise


# ---------------------------------------------------------------------------
# Retry (reads only)
# ---------------------------------------------------------------------------


class SafeRetryMiddleware(RetryMiddleware):
    """RetryMiddleware that never retries a tool call that could write.

    Replaying a non-idempotent write after a timeout can apply it twice, so only tools that are explicitly
    ``read_only_hint`` are retried; every other request type keeps the stock retry behaviour.
    """

    async def on_request(self, context: MiddlewareContext[Any], call_next: CallNext[Any, Any]) -> Any:
        if context.method == "tools/call":
            tool = await _resolve_tool(context)
            if not _is_read_only(tool):
                return await call_next(context)
        return await super().on_request(context, call_next)


# ---------------------------------------------------------------------------
# Response size guard
# ---------------------------------------------------------------------------


class ResponseSizeGuardMiddleware(Middleware):
    """Replace an oversized tool response with an error telling the agent how to narrow the query.

    FastMCP's stock ResponseLimitingMiddleware truncates mid-payload (invalid JSON) and strips every tool's
    outputSchema from discovery. Franc tools return typed MCPResponse objects, so a clear "too large" error
    is more useful to the agent and leaves tool discovery untouched.
    """

    def __init__(self, max_size: int) -> None:
        self.max_size = max_size

    async def on_call_tool(self, context: MiddlewareContext[Any], call_next: CallNext[Any, Any]) -> Any:
        result = await call_next(context)
        if not isinstance(result, ToolResult):
            return result
        size = len(pydantic_core.to_json(result, fallback=str))
        if size <= self.max_size:
            return result
        logger.warning("response_too_large tool=%s bytes=%d limit=%d", context.message.name, size, self.max_size)
        return ToolResult(
            content=[
                TextContent(
                    type="text",
                    text=(
                        f"Response from '{context.message.name}' was {size} bytes, over the {self.max_size} byte limit. "
                        "Narrow the request: add filters, lower `limit`, page with `offset`, or pick a specific "
                        "branch/device."
                    ),
                )
            ],
            is_error=True,
        )


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------

_metrics: MetricsMiddleware | None = None


def get_metrics() -> MetricsMiddleware | None:
    return _metrics


def configure_mcp_middleware(mcp: FastMCP) -> None:
    """Register the opt-in middleware on ``mcp`` according to the FRANC_* environment variables."""
    global _metrics

    if _env_bool("FRANC_OTEL"):
        try:
            from opentelemetry import trace

            mcp.add_middleware(OTelTracingMiddleware(trace.get_tracer("franc")))
        except ImportError:
            logger.warning("FRANC_OTEL=true but opentelemetry-api is not installed; tracing disabled.")

    if _env_bool("FRANC_METRICS"):
        _metrics = MetricsMiddleware()
        mcp.add_middleware(_metrics)
        collector = _metrics

        @mcp.custom_route("/metrics", methods=["GET"])
        async def _metrics_endpoint(_request: Request) -> PlainTextResponse:
            return PlainTextResponse(collector.prometheus_text(), media_type="text/plain; version=0.0.4")

    if _env_bool("FRANC_AUDIT", default=True):
        mcp.add_middleware(AuditMiddleware())

    auth_mode = os.getenv("FRANC_AUTH_MODE", AUTH_MODE_SHARED).strip().lower()
    if auth_mode == AUTH_MODE_TOKEN_PASSTHROUGH:
        mcp.add_middleware(TokenPassthroughMiddleware())
    elif auth_mode != AUTH_MODE_SHARED:
        logger.warning("Unknown FRANC_AUTH_MODE=%r; using %r.", auth_mode, AUTH_MODE_SHARED)

    if _env_bool("FRANC_READ_ONLY"):
        mcp.add_middleware(ReadOnlyMiddleware())

    if (retries := _env_int("FRANC_RETRIES", DEFAULT_RETRIES)) > 0:
        mcp.add_middleware(
            SafeRetryMiddleware(
                max_retries=retries,
                base_delay=0.5,
                retry_exceptions=(ConnectionError, TimeoutError, httpx.TransportError),
            )
        )

    if (max_bytes := _env_int("FRANC_MAX_RESPONSE_BYTES", DEFAULT_MAX_RESPONSE_BYTES)) > 0:
        mcp.add_middleware(ResponseSizeGuardMiddleware(max_bytes))
