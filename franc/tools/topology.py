"""Topology traversal tools — DC summary, devices, BGP, namespaces, fabric health, segments."""

from collections.abc import Sequence
from typing import TYPE_CHECKING, Annotated, Any

from fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from franc.constants import (
    KIND_BGP_PEERING,
    KIND_DEVICE,
    KIND_NAMESPACE,
    KIND_TOPOLOGY_DC,
    KIND_TOPOLOGY_POD,
    KIND_VIRTUAL_DEVICE,
    KIND_VLAN_SEGMENT,
    KIND_VXLAN_SEGMENT,
)
from franc.utils import (
    MCPResponse,
    MCPToolStatus,
    _gather_safe,
    _log_and_return_error,
    attr_val,
    maybe_compress,
    require_client,
    slim_node,
)

if TYPE_CHECKING:
    from infrahub_sdk import InfrahubClient

mcp: FastMCP = FastMCP(name="Infrahub Topology")

# Fields fetched per object type — keep these narrow to save tokens
_DC_FIELDS = ["name", "status", "amount_of_super_spines"]
_POD_FIELDS = ["index", "status", "amount_of_spines", "mlag_create"]
_DEVICE_FIELDS = ["name", "role", "platform", "status"]
_BGP_FIELDS = ["local_device", "remote_device", "local_as", "remote_as", "type", "status"]
_NS_FIELDS = ["name", "l3_vni", "namespace_type"]
_SEG_FIELDS = ["customer_name", "owner", "environment", "pbr_enabled"]


# ---------------------------------------------------------------------------
# Topology summary
# ---------------------------------------------------------------------------


@mcp.tool(
    tags={"topology", "retrieve"},
    annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True),
)
async def get_topology_summary(
    ctx: Context,
    datacenter: Annotated[
        str | None,
        Field(default=None, description="Filter by datacenter name (e.g. DC1). Returns all if omitted."),
    ],
    branch: Annotated[str | None, Field(default=None, description="Branch to read from (defaults to main).")],
) -> MCPResponse[dict[str, Any]]:
    """
    High-level topology summary for one or all datacenters.

    Returns per DC: name, status, super-spine count, pod count, pod designs,
    device-role breakdown, total devices, namespace count, VXLAN segment count.

    All sub-queries run in parallel — one round-trip worth of latency regardless of DC count.
    """
    try:
        client: InfrahubClient = require_client(ctx)
    except RuntimeError as exc:
        return await _log_and_return_error(ctx=ctx, error=str(exc), remediation="Start MCP with a configured client.")

    branch = branch or "main"

    # Fetch DCs and all topology data in one parallel round
    filter_kwargs: dict[str, Any] = {"name__value": datacenter} if datacenter else {}
    try:
        dc_nodes = (
            await client.filters(kind=KIND_TOPOLOGY_DC, branch=branch, prefetch_relationships=False, **filter_kwargs)
            if filter_kwargs
            else await client.all(kind=KIND_TOPOLOGY_DC, branch=branch, prefetch_relationships=False)
        )
    except Exception as exc:  # noqa: BLE001
        return await _log_and_return_error(ctx=ctx, error=exc, remediation="Could not fetch datacenters.")

    if not dc_nodes:
        return await _log_and_return_error(
            ctx=ctx,
            error=f"No TopologyDataCenter found{' named ' + datacenter if datacenter else ''}.",
            remediation="Use get_nodes(kind='TopologyDataCenter') to list available datacenters.",
        )

    # Fetch pods, devices, namespaces, segments in parallel (one gather for all DCs)
    pods_coro = client.all(kind=KIND_TOPOLOGY_POD, branch=branch, prefetch_relationships=False)
    devices_coro = client.all(kind=KIND_DEVICE, branch=branch, prefetch_relationships=False)
    ns_coro = client.all(kind=KIND_NAMESPACE, branch=branch, prefetch_relationships=False)
    vxlan_coro = client.all(kind=KIND_VXLAN_SEGMENT, branch=branch, prefetch_relationships=False)

    pods_all, devices_all, ns_all, vxlan_all = await _gather_safe(pods_coro, devices_coro, ns_coro, vxlan_coro)
    pods_all = pods_all or []
    devices_all = devices_all or []
    ns_all = ns_all or []
    vxlan_all = vxlan_all or []

    summaries: list[dict[str, Any]] = []
    for dc in dc_nodes:
        dc_name = attr_val(dc, "name", "?")

        # Pods that belong to this DC (name prefix match — pod name is computed as
        # "{dc_name}-POD-{index}" via TopologyPod.name jinja2 template)
        dc_pods = [
            p
            for p in pods_all
            if dc_name.upper() in str(getattr(p, "display_label", "") or attr_val(p, "name", "") or "").upper()
        ]
        pod_designs = sorted({attr_val(p, "design") for p in dc_pods if attr_val(p, "design")})

        # Devices that belong to this DC
        dc_devices = [d for d in devices_all if dc_name.upper() in (attr_val(d, "name", "") or "").upper()]
        role_counts: dict[str, int] = {}
        for d in dc_devices:
            role = attr_val(d, "role", "unknown") or "unknown"
            role_counts[role] = role_counts.get(role, 0) + 1

        summaries.append(
            {
                "name": dc_name,
                "status": attr_val(dc, "status"),
                "super_spines": attr_val(dc, "amount_of_super_spines"),
                "pod_count": len(dc_pods),
                "pod_designs": pod_designs,
                "device_roles": role_counts,
                "total_devices": len(dc_devices),
                "namespace_count": len(ns_all),
                "segment_count": len(vxlan_all),
            }
        )

    return MCPResponse(status=MCPToolStatus.SUCCESS, data={"datacenters": summaries})


# ---------------------------------------------------------------------------
# Topology devices
# ---------------------------------------------------------------------------


@mcp.tool(
    tags={"topology", "devices", "retrieve"},
    annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True),
)
async def get_topology_devices(
    ctx: Context,
    datacenter: Annotated[str, Field(description="Datacenter name to query (e.g. DC1).")],
    role: Annotated[
        str | None,
        Field(
            default=None,
            description="Filter by device role: spine, leaf, tor, border-leaf, super-spine, firewall, load-balancer.",
        ),
    ],
    pod_index: Annotated[int | None, Field(default=None, description="Filter by pod index (e.g. 1 for POD-1).")],
    branch: Annotated[str | None, Field(default=None, description="Branch to read from (defaults to main).")],
) -> MCPResponse:
    """
    List devices in a datacenter, filtered by role and/or pod index.

    Returns: name, role, platform, status. Auto-compresses if >5 devices.
    Uses slim_node extraction — no relationship fetches.
    """
    try:
        client: InfrahubClient = require_client(ctx)
    except RuntimeError as exc:
        return await _log_and_return_error(ctx=ctx, error=str(exc), remediation="Start MCP with a configured client.")

    branch = branch or "main"
    await ctx.info(f"Devices in {datacenter} role={role} pod={pod_index}")

    # Try SDK filter with deployment relationship first; fall back to GQL name-prefix scan
    # when the branch schema lacks the deployment__name__value filter argument.
    raw_devices: list[dict[str, Any]] | None = None
    filter_kwargs: dict[str, Any] = {"deployment__name__value": datacenter}
    if role:
        filter_kwargs["role__value"] = role
    try:
        sdk_nodes = await client.filters(kind=KIND_DEVICE, branch=branch, prefetch_relationships=False, **filter_kwargs)
        raw_devices = []
        for d in sdk_nodes:
            name = attr_val(d, "name", "?") or "?"
            if pod_index is not None:
                pod_tag = f"POD-{pod_index}"
                if pod_tag.upper() not in name.upper() and f"POD{pod_index}" not in name.upper():
                    continue
            entry = slim_node(d, _DEVICE_FIELDS)
            entry["id"] = getattr(d, "id", None)
            raw_devices.append(entry)
    except Exception:  # noqa: BLE001
        raw_devices = None  # fall through to GQL path

    if raw_devices is None:
        # GraphQL fallback — fetch all DcimPhysicalDevice and filter by name prefix in Python
        await ctx.info(f"Falling back to GraphQL name-prefix scan for {datacenter}")
        gql = """
        query {
            DcimPhysicalDevice {
                edges { node {
                    id
                    name   { value }
                    role   { value }
                    status { value }
                    platform { node { name { value } } }
                } }
            }
        }
        """
        try:
            r = await client.execute_graphql(query=gql, branch_name=branch)
        except Exception as exc:  # noqa: BLE001
            return await _log_and_return_error(ctx=ctx, error=exc, remediation="Device query failed.")

        dc_upper = datacenter.upper()
        raw_devices = []
        for edge in (r or {}).get("DcimPhysicalDevice", {}).get("edges", []):
            node = edge.get("node", {})
            name = (node.get("name") or {}).get("value") or ""
            if not name.upper().startswith(dc_upper + "-") and name.upper() != dc_upper:
                continue
            node_role = (node.get("role") or {}).get("value") or ""
            if role and node_role.lower() != role.lower():
                continue
            if pod_index is not None:
                pod_tag = f"POD{pod_index}"
                if pod_tag.upper() not in name.upper() and f"POD-{pod_index}" not in name.upper():
                    continue
            platform = ((node.get("platform") or {}).get("node") or {}).get("name", {}).get("value") or ""
            raw_devices.append(
                {
                    "id": node.get("id"),
                    "name": name,
                    "role": node_role,
                    "status": (node.get("status") or {}).get("value") or "",
                    "platform": platform,
                }
            )

    if not raw_devices:
        return await _log_and_return_error(
            ctx=ctx,
            error=f"No devices found in {datacenter}" + (f" role={role}" if role else "") + ".",
            remediation="Try without role filter or verify DC name with get_topology_summary.",
        )

    return await maybe_compress(ctx, raw_devices, "devices", "devices_gcf") or MCPResponse(
        status=MCPToolStatus.SUCCESS, data=raw_devices
    )


# ---------------------------------------------------------------------------
# BGP sessions
# ---------------------------------------------------------------------------


@mcp.tool(
    tags={"topology", "bgp", "retrieve"},
    annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True),
)
async def get_bgp_sessions(
    ctx: Context,
    datacenter: Annotated[str | None, Field(default=None, description="Filter by datacenter name.")],
    device_name: Annotated[str | None, Field(default=None, description="Filter by local device name.")],
    session_type: Annotated[str | None, Field(default=None, description="Filter: ebgp, ibgp, unnumbered.")],
    branch: Annotated[str | None, Field(default=None, description="Branch to read from (defaults to main).")],
) -> MCPResponse:
    """
    List BGP peering sessions filtered by datacenter, device, or session type.

    Returns: local/remote device, ASNs, type, status. Auto-compresses if >5 sessions.
    """
    try:
        client: InfrahubClient = require_client(ctx)
    except RuntimeError as exc:
        return await _log_and_return_error(ctx=ctx, error=str(exc), remediation="Start MCP with a configured client.")

    branch = branch or "main"
    filter_kwargs: dict[str, Any] = {}
    if device_name:
        filter_kwargs["local_device__name__value"] = device_name
    if session_type:
        filter_kwargs["type__value"] = session_type

    try:
        sessions = (
            await client.filters(kind=KIND_BGP_PEERING, branch=branch, prefetch_relationships=False, **filter_kwargs)
            if filter_kwargs
            else await client.all(kind=KIND_BGP_PEERING, branch=branch, prefetch_relationships=False)
        )
    except Exception as exc:  # noqa: BLE001
        return await _log_and_return_error(ctx=ctx, error=exc, remediation="BGP session query failed.")

    results: list[dict[str, Any]] = []
    for s in sessions:
        entry = slim_node(s, _BGP_FIELDS)
        entry["id"] = getattr(s, "id", None)

        if datacenter:
            local = str(entry.get("local_device") or "")
            remote = str(entry.get("remote_device") or "")
            if datacenter.upper() not in local.upper() and datacenter.upper() not in remote.upper():
                continue

        results.append(entry)

    if not results:
        return await _log_and_return_error(
            ctx=ctx,
            error="No BGP sessions found with the given filters.",
            remediation="Try without filters or verify device names with get_topology_devices.",
        )

    return await maybe_compress(ctx, results, "BGP sessions", "bgp_sessions_gcf") or MCPResponse(
        status=MCPToolStatus.SUCCESS, data=results
    )


