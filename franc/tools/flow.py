"""
Application component and routing-path flow tracing.

trace_application_flow  — AppComponent → segment → virtual FW → physical chassis
                          → fabric → DCI → cloud instances + cluster/VM capabilities.

trace_routing_connections — device → all path neighbors with per-interface health
                            annotation and BGP session validation.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from franc.constants import GRAPH_CONNECTOR_TYPES, NEO4J_DEFAULT_MAX_HOPS
from franc.flow_helpers import (
    _HEALTHY_STATUSES,
    _annotate_hop,
    _fetch_iface_status_map,
    _path_summary,
)
from franc.tools.graph import _name_to_uuid, _sdk_paths_to_hops, _sdk_rel_filters
from franc.utils import MCPResponse, MCPToolStatus, _log_and_return_error, maybe_compress, require_client

mcp: FastMCP = FastMCP(name="Infrahub Flow Tracing")

_KIND_TO_CTYPE = {
    "dcimcable": "cable",
    "topologyphysicalcircuit": "physical_circuit",
    "topologyvirtualcircuit": "virtual_circuit",
}

_BGP_GQL = """
query BGPSessions($device_name: String!) {
  ManagedBGPPeering(any__local_device__name__value: $device_name) {
    edges {
      node {
        name { value }
        status { value }
        type { value }
        local_device { node { name { value } } }
        remote_device { node { name { value } } }
      }
    }
  }
}
"""


@mcp.tool(
    tags={"flow", "application", "trace"},
    annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True),
)
async def trace_application_flow(
    ctx: Context,
    component_name: Annotated[
        str, Field(description="AppComponent name (e.g. 'web-frontend', 'api-gateway', 'order-db').")
    ],
    max_hops: Annotated[int, Field(default=NEO4J_DEFAULT_MAX_HOPS, description="BFS depth limit.")],
    branch: Annotated[str | None, Field(default=None, description="Branch to query (defaults to main).")] = None,
) -> MCPResponse[dict[str, Any]]:
    """
    Trace the complete end-to-end network flow for an application component.

    Reproduces the full traffic path a request would take:
      AppComponent → segment (CIDR, VRF, VLAN/VNI) → virtual FW → physical chassis
      → fabric → cross-connect → cloud circuit → cloud instances

    Also resolves Kubernetes cluster → node pools → worker instances and direct VM
    capabilities. Returns overall health based on interface status across all paths.

    Returns:
      {
        "component": {"name", "type", "application", "environment"},
        "network_segment": {CIDRs, namespace, firewalls, VNI/VLAN},
        "physical_chassis": ["DC3-FW-01"],
        "fabric_to_cloud_paths": [[hops]],
        "cloud_clusters": [{"cluster", "pools", "instances"}],
        "direct_vms": ["vm-01"],
        "status_warnings": [...],
        "overall_health": "ok|warning|broken"
      }
    """
    client = require_client(ctx)
    branch_name = branch or "main"
    await ctx.info(f"Tracing application flow for component '{component_name}'...")

    comp_nodes = await client.filters(kind="AppComponent", name__value=component_name, branch=branch_name)
    if not comp_nodes:
        return await _log_and_return_error(
            ctx=ctx,
            error=f"AppComponent '{component_name}' not found.",
            remediation="Verify component name with get_application_inventory().",
        )
    comp: Any = comp_nodes[0]

    comp_type = comp.component_type.value if hasattr(comp, "component_type") else None
    await comp.network_segment.fetch()
    segment_name: str = ""
    seg_peer = comp.network_segment.peer
    if seg_peer:
        segment_name = seg_peer.name.value

    # Resolve parent application
    application_label: str | None = None
    environment: str | None = None
    try:
        await comp.parent.fetch()
        app = comp.parent.peer
        if app:
            application_label = app.label.value if hasattr(app, "label") else app.name.value
            environment = app.environment.value if hasattr(app, "environment") else None
    except Exception:  # noqa: BLE001
        pass

    comp_info = {
        "name": component_name,
        "type": comp_type,
        "application": application_label,
        "environment": environment,
    }
    await ctx.info(f"  Component type: {comp_type}, segment: {segment_name or '(none)'}")

    seg_ctx: dict[str, Any] = {}
    physical_chassis: list[str] = []
    status_warnings: list[str] = []

    if segment_name:
        seg_nodes = await client.filters(kind="ManagedVxlanSegment", name__value=segment_name, branch=branch_name)
        if seg_nodes:
            seg: Any = seg_nodes[0]
            await seg.gateway.fetch()
            await seg.inline_service.fetch()
            await seg.deployments.fetch()
            await seg.segment_deployments.fetch()

            cidrs: list[str] = []
            gw = seg.gateway.peer
            if gw:
                await gw.ip_prefix.fetch()
                prefix_node = gw.ip_prefix.peer
                if prefix_node:
                    cidrs.append(str(prefix_node.prefix.value))
            fw_node = seg.inline_service.peer
            firewall = fw_node.name.value if fw_node else None
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

            seg_ctx = {
                "name": segment_name,
                "pbr_enabled": seg.pbr_enabled.value,
                "cidrs": [str(c) for c in cidrs],
                "firewall": firewall,
                "deployments": deployments,
                "allocations": allocations,
            }
            await ctx.info(f"  Segment CIDRs: {cidrs}  PBR={seg.pbr_enabled.value}")

            if firewall:
                # Check if virtual device — if so, resolve hosting chassis
                vdev_nodes = await client.filters(kind="DcimVirtualDevice", name__value=firewall, branch=branch_name)
                if vdev_nodes:
                    vdev: Any = vdev_nodes[0]
                    await vdev.hosting_device.fetch()
                    phys = vdev.hosting_device.peer
                    if phys:
                        physical_chassis.append(phys.name.value)
                        await ctx.info(f"  Virtual FW '{firewall}' hosted on: {phys.name.value}")
                else:
                    physical_chassis.append(firewall)

    # BFS from physical chassis to find fabric→cloud paths
    fabric_to_cloud: list[list[dict[str, str]]] = []
    all_devices_in_paths: set[str] = set()
    raw_paths_per_chassis: dict[str, list[list[dict[str, str]]]] = {}

    rel_filters = _sdk_rel_filters(GRAPH_CONNECTOR_TYPES)
    for chassis in physical_chassis:
        await ctx.info(f"  BFS from {chassis} (max {max_hops} hops)...")
        chassis_uuid = await _name_to_uuid(client, chassis, branch_name)
        if not chassis_uuid:
            await ctx.warning(f"  Device '{chassis}' UUID not found, skipping BFS.")
            continue
        # traverse_paths needs a destination — use get_device_neighbors to find cloud endpoints
        # then run traverse_paths for each
        # For now: get all virtual circuit neighbors (cloud endpoints)
        cloud_neighbors_gql = """
