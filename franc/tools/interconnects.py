"""Interconnect tools — DC-to-colo, colo-to-cloud, office-to-colo, and inter-metro circuits."""

from typing import TYPE_CHECKING, Annotated, Any

from fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from franc.constants import KIND_BGP_PEERING
from franc.utils import (
    MCPResponse,
    MCPToolStatus,
    _gather_safe,
    _log_and_return_error,
    attr_val,
    maybe_compress,
    require_client,
)

if TYPE_CHECKING:
    from infrahub_sdk import InfrahubClient

mcp: FastMCP = FastMCP(name="Infrahub Interconnects")

KIND_PHYS_CIRCUIT = "TopologyPhysicalCircuit"
KIND_CLOUD_DX = "CloudDirectConnect"
KIND_CLOUD_VIF = "CloudVirtualInterface"

_CIRCUIT_SEGMENT_LABELS = {
    "dc-colo": ["DC", "FR", "AM", "PA"],
    "office-colo": ["OFC-", "FR", "AM", "PA"],
    "colo-colo": ["FR", "AM", "PA"],
    "partner": ["PARTNER"],
}


def _classify_circuit(locs: list[str]) -> str:
    locs_upper = [l.upper() for l in locs]
    has_dc = any(l.startswith("DC") for l in locs_upper)
    has_ofc = any("OFC" in l for l in locs_upper)
    has_partner = any("PARTNER" in l for l in locs_upper)
    has_colo = any(
        any(l.startswith(p) for p in ("FR", "AM", "PA", "NY", "VA", "AMS", "FRA", "PAR")) for l in locs_upper
    )
    if has_partner:
        return "partner"
    if has_dc and has_colo:
        return "dc-colo"
    if has_ofc and has_colo:
        return "office-colo"
    if has_colo:
        return "colo-colo"
    return "other"


@mcp.tool(
    tags={"interconnects", "discover"},
    annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True),
)
async def get_interconnect_summary(
    ctx: Context,
    branch: Annotated[str, Field(default="main", description="Branch to query.")],
) -> MCPResponse[dict[str, Any]]:
    """
    Return a full summary of all interconnects by type:
      - DC ↔ Equinix colocation (cross-connects)
      - Office ↔ Equinix (SD-WAN underlay)
      - Equinix ↔ Equinix (inter-metro fabric)
      - Cloud Direct Connect / ExpressRoute (both modeled as CloudDirectConnect)
      - Partner MPLS/IPVPN

    Returns:
        {"by_type": {"dc-colo": N, "office-colo": N, "colo-colo": N, "partner": N},
         "cloud_dx": N, "total": N}
    """
    try:
        client: InfrahubClient = require_client(ctx)
    except RuntimeError as exc:
        return await _log_and_return_error(ctx=ctx, error=str(exc), remediation="Start MCP with a configured client.")

    await ctx.info("Fetching interconnect summary...")

    phys_r, dx_r = await _gather_safe(
        client.all(kind=KIND_PHYS_CIRCUIT, branch=branch, prefetch_relationships=False),
        client.all(kind=KIND_CLOUD_DX, branch=branch, prefetch_relationships=False),
    )

    by_type: dict[str, int] = {}
    for n in phys_r or []:
        locs = attr_val(n, "locations") or []
        locs_list = [str(l) for l in (locs if isinstance(locs, list) else [locs])]
        ct = _classify_circuit(locs_list)
        by_type[ct] = by_type.get(ct, 0) + 1

    return MCPResponse(
        status=MCPToolStatus.SUCCESS,
        data={
            "by_type": by_type,
            "cloud_dx": len(dx_r or []),
            "total_physical": len(phys_r or []),
            "total": len(phys_r or []) + len(dx_r or []),
        },
    )


_CIRCUIT_SCOPES = ("all", "dc-colo", "office", "colo-colo", "partner")


