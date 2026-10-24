import os
from typing import TYPE_CHECKING, Annotated, Any

from fastmcp import Context, FastMCP
from infrahub_sdk.branch import BranchData
from infrahub_sdk.exceptions import GraphQLError
from mcp.types import ToolAnnotations
from pydantic import Field

from franc.session import get_session_branch, reset_session_branch
from franc.utils import MCPResponse, MCPToolStatus, _log_and_return_error, require_client

if TYPE_CHECKING:
    from infrahub_sdk import InfrahubClient

mcp: FastMCP = FastMCP(name="Infrahub Branches")

_PROPOSED_CHANGE_KIND = "CoreProposedChange"

_ARTIFACT_QUERY = """
query($branch: String!) {
    CoreProposedChange(source_branch__value: $branch) {
        edges {
            node {
                name { value }
                state { value }
                validations {
                    edges {
                        node {
                            __typename
                            label { value }
                            conclusion { value }
                            ... on CoreArtifactValidator {
                                checks {
                                    edges {
                                        node {
                                            __typename
                                            ... on CoreArtifactCheck {
                                                name { value }
                                                artifact_id { value }
                                                storage_id { value }
                                                conclusion { value }
                                                severity { value }
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
    }
}
"""


@mcp.tool(
    tags={"branches", "create"},
    annotations=ToolAnnotations(readOnlyHint=False, idempotentHint=True, destructiveHint=False),
)
async def branch_create(
    ctx: Context,
    name: Annotated[str, Field(description="Name of the branch to create.")],
    sync_with_git: Annotated[bool, Field(default=False, description="Whether to sync the branch with git.")],
) -> MCPResponse:
    """Create a new branch in Infrahub."""

    try:
        client: InfrahubClient = require_client(ctx)
    except RuntimeError as exc:
        return await _log_and_return_error(
            ctx=ctx, error=str(exc), remediation="Start the MCP with a configured client."
        )
    await ctx.info(f"Creating branch {name} in Infrahub...")

    if not os.getenv("INFRAHUB_API_TOKEN"):
        await ctx.info("INFRAHUB_API_TOKEN not set; proceeding unauthenticated; branch creation may fail.")

    try:
        branch = await client.branch.create(
            branch_name=name,
            sync_with_git=sync_with_git,
            description="",
            wait_until_completion=True,
        )
    except GraphQLError as exc:
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
        data={"name": branch.name, "id": branch.id},
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


@mcp.tool(
    tags={"branches", "proposed-change", "create"},
    annotations=ToolAnnotations(readOnlyHint=False, idempotentHint=False, destructiveHint=False),
)
async def create_proposed_change(
    ctx: Context,
    source_branch: Annotated[str, Field(description="Branch with the changes (never 'main').")],
    name: Annotated[str, Field(description="Short descriptive name for the proposed change.")],
    description: Annotated[str, Field(default="", description="Optional longer description.")],
    destination_branch: Annotated[str, Field(default="main", description="Target branch (default: main).")],
    is_draft: Annotated[bool, Field(default=False, description="Mark as draft (not ready for merge).")],
) -> MCPResponse[dict[str, Any]]:
    """
    Create a Proposed Change in Infrahub to request merging source_branch into destination_branch.

    This triggers Infrahub to run all validators and generate artifacts (device configs, diffs, checks).
    Call get_proposed_change_artifacts() afterwards to see what configuration changed on each device.

    Returns:
        {"id", "name", "source_branch", "destination_branch", "state", "url"}
    """
    if source_branch == destination_branch:
        return await _log_and_return_error(
            ctx=ctx,
            error="source_branch and destination_branch must differ.",
            remediation="Never create a proposed change from main to main.",
        )

    try:
        client: InfrahubClient = require_client(ctx)
    except RuntimeError as exc:
        return await _log_and_return_error(ctx=ctx, error=str(exc), remediation="Start MCP with a configured client.")

    await ctx.info(f"Creating Proposed Change '{name}': {source_branch} → {destination_branch}")

    try:
        pc_node = await client.create(
            kind=_PROPOSED_CHANGE_KIND,
            data={
                "name": name,
                "description": description,
                "source_branch": source_branch,
                "destination_branch": destination_branch,
                "state": "open",
                "is_draft": is_draft,
            },
        )
        await pc_node.save(allow_upsert=False)
    except Exception as exc:  # noqa: BLE001
        return await _log_and_return_error(
            ctx=ctx,
            error=exc,
            remediation="Ensure the source branch exists and INFRAHUB_API_TOKEN has write permissions.",
        )

    pc_id = getattr(pc_node, "id", None)
    address = getattr(client, "address", "http://localhost:8000").rstrip("/")
    url = f"{address}/proposed-changes/{pc_id}" if pc_id else None

    await ctx.info(f"Proposed Change created: {pc_id} — validators and artifact generation now running.")

    return MCPResponse(
        status=MCPToolStatus.SUCCESS,
        data={
            "id": pc_id,
            "name": name,
            "source_branch": source_branch,
            "destination_branch": destination_branch,
            "state": "open",
            "is_draft": is_draft,
            "url": url,
        },
    )


