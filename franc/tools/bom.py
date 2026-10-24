"""Bill of Materials tools — physical/virtual device inventory for DC, Office, and Colocation.

Two tools:
  get_bill_of_materials  — full BOM for a given scope (datacenter / office / colocation / all)
  get_bom_diff           — differential BOM: what changed between two branches

Cables and SFPs:
  Cables carry a 'type' attribute: smf | mmf | copper | dac
  - smf / mmf cables require 2 SFPs per cable (one at each end)
  - copper / dac cables are direct-attach — no SFPs
  SFP types are inferred from the cable type.

Price lookup:
  No price attribute exists in Infrahub today — prices come from the built-in
  PRICE_TABLE dict below. Override or extend it as needed.
"""

from typing import TYPE_CHECKING, Annotated, Any

from fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from franc.constants import (
    KIND_CABLE,
    KIND_DEVICE,
    KIND_PHYS_CIRCUIT,
    KIND_VIRTUAL_DEVICE,
)
from franc.utils import (
    MCPResponse,
    MCPToolStatus,
    _gather_safe,
    _log_and_return_error,
    attr_val,
    require_client,
)

if TYPE_CHECKING:
    from infrahub_sdk import InfrahubClient

mcp: FastMCP = FastMCP(name="Infrahub BOM")

_PHYS_DEVICE_KIND = KIND_DEVICE
_VIRT_DEVICE_KIND = KIND_VIRTUAL_DEVICE

# Scope prefixes — matched against the device/cable name
_DC_PREFIXES = ("DC",)
_OFFICE_PREFIXES = ("OFC",)
_COLO_PREFIXES = ("FR", "AM", "PA", "NY", "VA", "MUC", "AMS", "FRA", "PAR", "STR", "EQX", "EXT")

# ---------------------------------------------------------------------------
# Price lookup table (USD list price — update as needed)
# ---------------------------------------------------------------------------

# Device types → unit price USD
DEVICE_PRICE: dict[str, float] = {
    "DCS-7050CX3-32C-R": 28_000,
    "DCS-7280CR3-32P4": 55_000,
    "DCS-7280CR3K-32P4": 60_000,
    "DCS-7060CX-32S": 18_000,
    "SRX-1500": 8_500,
    "SRX-4200": 22_000,
    "CloudGuard": 5_000,
    "FortiGate-1801F": 12_000,
    "PowerEdge-R640": 9_000,
    "PowerEdge-R740": 12_000,
    "PATCH-48": 400,
    "VCE-3000": 6_500,
}

# Cable types → price per cable (USD)
CABLE_PRICE: dict[str, float] = {
    "smf": 35,  # LC-LC single-mode patch, 3 m
    "mmf": 20,  # LC-LC multi-mode patch, 3 m
    "copper": 15,  # Cat6/Cat6A patch
    "dac": 80,  # DAC direct-attach copper (includes both ends)
}

# SFP types inferred from cable type — price per SFP (USD)
# copper/dac cables do NOT require SFPs
SFP_FROM_CABLE: dict[str, dict[str, Any]] = {
    "smf": {"sfp_type": "SFP-10G-LR", "unit_price": 150},
    "mmf": {"sfp_type": "SFP-10G-SR", "unit_price": 45},
}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _name_scope(name: str) -> str:
    up = (name or "").upper()
    if any(up.startswith(p) for p in _DC_PREFIXES):
        return "datacenter"
    if any(up.startswith(p) for p in _OFFICE_PREFIXES):
        return "office"
    if any(up.startswith(p) for p in _COLO_PREFIXES):
        return "colocation"
    return "other"


def _rel_label(node: Any, rel_name: str) -> str | None:
    rel = getattr(node, rel_name, None)
    if rel is None:
        return None
    label = getattr(rel, "display_label", None)
    if label:
        return str(label)
    hfid = getattr(rel, "hfid", None)
    if hfid and isinstance(hfid, list) and hfid:
        return str(hfid[0])
    return None


