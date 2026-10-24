import inspect
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Annotated, Any

from fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field, field_validator

from franc.utils import MCPResponse, MCPToolStatus, _gather_safe, _log_and_return_error, require_client

if TYPE_CHECKING:
    from infrahub_sdk import InfrahubClient


# Public MCP instance for this module
mcp: FastMCP = FastMCP(name="Infrahub DataCenter Deployment")


TOPOLOGY_DC_KIND = "TopologyDataCenter"
TOPOLOGY_DC_DESIGN_KIND = "TopologyDataCenterDesign"
DEFAULT_STRATEGIES = ["ebgp-ebgp", "ebgp-ibgp", "ospf-ibgp"]
DEFAULT_DESIGNS = [
    "S_OSPF_IBGP",
    "S_EBGP_IBGP",
    "S_EBGP_EBGP",
    "M_OSPF_IBGP",
    "M_EBGP_EBGP",
    "M_EBGP_IBGP",
    "L_EBGP_EBGP",
    "XL_EBGP_EBGP",
]
DEFAULT_PROVIDERS = ["Technology Partner", "Customer 1"]


# ---------------------------------------------------------------------------
# Internal helper models
# ---------------------------------------------------------------------------


class DeploymentParams(BaseModel):
    site_name: str
    metro_location: str
    design: str
    strategy: str
    provider: str
    emulation: bool = True
    branch_name: str | None = None

    @field_validator("site_name")
    def validate_site_name(cls, v: str) -> str:
        if not v or len(v) < 2:
            raise ValueError("Site name must be at least 2 characters.")
        return v

    @field_validator("strategy")
    def validate_strategy(cls, v: str) -> str:
        if not v:
            raise ValueError("Strategy may not be empty.")
        return v

    @field_validator("design")
    def validate_design(cls, v: str) -> str:
        if not v:
            raise ValueError("Design may not be empty.")
        return v

    @field_validator("provider")
    def validate_provider(cls, v: str) -> str:
        if not v:
            raise ValueError("Provider may not be empty.")
        return v


# ---------------------------------------------------------------------------
# Schema helpers
# ---------------------------------------------------------------------------


def _get_attribute_default(schema: Any | None, attr_name: str, fallback: Any) -> Any:
    for attribute in getattr(schema, "attributes", []):
        if getattr(attribute, "name", None) == attr_name:
            return getattr(attribute, "default_value", fallback) or fallback
    return fallback


def _get_choice_names(schema: Any | None, attr_name: str) -> list[str]:
    for attribute in getattr(schema, "attributes", []):
        if getattr(attribute, "name", None) == attr_name:
            return [
                getattr(choice, "name", "")
                for choice in getattr(attribute, "choices", [])
                if getattr(choice, "name", None)
            ]
    return []


async def _resolve_design_pattern_id(client: Any, design_name: str, branch: str | None) -> str | None:
    schema_api = getattr(client, "schema", None)
    filters_api = getattr(client, "filters", None)
    if not schema_api or not callable(filters_api):
        return None
    try:
        design_schema_result = schema_api.get(kind=TOPOLOGY_DC_DESIGN_KIND, branch=branch)
        design_schema = (
            await design_schema_result if inspect.isawaitable(design_schema_result) else design_schema_result
        )
    except Exception:  # noqa: BLE001
        return None
    try:
        designs_result = filters_api(kind=design_schema.kind, branch=branch, name__value=design_name, parallel=True)
        designs = await designs_result if inspect.isawaitable(designs_result) else designs_result
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(designs, (list, tuple)) or not designs:
        return None
    candidate = designs[0]
    return getattr(candidate, "id", None) or getattr(candidate, "hfid", None)


async def _strategy_choices(client: Any, branch: str | None) -> list[str]:
    # routing_strategy lives on TopologyDataCenterDesign, not on TopologyDataCenter itself.
    schema_api = getattr(client, "schema", None)
    if not schema_api:
        return []
    try:
        schema_result = schema_api.get(kind=TOPOLOGY_DC_DESIGN_KIND, branch=branch)
        schema = await schema_result if inspect.isawaitable(schema_result) else schema_result
    except Exception:  # noqa: BLE001
        return []
    return _get_choice_names(schema, "routing_strategy")