# ---------------------------------------------------------------------------
# Tenant namespaces
# ---------------------------------------------------------------------------


@mcp.tool(
    tags={"topology", "namespaces", "retrieve"},
    annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True),
)
async def get_tenant_namespaces(
    ctx: Context,
    branch: Annotated[str | None, Field(default=None, description="Branch to read from (defaults to main).")],
) -> MCPResponse:
    """
    List IPAM namespaces (VRFs) with L3 VNI and type.

    Customer deployments share Infrahub's built-in "default" namespace — there is no
    per-customer VRF. Remaining namespaces (e.g. INTERNET, TRANSIT) are shared
    hub-and-spoke VRFs; ownership is derived from the segments that use them
    (ManagedNetworkSegment.owner), not stored on the namespace itself.
    Auto-compresses if >5 namespaces.
    """
    try:
        client: InfrahubClient = require_client(ctx)
    except RuntimeError as exc:
        return await _log_and_return_error(ctx=ctx, error=str(exc), remediation="Start MCP with a configured client.")

    branch = branch or "main"

    try:
        ns_nodes = await client.all(kind=KIND_NAMESPACE, branch=branch, prefetch_relationships=False)
    except Exception as exc:  # noqa: BLE001
        return await _log_and_return_error(ctx=ctx, error=exc, remediation="Namespace query failed.")

    if not ns_nodes:
        return await _log_and_return_error(
            ctx=ctx,
            error="No namespaces found.",
            remediation="Ensure the Infrahub schema is loaded and bootstrap data is present.",
        )

    results = [slim_node(ns, _NS_FIELDS) for ns in ns_nodes]

    return await maybe_compress(ctx, results, "namespaces", "namespaces_gcf") or MCPResponse(
        status=MCPToolStatus.SUCCESS, data=results
    )


# ---------------------------------------------------------------------------
# Fabric health check
# ---------------------------------------------------------------------------


@mcp.tool(
    tags={"topology", "health", "retrieve"},
    annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True),
)
async def get_fabric_health(
    ctx: Context,
    datacenter: Annotated[str, Field(description="Datacenter name to analyse (e.g. DC1).")],
    branch: Annotated[str | None, Field(default=None, description="Branch to read from (defaults to main).")],
) -> MCPResponse[dict[str, Any]]:
    """
    Analyse fabric health for a datacenter in one call.

    Runs in parallel:
      - Device status breakdown (active vs inactive vs planned)
      - BGP session status breakdown (established vs idle vs active)
      - Virtual device count (vFW, vLB)
      - Namespace and segment counts per customer

    Returns a compact health report — no full object dumps.
    """
    try:
        client: InfrahubClient = require_client(ctx)
    except RuntimeError as exc:
        return await _log_and_return_error(ctx=ctx, error=str(exc), remediation="Start MCP with a configured client.")

    branch = branch or "main"
    await ctx.info(f"Running parallel fabric health check for {datacenter}...")

    # All five queries fire at the same time
    devices_coro = client.filters(
        kind=KIND_DEVICE, branch=branch, prefetch_relationships=False, deployment__name__value=datacenter
    )
    bgp_coro = client.all(kind=KIND_BGP_PEERING, branch=branch, prefetch_relationships=False)
    vdev_coro = client.filters(
        kind=KIND_VIRTUAL_DEVICE, branch=branch, prefetch_relationships=False, deployment__value=datacenter
    )
    ns_coro = client.all(kind=KIND_NAMESPACE, branch=branch, prefetch_relationships=False)
    seg_coro = client.all(kind=KIND_VXLAN_SEGMENT, branch=branch, prefetch_relationships=False)

    devices, bgp_sessions, vdevices, namespaces, segments = await _gather_safe(
        devices_coro, bgp_coro, vdev_coro, ns_coro, seg_coro
    )
    devices = devices or []
    bgp_sessions = bgp_sessions or []
    vdevices = vdevices or []
    namespaces = namespaces or []
    segments = segments or []

    # Device status breakdown
    device_status: dict[str, int] = {}
    for d in devices:
        st = attr_val(d, "status", "unknown") or "unknown"
        device_status[st] = device_status.get(st, 0) + 1

    # BGP status breakdown (only sessions for this DC)
    bgp_status: dict[str, int] = {}
    for s in bgp_sessions:
        local = str(attr_val(s, "local_device", "") or "")
        remote = str(attr_val(s, "remote_device", "") or "")
        if datacenter.upper() not in local.upper() and datacenter.upper() not in remote.upper():
            continue
        st = attr_val(s, "status", "unknown") or "unknown"
        bgp_status[st] = bgp_status.get(st, 0) + 1

    # Virtual device role breakdown
    vdev_roles: dict[str, int] = {}
    for v in vdevices:
        role = attr_val(v, "role", "unknown") or "unknown"
        vdev_roles[role] = vdev_roles.get(role, 0) + 1

    # Namespace per owner
    ns_by_owner: dict[str, int] = {}
    for ns in namespaces:
        owner = attr_val(ns, "owner", "unknown") or "unknown"
        ns_by_owner[owner] = ns_by_owner.get(owner, 0) + 1

    # Segments per owner
    seg_by_owner: dict[str, int] = {}
    for seg in segments:
        owner = attr_val(seg, "owner", "unknown") or "unknown"
        seg_by_owner[owner] = seg_by_owner.get(owner, 0) + 1

    # Health score: ratio of active devices
    total_dev = len(devices)
    active_dev = device_status.get("active", 0)
    health_pct = round(active_dev / total_dev * 100) if total_dev else 0

    # BGP established ratio
    total_bgp = sum(bgp_status.values())
    est_bgp = bgp_status.get("established", 0)
    bgp_health_pct = round(est_bgp / total_bgp * 100) if total_bgp else 0

    return MCPResponse(
        status=MCPToolStatus.SUCCESS,
        data={
            "datacenter": datacenter,
            "health_score": {
                "device_active_pct": health_pct,
                "bgp_established_pct": bgp_health_pct,
            },
            "devices": {
                "total": total_dev,
                "by_status": device_status,
            },
            "bgp": {
                "total": total_bgp,
                "by_status": bgp_status,
            },
            "virtual_devices": {
                "total": len(vdevices),
                "by_role": vdev_roles,
            },
            "tenants": {
                "namespace_count": len(namespaces),
                "namespaces_by_owner": ns_by_owner,
                "segment_count": len(segments),
                "segments_by_owner": seg_by_owner,
            },
        },
    )


# ---------------------------------------------------------------------------
# Segment inventory
# ---------------------------------------------------------------------------


@mcp.tool(
    tags={"topology", "segments", "retrieve"},
    annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True),
)
async def get_segment_inventory(
    ctx: Context,
    customer_id: Annotated[str | None, Field(default=None, description="Filter by customer owner (e.g. C001).")],
    environment: Annotated[str | None, Field(default=None, description="Filter by environment: p, n, d.")],
    include_vlan: Annotated[bool, Field(default=False, description="Include VLAN segments in addition to VXLAN.")],
    branch: Annotated[str | None, Field(default=None, description="Branch to read from (defaults to main).")],
) -> MCPResponse:
    """
    List VXLAN (and optionally VLAN) segments across all tenants.

    Runs VXLAN and VLAN queries in parallel when include_vlan=True.
    Returns: customer_name, owner, environment, pbr_enabled per segment.
    Auto-compresses if >5 segments.
    """
    try:
        client: InfrahubClient = require_client(ctx)
    except RuntimeError as exc:
        return await _log_and_return_error(ctx=ctx, error=str(exc), remediation="Start MCP with a configured client.")

    branch = branch or "main"

    vxlan_filter: dict[str, Any] = {}
    if customer_id:
        vxlan_filter["owner__org_id__value"] = customer_id.upper()
    if environment:
        vxlan_filter["environment__value"] = environment

    vxlan_coro = (
        client.filters(kind=KIND_VXLAN_SEGMENT, branch=branch, prefetch_relationships=False, **vxlan_filter)
        if vxlan_filter
        else client.all(kind=KIND_VXLAN_SEGMENT, branch=branch, prefetch_relationships=False)
    )

    if include_vlan:
        vlan_filter: dict[str, Any] = {}
        if customer_id:
            vlan_filter["owner__org_id__value"] = customer_id.upper()
        vlan_coro = (
            client.filters(kind=KIND_VLAN_SEGMENT, branch=branch, prefetch_relationships=False, **vlan_filter)
            if vlan_filter
            else client.all(kind=KIND_VLAN_SEGMENT, branch=branch, prefetch_relationships=False)
        )
        vxlan_nodes, vlan_nodes = await _gather_safe(vxlan_coro, vlan_coro)
    else:
        (vxlan_nodes,) = await _gather_safe(vxlan_coro)
        vlan_nodes = []

    vxlan_nodes = vxlan_nodes or []
    vlan_nodes = vlan_nodes or []

    results: list[dict[str, Any]] = []
    for seg in vxlan_nodes:
        entry = slim_node(seg, _SEG_FIELDS)
        entry["type"] = "vxlan"
        results.append(entry)

    for seg in vlan_nodes or []:
        entry = slim_node(seg, _SEG_FIELDS)
        entry["type"] = "vlan"
        results.append(entry)

    if not results:
        return await _log_and_return_error(
            ctx=ctx,
            error="No segments found with the given filters.",
            remediation="Try without filters or use discover_customer_options to see existing customers.",
        )

    return await maybe_compress(ctx, results, "segments", "segments_gcf") or MCPResponse(
        status=MCPToolStatus.SUCCESS, data=results
    )


# ---------------------------------------------------------------------------
# Topology diagram (Mermaid)
# ---------------------------------------------------------------------------

_ROLE_SHAPE: dict[str, tuple[str, str]] = {
    "super-spine": ("[", "]"),
    "spine": ("[", "]"),
    "border-leaf": ("[", "]"),
    "leaf": ("[", "]"),
    "l2-leaf": ("[", "]"),
    "firewall-active": ("([", "])"),
    "firewall-passive": ("([", "])"),
    "firewall": ("([", "])"),
    "load-balancer": ("([", "])"),
    "server": ("(", ")"),
    # Internet / cloud — Mermaid uses cloud-like stadium shape
    "internet": (">", "]"),
    "isp": (">", "]"),
    "saas": (">", "]"),
    "cloud-provider": (">", "]"),
    "cloud": (">", "]"),
    "ixp": (">", "]"),
    "partner": (">", "]"),
}

