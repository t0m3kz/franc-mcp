"""Cloud infrastructure tools — AWS/Azure regions, clusters, instances, networking."""

from typing import TYPE_CHECKING, Annotated, Any

from fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from franc.utils import MCPResponse, MCPToolStatus, _gather_safe, _log_and_return_error, attr_val, require_client

if TYPE_CHECKING:
    from infrahub_sdk import InfrahubClient

mcp: FastMCP = FastMCP(name="Infrahub Cloud")

KIND_CLOUD = "TopologyCloud"
KIND_CLOUD_REGION = "TopologyCloudRegion"
KIND_CLOUD_ZONE = "TopologyCloudZone"
KIND_CLOUD_ACCOUNT = "CloudAccount"
KIND_CLOUD_INSTANCE = "CloudInstance"
KIND_CLOUD_VPN = "CloudVirtualNetwork"
KIND_CLOUD_SEGMENT = "CloudNetworkSegment"
KIND_CLOUD_LB = "CloudLoadBalancer"
KIND_CLOUD_DX = "CloudDirectConnect"
KIND_CLOUD_VIF = "CloudVirtualInterface"
KIND_VIRT_CLUSTER = "VirtCluster"
KIND_VIRT_NODE_POOL = "VirtNodePool"


@mcp.tool(
    tags={"cloud", "discover"},
    annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True),
)
async def get_cloud_summary(
    ctx: Context,
    branch: Annotated[str, Field(default="main", description="Branch to query.")],
) -> MCPResponse[dict[str, Any]]:
    """
    Return a summary of all cloud deployments — providers, regions, accounts, clusters, instances.

    Direct Connect and ExpressRoute are both modeled as `CloudDirectConnect`; the provider
    (AWS/Azure/GCP) is reachable via the `account` relationship.

    Returns:
        {"accounts": [...], "clusters": [...], "instance_count": N,
         "direct_connects": [...], "load_balancers": [...]}
    """
    try:
        client: InfrahubClient = require_client(ctx)
    except RuntimeError as exc:
        return await _log_and_return_error(ctx=ctx, error=str(exc), remediation="Start MCP with a configured client.")

    await ctx.info("Fetching cloud summary...")

    results = await _gather_safe(
        client.all(kind=KIND_CLOUD_ACCOUNT, branch=branch, prefetch_relationships=False),
        client.all(kind=KIND_VIRT_CLUSTER, branch=branch, prefetch_relationships=False),
        client.all(kind=KIND_CLOUD_INSTANCE, branch=branch, prefetch_relationships=False),
        client.all(kind=KIND_CLOUD_DX, branch=branch, prefetch_relationships=False),
        client.all(kind=KIND_CLOUD_LB, branch=branch, prefetch_relationships=False),
    )
    accounts_r, clusters_r, instances_r, dx_r, lb_r = results

    accounts = [{"name": attr_val(n, "name"), "provider": attr_val(n, "provider")} for n in (accounts_r or [])]
    clusters = [
        {
            "name": attr_val(n, "name"),
            "platform": attr_val(n, "platform"),
            "version": attr_val(n, "version"),
            "status": attr_val(n, "status"),
            "deployment": attr_val(n, "deployment"),
            "account": attr_val(n, "cloud_account"),
        }
        for n in (clusters_r or [])
    ]
    direct_connects = [
        {
            "name": attr_val(n, "name"),
            "account": attr_val(n, "account"),
            "region": attr_val(n, "region"),
            "bandwidth": attr_val(n, "bandwidth"),
            "connection_type": attr_val(n, "connection_type"),
        }
        for n in (dx_r or [])
    ]
    load_balancers = [
        {"name": attr_val(n, "name"), "account": attr_val(n, "account"), "region": attr_val(n, "region")}
        for n in (lb_r or [])
    ]

    return MCPResponse(
        status=MCPToolStatus.SUCCESS,
        data={
            "accounts": accounts,
            "clusters": clusters,
            "instance_count": len(instances_r or []),
            "direct_connects": direct_connects,
            "load_balancers": load_balancers,
        },
    )


@mcp.tool(
    tags={"cloud", "retrieve"},
    annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True),
)
async def get_cloud_instances(
    ctx: Context,
    account: Annotated[str | None, Field(default=None, description="Filter by cloud account name (e.g. C001-AWS).")],
    region: Annotated[
        str | None, Field(default=None, description="Filter by availability zone/region (e.g. eu-central-1a).")
    ],
    branch: Annotated[str, Field(default="main", description="Branch to query.")],
) -> MCPResponse[dict[str, Any]]:
    """
    List cloud instances (EC2, AKS nodes, etc.) optionally filtered by account or region/zone.

    Returns:
        {"instances": [{"name", "account", "zone", "instance_type", "private_ip", "status"}]}
    """
    try:
        client: InfrahubClient = require_client(ctx)
    except RuntimeError as exc:
        return await _log_and_return_error(ctx=ctx, error=str(exc), remediation="Start MCP with a configured client.")

    await ctx.info(f"Fetching cloud instances (account={account}, region={region})...")

    filters: dict[str, Any] = {}
    if account:
        filters["account__name__value"] = account

    try:
        nodes = (
            await client.filters(kind=KIND_CLOUD_INSTANCE, branch=branch, **filters)
            if filters
            else await client.all(kind=KIND_CLOUD_INSTANCE, branch=branch, prefetch_relationships=False)
        )
    except Exception as exc:  # noqa: BLE001
        return await _log_and_return_error(
            ctx=ctx, error=exc, remediation="Verify KIND_CLOUD_INSTANCE exists in schema."
        )

    instances = []
    for n in nodes:
        zone = attr_val(n, "availability_zone")
        if region and zone and region not in str(zone):
            continue
        instances.append(
            {
                "name": attr_val(n, "name"),
                "account": attr_val(n, "account"),
                "zone": zone,
                "instance_type": attr_val(n, "instance_type"),
                "private_ip": attr_val(n, "private_ip"),
                "status": attr_val(n, "status"),
                "cpu": attr_val(n, "cpu_count"),
                "memory_gb": attr_val(n, "memory_gb"),
            }
        )

    return MCPResponse(
        status=MCPToolStatus.SUCCESS,
        data={"instances": instances, "count": len(instances)},
    )


