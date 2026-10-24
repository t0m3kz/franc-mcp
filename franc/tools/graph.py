"""
Graph traversal tools — path finding, segment trace, raw Cypher.

All path traversal uses InfrahubClient.traverse_paths() (SDK ≥ 1.10).
get_device_neighbors uses GraphQL queries.
execute_cypher is kept as an intentional raw-query escape hatch.
"""

from __future__ import annotations

from typing import Annotated, Any

import httpx
from fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from franc.constants import (
    GRAPH_CONNECTOR_TYPES,
    KIND_DEVICE,
    NEO4J_DEFAULT_MAX_HOPS,
)
from franc.traversal import (
    NodeResolutionError,
    _neo4j_credentials,
    _neo4j_run,
    cypher_write_violation,
    is_uuid,
    resolve_node_ref,
    shape_reachable_result,
)
from franc.utils import MCPResponse, MCPToolStatus, _log_and_return_error, maybe_compress, require_client

# ---------------------------------------------------------------------------
# SDK relationship filters per connector type
# ---------------------------------------------------------------------------

_REL_FILTERS: dict[str, list[str]] = {
    "cable": ["device__interface", "dcimcable__dcimendpoint"],
    "physical_circuit": [
        "device__interface",
        "physical_circuit__provider_a",
        "physical_circuit__provider_z",
    ],
    # virtual_circuit__interfaces is the schema identifier for TopologyVirtualCircuit ↔ DcimInterface
    "virtual_circuit": ["device__interface", "virtual_circuit__interfaces"],
    # cloud circuits reuse virtual circuit link + cloud endpoint attachment
    "cloud_circuit": ["device__interface", "virtual_circuit__interfaces", "virtual_circuit__cloud_endpoints"],
}


def _sdk_rel_filters(connector_types: list[str]) -> list[str]:
    """Merge relationship filter identifiers for the given connector types."""
    seen: set[str] = set()
    result: list[str] = []
    for ct in connector_types:
        for rel in _REL_FILTERS.get(ct, []):
            if rel not in seen:
                seen.add(rel)
                result.append(rel)
    return result


async def _name_to_uuid(client: Any, name: str, branch: str | None) -> str | None:
    """Return the UUID of a DcimDevice by name (a UUID passes through), or None if not found."""
    if is_uuid(name):
        return name
    nodes = await client.filters(kind=KIND_DEVICE, name__value=name, branch=branch or "main")
    return nodes[0].id if nodes else None


def _sdk_paths_to_hops(paths: list[Any]) -> list[list[dict[str, str]]]:
    """Convert PathTraversalResult.paths to hop-dict lists.

    Each path is a fixed 5-hop chain:
      hops[0] src Device → hops[1] src Interface → hops[2] Connector → hops[3] dst Interface → hops[4] dst Device
    """
    _kind_to_ctype = {
        "dcimcable": "cable",
        "topologyphysicalcircuit": "physical_circuit",
        "topologyvirtualcircuit": "virtual_circuit",
    }
    result: list[list[dict[str, str]]] = []
    for path in paths:
        hops = path.hops
        if len(hops) < 5:
            continue
        connector_kind = (hops[2].node.kind or "").lower()
        ctype = next((v for k, v in _kind_to_ctype.items() if k in connector_kind), "cable")
        result.append(
            [
                {
                    "src_device": hops[0].node.display_label,
                    "src_iface": hops[1].node.display_label,
                    "connector_type": ctype,
                    "connector": hops[2].node.display_label,
                    "dst_iface": hops[3].node.display_label,
                    "dst_device": hops[4].node.display_label,
                }
            ]
        )
    return result


mcp: FastMCP = FastMCP(name="Infrahub Graph Traversal")


# ---------------------------------------------------------------------------
# MCP tools
# ---------------------------------------------------------------------------