_ROLE_STYLE: dict[str, str] = {
    "super-spine": "fill:#1a3a5c,color:#fff,stroke:#0d2137",
    "spine": "fill:#2d6a8a,color:#fff,stroke:#1a4a6a",
    "border-leaf": "fill:#1a5c3a,color:#fff,stroke:#0d3a22",
    "leaf": "fill:#3a8a5c,color:#fff,stroke:#1a5c3a",
    "l2-leaf": "fill:#4a9a6a,color:#fff,stroke:#2a6a4a",
    "firewall-active": "fill:#8a2d2d,color:#fff,stroke:#5c1a1a",
    "firewall-passive": "fill:#8a2d2d,color:#fff,stroke:#5c1a1a",
    "firewall": "fill:#8a2d2d,color:#fff,stroke:#5c1a1a",
    "load-balancer": "fill:#7a5c1a,color:#fff,stroke:#5c3a00",
    "server": "fill:#4a4a4a,color:#fff,stroke:#2a2a2a",
    "internet": "fill:#555555,color:#fff,stroke:#333333",
    "isp": "fill:#555555,color:#fff,stroke:#333333",
    "saas": "fill:#e65c00,color:#fff,stroke:#b34700",
    "cloud-provider": "fill:#1565c0,color:#fff,stroke:#0d47a1",
    "cloud": "fill:#1565c0,color:#fff,stroke:#0d47a1",
    "ixp": "fill:#4a148c,color:#fff,stroke:#311b92",
    "partner": "fill:#1a5c3a,color:#fff,stroke:#0d3a22",
}

# Numeric layer index for each role — lower = closer to WAN edge.
# Used for diagram layout and max_role filtering.
_ROLE_LAYER: dict[str, int] = {
    "border-leaf": 0,
    "super-spine": 1,
    "firewall-active": 2,
    "firewall-passive": 2,
    "firewall": 2,
    "load-balancer": 3,
    "spine": 4,
    "leaf": 5,
    "l2-leaf": 6,
    "server": 7,
    "endpoint": 7,
    "internet": 8,
    "isp": 8,
    "saas": 8,
    "cloud-provider": 8,
    "cloud": 8,
    "ixp": 8,
    "partner": 8,
    "uplink": 8,
}


def _node_id(name: str) -> str:
    return name.replace("-", "_").replace(".", "_").replace("/", "_")


def _mermaid_node(name: str, role: str | None, label: str | None = None) -> str:
    r = (role or "").lower()
    open_b, close_b = _ROLE_SHAPE.get(r, ("[", "]"))
    display = label or name
    nid = _node_id(name)
    return f'    {nid}{open_b}"{display}"{close_b}'


_CLOUD_ROLES = {"internet", "isp", "saas", "cloud-provider", "cloud", "ixp", "partner", "uplink"}


def _group_devices(devices: list[dict]) -> dict[str, list[dict]]:
    """Group devices by pod prefix (e.g. POD1, POD2) or CORE / SEC / CLOUD."""
    groups: dict[str, list[dict]] = {}
    for d in devices:
        name = d.get("name", "")
        role = (d.get("role") or "").lower()
        if role in _CLOUD_ROLES:
            key = "CLOUD"
        elif role in ("firewall-active", "firewall-passive", "firewall", "load-balancer"):
            key = "SEC"
        elif "POD" in name.upper():
            parts = name.upper().split("-")
            for p in parts:
                if p.startswith("POD"):
                    key = p
                    break
            else:
                key = "CORE"
        elif role in ("super-spine", "border-leaf"):
            key = "CORE"
        else:
            key = "OTHER"
        groups.setdefault(key, []).append(d)
    return groups


def _generate_drawio_xml(
    dev_map: dict[str, dict],
    edge_list: Sequence[tuple[Any, ...]],  # (a, b, ?, iface_a, iface_b, edge_type) — trailing items optional
    datacenter: str,
) -> str:
    """Render datacenter topology as Draw.io XML with flat hierarchical layer layout.

    Nodes arranged in horizontal rows top-to-bottom by network role:
      BL → super-spine → firewall → load-balancer → spine → leaf → l2-leaf → server
    """
    NODE_W = 150
    NODE_H = 44
    H_GAP = 24
    V_GAP = 90
    LABEL_W = 110
    CANVAS_PAD_X = 30
    CANVAS_PAD_Y = 40

    ROLE_LAYER = _ROLE_LAYER
    LAYER_LABEL: dict[int, str] = {
        0: "Border Leaf",
        1: "Super-Spine",
        2: "Firewall",
        3: "Load Balancer",
        4: "Spine",
        5: "Leaf",
        6: "L2 Leaf",
        7: "Servers",
        8: "Internet / Cloud / External",
    }
    LAYER_BAND_COLOR: dict[int, str] = {
        0: "#e8f5e9",
        1: "#e3f2fd",
        2: "#ffebee",
        3: "#fff8e1",
        4: "#e1f5fe",
        5: "#f1f8e9",
        6: "#f3e5f5",
        7: "#f5f5f5",
        8: "#eeeeee",
    }
    # Draw.io cloud shape — used for internet/ISP/SaaS/cloud-provider nodes
    _CLOUD_SHAPE = (
        "shape=mxgraph.cisco.sites.generic_cloud;fillColor={fill};fontColor=#ffffff;strokeColor={stroke};fontStyle=1;"
    )

    DRAWIO_ROLE_STYLE: dict[str, str] = {
        "border-leaf": "rounded=1;arcSize=20;fillColor=#1a5c3a;fontColor=#ffffff;strokeColor=#0d3a22;",
        "super-spine": "rounded=1;arcSize=20;fillColor=#1a3a5c;fontColor=#ffffff;strokeColor=#0d2137;",
        "spine": "rounded=1;arcSize=20;fillColor=#2d6a8a;fontColor=#ffffff;strokeColor=#1a4a6a;",
        "leaf": "rounded=1;arcSize=20;fillColor=#3a8a5c;fontColor=#ffffff;strokeColor=#1a5c3a;",
        "l2-leaf": "rounded=1;arcSize=20;fillColor=#4a9a6a;fontColor=#ffffff;strokeColor=#2a6a4a;",
        "firewall-active": "ellipse;fillColor=#8a2d2d;fontColor=#ffffff;strokeColor=#5c1a1a;",
        "firewall-passive": "ellipse;fillColor=#8a2d2d;fontColor=#ffffff;strokeColor=#5c1a1a;",
        "firewall": "ellipse;fillColor=#8a2d2d;fontColor=#ffffff;strokeColor=#5c1a1a;",
        "load-balancer": "ellipse;fillColor=#7a5c1a;fontColor=#ffffff;strokeColor=#5c3a00;",
        "server": "rounded=1;arcSize=10;fillColor=#4a4a4a;fontColor=#ffffff;strokeColor=#2a2a2a;",
        "endpoint": "rounded=1;arcSize=10;fillColor=#4a4a4a;fontColor=#ffffff;strokeColor=#2a2a2a;",
        # Internet / cloud boundary nodes — rendered as clouds
        "internet": _CLOUD_SHAPE.format(fill="#555555", stroke="#333333"),
        "isp": _CLOUD_SHAPE.format(fill="#555555", stroke="#333333"),
        "saas": _CLOUD_SHAPE.format(fill="#e65c00", stroke="#b34700"),
        "cloud-provider": _CLOUD_SHAPE.format(fill="#1565c0", stroke="#0d47a1"),
        "cloud": _CLOUD_SHAPE.format(fill="#1565c0", stroke="#0d47a1"),
        "ixp": _CLOUD_SHAPE.format(fill="#4a148c", stroke="#311b92"),
        "partner": _CLOUD_SHAPE.format(fill="#1a5c3a", stroke="#0d3a22"),
        "uplink": _CLOUD_SHAPE.format(fill="#555555", stroke="#333333"),
    }
    DEFAULT_STYLE = "rounded=1;fillColor=#dae8fc;strokeColor=#6c8ebf;"

    # Bucket devices into layers
    layers: dict[int, list[dict]] = {}
    for d in dev_map.values():
        role = (d.get("role") or "").lower()
        layer = ROLE_LAYER.get(role, 5)
        layers.setdefault(layer, []).append(d)

    def _sort_key(d: dict) -> tuple[str, str]:
        name = d["name"]
        pod = ""
        for part in name.upper().split("-"):
            if part.startswith("POD"):
                pod = part
                break
        return (pod, name)

    for layer, nodes_in_layer in layers.items():
        nodes_in_layer.sort(key=_sort_key)

    active_layers = sorted(layers.keys())
    max_devs = max(len(devs) for devs in layers.values())

    # Canvas width: label column + widest row
    content_w = max_devs * NODE_W + (max_devs - 1) * H_GAP
    canvas_w = CANVAS_PAD_X + LABEL_W + content_w + CANVAS_PAD_X
    total_layers = len(active_layers)
    canvas_h = CANVAS_PAD_Y + total_layers * NODE_H + (total_layers - 1) * V_GAP + CANVAS_PAD_Y + 20

    # Compute y per layer index (sequential, skipping empty)
    layer_y: dict[int, float] = {}
    y_cur = float(CANVAS_PAD_Y)
    for layer in active_layers:
        layer_y[layer] = y_cur
        y_cur += NODE_H + V_GAP

    nodes_x_start = float(CANVAS_PAD_X + LABEL_W)

    cells: list[str] = ['<mxCell id="0" />', '<mxCell id="1" parent="0" />']
    edge_counter = [0]

    # Draw background bands + layer labels
    for layer in active_layers:
        y = layer_y[layer] - 12
        band_h = NODE_H + 24
        band_color = LAYER_BAND_COLOR.get(layer, "#f9f9f9")
        cells.append(
            f'<mxCell id="band_{layer}" value="" '
            f'style="rounded=0;fillColor={band_color};strokeColor=none;opacity=60;" '
            f'vertex="1" parent="1">\n'
            f'  <mxGeometry x="{CANVAS_PAD_X:.0f}" y="{y:.0f}" '
            f'width="{canvas_w - 2 * CANVAS_PAD_X:.0f}" height="{band_h:.0f}" as="geometry" />\n'
            f"</mxCell>"
        )
        label_text = LAYER_LABEL.get(layer, "")
        cells.append(
            f'<mxCell id="lbl_{layer}" value="{label_text}" '
            f'style="text;html=1;align=right;verticalAlign=middle;fontStyle=1;fontSize=11;'
            f'fontColor=#444444;strokeColor=none;fillColor=none;" '
            f'vertex="1" parent="1">\n'
            f'  <mxGeometry x="{CANVAS_PAD_X:.0f}" y="{layer_y[layer]:.0f}" '
            f'width="{LABEL_W - 12:.0f}" height="{NODE_H:.0f}" as="geometry" />\n'
            f"</mxCell>"
        )

    # Draw device nodes
    for layer in active_layers:
        devs = layers[layer]
        row_w = len(devs) * NODE_W + (len(devs) - 1) * H_GAP
        x_start = nodes_x_start + (content_w - row_w) / 2
        y = layer_y[layer]
        for i, d in enumerate(devs):
            name = d["name"]
            role = (d.get("role") or "").lower()
            style = DRAWIO_ROLE_STYLE.get(role, DEFAULT_STYLE)
            nid = _node_id(name)
            lx = x_start + i * (NODE_W + H_GAP)
            cells.append(
                f'<mxCell id="{nid}" value="{name}" '
                f'style="{style}fontSize=10;whiteSpace=wrap;html=1;fontStyle=1;" '
                f'vertex="1" parent="1">\n'
                f'  <mxGeometry x="{lx:.0f}" y="{y:.0f}" '
                f'width="{NODE_W}" height="{NODE_H}" as="geometry" />\n'
                f"</mxCell>"
            )

    # Draw edges
    # edge_list items: (a, b, name, iface_a, iface_b, edge_type)
    # edge_type: "cable" (solid) | "physical_circuit" (dashed blue) | "virtual_circuit" (dotted orange)
    _EDGE_STYLES = {
        "cable": "rounded=0;orthogonalLoop=1;jettySize=auto;html=1;strokeColor=#888888;strokeWidth=1.5;",
        "physical_circuit": "rounded=0;orthogonalLoop=1;jettySize=auto;html=1;strokeColor=#1565c0;strokeWidth=2;dashed=1;dashPattern=8 4;",
        "virtual_circuit": "rounded=0;orthogonalLoop=1;jettySize=auto;html=1;strokeColor=#e65c00;strokeWidth=2;dashed=1;dashPattern=4 4;",
    }
    _IFACE_STYLE = (
        "edgeLabel;html=1;align=center;verticalAlign=middle;"
        "resizable=0;points=[];fontSize=9;fontColor=#444444;"
        "labelBackgroundColor=#ffffffcc;labelBorderColor=none;"
    )
    seen: set[frozenset] = set()
    for edge_item in edge_list:
        a, b = edge_item[0], edge_item[1]
        iface_a = edge_item[3] if len(edge_item) > 3 else ""
        iface_b = edge_item[4] if len(edge_item) > 4 else ""
        edge_type = edge_item[5] if len(edge_item) > 5 else "cable"
        key = frozenset([a, b])
        if key in seen:
            continue
        seen.add(key)
        if a not in dev_map or b not in dev_map:
            continue
        edge_counter[0] += 1
        eid = f"e{edge_counter[0]}"
        style = _EDGE_STYLES.get(edge_type, _EDGE_STYLES["cable"])
        cells.append(
            f'<mxCell id="{eid}" value="" '
            f'style="{style}" '
            f'edge="1" source="{_node_id(a)}" target="{_node_id(b)}" parent="1">\n'
            f'  <mxGeometry relative="1" as="geometry" />\n'
            f"</mxCell>"
        )
        if iface_a:
            cells.append(
                f'<mxCell id="{eid}_sa" value="{iface_a}" style="{_IFACE_STYLE}" '
                f'vertex="1" connectable="0" parent="{eid}">\n'
                f'  <mxGeometry x="-0.7" relative="1" as="geometry" />\n'
                f"</mxCell>"
            )
        if iface_b:
            cells.append(
                f'<mxCell id="{eid}_ta" value="{iface_b}" style="{_IFACE_STYLE}" '
                f'vertex="1" connectable="0" parent="{eid}">\n'
                f'  <mxGeometry x="0.7" relative="1" as="geometry" />\n'
                f"</mxCell>"
            )

    cells_xml = "\n    ".join(cells)
    # dx/dy: half canvas size — centers the initial viewport on the diagram
    dx = int(canvas_w / 2)
    dy = int(canvas_h / 2)
    return (
        f'<mxfile host="app.diagrams.net">\n'
        f'  <diagram name="{datacenter} Topology">\n'
        f'    <mxGraphModel dx="{dx}" dy="{dy}" grid="0" gridSize="10" guides="1" '
        f'tooltips="1" connect="1" arrows="1" fold="1" page="1" pageScale="1" '
        f'pageWidth="{canvas_w:.0f}" pageHeight="{canvas_h:.0f}" math="0" shadow="0">\n'
        f"      <root>\n    {cells_xml}\n      </root>\n"
        f"    </mxGraphModel>\n  </diagram>\n</mxfile>"
    )