@mcp.tool(
    tags={"cloud", "retrieve"},
    annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True),
)
async def get_cloud_clusters(
    ctx: Context,
    account: Annotated[str | None, Field(default=None, description="Filter by cloud account (e.g. C001-AWS).")],
    branch: Annotated[str, Field(default="main", description="Branch to query.")],
) -> MCPResponse[dict[str, Any]]:
    """
    List Kubernetes / managed clusters (EKS, AKS) with their node pools and capabilities.

    Returns:
        {"clusters": [{"name", "platform", "version", "status", "node_pools": [...]}]}
    """
    try:
        client: InfrahubClient = require_client(ctx)
    except RuntimeError as exc:
        return await _log_and_return_error(ctx=ctx, error=str(exc), remediation="Start MCP with a configured client.")

    await ctx.info(f"Fetching clusters (account={account})...")

    cluster_coro = (
        client.filters(kind=KIND_VIRT_CLUSTER, branch=branch, cloud_account__name__value=account)
        if account
        else client.all(kind=KIND_VIRT_CLUSTER, branch=branch, prefetch_relationships=False)
    )
    pools_r, clusters_r = await _gather_safe(
        client.all(kind=KIND_VIRT_NODE_POOL, branch=branch, prefetch_relationships=False),
        cluster_coro,
    )

    pool_by_cluster: dict[str, list[dict[str, Any]]] = {}
    for p in pools_r or []:
        cluster_name = attr_val(p, "cluster")
        if not cluster_name:
            continue
        pool_by_cluster.setdefault(str(cluster_name), []).append(
            {
                "name": attr_val(p, "name"),
                "pool_type": attr_val(p, "pool_type"),
                "scaling_mode": attr_val(p, "scaling_mode"),
                "min_size": attr_val(p, "min_size"),
                "max_size": attr_val(p, "max_size"),
                "desired": attr_val(p, "desired_capacity"),
                "instance_flavor": attr_val(p, "instance_flavor"),
            }
        )

    clusters = []
    for c in clusters_r or []:
        name = attr_val(c, "name") or ""
        clusters.append(
            {
                "name": name,
                "platform": attr_val(c, "platform"),
                "version": attr_val(c, "version"),
                "status": attr_val(c, "status"),
                "ha_enabled": attr_val(c, "ha_enabled"),
                "deployment": attr_val(c, "deployment"),
                "account": attr_val(c, "cloud_account"),
                "node_pools": pool_by_cluster.get(name, []),
            }
        )

    return MCPResponse(
        status=MCPToolStatus.SUCCESS,
        data={"clusters": clusters, "count": len(clusters)},
    )


@mcp.tool(
    tags={"cloud", "retrieve"},
    annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True),
)
async def get_cloud_connectivity(
    ctx: Context,
    account: Annotated[str | None, Field(default=None, description="Filter by cloud account name.")],
    branch: Annotated[str, Field(default="main", description="Branch to query.")],
) -> MCPResponse[dict[str, Any]]:
    """
    Return cloud connectivity summary: Direct Connect / ExpressRoute and virtual interfaces.
    Shows the physical + virtual path from DC/Equinix into cloud.

    Direct Connect and ExpressRoute are both modeled as `CloudDirectConnect`
    (`connection_type`: dedicated/hosted); the cloud provider is reachable via `account`.

    Returns:
        {"direct_connects": [...], "virtual_interfaces": [...]}
    """
    try:
        client: InfrahubClient = require_client(ctx)
    except RuntimeError as exc:
        return await _log_and_return_error(ctx=ctx, error=str(exc), remediation="Start MCP with a configured client.")

    await ctx.info("Fetching cloud connectivity...")

    dx_r, vif_r = await _gather_safe(
        client.all(kind=KIND_CLOUD_DX, branch=branch, prefetch_relationships=False),
        client.all(kind=KIND_CLOUD_VIF, branch=branch, prefetch_relationships=False),
    )

    def _filter(nodes: list | None) -> list:
        if not nodes:
            return []
        if account:
            return [n for n in nodes if str(attr_val(n, "account") or "") == account]
        return nodes

    dx = [
        {
            "name": attr_val(n, "name"),
            "account": attr_val(n, "account"),
            "region": attr_val(n, "region"),
            "bandwidth": attr_val(n, "bandwidth"),
            "connection_type": attr_val(n, "connection_type"),
            "vlan_id": attr_val(n, "vlan_id"),
            "location": attr_val(n, "location"),
        }
        for n in _filter(dx_r)
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
        data={"direct_connects": dx, "virtual_interfaces": vifs},
    )
