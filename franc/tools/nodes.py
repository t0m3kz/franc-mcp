from typing import TYPE_CHECKING, Annotated, Any

from fastmcp import Context, FastMCP
from infrahub_sdk.exceptions import GraphQLError, SchemaNotFoundError
from infrahub_sdk.types import Order
from mcp.types import ToolAnnotations
from pydantic import Field

from franc.constants import schema_attribute_type_mapping
from franc.utils import (
    MCPResponse,
    MCPToolStatus,
    _log_and_return_error,
    cache_schema,
    convert_node_to_dict,
    extract_value,
    get_cached_schema,
    maybe_compress,
    require_client,
)

if TYPE_CHECKING:
    from infrahub_sdk.client import InfrahubClient

mcp: FastMCP = FastMCP(name="Infrahub Nodes")


@mcp.tool(tags={"nodes", "retrieve"}, annotations=ToolAnnotations(readOnlyHint=True))
async def get_nodes(
    ctx: Context,
    kind: Annotated[str, Field(description="Kind of the objects to retrieve.")],
    branch: Annotated[
        str | None,
        Field(default=None, description="Branch to retrieve the objects from. Defaults to None (uses default branch)."),
    ],
    filters: Annotated[dict[str, Any] | None, Field(default=None, description="Dictionary of filters to apply.")],
    partial_match: Annotated[bool, Field(default=False, description="Whether to use partial matching for filters.")],
) -> MCPResponse:
    """Get all objects of a specific kind from Infrahub.

    To retrieve the list of available kinds, use the `get_schema_mapping` tool.
    To retrieve the list of available filters for a specific kind, use the `get_node_filters` tool.

    Parameters:
        kind: Kind of the objects to retrieve.
        branch: Branch to retrieve the objects from. Defaults to None (uses default branch).
        filters: Dictionary of filters to apply.
        partial_match: Whether to use partial matching for filters.

    Returns:
        MCPResponse with success status and objects.

    """
    try:
        client: InfrahubClient = require_client(ctx)
    except RuntimeError as exc:
        return await _log_and_return_error(
            ctx=ctx, error=str(exc), remediation="Start the MCP with a configured client."
        )
    branch = branch or "main"

    # Verify if the kind exists in the schema and guide Tool if not
    schema = get_cached_schema(branch, kind)
    if schema is None:
        try:
            schema = await client.schema.get(kind=kind, branch=branch)
            cache_schema(branch, kind, schema)
        except SchemaNotFoundError:
            return await _log_and_return_error(
                ctx=ctx,
                error=f"Schema not found for kind: {kind}.",
                remediation="Use the `get_schema_mapping` tool to list available kinds.",
            )

    # TODO: Verify if the filters are valid for the kind and guide Tool if not

    try:
        if filters:
            await ctx.debug(f"Applying filters: {filters} with partial_match={partial_match}")
            try:
                nodes = await client.filters(
                    kind=schema.kind,
                    branch=branch,
                    partial_match=partial_match,
                    parallel=True,
                    order=Order(disable=True),
                    populate_store=True,
                    prefetch_relationships=True,
                    **filters,
                )
            except SchemaNotFoundError:
                # Retry without prefetch if peer schemas are missing
                await ctx.debug("Peer schema missing; retrying without prefetch_relationships")
                nodes = await client.filters(
                    kind=schema.kind,
                    branch=branch,
                    partial_match=partial_match,
                    parallel=True,
                    order=Order(disable=True),
                    populate_store=True,
                    prefetch_relationships=False,
                    **filters,
                )
        else:
            try:
                nodes = await client.all(
                    kind=schema.kind,
                    branch=branch,
                    parallel=True,
                    order=Order(disable=True),
                    populate_store=True,
                    prefetch_relationships=True,
                )
            except SchemaNotFoundError:
                # Retry without prefetch if peer schemas are missing
                await ctx.debug("Peer schema missing; retrying without prefetch_relationships")
                nodes = await client.all(
                    kind=schema.kind,
                    branch=branch,
                    parallel=True,
                    order=Order(disable=True),
                    populate_store=True,
                    prefetch_relationships=False,
                )
    except GraphQLError as exc:
        # Smart remediation: suggest checking filters or schema
        remediation = (
            "Check the provided filters or the kind name. "
            f"Use get_node_filters(kind='{kind}') to see available filter keys. "
            f"Use get_schema(kind='{kind}') to inspect the schema."
        )
        return await _log_and_return_error(ctx=ctx, error=exc, remediation=remediation)

    # Smart remediation: if no results, suggest alternatives
    if not nodes and filters:
        # Try to suggest what to do
        remediation = (
            f"No objects found for kind={kind} with filters={filters}. "
            f"Try: (1) Remove filters to see if any {kind} objects exist, "
            f"(2) Use get_node_filters(kind='{kind}') to verify filter keys are correct, "
            f"(3) Use partial_match=True for text filters."
        )
        await ctx.info(remediation)
    elif not nodes:
        await ctx.info(
            f"No {kind} objects exist in branch '{branch}'. This may be expected if the kind is newly added."
        )

    # Format the response with serializable data
    # serialized_nodes = []
    # for node in nodes:
    #     node_data = await convert_node_to_dict(obj=node, branch=branch)
    #     serialized_nodes.append(node_data)
    serialized_nodes = [obj.display_label for obj in nodes]

    return await maybe_compress(ctx, serialized_nodes, "nodes", "nodes_toon") or MCPResponse(
        status=MCPToolStatus.SUCCESS,
        data=serialized_nodes,
    )


