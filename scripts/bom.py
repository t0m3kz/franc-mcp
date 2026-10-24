#!/usr/bin/env python3
"""Generate a Bill of Materials for a datacenter, office, or colocation site.

Usage:
    uv run scripts/bom.py DC3 --branch test
    uv run scripts/bom.py DC1 --branch main --circuits
    uv run scripts/bom.py --scope office OFC-LON-BRANCH-01 --branch test
    uv run scripts/bom.py --diff --branch test --scope datacenter --name DC3

Examples:
    uv run scripts/bom.py DC3 --branch test
    uv run scripts/bom.py DC3 --branch test --diff
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

sys.path.insert(0, ".")

from scripts._common import call_tool, print_error_and_exit


def _print_bom(data: dict[str, Any], detailed: bool = False) -> None:
    print(f"\n{'=' * 72}")
    print(f"  BOM — scope={data['scope']}  name={data.get('scope_name') or '*'}  branch={data['branch']}")
    print(f"{'=' * 72}")

    phys = data.get("physical_devices", {})
    print(f"\nPhysical Devices  ({phys.get('total', 0)} total)")
    if detailed:
        print(f"  {'#':<4}  {'Name':<30}  {'Role':<15}  {'Device Type':<28}  {'Platform':<20}  {'Status'}")
        print(f"  {'─' * 4}  {'─' * 30}  {'─' * 15}  {'─' * 28}  {'─' * 20}  {'─' * 10}")
        for i, item in enumerate(phys.get("items", []), 1):
            print(
                f"  {i:<4}  {item.get('name', ''):<30}  {item.get('role', ''):<15}  "
                f"{item.get('device_type') or '':<28}  {item.get('platform') or '':<20}  {item.get('status') or ''}"
            )
        print()
    print(f"  {'Device Type':<30}  {'Qty':>4}  {'Unit Price':>12}  {'Line Total':>12}")
    print(f"  {'─' * 30}  {'─' * 4}  {'─' * 12}  {'─' * 12}")
    for line in phys.get("order_lines", []):
        price = f"${line['line_total_usd']:>11,.0f}" if line.get("line_total_usd") is not None else "    no price"
        print(f"  {line['device_type']:<30}  {line['qty']:>4}  ${line.get('unit_price_usd') or 0:>11,.0f}  {price}")
    print(f"  {'─' * 30}  {'─' * 4}  {'─' * 12}  {'─' * 12}")
    print(f"  {'Subtotal':<30}  {'':>4}  {'':>12}  ${phys.get('total_price_usd', 0):>11,.0f}")

    virt = data.get("virtual_devices")
    if virt and virt.get("total", 0) > 0:
        print(f"\nVirtual Devices  ({virt.get('total', 0)} total)")
        if detailed:
            print(f"  {'#':<4}  {'Name':<30}  {'Role':<15}  {'Device Type':<28}  {'Platform':<20}  {'Status'}")
            print(f"  {'─' * 4}  {'─' * 30}  {'─' * 15}  {'─' * 28}  {'─' * 20}  {'─' * 10}")
            for i, item in enumerate(virt.get("items", []), 1):
                print(
                    f"  {i:<4}  {item.get('name', ''):<30}  {item.get('role', ''):<15}  "
                    f"{item.get('device_type') or '':<28}  {item.get('platform') or '':<20}  {item.get('status') or ''}"
                )
            print()
        print(f"  {'Device Type':<30}  {'Qty':>4}  {'Unit Price':>12}  {'Line Total':>12}")
        print(f"  {'─' * 30}  {'─' * 4}  {'─' * 12}  {'─' * 12}")
        for line in virt.get("order_lines", []):
            price = f"${line['line_total_usd']:>11,.0f}" if line.get("line_total_usd") is not None else "    no price"
            print(f"  {line['device_type']:<30}  {line['qty']:>4}  ${line.get('unit_price_usd') or 0:>11,.0f}  {price}")
        print(f"  {'─' * 30}  {'─' * 4}  {'─' * 12}  {'─' * 12}")
        print(f"  {'Subtotal':<30}  {'':>4}  {'':>12}  ${virt.get('total_price_usd', 0):>11,.0f}")

    cables = data.get("cables")
    if cables:
        print(f"\nCables  ({cables.get('total', 0)} total)")
        print(f"  {'Cable Type':<30}  {'Qty':>4}  {'Unit Price':>12}  {'Line Total':>12}")
        print(f"  {'─' * 30}  {'─' * 4}  {'─' * 12}  {'─' * 12}")
        for line in cables.get("order_lines", []):
            price = f"${line['line_total_usd']:>11,.0f}" if line.get("line_total_usd") is not None else "    no price"
            print(f"  {line['cable_type']:<30}  {line['qty']:>4}  ${line.get('unit_price_usd') or 0:>11,.0f}  {price}")
        print(f"  {'─' * 30}  {'─' * 4}  {'─' * 12}  {'─' * 12}")
        print(f"  {'Subtotal':<30}  {'':>4}  {'':>12}  ${cables.get('total_price_usd', 0):>11,.0f}")

    sfps = data.get("sfps")
    if sfps and sfps.get("total", 0) > 0:
        print(f"\nSFPs  ({sfps.get('total', 0)} total — 2 per optical cable)")
        print(f"  {'SFP Type':<30}  {'Qty':>4}  {'Unit Price':>12}  {'Line Total':>12}")
        print(f"  {'─' * 30}  {'─' * 4}  {'─' * 12}  {'─' * 12}")
        for line in sfps.get("order_lines", []):
            price = f"${line['line_total_usd']:>11,.0f}" if line.get("line_total_usd") is not None else "    no price"
            print(f"  {line['sfp_type']:<30}  {line['qty']:>4}  ${line.get('unit_price_usd') or 0:>11,.0f}  {price}")
        print(f"  {'─' * 30}  {'─' * 4}  {'─' * 12}  {'─' * 12}")
        print(f"  {'Subtotal':<30}  {'':>4}  {'':>12}  ${sfps.get('total_price_usd', 0):>11,.0f}")

    circuits = data.get("circuits")
    if circuits and circuits.get("total", 0) > 0:
        print(f"\nCircuits  ({circuits.get('total', 0)} total)")
        print(f"  {'Name':<30}  {'Type':<12}  {'Provider':<15}  {'BW (Mbps)':>10}  {'Status'}")
        print(f"  {'─' * 30}  {'─' * 12}  {'─' * 15}  {'─' * 10}  {'─' * 10}")
        for item in circuits.get("items", []):
            print(
                f"  {item.get('name', ''):<30}  {item.get('circuit_type', ''):<12}  "
                f"{item.get('provider', ''):<15}  {item.get('bandwidth_mbps') or '':>10}  {item.get('status', '')}"
            )

    print(f"\n{'─' * 72}")
    print(f"  GRAND TOTAL                                              ${data.get('grand_total_usd', 0):>11,.0f}")
    print(f"{'=' * 72}\n")


def _print_diff(data: dict[str, Any]) -> None:
    print(f"\n{'=' * 60}")
    print(f"  BOM DIFF — {data['base_branch']} → {data['branch']}")
    print(f"  scope={data['scope']}  name={data.get('scope_name') or '*'}")
    print(f"  total changes: {data['total_changes']}  price delta: ${data['grand_total_delta_usd']:+,.0f}")
    print(f"{'=' * 60}")

    for section, label in [
        ("physical_devices", "Physical Devices"),
        ("virtual_devices", "Virtual Devices"),
        ("cables", "Cables"),
    ]:
        diff = data.get(section)
        if not diff:
            continue
        s = diff.get("summary", {})
        print(
            f"\n{label}: +{s.get('added', 0)} added  -{s.get('removed', 0)} removed  ~{s.get('changed', 0)} changed  ={s.get('unchanged', 0)} unchanged"
        )
        for item in diff.get("added", []):
            print(f"  + {item.get('name')}")
        for item in diff.get("removed", []):
            print(f"  - {item.get('name')}")
        for item in diff.get("changed", []):
            print(f"  ~ {item.get('name')}  {item.get('changes')}")
        delta = diff.get("price_delta_usd")
        if delta is not None:
            print(f"  Price delta: ${delta:+,.0f}")

    sfps = data.get("sfps")
    if sfps and sfps.get("order_lines"):
        print("\nSFP delta:")
        for line in sfps["order_lines"]:
            print(
                f"  {line['sfp_type']:<20}  +{line['added']} / -{line['removed']}  net={line['net_qty']:+}  delta=${line.get('line_delta_usd') or 0:+,.0f}"
            )
        print(f"  SFP price delta: ${sfps['price_delta_usd']:+,.0f}")

    print(f"\n{'─' * 60}")
    print(f"  GRAND TOTAL DELTA                              ${data['grand_total_delta_usd']:>+10,.0f}")
    print(f"{'=' * 60}\n")


async def main(
    scope_name: str | None,
    scope: str,
    branch: str,
    base_branch: str,
    include_circuits: bool,
    diff_mode: bool,
    json_out: bool,
    detailed: bool = False,
) -> None:
    if diff_mode:
        print(f"Diffing BOM for {scope_name or scope} ({base_branch} → {branch})...")
        result = await call_tool(
            "get_bom_diff",
            branch=branch,
            base_branch=base_branch,
            scope=scope,
            scope_name=scope_name,
            include_circuits=include_circuits,
        )
    else:
        print(f"Fetching BOM for {scope_name or scope} (branch={branch})...")
        result = await call_tool(
            "get_bill_of_materials",
            scope=scope,
            scope_name=scope_name,
            include_circuits=include_circuits,
            branch=branch,
        )

    if result.data is None:
        print_error_and_exit(result)

    if json_out:
        print(json.dumps(result.data, indent=2))
    elif diff_mode:
        _print_diff(result.data)
    else:
        _print_bom(result.data, detailed=detailed)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate Bill of Materials for a site.")
    parser.add_argument("name", nargs="?", help="Site name filter, e.g. DC3, OFC-LON-BRANCH-01")
    parser.add_argument(
        "--scope", default=None, help="Scope: datacenter, office, colocation, all (auto-detected from name if omitted)"
    )
    parser.add_argument("--branch", default="main", help="Branch to read from (default: main)")
    parser.add_argument("--base-branch", default="main", help="Base branch for diff (default: main)")
    parser.add_argument("--circuits", action="store_true", help="Include physical circuits")
    parser.add_argument("--diff", action="store_true", help="Show differential BOM vs base branch")
    parser.add_argument("--json", action="store_true", help="Output raw JSON")
    parser.add_argument("--detailed", action="store_true", help="Print individual device list under each section")
    args = parser.parse_args()

    # Auto-detect scope from name prefix
    scope = args.scope
    if scope is None and args.name:
        up = args.name.upper()
        if up.startswith("DC"):
            scope = "datacenter"
        elif up.startswith("OFC"):
            scope = "office"
        else:
            scope = "colocation"
    elif scope is None:
        scope = "all"

    asyncio.run(
        main(
            scope_name=args.name,
            scope=scope,
            branch=args.branch,
            base_branch=args.base_branch,
            include_circuits=args.circuits,
            diff_mode=args.diff,
            json_out=args.json,
            detailed=args.detailed,
        )
    )