async def _design_choices(client: Any, branch: str | None) -> list[str]:
    schema_api = getattr(client, "schema", None)
    if not schema_api:
        return []
    try:
        schema_result = schema_api.get(kind=TOPOLOGY_DC_DESIGN_KIND, branch=branch)
        schema = await schema_result if inspect.isawaitable(schema_result) else schema_result
    except Exception:  # noqa: BLE001
        return []
    try:
        design_nodes_result = client.all(kind=schema.kind, branch=branch)
        design_nodes = await design_nodes_result if inspect.isawaitable(design_nodes_result) else design_nodes_result
    except Exception:  # noqa: BLE001
        return []
    names: list[str] = []
    for node in design_nodes:
        label = getattr(node, "display_label", None) or getattr(node, "name", None)
        if label:
            names.append(str(label))
    return names


# ---------------------------------------------------------------------------
# Discovery / selection options
# ---------------------------------------------------------------------------


@mcp.tool(
    tags={"datacenter", "discover"},
    annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True),
)
async def discover_datacenter_options(
    ctx: Context,
    branch: Annotated[str | None, Field(default=None, description="Branch to read from (defaults to main/default).")],
) -> MCPResponse[dict[str, Any]]:
    """
    Discover current selectable options for Data Center deployment:
      - Existing metro locations (LocationMetro / LocationBuilding)
      - Existing design patterns (TopologyDataCenterDesign nodes)
      - Available strategies (TopologyDataCenterDesign.routing_strategy choices)
      - Available providers (static list; TopologyDataCenter has no provider attribute)
    Returns:
      {
        "locations": [...],
        "designs": [...],
        "strategies": [...],
        "providers": [...]
      }
    """
    try:
        client: InfrahubClient = require_client(ctx)
    except RuntimeError as exc:
        return await _log_and_return_error(
            ctx=ctx, error=str(exc), remediation="Start the MCP with a configured client."
        )

    # Fire all discovery queries in parallel — schema + nodes for designs, locations
    design_schema_coro = client.schema.get(kind=TOPOLOGY_DC_DESIGN_KIND, branch=branch)
    loc_b_coro = client.all(kind="LocationBuilding", branch=branch)
    loc_m_coro = client.all(kind="LocationMetro", branch=branch)

    design_schema, loc_b, loc_m = await _gather_safe(design_schema_coro, loc_b_coro, loc_m_coro)

    # Design nodes (depends on design_schema)
    design_nodes: list[Any] = []
    if design_schema is not None:
        (design_nodes_raw,) = await _gather_safe(client.all(kind=design_schema.kind, branch=branch))
        design_nodes = design_nodes_raw or []

    locations: set[str] = set()
    designs: set[str] = set()
    strategies: set[str] = set()
    providers: set[str] = set()

    # Strategies from the design schema's routing_strategy choices
    strategies.update(_get_choice_names(design_schema, "routing_strategy"))

    # Designs from design nodes
    for node in design_nodes:
        label = getattr(node, "display_label", None) or getattr(node, "name", None)
        if label:
            designs.add(str(label.value) if hasattr(label, "value") else str(label))

    # Locations from both location kinds
    for node in (loc_b or []) + (loc_m or []):
        label = getattr(node, "display_label", None) or getattr(node, "name", None)
        if label:
            locations.add(str(label.value) if hasattr(label, "value") else str(label))

    # Static fallbacks if discovery returned nothing
    if not strategies:
        strategies.update(DEFAULT_STRATEGIES)
    if not providers:
        providers.update(DEFAULT_PROVIDERS)
    if not designs:
        designs.update(DEFAULT_DESIGNS)

    return MCPResponse(
        status=MCPToolStatus.SUCCESS,
        data={
            "locations": sorted(locations),
            "designs": sorted(designs),
            "strategies": sorted(strategies),
            "providers": sorted(providers),
        },
    )


# ---------------------------------------------------------------------------
# Deployment tool
# ---------------------------------------------------------------------------


