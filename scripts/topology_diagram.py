#!/usr/bin/env python3
"""Generate a topology diagram for a datacenter, cluster, or path between two devices.

Usage:
    uv run scripts/topology_diagram.py <name> [--branch BRANCH] [--drawio] [--cluster] [--servers] [--out PATH]
    uv run scripts/topology_diagram.py --path <source> <target> [--branch BRANCH] [--out PATH]

Examples:
    uv run scripts/topology_diagram.py DC2 --branch test --drawio
    uv run scripts/topology_diagram.py DC3 --branch test --cluster
    uv run scripts/topology_diagram.py DC3-K8S-PROD --branch test --cluster
    uv run scripts/topology_diagram.py --path DC1-BL1 DC1-POD1-SRV-01 --branch test
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, ".")

from scripts._common import call_tool, print_error_and_exit


async def main(
    name: str | None,
    branch: str,
    use_drawio: bool,
    use_cluster: bool,
    include_servers: bool,
    max_role: str | None,
    out_path: str | None,
    path_source: str | None,
    path_target: str | None,
) -> None:
    # --- Path diagram mode ---
    if path_source and path_target:
        print(f"Finding paths: {path_source} → {path_target} (branch={branch})...")
        result = await call_tool("generate_path_diagram", source=path_source, target=path_target, branch=branch)
        if result.data is None:
            print_error_and_exit(result)
        data = result.data
        print(
            f"  paths={data['paths']}  hops={data['path_length']}  devices={data['devices']}  cables={data['cables']}"
        )
        slug = f"{path_source.lower()}_to_{path_target.lower()}".replace("-", "_")
        save_path = Path(out_path) if out_path else Path.cwd() / f"{slug}_path.drawio"
        save_path.write_text(data["drawio"], encoding="utf-8")
        print(f"Saved: {save_path}")
        return

    # Every mode below requires a positional name — enforced by argparse at the bottom
    # of this file (parser.error() if neither --path nor name is given).
    assert name is not None, "name is required when --path is not given"

    # --- Cluster diagram mode ---
    if use_cluster:
        is_specific = any(
            name.startswith(p)
            for p in ("DC3-K8S", "DC3-VSPHERE", "DC3-NUTANIX", "DC1-POD", "EQUINIX", "C001-EKS", "C001-AKS")
        )
        print(f"Fetching cluster topology for {'cluster' if is_specific else 'datacenter'}={name} (branch={branch})...")
        result = await call_tool(
            "generate_cluster_diagram",
            cluster_name=name if is_specific else None,
            datacenter=None if is_specific else name,
            platform=None,
            branch=branch,
        )
        if result.data is None:
            print_error_and_exit(result)
        data = result.data
        print(f"  clusters={data['clusters']}  node_pools={data['node_pools']}")
        save_path = Path(out_path) if out_path else Path.cwd() / f"{name.lower()}_clusters.drawio"
        save_path.write_text(data["drawio"], encoding="utf-8")
        print(f"Saved: {save_path}")
        return

    # --- DC topology diagram mode ---
    fmt = "drawio" if use_drawio else "mermaid"
    print(f"Fetching topology for {name} (branch={branch}, format={fmt})...")
    result = await call_tool(
        "generate_topology_diagram",
        datacenter=name,
        pod=None,
        include_servers=include_servers,
        max_role=max_role,
        format=fmt,
        branch=branch,
    )
    if result.data is None:
        print_error_and_exit(result)

    data = result.data
    print(f"  devices={data['devices']}  cables={data['cables']}")

    if use_drawio:
        save_path = Path(out_path) if out_path else Path.cwd() / f"{name.lower()}_topology.drawio"
        save_path.write_text(data["drawio"], encoding="utf-8")
        print(f"Saved: {save_path}")
    else:
        print()
        print(data["mermaid"])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate topology, cluster, or path diagram.")
    parser.add_argument("name", nargs="?", help="Datacenter (DC1/DC2/DC3) or cluster name")
    parser.add_argument(
        "--path", nargs=2, metavar=("SOURCE", "TARGET"), help="Generate path diagram between two devices"
    )
    parser.add_argument("--branch", default="main", help="Infrahub branch (default: main)")
    parser.add_argument("--drawio", action="store_true", help="Output Draw.io XML (default: Mermaid text)")
    parser.add_argument("--cluster", action="store_true", help="Generate cluster diagram (VirtCluster node pools)")
    parser.add_argument("--servers", action="store_true", help="Include server/compute nodes (DC diagrams only)")
    parser.add_argument(
        "--max-role",
        default=None,
        metavar="ROLE",
        help="Only include devices up to this role layer (e.g. leaf, spine, border-leaf)",
    )
    parser.add_argument("--out", default=None, help="Output file path")
    args = parser.parse_args()

    if args.path and not args.name:
        src, tgt = args.path
    elif args.path and args.name:
        parser.error("Use either --path SOURCE TARGET or a positional name, not both.")
        src = tgt = None
    else:
        src = tgt = None

    if not args.path and not args.name:
        parser.error("Provide either a datacenter/cluster name or --path SOURCE TARGET.")

    asyncio.run(
        main(args.name, args.branch, args.drawio, args.cluster, args.servers, args.max_role, args.out, src, tgt)
    )