query CloudNeighbors($dev_name: String!) {
  TopologyVirtualCircuit(limit: 200) {
    edges {
      node {
        name { value }
        interfaces {
          edges {
            node {
              device { node { name { value } } }
            }
          }
        }
      }
    }
  }
}
"""
        try:
            gql_result = await client.execute_graphql(
                cloud_neighbors_gql, branch_name=branch_name, variables={"dev_name": chassis}
            )
            cloud_endpoints: set[str] = set()
            for edge in gql_result.get("TopologyVirtualCircuit", {}).get("edges", []):
                for iface_edge in edge["node"].get("interfaces", {}).get("edges", []):
                    dev_node = (iface_edge["node"].get("device") or {}).get("node") or {}
                    dev_name = dev_node.get("name", {}).get("value", "")
                    if dev_name and dev_name != chassis:
                        cloud_endpoints.add(dev_name)

            for cloud_ep in list(cloud_endpoints)[:10]:
                ep_uuid = await _name_to_uuid(client, cloud_ep, branch_name)
                if not ep_uuid:
                    continue
                try:
                    result = await client.traverse_paths(
                        chassis_uuid,
                        ep_uuid,
                        relationship_filter=rel_filters,
                        max_depth=max_hops,
                        max_paths=5,
                        branch=branch_name,
                    )
                    paths = _sdk_paths_to_hops(result.paths)
                    if paths:
                        raw_paths_per_chassis.setdefault(chassis, []).extend(paths)
                        fabric_to_cloud.extend(paths)
                        for path in paths:
                            for hop in path:
                                all_devices_in_paths.update([hop["src_device"], hop["dst_device"]])
                except Exception:  # noqa: BLE001
                    pass
        except Exception as exc:  # noqa: BLE001
            await ctx.warning(f"BFS from {chassis} failed: {exc}")

    if all_devices_in_paths:
        try:
            status_map = await _fetch_iface_status_map(client, list(all_devices_in_paths), branch_name)
            for chassis, paths in raw_paths_per_chassis.items():
                for path in paths:
                    annotated = [_annotate_hop(h, status_map) for h in path]
                    summary = _path_summary(annotated)
                    status_warnings.extend(f"BROKEN [{chassis}]: {loc}" for loc in summary["broken_at"])
                    status_warnings.extend(f"WARNING [{chassis}]: {loc}" for loc in summary["warnings_at"])
        except Exception as exc:  # noqa: BLE001
            await ctx.warning(f"Interface status check failed: {exc}")

    # Cluster capabilities
    cloud_clusters: list[dict[str, Any]] = []
    try:
        await comp.cluster_capabilities.fetch()
        cluster_map: dict[str, dict[str, Any]] = {}
        for cc_peer in comp.cluster_capabilities.peers:
            cluster = cc_peer.peer
            if not cluster:
                continue
            cn = cluster.name.value
            if cn not in cluster_map:
                cluster_map[cn] = {"cluster": cn, "pools": {}, "instances": []}
            await cluster.node_pools.fetch()
            for pool_peer in cluster.node_pools.peers:
                pool = pool_peer.peer
                if not pool:
                    continue
                pn = pool.name.value
                cluster_map[cn]["pools"].setdefault(pn, [])
                await pool.instances.fetch()
                for inst_peer in pool.instances.peers:
                    inst = inst_peer.peer
                    if inst:
                        inst_name = inst.name.value
                        cluster_map[cn]["pools"][pn].append(inst_name)
                        cluster_map[cn]["instances"].append(inst_name)
        cloud_clusters = list(cluster_map.values())
        if cloud_clusters:
            await ctx.info(f"  Cloud clusters: {[c['cluster'] for c in cloud_clusters]}")
    except Exception as exc:  # noqa: BLE001
        await ctx.warning(f"Cluster capabilities query failed: {exc}")

    # Direct VM capabilities
    direct_vms: list[str] = []
    try:
        await comp.capabilities.fetch()
        for cap_peer in comp.capabilities.peers:
            vm = cap_peer.peer
            if vm and hasattr(vm, "name"):
                direct_vms.append(vm.name.value)
    except Exception:  # noqa: BLE001
        pass

    return MCPResponse(
        status=MCPToolStatus.SUCCESS,
        data={
            "component": comp_info,
            "network_segment": seg_ctx,
            "physical_chassis": physical_chassis,
            "fabric_to_cloud_paths": fabric_to_cloud[:10],
            "cloud_clusters": cloud_clusters,
            "direct_vms": direct_vms,
            "status_warnings": status_warnings,
            "overall_health": (
                "broken" if any("BROKEN" in w for w in status_warnings) else ("warning" if status_warnings else "ok")
            ),
        },
    )


@mcp.tool(
    tags={"flow", "routing", "health", "trace"},
    annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True),
)
async def trace_routing_connections(
    ctx: Context,
    device: Annotated[str, Field(description="Source device name (e.g. DC3-POD1-SP1, DC3-BL1).")],
    destination: Annotated[
        str | None,
        Field(default=None, description="Optional destination device. Omit to trace all immediate neighbors."),
    ],
    connector_types: Annotated[
        list[str] | None,
        Field(default=None, description="Connector types: cable, physical_circuit, virtual_circuit, cloud_circuit."),
    ],
    check_bgp: Annotated[bool, Field(default=True, description="Query BGP sessions and flag non-established.")],
    max_hops: Annotated[int, Field(default=NEO4J_DEFAULT_MAX_HOPS, description="BFS depth limit.")],
    branch: Annotated[str | None, Field(default=None, description="Branch to query (defaults to main).")] = None,
) -> MCPResponse[dict[str, Any]]:
    """
    Find routing connections from a device and validate every interface along each path.

    Per-hop interface status:
      active            → ok (traffic flows)
      maintenance/outage → warning (degraded)
      disabled/deleted  → BROKEN (traffic cannot flow)

    BGP sessions are also checked and flagged if non-established.

    Returns:
      {
        "device", "destination",
        "paths": [{"destination", "hops": [{hop + iface statuses}],
                   "path_status", "broken_at", "warnings_at"}],
        "bgp_sessions": [{"session_name", "remote_device", "status", "type", "health"}],
        "summary": {"total_paths", "broken_paths", "warning_paths", "ok_paths",
                    "bgp_non_established", "overall_health"}
      }
    """
    client = require_client(ctx)
    branch_name = branch or "main"
    connectors = connector_types or GRAPH_CONNECTOR_TYPES
    rel_filters = _sdk_rel_filters(connectors)
    await ctx.info(f"Tracing routing connections from '{device}'  dst={destination or 'all'}...")

    src_uuid = await _name_to_uuid(client, device, branch_name)
    if not src_uuid:
        return await _log_and_return_error(
            ctx=ctx,
            error=f"Device '{device}' not found.",
            remediation="Verify device name with get_topology_devices().",
        )

    raw_paths: list[tuple[str, list[dict[str, str]]]] = []
    all_devices: set[str] = {device}

    if destination:
        dst_uuid = await _name_to_uuid(client, destination, branch_name)
        if not dst_uuid:
            return await _log_and_return_error(
                ctx=ctx,
                error=f"Device '{destination}' not found.",
                remediation="Verify device name with get_topology_devices().",
            )
        try:
            result = await client.traverse_paths(
                src_uuid,
                dst_uuid,
                relationship_filter=rel_filters,
                max_depth=max_hops,
                max_paths=None,
                branch=branch_name,
            )
            for sdk_path in _sdk_paths_to_hops(result.paths):
                raw_paths.append((destination, sdk_path))
                for hop in sdk_path:
                    all_devices.update([hop["src_device"], hop["dst_device"]])
        except Exception as exc:  # noqa: BLE001
            return await _log_and_return_error(ctx=ctx, error=str(exc), remediation="traverse_paths failed.")
    else:
        # Use get_device_neighbors logic — cable neighbors via GraphQL
        from franc.tools.graph import get_device_neighbors  # local import to avoid circular

        neighbors_resp = await get_device_neighbors(
            ctx,
            device_name=device,
            connector_types=connectors,
            branch=branch_name,
        )
        if neighbors_resp.status == MCPToolStatus.SUCCESS and isinstance(neighbors_resp.data, list):
            for neighbor in neighbors_resp.data:
                dst_dev = neighbor.get("neighbor_device", "")
                if not dst_dev:
                    continue
                src_iface = neighbor.get("src_interface", "")
                dst_iface = neighbor.get("neighbor_interface", "")
                ctype = neighbor.get("connector_type", "cable")
                hop = {
                    "src_device": device,
                    "src_iface": src_iface,
                    "connector_type": ctype,
                    "connector": neighbor.get("connector", ""),
                    "dst_iface": dst_iface,
                    "dst_device": dst_dev,
                }
                raw_paths.append((dst_dev, [hop]))
                all_devices.update([device, dst_dev])

    await ctx.info(f"  Found {len(raw_paths)} path(s) across {len(all_devices)} devices")

    status_map: dict[tuple[str, str], str] = {}
    if all_devices:
        try:
            status_map = await _fetch_iface_status_map(client, list(all_devices), branch_name)
            non_active = [(d, i, s) for (d, i), s in status_map.items() if s not in _HEALTHY_STATUSES]
            if non_active:
                await ctx.warning(
                    f"  {len(non_active)} non-active interfaces: "
                    + ", ".join(f"{d}/{i}={s}" for d, i, s in non_active[:5])
                )
        except Exception as exc:  # noqa: BLE001
            await ctx.warning(f"Interface status check failed: {exc}")

    annotated_paths = []
    for dst_device, hops in raw_paths:
        annotated_hops = [_annotate_hop(h, status_map) for h in hops]
        summary = _path_summary(annotated_hops)
        annotated_paths.append({"destination": dst_device, "hops": annotated_hops, **summary})

    bgp_sessions: list[dict[str, Any]] = []
    if check_bgp:
        try:
            bgp_result = await client.execute_graphql(
                _BGP_GQL,
                branch_name=branch_name,
                variables={"device_name": device},
            )
            seen_sessions: set[str] = set()
            for edge in bgp_result.get("ManagedBGPPeering", {}).get("edges", []):
                node = edge["node"]
                sname = node.get("name", {}).get("value", "")
                if sname in seen_sessions:
                    continue
                seen_sessions.add(sname)
                status = (node.get("status") or {}).get("value", "unknown")
                local_dev = ((node.get("local_device") or {}).get("node") or {}).get("name", {}).get("value")
                remote_dev = ((node.get("remote_device") or {}).get("node") or {}).get("name", {}).get("value")
                bgp_sessions.append(
                    {
                        "session_name": sname,
                        "local_device": local_dev,
                        "remote_device": remote_dev,
                        "status": status,
                        "type": (node.get("type") or {}).get("value"),
                        "health": "ok" if status == "established" else "warning",
                    }
                )
            non_est = [s for s in bgp_sessions if s["health"] != "ok"]
            if non_est:
                await ctx.warning(
                    f"  {len(non_est)} non-established BGP: " + ", ".join(s["session_name"] for s in non_est[:5])
                )
        except Exception as exc:  # noqa: BLE001
            await ctx.warning(f"BGP session query failed: {exc}")

    total = len(annotated_paths)
    broken_count = sum(1 for p in annotated_paths if p.get("path_status") == "broken")
    warning_count = sum(1 for p in annotated_paths if p.get("path_status") == "warning")
    bgp_non_est = len([s for s in bgp_sessions if s["health"] != "ok"])
    overall = "broken" if broken_count else "warning" if (warning_count or bgp_non_est) else "ok"

    compressed = await maybe_compress(ctx, annotated_paths, "routing paths", "paths_gcf")
    paths_payload: dict[str, Any] = (
        compressed.data if compressed and isinstance(compressed.data, dict) else {"paths": annotated_paths}
    )

    return MCPResponse(
        status=MCPToolStatus.SUCCESS,
        data={
            "device": device,
            "destination": destination,
            **paths_payload,
            "bgp_sessions": bgp_sessions,
            "summary": {
                "total_paths": total,
                "broken_paths": broken_count,
                "warning_paths": warning_count,
                "ok_paths": total - broken_count - warning_count,
                "bgp_non_established": bgp_non_est,
                "overall_health": overall,
            },
        },
    )
