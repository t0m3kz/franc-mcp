#!/usr/bin/env python3
"""Office and branch site queries — summary, devices, WAN circuits.

Usage:
    uv run scripts/offices.py summary [--branch BRANCH]
    uv run scripts/offices.py devices <office> [--branch BRANCH]
    uv run scripts/offices.py circuits [--office OFFICE] [--branch BRANCH]

Examples:
    uv run scripts/offices.py summary --branch test
    uv run scripts/offices.py devices OFC-LON-BRANCH-01 --branch test
    uv run scripts/offices.py circuits --office OFC-LON-BRANCH-01
    uv run scripts/offices.py circuits
"""

from __future__ import annotations

import argparse
import asyncio
import sys

sys.path.insert(0, ".")

from scripts._common import call_tool, decode_gcf_field, print_error_and_exit


async def main(args: argparse.Namespace) -> None:
    if args.command == "summary":
        result = await call_tool("get_office_summary", branch=args.branch)
        if result.data is None:
            print_error_and_exit(result)
        data = result.data
        print(f"=== Office Summary (branch={args.branch}) — {data['count']} sites ===")
        for o in data["offices"]:
            name = o.get("name", "?")
            site_type = o.get("site_type") or o.get("type") or "—"
            city = o.get("city") or "—"
            devs = o.get("device_count", "—")
            circs = o.get("circuit_count", "—")
            print(f"  {name:<30} type={site_type:<12} city={city:<15} devices={devs}  circuits={circs}")

    elif args.command == "devices":
        result = await call_tool("get_office_devices", office=args.office, branch=args.branch)
        if result.data is None:
            print_error_and_exit(result)
        data = result.data
        print(f"=== Devices at {data['office']} (branch={args.branch}) — {data['count']} total ===")
        for d in data["devices"]:
            print(f"  {d['name']:<40} role={d['role'] or '—':<20} status={d['status'] or '—'}")

    elif args.command == "circuits":
        result = await call_tool("get_circuits", scope="office", location=args.office, branch=args.branch)
        if result.data is None:
            print_error_and_exit(result)
        circuits = decode_gcf_field(result.data, "circuits", "circuits_gcf")
        label = args.office or "all offices"
        print(f"=== WAN Circuits ({label}, branch={args.branch}) — {result.data['count']} total ===")
        for c in circuits:
            print(
                f"  {c['name']:<40} type={c['type'] or '—':<15} provider={c['provider'] or '—':<12} "
                f"bw={c['bandwidth_mbps'] or '—':<8} status={c['status'] or '—'}"
            )
            print(f"      locations: {', '.join(c['locations']) or '—'}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Office and branch site queries.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    p_summary = subparsers.add_parser("summary", help="List all office sites")
    p_summary.add_argument("--branch", default="main")

    p_devices = subparsers.add_parser("devices", help="List devices at an office")
    p_devices.add_argument("office", help="Office name (e.g. OFC-LON-BRANCH-01)")
    p_devices.add_argument("--branch", default="main")

    p_circuits = subparsers.add_parser("circuits", help="List WAN circuits for offices")
    p_circuits.add_argument("--office", default=None, help="Filter by office name (default: all)")
    p_circuits.add_argument("--branch", default="main")

    args = parser.parse_args()
    asyncio.run(main(args))