@mcp.tool(
    tags={"topology", "diagram", "retrieve"},
    annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True),
)
async def generate_topology_diagram(
    ctx: Context,
    datacenter: Annotated[str, Field(description="Datacenter name to diagram (e.g. DC1, DC3).")],
    pod: Annotated[
        str | None,
        Field(
            default=None,
            description=(
                "Optional pod filter. Accepts a full deployment name (e.g. 'DC1-1-POD-1') or a "
                "short label that is matched case-insensitively against pod names (e.g. 'pod1', 'POD-1'). "
                "When set, only devices in that pod are included."
            ),
        ),
    ],
    include_servers: Annotated[
        bool,
        Field(default=False, description="Include server/compute nodes (default: false — keeps diagram readable)."),
    ],
    max_role: Annotated[
        str | None,
        Field(
            default=None,
            description=(
                "Optional role ceiling. Only devices whose role is at or above this layer are included. "
                "Example values: 'leaf' (border-leaf + firewall + spine + leaf), 'spine' (no leafs), "
                "'border-leaf' (only border-leafs). Roles: border-leaf, super-spine, firewall, "
                "load-balancer, spine, leaf, l2-leaf, server. Overrides include_servers when set."
            ),
        ),
    ],
    format: Annotated[str, Field(default="mermaid", description="Output format: 'mermaid' (default) or 'drawio'.")],
    branch: Annotated[str | None, Field(default=None, description="Branch to read from (defaults to main).")],
) -> MCPResponse[dict[str, Any]]:
    """
    Generate a Mermaid or Draw.io diagram of a datacenter (or a single pod).

    Pass pod='POD1' (or the full deployment name) to restrict the diagram to
    that pod only — useful for large DCs where the full diagram is too busy.

    Pass max_role='leaf' to show only devices up to and including the leaf layer,
    or max_role='spine' to show only the core fabric without leafs.
    """
    try:
        client: InfrahubClient = require_client(ctx)
    except RuntimeError as exc:
        return await _log_and_return_error(ctx=ctx, error=str(exc), remediation="Start MCP with a configured client.")

    branch = branch or "main"
    label = f"{datacenter}/{pod}" if pod else datacenter
    await ctx.info(f"Generating topology diagram for {label}")

    # Fetch the DC deployment tree to discover pod names
    gql_dep_tree = f"""
    query {{
        TopologyDeployment(name__value: "{datacenter}") {{
            edges {{ node {{
                name {{ value }}
                children {{ edges {{ node {{ name {{ value }} }} }} }}
            }} }}
        }}
    }}
    """
    dep_tree = await client.execute_graphql(query=gql_dep_tree, branch_name=branch)
    all_pod_names: list[str] = []
    for e in (dep_tree or {}).get("TopologyDeployment", {}).get("edges", []):
        node = e.get("node", {})
        for child_e in (node.get("children") or {}).get("edges", []):
            child_name = (child_e.get("node", {}).get("name") or {}).get("value") or ""
            if child_name:
                all_pod_names.append(child_name)

    # Resolve pod filter → deployment name(s) to include
    if pod:
        pod_norm = pod.upper().replace("-", "").replace("_", "").replace(" ", "")
        matched = [
            p
            for p in all_pod_names
            if p.upper().replace("-", "").replace("_", "") == pod_norm or p.upper() == pod.upper()
        ]
        if not matched:
            available = ", ".join(all_pod_names) or "none found"
            return await _log_and_return_error(
                ctx=ctx,
                error=f"Pod '{pod}' not found in {datacenter}. Available: {available}",
                remediation="Use the full deployment name (e.g. 'DC1-1-POD-1') or a matching short label.",
            )
        all_deployment_names = matched
    else:
        # Full DC: include DC-level + all pod deployments
        all_deployment_names = [datacenter] + all_pod_names

    if not all_deployment_names:
        all_deployment_names = [datacenter]

    dep_filter = str(all_deployment_names).replace("'", '"')

    gql_devices = f"""
    query {{
        DcimPhysicalDevice(deployment__name__values: {dep_filter}) {{
            edges {{ node {{
                name {{ value }}
                role {{ value }}
            }} }}
        }}
    }}
    """
    gql_cables = f"""
    query {{
        DcimCable(deployment__name__values: {dep_filter}) {{
            edges {{ node {{
                name {{ value }}
                endpoints {{
                    edges {{ node {{
                        ... on DcimPhysicalInterface {{
                            device {{ node {{ name {{ value }} }} }}
                        }}
                    }} }}
                }}
            }} }}
        }}
    }}
    """
    try:
        dev_result, cable_result = await _gather_safe(
            client.execute_graphql(query=gql_devices, branch_name=branch),
            client.execute_graphql(query=gql_cables, branch_name=branch),
        )
    except Exception as exc:  # noqa: BLE001
        return await _log_and_return_error(ctx=ctx, error=exc, remediation="Device/cable query failed.")

    raw_devices = (dev_result or {}).get("DcimPhysicalDevice", {}).get("edges", [])
    raw_cables = (cable_result or {}).get("DcimCable", {}).get("edges", [])

    if not raw_devices:
        return await _log_and_return_error(
            ctx=ctx,
            error=f"No devices found for datacenter '{datacenter}'.",
            remediation="Verify the datacenter name with get_topology_summary.",
        )

    # Resolve max_role → numeric layer ceiling
    max_layer: int | None = None
    if max_role:
        max_role_norm = max_role.lower().replace("_", "-")
        max_layer = _ROLE_LAYER.get(max_role_norm)
        if max_layer is None:
            available_roles = ", ".join(sorted(_ROLE_LAYER, key=_ROLE_LAYER.__getitem__))
            return await _log_and_return_error(
                ctx=ctx,
                error=f"Unknown max_role '{max_role}'. Valid values: {available_roles}",
                remediation="Use a role name from the list, e.g. 'leaf', 'spine', 'border-leaf'.",
            )

    # Build device map: name → {name, role}
    dev_map: dict[str, dict] = {}
    for edge in raw_devices:
        node = edge.get("node", {})
        name = (node.get("name") or {}).get("value") or ""
        role = (node.get("role") or {}).get("value") or ""
        if not name:
            continue
        role_lower = role.lower()
        if max_layer is not None:
            if _ROLE_LAYER.get(role_lower, 999) > max_layer:
                continue
        elif not include_servers and role_lower in ("server", "endpoint"):
            continue
        if role_lower == "patch-panel":
            continue
        dev_map[name] = {"name": name, "role": role}

    # Build edge list from cables — extract device names from each endpoint
    edges: list[tuple[str, str, str]] = []
    for cable_edge in raw_cables:
        node = cable_edge.get("node", {})
        cname = (node.get("name") or {}).get("value") or ""
        ep_edges = (node.get("endpoints") or {}).get("edges", [])
        ep_devices: list[str] = []
        for ep in ep_edges:
            dev_node = ((ep.get("node") or {}).get("device") or {}).get("node") or {}
            dev_name = (dev_node.get("name") or {}).get("value") or ""
            if dev_name:
                ep_devices.append(dev_name)
        if len(ep_devices) >= 2:
            a_dev, b_dev = ep_devices[0], ep_devices[1]
            if a_dev in dev_map and b_dev in dev_map:
                edges.append((a_dev, b_dev, cname))

    if format == "drawio":
        diagram = _generate_drawio_xml(dev_map, edges, label)
        seen_edges: set[frozenset] = set()
        for a, b, _ in edges:
            seen_edges.add(frozenset([a, b]))
        return MCPResponse(
            status=MCPToolStatus.SUCCESS,
            data={
                "datacenter": datacenter,
                "pod": pod,
                "max_role": max_role,
                "devices": len(dev_map),
                "cables": len(seen_edges),
                "drawio": diagram,
            },
        )

    # --- Mermaid output ---
    groups = _group_devices(list(dev_map.values()))
    _GROUP_LABELS = {
        "CORE": "Fabric Core (BL + Super-Spines)",
        "SEC": "Security Service Chain",
        "CLOUD": "Internet / Cloud / External",
    }
    ordered_keys = (
        ["CORE", "SEC"]
        + sorted(k for k in groups if k.startswith("POD"))
        + sorted(k for k in groups if k not in ("CORE", "SEC", "CLOUD", "OTHER") and not k.startswith("POD"))
        + [k for k in ("OTHER", "CLOUD") if k in groups]
    )
    lines: list[str] = ["graph TD"]
    style_lines: list[str] = []
    for grp_key in ordered_keys:
        devs = groups.get(grp_key)
        if not devs:
            continue
        label = _GROUP_LABELS.get(grp_key, f"{grp_key} — {devs[0].get('role', '')} rack" if devs else grp_key)
        lines.append(f'  subgraph {grp_key}["{label}"]')
        for d in devs:
            lines.append(_mermaid_node(d["name"], d["role"]))
            role_key = (d["role"] or "").lower()
            if role_key in _ROLE_STYLE:
                style_lines.append(f"  style {_node_id(d['name'])} {_ROLE_STYLE[role_key]}")
        lines.append("  end")
        lines.append("")

    seen_edges2: set[frozenset] = set()
    for a, b, cname in edges:
        key = frozenset([a, b])
        if key in seen_edges2:
            continue
        seen_edges2.add(key)
        short = cname.replace("CBL-", "").replace(f"{datacenter}-", "")
        lines.append(f"  {_node_id(a)} ---|{short}| {_node_id(b)}")

    lines.append("")
    lines.extend(style_lines)
    diagram = "\n".join(lines)

    return MCPResponse(
        status=MCPToolStatus.SUCCESS,
        data={
            "datacenter": datacenter,
            "pod": pod,
            "max_role": max_role,
            "devices": len(dev_map),
            "cables": len(seen_edges2),
            "mermaid": diagram,
        },
    )