@mcp.tool(
    tags={"graph", "path", "traversal"},
    annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True),
)
async def find_physical_path(
    ctx: Context,
    src: Annotated[str, Field(description="Source device name (e.g. DC1-POD1-SRV-01).")],
    dst: Annotated[str, Field(description="Destination device name (e.g. FR2-EDGE-01).")],
    max_hops: Annotated[
        int, Field(default=NEO4J_DEFAULT_MAX_HOPS, description="BFS depth limit.")
    ] = NEO4J_DEFAULT_MAX_HOPS,
    all_paths: Annotated[
        bool,
        Field(
            default=False,
            description=(
                "False (default): return only the single shortest representative path "
                "— one hop sequence, enough to understand the route for troubleshooting. "
                "True: return all ECMP paths of equal shortest length."
            ),
        ),
    ] = False,
    connector_types: Annotated[
        list[str] | None,
        Field(
            default=None,
            description=(
                "Connector types to traverse. Defaults to all: cable, physical_circuit, virtual_circuit, cloud_circuit."
            ),
        ),
    ] = None,
    branch: Annotated[str | None, Field(default=None, description="Branch to query (defaults to main).")] = None,
) -> MCPResponse[dict[str, Any]]:
    """
    Find physical/logical paths between two devices using InfrahubClient.traverse_paths().

    Traverses cables, physical circuits (cross-connects), virtual circuits (MPLS/SD-WAN),
    and cloud circuits (Direct Connect, ExpressRoute) in a single BFS.

    Use cases:
      - Trace a server to an ISP edge router
      - Verify DC-to-colocation path exists
      - Find all redundant paths between two PODs
      - Confirm a planned change won't cut an active path

    Returns per path: list of hops with src_device, src_iface, connector, dst_iface, dst_device.
    Auto-compresses if >5 paths found.
    """
    ctypes = connector_types or GRAPH_CONNECTOR_TYPES
    await ctx.info(f"find_physical_path: {src} → {dst}  max_hops={max_hops}  all_paths={all_paths}")

    client = require_client(ctx)
    src_uuid = await _name_to_uuid(client, src, branch)
    dst_uuid = await _name_to_uuid(client, dst, branch)
    if not src_uuid or not dst_uuid:
        return await _log_and_return_error(
            ctx=ctx,
            error=f"Device '{src if not src_uuid else dst}' not found.",
            remediation="Check the device name with get_topology_devices().",
        )

    try:
        result = await client.traverse_paths(
            src_uuid,
            dst_uuid,
            relationship_filter=_sdk_rel_filters(ctypes),
            max_depth=max_hops,
            max_paths=None if all_paths else 10,
            shortest_paths_only=not all_paths,
            branch=branch,
        )
    except Exception as exc:  # noqa: BLE001
        return await _log_and_return_error(ctx=ctx, error=str(exc), remediation="traverse_paths failed.")

    if result.truncated_at_depth is not None:
        await ctx.warning(
            f"Path search truncated at depth {result.truncated_at_depth} (max_hops={max_hops}). "
            "Results may be incomplete — try increasing max_hops."
        )

    paths = _sdk_paths_to_hops(result.paths)
    if not paths:
        hint = (
            f" Search was truncated at depth {result.truncated_at_depth} — increase max_hops."
            if result.truncated_at_depth is not None
            else ""
        )
        return await _log_and_return_error(
            ctx=ctx,
            error=f"No paths found from '{src}' to '{dst}' within {max_hops} hops.{hint}",
            remediation=(
                "Check device names with get_topology_devices(). "
                "Try increasing max_hops or adding more connector_types. "
                "Ensure cables/circuits are loaded in Infrahub."
            ),
        )

    paths_to_return = paths if all_paths else paths[:1]
    await ctx.info(
        f"Found {len(paths)} path(s), shortest={len(paths_to_return[0])} hops"
        + ("" if all_paths else f"  (showing 1 of {len(paths)}, use all_paths=true for full ECMP set)")
    )
    return await maybe_compress(ctx, paths_to_return, "paths", "paths_gcf") or MCPResponse(
        status=MCPToolStatus.SUCCESS,
        data={
            "path_count": len(paths),
            "ecmp_available": len(paths),
            "truncated_at_depth": result.truncated_at_depth,
            "paths": paths_to_return,
        },
    )


