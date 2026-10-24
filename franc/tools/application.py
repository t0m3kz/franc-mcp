"""Application deployment tools — portfolio, applications, components, VM mapping."""

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Annotated, Any

from fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field, field_validator

from franc.constants import (
    DEFAULT_APP_CRITICALITIES,
    DEFAULT_APP_ENVIRONMENTS,
    KIND_APP_APPLICATION,
    KIND_APP_COMPONENT,
    KIND_APP_PORTFOLIO,
    KIND_CLOUD_VM,
    KIND_VXLAN_SEGMENT,
)
from franc.utils import MCPResponse, MCPToolStatus, _log_and_return_error, require_client

if TYPE_CHECKING:
    from infrahub_sdk import InfrahubClient

mcp: FastMCP = FastMCP(name="Infrahub Application Deployment")


# ---------------------------------------------------------------------------
# Internal models
# ---------------------------------------------------------------------------


class AppDeploymentParams(BaseModel):
    customer_id: str
    applications: list[dict[str, Any]] = Field(default_factory=list)
    branch_name: str | None = None

    @field_validator("customer_id")
    def validate_customer_id(cls, v: str) -> str:
        if not v or len(v) < 2:
            raise ValueError("Customer ID must be at least 2 characters.")
        return v.upper()


# ---------------------------------------------------------------------------
# Discovery tool
# ---------------------------------------------------------------------------


@mcp.tool(
    tags={"application", "discover"},
    annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True),
)
async def discover_application_options(
    ctx: Context,
    branch: Annotated[str | None, Field(default=None, description="Branch to read from (defaults to main).")],
    customer_id: Annotated[
        str | None, Field(default=None, description="Filter by customer ID (e.g. C001). Returns all if omitted.")
    ],
) -> MCPResponse[dict[str, Any]]:
    """
    Discover existing application deployment options:
      - Existing portfolios (AppPortfolio) per customer
      - Existing applications (AppApplication) with labels, environments, criticalities
      - Available VXLAN segments for the customer (namespace context)
      - Available cloud VMs that can be linked to components

    Returns:
      {
        "portfolios": [...],
        "applications": [...],
        "segments": [...],
        "vms": [...],
        "criticalities": [...],
        "environments": [...]
      }
    """
    try:
        client: InfrahubClient = require_client(ctx)
    except RuntimeError as exc:
        return await _log_and_return_error(ctx=ctx, error=str(exc), remediation="Start MCP with a configured client.")

    branch = branch or "main"
    portfolios: list[dict[str, Any]] = []
    applications: list[dict[str, Any]] = []
    segments: list[dict[str, Any]] = []
    vms: list[str] = []

    # Portfolios
    try:
        portfolio_nodes = await client.all(kind=KIND_APP_PORTFOLIO, branch=branch, prefetch_relationships=False)
        for p in portfolio_nodes:
            owner = getattr(p, "owner", None)
            owner_val = str(owner.value) if owner and hasattr(owner, "value") else None
            if customer_id and owner_val and owner_val.upper() != customer_id.upper():
                continue
            portfolios.append({"id": getattr(p, "id", None), "owner": owner_val})
    except Exception:  # noqa: BLE001
        pass

    # Applications
    try:
        app_nodes = await client.all(kind=KIND_APP_APPLICATION, branch=branch, prefetch_relationships=False)
        for app in app_nodes:
            label_attr = getattr(app, "label", None)
            env_attr = getattr(app, "environment", None)
            crit_attr = getattr(app, "criticality", None)
            parent_attr = getattr(app, "parent", None)
            owner_val = str(parent_attr.value) if parent_attr and hasattr(parent_attr, "value") else None
            if customer_id and owner_val and owner_val.upper() != customer_id.upper():
                continue
            applications.append(
                {
                    "id": getattr(app, "id", None),
                    "label": str(label_attr.value) if label_attr and hasattr(label_attr, "value") else None,
                    "environment": str(env_attr.value) if env_attr and hasattr(env_attr, "value") else None,
                    "criticality": str(crit_attr.value) if crit_attr and hasattr(crit_attr, "value") else None,
                    "owner": owner_val,
                }
            )
    except Exception:  # noqa: BLE001
        pass

    # VXLAN segments for customer
    try:
        filter_kwargs: dict[str, Any] = {}
        if customer_id:
            filter_kwargs["owner__value"] = customer_id.upper()
        seg_nodes = await client.filters(kind=KIND_VXLAN_SEGMENT, branch=branch, **filter_kwargs)
        for seg in seg_nodes:
            cn = getattr(seg, "customer_name", None)
            segments.append({"name": str(cn.value) if cn and hasattr(cn, "value") else str(cn)})
    except Exception:  # noqa: BLE001
        pass

    # Cloud VMs
    try:
        vm_nodes = await client.all(kind=KIND_CLOUD_VM, branch=branch, prefetch_relationships=False)
        for vm in vm_nodes[:50]:  # cap to avoid massive responses
            label = getattr(vm, "display_label", None) or getattr(vm, "name", None)
            if label:
                vms.append(str(label.value) if hasattr(label, "value") else str(label))
    except Exception:  # noqa: BLE001
        pass

    return MCPResponse(
        status=MCPToolStatus.SUCCESS,
        data={
            "portfolios": portfolios,
            "applications": applications,
            "segments": segments,
            "vms": vms,
            "criticalities": DEFAULT_APP_CRITICALITIES,
            "environments": DEFAULT_APP_ENVIRONMENTS,
        },
    )


