"""
Session branch management for Franc MCP.

Every write operation targets a per-session branch (mcp/session-YYYYMMDD-<hex>)
so nothing ever lands in main directly. The session branch is created lazily on
the first write and reused for the lifetime of the MCP session.

Thread-safety: asyncio.Lock per session prevents duplicate branch creation when
two writes fire concurrently in the same session.
"""

import asyncio
import hashlib
import logging
import os
import time
from dataclasses import dataclass, field
from weakref import WeakKeyDictionary

from fastmcp import Context
from infrahub_sdk import InfrahubClient
from infrahub_sdk.exceptions import GraphQLError

logger = logging.getLogger("franc.session")

# Pattern placeholders: {date} → YYYYMMDD, {hex} → 8-char random-ish hex
_DEFAULT_BRANCH_PATTERN = "mcp/session-{date}-{hex}"
_MAX_BRANCH_RETRIES = 5


def _branch_hex(session_key: str, attempt: int) -> str:
    """Deterministic but unique-enough hex from session key + attempt."""
    raw = f"{session_key}-{attempt}-{time.monotonic_ns()}"
    return hashlib.sha1(raw.encode()).hexdigest()[:8]


def _make_branch_name(pattern: str, session_key: str, attempt: int) -> str:
    from datetime import datetime, timezone

    date_str = datetime.now(tz=timezone.utc).date().strftime("%Y%m%d")
    hex_str = _branch_hex(session_key, attempt)
    return pattern.format(date=date_str, hex=hex_str, user="franc")


@dataclass
class SessionState:
    """Per-session mutable state tracked across tool calls."""

    branch: str | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


# WeakKeyDictionary so entries are GC'd when the session Context is collected.
_session_states: WeakKeyDictionary = WeakKeyDictionary()


def _get_or_create_session_state(ctx: Context) -> SessionState:
    """Return (or lazily create) a SessionState for this MCP context object."""
    # Use the request_context as a stable identity key; fall back to ctx itself.
    key = getattr(ctx, "request_context", None) or ctx
    if key not in _session_states:
        _session_states[key] = SessionState()
    return _session_states[key]


async def get_session_branch(ctx: Context) -> str | None:
    """Return the current session branch name, or None if none created yet."""
    state = _get_or_create_session_state(ctx)
    return state.branch


async def require_session_branch(ctx: Context, client: InfrahubClient) -> str:
    """
    Return the active session branch, creating it if needed.

    On first call per session: creates a branch named mcp/session-YYYYMMDD-<hex>
    and stores it on the session state. Subsequent calls return the cached name.
    On collision: retries up to _MAX_BRANCH_RETRIES times with a new hex suffix.
    On stale branch (deleted/merged externally): resets and creates a new one.
    """
    state = _get_or_create_session_state(ctx)

    # Fast path — branch already allocated
    if state.branch is not None:
        if await _branch_still_exists(client, state.branch):
            return state.branch
        logger.warning("Session branch '%s' no longer exists — recreating.", state.branch)
        state.branch = None

    # Slow path — allocate under lock to prevent concurrent double-creation
    async with state.lock:
        if state.branch is not None:
            return state.branch

        pattern = os.getenv("FRANC_SESSION_BRANCH_PATTERN", _DEFAULT_BRANCH_PATTERN)
        session_key = str(id(state))

        for attempt in range(_MAX_BRANCH_RETRIES):
            name = _make_branch_name(pattern, session_key, attempt)
            try:
                await client.branch.create(
                    branch_name=name,
                    sync_with_git=False,
                    description="Franc MCP session branch — auto-created, never merge directly",
                    wait_until_completion=True,
                )
                state.branch = name
                await ctx.info(f"Session branch created: {name}")
                return name
            except GraphQLError as exc:
                msg = str(exc).lower()
                if "already exists" in msg or "duplicate" in msg or "conflict" in msg:
                    logger.debug("Branch name '%s' taken, retrying (attempt %d)", name, attempt)
                    continue
                raise

        raise RuntimeError(
            f"Failed to create session branch after {_MAX_BRANCH_RETRIES} attempts. "
            "Something is very wrong — like naming your variables after your pets."
        )


async def reset_session_branch(ctx: Context) -> None:
    """Discard the current session branch reference (does NOT delete the branch in Infrahub)."""
    state = _get_or_create_session_state(ctx)
    async with state.lock:
        old = state.branch
        state.branch = None
        if old:
            await ctx.info(f"Session branch reference cleared (was: {old}). Next write will create a new branch.")


async def _branch_still_exists(client: InfrahubClient, branch_name: str) -> bool:
    """Return True if the branch still exists in Infrahub."""
    try:
        branches = await client.branch.all()
        return branch_name in branches
    except Exception:  # noqa: BLE001
        return False
