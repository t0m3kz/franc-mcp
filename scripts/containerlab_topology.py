#!/usr/bin/env python3
"""Generate a containerlab topology YAML for the path between two devices.

Usage:
    uv run scripts/containerlab_topology.py <source> <target> [--branch BRANCH] [--out PATH]

Examples:
    uv run scripts/containerlab_topology.py DC1-BL1 DC1-POD1-SRV-01 --branch test
    uv run scripts/containerlab_topology.py DC1-BL1 DC1-POD2-SP1 --branch main --out my-lab.yaml
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, ".")

from scripts._common import call_tool, print_error_and_exit


async def main(source: str, target: str, branch: str, out_path: str | None) -> None:
    print(f"Finding path: {source} → {target} (branch={branch})...")
    result = await call_tool("generate_containerlab_topology", source=source, target=target, branch=branch)

    if result.data is None:
        print_error_and_exit(result)

    data = result.data
    print(f"  paths={data['paths']}  nodes={data['nodes']}  links={data['links']}")

    slug = f"{source.lower()}_to_{target.lower()}".replace("-", "_")
    save_path = Path(out_path) if out_path else Path.cwd() / f"{slug}_clab.yaml"
    save_path.write_text(data["containerlab"], encoding="utf-8")
    print(f"Saved: {save_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate containerlab topology between two devices.")
    parser.add_argument("source", help="Source device name (e.g. DC1-BL1)")
    parser.add_argument("target", help="Target device name (e.g. DC1-POD1-SRV-01)")
    parser.add_argument("--branch", default="main", help="Infrahub branch (default: main)")
    parser.add_argument("--out", default=None, help="Output file path")
    args = parser.parse_args()

    asyncio.run(main(args.source, args.target, args.branch, args.out))
