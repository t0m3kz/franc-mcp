"""Customer boarding tools — namespace, virtual firewall, VXLAN segments per tenant."""

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Annotated, Any

from fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field, field_validator

from franc.constants import (
    DEFAULT_ENVIRONMENTS,
    DEFAULT_FIREWALL_PLATFORMS,
    DEFAULT_FIREWALL_TYPES,
    KIND_CUSTOMER,
    KIND_CUSTOMER_DC,
    KIND_PREFIX,
    KIND_VIRTUAL_DEVICE,
    KIND_VXLAN_SEGMENT,
)
from franc.utils import MCPResponse, MCPToolStatus, _gather_safe, _log_and_return_error, attr_val, require_client

if TYPE_CHECKING:
    from infrahub_sdk import InfrahubClient

mcp: FastMCP = FastMCP(name="Infrahub Customer Boarding")


# ---------------------------------------------------------------------------
# Internal models
# ---------------------------------------------------------------------------


class CustomerBoardingParams(BaseModel):
    customer_id: str
    customer_name: str
    datacenter: str
    environment: str = "p"
    firewall_type: str = "CloudGuard"
    firewall_platform: str = "checkpoint_gaia"
    hosting_device_active: str | None = None
    hosting_device_standby: str | None = None
    segments: list[dict[str, Any]] = Field(default_factory=list)
    branch_name: str | None = None

    @field_validator("customer_id")
    def validate_customer_id(cls, v: str) -> str:
        if not v or len(v) < 2:
            raise ValueError("Customer ID must be at least 2 characters (e.g. C001).")
        return v.upper()

    @field_validator("datacenter")
    def validate_datacenter(cls, v: str) -> str:
        if not v:
            raise ValueError("Datacenter must not be empty.")
        return v.upper()

    @field_validator("environment")
    def validate_environment(cls, v: str) -> str:
        if v not in DEFAULT_ENVIRONMENTS:
            raise ValueError(f"Environment must be one of: {DEFAULT_ENVIRONMENTS}.")
        return v


# ---------------------------------------------------------------------------
# Discovery tool
# ---------------------------------------------------------------------------


@mcp.tool(
    tags={"customer", "discover"},
    annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True),
)
async def discover_customer_options(
    ctx: Context,
    branch: Annotated[str | None, Field(default=None, description="Branch to read from (defaults to main).")],
) -> MCPResponse[dict[str, Any]]:
    """
    Discover existing options for customer boarding:
      - Existing customers (OrganizationCustomer)
      - Available datacenters (TopologyDataCenter)
      - Existing virtual firewall types
      - Available environments

    Note: customer deployments share Infrahub's built-in "default" IPAM namespace —
    there is no per-customer BuiltinIPNamespace/VRF to discover.

    Returns:
      {
        "customers": [...],
        "datacenters": [...],
        "firewall_types": [...],
        "firewall_platforms": [...],
        "environments": [...]
      }
    """
    try:
        client: InfrahubClient = require_client(ctx)
    except RuntimeError as exc:
        return await _log_and_return_error(ctx=ctx, error=str(exc), remediation="Start MCP with a configured client.")

    branch = branch or "main"

    # Parallel discovery — customers, DCs, virtual devices in one round-trip
    cust_coro = client.all(kind=KIND_CUSTOMER, branch=branch, prefetch_relationships=False)
    dc_coro = client.all(kind="TopologyDataCenter", branch=branch, prefetch_relationships=False)
    vdev_coro = client.all(kind=KIND_VIRTUAL_DEVICE, branch=branch, prefetch_relationships=False)

    cust_nodes, dc_nodes, vdev_nodes = await _gather_safe(cust_coro, dc_coro, vdev_coro)

    customers: set[str] = set()
    datacenters: set[str] = set()
    firewall_types: set[str] = set(DEFAULT_FIREWALL_TYPES)

    for cust in cust_nodes or []:
        org_id = attr_val(cust, "org_id")
        if org_id:
            customers.add(str(org_id))

    for dc in dc_nodes or []:
        label = attr_val(dc, "name")
        if label:
            datacenters.add(str(label))

    for vd in vdev_nodes or []:
        dt = attr_val(vd, "device_type")
        if dt:
            firewall_types.add(str(dt))

    return MCPResponse(
        status=MCPToolStatus.SUCCESS,
        data={
            "customers": sorted(customers),
            "datacenters": sorted(datacenters),
            "firewall_types": sorted(firewall_types),
            "firewall_platforms": DEFAULT_FIREWALL_PLATFORMS,
            "environments": DEFAULT_ENVIRONMENTS,
        },
    )