@mcp.tool(tags={"nodes", "filters", "retrieve"}, annotations=ToolAnnotations(readOnlyHint=True))
async def get_node_filters(
    ctx: Context,
    kind: Annotated[str, Field(description="Kind of the objects to retrieve.")],
    branch: Annotated[
        str | None,
        Field(default=None, description="Branch to retrieve the objects from. Defaults to None (uses default branch)."),
    ],
) -> MCPResponse:
    """Retrieve all the available filters for a specific schema node kind.

    Filter types returned:
      - attribute__value        -> Filter by single attribute value
      - attribute__values       -> Filter by list of attribute values (OR condition)
      - relationship__attribute__value    -> Filter by related object's attribute
      - relationship__attribute__values   -> Filter by related object's attribute list

    Examples:
      - {"name__value": "device-01"} - Get objects where name equals "device-01"
      - {"status__values": ["active", "planned"]} - Get objects with status active OR planned
      - {"location__name__value": "Paris"} - Get objects whose location's name is "Paris"

    Note: Relationship filters allow filtering on related objects without fetching them.
    """
    try:
        client: InfrahubClient = require_client(ctx)
    except RuntimeError as exc:
        return await _log_and_return_error(
            ctx=ctx, error=str(exc), remediation="Start the MCP with a configured client."
        )
    import asyncio

    branch = branch or "main"

    schema = get_cached_schema(branch, kind)
    if schema is None:
        try:
            schema = await client.schema.get(kind=kind, branch=branch)
            cache_schema(branch, kind, schema)
        except SchemaNotFoundError:
            return await _log_and_return_error(
                ctx=ctx,
                error=f"Schema not found for kind: {kind}.",
                remediation="Use the `get_schema_mapping` tool to list available kinds.",
            )

    filters: dict[str, str] = {}
    for attribute in getattr(schema, "attributes", []):
        type_name = schema_attribute_type_mapping.get(attribute.kind, "String")
        filters[f"{attribute.name}__value"] = type_name
        filters[f"{attribute.name}__values"] = f"List[{type_name}]"

    relationships = getattr(schema, "relationships", [])

    async def _fetch_peer_schema(rel):
        cached = get_cached_schema(branch, rel.peer)
        if cached is not None:
            return rel, cached
        try:
            peer_schema = await client.schema.get(kind=rel.peer, branch=branch)
            cache_schema(branch, rel.peer, peer_schema)
            return rel, peer_schema
        except SchemaNotFoundError:
            await ctx.debug(f"Skipping relationship '{rel.name}' peer '{rel.peer}' (schema missing).")
            return rel, None

    peer_results = await asyncio.gather(*[_fetch_peer_schema(rel) for rel in relationships])

    for rel, peer_schema in peer_results:
        if peer_schema is None:
            continue
        for attribute in getattr(peer_schema, "attributes", []):
            type_name = schema_attribute_type_mapping.get(attribute.kind, "String")
            filters[f"{rel.name}__{attribute.name}__value"] = type_name
            filters[f"{rel.name}__{attribute.name}__values"] = f"List[{type_name}]"

    return MCPResponse(status=MCPToolStatus.SUCCESS, data=filters)


