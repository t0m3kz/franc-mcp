"""External connectivity tools — ISPs, partner networks, BGP peering inventory."""

from typing import TYPE_CHECKING, Annotated, Any

from fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from franc.utils import MCPResponse, MCPToolStatus, _gather_safe, _log_and_return_error, attr_val, require_client

if TYPE_CHECKING:
    from infrahub_sdk import InfrahubClient

mcp: FastMCP = FastMCP(name="Infrahub External Connectivity")

KIND_EXTERNAL = "TopologyExternal"
KIND_EXTERNAL_PROVIDER = "TopologyExternalProviders"
KIND_EXT_PEER = "ManagedExternalPeer"
KIND_BGP = "ManagedBGPPeering"

# Demo reference data
DEMO_ISPS = [
    {"name": "BT", "type": "TRANSIT", "circuit": "BT-INET-PRIMARY"},
    {"name": "BT UK", "type": "TRANSIT", "circuit": "BT-UK-INET"},
    {"name": "Colt", "type": "TRANSIT", "circuit": "COLT-INET"},
    {"name": "DE-CIX", "type": "IXP", "circuit": "DE-CIX-FR-RS"},
]
DEMO_PARTNERS = [
    {"name": "ACME", "type": "PARTNER", "circuit": "ACME-MPLS-01"},
    {"name": "GLOBEX", "type": "PARTNER", "circuit": "GLOBEX-IPVPN-01"},
]


@mcp.tool(
    tags={"external", "discover"},
    annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True),
)
async def get_external_providers(
    ctx: Context,
    branch: Annotated[str, Field(default="main", description="Branch to query.")],
) -> MCPResponse[dict[str, Any]]:
    """
    Return a summary of all external providers — ISPs (TRANSIT/IXP) and partner networks (PARTNER/MPLS).

    Demo ISPs: BT (transit), BT UK (transit), Colt (transit), DE-CIX (IXP Frankfurt).
    Demo partners: ACME Corp (MPLS/IPVPN), GLOBEX (IPVPN).

    Returns:
        {"isps": [...], "partners": [...], "external_peers": [...]}
    """
    try:
        client: InfrahubClient = require_client(ctx)
    except RuntimeError as exc:
        return await _log_and_return_error(ctx=ctx, error=str(exc), remediation="Start MCP with a configured client.")

    await ctx.info("Fetching external providers and peers...")

    ext_r, peers_r = await _gather_safe(
        client.all(kind=KIND_EXTERNAL_PROVIDER, branch=branch, prefetch_relationships=False),
        client.all(kind=KIND_EXT_PEER, branch=branch, prefetch_relationships=False),
    )

    isps = []
    partners = []
    for n in ext_r or []:
        pt = str(attr_val(n, "provider_type") or "").lower()
        entry = {"name": attr_val(n, "name"), "provider": attr_val(n, "provider"), "status": attr_val(n, "status")}
        if pt in ("internet", "exchange"):
            isps.append(entry)
        else:
            partners.append(entry)

    peers = [
        {
            "name": attr_val(n, "name"),
            "peer_type": attr_val(n, "peer_type"),
            "provider_name": attr_val(n, "provider_name"),
            "circuit_id": attr_val(n, "circuit_id"),
        }
        for n in (peers_r or [])
    ]

    # Use demo reference if no data returned
    if not isps:
        isps = DEMO_ISPS
    if not partners:
        partners = DEMO_PARTNERS

    return MCPResponse(
        status=MCPToolStatus.SUCCESS,
        data={
            "isps": isps,
            "partners": partners,
            "external_peers": peers,
            "isp_count": len(isps),
            "partner_count": len(partners),
        },
    )


@mcp.tool(
    tags={"external", "bgp", "retrieve"},
    annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True),
)
async def get_external_bgp_sessions(
    ctx: Context,
    provider: Annotated[
        str | None, Field(default=None, description="Filter by provider name (e.g. BT, DE-CIX, ACME).")
    ],
    session_type: Annotated[str | None, Field(default=None, description="Filter by type: TRANSIT, IXP, PARTNER.")],
    branch: Annotated[str, Field(default="main", description="Branch to query.")],
) -> MCPResponse[dict[str, Any]]:
    """
    List all BGP sessions to external providers (ISPs, IXPs, partners).

    Returns:
        {"sessions": [{"name", "local_device", "remote_device", "local_as", "remote_as", "type", "status"}]}
    """
    try:
        client: InfrahubClient = require_client(ctx)
    except RuntimeError as exc:
        return await _log_and_return_error(ctx=ctx, error=str(exc), remediation="Start MCP with a configured client.")

    await ctx.info(f"Fetching external BGP sessions (provider={provider}, type={session_type})...")

    try:
        nodes = await client.all(kind=KIND_BGP, branch=branch, prefetch_relationships=False)
    except Exception as exc:  # noqa: BLE001
        return await _log_and_return_error(ctx=ctx, error=exc, remediation="Check schema for ManagedBGPPeering.")

    # External BGP sessions involve non-fabric ASNs — filter by remote_device naming
    sessions = []
    for n in nodes:
        remote = str(attr_val(n, "remote_device") or "")
        stype = str(attr_val(n, "type") or "")
        # Heuristic: external sessions have remote devices prefixed EXT-, ISP-, FR*-EDGE, AM*-EDGE, PA*-EDGE
        is_external = any(remote.startswith(p) for p in ("EXT-", "ISP-", "FR", "AM", "PA", "DE-CIX"))
        if (
            not is_external
            and not any(kw in remote.upper() for kw in ("EDGE", "PEER", "TRANSIT", "PARTNER"))
            and stype.lower() not in ("ebgp", "transit", "ixp", "partner")
        ):
            continue

        if (
            provider
            and provider.upper() not in remote.upper()
            and provider.upper() not in str(attr_val(n, "local_device") or "").upper()
        ):
            continue
        if session_type and stype.lower() != session_type.lower():
            continue

        sessions.append(
            {
                "name": attr_val(n, "name"),
                "local_device": attr_val(n, "local_device"),
                "remote_device": remote,
                "local_as": attr_val(n, "local_as"),
                "remote_as": attr_val(n, "remote_as"),
                "type": stype,
                "status": attr_val(n, "status"),
            }
        )

    return MCPResponse(
        status=MCPToolStatus.SUCCESS,
        data={"sessions": sessions, "count": len(sessions)},
    )


# Partner circuit queries live in franc.tools.interconnects.get_circuits(scope="partner").