# ---------------------------------------------------------------------------
# Cluster topology diagram
# ---------------------------------------------------------------------------

_POOL_TYPE_LAYER: dict[str, int] = {
    "control_plane": 0,
    "worker": 1,
    "hypervisor": 1,
    "storage": 2,
    "management": 3,
}

_POOL_TYPE_LABEL: dict[str, str] = {
    "control_plane": "Control Plane",
    "worker": "Workers",
    "hypervisor": "Hypervisor (Compute)",
    "storage": "Storage",
    "management": "Management",
}

_POOL_TYPE_COLOR: dict[str, str] = {
    "control_plane": "#1a3a5c",
    "worker": "#2d6a8a",
    "hypervisor": "#1a5c3a",
    "storage": "#7a5c1a",
    "management": "#4a148c",
}

_PLATFORM_HEADER_COLOR: dict[str, str] = {
    "kubernetes": "#326ce5",
    "eks": "#326ce5",
    "aks": "#326ce5",
    "vmware_vsphere": "#717070",
    "nutanix": "#024da1",
}


def _cluster_drawio_xml(clusters: list[dict], datacenter: str | None) -> str:
    """Render one or more VirtClusters as Draw.io swimlane columns."""
    NODE_W = 220
    NODE_H = 56
    V_GAP = 16
    COL_GAP = 40
    TITLE_H = 32
    PAD_X = 20
    PAD_Y = 16
    CANVAS_PAD = 40

    # Sort pools within each cluster by layer
    for cl in clusters:
        cl["pools"].sort(key=lambda p: _POOL_TYPE_LAYER.get((p.get("pool_type") or "").lower(), 9))

    # Compute column heights
    def _col_h(cl: dict) -> float:
        pools = cl["pools"]
        return TITLE_H + PAD_Y + len(pools) * NODE_H + (len(pools) - 1) * V_GAP + PAD_Y

    max_h = max((_col_h(cl) for cl in clusters), default=200.0)
    cells: list[str] = ['<mxCell id="0" />', '<mxCell id="1" parent="0" />']
    cell_id = [10]

    def _cid() -> str:
        cell_id[0] += 1
        return str(cell_id[0])

    x_cur = float(CANVAS_PAD)
    for cl in clusters:
        name = cl["name"]
        platform = (cl.get("platform") or "kubernetes").lower()
        version = cl.get("version") or ""
        ha = cl.get("ha_enabled", False)
        col_h = _col_h(cl)
        hdr_color = _PLATFORM_HEADER_COLOR.get(platform, "#326ce5")

        # Swimlane container
        swim_id = _cid()
        ha_badge = " ✦HA" if ha else ""
        plat_label = platform.upper().replace("_", " ")
        header_text = f"{name}\\n{plat_label} {version}{ha_badge}"
        cells.append(
            f'<mxCell id="{swim_id}" value="{header_text}" '
            f'style="swimlane;startSize={TITLE_H};fillColor={hdr_color};fontColor=#ffffff;'
            f'strokeColor=#cccccc;fontStyle=1;fontSize=11;swimlaneLine=1;" '
            f'vertex="1" parent="1">\n'
            f'  <mxGeometry x="{x_cur:.0f}" y="{CANVAS_PAD:.0f}" '
            f'width="{NODE_W + 2 * PAD_X:.0f}" height="{col_h:.0f}" as="geometry" />\n'
            f"</mxCell>"
        )

        y_off = float(TITLE_H + PAD_Y)
        for pool in cl["pools"]:
            pool_type = (pool.get("pool_type") or "worker").lower()
            pool_name = pool.get("name") or pool_type
            nc = pool.get("node_count")
            min_s = pool.get("min_size")
            max_s = pool.get("max_size")
            flavor = pool.get("instance_flavor") or ""
            scaling = (pool.get("scaling_mode") or "static").lower()

            if scaling == "autoscaling" and min_s and max_s:
                count_str = f"{min_s}–{max_s} nodes (auto)"
            elif nc:
                count_str = f"×{nc} nodes"
            else:
                count_str = "?"

            fill = _POOL_TYPE_COLOR.get(pool_type, "#555555")
            type_label = _POOL_TYPE_LABEL.get(pool_type, pool_type.replace("_", " ").title())
            flavor_str = f"\\n{flavor}" if flavor else ""
            label = f"{type_label}\\n{pool_name}\\n{count_str}{flavor_str}"

            cells.append(
                f'<mxCell id="{_cid()}" value="{label}" '
                f'style="rounded=1;arcSize=10;fillColor={fill};fontColor=#ffffff;'
                f'strokeColor=#333333;fontSize=10;whiteSpace=wrap;html=1;verticalAlign=middle;" '
                f'vertex="1" parent="{swim_id}">\n'
                f'  <mxGeometry x="{PAD_X:.0f}" y="{y_off:.0f}" '
                f'width="{NODE_W:.0f}" height="{NODE_H:.0f}" as="geometry" />\n'
                f"</mxCell>"
            )
            y_off += NODE_H + V_GAP

        x_cur += NODE_W + 2 * PAD_X + COL_GAP

    canvas_w = int(x_cur + CANVAS_PAD)
    canvas_h = int(max_h + 2 * CANVAS_PAD + 20)
    cells_xml = "\n    ".join(cells)
    title = f"{datacenter} Clusters" if datacenter else "Cluster Topology"
    dx = canvas_w // 2
    dy = canvas_h // 2
    return (
        f'<mxfile host="app.diagrams.net">\n'
        f'  <diagram name="{title}">\n'
        f'    <mxGraphModel dx="{dx}" dy="{dy}" grid="0" gridSize="10" guides="1" '
        f'tooltips="1" connect="1" arrows="1" fold="1" page="1" pageScale="1" '
        f'pageWidth="{canvas_w}" pageHeight="{canvas_h}" math="0" shadow="0">\n'
        f"      <root>\n    {cells_xml}\n      </root>\n"
        f"    </mxGraphModel>\n  </diagram>\n</mxfile>"
    )


def _bfs_paths(
    graph: dict[str, set[str]],
    source: str,
    target: str,
) -> list[list[str]]:
    """BFS returning ALL shortest paths from source to target.

    Uses a parent-map approach so multiple equal-length paths through
    the same intermediate node are all captured.
    """
    if source not in graph or target not in graph:
        return []

    # parents[node] = set of nodes that reach it on a shortest path
    parents: dict[str, set[str]] = {source: set()}
    level: set[str] = {source}
    found = False

    while level and not found:
        next_level: set[str] = set()
        for node in level:
            for neighbour in graph.get(node, set()):
                if neighbour not in parents:
                    parents[neighbour] = {node}
                    next_level.add(neighbour)
                    if neighbour == target:
                        found = True
                elif neighbour in next_level:
                    # Same level — additional parent for this node
                    parents[neighbour].add(node)
        level = next_level

    if target not in parents:
        return []

    # Reconstruct all paths back from target to source
    paths: list[list[str]] = []

    def _reconstruct(node: str, path: list[str]) -> None:
        if node == source:
            paths.append([source] + path[::-1])
            return
        for parent in parents[node]:
            _reconstruct(parent, path + [node])

    _reconstruct(target, [])
    return paths


@mcp.tool(
    tags={"topology", "diagram", "path", "retrieve"},
    annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True),
)
async def generate_path_diagram(
    ctx: Context,
    source: Annotated[str, Field(description="Source device name (e.g. DC1-BL1).")],
    target: Annotated[str, Field(description="Target device name (e.g. DC1-POD1-SRV-01).")],
    branch: Annotated[str | None, Field(default=None, description="Branch to query (defaults to main).")] = None,
) -> MCPResponse[dict[str, Any]]:
    """
    Generate a Draw.io diagram showing all shortest paths between two devices.

    Uses InfrahubClient.traverse_paths() to BFS over cables, physical circuits, and
    virtual circuits. Device roles are fetched via GraphQL for diagram coloring.
    """
    from franc.tools.graph import _name_to_uuid, _sdk_paths_to_hops, _sdk_rel_filters

    client = require_client(ctx)
    await ctx.info(f"Finding paths from {source} to {target}")

    src_uuid = await _name_to_uuid(client, source, branch)
    dst_uuid = await _name_to_uuid(client, target, branch)
    if not src_uuid or not dst_uuid:
        return await _log_and_return_error(
            ctx=ctx,
            error=f"Device '{source if not src_uuid else target}' not found.",
            remediation="Check the device name with get_topology_devices().",
        )

    rel_filters = _sdk_rel_filters(["cable", "physical_circuit", "virtual_circuit", "cloud_circuit"])
    try:
        result = await client.traverse_paths(
            src_uuid,
            dst_uuid,
            relationship_filter=rel_filters,
            branch=branch,
        )
    except Exception as exc:  # noqa: BLE001
        return await _log_and_return_error(ctx=ctx, error=str(exc), remediation="traverse_paths failed.")

    if result.truncated_at_depth is not None:
        await ctx.warning(
            f"Path search truncated at depth {result.truncated_at_depth}. "
            "Diagram may not show all paths — try increasing max_hops."
        )

    hop_lists = _sdk_paths_to_hops(result.paths)
    if not hop_lists:
        hint = (
            f" Search was truncated at depth {result.truncated_at_depth}."
            if result.truncated_at_depth is not None
            else ""
        )
        return await _log_and_return_error(
            ctx=ctx,
            error=f"No path found between '{source}' and '{target}'.{hint}",
            remediation="Verify devices are connected via DcimCable, TopologyPhysicalCircuit, or TopologyVirtualCircuit.",
        )

    path_nodes: set[str] = {source, target}
    seen_edges: set[frozenset] = set()
    path_edges: list[tuple] = []
    for hop_list in hop_lists:
        for hop in hop_list:
            path_nodes.add(hop["src_device"])
            path_nodes.add(hop["dst_device"])
            key = frozenset([hop["src_device"], hop["dst_device"]])
            if key not in seen_edges:
                seen_edges.add(key)
                path_edges.append(
                    (
                        hop["src_device"],
                        hop["dst_device"],
                        hop["connector"],
                        hop["src_iface"],
                        hop["dst_iface"],
                        hop["connector_type"],
                    )
                )

    # Fetch roles for path devices only via GraphQL
    role_map: dict[str, str] = {}
    try:
        ids = [result.source.id, result.destination.id] + [
            h.node.id for p in result.paths for h in p.hops if h.node.kind == KIND_DEVICE
        ]
        dev_nodes = await client.filters(kind=KIND_DEVICE, ids=ids, branch=branch or "main")
        for dn in dev_nodes:
            name = getattr(getattr(dn, "name", None), "value", None)
            role = getattr(getattr(dn, "role", None), "value", None)
            if name:
                role_map[name] = role or ""
    except Exception:  # noqa: BLE001
        pass

    path_dev_map = {n: {"name": n, "role": role_map.get(n, "")} for n in path_nodes}
    diagram = _generate_drawio_xml(path_dev_map, path_edges, f"{source} → {target}")

    await ctx.info(f"Found {len(hop_lists)} path(s), {len(path_nodes)} devices, {len(path_edges)} unique hops")
    return MCPResponse(
        status=MCPToolStatus.SUCCESS,
        data={
            "source": source,
            "target": target,
            "paths": len(hop_lists),
            "path_length": len(hop_lists[0]),
            "devices": len(path_dev_map),
            "cables": len(path_edges),
            "drawio": diagram,
        },
    )


