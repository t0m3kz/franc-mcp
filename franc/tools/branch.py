from typing import TYPE_CHECKING, Annotated

from fastmcp import Context, FastMCP
from infrahub_sdk.branch import BranchData
from infrahub_sdk.exceptions import GraphQLError
from mcp.types import ToolAnnotations
from pydantic import Field

from franc.utils import MCPResponse, MCPToolStatus, _log_and_return_error, require_client

if TYPE_CHECKING:
    from infrahub_sdk import InfrahubClient

mcp: FastMCP = FastMCP(name="Infrahub Branches")


@mcp.tool(
    tags={"branches", "create"},
    annotations=ToolAnnotations(readOnlyHint=False, idempotentHint=True, destructiveHint=False),
)
async def branch_create(
    ctx: Context,
    name: Annotated[str, Field(description="Name of the branch to create.")],
    sync_with_git: Annotated[bool, Field(default=False, description="Whether to sync the branch with git.")],
) -> MCPResponse:
    """Create a new branch in infrahub.

    Parameters:
        name: Name of the branch to create.
        sync_with_git: Whether to sync the branch with git. Defaults to False.

    Returns:
        Dictionary with success status and branch details.
    """

    try:
        client: InfrahubClient = require_client(ctx)
    except RuntimeError as exc:
        return await _log_and_return_error(
            ctx=ctx, error=str(exc), remediation="Start the MCP with a configured client."
        )
    await ctx.info(f"Creating branch {name} in Infrahub...")

    # Detect whether a token was configured (infrahub-sdk reads INFRAHUB_API_TOKEN from env)
    import os  # local import to avoid polluting module namespace unnecessarily

    token_present = bool(os.getenv("INFRAHUB_API_TOKEN"))
    if not token_present:
        await ctx.info(
            "INFRAHUB_API_TOKEN not set; proceeding unauthenticated; branch creation may fail with authorization error."
        )

    try:
        branch = await client.branch.create(
            branch_name=name,
            sync_with_git=sync_with_git,
            description="",
            wait_until_completion=True,
        )
    except GraphQLError as exc:
        # Heuristics to classify common failure causes
        msg = str(exc).lower()
        if "permission" in msg or "not authorized" in msg or "forbidden" in msg or "unauthorized" in msg:
            remediation = (
                "Validate that the API token has branch management rights. "
                "If running locally, ensure INFRAHUB_API_TOKEN belongs to an admin or a role with branch:create."
            )
        elif "already exists" in msg or "duplicate" in msg or "conflict" in msg:
            remediation = "Choose a different branch name; it already exists."
        else:
            remediation = "Re-run with debug logging; inspect server logs for details."
        return await _log_and_return_error(ctx=ctx, error=exc, remediation=remediation)

    return MCPResponse(
        status=MCPToolStatus.SUCCESS,
        data={
            "name": branch.name,
            "id": branch.id,
        },
    )


@mcp.tool(tags={"branches", "retrieve"}, annotations=ToolAnnotations(readOnlyHint=True))
async def get_branches(ctx: Context) -> MCPResponse:
    """Retrieve all branches from Infrahub."""

    try:
        client: InfrahubClient = require_client(ctx)
    except RuntimeError as exc:
        return await _log_and_return_error(
            ctx=ctx, error=str(exc), remediation="Start the MCP with a configured client."
        )
    await ctx.info("Fetching all branches from Infrahub...")

    branches: dict[str, BranchData] = await client.branch.all()

    return MCPResponse(status=MCPToolStatus.SUCCESS, data=branches)
