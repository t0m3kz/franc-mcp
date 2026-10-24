#!/usr/bin/env python3
"""Show segment details: owner, environment, PBR state.

Usage:
    uv run scripts/trace_segment.py <segment_name> [--branch BRANCH]

Example:
    uv run scripts/trace_segment.py c005-app-backend-stretch-p --branch test
    uv run scripts/trace_segment.py c005-web-frontend-local-dc10-p --branch test

Note: HA-group/active-firewall-selection reporting (present in an earlier version of this
script) relied on a traverser that no longer exists in franc.traversal. This script now
reports only what get_segment_inventory (franc/tools/topology.py) exposes today.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

sys.path.insert(0, ".")

from scripts._common import call_tool, decode_gcf_field, print_error_and_exit


async def main(segment: str, branch: str) -> None:
    # owner is stored as a display label (e.g. "Drentec BV (C005)"), not the bare customer_id
    # used elsewhere, so filtering server-side by customer_id doesn't match — fetch everything
    # and match client-side by customer_name instead.
    result = await call_tool("get_segment_inventory", include_vlan=True, branch=branch)
    if result.data is None:
        print_error_and_exit(result)

    entries = decode_gcf_field(result.data, "segments", "segments_gcf")
    # Full segment name is "<customer_id>-<customer_name>-<environment>"; owner is a display
    # label ("Drentec BV (C005)"), so match on customer_name + environment instead.
    match = next(
        (
            e
            for e in entries
            if str(e.get("customer_name")) in segment and str(e.get("environment", "")) == segment.rsplit("-", 1)[-1]
        ),
        None,
    )
    if match is None:
        print(f"ERROR: Segment matching '{segment}' not found (branch={branch}).")
        print("HINT:  Use get_segment_inventory() to list available segments.")
        sys.exit(1)

    print(f"Segment name (query): {segment}")
    print(f"Customer name:        {match.get('customer_name', '—')}")
    print(f"Owner:                {match.get('owner', '—')}")
    print(f"Environment:          {match.get('environment', '—')}")
    print(f"PBR enabled:          {match.get('pbr_enabled', '—')}")
    print(f"Type:                 {match.get('type', '—')}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Show segment details.")
    parser.add_argument("segment", help="Segment name (e.g. c005-app-backend-stretch-p)")
    parser.add_argument("--branch", default="main", help="Infrahub branch (default: main)")
    args = parser.parse_args()
    asyncio.run(main(args.segment, args.branch))
