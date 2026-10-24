#!/usr/bin/env python3
"""Colocation facility queries — summary, devices, physical circuits.

Usage:
    uv run scripts/colocation.py summary [--branch BRANCH]
    uv run scripts/colocation.py devices [--zone ZONE] [--branch BRANCH]
    uv run scripts/colocation.py circuits [--location LOCATION] [--type TYPE] [--provider PROVIDER] [--branch BRANCH]

Examples:
    uv run scripts/colocation.py summary --branch test
    uv run scripts/colocation.py devices --zone FR2 --branch test
    uv run scripts/colocation.py circuits --location DC1 --branch test
    uv run scripts/colocation.py circuits --type cross_connect --provider Equinix
"""

from __future__ import annotations

import argparse
import asyncio
import sys

sys.path.insert(0, ".")

from scripts._common import call_tool, decode_gcf_field, print_error_and_exit


async def main(args: argparse.Namespace) -> None:
    if args.command == "summary":
        result = await call_tool("get_colocation_summary", branch=args.branch)
        if result.data is None:
            print_error_and_exit(result)
        data = result.data
        print(f"=== Colocation Summary (branch={args.branch}) ===")
        print(f"  Providers : {data['provider_count']}")
        for p in data["providers"]:
            print(f"    {p['name']}")
        print(f"  Metros    : {data['metro_count']}")
        for m in data["metros"]:
            print(f"    {m['name']:<20} provider={m['provider'] or '—'}")
        print(f"  Zones     : {data['zone_count']}")
        for z in data["zones"]:
            print(f"    {z['name']:<20} metro={z['metro'] or '—'}")

    elif args.command == "devices":
        result = await call_tool("get_colocation_devices", zone=args.zone, branch=args.branch)
        if result.data is None:
            print_error_and_exit(result)
        data = result.data
        print(f"=== Colocation Devices (zone={args.zone or 'all'}, branch={args.branch}) — {data['count']} total ===")
        for d in data["devices"]:
            print(
                f"  {d['name']:<40} role={d['role'] or '—':<20} deployment={d['deployment'] or '—':<15} status={d['status'] or '—'}"
            )

    elif args.command == "circuits":
        result = await call_tool(
            "get_circuits",
            scope="all",
            location=args.location,
            circuit_type=args.type,
            provider=args.provider,
            branch=args.branch,
        )
        if result.data is None:
            print_error_and_exit(result)
        circuits = decode_gcf_field(result.data, "circuits", "circuits_gcf")
        print(f"=== Physical Circuits (branch={args.branch}) — {result.data['count']} total ===")
        for c in circuits:
            locs = ", ".join(str(l) for l in (c["locations"] or [])) if c["locations"] else "—"
            print(
                f"  {c['name']:<40} type={c['type'] or '—':<15} provider={c['provider'] or '—':<12} "
                f"bw={c['bandwidth_mbps'] or '—':<8} status={c['status'] or '—'}"
            )
            print(f"      locations: {locs}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Colocation facility queries.")
    parser.add_argument("command", choices=["summary", "devices", "circuits"])
    parser.add_argument("--branch", default="main", help="Infrahub branch (default: main)")
    parser.add_argument("--zone", default=None, help="Filter devices by zone (e.g. FR2, AM1)")
    parser.add_argument("--location", default=None, help="Filter circuits by location (e.g. DC1, FR2)")
    parser.add_argument("--type", default=None, help="Filter circuits by type (e.g. cross_connect, mpls)")
    parser.add_argument("--provider", default=None, help="Filter circuits by provider name")
    args = parser.parse_args()

    asyncio.run(main(args))