# ---------------------------------------------------------------------------
# Create tool
# ---------------------------------------------------------------------------


@mcp.tool(
    tags={"application", "create"},
    annotations=ToolAnnotations(readOnlyHint=False, idempotentHint=False, destructiveHint=False),
)
async def create_application_deployment(
    ctx: Context,
    customer_id: Annotated[str, Field(description="Customer owner ID (e.g. C001).")],
    applications: Annotated[
        list[dict[str, Any]],
        Field(
            description=(
                "List of application definitions. Each dict: "
                "{label, environment, criticality, components?}. "
                "components is a list of {name, segment?, vm?}. "
                "Example: [{label: 'trade-portal', environment: 'p', criticality: 'critical', "
                "components: [{name: 'frontend', segment: 'web-frontend', vm: 'app-vm-01'}]}]"
            )
        ),
    ],
    branch_name: Annotated[
        str | None, Field(default=None, description="Branch for deployment. Auto-generated if omitted.")
    ],
) -> MCPResponse[dict[str, Any]]:
    """
    Deploy applications for a customer in three steps:
      1. Ensure AppPortfolio exists for customer (create if missing)
      2. Create AppApplication entries (label + environment + criticality)
      3. Create AppComponent entries linked to applications, segments, and VMs

    All objects land on an isolated branch. Use validate_application_deployment to verify.

    Returns:
      {"branch", "portfolio_id", "applications": [...], "components": [...], "status"}
    """
    try:
        client: InfrahubClient = require_client(ctx)
    except RuntimeError as exc:
        return await _log_and_return_error(ctx=ctx, error=str(exc), remediation="Start MCP with a configured client.")

    try:
        params = AppDeploymentParams(customer_id=customer_id, applications=applications, branch_name=branch_name)
    except Exception as exc:  # noqa: BLE001
        return await _log_and_return_error(ctx=ctx, error=exc, remediation="Fix invalid input fields.")

    if not params.branch_name:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        params.branch_name = f"app-deploy-{params.customer_id.lower()}-{ts}"

    await ctx.info(f"Deploying apps for {params.customer_id} on branch: {params.branch_name}")

    # Create branch
    try:
        await client.branch.create(
            branch_name=params.branch_name,
            sync_with_git=False,
            description=f"App deployment: {params.customer_id}",
            wait_until_completion=True,
        )
    except Exception as exc:  # noqa: BLE001
        return await _log_and_return_error(
            ctx=ctx, error=exc, remediation="Ensure INFRAHUB_API_TOKEN has branch:create permissions."
        )

    # ---- Step 1: Portfolio ----
    portfolio_id: str | None = None
    try:
        existing = await client.filters(
            kind=KIND_APP_PORTFOLIO, branch=params.branch_name, owner__value=params.customer_id
        )
        if existing:
            portfolio_id = getattr(existing[0], "id", None)
            await ctx.info(f"Reusing existing portfolio for {params.customer_id}")
        else:
            p_node = await client.create(
                kind=KIND_APP_PORTFOLIO, data={"owner": params.customer_id}, branch=params.branch_name
            )
            await p_node.save(allow_upsert=True)
            portfolio_id = getattr(p_node, "id", None)
            await ctx.info(f"Created portfolio for {params.customer_id}")
    except Exception as exc:  # noqa: BLE001
        return await _log_and_return_error(ctx=ctx, error=exc, remediation="Portfolio creation failed.")

    app_summaries: list[dict[str, Any]] = []
    component_summaries: list[dict[str, Any]] = []

    for app_def in params.applications:
        label = app_def.get("label", "")
        environment = app_def.get("environment", "p")
        criticality = app_def.get("criticality", "medium")
        components = app_def.get("components", [])

        # ---- Step 2: AppApplication ----
        app_id: str | None = None
        try:
            app_node = await client.create(
                kind=KIND_APP_APPLICATION,
                data={
                    "label": label,
                    "environment": environment,
                    "criticality": criticality,
                    "parent": params.customer_id,
                },
                branch=params.branch_name,
            )
            await app_node.save(allow_upsert=True)
            app_id = getattr(app_node, "id", None)
            app_summaries.append({"label": label, "environment": environment, "criticality": criticality})
            await ctx.info(f"Created application: {label} ({environment})")
        except Exception as exc:  # noqa: BLE001
            await ctx.warning(f"Could not create app '{label}': {exc}")
            continue

        # ---- Step 3: AppComponent ----
        for comp_def in components:
            comp_name = comp_def.get("name", "")
            comp_segment = comp_def.get("segment")
            comp_vm = comp_def.get("vm")

            try:
                comp_data: dict[str, Any] = {
                    "name": comp_name,
                    "parent": app_id,
                }
                if comp_segment:
                    comp_data["network_segment"] = comp_segment
                if comp_vm:
                    comp_data["instances"] = [comp_vm]
                comp_node = await client.create(kind=KIND_APP_COMPONENT, data=comp_data, branch=params.branch_name)
                await comp_node.save(allow_upsert=True)
                component_summaries.append({"name": comp_name, "application": label, "segment": comp_segment})
                await ctx.info(f"Created component: {comp_name} → {label}")
            except Exception as exc:  # noqa: BLE001
                await ctx.warning(f"Could not create component '{comp_name}': {exc}")

    return MCPResponse(
        status=MCPToolStatus.SUCCESS,
        data={
            "branch": params.branch_name,
            "portfolio_id": portfolio_id,
            "applications": app_summaries,
            "components": component_summaries,
            "status": "deployed",
        },
    )


