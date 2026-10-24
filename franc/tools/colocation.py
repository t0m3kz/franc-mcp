"""Colocation tools — Equinix/Megaport metros, zones, edge devices, cross-connects."""

from typing import TYPE_CHECKING, Annotated, Any

from fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from franc.utils import MCPResponse, MCPToolStatus, _gather_safe, _log_and_return_error, attr_val, require_client

if TYPE_CHECKING:
    from infrahub_sdk import InfrahubClient

mcp: FastMCP = FastMCP(name="Infrahub Colocation")

KIND_COLO = "TopologyColocation"
KIND_COLO_METRO = "TopologyColocationMetro"
KIND_COLO_ZONE = "TopologyColocationZone"
KIND_DEVICE = "DcimPhysicalDevice"
KIND_BUILDING = "LocationBuilding"


@mcp.tool(
    tags={"colocation", "discover"},
    annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True),
)
async def get_colocation_summary(
    ctx: Context,
    branch: Annotated[str, Field(default="main", description="Branch to query.")],
) -> MCPResponse[dict[str, Any]]:
    """
    Return a summary of all colocation facilities — providers, metros, zones, and edge device counts.

    Providers in the demo: EQUINIX (AM/FR/PA), MEGAPORT (AMS/FRA/PAR), CORESITE (NY/VA).

    Returns:
        {"providers": [...], "metros": [...], "zones": [...], "device_count": N}
    """
    try:
        client: InfrahubClient = require_client(ctx)
    except RuntimeError as exc:
        return await _log_and_return_error(ctx=ctx, error=str(exc), remediation="Start MCP with a configured client.")

    await ctx.info("Fetching colocation summary...")

    colo_r, metro_r, zone_r = await _gather_safe(
        client.all(kind=KIND_COLO, branch=branch, prefetch_relationships=False),
        client.all(kind=KIND_COLO_METRO, branch=branch, prefetch_relationships=False),
        client.all(kind=KIND_COLO_ZONE, branch=branch, prefetch_relationships=False),
    )

    providers = [{"name": attr_val(n, "name")} for n in (colo_r or [])]
    metros = [{"name": attr_val(n, "name"), "location": attr_val(n, "location")} for n in (metro_r or [])]
    zones = [{"name": attr_val(n, "name"), "deployment_type": attr_val(n, "deployment_type")} for n in (zone_r or [])]

    return MCPResponse(
        status=MCPToolStatus.SUCCESS,
        data={
            "providers": providers,
            "metros": metros,
            "zones": zones,
            "provider_count": len(providers),
            "metro_count": len(metros),
            "zone_count": len(zones),
        },
    )


@mcp.tool(
    tags={"colocation", "retrieve"},
    annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True),
)
async def get_colocation_devices(
    ctx: Context,
    zone: Annotated[str | None, Field(default=None, description="Filter by zone name (e.g. FR2, AM1, PA4).")],
    branch: Annotated[str, Field(default="main", description="Branch to query.")],
) -> MCPResponse[dict[str, Any]]:
    """
    List edge devices at colocation sites (edge routers, patch panels, VCG gateways).

    Returns:
        {"devices": [{"name", "deployment", "status", "role"}]}
    """
    try:
        client: InfrahubClient = require_client(ctx)
    except RuntimeError as exc:
        return await _log_and_return_error(ctx=ctx, error=str(exc), remediation="Start MCP with a configured client.")

    await ctx.info(f"Fetching colocation devices (zone={zone})...")

    try:
        if zone:
            nodes = await client.filters(kind=KIND_DEVICE, branch=branch, deployment__name__value=zone)
        else:
            nodes = await client.all(kind=KIND_DEVICE, branch=branch, prefetch_relationships=False)
    except Exception as exc:  # noqa: BLE001
        return await _log_and_return_error(ctx=ctx, error=exc, remediation="Check schema for DcimPhysicalDevice.")

    # Colo devices have deployment names matching zone shortnames (FR2, AM1, etc.)
    colo_zone_prefixes = {"FR", "AM", "PA", "NY", "VA", "MUC", "AMS", "FRA", "PAR", "STR", "EQX", "EXT"}
    devices = []
    for n in nodes:
        name = attr_val(n, "name") or ""
        deployment = attr_val(n, "deployment") or ""
        # Filter to only colo-looking deployments unless explicitly filtered
        if not zone:
            dep_upper = str(deployment).upper()
            if not any(dep_upper.startswith(p) for p in colo_zone_prefixes):
                continue
        devices.append(
            {
                "name": name,
                "deployment": deployment,
                "status": attr_val(n, "status"),
                "role": attr_val(n, "role"),
            }
        )

    return MCPResponse(
        status=MCPToolStatus.SUCCESS,
        data={"devices": devices, "count": len(devices)},
    )


# Physical circuit queries live in franc.tools.interconnects.get_circuits(scope=...).
