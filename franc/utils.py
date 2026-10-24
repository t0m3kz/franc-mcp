import asyncio
import hashlib
import json
from contextvars import ContextVar
from enum import Enum
from pathlib import Path
from typing import Any, Generic, TypeVar

import gcf
import tiktoken
from fastmcp import Context
from infrahub_sdk import Config, InfrahubClient
from infrahub_sdk.node import Attribute, InfrahubNode, RelatedNode, RelationshipManager
from pydantic import BaseModel

_TIKTOKEN_ENCODER = tiktoken.get_encoding("cl100k_base")

CURRENT_DIRECTORY = Path(__file__).parent.resolve()
PROMPTS_DIRECTORY = CURRENT_DIRECTORY / "prompts"

# SDK object cache: (branch, kind) -> InfrahubNode schema object (used by nodes tools)
_SCHEMA_CACHE: dict[tuple[str | None, str], Any] = {}

# Serialized dict cache: (branch, kind) -> model_dump() dict (used by get_schema tool)
_SCHEMA_DICT_CACHE: dict[tuple[str | None, str], dict[str, Any]] = {}

# Mapping cache: branch -> {kind: label}
_SCHEMA_MAPPING_CACHE: dict[str | None, dict[str, str]] = {}

# Caller's Infrahub token for the current tool call (set by TokenPassthroughMiddleware; None = shared token)
request_token: ContextVar[str | None] = ContextVar("franc_request_token", default=None)

# Per-token clients, keyed by token digest so raw tokens are never dict keys; oldest evicted first
_TOKEN_CLIENTS: dict[str, InfrahubClient] = {}
_TOKEN_CLIENTS_MAX = 64


T = TypeVar("T")


class MCPToolStatus(Enum):
    SUCCESS = "success"
    ERROR = "error"


class MCPResponse(BaseModel, Generic[T]):
    status: MCPToolStatus
    data: T | None = None
    error: str | None = None
    remediation: str | None = None


def get_prompt(name: str) -> str:
    prompt_file = PROMPTS_DIRECTORY / f"{name}.md"
    if not prompt_file.exists():
        raise FileNotFoundError(f"Prompt file '{prompt_file}' does not exist.")
    return (PROMPTS_DIRECTORY / f"{name}.md").read_text()


def extract_value(val):
    """
    Helper to extract display_label or value from Infrahub object/relationship dicts or lists.
    """
    if isinstance(val, dict):
        if "node" in val:
            return val["node"].get("display_label", "")
        if "edges" in val:
            return [edge["node"].get("display_label", "") for edge in val["edges"] if "node" in edge]
        if set(val.keys()) == {"value"}:
            return val["value"]
        return {k: extract_value(v) for k, v in val.items()}
    if isinstance(val, list):
        return [extract_value(item) for item in val]
    return val


async def _log_and_return_error(ctx: Context, error: str | Exception, remediation: str | None = None) -> MCPResponse:
    """Log an error and return a standardized error response."""
    if isinstance(error, Exception):
        error = str(error)
    await ctx.error(message=error)
    return MCPResponse(
        status=MCPToolStatus.ERROR,
        error=error,
        remediation=remediation,
    )


def require_client(ctx: Context) -> "InfrahubClient":
    """Fetch Infrahub client from the MCP lifespan context, raising if unavailable."""
    request_ctx = ctx.request_context
    if request_ctx is None:
        raise RuntimeError("request_context missing; Infrahub client unavailable.")

    # Allow tests to inject a client directly on the MCP instance without restarting the server
    server = getattr(request_ctx, "server", None)
    server_test_client = getattr(server, "test_client", None)
    if server_test_client is not None:
        return server_test_client

    # Fallback to module-level MCP instance if request_context does not expose the server
    try:
        from franc import server as franc_server  # Lazy import to avoid circular dependency

        module_test_client = getattr(getattr(franc_server, "mcp", None), "test_client", None)
    except Exception:  # noqa: BLE001  # pragma: no cover - defensive guard for import-time issues
        module_test_client = None
    if module_test_client is not None:
        return module_test_client

    lifespan_ctx = getattr(request_ctx, "lifespan_context", None)
    if lifespan_ctx is None:
        raise RuntimeError("lifespan_context missing; Infrahub client unavailable.")
    client = getattr(lifespan_ctx, "client", None)
    if client is None:
        raise RuntimeError("lifespan_context.client missing; Infrahub client unavailable.")
    if token := request_token.get():
        return _client_for_token(client, token)
    return client