@mcp.tool(tags={"nodes", "retrieve"}, annotations=ToolAnnotations(readOnlyHint=True))
async def get_related_nodes(
    ctx: Context,
    kind: Annotated[str, Field(description="Kind of the objects to retrieve.")],
    relation: Annotated[str, Field(description="Name of the relation to fetch.")],
    filters: Annotated[dict[str, Any] | None, Field(default=None, description="Dictionary of filters to apply.")],
    branch: Annotated[
        str | None,
        Field(default=None, description="Branch to retrieve the objects from. Defaults to None (uses default branch)."),
    ],
) -> MCPResponse:
    """Retrieve related nodes by relation name and a kind.

    Args:
        kind: Kind of the node to fetch.
        filters: Filters to apply on the node to fetch.
        relation: Name of the relation to fetch.
        branch: Branch to fetch the node from. Defaults to None (uses default branch).

    Returns:
        MCPResponse with success status and objects.

    """
    try:
        client: InfrahubClient = require_client(ctx)
    except RuntimeError as exc:
        return await _log_and_return_error(
            ctx=ctx, error=str(exc), remediation="Start the MCP with a configured client."
        )
    filters = filters or {}

    try:
        node_id = node_hfid = None
        if filters.get("ids"):
            node_id = filters["ids"][0]
        elif filters.get("hfid"):
            node_hfid = filters["hfid"]
        if node_id:
            node = await client.get(
                kind=kind,
                id=node_id,
                branch=branch,
                include=[relation],
                prefetch_relationships=True,
                populate_store=True,
            )
        elif node_hfid:
            node = await client.get(
                kind=kind,
                hfid=node_hfid,
                branch=branch,
                include=[relation],
                prefetch_relationships=True,
                populate_store=True,
            )
    except Exception as exc:  # noqa: BLE001
        return await _log_and_return_error(ctx=ctx, error=exc)

    rel = getattr(node, relation, None)
    if not rel:
        return await _log_and_return_error(
            ctx=ctx,
            error=f"Relation '{relation}' not found in kind '{kind}'.",
            remediation="Check the schema for the kind to confirm if the relation exists.",
        )
    peers = [
        await convert_node_to_dict(
            branch=branch,
            obj=peer.peer,
            include_id=True,
        )
        for peer in rel.peers
    ]

    return MCPResponse(
        status=MCPToolStatus.SUCCESS,
        data=peers,
    )