@mcp.tool(
    tags={"branches", "proposed-change", "artifacts", "retrieve"},
    annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True),
)
async def get_proposed_change_artifacts(
    ctx: Context,
    source_branch: Annotated[str, Field(description="Source branch of the proposed change to inspect.")],
) -> MCPResponse[dict[str, Any]]:
    """
    Return all artifacts generated by the Proposed Change for a given branch.

    Artifacts are the rendered device configurations (startup config, BGP policy, etc.)
    produced by Infrahub transforms after the proposed change triggers validators.
    Use this to answer: "What configuration changed on which devices?"

    Returns:
        {
          "proposed_change": {"name", "state"},
          "artifacts": [{"device", "definition", "status", "artifact_id", "storage_id"}],
          "summary": {"total", "by_definition": {...}, "failed": [...]}
        }
    """
    try:
        client: InfrahubClient = require_client(ctx)
    except RuntimeError as exc:
        return await _log_and_return_error(ctx=ctx, error=str(exc), remediation="Start MCP with a configured client.")

    await ctx.info(f"Fetching artifacts for proposed change on branch '{source_branch}'...")

    try:
        result = await client.execute_graphql(
            query=_ARTIFACT_QUERY,
            branch_name="main",
            variables={"branch": source_branch},
        )
    except Exception as exc:  # noqa: BLE001
        return await _log_and_return_error(
            ctx=ctx,
            error=exc,
            remediation="Ensure the proposed change exists and the branch name is correct.",
        )

    pc_edges = result.get("CoreProposedChange", {}).get("edges", [])
    if not pc_edges:
        return await _log_and_return_error(
            ctx=ctx,
            error=f"No proposed change found for source branch '{source_branch}'.",
            remediation="Create one with create_proposed_change() first.",
        )

    pc_node = pc_edges[0]["node"]
    pc_info = {
        "name": pc_node.get("name", {}).get("value", ""),
        "state": pc_node.get("state", {}).get("value", ""),
    }

    artifacts: list[dict[str, Any]] = []
    for val_edge in pc_node.get("validations", {}).get("edges", []):
        val = val_edge["node"]
        if val.get("__typename") != "CoreArtifactValidator":
            continue
        definition = val.get("label", {}).get("value", "unknown")
        for check_edge in val.get("checks", {}).get("edges", []):
            check = check_edge["node"]
            if check.get("__typename") != "CoreArtifactCheck":
                continue
            artifacts.append(
                {
                    "device": check.get("name", {}).get("value", ""),
                    "definition": definition,
                    "status": check.get("conclusion", {}).get("value", "unknown"),
                    "artifact_id": check.get("artifact_id", {}).get("value", ""),
                    "storage_id": check.get("storage_id", {}).get("value", ""),
                }
            )

    by_definition: dict[str, dict[str, int]] = {}
    failed: list[dict[str, Any]] = []
    for art in artifacts:
        defn = art["definition"]
        status = art["status"].lower()
        by_definition.setdefault(defn, {})
        by_definition[defn][status] = by_definition[defn].get(status, 0) + 1
        if status not in ("success", "passed"):
            failed.append(art)

    return MCPResponse(
        status=MCPToolStatus.SUCCESS,
        data={
            "proposed_change": pc_info,
            "artifacts": artifacts,
            "summary": {
                "total": len(artifacts),
                "by_definition": by_definition,
                "failed": failed,
            },
        },
    )


