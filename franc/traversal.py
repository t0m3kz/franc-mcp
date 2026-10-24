"""
Graph helpers: Neo4j HTTP client and traversal node-reference resolution.

This module is pure infrastructure — no fastmcp, no MCP tool definitions.
It is imported by franc.tools.graph: the Neo4j helper powers the execute_cypher escape-hatch tool,
the resolver/shaper helpers power find_reachable.
All path traversal itself uses InfrahubClient.traverse_paths() / reachable_nodes() (SDK >= 1.10), not Cypher.
"""

from __future__ import annotations

import base64
import os
import uuid
from typing import TYPE_CHECKING, Any

import httpx
from infrahub_sdk.exceptions import NodeNotFoundError, SchemaNotFoundError

from franc.constants import (
    NEO4J_HTTP_URL,
    NEO4J_PASSWORD,
    NEO4J_TX_ENDPOINT,
    NEO4J_USER,
)

if TYPE_CHECKING:
    from infrahub_sdk.client import InfrahubClient
    from infrahub_sdk.graph_traversal import PathNode, ReachableNodesResult
    from infrahub_sdk.node import InfrahubNode

# ---------------------------------------------------------------------------
# Neo4j HTTP helper
# ---------------------------------------------------------------------------


def _neo4j_run(
    query: str,
    url: str = NEO4J_HTTP_URL,
    user: str = NEO4J_USER,
    password: str = NEO4J_PASSWORD,
) -> list[dict[str, Any]]:
    """Execute a single Cypher statement via Neo4j HTTP transaction API."""
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    resp = httpx.post(
        f"{url}{NEO4J_TX_ENDPOINT}",
        headers={"Authorization": f"Basic {token}", "Content-Type": "application/json"},
        json={"statements": [{"statement": query}]},
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("errors"):
        raise RuntimeError(f"Cypher error: {data['errors']}")
    result = data["results"][0]
    cols = result["columns"]
    return [dict(zip(cols, row["row"])) for row in result["data"]]


def _neo4j_credentials(ctx_neo4j_url: str | None) -> tuple[str, str, str]:
    url = ctx_neo4j_url or os.getenv("NEO4J_HTTP_URL", NEO4J_HTTP_URL)
    user = os.getenv("NEO4J_USER", NEO4J_USER)
    password = os.getenv("NEO4J_PASSWORD", NEO4J_PASSWORD)
    return url, user, password


# ---------------------------------------------------------------------------
# Traversal node references (UUID or kind-qualified HFID)
# ---------------------------------------------------------------------------

# Matches infrahub_sdk's get_human_friendly_id_as_string(include_kind=True) output: Kind__part1__part2
_HFID_SEPARATOR = "__"
_MIN_HFID_PARTS = 2


class NodeResolutionError(Exception):
    """Raised when a node reference cannot be resolved to a node."""


def is_uuid(value: str) -> bool:
    """True only for a canonical dashed UUID — the form Infrahub node ids take."""
    try:
        return str(uuid.UUID(value)) == value.lower()
    except ValueError:
        return False


async def resolve_node_ref(client: InfrahubClient, ref: str, *, branch: str | None = None) -> str | InfrahubNode:
    """Resolve a UUID or kind-qualified HFID (``Kind__part1__part2``) for use with the SDK traversal calls.

    A UUID is passed through unverified (existence can't be checked without knowing the kind; a missing node
    surfaces later as a traversal error). Anything else must be a kind-qualified HFID and is looked up.

    Raises:
        NodeResolutionError: malformed reference, unknown kind, or no node matches the HFID.
    """
    if is_uuid(ref):
        return ref
    parts = ref.split(_HFID_SEPARATOR)
    if len(parts) < _MIN_HFID_PARTS:
        raise NodeResolutionError(f"'{ref}' is neither a UUID nor a kind-qualified HFID (expected 'Kind__id').")
    kind, hfid = parts[0], parts[1:]
    try:
        return await client.get(kind=kind, hfid=hfid, branch=branch)
    except (NodeNotFoundError, SchemaNotFoundError, ValueError, IndexError) as exc:
        raise NodeResolutionError(f"Could not resolve '{ref}': {exc}") from exc


def _shape_node(node: PathNode) -> dict[str, Any]:
    return {"id": node.id, "kind": node.kind, "display_label": node.display_label, "hfid": node.hfid}


def shape_reachable_result(result: ReachableNodesResult) -> dict[str, Any]:
    """Compact, serializable form of a ReachableNodesResult."""
    return {
        "source": _shape_node(result.source),
        "count": result.count,
        "dependencies": [
            {
                "depth": dep.depth,
                "node": _shape_node(dep.node),
                "path": [
                    {
                        "kind": hop.node.kind,
                        "display_label": hop.node.display_label,
                        **({"relationship": hop.relationship.from_label} if hop.relationship is not None else {}),
                    }
                    for hop in dep.path.hops
                ],
            }
            for dep in result.dependencies
        ],
    }
