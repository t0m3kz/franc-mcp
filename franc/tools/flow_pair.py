"""
Component-to-component and application-to-application path tracing.

Both tools resolve logical app/component identifiers to physical servers via
the Infrahub SDK, then run pairwise traverse_paths to find ECMP paths with
interface health annotations.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from franc.constants import GRAPH_CONNECTOR_TYPES, NEO4J_DEFAULT_MAX_HOPS
from franc.flow_helpers import (
    _annotate_hop,
    _fetch_iface_status_map,
    _path_summary,
    _resolve_app_sdk,
    _resolve_component_sdk,
)
from franc.tools.graph import _name_to_uuid, _sdk_paths_to_hops, _sdk_rel_filters
from franc.utils import MCPResponse, MCPToolStatus, _log_and_return_error, require_client

mcp: FastMCP = FastMCP(name="Infrahub Flow Pair Tracing")


@mcp.tool(
    tags={"flow", "application", "trace", "component"},
    annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True),
)
async def trace_component_to_component(
    ctx: Context,
    src_component: Annotated[
        str,
        Field(
            description=(
                "Source AppComponent — short name (e.g. 'web-frontend') or full slug "
                "(e.g. 'c001-trade-portal-p-web-frontend'). "
                "Short names match any component with that name across all apps."
            )
        ),
    ],
    dst_component: Annotated[
        str,
        Field(
            description=(
                "Destination AppComponent — short name or full slug (e.g. 'auth-api', 'c001-auth-platform-p-auth-api')."
            )
        ),
    ],
    connector_types: Annotated[
        list[str] | None,
        Field(default=None, description="Connector types to follow. Default: all."),
    ],
    max_hops: Annotated[int, Field(default=NEO4J_DEFAULT_MAX_HOPS, description="BFS depth limit.")],
    branch: Annotated[str | None, Field(default=None, description="Branch to query (defaults to main).")] = None,
) -> MCPResponse[dict[str, Any]]:
    """
    Trace the physical network path between two application components.

    Resolves each component to its network segment, finds the servers carrying
    that segment, then runs BFS across all (src_server, dst_server) pairs to
    produce ECMP paths with interface health annotations.

    Component → segment resolution:
      AppComponent -[app_component__network_segment]-> ManagedVxlanSegment
      ManagedVxlanSegment -[interface_capabilities]-> DcimLAGInterface -> server

    If a component name is ambiguous (multiple slugs match — e.g. "web-frontend"
    exists in both trade-portal and shipment-tracker), all matching segments and
    their servers are included and the `ambiguous` flag is set to true.

    Path tracing:
      - All unique (src_server, dst_server) pairs are traced.
      - Same-server pairs are reported as co-located without BFS.
      - ECMP paths at the shortest hop count are returned per server pair.
      - Every hop is annotated with src/dst interface status.

    Returns:
      {
        "src": {"component", "slug", "type", "segment", "servers", "ambiguous"},
        "dst": {same shape},
        "server_pairs": [
          {
            "src_server", "dst_server", "co_located",
            "ecmp_paths", "shortest_hops", "path_status",
            "broken_at", "warnings_at", "representative_route"
          }, ...
        ],
        "summary": {"total_server_pairs", "co_located_pairs", "routed_pairs",
                    "total_ecmp_paths", "max_hops", "min_hops", "overall_health"}
      }
    """
    client = require_client(ctx)
    branch_name = branch or "main"
    connectors = connector_types or GRAPH_CONNECTOR_TYPES
    await ctx.info(f"Resolving components: '{src_component}' -> '{dst_component}'")

    src_info = await _resolve_component_sdk(client, src_component, branch_name)
    dst_info = await _resolve_component_sdk(client, dst_component, branch_name)

    if "error" in src_info:
        return await _log_and_return_error(ctx=ctx, error=src_info["error"], remediation="Check component name/slug.")
    if "error" in dst_info:
        return await _log_and_return_error(ctx=ctx, error=dst_info["error"], remediation="Check component name/slug.")

    src_servers: list[str] = src_info["servers"]
    dst_servers: list[str] = dst_info["servers"]

    if not src_servers:
        return await _log_and_return_error(
            ctx=ctx,
            error=f"No servers found for component '{src_component}' (segment={src_info.get('segments')}).",
            remediation="Ensure vxlan_segment generator has run and LAG interfaces are loaded.",
        )
    if not dst_servers:
        return await _log_and_return_error(
            ctx=ctx,
            error=f"No servers found for component '{dst_component}' (segment={dst_info.get('segments')}).",
            remediation="Ensure vxlan_segment generator has run and LAG interfaces are loaded.",
        )

    await ctx.info(
        f"  src servers ({len(src_servers)}): {src_servers[:4]}  dst servers ({len(dst_servers)}): {dst_servers[:4]}"
    )

    rel_filters = _sdk_rel_filters(connectors)
    server_pairs: list[dict[str, Any]] = []
    all_path_devices: set[str] = set()
    all_hop_dicts_per_pair: list[tuple[int, list[list[dict[str, Any]]]]] = []

    for src_srv in src_servers:
        for dst_srv in dst_servers:
            if src_srv == dst_srv:
                server_pairs.append(
                    {
                        "src_server": src_srv,
                        "dst_server": dst_srv,
                        "co_located": True,
                        "ecmp_paths": 1,
                        "shortest_hops": 0,
                        "path_status": "ok",
                        "broken_at": [],
                        "warnings_at": [],
                        "representative_route": [src_srv],
                    }
                )
                continue

            s_uuid = await _name_to_uuid(client, src_srv, branch_name)
            d_uuid = await _name_to_uuid(client, dst_srv, branch_name)
            if not s_uuid or not d_uuid:
                await ctx.warning(f"  UUID not found: {src_srv if not s_uuid else dst_srv}")
                continue

            try:
                result = await client.traverse_paths(
                    s_uuid,
                    d_uuid,
                    relationship_filter=rel_filters,
                    max_depth=max_hops,
                    max_paths=10,
                    branch=branch_name,
                )
                sdk_paths = _sdk_paths_to_hops(result.paths)
            except Exception as exc:  # noqa: BLE001
                await ctx.warning(f"  traverse_paths {src_srv}->{dst_srv} failed: {exc}")
                continue

            if not sdk_paths:
                server_pairs.append(
                    {
                        "src_server": src_srv,
                        "dst_server": dst_srv,
                        "co_located": False,
                        "ecmp_paths": 0,
                        "shortest_hops": -1,
                        "path_status": "unreachable",
                        "broken_at": [],
                        "warnings_at": [],
                        "representative_route": [],
                    }
                )
                continue

            shortest = len(sdk_paths[0])
            all_path_devices.update([src_srv, dst_srv])
            for path in sdk_paths:
                for h in path:
                    all_path_devices.update([h["src_device"], h["dst_device"]])

            pair_index = len(server_pairs)
            all_hop_dicts_per_pair.append((pair_index, sdk_paths))
            rep_route = [src_srv] + [h["dst_device"] for h in sdk_paths[0]]
            server_pairs.append(
                {
                    "src_server": src_srv,
                    "dst_server": dst_srv,
                    "co_located": False,
                    "ecmp_paths": len(sdk_paths),
                    "shortest_hops": shortest,
                    "path_status": "pending",
                    "broken_at": [],
                    "warnings_at": [],
                    "representative_route": rep_route,
                }
            )

    await ctx.info(
        f"  {len(server_pairs)} server pair(s), {sum(p['ecmp_paths'] for p in server_pairs)} total ECMP paths"
    )

    status_map: dict[tuple[str, str], str] = {}
    if all_path_devices:
        try:
            status_map = await _fetch_iface_status_map(client, list(all_path_devices), branch_name)
        except Exception as exc:  # noqa: BLE001
            await ctx.warning(f"Interface status check failed: {exc}")

    for pair_index, hop_dicts in all_hop_dicts_per_pair:
        broken_at: list[str] = []
        warnings_at: list[str] = []
        for path in hop_dicts:
            annotated = [_annotate_hop(h, status_map) for h in path]
            summary = _path_summary(annotated)
            broken_at.extend(summary["broken_at"])
            warnings_at.extend(summary["warnings_at"])
        broken_at = list(dict.fromkeys(broken_at))
        warnings_at = list(dict.fromkeys(warnings_at))
        pair = server_pairs[pair_index]
        pair["path_status"] = "broken" if broken_at else ("warning" if warnings_at else "ok")
        pair["broken_at"] = broken_at
        pair["warnings_at"] = warnings_at

    routed = [p for p in server_pairs if not p["co_located"] and p["ecmp_paths"] > 0]
    co_located = [p for p in server_pairs if p["co_located"]]
    hops_list = [p["shortest_hops"] for p in routed]
    overall = (
        "broken"
        if any(p["path_status"] == "broken" for p in server_pairs)
        else "warning"
        if any(p["path_status"] == "warning" for p in server_pairs)
        else "ok"
    )

    return MCPResponse(
        status=MCPToolStatus.SUCCESS,
        data={
            "src": src_info,
            "dst": dst_info,
            "server_pairs": server_pairs,
            "summary": {
                "total_server_pairs": len(server_pairs),
                "co_located_pairs": len(co_located),
                "routed_pairs": len(routed),
                "total_ecmp_paths": sum(p["ecmp_paths"] for p in routed),
                "min_hops": min(hops_list, default=0),
                "max_hops": max(hops_list, default=0),
                "overall_health": overall,
            },
        },
    )


@mcp.tool(
    tags={"flow", "application", "trace", "app-to-app"},
    annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True),
)
async def trace_app_to_app(
    ctx: Context,
    src_app: Annotated[
        str,
        Field(
            description=(
                "Source application name or label (e.g. 'trade-portal', 'fleet-manager'). "
                "All components of this application are resolved to their network segments and servers."
            )
        ),
    ],
    dst_app: Annotated[
        str,
        Field(
            description=(
                "Destination application name or label (e.g. 'auth-platform', 'risk-engine'). "
                "All components of this application are resolved to their network segments and servers."
            )
        ),
    ],
    connector_types: Annotated[
        list[str] | None,
        Field(default=None, description="Connector types to follow. Default: all."),
    ] = None,
    max_hops: Annotated[
        int, Field(default=NEO4J_DEFAULT_MAX_HOPS, description="BFS depth limit.")
    ] = NEO4J_DEFAULT_MAX_HOPS,
    branch: Annotated[str | None, Field(default=None, description="Branch to query (defaults to main).")] = None,
) -> MCPResponse[dict[str, Any]]:
    """
    Trace all physical network paths between two applications.

    Resolves every component of src_app and dst_app to their respective network
    segments and servers, then runs BFS across all (src_server, dst_server) pairs.

    Component → segment resolution:
      AppApplication -[label/name]-> AppComponent -[network_segment]->
      ManagedVxlanSegment -[interface_capabilities]-> DcimLAGInterface -> server

    Returns:
      {
        "src_app": {"name", "components": [{"slug", "type", "segment", "servers",
                                             "pbr_enabled", "firewall"}, ...]},
        "dst_app": {same shape},
        "component_pairs": [
          {
            "src_component", "src_slug", "src_segment",
            "dst_component", "dst_slug", "dst_segment",
            "server_pairs": [
              {
                "src_server", "dst_server", "co_located",
                "ecmp_paths", "shortest_hops", "path_status",
                "paths": [{hop dicts with interface health}]
              }, ...
            ]
          }, ...
        ],
        "summary": {"src_app", "dst_app", "total_component_pairs", "total_server_pairs",
                    "co_located_pairs", "routed_pairs", "unreachable_pairs",
                    "total_ecmp_paths", "min_hops", "max_hops", "overall_health"}
      }
    """
    client = require_client(ctx)
    branch_name = branch or "main"
    connectors = connector_types or GRAPH_CONNECTOR_TYPES
    rel_filters = _sdk_rel_filters(connectors)

    await ctx.info(f"trace_app_to_app: {src_app!r} → {dst_app!r}")
    src_label, src_comps = await _resolve_app_sdk(client, src_app, branch_name)
    dst_label, dst_comps = await _resolve_app_sdk(client, dst_app, branch_name)

    if not src_comps:
        return await _log_and_return_error(
            ctx=ctx,
            error=f"Application '{src_app}' not found or has no components.",
            remediation="Check app name with get_application_inventory().",
        )
    if not dst_comps:
        return await _log_and_return_error(
            ctx=ctx,
            error=f"Application '{dst_app}' not found or has no components.",
            remediation="Check app name with get_application_inventory().",
        )

    await ctx.info(f"src: {len(src_comps)} components  dst: {len(dst_comps)} components")

    component_pairs: list[dict[str, Any]] = []
    all_devices: set[str] = set()

    for sc in src_comps:
        for dc in dst_comps:
            if not sc["servers"] or not dc["servers"]:
                continue
            pair_server_pairs: list[dict[str, Any]] = []

            for s_srv in sc["servers"]:
                for d_srv in dc["servers"]:
                    all_devices.update([s_srv, d_srv])
                    if s_srv == d_srv:
                        pair_server_pairs.append(
                            {
                                "src_server": s_srv,
                                "dst_server": d_srv,
                                "co_located": True,
                                "ecmp_paths": 0,
                                "shortest_hops": 0,
                                "path_status": "co_located",
                                "paths": [],
                            }
                        )
                        continue

                    s_uuid = await _name_to_uuid(client, s_srv, branch_name)
                    d_uuid = await _name_to_uuid(client, d_srv, branch_name)
                    if not s_uuid or not d_uuid:
                        await ctx.warning(f"  UUID not found: {s_srv if not s_uuid else d_srv}")
                        continue

                    try:
                        result = await client.traverse_paths(
                            s_uuid,
                            d_uuid,
                            relationship_filter=rel_filters,
                            max_depth=max_hops,
                            max_paths=10,
                            branch=branch_name,
                        )
                        sdk_paths = _sdk_paths_to_hops(result.paths)
                    except Exception as exc:  # noqa: BLE001
                        await ctx.warning(f"  traverse_paths {s_srv}->{d_srv} failed: {exc}")
                        continue

                    if not sdk_paths:
                        pair_server_pairs.append(
                            {
                                "src_server": s_srv,
                                "dst_server": d_srv,
                                "co_located": False,
                                "ecmp_paths": 0,
                                "shortest_hops": 0,
                                "path_status": "unreachable",
                                "paths": [],
                            }
                        )
                        continue

                    for p in sdk_paths:
                        for h in p:
                            all_devices.update([h["src_device"], h["dst_device"]])

                    # Inject a synthetic firewall hop after the first physical hop
                    # when the source segment has PBR enabled and a firewall assigned.
                    src_seg_ctx = sc.get("seg_ctx") or {}
                    fw_name = src_seg_ctx.get("firewall")
                    pbr_enabled = src_seg_ctx.get("pbr_enabled")
                    if pbr_enabled and fw_name:
                        all_devices.add(fw_name)
                        fw_hop = {
                            "src_device": fw_name,
                            "dst_device": fw_name,
                            "src_iface": "",
                            "dst_iface": "",
                            "connector": "",
                            "connector_type": "pbr-policy",
                            "fw_policy": True,
                            "firewall": fw_name,
                        }
                        sdk_paths = [path[:1] + [fw_hop] + path[1:] for path in sdk_paths]

                    pair_server_pairs.append(
                        {
                            "src_server": s_srv,
                            "dst_server": d_srv,
                            "co_located": False,
                            "ecmp_paths": len(sdk_paths),
                            "shortest_hops": len(sdk_paths[0]),
                            "path_status": "ok",
                            "paths": sdk_paths,
                            "firewall": fw_name,
                            "pbr_enabled": bool(pbr_enabled),
                        }
                    )

            if pair_server_pairs:
                component_pairs.append(
                    {
                        "src_component": sc["name"],
                        "src_slug": sc["slug"],
                        "src_segment": sc["segment"],
                        "dst_component": dc["name"],
                        "dst_slug": dc["slug"],
                        "dst_segment": dc["segment"],
                        "server_pairs": pair_server_pairs,
                    }
                )

    if all_devices:
        try:
            status_map = await _fetch_iface_status_map(client, list(all_devices), branch_name)
            for cp in component_pairs:
                for sp in cp["server_pairs"]:
                    if sp["path_status"] in ("co_located", "unreachable"):
                        continue
                    sp["paths"] = [[_annotate_hop(h, status_map) for h in path] for path in sp["paths"]]
                    if sp["paths"]:
                        sp["path_status"] = _path_summary(sp["paths"][0])["path_status"]
        except Exception as exc:  # noqa: BLE001
            await ctx.warning(f"Interface status check failed: {exc}")

    all_server_pairs = [sp for cp in component_pairs for sp in cp["server_pairs"]]
    co_locs = [sp for sp in all_server_pairs if sp["co_located"]]
    routed = [sp for sp in all_server_pairs if not sp["co_located"] and sp["ecmp_paths"] > 0]
    unreachable = [sp for sp in all_server_pairs if not sp["co_located"] and sp["ecmp_paths"] == 0]
    hops_list = [sp["shortest_hops"] for sp in routed]
    overall = (
        "broken"
        if any(sp["path_status"] == "broken" for sp in all_server_pairs)
        else "warning"
        if any(sp["path_status"] == "warning" for sp in all_server_pairs)
        else "ok"
    )

    return MCPResponse(
        status=MCPToolStatus.SUCCESS,
        data={
            "src_app": {"name": src_label, "components": src_comps},
            "dst_app": {"name": dst_label, "components": dst_comps},
            "component_pairs": component_pairs,
            "summary": {
                "src_app": src_app,
                "dst_app": dst_app,
                "total_component_pairs": len(component_pairs),
                "total_server_pairs": len(all_server_pairs),
                "co_located_pairs": len(co_locs),
                "routed_pairs": len(routed),
                "unreachable_pairs": len(unreachable),
                "total_ecmp_paths": sum(sp["ecmp_paths"] for sp in routed),
                "min_hops": min(hops_list, default=0),
                "max_hops": max(hops_list, default=0),
                "overall_health": overall,
            },
        },
    )