def _client_for_token(base: InfrahubClient, token: str) -> InfrahubClient:
    """Client that authenticates as the caller (token passthrough), reusing the shared client's address."""
    key = hashlib.sha256(token.encode()).hexdigest()
    cached = _TOKEN_CLIENTS.get(key)
    if cached is None:
        if len(_TOKEN_CLIENTS) >= _TOKEN_CLIENTS_MAX:
            _TOKEN_CLIENTS.pop(next(iter(_TOKEN_CLIENTS)))
        cached = _TOKEN_CLIENTS[key] = InfrahubClient(address=base.address, config=Config(api_token=token))
    return cached


async def convert_node_to_dict(*, obj: InfrahubNode, branch: str | None, include_id: bool = True) -> dict[str, Any]:
    data = {}

    if include_id:
        data["index"] = obj.id or None

    for attr_name in obj._schema.attribute_names:
        attr: Attribute = getattr(obj, attr_name)
        data[attr_name] = str(attr.value)

    for rel_name in obj._schema.relationship_names:
        rel = getattr(obj, rel_name)
        if rel and isinstance(rel, RelatedNode):
            if not rel.initialized:
                await rel.fetch()
            related_node = obj._client.store.get(
                branch=branch,
                key=rel.peer.id,
                raise_when_missing=False,
            )
            if related_node:
                data[rel_name] = (
                    related_node.get_human_friendly_id_as_string(include_kind=True)
                    if related_node.hfid
                    else related_node.id
                )
        elif rel and isinstance(rel, RelationshipManager):
            peers: list[Any] = []
            if not rel.initialized:
                await rel.fetch()
            for peer in rel.peers:
                # FIXME: We are using the store to avoid doing to many queries to Infrahub
                # but we could end up doing store+infrahub if the store is not populated
                if peer.id is not None:
                    related_node = obj._client.store.get(
                        key=peer.id,
                        raise_when_missing=False,
                        branch=branch,
                    )
                else:
                    related_node = None
                if not related_node:
                    fetch_coro = getattr(peer, "fetch", None)
                    if fetch_coro is not None:
                        await fetch_coro()
                    related_node = peer.peer
                peers.append(
                    related_node.get_human_friendly_id_as_string(include_kind=True)
                    if related_node.hfid
                    else related_node.id,
                )
            data[rel_name] = peers
    return data


def encode_with_gcf(data: Any) -> str:
    """
    Encode data using the GCF (Graph Compact Format) generic profile for
    token-efficient transmission.

    Args:
        data: Python object to encode

    Returns:
        GCF-formatted string
    """
    return gcf.encode_generic(data)


def decode_from_gcf(encoded: str) -> Any:
    """
    Decode data from GCF format back to Python objects.

    Args:
        encoded: GCF-formatted string

    Returns:
        Decoded Python object
    """
    return gcf.decode_generic(encoded)


def _gcf_round_trips(data: Any, encoded: str) -> bool:
    """Verify encode->decode reproduces the original data before trusting the encoding."""
    try:
        return gcf.decode_generic(encoded) == data
    except Exception:  # noqa: BLE001
        return False


def estimate_token_savings(data: Any) -> tuple[dict[str, Any], str]:
    """
    Calculate token savings from GCF vs JSON and return the encoded GCF string.

    Falls back to the JSON string itself (0% savings) if the GCF encoding does not
    round-trip cleanly for this data shape — callers always get something decodable.

    Returns:
        (stats_dict, encoded_str) — stats has json_tokens, gcf_tokens, savings_percent;
        encoded_str is reusable so callers avoid a second encode_generic() call.
    """
    json_str = json.dumps(data)
    gcf_str = gcf.encode_generic(data)

    if not _gcf_round_trips(data, gcf_str):
        gcf_str = json_str

    json_tokens = len(_TIKTOKEN_ENCODER.encode(json_str))
    gcf_tokens = len(_TIKTOKEN_ENCODER.encode(gcf_str))
    savings = ((json_tokens - gcf_tokens) / json_tokens * 100) if json_tokens > 0 else 0

    stats = {
        "json_tokens": json_tokens,
        "gcf_tokens": gcf_tokens,
        "savings_percent": round(savings, 1),
    }
    return stats, gcf_str