@mcp.tool(
    tags={"graph", "impact", "traversal"},
    annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True),
)
async def find_reachable(
    ctx: Context,
    source: Annotated[
        str,
        Field(
            description=(
                "Start node: a device name, a node UUID, or a kind-qualified HFID "
                "(e.g. 'DcimPhysicalDevice__dc1-fw-01') as returned by get_nodes(include_attributes=true)."
            )
        ),
    ],
    target_kinds: Annotated[
        list[str], Field(min_length=1, description="Kinds of nodes to look for (e.g. ['ManagedAppComponent']).")
    ],
    max_depth: Annotated[int | None, Field(default=None, description="Maximum relationship hops to explore.")] = None,
    max_results: Annotated[int, Field(default=20, ge=1, description="Maximum distinct reachable nodes.")] = 20,
    shortest_paths_only: Annotated[
        bool, Field(default=True, description="Return only the shortest path to each target.")
    ] = True,
    branch: Annotated[str | None, Field(default=None, description="Branch to query (defaults to main).")] = None,
) -> MCPResponse[dict[str, Any]]:
    """
    Impact / dependency analysis: find nodes of the given kinds reachable from a source node.

    Uses InfrahubClient.reachable_nodes() (Infrahub >= 1.10). Typical questions:
      - "Which app components sit behind this firewall?"  (source=firewall, target_kinds=[component kind])
      - "Which devices are affected if this circuit goes down?"

    Returns per dependency: depth, node identity and the hop path from the source.
    """
    client = require_client(ctx)
    branch_name = branch or "main"
    await ctx.info(f"find_reachable: {source} → {target_kinds}  max_depth={max_depth}  max_results={max_results}")

    src: Any = None
    hfid_error: NodeResolutionError | None = None
    if "__" in source:  # UUIDs have no "__"; a kind-qualified HFID always does
        try:
            src = await resolve_node_ref(client, source, branch=branch_name)
        except NodeResolutionError as exc:
            hfid_error = exc  # may still be a device name that happens to contain "__"
    if src is None:
        src = await _name_to_uuid(client, source, branch_name)
    if src is None:
        return await _log_and_return_error(
            ctx=ctx,
            error=str(hfid_error) if hfid_error else f"Device '{source}' not found.",
            remediation="Use get_topology_devices() for device names or get_nodes(include_attributes=true) for ids.",
        )

    try:
        result = await client.reachable_nodes(
            src,
            target_kinds,  # ty: ignore[invalid-argument-type]  # SDK list type is invariant over kind strings
            max_depth=max_depth,
            max_results=max_results,
            shortest_paths_only=shortest_paths_only,
            branch=branch_name,
        )
    except Exception as exc:  # noqa: BLE001
        return await _log_and_return_error(
            ctx=ctx,
            error=str(exc),
            remediation="reachable_nodes failed — it needs Infrahub >= 1.10 and valid target kinds (get_schema_mapping).",
        )

    shaped = shape_reachable_result(result)
    await ctx.info(f"Found {shaped['count']} reachable node(s)")
    return MCPResponse(status=MCPToolStatus.SUCCESS, data=shaped)