@mcp.tool(tags={"nodes", "retrieve"}, annotations=ToolAnnotations(readOnlyHint=True))
async def get_object_details(
    ctx: Context,
    kind: Annotated[str, Field(description="Kind of the object to retrieve.")],
    filters: Annotated[dict[str, Any], Field(description="Attribute filters to identify the object.")],
    branch: Annotated[str | None, Field(default=None, description="Branch scope (optional).")],
) -> MCPResponse[dict[str, Any]]:
    """
    Return a flattened dictionary of a single object's attributes & relationships.

    Returns:
      - All attributes with their values
      - All relationships with display_labels from related objects
      - Single relationships: display_label string
      - Multiple relationships: list of display_label strings
    """
    try:
        client: InfrahubClient = require_client(ctx)
    except RuntimeError as exc:
        return await _log_and_return_error(
            ctx=ctx, error=str(exc), remediation="Start the MCP with a configured client."
        )

    branch = branch or "main"

    schema = get_cached_schema(branch, kind)
    if schema is None:
        try:
            schema = await client.schema.get(kind=kind, branch=branch)
            cache_schema(branch, kind, schema)
        except SchemaNotFoundError:
            return await _log_and_return_error(
                ctx=ctx,
                error=f"Schema not found for kind: {kind}.",
                remediation="Use get_schema_mapping to list available kinds.",
            )

    # Build list of relationships to include - try to include all, skip missing peer schemas
    rel_many: list[str] = []
    for r in getattr(schema, "relationships", []):
        peer_schema = get_cached_schema(branch, r.peer)
        if peer_schema is None:
            try:
                peer_schema = await client.schema.get(kind=r.peer, branch=branch)
                cache_schema(branch, r.peer, peer_schema)
            except SchemaNotFoundError:
                await ctx.debug(f"Skipping include for '{r.name}' (peer schema '{r.peer}' missing).")
                continue
        if getattr(r, "cardinality", "") == "many":
            rel_many.append(r.name)

    try:
        # For .get(), we need to handle the filter differently
        # If we have name__value, convert to just the field name for .filters()
        # Then take the first result
        if len(filters) == 1 and next(iter(filters.keys())).endswith("__value"):
            # Use .filters() instead of .get() for GraphQL-style filters
            filter_key = next(iter(filters.keys()))
            objs = await client.filters(
                kind=schema.kind,
                branch=branch,
                include=rel_many,
                prefetch_relationships=False,  # Avoid schema errors
                populate_store=True,
                parallel=False,
                **{filter_key: filters[filter_key]},
            )
            obj = objs[0] if objs else None
        else:
            # Use direct .get() for simple filters
            obj = await client.get(
                kind=schema.kind,
                branch=branch,
                include=rel_many,
                prefetch_relationships=False,  # Avoid schema errors
                populate_store=True,
                **filters,
            )
    except Exception as exc:  # noqa: BLE001
        return await _log_and_return_error(ctx=ctx, error=exc, remediation="Object retrieval failed.")

    if not obj:
        return await _log_and_return_error(
            ctx=ctx,
            error=f"No object found for kind={kind} with filters={filters}.",
            remediation="Verify filter keys and values using get_node_filters.",
        )

    # Build result with all attributes and relationships
    result = {}

    # Add all attributes
    for attr_name in schema.attribute_names:
        attr = getattr(obj, attr_name, None)
        if attr is not None:
            result[attr_name] = attr.value if hasattr(attr, "value") else str(attr)

    # Add all relationships with display_labels
    for rel_name in schema.relationship_names:
        rel = getattr(obj, rel_name, None)
        if rel is None:
            result[rel_name] = None
            continue

        # Handle single relationships
        if hasattr(rel, "peer") and rel.peer:
            peer = rel.peer
            result[rel_name] = getattr(peer, "display_label", str(peer))
        # Handle multiple relationships
        elif hasattr(rel, "peers"):
            result[rel_name] = [
                getattr(p.peer, "display_label", str(p.peer)) if hasattr(p, "peer") else str(p) for p in rel.peers
            ]
        else:
            result[rel_name] = str(rel)

    return MCPResponse(status=MCPToolStatus.SUCCESS, data=result)


