"""
Generic write tools for Franc MCP.

All writes land on a session branch (auto-created on first call) — never main.
Provides:
  - node_upsert  — create-or-update any node by kind + data dict
  - node_delete  — delete a node by ID
"""

from typing import TYPE_CHECKING, Annotated, Any

from fastmcp import Context, FastMCP
from infrahub_sdk.exceptions import GraphQLError
from mcp.types import ToolAnnotations
from pydantic import Field

from franc.session import require_session_branch
from franc.utils import MCPResponse, MCPToolStatus, _log_and_return_error, require_client

if TYPE_CHECKING:
    from infrahub_sdk import InfrahubClient

mcp: FastMCP = FastMCP(name="Franc Write")

_BLOCKED_MUTATIONS = {
    "BranchCreate",
    "BranchDelete",
    "BranchMerge",
    "BranchRebase",
    "CoreSchemaCreate",
    "CoreSchemaDelete",
    "CoreSchemaUpdate",
    "InfrahubSchemaLoad",
}


@mcp.tool(
    tags={"write", "upsert"},
    annotations=ToolAnnotations(readOnlyHint=False, idempotentHint=True, destructiveHint=False),
)
async def node_upsert(
    ctx: Context,
    kind: Annotated[str, Field(description="Schema kind to create or update, e.g. 'DcimPhysicalDevice'.")],
    data: Annotated[
        dict[str, Any],
        Field(
            description=(
                "Field values for the node. "
                "Use hfid strings for related nodes (e.g. 'LocationRack__DC1-RACK-01'). "
                "Include 'hfid' or 'id' to update an existing node; omit both to create a new one."
            )
        ),
    ],
    branch: Annotated[
        str | None,
        Field(
            default=None,
            description=(
                "Target branch. Leave empty to use the auto-managed session branch "
                "(strongly recommended — never write to main directly)."
            ),
        ),
    ] = None,
) -> MCPResponse[dict[str, Any]]:
    """
    Create or update a node of the given schema kind on the session branch.

    If the node already exists (matched by hfid or id), it is updated (upsert).
    If it does not exist, a new node is created.

    The session branch is auto-created on the first write in a session.
    Call create_proposed_change() when ready to merge changes into main.

    Returns:
        {"id", "display_label", "branch", "skipped_attributes": [...]}
    """
    try:
        client: InfrahubClient = require_client(ctx)
    except RuntimeError as exc:
        return await _log_and_return_error(ctx=ctx, error=str(exc))

    target_branch = branch or await require_session_branch(ctx, client)

    await ctx.info(f"Upserting {kind} on branch '{target_branch}'...")

    skipped: list[str] = []
    try:
        # Fetch schema to identify valid attributes and filter unknown keys
        schema = await client.schema.get(kind=kind, branch=target_branch)
        valid_keys = set(schema.attribute_names) | set(schema.relationship_names) | {"hfid", "id"}
        clean_data: dict[str, Any] = {}
        for k, v in data.items():
            if k in valid_keys:
                clean_data[k] = v
            else:
                skipped.append(k)
    except Exception:  # noqa: BLE001
        # Schema fetch failed — pass data as-is and let the SDK validate
        clean_data = data

    if skipped:
        await ctx.info(f"Skipping unknown attributes: {skipped}")

    try:
        node = await client.create(kind=kind, data=clean_data, branch=target_branch)
        await node.save(allow_upsert=True)
    except GraphQLError as exc:
        msg = str(exc).lower()
        if "permission" in msg or "unauthorized" in msg or "forbidden" in msg:
            rem = "Check INFRAHUB_API_TOKEN permissions for write access."
        elif "not found" in msg or "does not exist" in msg:
            rem = f"Kind '{kind}' may not exist in the schema. Use get_schema_mapping() to verify."
        else:
            rem = "Inspect server logs or retry with corrected data."
        return await _log_and_return_error(ctx=ctx, error=exc, remediation=rem)
    except Exception as exc:  # noqa: BLE001
        return await _log_and_return_error(
            ctx=ctx,
            error=exc,
            remediation="Verify the data format and that all required fields are provided.",
        )

    node_id = getattr(node, "id", None)
    display_label = getattr(node, "_display_label", None) or str(node_id)

    await ctx.info(f"Upserted {kind} '{display_label}' ({node_id}) on branch '{target_branch}'.")

    return MCPResponse(
        status=MCPToolStatus.SUCCESS,
        data={
            "id": node_id,
            "display_label": display_label,
            "branch": target_branch,
            "skipped_attributes": skipped,
        },
    )


@mcp.tool(
    tags={"write", "delete"},
    annotations=ToolAnnotations(readOnlyHint=False, idempotentHint=False, destructiveHint=True),
)
async def node_delete(
    ctx: Context,
    node_id: Annotated[str, Field(description="UUID of the node to delete.")],
    kind: Annotated[
        str,
        Field(description="Schema kind of the node, e.g. 'DcimPhysicalDevice'. Required to fetch before delete."),
    ],
    branch: Annotated[
        str | None,
        Field(
            default=None,
            description="Target branch. Leave empty to use the session branch.",
        ),
    ] = None,
) -> MCPResponse[dict[str, Any]]:
    """
    Delete a node by ID on the session branch.

    The node is fetched first to confirm it exists, then deleted.
    Deletion is staged on the session branch — it does NOT affect main until merged.

    Returns:
        {"id", "kind", "branch", "deleted": True}
    """
    try:
        client: InfrahubClient = require_client(ctx)
    except RuntimeError as exc:
        return await _log_and_return_error(ctx=ctx, error=str(exc))

    target_branch = branch or await require_session_branch(ctx, client)

    await ctx.info(f"Deleting {kind} '{node_id}' from branch '{target_branch}'...")

    try:
        node = await client.get(kind=kind, id=node_id, branch=target_branch)
    except Exception as exc:  # noqa: BLE001
        return await _log_and_return_error(
            ctx=ctx,
            error=exc,
            remediation=f"Verify that a {kind} with id='{node_id}' exists on branch '{target_branch}'.",
        )

    try:
        await node.delete()
    except GraphQLError as exc:
        msg = str(exc).lower()
        if "permission" in msg or "unauthorized" in msg:
            rem = "Check INFRAHUB_API_TOKEN permissions for delete access."
        else:
            rem = "Check server logs for details."
        return await _log_and_return_error(ctx=ctx, error=exc, remediation=rem)
    except Exception as exc:  # noqa: BLE001
        return await _log_and_return_error(ctx=ctx, error=exc)

    await ctx.info(f"Deleted {kind} '{node_id}' from '{target_branch}'.")

    return MCPResponse(
        status=MCPToolStatus.SUCCESS,
        data={"id": node_id, "kind": kind, "branch": target_branch, "deleted": True},
    )