@mcp.tool(
    tags={"graph", "segment", "traversal"},
    annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True),
)
async def trace_segment_to_cloud(
    ctx: Context,
    src: Annotated[str, Field(description="Source device name (e.g. DC1-POD1-SRV-01).")],
    dst: Annotated[str, Field(description="Destination device name (e.g. AWS-EU-WEST-1-TGW-01).")],
    segment_name: Annotated[
        str,
        Field(
            description=(
                "Full VXLAN segment name as stored in Infrahub "
                "(e.g. 'Customer 1 - production - web-frontend'). "
                "Use get_segment_inventory() to find exact names."
            )
        ),
    ],
    max_hops: Annotated[
        int, Field(default=NEO4J_DEFAULT_MAX_HOPS, description="BFS depth limit.")
    ] = NEO4J_DEFAULT_MAX_HOPS,
    branch: Annotated[str | None, Field(default=None, description="Branch to query (defaults to main).")] = None,
) -> MCPResponse[dict[str, Any]]:
    """
    Trace a VXLAN segment path from a specific server to a specific cloud endpoint.

    Returns:
      - Segment context: CIDRs, namespace (VRF), deployments, VLAN/VNI per DC, firewall
      - Path from src to dst with connector details

    Use for:
      - Troubleshooting "why can't this VM reach the cloud?"
      - Verifying PBR is routing through the correct vFirewall
      - Confirming VLAN/VNI allocation is correct per DC
    """
    client = require_client(ctx)
    branch_name = branch or "main"
    await ctx.info(f"Resolving segment context for: {segment_name}")

    segs = await client.filters(kind="ManagedVxlanSegment", name__value=segment_name, branch=branch_name)
    if not segs:
        return await _log_and_return_error(
            ctx=ctx,
            error=f"Segment '{segment_name}' not found.",
            remediation="Check exact name with get_segment_inventory().",
        )
    seg: Any = segs[0]

    await seg.gateway.fetch()
    await seg.inline_service.fetch()
    await seg.deployments.fetch()
    await seg.segment_deployments.fetch()
    await seg.interface_capabilities.fetch()

    cidrs: list[str] = []
    gw = seg.gateway.peer
    if gw:
        await gw.ip_prefix.fetch()
        prefix_node = gw.ip_prefix.peer
        if prefix_node:
            cidrs.append(str(prefix_node.prefix.value))
    firewall = seg.inline_service.peer.name.value if seg.inline_service.peer else None
    deployments = [d.peer.name.value for d in seg.deployments.peers if d.peer]

    allocations: dict[str, dict] = {}
    for sd in seg.segment_deployments.peers:
        sd_node = sd.peer
        await sd_node.deployment.fetch()
        dep = sd_node.deployment.peer
        if dep:
            allocations[dep.name.value] = {
                "vlan_id": sd_node.vlan_id.value,
                "vni": sd_node.vni.value,
            }

    servers: list[str] = []
    for ic in seg.interface_capabilities.peers:
        iface = ic.peer
        if iface:
            await iface.device.fetch()
            dev = iface.device.peer
            if dev and dev.name.value not in servers:
                servers.append(dev.name.value)

    seg_context = {
        "name": segment_name,
        "pbr_enabled": seg.pbr_enabled.value,
        "cidrs": [str(c) for c in cidrs],
        "firewall": firewall,
        "deployments": deployments,
        "allocations": allocations,
        "servers": servers,
    }
    await ctx.info(f"Segment: servers={servers}  firewall={firewall}  pbr={seg.pbr_enabled.value}")

    src_uuid = await _name_to_uuid(client, src, branch_name)
    dst_uuid = await _name_to_uuid(client, dst, branch_name)
    if not src_uuid or not dst_uuid:
        return await _log_and_return_error(
            ctx=ctx,
            error=f"Device '{src if not src_uuid else dst}' not found.",
            remediation="Check the device name with get_topology_devices().",
        )

    await ctx.info(f"Tracing path: {src} → {dst}")
    try:
        result = await client.traverse_paths(
            src_uuid,
            dst_uuid,
            relationship_filter=_sdk_rel_filters(GRAPH_CONNECTOR_TYPES),
            max_depth=max_hops,
            branch=branch_name,
        )
    except Exception as exc:  # noqa: BLE001
        return await _log_and_return_error(ctx=ctx, error=str(exc), remediation="traverse_paths failed.")

    if result.truncated_at_depth is not None:
        await ctx.warning(
            f"Path search truncated at depth {result.truncated_at_depth} (max_hops={max_hops}). "
            "Results may be incomplete — try increasing max_hops."
        )

    paths = _sdk_paths_to_hops(result.paths)
    if not paths:
        hint = (
            f" Search was truncated at depth {result.truncated_at_depth} — increase max_hops."
            if result.truncated_at_depth is not None
            else ""
        )
        return await _log_and_return_error(
            ctx=ctx,
            error=f"No path found from '{src}' to '{dst}'.{hint}",
            remediation="Verify both devices exist and are connected. Try increasing max_hops.",
        )

    await ctx.info(f"Found {len(paths)} path(s)")
    return MCPResponse(
        status=MCPToolStatus.SUCCESS,
        data={
            "segment": seg_context,
            "src": src,
            "dst": dst,
            "path_count": len(paths),
            "paths": paths,
        },
    )


_GQL_CABLE_NEIGHBORS = """
query CableNeighbors($device: String!) {
  DcimPhysicalInterface(device__name__value: $device) {
    edges { node {
      name { value }
      cable { node {
        name { value }
        endpoints { edges { node {
          ... on DcimPhysicalInterface {
            name { value }
            device { node { name { value } } }
          }
        } } }
      } }
    } }
  }
}
"""

_GQL_PHYSICAL_CIRCUIT_NEIGHBORS = """
query PhysicalCircuitNeighbors {
  TopologyPhysicalCircuit(limit: 200) {
    edges { node {
      name { value }
      provider_a { node { ... on DcimPhysicalInterface {
        name { value }
        device { node { name { value } } }
      } } }
      provider_z { node { ... on DcimPhysicalInterface {
        name { value }
        device { node { name { value } } }
      } } }
    } }
  }
}
"""

_GQL_VIRTUAL_CIRCUIT_NEIGHBORS = """
query VirtualCircuitNeighbors {
  TopologyVirtualCircuit(limit: 200) {
    edges { node {
      name { value }
      interfaces { edges { node {
        ... on DcimVirtualInterface {
          name { value }
          device { node { name { value } } }
        }
      } } }
    } }
  }
}
"""