def get_cached_schema(branch: str | None, kind: str) -> Any | None:
    """Retrieve cached schema if available."""
    return _SCHEMA_CACHE.get((branch, kind))


def cache_schema(branch: str | None, kind: str, schema: Any) -> None:
    """Cache schema data to reduce redundant fetches."""
    _SCHEMA_CACHE[(branch, kind)] = schema


def get_cached_schema_mapping(branch: str | None) -> dict[str, str] | None:
    """Retrieve cached schema mapping if available."""
    return _SCHEMA_MAPPING_CACHE.get(branch)


def cache_schema_mapping(branch: str | None, mapping: dict[str, str]) -> None:
    """Cache schema mapping to reduce redundant fetches."""
    _SCHEMA_MAPPING_CACHE[branch] = mapping


def get_cached_schema_dict(branch: str | None, kind: str) -> dict[str, Any] | None:
    """Retrieve cached serialized schema dict (model_dump output) if available."""
    return _SCHEMA_DICT_CACHE.get((branch, kind))


def cache_schema_dict(branch: str | None, kind: str, schema_dict: dict[str, Any]) -> None:
    """Cache serialized schema dict to reduce redundant fetches."""
    _SCHEMA_DICT_CACHE[(branch, kind)] = schema_dict


def clear_schema_cache() -> None:
    """Clear all cached schema data. Useful for testing or when schemas change."""
    _SCHEMA_CACHE.clear()
    _SCHEMA_DICT_CACHE.clear()
    _SCHEMA_MAPPING_CACHE.clear()


def attr_val(node: Any, name: str, default: Any = None) -> Any:
    """Extract .value from an Infrahub SDK attribute, falling back to str() or default.

    Replaces the 5-line getattr/hasattr/value dance repeated across every tool.
    For RelatedNode relationships, returns display_label → id (no fetch needed).
    """
    attr = getattr(node, name, None)
    if attr is None:
        return default
    # Cardinality-one relationship: return display label or id without fetching
    if isinstance(attr, RelatedNode):
        label = getattr(attr, "_display_label", None)
        if label:
            return label
        rid = getattr(attr, "_id", None)
        return rid if rid else default
    if hasattr(attr, "value"):
        return attr.value if attr.value is not None else default
    return str(attr) if attr else default


def slim_node(node: Any, fields: list[str]) -> dict[str, Any]:
    """Extract only the requested fields from an Infrahub node as a flat dict.

    Avoids full convert_node_to_dict when only a few fields are needed,
    which saves tokens and avoids unnecessary relationship fetches.
    """
    return {f: attr_val(node, f) for f in fields}


async def _gather_safe(*coros: Any) -> list[Any]:
    """Run coroutines concurrently; replace exceptions with None instead of raising.

    Use for parallel Infrahub queries where partial failure is acceptable
    (e.g. discovering options where some schema kinds may not exist).
    """
    results = await asyncio.gather(*coros, return_exceptions=True)
    return [None if isinstance(r, BaseException) else r for r in results]


async def maybe_compress(
    ctx: Context,
    data: Any,
    kind_label: str,
    data_key: str,
) -> "MCPResponse | None":
    """Return a compressed MCPResponse only if GCF is a genuine, token-saving candidate; else None.

    Candidates over policy: GCF and JSON are both measured in tokens, and GCF is used only
    when it actually wins. If the GCF encoding doesn't round-trip cleanly for this data shape,
    compression is skipped entirely — no undecodable payload is ever shipped under a `*_gcf` key.

    Callers should do:
        return await maybe_compress(...) or MCPResponse(status=SUCCESS, data=data)
    """
    from franc.constants import GCF_AUTO_THRESHOLD_ITEMS  # avoid circular at import time

    if len(data) <= GCF_AUTO_THRESHOLD_ITEMS:
        return None

    stats, gcf_str = estimate_token_savings(data)
    if stats["gcf_tokens"] >= stats["json_tokens"] or not _gcf_round_trips(data, gcf_str):
        return None

    await ctx.info(
        f"Auto-compressing {len(data)} {kind_label} with GCF "
        f"(saving {stats['savings_percent']}%, {stats['json_tokens'] - stats['gcf_tokens']} tokens)"
    )
    return MCPResponse(
        status=MCPToolStatus.SUCCESS,
        data={data_key: gcf_str, "count": len(data)},
    )