# ---------------------------------------------------------------------------
# Containerlab topology generator
# ---------------------------------------------------------------------------

# Infrahub platform → containerlab kind + image
_CLAB_PLATFORM: dict[str, dict[str, str]] = {
    "arista_eos": {"kind": "ceos", "image": "ceos:latest"},
    "juniper_junos": {"kind": "vr-junos", "image": "vrnetlab/vr-junos:latest"},
    "juniper_evo": {"kind": "vr-junos", "image": "vrnetlab/vr-junos:latest"},
    "cisco_ios": {"kind": "vr-ios", "image": "vrnetlab/vr-cisco-ios:latest"},
    "cisco_iosxe": {"kind": "vr-xе", "image": "vrnetlab/vr-cisco-xе:latest"},
    "cisco_iosxr": {"kind": "vr-xr", "image": "vrnetlab/vr-cisco-xr:latest"},
    "cisco_nxos": {"kind": "vr-nxos", "image": "vrnetlab/vr-cisco-nxos:latest"},
    "checkpoint_gaia": {"kind": "linux", "image": "check-point/cloudguard:latest"},
    "palo_alto_panos": {"kind": "linux", "image": "paloaltonetworks/panos:latest"},
    "fortinet_fortios": {"kind": "linux", "image": "fortinet/fortios:latest"},
    "linux": {"kind": "linux", "image": "alpine:latest"},
}
_CLAB_DEFAULT = {"kind": "linux", "image": "alpine:latest"}


def _clab_node_name(device_name: str) -> str:
    """Sanitise device name for containerlab (lowercase, hyphens only)."""
    return device_name.lower().replace("_", "-").replace(".", "-")


def _generate_containerlab_yaml(
    dev_map: dict[str, dict],
    edge_list: list[tuple],
    lab_name: str,
) -> str:
    """Render path topology as a containerlab YAML string.

    edge_list items: (src_device, dst_device, connector, src_iface, dst_iface, edge_type)
    """
    import yaml  # stdlib via PyYAML

    nodes: dict[str, dict] = {}
    for name, info in dev_map.items():
        platform = (info.get("platform") or "").lower()
        clab = _CLAB_PLATFORM.get(platform, _CLAB_DEFAULT)
        node_name = _clab_node_name(name)
        nodes[node_name] = {
            "kind": clab["kind"],
            "image": clab["image"],
        }

    links: list[dict] = []
    seen: set[frozenset] = set()
    # Track per-node interface counters for fallback naming
    iface_counters: dict[str, int] = {}

    for edge in edge_list:
        a, b = edge[0], edge[1]
        iface_a = edge[3] if len(edge) > 3 else ""
        iface_b = edge[4] if len(edge) > 4 else ""
        key = frozenset([a, b])
        if key in seen:
            continue
        seen.add(key)
        if a not in dev_map or b not in dev_map:
            continue

        na, nb = _clab_node_name(a), _clab_node_name(b)

        # Use real interface names if available, otherwise eth<n>
        if not iface_a:
            iface_counters[na] = iface_counters.get(na, 0) + 1
            iface_a = f"eth{iface_counters[na]}"
        if not iface_b:
            iface_counters[nb] = iface_counters.get(nb, 0) + 1
            iface_b = f"eth{iface_counters[nb]}"

        links.append({"endpoints": [f"{na}:{iface_a}", f"{nb}:{iface_b}"]})

    topology: dict = {
        "name": lab_name,
        "topology": {
            "nodes": nodes,
            "links": links,
        },
    }
    return yaml.dump(topology, default_flow_style=False, sort_keys=False, allow_unicode=True)


@mcp.tool(
    tags={"topology", "containerlab", "retrieve"},
    annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True),
)
async def generate_containerlab_topology(
    ctx: Context,
    source: Annotated[str, Field(description="Source device name (e.g. DC1-BL1).")],
    target: Annotated[str, Field(description="Target device name (e.g. DC1-POD1-SRV-01).")],
    branch: Annotated[str | None, Field(default=None, description="Branch to query (defaults to main).")] = None,
) -> MCPResponse[dict[str, Any]]:
    """
    Generate a containerlab topology YAML for all shortest paths between two devices.

    Discovers the full path (all intermediate hops) via InfrahubClient.traverse_paths(),
    fetches platform/role for each device, and maps Infrahub platforms to containerlab
    node kinds and images. Real interface names from cable endpoints are used for links.

    Returns the YAML string in data.containerlab plus summary counts.
    """
    from franc.tools.graph import _name_to_uuid, _sdk_paths_to_hops, _sdk_rel_filters

    client = require_client(ctx)
    branch = branch or "main"
    await ctx.info(f"Building containerlab topology: {source} → {target} (branch={branch})")

    src_uuid = await _name_to_uuid(client, source, branch)
    dst_uuid = await _name_to_uuid(client, target, branch)
    if not src_uuid or not dst_uuid:
        missing = source if not src_uuid else target
        return await _log_and_return_error(
            ctx=ctx,
            error=f"Device '{missing}' not found on branch '{branch}'.",
            remediation="Check the device name with get_topology_devices().",
        )

    rel_filters = _sdk_rel_filters(["cable", "physical_circuit", "virtual_circuit"])
    try:
        result = await client.traverse_paths(src_uuid, dst_uuid, relationship_filter=rel_filters, branch=branch)
    except Exception as exc:  # noqa: BLE001
        return await _log_and_return_error(ctx=ctx, error=str(exc), remediation="traverse_paths failed.")

    if result.truncated_at_depth is not None:
        await ctx.warning(
            f"Path search truncated at depth {result.truncated_at_depth}. Containerlab topology may be incomplete."
        )

    hop_lists = _sdk_paths_to_hops(result.paths)
    if not hop_lists:
        hint = (
            f" Search was truncated at depth {result.truncated_at_depth}."
            if result.truncated_at_depth is not None
            else ""
        )
        return await _log_and_return_error(
            ctx=ctx,
            error=f"No path found between '{source}' and '{target}'.{hint}",
            remediation="Verify devices are connected via DcimCable or TopologyPhysicalCircuit.",
        )

    path_nodes: set[str] = {source, target}
    seen_edges: set[frozenset] = set()
    path_edges: list[tuple] = []
    for hop_list in hop_lists:
        for hop in hop_list:
            path_nodes.add(hop["src_device"])
            path_nodes.add(hop["dst_device"])
            key = frozenset([hop["src_device"], hop["dst_device"]])
            if key not in seen_edges:
                seen_edges.add(key)
                path_edges.append(
                    (
                        hop["src_device"],
                        hop["dst_device"],
                        hop["connector"],
                        hop["src_iface"],
                        hop["dst_iface"],
                        hop["connector_type"],
                    )
                )

    # Fetch role + platform for each device on the path
    role_map: dict[str, str] = {}
    platform_map: dict[str, str] = {}
    try:
        gql = """
        query ($names: [String!]!) {
            DcimPhysicalDevice(name__values: $names) {
                edges { node {
                    name { value }
                    role { value }
                    platform { node { name { value } } }
                } }
            }
        }
        """
        r = await client.execute_graphql(
            query=gql,
            variables={"names": list(path_nodes)},
            branch_name=branch,
        )
        for e in (r or {}).get("DcimPhysicalDevice", {}).get("edges", []):
            n = e.get("node", {})
            name = (n.get("name") or {}).get("value") or ""
            role = (n.get("role") or {}).get("value") or ""
            platform = ((n.get("platform") or {}).get("node") or {}).get("name", {}).get("value") or ""
            if name:
                role_map[name] = role
                platform_map[name] = platform
    except Exception:  # noqa: BLE001
        pass

    dev_map = {n: {"name": n, "role": role_map.get(n, ""), "platform": platform_map.get(n, "")} for n in path_nodes}

    lab_name = f"{_clab_node_name(source)}-to-{_clab_node_name(target)}"
    clab_yaml = _generate_containerlab_yaml(dev_map, path_edges, lab_name)

    await ctx.info(f"Containerlab topology ready — {len(dev_map)} nodes, {len(path_edges)} links")
    return MCPResponse(
        status=MCPToolStatus.SUCCESS,
        data={
            "source": source,
            "target": target,
            "branch": branch,
            "paths": len(hop_lists),
            "nodes": len(dev_map),
            "links": len(path_edges),
            "lab_name": lab_name,
            "containerlab": clab_yaml,
        },
    )


@mcp.tool(
    tags={"topology", "cluster", "diagram", "retrieve"},
    annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True),
)
async def generate_cluster_diagram(
    ctx: Context,
    cluster_name: Annotated[
        str | None,
        Field(default=None, description="Specific cluster name (e.g. DC3-K8S-PROD). If omitted, shows all clusters."),
    ],
    datacenter: Annotated[
        str | None,
        Field(
            default=None,
            description="Filter by datacenter/deployment prefix (e.g. DC3, AM1). Shows all clusters in that site.",
        ),
    ],
    platform: Annotated[
        str | None,
        Field(default=None, description="Filter by platform: kubernetes, eks, aks, vmware_vsphere, nutanix."),
    ],
    branch: Annotated[str | None, Field(default=None, description="Branch to read from (defaults to main).")],
) -> MCPResponse[dict[str, Any]]:
    """
    Generate a Draw.io cluster topology diagram showing VirtCluster node pools.

    Each cluster renders as a column with colour-coded node pool rows:
    control-plane (dark blue) → workers/hypervisor (green) → storage (amber) → management (purple).

    Filter by cluster name, datacenter prefix, or platform type.
    """
    try:
        client: InfrahubClient = require_client(ctx)
    except RuntimeError as exc:
        return await _log_and_return_error(ctx=ctx, error=str(exc), remediation="Start MCP with a configured client.")

    branch = branch or "main"
    await ctx.info(f"Fetching cluster topology (cluster={cluster_name} dc={datacenter} platform={platform})")

    # Fetch all clusters then filter in Python — Infrahub doesn't support icontains on name
    cl_filter = ""
    if cluster_name:
        cl_filter = f'(name__value: "{cluster_name}")'
    elif platform:
        cl_filter = f'(platform__value: "{platform}")'

    gql_clusters = f"""
    query {{
        VirtCluster{cl_filter} {{
            edges {{ node {{
                name {{ value }}
                platform {{ value }}
                version {{ value }}
                ha_enabled {{ value }}
                status {{ value }}
                deployment {{ node {{ name {{ value }} }} }}
            }} }}
        }}
    }}
    """

    try:
        cl_result = await client.execute_graphql(query=gql_clusters, branch_name=branch)
    except Exception as exc:  # noqa: BLE001
        return await _log_and_return_error(ctx=ctx, error=exc, remediation="Cluster query failed.")

    all_clusters = (cl_result or {}).get("VirtCluster", {}).get("edges", [])

    # Filter by datacenter prefix in Python (cluster name starts with DC prefix,
    # or deployment name contains datacenter string)
    if datacenter and not cluster_name and not platform:
        dc_upper = datacenter.upper()
        all_clusters = [
            e
            for e in all_clusters
            if e["node"]["name"]["value"].upper().startswith(dc_upper)
            or dc_upper
            in (((e["node"].get("deployment") or {}).get("node") or {}).get("name", {}).get("value", "").upper())
        ]

    raw_clusters = all_clusters
    if not raw_clusters:
        return await _log_and_return_error(
            ctx=ctx,
            error=f"No clusters found (cluster={cluster_name} dc={datacenter} platform={platform} branch={branch}).",
            remediation="Try branch='test' — cluster data lives on the test branch.",
        )

    # Fetch node pools for all clusters in parallel
    async def _fetch_pools(cname: str) -> list[dict]:
        gql = f"""
        query {{
            VirtNodePool(cluster__name__value: "{cname}") {{
                edges {{ node {{
                    name {{ value }}
                    pool_type {{ value }}
                    node_count {{ value }}
                    instance_flavor {{ value }}
                    scaling_mode {{ value }}
                    min_size {{ value }}
                    max_size {{ value }}
                    cpu_per_node {{ value }}
                    memory_per_node_gb {{ value }}
                }} }}
            }}
        }}
        """
        r = await client.execute_graphql(query=gql, branch_name=branch)
        pools = []
        for e in (r or {}).get("VirtNodePool", {}).get("edges", []):
            n = e.get("node", {})
            pools.append(
                {
                    "name": (n.get("name") or {}).get("value") or "",
                    "pool_type": (n.get("pool_type") or {}).get("value") or "worker",
                    "node_count": (n.get("node_count") or {}).get("value"),
                    "instance_flavor": (n.get("instance_flavor") or {}).get("value") or "",
                    "scaling_mode": (n.get("scaling_mode") or {}).get("value") or "static",
                    "min_size": (n.get("min_size") or {}).get("value"),
                    "max_size": (n.get("max_size") or {}).get("value"),
                    "cpu_per_node": (n.get("cpu_per_node") or {}).get("value"),
                    "memory_per_node_gb": (n.get("memory_per_node_gb") or {}).get("value"),
                }
            )
        return pools

    pool_results = await _gather_safe(*[_fetch_pools(e["node"]["name"]["value"]) for e in raw_clusters])

    clusters_data: list[dict] = []
    for i, edge in enumerate(raw_clusters):
        n = edge["node"]
        dep = ((n.get("deployment") or {}).get("node") or {}).get("name", {}).get("value") or ""
        clusters_data.append(
            {
                "name": n["name"]["value"],
                "platform": (n.get("platform") or {}).get("value") or "",
                "version": (n.get("version") or {}).get("value") or "",
                "ha_enabled": (n.get("ha_enabled") or {}).get("value") or False,
                "status": (n.get("status") or {}).get("value") or "",
                "deployment": dep,
                "pools": pool_results[i] or [],
            }
        )

    diagram = _cluster_drawio_xml(clusters_data, datacenter or cluster_name)

    return MCPResponse(
        status=MCPToolStatus.SUCCESS,
        data={
            "clusters": len(clusters_data),
            "node_pools": sum(len(c["pools"]) for c in clusters_data),
            "branch": branch,
            "drawio": diagram,
        },
    )


