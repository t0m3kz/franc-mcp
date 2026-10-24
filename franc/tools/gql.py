"""
GraphQL tools for Franc MCP.

Provides:
  - query_graphql   — read-only GraphQL queries with AST-level mutation blocking
  - mutate_graphql  — write operations on the session branch with a blocklist for
                      dangerous ops (branch management, schema mutations)
"""

import re
from typing import TYPE_CHECKING, Annotated, Any

from fastmcp import Context, FastMCP
from infrahub_sdk.exceptions import GraphQLError
from mcp.types import ToolAnnotations
from pydantic import Field

from franc.session import require_session_branch
from franc.utils import MCPResponse, MCPToolStatus, _log_and_return_error, require_client

if TYPE_CHECKING:
    from infrahub_sdk import InfrahubClient

mcp: FastMCP = FastMCP(name="Franc GraphQL")

# Mutations that must never be called via Franc — branch/schema management bypasses the review workflow
_BLOCKED_MUTATIONS: frozenset[str] = frozenset(
    {
        "BranchCreate",
        "BranchDelete",
        "BranchMerge",
        "BranchRebase",
        "BranchUpdate",
        "CoreSchemaCreate",
        "CoreSchemaDelete",
        "CoreSchemaUpdate",
        "InfrahubSchemaLoad",
        "CoreProposedChangeApprove",
        "CoreProposedChangeMerge",
        "CoreProposedChangeClose",
    }
)

# Regex to detect mutation keyword at the operation level
_MUTATION_RE = re.compile(r"\bmutation\b", re.IGNORECASE)
# Extracts the first operation name from a mutation string
_OPERATION_NAME_RE = re.compile(r"\bmutation\s+(\w+)", re.IGNORECASE)


def _contains_mutation(query: str) -> bool:
    """Return True if the query string contains a mutation operation."""
    return bool(_MUTATION_RE.search(query))


def _extract_mutation_names(query: str) -> list[str]:
    """Extract mutation operation names from a query string."""
    return _OPERATION_NAME_RE.findall(query)


def _check_blocked(query: str) -> str | None:
    """Return an error string if any blocked mutation name appears, else None."""
    names = _extract_mutation_names(query)
    for name in names:
        if name in _BLOCKED_MUTATIONS:
            return (
                f"Mutation '{name}' is blocked — use Franc's dedicated tools for branch and schema management. "
                f"Blocked list: {', '.join(sorted(_BLOCKED_MUTATIONS))}"
            )
    # Also check raw text for blocked names (inline/anonymous mutations)
    for blocked in _BLOCKED_MUTATIONS:
        if re.search(rf"\b{re.escape(blocked)}\b", query):
            return f"Mutation '{blocked}' is blocked."
    return None


@mcp.tool(
    tags={"graphql", "query", "read"},
    annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True, destructive_hint=False),
)
async def query_graphql(
    ctx: Context,
    query: Annotated[
        str,
        Field(description="GraphQL query string. Mutations are rejected — use mutate_graphql for writes."),
    ],
    variables: Annotated[
        dict[str, Any] | None,
        Field(default=None, description="Optional variables dict for the query."),
    ] = None,
    branch: Annotated[
        str | None,
        Field(default=None, description="Branch to query against. Defaults to main."),
    ] = None,
) -> MCPResponse[dict[str, Any]]:
    """
    Execute a read-only GraphQL query against Infrahub.

    Mutations are blocked at the text level — the query is inspected for the 'mutation'
    keyword before execution. Use mutate_graphql() for any write operations.

    Returns:
        The raw GraphQL response data dict.
    """
    if _contains_mutation(query):
        return await _log_and_return_error(
            ctx=ctx,
            error="query_graphql only accepts queries, not mutations.",
            remediation="Use mutate_graphql() for write operations, or node_upsert()/node_delete() for common writes.",
        )

    try:
        client: InfrahubClient = require_client(ctx)
    except RuntimeError as exc:
        return await _log_and_return_error(ctx=ctx, error=str(exc))

    target_branch = branch or "main"
    await ctx.info(f"Executing GraphQL query on branch '{target_branch}'...")

    try:
        result = await client.execute_graphql(
            query=query,
            branch_name=target_branch,
            variables=variables or {},
        )
    except GraphQLError as exc:
        return await _log_and_return_error(
            ctx=ctx,
            error=exc,
            remediation="Check the query syntax and field names. Use get_schema() to inspect available fields.",
        )
    except Exception as exc:  # noqa: BLE001
        return await _log_and_return_error(ctx=ctx, error=exc)

    return MCPResponse(status=MCPToolStatus.SUCCESS, data=result)


@mcp.tool(
    tags={"graphql", "mutation", "write"},
    annotations=ToolAnnotations(read_only_hint=False, idempotent_hint=False, destructive_hint=False),
)
async def mutate_graphql(
    ctx: Context,
    mutation: Annotated[
        str,
        Field(
            description=(
                "GraphQL mutation string. "
                "Branch/schema management mutations are blocked — use Franc's dedicated tools. "
                "The mutation will be executed on the session branch."
            )
        ),
    ],
    variables: Annotated[
        dict[str, Any] | None,
        Field(default=None, description="Optional variables dict for the mutation."),
    ] = None,
    branch: Annotated[
        str | None,
        Field(
            default=None,
            description=(
                "Target branch. Leave empty to use the session branch (recommended). "
                "Never pass 'main' here — changes should go through the review workflow."
            ),
        ),
    ] = None,
) -> MCPResponse[dict[str, Any]]:
    """
    Execute a GraphQL mutation on the session branch.

    Branch/schema management operations are blocked. For common node operations,
    prefer node_upsert() and node_delete() — they are safer and handle schema validation.

    The session branch is auto-created on the first write in a session.
    Call create_proposed_change() when ready to merge changes into main.

    Returns:
        The raw GraphQL response data dict plus {"branch": <target_branch>}.
    """
    # Reject blocked mutations before touching the network
    blocked_error = _check_blocked(mutation)
    if blocked_error:
        return await _log_and_return_error(
            ctx=ctx,
            error=blocked_error,
            remediation=(
                "Use branch_create() for branch management, or get_schema()/get_schemas() "
                "for schema inspection. Schema changes should go through the infrahubctl CLI."
            ),
        )

    try:
        client: InfrahubClient = require_client(ctx)
    except RuntimeError as exc:
        return await _log_and_return_error(ctx=ctx, error=str(exc))

    if branch and branch.lower() == "main":
        return await _log_and_return_error(
            ctx=ctx,
            error="Direct mutations to 'main' are not allowed.",
            remediation="Omit the branch parameter to use the session branch, then create a Proposed Change.",
        )

    target_branch = branch or await require_session_branch(ctx, client)

    await ctx.info(f"Executing GraphQL mutation on branch '{target_branch}'...")

    try:
        result = await client.execute_graphql(
            query=mutation,
            branch_name=target_branch,
            variables=variables or {},
        )
    except GraphQLError as exc:
        return await _log_and_return_error(
            ctx=ctx,
            error=exc,
            remediation="Check the mutation syntax, field names, and variable types.",
        )
    except Exception as exc:  # noqa: BLE001
        return await _log_and_return_error(ctx=ctx, error=exc)

    result["branch"] = target_branch
    return MCPResponse(status=MCPToolStatus.SUCCESS, data=result)