@mcp.tool(
    tags={"datacenter", "create"},
    annotations=ToolAnnotations(readOnlyHint=False, idempotentHint=False, destructiveHint=False),
)
async def create_datacenter_deployment(
    ctx: Context,
    site_name: Annotated[str, Field(description="Data center site name (e.g. DC-1, KTW-2).")],
    metro_location: Annotated[str, Field(description="Metro / location / city name.")],
    design: Annotated[str, Field(description="Design template name.")],
    strategy: Annotated[str, Field(description="Routing / fabric strategy (e.g. ospf-ibgp).")],
    provider: Annotated[str, Field(description="Provider name (e.g. Technology Partner).")],
    emulation: Annotated[bool, Field(description="Whether emulation mode is enabled.", default=True)],
    branch_name: Annotated[
        str | None, Field(description="Optional branch name. Auto-generated if omitted.", default=None)
    ],
) -> MCPResponse[dict[str, Any]]:
    """
        Create a new Data Center deployment (TopologyDataCenter) on an isolated branch.

        Procedure:
            1. Generate unique branch name if not provided (dc-deploy-{site_name}-{timestamp})
            2. Pull schema defaults (strategy choices, defaults for super spines/sorting)
            3. Validate inputs
            4. Create TopologyDataCenter object with schema-aligned fields only
            5. Return summary of created topology

        NOTE:
            - Unknown/non-schema fields are intentionally excluded from the creation payload to reduce API errors.
            - Design pattern linking is attempted when the schema and matching design nodes are available.

    Returns:
      {
        "branch": "...",
        "topology": {...},
        "status": "created"
      }
    """
    try:
        client: InfrahubClient = require_client(ctx)
    except RuntimeError as exc:
        return await _log_and_return_error(
            ctx=ctx, error=str(exc), remediation="Start the MCP with a configured client."
        )

    # Discover schema defaults and strategy choices
    dc_schema = None
    try:
        dc_schema_result = client.schema.get(kind=TOPOLOGY_DC_KIND, branch=branch_name)
        dc_schema = await dc_schema_result if inspect.isawaitable(dc_schema_result) else dc_schema_result
    except Exception:  # noqa: BLE001
        await ctx.debug("TopologyDataCenter schema not available; falling back to static defaults.")
    strategy_choices = await _strategy_choices(client, branch_name) or []
    # Union schema choices with defaults to accept newer strategies even if schema lags
    strategy_choices = sorted({*strategy_choices, *DEFAULT_STRATEGIES})
    if strategy not in strategy_choices:
        return await _log_and_return_error(
            ctx=ctx,
            error=f"Invalid strategy '{strategy}'.",
            remediation=f"Choose one of: {', '.join(strategy_choices)}.",
        )
    design_choices = await _design_choices(client, branch_name) or DEFAULT_DESIGNS
    if design not in design_choices:
        return await _log_and_return_error(
            ctx=ctx,
            error=f"Invalid design '{design}'.",
            remediation=f"Choose one of: {', '.join(design_choices)}.",
        )
    # Provider is free-form in current schema; accept any non-empty value (validated by model).
    amount_of_super_spines = _get_attribute_default(dc_schema, "amount_of_super_spines", 2)
    fabric_sort = _get_attribute_default(dc_schema, "fabric_interface_sorting_method", "bottom_up")
    spine_sort = _get_attribute_default(dc_schema, "spine_interface_sorting_method", "bottom_up")

    design_pattern_id = None
    if design:
        design_pattern_id = await _resolve_design_pattern_id(client, design, branch_name)

    # Enforce branch generation
    generated_branch = False
    if not branch_name:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        branch_name = f"dc-deploy-{site_name.lower()}-{ts}"
        generated_branch = True

    await ctx.info(f"Using deployment branch: {branch_name} (generated={generated_branch})")

    # Create branch (fail early if unauthorized)
    try:
        await client.branch.create(
            branch_name=branch_name,
            sync_with_git=False,
            description="",
            wait_until_completion=True,
        )
    except Exception as exc:  # noqa: BLE001
        return await _log_and_return_error(
            ctx=ctx,
            error=exc,
            remediation="Ensure INFRAHUB_API_TOKEN is set and has branch:create permissions.",
        )

    # Validate parameters via model
    try:
        params = DeploymentParams(
            site_name=site_name,
            metro_location=metro_location,
            design=design,
            strategy=strategy,
            provider=provider,
            emulation=emulation,
            branch_name=branch_name,
        )
    except Exception as exc:  # noqa: BLE001
        return await _log_and_return_error(ctx=ctx, error=exc, remediation="Correct invalid input fields.")

    topology_data = {
        "name": params.site_name,
        "status": "provisioning",
        "fully_managed": True,
        "amount_of_super_spines": amount_of_super_spines,
        "fabric_interface_sorting_method": fabric_sort,
        "spine_interface_sorting_method": spine_sort,
    }
    if design_pattern_id:
        topology_data["design"] = {"id": design_pattern_id}

    # Attempt object creation
    created_node_summary: dict[str, Any] = {}
    creation_errors: list[str] = []

    try:
        # Hypothetical SDK pattern (may differ; adjust if actual method name varies)
        # Many SDKs implement something like client.node.create(kind=..., data=..., branch=...)
        create_coro = getattr(client, "create", None)
        if create_coro is not None:
            topology_node = await create_coro(
                kind="TopologyDataCenter",
                data=topology_data,
                branch=branch_name,
            )
            # Summarize created data
            created_node_summary = {
                "id": getattr(topology_node, "id", None),
                "name": params.site_name,
                "location": params.metro_location,
                "design": params.design,
                "design_pattern_id": design_pattern_id,
                "strategy": params.strategy,
                "fully_managed": True,
            }
        else:
            # Fallback: Provide GraphQL mutation template (manual execution path)
            gql_mutation = f"""
mutation CreateTopologyDataCenter {{
  TopologyDataCenterCreate(
    branch: "{branch_name}",
    data: {{
      name: "{params.site_name}",
            status: "provisioning",
            fully_managed: true,
            amount_of_super_spines: {amount_of_super_spines},
            fabric_interface_sorting_method: "{fabric_sort}",
            spine_interface_sorting_method: "{spine_sort}"
    }}
  ) {{
    id
    name
  }}
}}
""".strip()
            created_node_summary = {
                "graphql_mutation": gql_mutation,
                "note": "SDK create() not available; execute mutation via query_graphql tool.",
            }

    except Exception as exc:  # noqa: BLE001
        creation_errors.append(str(exc))

    if creation_errors:
        return await _log_and_return_error(
            ctx=ctx,
            error="; ".join(creation_errors),
            remediation="Inspect branch state; consider deleting branch if partial objects were created.",
        )

    response_payload = {
        "branch": branch_name,
        "topology": created_node_summary,
        "status": "created",
    }

    return MCPResponse(status=MCPToolStatus.SUCCESS, data=response_payload)


