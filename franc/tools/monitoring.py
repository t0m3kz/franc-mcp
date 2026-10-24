"""
Path monitoring inventory tool — SDK traverse_paths + Infrahub GraphQL join.

Finds all ECMP-unique interfaces on a path and enriches them with
branch-aware status, role, and IP metadata from Infrahub GraphQL.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any

from fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from franc.constants import GRAPH_CONNECTOR_TYPES, NEO4J_DEFAULT_MAX_HOPS
from franc.flow_helpers import _IFACE_STATUS_LABEL, _PATH_INVENTORY_GQL
from franc.tools.graph import _name_to_uuid, _sdk_rel_filters
from franc.utils import MCPResponse, MCPToolStatus, _log_and_return_error, require_client

if TYPE_CHECKING:
    from infrahub_sdk import InfrahubClient

mcp: FastMCP = FastMCP(name="Infrahub Path Monitoring")

_KIND_TO_CTYPE = {
    "dcimcable": "cable",
    "topologyphysicalcircuit": "physical_circuit",
    "topologyvirtualcircuit": "virtual_circuit",
}


@mcp.tool(
    tags={"flow", "monitoring", "inventory", "path"},
    annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True),
)
async def get_path_monitoring_inventory(
    ctx: Context,
    src: Annotated[str, Field(description="Source device name.")],
    dst: Annotated[str, Field(description="Destination device name.")],
    branch: Annotated[str, Field(default="main", description="Infrahub branch to query for interface metadata.")],
    max_hops: Annotated[int, Field(default=NEO4J_DEFAULT_MAX_HOPS, description="BFS depth limit.")],
    connector_types: Annotated[
        list[str] | None,
        Field(default=None, description="Connector types to follow. Default: all."),
    ],
) -> MCPResponse[dict[str, Any]]:
    """
    Build the exclusive monitoring inventory for all devices and interfaces on a path.

    Two-phase approach:
      1. SDK traverse_paths to find all ECMP shortest paths between src and dst.
         Deduplicates across all paths → unique set of (device, interface) pairs.
      2. Queries Infrahub GraphQL (branch-aware) for live status, role, IP address,
         and enabled flag on exactly those interfaces.

    This solves the problem of 549,168 raw BFS paths between two TORs — instead of
    dumping every permutation, you get the exact flat list of interfaces that monitoring
    must watch to cover the full ECMP fabric.

    Returns:
      {
        "src", "dst", "branch",
        "path_summary": {"shortest_hops", "ecmp_paths", "unique_devices", "unique_interfaces"},
        "monitoring_inventory": [{"device", "device_role", "interface", "iface_role",
                                   "connector_type", "status", "health", "enabled",
                                   "ip_address", "description"}, ...],
        "broken": [...],
        "warnings": [...]
      }
    """
    connectors = connector_types or GRAPH_CONNECTOR_TYPES
    client: InfrahubClient = require_client(ctx)
    await ctx.info(f"Resolving {src} -> {dst} (branch={branch})...")

    src_uuid = await _name_to_uuid(client, src, branch)
    dst_uuid = await _name_to_uuid(client, dst, branch)
    if not src_uuid or not dst_uuid:
        return await _log_and_return_error(
            ctx=ctx,
            error=f"Device '{src if not src_uuid else dst}' not found.",
            remediation="Verify device names with get_topology_devices().",
        )

    unique_ifaces: dict[tuple[str, str], str] = {}
    path_count = 0
    shortest_hops = 0

    try:
        result = await client.traverse_paths(
            src_uuid,
            dst_uuid,
            relationship_filter=_sdk_rel_filters(connectors),
            max_depth=max_hops,
            max_paths=None,
            branch=branch,
        )
        paths = result.paths
        path_count = len(paths)
        if not paths:
            return await _log_and_return_error(
                ctx=ctx,
                error=f"No path found between '{src}' and '{dst}' within {max_hops} hops.",
                remediation="Verify device names with get_device_neighbors(). Check connector_types.",
            )
        shortest_hops = min(len(p.hops) for p in paths)
        for path in paths:
            hops = path.hops
            if len(hops) < 5:
                continue
            connector_kind = (hops[2].node.kind or "").lower()
            ctype = next((v for k, v in _KIND_TO_CTYPE.items() if k in connector_kind), "cable")
            src_dev = hops[0].node.display_label
            src_iface = hops[1].node.display_label
            dst_iface = hops[3].node.display_label
            dst_dev = hops[4].node.display_label
            unique_ifaces[(src_dev, src_iface)] = ctype
            unique_ifaces[(dst_dev, dst_iface)] = ctype
    except Exception as exc:  # noqa: BLE001
        return await _log_and_return_error(ctx=ctx, error=str(exc), remediation="traverse_paths failed.")

    unique_devices = sorted({dev for dev, _ in unique_ifaces})
    await ctx.info(
        f"  {path_count} ECMP path(s) x {shortest_hops} hops -> "
        f"{len(unique_devices)} devices, {len(unique_ifaces)} unique interfaces"
    )

    try:
        client: InfrahubClient = require_client(ctx)
    except RuntimeError as exc:
        return await _log_and_return_error(ctx=ctx, error=str(exc), remediation="Start MCP with a configured client.")

    try:
        result = await client.execute_graphql(
            query=_PATH_INVENTORY_GQL,
            branch_name=branch,
            variables={"device_names": unique_devices},
        )
    except Exception as exc:  # noqa: BLE001
        return await _log_and_return_error(
            ctx=ctx,
            error=str(exc),
            remediation=f"GraphQL query failed on branch '{branch}'. Check branch name.",
        )

    gql_iface_meta: dict[tuple[str, str], dict[str, Any]] = {}
    for dev_edge in result.get("DcimDevice", {}).get("edges", []):
        dev_node = dev_edge["node"]
        dev_name: str = dev_node.get("name", {}).get("value", "")
        dev_role: str = dev_node.get("role", {}).get("value", "")
        for iface_edge in dev_node.get("interfaces", {}).get("edges", []):
            iface = iface_edge["node"]
            iface_name: str = iface.get("name", {}).get("value", "")
            ip_node = (iface.get("ip_address") or {}).get("node") or {}
            gql_iface_meta[(dev_name, iface_name)] = {
                "device_role": dev_role,
                "iface_role": (iface.get("role") or {}).get("value", ""),
                "status": (iface.get("status") or {}).get("value", "unknown"),
                "enabled": (iface.get("enabled") or {}).get("value"),
                "ip_address": ip_node.get("address", {}).get("value"),
                "description": (iface.get("description") or {}).get("value", ""),
                "iface_type": (iface.get("interface_type") or {}).get("value", ""),
            }

    monitoring_inventory: list[dict[str, Any]] = []
    for (dev, iface), connector in sorted(unique_ifaces.items()):
        meta = gql_iface_meta.get((dev, iface), {})
        status = meta.get("status", "unknown")
        monitoring_inventory.append(
            {
                "device": dev,
                "device_role": meta.get("device_role", ""),
                "interface": iface,
                "iface_role": meta.get("iface_role", ""),
                "connector_type": connector,
                "status": status,
                "health": _IFACE_STATUS_LABEL.get(status, "unknown"),
                "enabled": meta.get("enabled"),
                "ip_address": meta.get("ip_address"),
                "description": meta.get("description", ""),
            }
        )

    broken = [i for i in monitoring_inventory if i["health"] == "broken"]
    warnings = [i for i in monitoring_inventory if i["health"] == "warning"]

    if broken:
        await ctx.warning(
            f"  {len(broken)} BROKEN interface(s): "
            + ", ".join(f"{i['device']}/{i['interface']}({i['status']})" for i in broken[:5])
        )

    return MCPResponse(
        status=MCPToolStatus.SUCCESS,
        data={
            "src": src,
            "dst": dst,
            "branch": branch,
            "path_summary": {
                "shortest_hops": shortest_hops,
                "ecmp_paths": path_count,
                "unique_devices": len(unique_devices),
                "unique_interfaces": len(unique_ifaces),
            },
            "monitoring_inventory": monitoring_inventory,
            "broken": broken,
            "warnings": warnings,
        },
    )