# ---------------------------------------------------------------------------
# Impact analysis
# ---------------------------------------------------------------------------


@mcp.tool(
    tags={"topology", "impact", "retrieve"},
    annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True),
)
async def analyze_impact(
    ctx: Context,
    remove_device: Annotated[
        str | None,
        Field(default=None, description="Device name to remove (e.g. DC1-BL1, DC1-C001-FW-01)."),
    ],
    remove_cable: Annotated[
        str | None,
        Field(default=None, description="Cable name to remove (e.g. CBL-DC1-BL1-POD1-SP1)."),
    ],
    branch: Annotated[str | None, Field(default=None, description="Branch to query (defaults to main).")] = None,
) -> MCPResponse[dict[str, Any]]:
    """
    Impact analysis: what breaks if a device or cable goes offline?

    Uses SDK for all graph traversal and app-to-server resolution:

    1. App → servers via:  AppApplication → AppComponent → segment → LAG → device
    2. Server cable neighbors (get_device_neighbors) — counts distinct border/spine
       nodes reachable before and after removing the element.

    Reports per application:
    - outage   — server can no longer reach any border/spine device
    - degraded — fewer border/spine devices reachable (ECMP reduced)
    - ok       — unaffected
    """
    if not remove_device and not remove_cable:
        return await _log_and_return_error(
            ctx=ctx,
            error="Provide remove_device or remove_cable.",
            remediation="Example: remove_device='DC1-BL1' or remove_cable='CBL-DC1-BL1-POD1-SP1'.",
        )

    client = require_client(ctx)
    branch_name = branch or "main"
    target_label = remove_device or remove_cable
    await ctx.info(f"Analyzing impact of removing {'device' if remove_device else 'cable'} '{target_label}'")

    # Fetch all servers and border devices by role
    server_nodes = await client.filters(kind=KIND_DEVICE, role__value__in=["endpoint"], branch=branch_name)
    border_nodes = await client.filters(
        kind=KIND_DEVICE,
        role__value__in=["border-leaf", "spine", "super-spine", "edge", "core"],
        branch=branch_name,
    )

    all_servers = [attr_val(n, "name") for n in (server_nodes or [])]
    all_borders: set[str] = {attr_val(n, "name") for n in (border_nodes or [])}
    if remove_device:
        all_borders.discard(remove_device)

    if not all_servers or not all_borders:
        return await _log_and_return_error(
            ctx=ctx,
            error="No servers or border devices found.",
            remediation="Ensure devices exist with role='server'/'border-leaf'/'spine' etc.",
        )

    # App → server mapping via AppApplication → AppComponent → ManagedVxlanSegment → interface_capabilities
    app_server_map: dict[str, dict[str, Any]] = {}
    app_nodes = await client.filters(kind="AppApplication", branch=branch_name)
    for app_node in app_nodes:
        app: Any = app_node
        app_name = app.name.value
        criticality = app.criticality.value if hasattr(app, "criticality") else "medium"
        app_prefix = app_name + "-"
        comp_nodes = await client.filters(kind="AppComponent", branch=branch_name)
        for comp_node in comp_nodes:
            comp: Any = comp_node
            slug = str(comp.slug.value if hasattr(comp.slug, "value") else "")
            if not slug.startswith(app_prefix):
                continue
            await comp.network_segment.fetch()
            seg_node = comp.network_segment.peer
            if not seg_node:
                continue
            await seg_node.interface_capabilities.fetch()
            for ic in seg_node.interface_capabilities.peers:
                iface = ic.peer
                if not iface:
                    continue
                await iface.device.fetch()
                dev = iface.device.peer
                if not dev:
                    continue
                srv = dev.name.value
                if app_name not in app_server_map:
                    app_server_map[app_name] = {"criticality": criticality, "servers": []}
                if srv not in app_server_map[app_name]["servers"]:
                    app_server_map[app_name]["servers"].append(srv)

    # Cable-neighbor reachability: use get_device_neighbors for each server
    from franc.tools.graph import get_device_neighbors

    async def _cable_neighbors(device: str) -> set[str]:
        resp = await get_device_neighbors(ctx, device_name=device, connector_types=["cable"], branch=branch_name)
        if resp.status != MCPToolStatus.SUCCESS or not isinstance(resp.data, list):
            return set()
        return {n["neighbor_device"] for n in resp.data if n.get("neighbor_device")}

    def _filter_neighbors(
        neighbors: set[str],
        skip_device: str | None,
        skip_cable: str | None,
        hop_connectors: dict[str, str],
    ) -> set[str]:
        result: set[str] = set()
        for n in neighbors:
            if skip_device and n == skip_device:
                continue
            if skip_cable and hop_connectors.get(n) == skip_cable:
                continue
            result.add(n)
        return result

    server_status: dict[str, str] = {}
    for srv in all_servers:
        if srv == remove_device:
            continue
        neighbors = await _cable_neighbors(srv)
        before = len(neighbors & all_borders)
        filtered = neighbors - ({remove_device} if remove_device else set())
        after = len(filtered & all_borders)
        if after == 0 and before > 0:
            server_status[srv] = "outage"
        elif after < before:
            server_status[srv] = "degraded"

    # Map affected servers → applications
    app_map: dict[str, dict[str, Any]] = {}
    for aname, info in app_server_map.items():
        for srv in info["servers"]:
            status = server_status.get(srv)
            if not status:
                continue
            if aname not in app_map:
                app_map[aname] = {"criticality": info["criticality"], "servers": [], "worst": "ok"}
            entry = app_map[aname]
            if srv not in entry["servers"]:
                entry["servers"].append(srv)
            if status == "outage":
                entry["worst"] = "outage"
            elif status == "degraded" and entry["worst"] != "outage":
                entry["worst"] = "degraded"

    app_servers = {srv for e in app_map.values() for srv in e["servers"]}
    unmapped = sorted(srv for srv in server_status if srv not in app_servers)

    _CRIT = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    _STAT = {"outage": 0, "degraded": 1}
    app_impacts = sorted(
        [
            {
                "application": aname,
                "criticality": e["criticality"],
                "status": e["worst"],
                "affected_servers": sorted(e["servers"]),
            }
            for aname, e in app_map.items()
            if e["worst"] != "ok"
        ],
        key=lambda x: (_STAT.get(x["status"], 9), _CRIT.get(x["criticality"], 9)),
    )

    outage_count = sum(1 for a in app_impacts if a["status"] == "outage")
    degraded_count = sum(1 for a in app_impacts if a["status"] == "degraded")

    lines = [f"Impact of removing {target_label}:"]
    if not app_impacts and not unmapped:
        lines.append("  No application impact detected.")
    else:
        lines.append(f"  Applications affected: {len(app_impacts)} ({outage_count} outage, {degraded_count} degraded)")
        for ai in app_impacts:
            icon = "✗" if ai["status"] == "outage" else "⚠"
            lines.append(
                f"  {icon} {ai['application']} [{ai['criticality']}]  — {len(ai['affected_servers'])} server(s) {ai['status']}"
            )
            for srv in ai["affected_servers"][:4]:
                lines.append(f"      {srv}")
            if len(ai["affected_servers"]) > 4:
                lines.append(f"      ... +{len(ai['affected_servers']) - 4} more")
        if unmapped:
            lines.append(f"  Servers with no app mapping: {', '.join(unmapped[:10])}")
            if len(unmapped) > 10:
                lines.append(f"  ... +{len(unmapped) - 10} more unmapped")

    await ctx.info("\n".join(lines))

    return MCPResponse(
        status=MCPToolStatus.SUCCESS,
        data={
            "removed": target_label,
            "removed_type": "device" if remove_device else "cable",
            "applications_affected": len(app_impacts),
            "outage_count": outage_count,
            "degraded_count": degraded_count,
            "app_impacts": app_impacts,
            "unmapped_servers_affected": unmapped,
            "summary": "\n".join(lines),
        },
    )


# ---------------------------------------------------------------------------
# Firewall placement check
# ---------------------------------------------------------------------------