# ---------------------------------------------------------------------------
# Boarding tool
# ---------------------------------------------------------------------------


@mcp.tool(
    tags={"customer", "create"},
    annotations=ToolAnnotations(readOnlyHint=False, idempotentHint=False, destructiveHint=False),
)
async def create_customer_boarding(
    ctx: Context,
    customer_id: Annotated[
        str, Field(description="Customer identifier (e.g. C001). Must match OrganizationCustomer.org_id.")
    ],
    customer_name: Annotated[str, Field(description="Human-readable customer name (e.g. 'Nordix Ltd.').")],
    datacenter: Annotated[str, Field(description="Target datacenter name (e.g. DC3).")],
    environment: Annotated[str, Field(default="p", description="Environment: p=production, n=non-prod, d=dev.")],
    firewall_type: Annotated[str, Field(default="CloudGuard", description="Virtual firewall device type.")],
    firewall_platform: Annotated[str, Field(default="checkpoint_gaia", description="Firewall platform/OS.")],
    hosting_device_active: Annotated[str | None, Field(default=None, description="Active firewall chassis hostname.")],
    hosting_device_standby: Annotated[
        str | None, Field(default=None, description="Standby firewall chassis hostname.")
    ],
    segments: Annotated[
        list[dict[str, Any]],
        Field(
            default_factory=list,
            description=(
                "VXLAN segments to create. Each dict: "
                "{customer_name, prefix_cidr, gateway_ip, arp_suppression?}. "
                "Example: [{customer_name: 'web-frontend', prefix_cidr: '10.1.10.0/24', gateway_ip: '10.1.10.1/24'}]"
            ),
        ),
    ],
    branch_name: Annotated[
        str | None, Field(default=None, description="Branch for this boarding. Auto-generated if omitted.")
    ],
) -> MCPResponse[dict[str, Any]]:
    """
    Board a new customer onto the VXLAN fabric in three steps:
      1. Create TopologyCustomerDC (customer's footprint on this DC — owner + parent DC)
      2. Create DcimVirtualDevice(s) (active + standby virtual firewall)
      3. Create ManagedVxlanSegment(s) with PBR enabled, attached to the TopologyCustomerDC

    customer_id must already exist as an OrganizationCustomer.org_id — this tool does not
    create customers, only their deployment footprint. Customer deployments share Infrahub's
    built-in "default" IPAM namespace; there is no per-customer VRF to provision.

    All objects land on an isolated branch. Use validate_customer_boarding to verify.

    Returns:
      {"branch", "customer_deployment", "firewalls": [...], "segments": [...], "status"}
    """
    try:
        client: InfrahubClient = require_client(ctx)
    except RuntimeError as exc:
        return await _log_and_return_error(ctx=ctx, error=str(exc), remediation="Start MCP with a configured client.")

    # Validate params
    try:
        params = CustomerBoardingParams(
            customer_id=customer_id,
            customer_name=customer_name,
            datacenter=datacenter,
            environment=environment,
            firewall_type=firewall_type,
            firewall_platform=firewall_platform,
            hosting_device_active=hosting_device_active,
            hosting_device_standby=hosting_device_standby,
            segments=segments,
            branch_name=branch_name,
        )
    except Exception as exc:  # noqa: BLE001
        return await _log_and_return_error(ctx=ctx, error=exc, remediation="Fix invalid input fields.")

    # Auto-generate branch if not provided
    if not params.branch_name:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        params.branch_name = f"customer-{params.customer_id.lower()}-{params.datacenter.lower()}-{ts}"

    await ctx.info(f"Boarding {params.customer_id} on branch: {params.branch_name}")

    # Create branch
    try:
        await client.branch.create(
            branch_name=params.branch_name,
            sync_with_git=False,
            description=f"Customer boarding: {params.customer_id} on {params.datacenter}",
            wait_until_completion=True,
        )
    except Exception as exc:  # noqa: BLE001
        return await _log_and_return_error(
            ctx=ctx,
            error=exc,
            remediation="Ensure INFRAHUB_API_TOKEN has branch:create permissions.",
        )

    # ---- Step 1: TopologyCustomerDC (customer's footprint on this DC) ----
    # `name` is computed by Infrahub (org_id + environment + parent DC short name) —
    # not settable; the value below is a request placeholder only, read back after save.
    cust_dc_summary: dict[str, Any] = {}
    try:
        cust_dc_data: dict[str, Any] = {
            # org_id is a read-only computed_attribute derived from owner — not settable.
            "environment": params.environment,
            "owner": [params.customer_id],
            "parent": [params.datacenter],
        }
        cust_dc_node = await client.create(kind=KIND_CUSTOMER_DC, data=cust_dc_data, branch=params.branch_name)
        await cust_dc_node.save(allow_upsert=True)
        cust_dc_summary = {"id": getattr(cust_dc_node, "id", None), "name": attr_val(cust_dc_node, "name")}
        await ctx.info(f"Created customer deployment: {cust_dc_summary.get('name')}")
    except Exception as exc:  # noqa: BLE001
        return await _log_and_return_error(
            ctx=ctx,
            error=exc,
            remediation=(
                f"Ensure OrganizationCustomer with org_id='{params.customer_id}' exists "
                f"and TopologyDataCenter '{params.datacenter}' exists."
            ),
        )
    cust_dc_id = cust_dc_summary.get("id")

    # ---- Step 2: Virtual firewalls ----
    fw_active_name = f"{params.datacenter}-{params.customer_id}-FW-01"
    fw_standby_name = f"{params.datacenter}-{params.customer_id}-FW-02"
    firewall_summaries: list[dict[str, Any]] = []

    for fw_name, fw_status, host_device in [
        (fw_active_name, "active", params.hosting_device_active),
        (fw_standby_name, "active", params.hosting_device_standby),
    ]:
        try:
            fw_data: dict[str, Any] = {
                "name": fw_name,
                "role": "firewall",
                "status": fw_status,
                "device_type": [params.firewall_type],
                "platform": [params.firewall_platform],
                "deployment": [params.datacenter],
                "owner": [params.customer_id],
            }
            if host_device:
                fw_data["hosting_device"] = host_device
            fw_node = await client.create(kind=KIND_VIRTUAL_DEVICE, data=fw_data, branch=params.branch_name)
            await fw_node.save(allow_upsert=True)
            firewall_summaries.append({"id": getattr(fw_node, "id", None), "name": fw_name})
            await ctx.info(f"Created virtual firewall: {fw_name}")
        except Exception as exc:  # noqa: BLE001
            await ctx.warning(f"Could not create {fw_name}: {exc}")

    # ---- Step 3: VXLAN segments ----
    segment_summaries: list[dict[str, Any]] = []
    for seg in params.segments:
        seg_customer_name = seg.get("customer_name", "segment")
        prefix_cidr = seg.get("prefix_cidr", "")
        gateway_ip = seg.get("gateway_ip", "")
        arp_suppression = seg.get("arp_suppression", True)

        try:
            if prefix_cidr:
                prefix_node = await client.create(
                    kind=KIND_PREFIX,
                    data={"prefix": prefix_cidr, "status": "active"},
                    branch=params.branch_name,
                )
                await prefix_node.save(allow_upsert=True)

            gateway_ref: dict[str, Any] | None = None
            if gateway_ip:
                gw_node = await client.create(
                    kind="IpamIPAddress",
                    data={"address": gateway_ip},
                    branch=params.branch_name,
                )
                await gw_node.save(allow_upsert=True)
                gateway_ref = {"id": gw_node.id}

            # NOTE: segment.inline_service targets ManagedGeneric, whose firewall-side
            # implementers are ManagedFirewallHA/ManagedFirewallContext (an HA cluster node
            # joined to its member devices via DcimCapabilities) — not a bare DcimVirtualDevice
            # like fw_active_name above. Wiring a segment to its firewall pair requires creating
            # that HA cluster node first; until that's implemented here, pbr_enabled is left on
            # but inline_service is intentionally omitted rather than pointed at the wrong kind.
            seg_data: dict[str, Any] = {
                "customer_name": seg_customer_name,
                "environment": params.environment,
                "owner": [params.customer_id],
                "arp_suppression": arp_suppression,
                "pbr_enabled": True,
                "customer_deployments": [{"id": cust_dc_id}] if cust_dc_id else [],
            }
            if gateway_ref:
                seg_data["gateway"] = gateway_ref
            seg_node = await client.create(kind=KIND_VXLAN_SEGMENT, data=seg_data, branch=params.branch_name)
            await seg_node.save(allow_upsert=True)
            segment_summaries.append({"name": seg_customer_name, "prefix": prefix_cidr})
            await ctx.info(f"Created VXLAN segment: {seg_customer_name} ({prefix_cidr})")
        except Exception as exc:  # noqa: BLE001
            await ctx.warning(f"Could not create segment '{seg_customer_name}': {exc}")

    return MCPResponse(
        status=MCPToolStatus.SUCCESS,
        data={
            "branch": params.branch_name,
            "customer_deployment": cust_dc_summary,
            "firewalls": firewall_summaries,
            "segments": segment_summaries,
            "status": "boarded",
        },
    )


