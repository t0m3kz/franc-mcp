#!/usr/bin/env python3
"""Show full ECMP paths between two devices.

Usage:
    uv run scripts/ecmp_path.py <src> <dst> [--branch BRANCH] [--max-hops N]

Examples:
    uv run scripts/ecmp_path.py OFC-BCN-BRANCH-AP-01 DC3-POD4-NTX-04 --branch test
    uv run scripts/ecmp_path.py DC3-BL1 DC3-POD4-NTX-04 --branch main
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Any

sys.path.insert(0, ".")

from scripts._common import call_tool, print_error_and_exit


def _print_paths(src: str, dst: str, data: dict[str, Any]) -> None:
    paths = data.get("paths", [])
    ecmp = data.get("ecmp_available", len(paths))
    print(f"\n{'=' * 72}")
    print(f"  ECMP Paths: {src}  →  {dst}")
    print(f"  Paths found: {ecmp}   Hops (shortest): {len(paths[0]) if paths else 0}")
    print(f"{'=' * 72}")

    for i, path in enumerate(paths, 1):
        print(f"\n  Path {i}/{ecmp}  ({len(path)} hops)")
        print(f"  {'─' * 68}")
        # Header line: just the device chain
        devices = []
        for hop in path:
            if not devices:
                devices.append(hop.get("src_device", "?"))
            devices.append(hop.get("dst_device", "?"))
        print(f"  Route: {' → '.join(devices)}")
        print()
        # Per-hop details
        for j, hop in enumerate(path, 1):
            src_d = hop.get("src_device", "?")
            src_i = hop.get("src_iface", "?")
            dst_d = hop.get("dst_device", "?")
            dst_i = hop.get("dst_iface", "?")
            cable = hop.get("connector", "")
            ctype = hop.get("connector_type", "")
            cable_str = f" [{cable}]" if cable else ""
            ctype_str = f" ({ctype})" if ctype else ""
            print(f"    hop {j:>2}: {src_d}/{src_i}  ──{ctype_str}{cable_str}──▶  {dst_d}/{dst_i}")

    print(f"\n{'=' * 72}\n")


async def main(src: str, dst: str, branch: str, max_hops: int) -> None:
    print(f"Finding all ECMP paths: {src} → {dst}  (branch={branch}, max_hops={max_hops})...")
    result = await call_tool("find_physical_path", src=src, dst=dst, max_hops=max_hops, all_paths=True, branch=branch)

    if result.data is None:
        print_error_and_exit(result)

    _print_paths(src, dst, result.data)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Show full ECMP paths between two devices.")
    parser.add_argument("src", help="Source device name")
    parser.add_argument("dst", help="Destination device name")
    parser.add_argument("--branch", default="main", help="Infrahub branch (default: main)")
    parser.add_argument("--max-hops", type=int, default=20, help="BFS depth limit (default: 20)")
    args = parser.parse_args()

    asyncio.run(main(args.src, args.dst, args.branch, args.max_hops))