@mcp.tool(
    tags={"interconnects", "colocation", "external", "offices", "circuits", "retrieve"},
    annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True),
)
async def get_circuits(
    ctx: Context,
    scope: Annotated[
        str,
        Field(
            default="all",
            description=(
                "Circuit scope: 'all', 'dc-colo' (DC ↔ Equinix), 'office' (office ↔ Equinix SD-WAN), "
                "'colo-colo' (inter-metro fabric), or 'partner' (MPLS/IPVPN handoffs)."
            ),
        ),
    ],
    location: Annotated[
        str | None,
        Field(default=None, description="Filter by any endpoint location substring (e.g. DC1, FR2, OFC-LON, ACME)."),
    ],
    circuit_type: Annotated[
        str | None, Field(default=None, description="Filter by type: cross_connect, mpls, dark_fiber, ipvpn.")
    ],
    provider: Annotated[str | None, Field(default=None, description="Filter by provider name (e.g. Equinix, BT).")],
    branch: Annotated[str, Field(default="main", description="Branch to query.")],
) -> MCPResponse[dict[str, Any]]:
    """
    List physical circuits (`TopologyPhysicalCircuit`), optionally scoped by endpoint type.

    Replaces the former per-domain circuit tools (get_dc_to_colo_circuits, get_physical_circuits,
    get_partner_circuits, get_office_wan_circuits) with one tool parameterized by `scope` — all
    four read the same underlying kind and differed only in which endpoint locations they matched.

    Returns:
        {"circuits": [{"name", "circuit_id", "type", "provider", "bandwidth_mbps",
                        "status", "locations", "description"}], "count": N}
    """
    if scope not in _CIRCUIT_SCOPES:
        return await _log_and_return_error(
            ctx=ctx,
            error=f"Invalid scope '{scope}'.",
            remediation=f"Use one of: {', '.join(_CIRCUIT_SCOPES)}.",
        )

    try:
        client: InfrahubClient = require_client(ctx)
    except RuntimeError as exc:
        return await _log_and_return_error(ctx=ctx, error=str(exc), remediation="Start MCP with a configured client.")

    await ctx.info(f"Fetching circuits (scope={scope}, location={location}, type={circuit_type})...")

    try:
        nodes = await client.all(kind=KIND_PHYS_CIRCUIT, branch=branch, prefetch_relationships=False)
    except Exception as exc:  # noqa: BLE001
        return await _log_and_return_error(ctx=ctx, error=exc, remediation="Check schema for TopologyPhysicalCircuit.")

    circuits = []
    for n in nodes:
        locs = attr_val(n, "locations") or []
        locs_list = [str(l) for l in (locs if isinstance(locs, list) else [locs])]
        ct = str(attr_val(n, "circuit_type") or "")

        if scope == "partner":
            is_partner = _classify_circuit(locs_list) == "partner" or ct.lower() in ("mpls", "ipvpn")
            if not is_partner:
                continue
        elif scope == "office":
            if not any("OFC" in l.upper() for l in locs_list):
                continue
        elif scope != "all" and _classify_circuit(locs_list) != scope:
            continue

        if location and not any(location.upper() in l.upper() for l in locs_list):
            continue
        if circuit_type and ct.lower() != circuit_type.lower():
            continue
        prov = attr_val(n, "provider") or ""
        if provider and str(prov).lower() != provider.lower():
            continue

        circuits.append(
            {
                "name": attr_val(n, "name"),
                "circuit_id": attr_val(n, "circuit_id"),
                "type": ct,
                "provider": prov,
                "bandwidth_mbps": attr_val(n, "committed_rate"),
                "status": attr_val(n, "status"),
                "locations": locs_list,
                "description": attr_val(n, "description"),
            }
        )

    return await maybe_compress(ctx, circuits, "circuits", "circuits_gcf") or MCPResponse(
        status=MCPToolStatus.SUCCESS,
        data={"circuits": circuits, "count": len(circuits)},
    )


