from typing import TYPE_CHECKING, Annotated

from fastmcp import Context, FastMCP
from infrahub_sdk.exceptions import BranchNotFoundError, SchemaNotFoundError
from mcp.types import ToolAnnotations
from pydantic import Field

from franc.constants import NAMESPACES_INTERNAL
from franc.utils import (
    MCPResponse,
    MCPToolStatus,
    _log_and_return_error,
    cache_schema_dict,
    cache_schema_mapping,
    get_cached_schema_dict,
    get_cached_schema_mapping,
    maybe_compress,
    require_client,
)

if TYPE_CHECKING:
    from infrahub_sdk import InfrahubClient

mcp: FastMCP = FastMCP(name="Infrahub Schemas")


@mcp.tool(tags={"schemas", "retrieve"}, annotations=ToolAnnotations(readOnlyHint=True))
async def get_schema_mapping(
    ctx: Context,
    branch: Annotated[
        str | None,
        Field(default=None, description="Branch to retrieve the mapping from. Defaults to None (uses default branch)."),
    ],
) -> MCPResponse:
    """List all schema nodes and generics available in Infrahub

    Parameters:
        branch: Branch to retrieve the mapping from. Defaults to None (uses default branch).

    Returns:
        Dictionary with success status and schema mapping.
    """
    # Check cache first
    cached_mapping = get_cached_schema_mapping(branch)
    if cached_mapping:
        return await maybe_compress(ctx, cached_mapping, "schema mappings", "schema_mapping_toon") or MCPResponse(
            status=MCPToolStatus.SUCCESS,
            data=cached_mapping,
        )

    try:
        client: InfrahubClient = require_client(ctx)
    except RuntimeError as exc:
        return await _log_and_return_error(
            ctx=ctx, error=str(exc), remediation="Start the MCP with a configured client."
        )
    try:
        all_schemas = await client.schema.all(branch=branch)
    except BranchNotFoundError as exc:
        return await _log_and_return_error(ctx=ctx, error=exc, remediation="Check the branch name or your permissions.")

    # TODO: Should we add the description ?
    schema_mapping = {
        kind: node.label or "" for kind, node in all_schemas.items() if node.namespace not in NAMESPACES_INTERNAL
    }

    # Cache the result
    cache_schema_mapping(branch, schema_mapping)

    return await maybe_compress(ctx, schema_mapping, "schema mappings", "schema_mapping_toon") or MCPResponse(
        status=MCPToolStatus.SUCCESS,
        data=schema_mapping,
    )


@mcp.tool(tags={"schemas", "retrieve"}, annotations=ToolAnnotations(readOnlyHint=True))
async def get_schema(
    ctx: Context,
    kind: Annotated[str, Field(description="Schema Kind to retrieve.")],
    branch: Annotated[
        str | None,
        Field(default=None, description="Branch to retrieve the schema from. Defaults to None (uses default branch)."),
    ],
) -> MCPResponse:
    """Retrieve the full schema for a specific kind.
    This includes attributes, relationships, and their types.

    Parameters:
        kind: Schema Kind to retrieve.
        branch: Branch to retrieve the schema from. Defaults to None (uses default branch).

    Returns:
        Dictionary with success status and schema.
    """
    cached = get_cached_schema_dict(branch, kind)
    if cached is not None:
        return MCPResponse(status=MCPToolStatus.SUCCESS, data=cached)

    try:
        client: InfrahubClient = require_client(ctx)
    except RuntimeError as exc:
        return await _log_and_return_error(
            ctx=ctx, error=str(exc), remediation="Start the MCP with a configured client."
        )
    try:
        schema = await client.schema.get(kind=kind, branch=branch)
    except SchemaNotFoundError:
        error_msg = f"Schema not found for kind: {kind}."
        remediation_msg = "Use the `get_schema_mapping` tool to list available kinds."
        return await _log_and_return_error(ctx=ctx, error=error_msg, remediation=remediation_msg)
    except BranchNotFoundError as exc:
        return await _log_and_return_error(ctx=ctx, error=exc, remediation="Check the branch name or your permissions.")

    schema_dict = schema.model_dump()
    cache_schema_dict(branch, kind, schema_dict)

    return MCPResponse(status=MCPToolStatus.SUCCESS, data=schema_dict)


@mcp.tool(tags={"schemas", "retrieve"}, annotations=ToolAnnotations(readOnlyHint=True))
async def get_schemas(
    ctx: Context,
    branch: Annotated[
        str | None,
        Field(default=None, description="Branch to retrieve schemas from. Defaults to None (uses default branch)."),
    ],
    exclude_profiles: Annotated[
        bool, Field(default=True, description="Whether to exclude Profile schemas. Defaults to True.")
    ],
    exclude_templates: Annotated[
        bool, Field(default=True, description="Whether to exclude Template schemas. Defaults to True.")
    ],
) -> MCPResponse:
    """Retrieve all schemas from Infrahub, optionally excluding Profiles and Templates.

    Parameters:
        infrahub_client: Infrahub client to use
        branch: Branch to retrieve schemas from
        exclude_profiles: Whether to exclude Profile schemas. Defaults to True.
        exclude_templates: Whether to exclude Template schemas. Defaults to True.

    Returns:
        Dictionary with success status and schemas.

    """
    try:
        client: InfrahubClient = require_client(ctx)
    except RuntimeError as exc:
        return await _log_and_return_error(
            ctx=ctx, error=str(exc), remediation="Start the MCP with a configured client."
        )
    try:
        all_schemas = await client.schema.all(branch=branch)
    except BranchNotFoundError as exc:
        return await _log_and_return_error(ctx=ctx, error=exc, remediation="Check the branch name or your permissions.")

    # Filter out Profile and Template if requested
    filtered_schemas = {}
    for kind, schema in all_schemas.items():
        if (exclude_templates and schema.namespace == "Template") or (
            exclude_profiles and schema.namespace == "Profile"
        ):
            continue
        filtered_schemas[kind] = schema.model_dump()

    return await maybe_compress(ctx, filtered_schemas, "full schemas", "schemas_toon") or MCPResponse(
        status=MCPToolStatus.SUCCESS,
        data=filtered_schemas,
    )


@mcp.tool(tags={"schemas", "required"}, annotations=ToolAnnotations(readOnlyHint=True))
async def get_required_fields(ctx: Context, kind: str) -> MCPResponse[list[str]]:
    """
    List all required attribute fields for a given kind in Infrahub, based on the schema definition.

    Args:
        kind (str): The object kind (schema node name).

    Returns:
        list[str]: List of required attribute field names (strings).

    Example:
        ["name", "serial_number"]

    Notes:
        - Only attributes marked as required in the schema are included.
        - Does not include required relationships by default.
        - Handles both dict and object-based schemas for compatibility with test mocks and real Infrahub schemas.
        - For LLMs: Use this to determine which fields must be provided when creating objects of this kind.
    """
    try:
        client: InfrahubClient = require_client(ctx)
    except RuntimeError as exc:
        return await _log_and_return_error(
            ctx=ctx, error=str(exc), remediation="Start the MCP with a configured client."
        )
    schema = await client.schema.get(kind=kind)
    attrs = getattr(schema, "attributes", []) or []
    rels = getattr(schema, "relationships", []) or []
    required_fields = [attr.name for attr in attrs if not getattr(attr, "optional", True)]
    required_fields += [rel.name for rel in rels if not getattr(rel, "optional", True)]
    return MCPResponse(status=MCPToolStatus.SUCCESS, data=required_fields)

