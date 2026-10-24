"""Office and branch site tools — TopologyOffice, edge devices, WAN connectivity."""

from typing import TYPE_CHECKING, Annotated, Any

from fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from franc.utils import MCPResponse, MCPToolStatus, _gather_safe, _log_and_return_error, attr_val, require_client

if TYPE_CHECKING:
    from infrahub_sdk import InfrahubClient

mcp: FastMCP = FastMCP(name="Infrahub Offices")

KIND_OFFICE = "TopologyOffice"
KIND_DEVICE = "DcimPhysicalDevice"
KIND_PHYS_CIRCUIT = "TopologyPhysicalCircuit"
KIND_BGP = "ManagedBGPPeering"

# Known office deployments in the demo
DEMO_OFFICES = [
    {"name": "OFC-LON-BRANCH-01", "city": "London", "type": "branch"},
    {"name": "OFC-EDI-BRANCH-01", "city": "Edinburgh", "type": "branch"},
    {"name": "OFC-BCN-BRANCH-01", "city": "Barcelona", "type": "branch"},
    {"name": "OFC-MAD-REGIONAL-01", "city": "Madrid", "type": "regional"},
    {"name": "OFC-STR-BRANCH-01", "city": "Stuttgart", "type": "branch"},
]


@mcp.tool(
    tags={"offices", "discover"},
    annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True),
)
async def get_office_summary(
    ctx: Context,
    branch: Annotated[str, Field(default="main", description="Branch to query.")],
) -> MCPResponse[dict[str, Any]]:
    """
    Return a summary of all office/branch sites — names, types, devices, and WAN circuit counts.

    Demo offices: London (branch), Edinburgh (branch), Barcelona (branch), Madrid (regional), Stuttgart (branch).
    All connect via Equinix SD-WAN (Arista VCE edges) to FR/PA metros.

    Returns:
        {"offices": [{"name", "city", "type", "device_count", "circuit_count"}]}
    """
    try:
        client: InfrahubClient = require_client(ctx)
    except RuntimeError as exc:
        return await _log_and_return_error(ctx=ctx, error=str(exc), remediation="Start MCP with a configured client.")

    await ctx.info("Fetching office topology...")

    offices_r, devices_r, circuits_r = await _gather_safe(
        client.all(kind=KIND_OFFICE, branch=branch, prefetch_relationships=False),
        client.all(kind=KIND_DEVICE, branch=branch, prefetch_relationships=False),
        client.all(kind=KIND_PHYS_CIRCUIT, branch=branch, prefetch_relationships=False),
    )

    # Index devices and circuits by deployment name
    dev_by_dep: dict[str, int] = {}
    for d in devices_r or []:
        dep = str(attr_val(d, "deployment") or "")
        if dep.startswith("OFC-"):
            dev_by_dep[dep] = dev_by_dep.get(dep, 0) + 1

    circ_by_loc: dict[str, int] = {}
    for c in circuits_r or []:
        locs = attr_val(c, "locations") or []
        for loc in locs if isinstance(locs, list) else [locs]:
            loc_str = str(loc)
            if "OFC" in loc_str:
                circ_by_loc[loc_str] = circ_by_loc.get(loc_str, 0) + 1

    offices = []
    for o in offices_r or []:
        name = attr_val(o, "name") or ""
        offices.append(
            {
                "name": name,
                "site_type": attr_val(o, "site_type"),
                "locations": attr_val(o, "locations"),
                "device_count": dev_by_dep.get(name, 0),
                "circuit_count": circ_by_loc.get(name, 0),
            }
        )

    # Fall back to demo reference if Infrahub returns nothing
    if not offices:
        offices = DEMO_OFFICES

    return MCPResponse(
        status=MCPToolStatus.SUCCESS,
        data={"offices": offices, "count": len(offices)},
    )


@mcp.tool(
    tags={"offices", "retrieve"},
    annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True),
)
async def get_office_devices(
    ctx: Context,
    office: Annotated[str, Field(description="Office topology name (e.g. OFC-LON-BRANCH-01).")],
    branch: Annotated[str, Field(default="main", description="Branch to query.")],
) -> MCPResponse[dict[str, Any]]:
    """
    List all devices at a branch/office site — edge routers, access switches, APs.

    Returns:
        {"devices": [{"name", "status", "role"}]}
    """
    try:
        client: InfrahubClient = require_client(ctx)
    except RuntimeError as exc:
        return await _log_and_return_error(ctx=ctx, error=str(exc), remediation="Start MCP with a configured client.")

    await ctx.info(f"Fetching devices at {office}...")

    try:
        nodes = await client.filters(kind=KIND_DEVICE, branch=branch, deployment__name__value=office)
    except Exception as exc:  # noqa: BLE001
        return await _log_and_return_error(
            ctx=ctx, error=exc, remediation="Check office name with get_office_summary()."
        )

    devices = [
        {
            "name": attr_val(n, "name"),
            "status": attr_val(n, "status"),
            "role": attr_val(n, "role"),
        }
        for n in nodes
    ]

    return MCPResponse(
        status=MCPToolStatus.SUCCESS,
        data={"office": office, "devices": devices, "count": len(devices)},
    )


# Office WAN circuit queries live in franc.tools.interconnects.get_circuits(scope="office").