# ---------------------------------------------------------------------------
# Validation tool
# ---------------------------------------------------------------------------


@mcp.tool(
    tags={"application", "validate"},
    annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True),
)
async def validate_application_deployment(
    ctx: Context,
    branch: Annotated[str, Field(description="Branch where deployment was created.")],
    customer_id: Annotated[str, Field(description="Customer owner ID (e.g. C001).")],
) -> MCPResponse[dict[str, Any]]:
    """
    Validate application deployment on a given branch:
      - AppPortfolio exists for customer
      - AppApplication entries exist with expected fields
      - AppComponent entries exist

    Returns counts and labels.
    """
    try:
        client: InfrahubClient = require_client(ctx)
    except RuntimeError as exc:
        return await _log_and_return_error(ctx=ctx, error=str(exc), remediation="Start MCP with a configured client.")

    customer_id = customer_id.upper()
    await ctx.info(f"Validating app deployment for {customer_id} on branch {branch}...")

    portfolios: list[str] = []
    app_labels: list[str] = []
    component_names: list[str] = []

    try:
        ps = await client.filters(kind=KIND_APP_PORTFOLIO, branch=branch, owner__value=customer_id)
        portfolios = [getattr(p, "id", "?") for p in ps]
    except Exception:  # noqa: BLE001
        pass

    try:
        apps = await client.all(kind=KIND_APP_APPLICATION, branch=branch, prefetch_relationships=False)
        for app in apps:
            parent = getattr(app, "parent", None)
            owner = str(parent.value) if parent and hasattr(parent, "value") else None
            if owner and owner.upper() == customer_id:
                label_attr = getattr(app, "label", None)
                app_labels.append(str(label_attr.value) if label_attr and hasattr(label_attr, "value") else "?")
    except Exception:  # noqa: BLE001
        pass

    try:
        comps = await client.all(kind=KIND_APP_COMPONENT, branch=branch, prefetch_relationships=False)
        for comp in comps:
            name_attr = getattr(comp, "name", None)
            component_names.append(str(name_attr.value) if name_attr and hasattr(name_attr, "value") else "?")
    except Exception:  # noqa: BLE001
        pass

    return MCPResponse(
        status=MCPToolStatus.SUCCESS,
        data={
            "deployment_valid": len(portfolios) > 0 and len(app_labels) > 0,
            "customer_id": customer_id,
            "portfolios": portfolios,
            "applications": app_labels,
            "components": component_names,
            "summary": {
                "portfolio_count": len(portfolios),
                "application_count": len(app_labels),
                "component_count": len(component_names),
            },
        },
    )