def _gval(node: dict, *keys: str, default: str = "") -> str:
    """Walk nested GQL node dicts, return leaf value or default."""
    cur: Any = node
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k) or {}
    return cur.get("value", default) if isinstance(cur, dict) else default


@mcp.tool(
    tags={"graph", "neighbors", "traversal"},
    annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True),
)
async def get_device_neighbors(
    ctx: Context,
    device_name: Annotated[str, Field(description="Device name to query (e.g. DC1-POD1-SPINE-01).")],
    connector_types: Annotated[
        list[str] | None,
        Field(default=None, description="Limit to: cable, physical_circuit, virtual_circuit, cloud_circuit."),
    ] = None,
    branch: Annotated[str | None, Field(default=None, description="Branch to query (defaults to main).")] = None,
) -> MCPResponse:
    """
    List all one-hop neighbors of a device via cables and circuits.

    Returns: src_iface, connector_type, connector, dst_iface, neighbor device.

    Use for:
      - Quick connectivity check ("what is this spine cabled to?")
      - Pre-change impact: "what breaks if I remove this device?"
      - Verifying cabling matches topology design
    """
    ctypes = set(connector_types or ["cable"])
    branch_name = branch or "main"
    client = require_client(ctx)
    await ctx.info(f"Fetching neighbors of {device_name} (branch={branch_name})...")

    results: list[dict[str, str]] = []

    try:
        if "cable" in ctypes:
            r = await client.execute_graphql(
                _GQL_CABLE_NEIGHBORS, branch_name=branch_name, variables={"device": device_name}
            )
            for edge in r["DcimPhysicalInterface"]["edges"]:
                iface = edge["node"]
                cable = (iface.get("cable") or {}).get("node")
                if not cable:
                    continue
                src = _gval(iface, "name")
                cname = _gval(cable, "name")
                for ep in cable["endpoints"]["edges"]:
                    far = ep["node"]
                    far_dev = _gval(far, "device", "node", "name")
                    if far_dev and far_dev != device_name:
                        results.append(
                            {
                                "src_iface": src,
                                "connector_type": "cable",
                                "connector": cname,
                                "dst_iface": _gval(far, "name"),
                                "neighbor": far_dev,
                            }
                        )

        if "physical_circuit" in ctypes:
            r = await client.execute_graphql(_GQL_PHYSICAL_CIRCUIT_NEIGHBORS, branch_name=branch_name)
            for edge in r["TopologyPhysicalCircuit"]["edges"]:
                c = edge["node"]
                cname = _gval(c, "name")
                pa = (c.get("provider_a") or {}).get("node") or {}
                pz = (c.get("provider_z") or {}).get("node") or {}
                dev_a = _gval(pa, "device", "node", "name")
                dev_z = _gval(pz, "device", "node", "name")
                if dev_a == device_name and dev_z:
                    results.append(
                        {
                            "src_iface": _gval(pa, "name"),
                            "connector_type": "physical_circuit",
                            "connector": cname,
                            "dst_iface": _gval(pz, "name"),
                            "neighbor": dev_z,
                        }
                    )
                elif dev_z == device_name and dev_a:
                    results.append(
                        {
                            "src_iface": _gval(pz, "name"),
                            "connector_type": "physical_circuit",
                            "connector": cname,
                            "dst_iface": _gval(pa, "name"),
                            "neighbor": dev_a,
                        }
                    )

        if "virtual_circuit" in ctypes or "cloud_circuit" in ctypes:
            r = await client.execute_graphql(_GQL_VIRTUAL_CIRCUIT_NEIGHBORS, branch_name=branch_name)
            for edge in r["TopologyVirtualCircuit"]["edges"]:
                vc = edge["node"]
                vcname = _gval(vc, "name")
                src_iface = ""
                far_peers: list[dict[str, str]] = []
                for ie in vc.get("interfaces", {}).get("edges", []):
                    iface = ie["node"]
                    dev = _gval(iface, "device", "node", "name")
                    if dev == device_name:
                        src_iface = _gval(iface, "name")
                    elif dev:
                        far_peers.append({"iface": _gval(iface, "name"), "device": dev})
                for peer in far_peers if src_iface else []:
                    results.append(
                        {
                            "src_iface": src_iface,
                            "connector_type": "virtual_circuit",
                            "connector": vcname,
                            "dst_iface": peer["iface"],
                            "neighbor": peer["device"],
                        }
                    )

    except Exception as exc:  # noqa: BLE001
        return await _log_and_return_error(
            ctx=ctx,
            error=str(exc),
            remediation="GraphQL query failed. Check device name and branch.",
        )

    if not results:
        return await _log_and_return_error(
            ctx=ctx,
            error=f"No neighbors found for '{device_name}'.",
            remediation=(
                "Verify device name with get_topology_devices(). "
                "Ensure cables/circuits are loaded. Try adding connector_types=['physical_circuit']."
            ),
        )

    await ctx.info(f"Found {len(results)} neighbor(s)")
    return await maybe_compress(ctx, results, "neighbors", "neighbors_gcf") or MCPResponse(
        status=MCPToolStatus.SUCCESS, data=results
    )


