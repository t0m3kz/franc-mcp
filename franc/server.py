from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator

from fastmcp import Context, FastMCP
from infrahub_sdk import InfrahubClient


@dataclass
class ApplicationContext:
    client: InfrahubClient


@asynccontextmanager
async def app_lifespan(server: FastMCP) -> AsyncIterator[ApplicationContext]:
    # Allow dependency injection for tests: if server has 'test_client', use it
    client = getattr(server, "test_client", None)
    if client is None:
        client = InfrahubClient(address="http://localhost:8000")
    try:
        yield ApplicationContext(client=client)
    finally:
        pass


mcp = FastMCP("Franc MCP", lifespan=app_lifespan)


def attr_type(attr):
    """
    Get the type of an attribute based on its kind."""
    # You can expand this mapping as needed
    kind_map = {
        "Text": "String",
        "String": "String",
        "Integer": "Int",
        "Float": "Float",
        "Boolean": "Boolean",
        "DateTime": "DateTime",
    }
    kind = getattr(attr, "kind", None)
    return kind_map.get(str(kind), "String") if kind is not None else "String"


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


@mcp.tool
async def get_object_details(ctx: Context, kind: str, filters: dict) -> dict:
    """
    Get all attribute values and relationship display labels for a single object.

    Args:
        ctx (Context): FastMCP context (injected).
        kind (str): Object kind (schema node name).
        filters (dict): Filter(s) to identify the object (e.g., {"id": ...} or attribute filters).

    Returns:
        dict: Dictionary with all attribute values and relationship display labels. For many relationships, returns a list of display labels.

    Example:
        {
            "name": "Router1",
            "location": "Building A",
            "tags": ["Core", "Edge"]
        }

    Notes:
        - Attribute values are returned as plain values.
        - Relationships of cardinality one return the display_label (or None).
        - Relationships of cardinality many return a list of display labels (or empty list).
        - Returns None for missing attributes/relationships.
        - Handles both dict and object-based schemas for compatibility with test mocks and real Infrahub schemas.
        - For LLMs: Use this to get all fields for a single object. Always check for None values. Field names are schema-driven.
    """
    client: InfrahubClient = ctx.request_context.lifespan_context.client
    schema = await client.schema.get(kind=kind)
    rels = getattr(schema, "relationships", [])
    rel_many = [r.name for r in rels if getattr(r, "cardinality", None) == "many"]
    obj = await client.get(kind=kind, include=rel_many, **filters)
    if not obj:
        return {}
    raw_data = obj.get_raw_graphql_data() or {}
    return {k: extract_value(v) for k, v in raw_data.items()}


@mcp.tool
async def get_required_fields(ctx: Context, kind: str) -> list[str]:
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
    client: InfrahubClient = ctx.request_context.lifespan_context.client
    schema = await client.schema.get(kind=kind)
    attrs = getattr(schema, "attributes", []) or []
    rels = getattr(schema, "relationships", []) or []
    required_fields = [attr.name for attr in attrs if not getattr(attr, "optional", True)]
    required_fields += [rel.name for rel in rels if not getattr(rel, "optional", True)]
    return required_fields


@mcp.tool
async def get_objects(ctx: Context, kind: str, filters: dict | None = None) -> list[str]:
    """
    List all objects of a specific kind in Infrahub, optionally filtered by attribute values.

    Args:
        kind (str): The object kind (schema node name).
        filters (dict, optional): Dictionary of filter criteria (see get_node_filters for available filters).

    Returns:
        list[str]: List of display labels for all matching objects.

    Example:
        ["Router1", "Router2", "Switch1"]

    Notes:
        - Use list_schema_nodes to get available kinds.
        - Use get_node_filters to get available filters for a kind.
        - Filters should match the filter keys returned by get_node_filters.
        - Returns only display labels, not full objects. Use get_object_details for full data.
        - For LLMs: If you need to enumerate or select objects, use this tool before requesting details.
    """
    client: InfrahubClient = ctx.request_context.lifespan_context.client
    if filters:
        objects = await client.filters(kind=kind, **filters)
    else:
        objects = await client.all(kind=kind)
    return [str(obj.display_label) for obj in objects if getattr(obj, "display_label", None) is not None]


@mcp.tool
async def get_node_filters(ctx: Context, kind: str) -> dict[str, Any]:
    """
    Get all available filters for a specific kind in Infrahub, including attributes and relationships.

    Args:
        kind (str): The object kind (schema node name).

    Returns:
        dict[str, Any]: Dictionary mapping filter names to their types or descriptions.

    Example:
        {
            "name__value": "String",
            "status__values": "List[String]",
            "parent__name__value": "String"
        }

    Notes:
        - For each attribute, generates filters like attribute__value, attribute__values, etc.
        - For each relationship, generates the same filters, prefixed with the relationship name.
        - Useful for constructing filter queries for get_objects.
        - Filter types are string representations; LLMs should use these to validate or suggest filter values.
        - For LLMs: Use this tool to dynamically discover valid filter keys and types for a given kind.
    """
    client: InfrahubClient = ctx.request_context.lifespan_context.client
    schema = await client.schema.get(kind=kind)
    filters = {}

    # Attribute filters
    for attribute in getattr(schema, "attributes", []):
        t = attr_type(attribute)
        filters[f"{attribute.name}__value"] = t
        filters[f"{attribute.name}__values"] = f"List[{t}]"

    # Relationship filters
    for rel in getattr(schema, "relationships", []):
        rel_name = rel.name
        peer_kind = getattr(rel, "peer", None)
        if peer_kind:
            try:
                peer_schema = await client.schema.get(kind=peer_kind)
                for attr in getattr(peer_schema, "attributes", []):
                    t = attr_type(attr)
                    filters[f"{rel_name}__{attr.name}__value"] = t
                    filters[f"{rel_name}__{attr.name}__values"] = f"List[{t}]"
            except Exception:
                pass  # If peer schema can't be loaded, skip

    return filters


@mcp.tool
async def list_schema_nodes(ctx: Context) -> dict[str, str]:
    """
    List all available schema nodes (object kinds) in Infrahub.

    Returns:
        dict[str, str]: Mapping of kind name to display label.

        Example:
            {
                "LocationBuilding": "Building",
                "DcimPhysicalDevice": "Physical Device",
                "IpamIPPrefix": "IP Prefix"
            }
    Notes:
        - Use this tool to discover all available object kinds before prompting for kind-specific actions.
    """
    client: InfrahubClient = ctx.request_context.lifespan_context.client
    schema = await client.schema.all()
    return {
        str(kind): str(node.label)
        for kind, node in schema.items()
        if isinstance(kind, str) and hasattr(node, "label") and node.label is not None
    }