def _device_row(node: Any) -> dict[str, Any]:
    return {
        "name": attr_val(node, "name"),
        "role": attr_val(node, "role"),
        "platform": _rel_label(node, "platform"),
        "device_type": _rel_label(node, "device_type"),
        "status": attr_val(node, "status"),
    }


def _cable_row(node: Any) -> dict[str, Any]:
    dep_rel = getattr(node, "deployment", None)
    dep_label = getattr(dep_rel, "display_label", None) or ""
    return {
        "name": attr_val(node, "name"),
        "type": attr_val(node, "type"),
        "deployment": dep_label,
    }


def _circuit_row(node: Any) -> dict[str, Any]:
    return {
        "name": attr_val(node, "name"),
        "circuit_id": attr_val(node, "circuit_id"),
        "circuit_type": attr_val(node, "circuit_type"),
        "provider": attr_val(node, "provider"),
        "bandwidth_mbps": attr_val(node, "committed_rate"),
        "status": attr_val(node, "status"),
    }


def _filter_by_scope(
    items: list[dict[str, Any]],
    scope: str,
    scope_name: str | None,
    name_key: str = "name",
) -> list[dict[str, Any]]:
    out = []
    for item in items:
        # For cables: prefer 'deployment' field for scope classification
        dep = str(item.get("deployment") or "")
        name = str(item.get(name_key) or "")
        classify_on = dep if dep else name
        if scope != "all" and _name_scope(classify_on) != scope:
            continue
        if scope_name and scope_name.upper() not in (dep.upper() + name.upper()):
            continue
        out.append(item)
    return out


def _summarise_devices(devices: list[dict[str, Any]]) -> dict[str, Any]:
    by_role: dict[str, int] = {}
    by_status: dict[str, int] = {}
    by_platform: dict[str, int] = {}
    by_device_type: dict[str, int] = {}

    for d in devices:
        role = d.get("role") or "unknown"
        status = d.get("status") or "unknown"
        platform = d.get("platform") or "unknown"
        device_type = d.get("device_type") or "unknown"

        by_role[role] = by_role.get(role, 0) + 1
        by_status[status] = by_status.get(status, 0) + 1
        by_platform[platform] = by_platform.get(platform, 0) + 1
        by_device_type[device_type] = by_device_type.get(device_type, 0) + 1

    # Price breakdown per device type
    order_lines: list[dict[str, Any]] = []
    total_price = 0.0
    for dt, qty in sorted(by_device_type.items()):
        unit = DEVICE_PRICE.get(dt)
        line_total = unit * qty if unit is not None else None
        if line_total:
            total_price += line_total
        order_lines.append(
            {
                "device_type": dt,
                "qty": qty,
                "unit_price_usd": unit,
                "line_total_usd": line_total,
            }
        )

    return {
        "total": len(devices),
        "by_role": by_role,
        "by_status": by_status,
        "by_platform": by_platform,
        "by_device_type": by_device_type,
        "order_lines": order_lines,
        "total_price_usd": round(total_price, 2),
    }


