#!/usr/bin/env python3
"""Cloud infrastructure queries — summary, instances, clusters, connectivity.

Usage:
    uv run scripts/cloud.py summary [--branch BRANCH]
    uv run scripts/cloud.py instances [--account ACCOUNT] [--region REGION] [--branch BRANCH]
    uv run scripts/cloud.py clusters [--account ACCOUNT] [--branch BRANCH]
    uv run scripts/cloud.py connectivity [--account ACCOUNT] [--branch BRANCH]

Examples:
    uv run scripts/cloud.py summary --branch test
    uv run scripts/cloud.py instances --account C001-AWS --branch test
    uv run scripts/cloud.py clusters --branch test
    uv run scripts/cloud.py connectivity --account C001-AWS
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

sys.path.insert(0, ".")

from scripts._common import call_tool, print_error_and_exit


def _print_result(result, label: str) -> None:
    if result.data is None:
        print_error_and_exit(result)
    print(f"=== {label} ===")
    print(json.dumps(result.data, indent=2))


async def main(args: argparse.Namespace) -> None:
    if args.command == "summary":
        result = await call_tool("get_cloud_summary", branch=args.branch)
        _print_result(result, f"Cloud Summary (branch={args.branch})")

    elif args.command == "instances":
        result = await call_tool("get_cloud_instances", account=args.account, region=args.region, branch=args.branch)
        if result.data is None:
            print_error_and_exit(result)
        data = result.data
        print(f"=== Cloud Instances (branch={args.branch}) — {data['count']} total ===")
        for i in data["instances"]:
            print(
                f"  {i['name']:<40} account={i['account'] or '—':<20} zone={i['zone'] or '—':<20} "
                f"type={i['instance_type'] or '—':<15} status={i['status'] or '—'}"
            )

    elif args.command == "clusters":
        result = await call_tool("get_cloud_clusters", account=args.account, branch=args.branch)
        if result.data is None:
            print_error_and_exit(result)
        data = result.data
        print(f"=== Cloud Clusters (branch={args.branch}) — {data['count']} total ===")
        for c in data["clusters"]:
            pools = c.get("node_pools") or []
            print(
                f"  {c['name']:<40} platform={c['platform'] or '—':<15} version={c['version'] or '—':<10} "
                f"status={c['status'] or '—':<10} pools={len(pools)}"
            )
            for p in pools:
                sizing = (
                    f"{p.get('min_size', '?')}–{p.get('max_size', '?')}"
                    if p.get("scaling_mode") == "autoscaling"
                    else str(p.get("desired") or "?")
                )
                print(
                    f"      {p['name']:<35} type={p['pool_type'] or '—':<15} flavor={p['instance_flavor'] or '—':<20} nodes={sizing}"
                )

    elif args.command == "connectivity":
        result = await call_tool("get_cloud_connectivity", account=args.account, branch=args.branch)
        if result.data is None:
            print_error_and_exit(result)
        data = result.data
        print(f"=== Cloud Connectivity (branch={args.branch}) ===")
        print(f"  Direct Connects : {len(data['direct_connects'])}")
        for dx in data["direct_connects"]:
            print(
                f"    {dx['name']:<40} account={dx['account'] or '—':<20} region={dx['region'] or '—':<20} bw={dx['bandwidth'] or '—'}"
            )
        print(f"  Express Routes  : {len(data['express_routes'])}")
        for er in data["express_routes"]:
            print(
                f"    {er['name']:<40} account={er['account'] or '—':<20} region={er['region'] or '—':<20} bw={er['bandwidth'] or '—'}"
            )
        print(f"  Virtual Interfaces: {len(data['virtual_interfaces'])}")
        for vif in data["virtual_interfaces"]:
            print(f"    {vif['name']:<40} type={vif['vif_type'] or '—':<15} vlan={vif['vlan_id'] or '—'}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Cloud infrastructure queries.")
    parser.add_argument("command", choices=["summary", "instances", "clusters", "connectivity"])
    parser.add_argument("--branch", default="main", help="Infrahub branch (default: main)")
    parser.add_argument("--account", default=None, help="Filter by cloud account name")
    parser.add_argument("--region", default=None, help="Filter instances by region/zone")
    args = parser.parse_args()

    asyncio.run(main(args))
