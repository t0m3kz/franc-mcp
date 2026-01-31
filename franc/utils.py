from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Generic, Optional, TypeVar

import tiktoken
import toons
from fastmcp import Context
from infrahub_sdk.node import Attribute, InfrahubNode, RelatedNode, RelationshipManager
from pydantic import BaseModel

CURRENT_DIRECTORY = Path(__file__).parent.resolve()
PROMPTS_DIRECTORY = CURRENT_DIRECTORY / "prompts"

# Simple in-memory cache for schema metadata
# Key: (branch, kind) -> schema data
_SCHEMA_CACHE: dict[tuple[str | None, str], Any] = {}

# Cache for schema mappings
# Key: branch -> mapping dict
_SCHEMA_MAPPING_CACHE: dict[str | None, dict[str, str]] = {}


T = TypeVar("T")


class MCPToolStatus(Enum):
    SUCCESS = "success"
    ERROR = "error"


class MCPResponse(BaseModel, Generic[T]):
    status: MCPToolStatus
    data: Optional[T] = None
    error: Optional[str] = None
    remediation: Optional[str] = None


if TYPE_CHECKING:
    from infrahub_sdk import InfrahubClient


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
    except Exception:  # pragma: no cover - defensive guard for import-time issues
        module_test_client = None
    if module_test_client is not None:
        return module_test_client

    lifespan_ctx = getattr(request_ctx, "lifespan_context", None)
    if lifespan_ctx is None:
        raise RuntimeError("lifespan_context missing; Infrahub client unavailable.")
    client = getattr(lifespan_ctx, "client", None)
    if client is None:
        raise RuntimeError("lifespan_context.client missing; Infrahub client unavailable.")
    return client


async def convert_node_to_dict(*, obj: InfrahubNode, branch: str | None, include_id: bool = True) -> dict[str, Any]:  # noqa: C901
    data = {}

    if include_id:
        data["index"] = obj.id or None

    for attr_name in obj._schema.attribute_names:  # noqa: SLF001
        attr: Attribute = getattr(obj, attr_name)
        data[attr_name] = str(attr.value)

    for rel_name in obj._schema.relationship_names:  # noqa: SLF001
        rel = getattr(obj, rel_name)
        if rel and isinstance(rel, RelatedNode):
            if not rel.initialized:
                await rel.fetch()
            related_node = obj._client.store.get(  # noqa: SLF001
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


def encode_with_toon(data: Any) -> str:
    """
    Encode data using toon format for token-efficient transmission.

    Args:
        data: Python object to encode

    Returns:
        TOON-formatted string
    """
    return toons.dumps(data)  # type: ignore[attr-defined]


def decode_from_toon(encoded: str) -> Any:
    """
    Decode data from toon format back to Python objects.

    Args:
        encoded: TOON-formatted string

    Returns:
        Decoded Python object
    """
    return toons.loads(encoded)  # type: ignore[attr-defined]


def estimate_token_savings(data: Any) -> dict[str, Any]:
    """
    Calculate actual token savings from using toon encoding vs JSON.

    Uses tiktoken (cl100k_base encoding for GPT-4) to measure real token counts.

    Args:
        data: Python object to analyze

    Returns:
        Dict with json_tokens, toon_tokens, json_length, toon_length, savings_percent
    """
    import json

    json_str = json.dumps(data)
    toon_str = toons.dumps(data)  # type: ignore[attr-defined]

    # Get actual token counts using GPT-4 tokenizer
    enc = tiktoken.get_encoding("cl100k_base")
    json_tokens = len(enc.encode(json_str))
    toon_tokens = len(enc.encode(toon_str))

    # Also include character lengths for reference
    json_len = len(json_str)
    toon_len = len(toon_str)

    # Calculate savings based on actual tokens
    savings = ((json_tokens - toon_tokens) / json_tokens * 100) if json_tokens > 0 else 0

    return {
        "json_tokens": json_tokens,
        "toon_tokens": toon_tokens,
        "json_length": json_len,
        "toon_length": toon_len,
        "savings_percent": round(savings, 1),
    }


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


def clear_schema_cache() -> None:
    """Clear all cached schema data. Useful for testing or when schemas change."""
    _SCHEMA_CACHE.clear()
    _SCHEMA_MAPPING_CACHE.clear()