def _summarise_cables_and_sfps(cables: list[dict[str, Any]]) -> dict[str, Any]:
    """
    Produce cable order lines and derive SFP requirements.

    Rules:
      - smf / mmf cables → 2 SFPs per cable (one at each end)
      - copper / dac cables → no SFPs (direct-attach)
    """
    by_type: dict[str, int] = {}
    for c in cables:
        t = c.get("type") or "unknown"
        by_type[t] = by_type.get(t, 0) + 1

    # Cable order lines
    cable_lines: list[dict[str, Any]] = []
    cable_total = 0.0
    for ctype, qty in sorted(by_type.items()):
        unit = CABLE_PRICE.get(ctype)
        line_total = unit * qty if unit is not None else None
        if line_total:
            cable_total += line_total
        cable_lines.append(
            {
                "cable_type": ctype,
                "qty": qty,
                "unit_price_usd": unit,
                "line_total_usd": line_total,
            }
        )

    # SFP requirements derived from optical cables
    sfp_by_type: dict[str, int] = {}
    for ctype, qty in by_type.items():
        sfp_info = SFP_FROM_CABLE.get(ctype)
        if sfp_info:
            sfp_t = sfp_info["sfp_type"]
            sfp_by_type[sfp_t] = sfp_by_type.get(sfp_t, 0) + qty * 2  # 2 per cable

    sfp_lines: list[dict[str, Any]] = []
    sfp_total = 0.0
    for sfp_t, qty in sorted(sfp_by_type.items()):
        # Find unit price from SFP_FROM_CABLE
        unit = next(
            (v["unit_price"] for v in SFP_FROM_CABLE.values() if v["sfp_type"] == sfp_t),
            None,
        )
        line_total = unit * qty if unit is not None else None
        if line_total:
            sfp_total += line_total
        sfp_lines.append(
            {
                "sfp_type": sfp_t,
                "qty": qty,
                "unit_price_usd": unit,
                "line_total_usd": line_total,
            }
        )

    return {
        "cables": {
            "total": len(cables),
            "by_type": by_type,
            "order_lines": cable_lines,
            "total_price_usd": round(cable_total, 2),
        },
        "sfps": {
            "total": sum(sfp_by_type.values()),
            "by_type": sfp_by_type,
            "order_lines": sfp_lines,
            "total_price_usd": round(sfp_total, 2),
            "note": "2 SFPs per optical cable (smf/mmf). DAC/copper cables excluded.",
        },
    }


def _summarise_circuits(circuits: list[dict[str, Any]]) -> dict[str, Any]:
    by_type: dict[str, int] = {}
    by_provider: dict[str, int] = {}
    by_status: dict[str, int] = {}

    for c in circuits:
        ct = c.get("circuit_type") or "unknown"
        prov = c.get("provider") or "unknown"
        st = c.get("status") or "unknown"
        by_type[ct] = by_type.get(ct, 0) + 1
        by_provider[prov] = by_provider.get(prov, 0) + 1
        by_status[st] = by_status.get(st, 0) + 1

    return {
        "total": len(circuits),
        "by_type": by_type,
        "by_provider": by_provider,
        "by_status": by_status,
    }


# ---------------------------------------------------------------------------
# BOM tool
# ---------------------------------------------------------------------------