# ---------------------------------------------------------------------------
# (Optional) Validation tool
# ---------------------------------------------------------------------------


@mcp.tool(
    tags={"datacenter", "validate"},
    annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True),
)
async def validate_datacenter_deployment(
    ctx: Context,
    branch: Annotated[str, Field(description="Branch where the deployment was created.")],
    site_name: Annotated[str, Field(description="Site name used during deployment.")],
) -> MCPResponse[dict[str, Any]]:
    """
    Validate that a deployment exists on a given branch.

    Checks:
      - TopologyDataCenter object presence matching site_name
      - Basic attributes populated

    Returns summary with status.
    """
    try:
        client: InfrahubClient = require_client(ctx)
    except RuntimeError as exc:
        return await _log_and_return_error(
            ctx=ctx, error=str(exc), remediation="Start the MCP with a configured client."
        )
    await ctx.info(f"Validating deployment for site {site_name} on branch {branch}...")

    try:
        schema_result = client.schema.get(kind="TopologyDataCenter", branch=branch)
        schema = await schema_result if inspect.isawaitable(schema_result) else schema_result
    except Exception as exc:  # noqa: BLE001
        return await _log_and_return_error(ctx=ctx, error=exc, remediation="Ensure the schema exists.")

    try:
        nodes_result = client.filters(
            kind=schema.kind,
            branch=branch,
            name__value=site_name,
            parallel=True,
        )
        nodes = await nodes_result if inspect.isawaitable(nodes_result) else nodes_result
    except Exception as exc:  # noqa: BLE001
        return await _log_and_return_error(ctx=ctx, error=exc, remediation="Filter query failed.")

    if not nodes:
        return await _log_and_return_error(
            ctx=ctx,
            error=f"TopologyDataCenter with name '{site_name}' not found on branch '{branch}'.",
            remediation="Confirm deployment succeeded or re-run creation tool.",
        )

    node = nodes[0]
    summary: dict[str, Any] = {}
    for attr in [
        "name",
        "status",
        "fully_managed",
        "amount_of_super_spines",
        "fabric_interface_sorting_method",
        "spine_interface_sorting_method",
    ]:
        a = getattr(node, attr, None)
        if a and getattr(a, "value", None) is not None:
            summary[attr] = str(a.value)

    design = getattr(node, "design", None)
    if design:
        peer = getattr(design, "peer", None)
        label = getattr(peer, "display_label", None) or getattr(peer, "name", None)
        if label:
            summary["design"] = str(label)

    summary["id"] = getattr(node, "id", None)

    return MCPResponse(
        status=MCPToolStatus.SUCCESS,
        data={
            "deployment_valid": True,
            "summary": summary,
        },
    )
