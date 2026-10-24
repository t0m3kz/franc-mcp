"""
SDK/GraphQL helpers for flow.py and flow_pair.py MCP tools.

All Cypher queries have been replaced with Infrahub SDK calls and GraphQL.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from infrahub_sdk import InfrahubClient

__all__ = [
    "_DEAD_STATUSES",
    "_HEALTHY_STATUSES",
    "_IFACE_STATUS_LABEL",
    "_PATH_INVENTORY_GQL",
    "_WARN_STATUSES",
    "_annotate_hop",
    "_build_iface_status_map",
    "_iface_health",
    "_path_summary",
    "_resolve_app_sdk",
    "_resolve_component_sdk",
    "_segment_servers_sdk",
]

_HEALTHY_STATUSES = {"active"}
_WARN_STATUSES = {"maintenance", "outage"}
_DEAD_STATUSES = {"disabled", "deleted"}


# ---------------------------------------------------------------------------
# SDK segment/component/app resolution helpers
# ---------------------------------------------------------------------------


async def _segment_servers_sdk(client: InfrahubClient, segment_name: str, branch: str) -> list[str]:
    """Return server device names attached to a ManagedVxlanSegment via interface_capabilities."""
    seg_nodes = await client.filters(kind="ManagedVxlanSegment", name__value=segment_name, branch=branch)
    if not seg_nodes:
        return []
    seg: Any = seg_nodes[0]
    await seg.interface_capabilities.fetch()
    servers: list[str] = []
    for ic in seg.interface_capabilities.peers:
        iface = ic.peer
        if iface:
            await iface.device.fetch()
            dev = iface.device.peer
            if dev and dev.name.value not in servers:
                servers.append(dev.name.value)
    return servers


async def _resolve_component_sdk(
    client: InfrahubClient,
    identifier: str,
    branch: str,
) -> dict[str, Any]:
    """Resolve AppComponent by name or slug → segments + servers."""
    comp_nodes = await client.filters(kind="AppComponent", name__value=identifier, branch=branch)
    if not comp_nodes:
        comp_nodes = await client.filters(kind="AppComponent", slug__value=identifier, branch=branch)
    if not comp_nodes:
        return {"error": f"Component '{identifier}' not found"}

    seen_slugs: dict[str, dict[str, Any]] = {}
    for comp_node in comp_nodes:
        comp: Any = comp_node
        slug = str(comp.slug.value if hasattr(comp.slug, "value") else comp.name.value)
        if slug in seen_slugs:
            continue
        await comp.network_segment.fetch()
        seg_names: list[str] = []
        seg_node = comp.network_segment.peer
        if seg_node:
            seg_names.append(seg_node.name.value)
        comp_type = comp.component_type.value if hasattr(comp, "component_type") else None
        seen_slugs[slug] = {
            "component": comp.name.value,
            "slug": slug,
            "type": comp_type,
            "segments": seg_names,
            "servers": [],
        }

    all_servers: list[str] = []
    for entry in seen_slugs.values():
        for seg_name in entry["segments"]:
            for srv in await _segment_servers_sdk(client, seg_name, branch):
                if srv not in all_servers:
                    all_servers.append(srv)
                if srv not in entry["servers"]:
                    entry["servers"].append(srv)

    slugs = list(seen_slugs.values())
    segments = sorted({s for e in slugs for s in e["segments"]})
    ambiguous = len(slugs) > 1

    return {
        "component": slugs[0]["component"],
        "slug": slugs[0]["slug"] if not ambiguous else identifier,
        "type": slugs[0]["type"],
        "segment": segments[0] if len(segments) == 1 else None,
        "segments": segments,
        "servers": all_servers,
        "ambiguous": ambiguous,
        "matches": slugs if ambiguous else None,
    }


async def _resolve_app_sdk(
    client: InfrahubClient,
    app_id: str,
    branch: str,
) -> tuple[str, list[dict[str, Any]]]:
    """Return (app_name, list of component dicts with servers and segment context)."""
    app_nodes = await client.filters(kind="AppApplication", name__value=app_id, branch=branch)
    if not app_nodes:
        app_nodes = await client.filters(kind="AppApplication", label__value=app_id, branch=branch)
    if not app_nodes:
        return app_id, []

    app: Any = app_nodes[0]
    app_name = app.name.value

    all_comps: list[Any] = await client.filters(kind="AppComponent", branch=branch)
    prefix = app_name + "-"
    filtered = [c for c in all_comps if str(c.slug.value if hasattr(c.slug, "value") else "").startswith(prefix)]

    result: list[dict[str, Any]] = []
    for comp_node in filtered:
        comp: Any = comp_node
        slug = str(comp.slug.value if hasattr(comp.slug, "value") else comp.name.value)
        await comp.network_segment.fetch()
        seg_name: str | None = None
        seg_peer = comp.network_segment.peer
        if seg_peer:
            seg_name = seg_peer.name.value

        servers = await _segment_servers_sdk(client, seg_name, branch) if seg_name else []

        seg_ctx: dict[str, Any] | None = None
        if seg_name:
            seg_nodes = await client.filters(kind="ManagedVxlanSegment", name__value=seg_name, branch=branch)
            if seg_nodes:
                seg: Any = seg_nodes[0]
                fw_name: str | None = None
                if hasattr(seg, "inline_service"):
                    try:
                        await seg.inline_service.fetch()
                        fw_name = seg.inline_service.peer.name.value if seg.inline_service.peer else None
                    except Exception:  # noqa: BLE001
                        pass
                seg_ctx = {
                    "name": seg_name,
                    "pbr_enabled": getattr(seg.pbr_enabled, "value", None),
                    "firewall": fw_name,
                    "servers": servers,
                }

        comp_type = comp.component_type.value if hasattr(comp, "component_type") else ""
        result.append(
            {
                "slug": slug,
                "name": comp.name.value,
                "type": comp_type,
                "segment": seg_name or "",
                "servers": servers,
                "seg_ctx": seg_ctx,
            }
        )

    return app_name, result


# ---------------------------------------------------------------------------
# Interface status via GraphQL
# ---------------------------------------------------------------------------

_PATH_INVENTORY_GQL = """
query PathMonitoringInventory($device_names: [String!]!) {
  DcimDevice(name__values: $device_names) {
    edges {
      node {
        id
        name { value }
        role { value }
        interfaces {
          edges {
            node {
              __typename
              id
              name { value }
              status { value }
              role { value }
              description { value }
              ... on DcimPhysicalInterface {
                enabled { value }
                interface_type { value }
                ip_address {
                  node { address { value } }
                }
              }
              ... on DcimVirtualInterface {
                ip_address {
                  node { address { value } }
                }
              }
              ... on DcimLAGInterface {
                lag_id { value }
                member_interfaces {
                  edges { node { name { value } } }
                }
              }
            }
          }
        }
      }
    }
  }
}
"""

_IFACE_STATUS_LABEL = {
    "active": "ok",
    "maintenance": "warning",
    "outage": "warning",
    "disabled": "broken",
    "deleted": "broken",
}


async def _fetch_iface_status_map(
    client: InfrahubClient,
    device_names: list[str],
    branch: str,
) -> dict[tuple[str, str], str]:
    """Fetch interface statuses for the given devices via GraphQL."""
    if not device_names:
        return {}
    result = await client.execute_graphql(
        query=_PATH_INVENTORY_GQL,
        branch_name=branch,
        variables={"device_names": sorted(device_names)},
    )
    rows: list[dict[str, Any]] = []
    for dev_edge in result.get("DcimDevice", {}).get("edges", []):
        dev_node = dev_edge["node"]
        dev_name = dev_node.get("name", {}).get("value", "")
        for iface_edge in dev_node.get("interfaces", {}).get("edges", []):
            iface = iface_edge["node"]
            iface_name = iface.get("name", {}).get("value", "")
            status = (iface.get("status") or {}).get("value", "unknown")
            rows.append({"device": dev_name, "interface": iface_name, "status": status})
    return _build_iface_status_map(rows)


# ---------------------------------------------------------------------------
# Path status annotation helpers
# ---------------------------------------------------------------------------


def _build_iface_status_map(rows: list[dict[str, Any]]) -> dict[tuple[str, str], str]:
    return {(r["device"], r["interface"]): r["status"] for r in rows if r.get("device") and r.get("interface")}


def _iface_health(status: str) -> str:
    if status in _HEALTHY_STATUSES:
        return "ok"
    if status in _WARN_STATUSES:
        return "warning"
    if status in _DEAD_STATUSES:
        return "broken"
    return "unknown"


def _annotate_hop(hop: dict[str, Any], status_map: dict[tuple[str, str], str]) -> dict[str, Any]:
    src_status = status_map.get((hop.get("src_device", ""), hop.get("src_iface", "")), "unknown")
    dst_status = status_map.get((hop.get("dst_device", ""), hop.get("dst_iface", "")), "unknown")
    src_health = _iface_health(src_status)
    dst_health = _iface_health(dst_status)
    return {
        **hop,
        "src_iface_status": src_status,
        "dst_iface_status": dst_status,
        "src_health": src_health,
        "dst_health": dst_health,
        "hop_broken": src_health == "broken" or dst_health == "broken",
        "hop_warning": src_health == "warning" or dst_health == "warning",
    }


def _path_summary(annotated_hops: list[dict[str, Any]]) -> dict[str, Any]:
    broken = [h for h in annotated_hops if h["hop_broken"]]
    warning = [h for h in annotated_hops if h["hop_warning"] and not h["hop_broken"]]
    return {
        "total_hops": len(annotated_hops),
        "healthy_hops": len(annotated_hops) - len(broken) - len(warning),
        "broken_hops": len(broken),
        "warning_hops": len(warning),
        "path_status": "broken" if broken else ("warning" if warning else "ok"),
        "broken_at": [
            f"{h['src_device']}/{h['src_iface']}({h['src_iface_status']}) -> "
            f"{h['dst_device']}/{h['dst_iface']}({h['dst_iface_status']})"
            for h in broken
        ],
        "warnings_at": [
            f"{h['src_device']}/{h['src_iface']}({h['src_iface_status']}) -> "
            f"{h['dst_device']}/{h['dst_iface']}({h['dst_iface_status']})"
            for h in warning
        ],
    }