@mcp.tool(
    tags={"bom", "inventory", "retrieve"},
    annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True),
)
async def get_bill_of_materials(
    ctx: Context,
    scope: Annotated[
        str,
        Field(
            default="all",
            description=(
                "Inventory scope: 'datacenter', 'office', 'colocation', or 'all'. "
                "Use 'datacenter' for DC fabric nodes, 'office' for branch sites, "
                "'colocation' for Equinix/colo edge devices."
            ),
        ),
    ] = "all",
    scope_name: Annotated[
        str | None,
        Field(
            default=None,
            description=(
                "Optional name filter within the scope — e.g. 'DC1', 'DC3', "
                "'OFC-LON-BRANCH-01', 'FR2'. Substring match on deployment name."
            ),
        ),
    ] = None,
    include_virtual: Annotated[
        bool,
        Field(default=True, description="Include virtual devices (vFW, vLB) in the BOM."),
    ] = True,
    include_cables: Annotated[
        bool,
        Field(
            default=True,
            description=(
                "Include cables and derived SFP requirements in the BOM. "
                "Optical cables (smf/mmf) require 2 SFPs each; DAC/copper do not."
            ),
        ),
    ] = True,
    include_circuits: Annotated[
        bool,
        Field(default=False, description="Include physical circuits (cross-connects, MPLS, dark-fiber)."),
    ] = False,
    branch: Annotated[
        str | None,
        Field(default=None, description="Branch to read from (defaults to main)."),
    ] = None,
) -> MCPResponse[dict[str, Any]]:
    """
    Produce a Bill of Materials (BOM) for a datacenter, office, colocation site, or all scopes.

    The BOM covers:
      - Physical devices: role, platform, device_type, status + order lines with unit price
      - Virtual devices (optional): role, platform, status + order lines with unit price
      - Cables + SFPs (optional): cable type breakdown, SFP requirements (2 per optical cable),
        order lines with unit prices and line totals
      - Physical circuits (optional): type, provider, bandwidth, status

    Price lookup uses a built-in table (PRICE_TABLE in bom.py) since Infrahub has no price field.
    Prices are indicative USD list prices — override the table for actual contract prices.

    Scope values:
      - 'datacenter' — DC fabric: border-leaves, spines, leafs, servers
      - 'office'     — Branch/WAN edge: VCE routers, access switches, APs
      - 'colocation' — Colo edge: patch panels, edge routers, VCG gateways
      - 'all'        — Everything in Infrahub

    Returns:
      {
        "scope", "scope_name", "branch",
        "physical_devices": {
          "total", "by_role", "by_status", "by_platform", "by_device_type",
          "order_lines": [{"device_type", "qty", "unit_price_usd", "line_total_usd"}],
          "total_price_usd",
          "items": [...]
        },
        "virtual_devices": { same structure },
        "cables": {
          "total", "by_type",
          "order_lines": [{"cable_type", "qty", "unit_price_usd", "line_total_usd"}],
          "total_price_usd"
        },
        "sfps": {
          "total", "by_type",
          "order_lines": [{"sfp_type", "qty", "unit_price_usd", "line_total_usd"}],
          "total_price_usd", "note"
        },
        "grand_total_usd": N
      }
    """
    try:
        client: InfrahubClient = require_client(ctx)
    except RuntimeError as exc:
        return await _log_and_return_error(ctx=ctx, error=str(exc), remediation="Start MCP with a configured client.")

    scope = (scope or "all").lower()
    branch = branch or "main"

    valid_scopes = {"datacenter", "office", "colocation", "all"}
    if scope not in valid_scopes:
        return await _log_and_return_error(
            ctx=ctx,
            error=f"Invalid scope '{scope}'. Must be one of: {', '.join(sorted(valid_scopes))}.",
            remediation="Use 'datacenter', 'office', 'colocation', or 'all'.",
        )

    await ctx.info(f"Building BOM: scope={scope} name={scope_name or '*'} branch={branch}")

    coros: list[Any] = [
        client.all(kind=_PHYS_DEVICE_KIND, branch=branch, prefetch_relationships=False),
    ]
    if include_virtual:
        coros.append(client.all(kind=_VIRT_DEVICE_KIND, branch=branch, prefetch_relationships=False))
    if include_cables:
        coros.append(client.all(kind=KIND_CABLE, branch=branch, prefetch_relationships=False))
    if include_circuits:
        coros.append(client.all(kind=KIND_PHYS_CIRCUIT, branch=branch, prefetch_relationships=False))

    results = await _gather_safe(*coros)

    idx = 0
    phys_nodes = results[idx] or []
    idx += 1
    virt_nodes = (results[idx] or []) if include_virtual else []
    idx += 1 if include_virtual else 0
    cable_nodes = (results[idx] or []) if include_cables else []
    idx += 1 if include_cables else 0
    circuit_nodes = (results[idx] or []) if include_circuits else []

    phys_rows = _filter_by_scope([_device_row(n) for n in phys_nodes], scope, scope_name)
    virt_rows = _filter_by_scope([_device_row(n) for n in virt_nodes], scope, scope_name)
    cable_rows = _filter_by_scope([_cable_row(n) for n in cable_nodes], scope, scope_name)
    circuit_rows = _filter_by_scope([_circuit_row(n) for n in circuit_nodes], "all", scope_name)

    phys_summary = _summarise_devices(phys_rows)
    virt_summary = _summarise_devices(virt_rows)
    cables_sfps = _summarise_cables_and_sfps(cable_rows) if include_cables else {}

    grand_total = (
        phys_summary["total_price_usd"]
        + virt_summary["total_price_usd"]
        + (cables_sfps.get("cables", {}).get("total_price_usd", 0))
        + (cables_sfps.get("sfps", {}).get("total_price_usd", 0))
    )

    data: dict[str, Any] = {
        "scope": scope,
        "scope_name": scope_name,
        "branch": branch,
        "physical_devices": {**phys_summary, "items": phys_rows},
    }

    if include_virtual:
        data["virtual_devices"] = {**virt_summary, "items": virt_rows}

    if include_cables:
        data["cables"] = cables_sfps["cables"]
        data["sfps"] = cables_sfps["sfps"]

    if include_circuits:
        data["circuits"] = {**_summarise_circuits(circuit_rows), "items": circuit_rows}

    data["grand_total_usd"] = round(grand_total, 2)

    await ctx.info(
        f"BOM complete — devices={len(phys_rows)} virtual={len(virt_rows)} "
        f"cables={len(cable_rows)} grand_total=${grand_total:,.0f}"
    )

    return MCPResponse(status=MCPToolStatus.SUCCESS, data=data)