@mcp.tool(
    tags={"graph", "path", "traversal", "universal"},
    annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True),
)
async def trace_path(
    ctx: Context,
    src: Annotated[
        str,
        Field(
            description=(
                "Source endpoint. Accepts any of:\n"
                "  • device name         — 'DC1-POD1-SRV-01'\n"
                "  • device:iface        — 'DC1-POD1-SRV-01:eno1'\n"
                "  • segment name        — 'c001-web-frontend-p'\n"
                "  • component slug      — 'c001-trade-portal-p-web-frontend'\n"
                "  • component short name — 'web-frontend' (matches first unambiguous hit)\n"
                "Segments and components expand to all servers carrying them."
            )
        ),
    ],
    dst: Annotated[
        str,
        Field(
            description=(
                "Destination endpoint. Same formats as src:\n"
                "  device, device:iface, segment name, component slug, component short name."
            )
        ),
    ],
    connector_types: Annotated[
        list[str] | None,
        Field(
            default=None,
            description=(
                "Connector types to follow. Defaults to all: cable, physical_circuit, virtual_circuit, cloud_circuit."
            ),
        ),
    ] = None,
    max_hops: Annotated[
        int, Field(default=NEO4J_DEFAULT_MAX_HOPS, description="BFS depth limit.")
    ] = NEO4J_DEFAULT_MAX_HOPS,
    all_paths: Annotated[
        bool,
        Field(
            default=False,
            description=(
                "False (default): return only the single shortest representative path per pair "
                "— one hop sequence for quick troubleshooting. "
                "True: return all ECMP paths of equal shortest length."
            ),
        ),
    ] = False,
    branch: Annotated[str | None, Field(default=None, description="Branch to query (defaults to main).")] = None,
) -> MCPResponse[dict[str, Any]]:
    """
    Universal path tracer — resolves any source and destination to device(s) then runs BFS.

    Resolution order for each endpoint:
      1. device:iface  → single device, BFS pinned to that interface
      2. segment name  → all servers carrying that VXLAN segment on a LAG
      3. component slug or short name → segment → servers (via app_component__network_segment)
      4. device name   → single device

    When src or dst expands to multiple servers (segment / component), BFS runs for
    every (src_server, dst_server) pair. Same-server pairs are reported as co-located.

    Default behaviour (all_paths=False): returns one representative path per pair — shortest
    hop sequence found. Use all_paths=True for the full ECMP set (troubleshooting redundancy).

    Returns:
      {
        "src": {"raw": "...", "kind": "device|device:iface|segment|component",
                "devices": [{"device": "...", "iface": null|"..."}], "label": "..."},
        "dst": {same shape},
        "pairs": [
          {
            "src_device": "...", "src_iface": null|"...",
            "dst_device": "...", "dst_iface": null|"...",
            "co_located": bool,
            "path_count": int,
            "shortest_hops": int,
            "paths": [[{src_device, src_iface, connector_type, connector, dst_iface, dst_device}, ...], ...]
          }, ...
        ],
        "summary": {"total_pairs", "co_located", "routed", "unreachable",
                    "total_paths", "min_hops", "max_hops"}
      }
    """

    async def _segment_servers(segment_name: str) -> list[str]:
        """Return server device names attached to a ManagedVxlanSegment via interface_capabilities."""
        seg_nodes = await client.filters(kind="ManagedVxlanSegment", name__value=segment_name, branch=branch_name)
        if not seg_nodes:
            return []
        seg: Any = seg_nodes[0]
        await seg.interface_capabilities.fetch()
        servers: list[str] = []
        for ic in seg.interface_capabilities.peers:
            iface = ic.peer
            if iface:
                await iface.device.fetch()
                dev = iface.device.peer
                if dev and dev.name.value not in servers:
                    servers.append(dev.name.value)
        return servers

    async def _resolve_endpoint(raw: str) -> dict[str, Any]:
        """Resolve an endpoint string to a list of {device, iface} entries."""
        # 1. device:iface pin
        if ":" in raw and not raw.startswith("http"):
            device, iface = raw.split(":", 1)
            return {
                "raw": raw,
                "kind": "device:iface",
                "label": raw,
                "devices": [{"device": device.strip(), "iface": iface.strip()}],
            }

        # 2. segment name
        seg_servers = await _segment_servers(raw)
        if seg_servers:
            return {
                "raw": raw,
                "kind": "segment",
                "label": f"segment:{raw}",
                "devices": [{"device": s, "iface": None} for s in seg_servers],
            }

        # 3. AppComponent slug / short name → segments → servers
        comp_nodes = await client.filters(kind="AppComponent", name__value=raw, branch=branch_name)
        if not comp_nodes:
            comp_nodes = await client.filters(kind="AppComponent", slug__value=raw, branch=branch_name)
        if comp_nodes:
            comp: Any = comp_nodes[0]
            await comp.network_segment.fetch()
            seg_names: list[str] = []
            seg_node = comp.network_segment.peer
            if seg_node and seg_node.name.value not in seg_names:
                seg_names.append(seg_node.name.value)
            all_servers: list[str] = []
            for sn in seg_names:
                for s in await _segment_servers(sn):
                    if s not in all_servers:
                        all_servers.append(s)
            if all_servers:
                return {
                    "raw": raw,
                    "kind": "component",
                    "label": f"component:{raw}",
                    "segments": seg_names,
                    "devices": [{"device": s, "iface": None} for s in all_servers],
                }

        # 4. bare device name
        return {"raw": raw, "kind": "device", "label": raw, "devices": [{"device": raw, "iface": None}]}

    client = require_client(ctx)
    branch_name = branch or "main"
    connectors = connector_types or GRAPH_CONNECTOR_TYPES
    await ctx.info(f"trace_path: resolving endpoints  src={src!r}  dst={dst!r}")

    src_ep = await _resolve_endpoint(src)
    dst_ep = await _resolve_endpoint(dst)

    await ctx.info(f"src resolved: kind={src_ep['kind']} devices={[d['device'] for d in src_ep['devices']]}")
    await ctx.info(f"dst resolved: kind={dst_ep['kind']} devices={[d['device'] for d in dst_ep['devices']]}")

    rel_filters = _sdk_rel_filters(connectors)
    pairs: list[dict[str, Any]] = []
    for sd in src_ep["devices"]:
        for dd in dst_ep["devices"]:
            s_dev, s_iface = sd["device"], sd.get("iface")
            d_dev, d_iface = dd["device"], dd.get("iface")

            if s_dev == d_dev:
                pairs.append(
                    {
                        "src_device": s_dev,
                        "src_iface": s_iface,
                        "dst_device": d_dev,
                        "dst_iface": d_iface,
                        "co_located": True,
                        "path_count": 0,
                        "shortest_hops": 0,
                        "paths": [],
                    }
                )
                continue

            s_uuid = await _name_to_uuid(client, s_dev, branch_name)
            d_uuid = await _name_to_uuid(client, d_dev, branch_name)
            if not s_uuid or not d_uuid:
                await ctx.warning(f"Device not found: {s_dev if not s_uuid else d_dev}")
                pairs.append(
                    {
                        "src_device": s_dev,
                        "src_iface": s_iface,
                        "dst_device": d_dev,
                        "dst_iface": d_iface,
                        "co_located": False,
                        "path_count": 0,
                        "shortest_hops": 0,
                        "paths": [],
                    }
                )
                continue

            try:
                result = await client.traverse_paths(
                    s_uuid,
                    d_uuid,
                    relationship_filter=rel_filters,
                    max_depth=max_hops,
                    max_paths=None if all_paths else 10,
                    shortest_paths_only=not all_paths,
                    branch=branch_name,
                )
                if result.truncated_at_depth is not None:
                    await ctx.warning(
                        f"Path search {s_dev}→{d_dev} truncated at depth {result.truncated_at_depth}. "
                        "ECMP results may be incomplete."
                    )
                sdk_paths = _sdk_paths_to_hops(result.paths)
            except Exception as exc:  # noqa: BLE001
                await ctx.warning(f"traverse_paths failed {s_dev}→{d_dev}: {exc}")
                sdk_paths = []

            paths_to_return = sdk_paths if all_paths else sdk_paths[:1]
            pairs.append(
                {
                    "src_device": s_dev,
                    "src_iface": s_iface,
                    "dst_device": d_dev,
                    "dst_iface": d_iface,
                    "co_located": False,
                    "path_count": len(sdk_paths),
                    "ecmp_available": len(sdk_paths),
                    "shortest_hops": len(sdk_paths[0]) if sdk_paths else 0,
                    "paths": paths_to_return,
                }
            )

    routed = [p for p in pairs if not p["co_located"] and p["path_count"] > 0]
    unreachable = [p for p in pairs if not p["co_located"] and p["path_count"] == 0]
    co_locs = [p for p in pairs if p["co_located"]]
    hops_list = [p["shortest_hops"] for p in routed]

    return MCPResponse(
        status=MCPToolStatus.SUCCESS,
        data={
            "src": src_ep,
            "dst": dst_ep,
            "pairs": pairs,
            "summary": {
                "total_pairs": len(pairs),
                "co_located": len(co_locs),
                "routed": len(routed),
                "unreachable": len(unreachable),
                "total_paths": sum(p["path_count"] for p in routed),
                "min_hops": min(hops_list, default=0),
                "max_hops": max(hops_list, default=0),
            },
        },
    )