@mcp.tool(
    tags={"branches", "proposed-change", "diff", "retrieve"},
    annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True),
)
async def get_branch_diff(
    ctx: Context,
    branch: Annotated[str, Field(description="Branch to diff against main.")],
) -> MCPResponse[dict[str, Any]]:
    """
    Return a summary of all object changes on a branch compared to main.

    Shows counts per schema kind (added / updated / removed) and lists
    each changed node with its kind, action, and display label.
    Useful for reviewing what will be merged before creating a proposed change.

    Returns:
        {"totals": {...}, "by_kind": {...}, "nodes": [...]}
    """
    try:
        client: InfrahubClient = require_client(ctx)
    except RuntimeError as exc:
        return await _log_and_return_error(ctx=ctx, error=str(exc), remediation="Start MCP with a configured client.")

    await ctx.info(f"Fetching diff tree for branch '{branch}'...")

    try:
        diff_tree = await client.get_diff_tree(branch=branch)
    except Exception as exc:  # noqa: BLE001
        return await _log_and_return_error(
            ctx=ctx,
            error=exc,
            remediation="Ensure the branch exists. A DiffUpdate task may need to run first.",
        )

    if diff_tree is None:
        return MCPResponse(
            status=MCPToolStatus.SUCCESS,
            data={"totals": {}, "by_kind": {}, "nodes": [], "note": "No diff available yet — try again shortly."},
        )

    by_kind: dict[str, dict[str, int]] = {}
    nodes: list[dict[str, Any]] = []
    for node in diff_tree.get("nodes", []):
        kind = node.get("kind", "unknown")
        action = node.get("action", "unknown").lower()
        by_kind.setdefault(kind, {})
        by_kind[kind][action] = by_kind[kind].get(action, 0) + 1
        nodes.append(
            {
                "kind": kind,
                "action": action,
                "label": node.get("label") or node.get("id", ""),
            }
        )

    return MCPResponse(
        status=MCPToolStatus.SUCCESS,
        data={
            "branch": branch,
            "totals": {
                "num_added": diff_tree.get("num_added", 0),
                "num_updated": diff_tree.get("num_updated", 0),
                "num_removed": diff_tree.get("num_removed", 0),
                "num_conflicts": diff_tree.get("num_conflicts", 0),
            },
            "by_kind": by_kind,
            "nodes": nodes,
        },
    )


@mcp.tool(
    tags={"branches", "session", "retrieve"},
    annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True),
)
async def get_session_info(ctx: Context) -> MCPResponse[dict[str, Any]]:
    """
    Return the current session state: active branch, write readiness, and a usage hint.

    Call this at the start of a session or whenever you need to know which branch
    writes will land on. The session branch is lazily created on the first write.

    Returns:
        {"session_branch": str | None, "write_ready": bool, "hint": str}
    """
    branch = await get_session_branch(ctx)
    write_ready = branch is not None

    if branch:
        hint = (
            f"Session branch '{branch}' is active. All writes will target this branch. "
            "Call create_proposed_change() when ready to merge into main."
        )
    else:
        hint = (
            "No session branch created yet. The first call to node_upsert(), node_delete(), "
            "or mutate_graphql() will automatically create one."
        )

    return MCPResponse(
        status=MCPToolStatus.SUCCESS,
        data={"session_branch": branch, "write_ready": write_ready, "hint": hint},
    )


@mcp.tool(
    tags={"branches", "session", "reset"},
    annotations=ToolAnnotations(readOnlyHint=False, idempotentHint=True, destructiveHint=False),
)
async def reset_session(ctx: Context) -> MCPResponse[dict[str, Any]]:
    """
    Clear the current session branch reference.

    This does NOT delete the branch in Infrahub — it only forgets the reference so
    the next write will create a fresh session branch. Use this when you want to
    start a new logical unit of work without losing previous changes.

    Returns:
        {"cleared": bool, "previous_branch": str | None}
    """
    previous = await get_session_branch(ctx)
    await reset_session_branch(ctx)

    await ctx.info(
        f"Session branch reference cleared (was: {previous!r}). Next write will create a new session branch."
    )

    return MCPResponse(
        status=MCPToolStatus.SUCCESS,
        data={"cleared": True, "previous_branch": previous},
    )