# ---------------------------------------------------------------------------
# Validation tool
# ---------------------------------------------------------------------------


@mcp.tool(
    tags={"customer", "validate"},
    annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True),
)
async def validate_customer_boarding(
    ctx: Context,
    branch: Annotated[str, Field(description="Branch where boarding was created.")],
    customer_id: Annotated[str, Field(description="Customer ID used during boarding (e.g. C001).")],
) -> MCPResponse[dict[str, Any]]:
    """
    Validate customer boarding on a given branch:
      - TopologyCustomerDC exists for customer_id on this DC
      - Virtual firewalls (DcimVirtualDevice) exist for customer
      - ManagedVxlanSegment(s) exist for customer

    Returns summary with counts and names.
    """
    try:
        client: InfrahubClient = require_client(ctx)
    except RuntimeError as exc:
        return await _log_and_return_error(ctx=ctx, error=str(exc), remediation="Start MCP with a configured client.")

    customer_id = customer_id.upper()
    await ctx.info(f"Validating boarding for {customer_id} on branch {branch}...")

    deployment_found: list[str] = []
    fw_found: list[str] = []
    seg_found: list[str] = []

    # Check customer deployment (TopologyCustomerDC)
    try:
        deployments = await client.filters(kind=KIND_CUSTOMER_DC, branch=branch, org_id__value=customer_id)
        for dep in deployments:
            name = attr_val(dep, "name")
            if name:
                deployment_found.append(str(name))
    except Exception:  # noqa: BLE001
        pass

    # Check virtual firewalls
    try:
        vdevices = await client.all(kind=KIND_VIRTUAL_DEVICE, branch=branch, prefetch_relationships=False)
        for vd in vdevices:
            name = attr_val(vd, "name") or ""
            if customer_id in name:
                fw_found.append(name)
    except Exception:  # noqa: BLE001
        pass

    # Check segments
    try:
        segments = await client.filters(kind=KIND_VXLAN_SEGMENT, branch=branch, owner__org_id__value=customer_id)
        for seg in segments:
            cn = attr_val(seg, "customer_name")
            if cn:
                seg_found.append(str(cn))
    except Exception:  # noqa: BLE001
        pass

    valid = len(deployment_found) > 0
    return MCPResponse(
        status=MCPToolStatus.SUCCESS,
        data={
            "boarding_valid": valid,
            "customer_id": customer_id,
            "customer_deployments": deployment_found,
            "firewalls": fw_found,
            "segments": seg_found,
            "summary": {
                "deployment_count": len(deployment_found),
                "firewall_count": len(fw_found),
                "segment_count": len(seg_found),
            },
        },
    )