# ---------------------------------------------------------------------------
# Differential BOM tool
# ---------------------------------------------------------------------------


@mcp.tool(
    tags={"bom", "diff", "retrieve"},
    annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True),
)
async def get_bom_diff(
    ctx: Context,
    branch: Annotated[
        str,
        Field(description="Feature/change branch to compare against the base."),
    ],
    base_branch: Annotated[
        str,
        Field(default="main", description="Reference branch (defaults to main)."),
    ] = "main",
    scope: Annotated[
        str,
        Field(default="all", description="Scope to diff: 'datacenter', 'office', 'colocation', or 'all'."),
    ] = "all",
    scope_name: Annotated[
        str | None,
        Field(default=None, description="Optional name filter (e.g. 'DC1', 'FR2', 'OFC-LON-BRANCH-01')."),
    ] = None,
    include_virtual: Annotated[
        bool,
        Field(default=True, description="Include virtual devices in the diff."),
    ] = True,
    include_cables: Annotated[
        bool,
        Field(default=True, description="Include cables and SFPs in the diff."),
    ] = True,
    include_circuits: Annotated[
        bool,
        Field(default=False, description="Include physical circuits in the diff."),
    ] = False,
) -> MCPResponse[dict[str, Any]]:
    """
    Differential Bill of Materials — show only what changed between two branches.

    Compares the BOM on `branch` against `base_branch` and returns three sections
    per object type (physical devices, virtual devices, cables, SFPs):

      - added:    items present on `branch` but not on `base_branch`
      - removed:  items present on `base_branch` but not on `branch`
      - changed:  items present on both branches but with different attributes

    Also computes the price delta so you know the cost impact of the change.

    Returns:
      {
        "branch", "base_branch", "scope", "scope_name",
        "physical_devices": {"added", "removed", "changed", "summary", "price_delta_usd"},
        "virtual_devices":  { same },
        "cables":           { same },
        "sfps":             { derived from cable diff },
        "total_changes": N,
        "grand_total_delta_usd": N,
      }
    """
    try:
        client: InfrahubClient = require_client(ctx)
    except RuntimeError as exc:
        return await _log_and_return_error(ctx=ctx, error=str(exc), remediation="Start MCP with a configured client.")

    scope = (scope or "all").lower()
    valid_scopes = {"datacenter", "office", "colocation", "all"}
    if scope not in valid_scopes:
        return await _log_and_return_error(
            ctx=ctx,
            error=f"Invalid scope '{scope}'. Must be one of: {', '.join(sorted(valid_scopes))}.",
            remediation="Use 'datacenter', 'office', 'colocation', or 'all'.",
        )

    await ctx.info(f"Diffing BOM: {base_branch} → {branch}  scope={scope} name={scope_name or '*'}")

    fetch_kinds: list[str] = [_PHYS_DEVICE_KIND]
    if include_virtual:
        fetch_kinds.append(_VIRT_DEVICE_KIND)
    if include_cables:
        fetch_kinds.append(KIND_CABLE)
    if include_circuits:
        fetch_kinds.append(KIND_PHYS_CIRCUIT)

    base_coros = [client.all(kind=k, branch=base_branch, prefetch_relationships=False) for k in fetch_kinds]
    head_coros = [client.all(kind=k, branch=branch, prefetch_relationships=False) for k in fetch_kinds]
    all_results = await _gather_safe(*base_coros, *head_coros)
    n = len(fetch_kinds)
    base_results, head_results = all_results[:n], all_results[n:]

    def _extract(res: list[Any], i: int, row_fn: Any) -> list[dict[str, Any]]:
        return [row_fn(nd) for nd in (res[i] or [])]

    def _diff(
        base: list[dict[str, Any]],
        head: list[dict[str, Any]],
        key: str = "name",
        compare_fields: list[str] | None = None,
    ) -> dict[str, Any]:
        if compare_fields is None:
            compare_fields = ["role", "platform", "device_type", "status"]
        base_map = {str(item.get(key) or ""): item for item in base}
        head_map = {str(item.get(key) or ""): item for item in head}
        base_keys, head_keys = set(base_map), set(head_map)
        added = [head_map[k] for k in sorted(head_keys - base_keys)]
        removed = [base_map[k] for k in sorted(base_keys - head_keys)]
        changed = []
        for k in sorted(base_keys & head_keys):
            b, h = base_map[k], head_map[k]
            diffs = {f: {"before": b.get(f), "after": h.get(f)} for f in compare_fields if b.get(f) != h.get(f)}
            if diffs:
                changed.append({key: k, "changes": diffs})
        return {
            "added": added,
            "removed": removed,
            "changed": changed,
            "summary": {
                "added": len(added),
                "removed": len(removed),
                "changed": len(changed),
                "unchanged": len(base_keys & head_keys) - len(changed),
            },
        }

    def _device_price_delta(diff_result: dict[str, Any]) -> float:
        delta = 0.0
        for item in diff_result["added"]:
            p = DEVICE_PRICE.get(item.get("device_type") or "", 0)
            delta += p
        for item in diff_result["removed"]:
            p = DEVICE_PRICE.get(item.get("device_type") or "", 0)
            delta -= p
        return delta

    def _cable_price_delta(diff_result: dict[str, Any]) -> float:
        delta = 0.0
        for item in diff_result["added"]:
            delta += CABLE_PRICE.get(item.get("type") or "", 0)
        for item in diff_result["removed"]:
            delta -= CABLE_PRICE.get(item.get("type") or "", 0)
        return delta

    def _sfp_delta_from_cable_diff(diff_result: dict[str, Any]) -> dict[str, Any]:
        """Derive SFP delta from cable added/removed lists."""
        sfp_added: dict[str, int] = {}
        sfp_removed: dict[str, int] = {}
        for item in diff_result["added"]:
            sfp_info = SFP_FROM_CABLE.get(item.get("type") or "")
            if sfp_info:
                t = sfp_info["sfp_type"]
                sfp_added[t] = sfp_added.get(t, 0) + 2
        for item in diff_result["removed"]:
            sfp_info = SFP_FROM_CABLE.get(item.get("type") or "")
            if sfp_info:
                t = sfp_info["sfp_type"]
                sfp_removed[t] = sfp_removed.get(t, 0) + 2

        sfp_price_delta = 0.0
        all_sfp_types = set(sfp_added) | set(sfp_removed)
        sfp_lines = []
        for t in sorted(all_sfp_types):
            unit = next((v["unit_price"] for v in SFP_FROM_CABLE.values() if v["sfp_type"] == t), None)
            added_qty = sfp_added.get(t, 0)
            removed_qty = sfp_removed.get(t, 0)
            line_delta = ((added_qty - removed_qty) * unit) if unit else None
            if line_delta:
                sfp_price_delta += line_delta
            sfp_lines.append(
                {
                    "sfp_type": t,
                    "added": added_qty,
                    "removed": removed_qty,
                    "net_qty": added_qty - removed_qty,
                    "unit_price_usd": unit,
                    "line_delta_usd": line_delta,
                }
            )
        return {
            "order_lines": sfp_lines,
            "price_delta_usd": round(sfp_price_delta, 2),
        }

    i = 0
    base_phys = _filter_by_scope(_extract(base_results, i, _device_row), scope, scope_name)
    head_phys = _filter_by_scope(_extract(head_results, i, _device_row), scope, scope_name)
    i += 1

    base_virt = _filter_by_scope(_extract(base_results, i, _device_row), scope, scope_name) if include_virtual else []
    head_virt = _filter_by_scope(_extract(head_results, i, _device_row), scope, scope_name) if include_virtual else []
    if include_virtual:
        i += 1

    base_cables = _filter_by_scope(_extract(base_results, i, _cable_row), scope, scope_name) if include_cables else []
    head_cables = _filter_by_scope(_extract(head_results, i, _cable_row), scope, scope_name) if include_cables else []
    if include_cables:
        i += 1

    base_circ = _filter_by_scope(_extract(base_results, i, _circuit_row), "all", scope_name) if include_circuits else []
    head_circ = _filter_by_scope(_extract(head_results, i, _circuit_row), "all", scope_name) if include_circuits else []

    phys_diff = _diff(base_phys, head_phys)
    virt_diff = _diff(base_virt, head_virt) if include_virtual else None
    cable_diff = _diff(base_cables, head_cables, compare_fields=["type"]) if include_cables else None
    circ_diff = (
        _diff(base_circ, head_circ, compare_fields=["circuit_type", "provider", "bandwidth_mbps", "status"])
        if include_circuits
        else None
    )

    phys_price_delta = _device_price_delta(phys_diff)
    virt_price_delta = _device_price_delta(virt_diff) if virt_diff else 0.0
    cable_price_delta = _cable_price_delta(cable_diff) if cable_diff else 0.0
    sfp_delta = _sfp_delta_from_cable_diff(cable_diff) if cable_diff else {"order_lines": [], "price_delta_usd": 0.0}

    phys_diff["price_delta_usd"] = round(phys_price_delta, 2)
    if virt_diff:
        virt_diff["price_delta_usd"] = round(virt_price_delta, 2)
    if cable_diff:
        cable_diff["price_delta_usd"] = round(cable_price_delta, 2)

    grand_delta = round(phys_price_delta + virt_price_delta + cable_price_delta + sfp_delta["price_delta_usd"], 2)

    total_changes = (
        phys_diff["summary"]["added"]
        + phys_diff["summary"]["removed"]
        + phys_diff["summary"]["changed"]
        + (
            (virt_diff["summary"]["added"] + virt_diff["summary"]["removed"] + virt_diff["summary"]["changed"])
            if virt_diff
            else 0
        )
        + (
            (cable_diff["summary"]["added"] + cable_diff["summary"]["removed"] + cable_diff["summary"]["changed"])
            if cable_diff
            else 0
        )
        + (
            (circ_diff["summary"]["added"] + circ_diff["summary"]["removed"] + circ_diff["summary"]["changed"])
            if circ_diff
            else 0
        )
    )

    await ctx.info(f"BOM diff complete — {total_changes} change(s), price delta ${grand_delta:+,.0f}")

    result: dict[str, Any] = {
        "branch": branch,
        "base_branch": base_branch,
        "scope": scope,
        "scope_name": scope_name,
        "total_changes": total_changes,
        "grand_total_delta_usd": grand_delta,
        "physical_devices": phys_diff,
    }
    if include_virtual:
        result["virtual_devices"] = virt_diff
    if include_cables:
        result["cables"] = cable_diff
        result["sfps"] = sfp_delta
    if include_circuits:
        result["circuits"] = circ_diff

    return MCPResponse(status=MCPToolStatus.SUCCESS, data=result)