@mcp.tool(
    tags={"topology", "firewall", "retrieve"},
    annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True),
)
async def check_firewall_placement(
    ctx: Context,
    customer: Annotated[
        str | None,
        Field(default=None, description="Filter by customer prefix (e.g. 'c001'). Omit to check all customers."),
    ],
    branch: Annotated[str | None, Field(default=None, description="Branch to query (defaults to main).")] = None,
) -> MCPResponse[dict[str, Any]]:
    """
    Firewall placement analysis: are servers routing through a local or remote firewall?

    For each PBR-enabled segment, checks whether every server carrying the segment
    shares a DC with the assigned firewall. A "remote" firewall means traffic is
    hairpinning across DCs, which is sub-optimal.

    Reports per segment:
    - local   — server's DC matches the firewall's DC
    - remote  — server is in a different DC from the firewall (cross-DC hairpin)
    """
    client = require_client(ctx)
    branch_name = branch or "main"
    await ctx.info(f"Checking firewall placement{' for ' + customer if customer else ''}")

    # Fetch all segments
    seg_nodes = await client.filters(kind="ManagedVxlanSegment", branch=branch_name)

    results: list[dict[str, Any]] = []
    for seg_node in seg_nodes:
        seg: Any = seg_node
        seg_name = seg.name.value
        if customer and not seg_name.startswith(customer):
            continue
        if not seg.pbr_enabled.value:
            continue

        await seg.inline_service.fetch()
        fw_node = seg.inline_service.peer
        if not fw_node:
            continue
        fw_name = fw_node.name.value

        # Firewall's DC — infer from deployment or name prefix
        fw_dc = fw_name.split("-")[0] if "-" in fw_name else fw_name

        # Servers attached to this segment
        await seg.interface_capabilities.fetch()
        servers_data: list[dict] = []
        for ic in seg.interface_capabilities.peers:
            iface = ic.peer
            if not iface:
                continue
            await iface.device.fetch()
            dev = iface.device.peer
            if not dev:
                continue
            srv_name = dev.name.value
            srv_dc = srv_name.split("-")[0] if "-" in srv_name else srv_name
            placement = "local" if srv_dc == fw_dc else "remote"
            srv_entry = {"server": srv_name, "server_dc": srv_dc, "placement": placement}
            if srv_entry not in servers_data:
                servers_data.append(srv_entry)

        local_count = sum(1 for s in servers_data if s["placement"] == "local")
        remote_count = sum(1 for s in servers_data if s["placement"] == "remote")
        worst = "remote" if remote_count > 0 else "local"

        results.append(
            {
                "segment": seg_name,
                "firewall": fw_name,
                "firewall_dc": fw_dc,
                "pbr_enabled": True,
                "servers_local": local_count,
                "servers_remote": remote_count,
                "worst": worst,
                "servers": sorted(servers_data, key=lambda s: s["server"]),
            }
        )

    results.sort(key=lambda x: (0 if x["worst"] == "remote" else 1, x["segment"]))

    remote_segs = [r for r in results if r["worst"] == "remote"]
    local_segs = [r for r in results if r["worst"] == "local"]

    lines: list[str] = ["Firewall placement check:"]
    if not results:
        lines.append("  No PBR-enabled segments found.")
    else:
        lines.append(
            f"  Segments checked: {len(results)}  ({len(remote_segs)} with remote hairpin, {len(local_segs)} fully local)"
        )
        for r in results:
            icon = "✗" if r["worst"] == "remote" else "✓"
            lines.append(f"\n  {icon} {r['segment']}  fw={r['firewall']} ({r['firewall_dc']})")
            for s in r["servers"]:
                marker = "✗ remote" if s["placement"] == "remote" else "✓ local"
                lines.append(f"      {s['server']} ({s['server_dc']})  →  {marker}")

    await ctx.info("\n".join(lines))

    return MCPResponse(
        status=MCPToolStatus.SUCCESS,
        data={
            "customer": customer,
            "segments_checked": len(results),
            "remote_hairpin_count": len(remote_segs),
            "results": results,
            "summary": "\n".join(lines),
        },
    )


# ---------------------------------------------------------------------------
# PBR coverage gaps
# ---------------------------------------------------------------------------


@mcp.tool(
    tags={"topology", "firewall", "pbr", "retrieve"},
    annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True),
)
async def check_pbr_coverage(
    ctx: Context,
    customer: Annotated[
        str | None,
        Field(default=None, description="Filter by customer prefix (e.g. 'c001'). Omit to check all customers."),
    ],
    branch: Annotated[str | None, Field(default=None, description="Branch to query (defaults to main).")] = None,
) -> MCPResponse[dict[str, Any]]:
    """
    PBR coverage analysis: which segments are missing PBR or firewall assignment?

    Reports segments where:
    - pbr_enabled=false but a firewall exists (PBR disabled despite FW being assigned)
    - pbr_enabled=true but no firewall is assigned (PBR enabled but nowhere to route)
    - No firewall assigned at all (traffic is unprotected)
    """
    client = require_client(ctx)
    branch_name = branch or "main"
    await ctx.info(f"Checking PBR coverage{' for ' + customer if customer else ''}")

    seg_nodes = await client.filters(kind="ManagedVxlanSegment", branch=branch_name)

    seg_info: dict[str, dict] = {}
    for seg_node in seg_nodes:
        seg: Any = seg_node
        seg_name = seg.name.value
        if customer and not seg_name.startswith(customer):
            continue
        pbr = bool(seg.pbr_enabled.value)
        await seg.inline_service.fetch()
        fw_node = seg.inline_service.peer
        fw_name = fw_node.name.value if fw_node else ""

        # Namespace (VRF) — reached via the segment's anycast gateway IP, not directly
        namespace = ""
        try:
            await seg.gateway.fetch()
            gw = seg.gateway.peer
            if gw:
                await gw.ip_namespace.fetch()
                ns = gw.ip_namespace.peer
                if ns:
                    namespace = ns.name.value
        except Exception:  # noqa: BLE001
            pass

        seg_info[seg_name] = {
            "pbr_enabled": pbr,
            "firewalls": [fw_name] if fw_name else [],
            "namespace": namespace,
        }

    gaps: list[dict] = []
    ok: list[str] = []

    for seg, info in sorted(seg_info.items()):
        has_fw = bool(info["firewalls"])
        pbr = info["pbr_enabled"]

        if pbr and not has_fw:
            issue = "pbr_enabled_no_firewall"
            severity = "critical"
        elif not pbr and has_fw:
            issue = "firewall_assigned_pbr_disabled"
            severity = "warning"
        elif not has_fw:
            issue = "no_firewall"
            severity = "info"
        else:
            ok.append(seg)
            continue

        gaps.append(
            {
                "segment": seg,
                "issue": issue,
                "severity": severity,
                "pbr_enabled": pbr,
                "firewalls": info["firewalls"],
                "namespace": info["namespace"],
            }
        )

    _SEV = {"critical": 0, "warning": 1, "info": 2}
    gaps.sort(key=lambda x: (_SEV.get(x["severity"], 9), x["segment"]))

    lines: list[str] = ["PBR coverage check:"]
    lines.append(f"  Segments: {len(seg_info)} total  ({len(gaps)} gaps, {len(ok)} ok)")
    if gaps:
        lines.append("")
        for g in gaps:
            icon = {"critical": "✗", "warning": "⚠", "info": "ℹ"}.get(g["severity"], "?")
            fw_str = ", ".join(g["firewalls"]) if g["firewalls"] else "none"
            lines.append(f"  {icon} {g['segment']}  [{g['severity']}]")
            lines.append(f"      pbr={g['pbr_enabled']}  firewall={fw_str}  issue={g['issue']}")
    else:
        lines.append("  All segments have consistent PBR + firewall configuration.")

    await ctx.info("\n".join(lines))

    return MCPResponse(
        status=MCPToolStatus.SUCCESS,
        data={
            "customer": customer,
            "total_segments": len(seg_info),
            "gaps": gaps,
            "ok_count": len(ok),
            "summary": "\n".join(lines),
        },
    )


# ---------------------------------------------------------------------------
# Segment deployment coverage
# ---------------------------------------------------------------------------


@mcp.tool(
    tags={"topology", "segment", "retrieve"},
    annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True),
)
async def check_segment_coverage(
    ctx: Context,
    customer: Annotated[
        str | None,
        Field(default=None, description="Filter by customer prefix (e.g. 'c001'). Omit to check all customers."),
    ],
    datacenters: Annotated[
        str | None,
        Field(
            default=None,
            description=(
                "Comma-separated list of DC names to check coverage against (e.g. 'DC1,DC2,DC3'). "
                "Omit to use all known DCs."
            ),
        ),
    ],
    branch: Annotated[str | None, Field(default=None, description="Branch to query (defaults to main).")] = None,
) -> MCPResponse[dict[str, Any]]:
    """
    Segment deployment coverage: which segments are missing from which DCs?

    Cross-references every segment's ManagedSegmentDeployment records against
    the set of known datacenters and flags gaps.

    Reports per segment:
    - present  — segment has a deployment in that DC
    - missing  — no deployment found
    """
    client = require_client(ctx)
    branch_name = branch or "main"
    await ctx.info(f"Checking segment coverage{' for ' + customer if customer else ''}")

    # Collect all top-level DC names (deployments without a parent)
    all_dep_nodes = await client.filters(kind="TopologyDeployment", branch=branch_name)
    all_dc_names: set[str] = set()
    for dep_node in all_dep_nodes:
        dep: Any = dep_node
        dep_name = dep.name.value
        try:
            await dep.parent.fetch()
            parent = dep.parent.peer
        except Exception:  # noqa: BLE001
            parent = None
        if parent is None:
            all_dc_names.add(dep_name)

    if datacenters:
        target_dcs = {d.strip() for d in datacenters.split(",") if d.strip()}
        unknown = target_dcs - all_dc_names
        if unknown:
            return await _log_and_return_error(
                ctx=ctx,
                error=f"Unknown datacenter(s): {', '.join(sorted(unknown))}. Known: {', '.join(sorted(all_dc_names))}",
                remediation="Use exact DC names (e.g. DC1, DC2, DC3).",
            )
        check_dcs = sorted(target_dcs)
    else:
        check_dcs = sorted(all_dc_names)

    if not check_dcs:
        return await _log_and_return_error(
            ctx=ctx,
            error="No datacenters found.",
            remediation="Ensure TopologyDeployment nodes exist in Infrahub.",
        )

    # Fetch all segments and their deployments
    seg_nodes = await client.filters(kind="ManagedVxlanSegment", branch=branch_name)
    seg_deployments: dict[str, set[str]] = {}

    for seg_node in seg_nodes:
        seg: Any = seg_node
        seg_name = seg.name.value
        if customer and not seg_name.startswith(customer):
            continue
        seg_deployments.setdefault(seg_name, set())
        await seg.segment_deployments.fetch()
        for sd_peer in seg.segment_deployments.peers:
            sd_node = sd_peer.peer
            if not sd_node:
                continue
            await sd_node.deployment.fetch()
            dep = sd_node.deployment.peer
            if dep:
                dep_name = dep.name.value
                dc_key = dep_name.split("-")[0] if "-" in dep_name else dep_name
                if dc_key in check_dcs:
                    seg_deployments[seg_name].add(dc_key)

    results: list[dict] = []
    for seg in sorted(seg_deployments):
        present = seg_deployments[seg]
        missing = sorted(set(check_dcs) - present)
        results.append(
            {
                "segment": seg,
                "deployed_in": sorted(present),
                "missing_from": missing,
                "dc_count": len(present),
                "coverage": "full" if not missing else ("partial" if present else "none"),
            }
        )

    results.sort(key=lambda x: (0 if x["missing_from"] else 1, len(x["missing_from"]) * -1, x["segment"]))

    gaps = [r for r in results if r["missing_from"]]
    full = [r for r in results if not r["missing_from"]]

    lines: list[str] = [f"Segment deployment coverage ({', '.join(check_dcs)}):"]
    lines.append(f"  Segments: {len(results)} total  ({len(gaps)} with gaps, {len(full)} fully deployed)")
    if gaps:
        lines.append("")
        for r in results:
            if not r["missing_from"]:
                dc_str = "  ".join(f"{dc} ✓" for dc in check_dcs)
                lines.append(f"  ✓ {r['segment']:<40}  {dc_str}")
            else:
                dc_str = "  ".join(f"{dc} ✓" if dc in r["deployed_in"] else f"{dc} ✗" for dc in check_dcs)
                lines.append(f"  ✗ {r['segment']:<40}  {dc_str}")

    await ctx.info("\n".join(lines))

    return MCPResponse(
        status=MCPToolStatus.SUCCESS,
        data={
            "customer": customer,
            "datacenters": check_dcs,
            "total_segments": len(results),
            "gap_count": len(gaps),
            "results": results,
            "summary": "\n".join(lines),
        },
    )