@mcp.tool(tags={"nodes", "retrieve", "batch"}, annotations=ToolAnnotations(readOnlyHint=True))
async def get_objects_details(
    ctx: Context,
    kind: Annotated[str, Field(description="Kind of the objects to retrieve.")],
    filters: Annotated[dict[str, Any] | None, Field(default=None, description="Filters to narrow down objects.")],
    branch: Annotated[str | None, Field(default=None, description="Branch scope (optional).")],
    limit: Annotated[int | None, Field(default=100, description="Maximum number of objects to fetch (default 100).")],
    fields: Annotated[
        list[str] | None,
        Field(
            default=None,
            description="Specific fields to retrieve (attributes/relationships). If None, fetches all fields.",
        ),
    ],
) -> MCPResponse:
    """
    Fetch multiple objects with their full attributes and relationships in one query.

    This is more efficient than calling get_object_details multiple times.
    Perfect for requests like "list all devices with their IP addresses".

    Args:
        kind: Kind of the objects to retrieve
        filters: Optional filters to apply (e.g., {"status__value": "active"})
        branch: Branch to query (defaults to main)
        limit: Maximum number of objects to return (default 100)
        fields: Specific fields to retrieve. Examples:
                ["name", "primary_address"] - only these fields
                None (default) - all fields

    Returns:
        MCPResponse with list of flattened object dictionaries
    """
    try:
        client: InfrahubClient = require_client(ctx)
    except RuntimeError as exc:
        return await _log_and_return_error(
            ctx=ctx, error=str(exc), remediation="Start the MCP with a configured client."
        )

    branch = branch or "main"
    filters = filters or {}

    schema = get_cached_schema(branch, kind)
    if schema is None:
        try:
            schema = await client.schema.get(kind=kind, branch=branch)
            cache_schema(branch, kind, schema)
        except SchemaNotFoundError:
            return await _log_and_return_error(
                ctx=ctx,
                error=f"Schema not found for kind: {kind}.",
                remediation="Use get_schema_mapping to list available kinds.",
            )

    # Build includes for many-cardinality relationships
    # Filter by requested fields if specified
    rel_many: list[str] = []
    for r in getattr(schema, "relationships", []):
        if fields is not None and r.name not in fields:
            continue
        if getattr(r, "cardinality", "") != "many":
            continue
        peer_schema = get_cached_schema(branch, r.peer)
        if peer_schema is None:
            try:
                peer_schema = await client.schema.get(kind=r.peer, branch=branch)
                cache_schema(branch, r.peer, peer_schema)
            except SchemaNotFoundError:
                await ctx.debug(f"Skipping include for '{r.name}' (peer schema '{r.peer}' missing).")
                continue
        rel_many.append(r.name)

    try:
        objects = await client.all(
            kind=schema.kind,
            branch=branch,
            include=rel_many,
            prefetch_relationships=True,
            populate_store=True,
            limit=limit,
            **filters,
        )
    except Exception as exc:  # noqa: BLE001
        return await _log_and_return_error(ctx=ctx, error=exc, remediation="Batch object retrieval failed.")

    if not objects:
        # Smart remediation with specific suggestions
        remediation = (
            f"No objects found for kind={kind} with filters={filters}. "
            f"Suggestions: "
            f"(1) Try get_nodes(kind='{kind}') to see if ANY objects exist. "
            f"(2) Use get_node_filters(kind='{kind}') to verify filter syntax. "
        )
        if filters:
            remediation += f"(3) Remove filters to see all {kind} objects. "
            # Check if using relationship filters
            rel_filters = [k for k in filters if "__" in k]
            if rel_filters:
                remediation += f"(4) Relationship filters detected: {rel_filters} - verify related objects exist."

        return await _log_and_return_error(
            ctx=ctx,
            error=f"No objects found for kind={kind} with filters={filters}.",
            remediation=remediation,
        )

    # Flatten all objects, filtering by requested fields
    flattened_objects = []
    for obj in objects:
        raw = obj.get_raw_graphql_data() or {}
        flattened = {k: extract_value(v) for k, v in raw.items()}

        # Filter to requested fields if specified
        if fields:
            flattened = {
                k: v for k, v in flattened.items() if k in fields or k in ["id", "display_label", "__typename"]
            }

        flattened_objects.append(flattened)

    return await maybe_compress(ctx, flattened_objects, f"{kind} objects", "objects_details_toon") or MCPResponse(
        status=MCPToolStatus.SUCCESS,
        data=flattened_objects,
    )