@mcp.tool(
    tags={"graph", "cypher", "raw"},
    annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=False),
)
async def execute_cypher(
    ctx: Context,
    query: Annotated[
        str,
        Field(
            description=(
                "Raw Cypher query to execute against Neo4j. "
                "READ-ONLY: use only MATCH/RETURN statements. "
                "Do NOT use CREATE/MERGE/DELETE/SET — those corrupt Infrahub state. "
                "Always use unique variable names per MATCH clause to avoid silent row collapse."
            )
        ),
    ],
) -> MCPResponse:
    """
    Execute a raw read-only Cypher query against the Infrahub Neo4j graph.

    Use this for ad-hoc analysis that the other traversal tools don't cover:
      - Count cables of a specific type
      - Find devices with no cabling
      - Identify orphaned interfaces
      - Custom multi-hop traversal with filtering
      - Impact analysis of a proposed change

    Graph model reminders:
      - Attributes: (node)-[:HAS_ATTRIBUTE]->(attr {name:"field"})-[:HAS_VALUE]->(val {value:...})
      - Device↔Interface: both → IS_RELATED → Relationship{name:"device__interface"}
      - Cables: cable + interface → IS_RELATED → Relationship{name:"dcimcable__dcimendpoint"}
      - Circuits: interface → IS_RELATED → Relationship{name:"physical_circuit__provider_a/z"}
      - IMPORTANT: reuse of variable names across MATCH clauses silently collapses rows to 0

    Returns raw rows. Auto-compresses if >5 rows.
    """
    # Guard against writes, procedure calls (apoc/dbms), file loads and statement chaining
    if keyword := cypher_write_violation(query):
        return await _log_and_return_error(
            ctx=ctx,
            error=f"Forbidden Cypher keyword: {keyword}. execute_cypher only accepts read queries (MATCH/RETURN).",
            remediation="Remove write/CALL/LOAD clauses and statement separators. Use Infrahub SDK tools to modify data.",
        )

    await ctx.info("Executing raw Cypher query...")
    try:
        url, user, password = _neo4j_credentials()
        rows = _neo4j_run(query, url, user, password)
    except httpx.ConnectError as exc:
        return await _log_and_return_error(
            ctx=ctx,
            error=str(exc),
            remediation="Neo4j unreachable. Check NEO4J_HTTP_URL env var.",
        )
    except RuntimeError as exc:
        return await _log_and_return_error(
            ctx=ctx,
            error=exc,
            remediation=(
                "Common Cypher mistakes: "
                "(1) Reused variable names across MATCH clauses — use unique names per hop. "
                "(2) Wrong relationship name — check 'device__interface' vs 'dcimcable__dcimendpoint'. "
                "(3) Wrong node label — use DcimPhysicalDevice not DcimDevice for physical devices."
            ),
        )

    if not rows:
        await ctx.info("Query returned 0 rows.")
        return MCPResponse(status=MCPToolStatus.SUCCESS, data={"rows": [], "count": 0})

    await ctx.info(f"Query returned {len(rows)} row(s)")
    return await maybe_compress(ctx, rows, "Cypher rows", "cypher_rows_gcf") or MCPResponse(
        status=MCPToolStatus.SUCCESS,
        data={"rows": rows, "count": len(rows)},
    )