@mcp.tool(
    tags={"interconnects", "cloud", "retrieve"},
    annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True),
)
async def get_cloud_circuits(
    ctx: Context,
    provider: Annotated[str | None, Field(default=None, description="Filter by cloud provider: AWS or Azure.")],
    account: Annotated[str | None, Field(default=None, description="Filter by cloud account name (e.g. C001-AWS).")],
    branch: Annotated[str, Field(default="main", description="Branch to query.")],
) -> MCPResponse[dict[str, Any]]:
    """
    List cloud connectivity circuits: AWS Direct Connect and Azure ExpressRoute.

    Both are modeled as `CloudDirectConnect`; provider is inferred from the account name
    (accounts follow a `-AWS`/`-AZURE` naming convention) since the node itself carries no
    provider attribute — only `connection_type` (dedicated/hosted).

    Physical path: Equinix edge → cross-connect → cloud DX/ER endpoint.
    Logical path: VIF (transit/private) → Transit Gateway / Virtual Network Gateway.

    Returns:
        {"aws": [{"name", "account", "region", "bandwidth", "vlan_id", "location"}],
         "azure": [{"name", "account", "region", "bandwidth"}]}
    """
    try:
        client: InfrahubClient = require_client(ctx)
    except RuntimeError as exc:
        return await _log_and_return_error(ctx=ctx, error=str(exc), remediation="Start MCP with a configured client.")

    await ctx.info(f"Fetching cloud circuits (provider={provider}, account={account})...")

    dx_r, vif_r = await _gather_safe(
        client.all(kind=KIND_CLOUD_DX, branch=branch, prefetch_relationships=False),
        client.all(kind=KIND_CLOUD_VIF, branch=branch, prefetch_relationships=False),
    )

    def _want(nodes: list | None, prov_check: str) -> list:
        if provider and provider.upper() != prov_check:
            return []
        result = []
        for n in nodes or []:
            acct = str(attr_val(n, "account") or "")
            if prov_check not in acct.upper():
                continue
            if account and account.upper() not in acct.upper():
                continue
            result.append(n)
        return result

    aws_circuits = [
        {
            "name": attr_val(n, "name"),
            "account": attr_val(n, "account"),
            "region": attr_val(n, "region"),
            "bandwidth": attr_val(n, "bandwidth"),
            "connection_type": attr_val(n, "connection_type"),
            "vlan_id": attr_val(n, "vlan_id"),
            "location": attr_val(n, "location"),
        }
        for n in _want(dx_r, "AWS")
    ]
    azure_circuits = [
        {
            "name": attr_val(n, "name"),
            "account": attr_val(n, "account"),
            "region": attr_val(n, "region"),
            "bandwidth": attr_val(n, "bandwidth"),
            "connection_type": attr_val(n, "connection_type"),
            "vlan_id": attr_val(n, "vlan_id"),
            "location": attr_val(n, "location"),
        }
        for n in _want(dx_r, "AZURE")
    ]
    vifs = [
        {
            "name": attr_val(n, "name"),
            "vif_type": attr_val(n, "vif_type"),
            "vlan_id": attr_val(n, "vlan_id"),
            "direct_connect": attr_val(n, "direct_connect"),
        }
        for n in (vif_r or [])
    ]

    return MCPResponse(
        status=MCPToolStatus.SUCCESS,
        data={
            "aws": aws_circuits,
            "azure": azure_circuits,
            "virtual_interfaces": vifs,
            "total": len(aws_circuits) + len(azure_circuits),
        },
    )


@mcp.tool(
    tags={"interconnects", "bgp", "retrieve"},
    annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True),
)
async def get_dci_bgp_sessions(
    ctx: Context,
    datacenter: Annotated[str | None, Field(default=None, description="Filter by DC (e.g. DC1, DC3).")],
    branch: Annotated[str, Field(default="main", description="Branch to query.")],
) -> MCPResponse[dict[str, Any]]:
    """
    List DCI (Data Center Interconnect) BGP sessions between border-leafs and Equinix edge routers.

    These are the eBGP sessions that stitch DC fabrics together through the Equinix inter-metro fabric.

    Returns:
        {"sessions": [{"name", "local_device", "remote_device", "local_as", "remote_as", "type", "status"}]}
    """
    try:
        client: InfrahubClient = require_client(ctx)
    except RuntimeError as exc:
        return await _log_and_return_error(ctx=ctx, error=str(exc), remediation="Start MCP with a configured client.")

    await ctx.info(f"Fetching DCI BGP sessions (dc={datacenter})...")

    try:
        nodes = await client.all(kind=KIND_BGP_PEERING, branch=branch, prefetch_relationships=False)
    except Exception as exc:  # noqa: BLE001
        return await _log_and_return_error(ctx=ctx, error=exc, remediation="Check schema for ManagedBGPPeering.")

    sessions = []
    for n in nodes:
        local = str(attr_val(n, "local_device") or "")
        remote = str(attr_val(n, "remote_device") or "")
        # DCI sessions: BL ↔ EQX-EDGE, or EDGE ↔ EDGE
        is_dci = (
            ("BL" in local.upper() or "BL" in remote.upper())
            and any(p in remote.upper() or p in local.upper() for p in ("EDGE", "FR", "AM", "PA"))
        ) or ("EDGE" in local.upper() and "EDGE" in remote.upper())
        if not is_dci:
            continue
        if datacenter and datacenter.upper() not in local.upper() and datacenter.upper() not in remote.upper():
            continue
        sessions.append(
            {
                "name": attr_val(n, "name"),
                "local_device": local,
                "remote_device": remote,
                "local_as": attr_val(n, "local_as"),
                "remote_as": attr_val(n, "remote_as"),
                "type": attr_val(n, "type"),
                "status": attr_val(n, "status"),
            }
        )

    return MCPResponse(
        status=MCPToolStatus.SUCCESS,
        data={"sessions": sessions, "count": len(sessions)},
    )
