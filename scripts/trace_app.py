#!/usr/bin/env python3
"""Trace network paths between two applications.

Usage:
    uv run scripts/trace_app.py <src_app> <dst_app> [--max-hops N] [--ecmp] [--hops]

Examples:
    uv run scripts/trace_app.py trade-portal fleet-manager
    uv run scripts/trace_app.py trade-portal fleet-manager --ecmp
    uv run scripts/trace_app.py trade-portal fleet-manager --hops
"""

from __future__ import annotations

import argparse
import asyncio
import sys

sys.path.insert(0, ".")

from scripts._common import call_tool

_STATUS_ICON = {"ok": "✓", "broken": "✗", "warning": "⚠", "unreachable": "—", "co_located": "≡"}


def _col_widths(rows: list[list[str]]) -> list[int]:
    if not rows:
        return []
    return [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]


def _print_table(headers: list[str], rows: list[list[str]]) -> None:
    all_rows = [headers] + rows
    widths = _col_widths(all_rows)
    sep = "+-" + "-+-".join("-" * w for w in widths) + "-+"
    fmt = "| " + " | ".join(f"{{:<{w}}}" for w in widths) + " |"
    print(sep)
    print(fmt.format(*headers))
    print(sep)
    for row in rows:
        print(fmt.format(*row))
    print(sep)


def _fmt_hop(hop: dict, indent: str = "        ") -> str:
    if hop.get("fw_policy"):
        fw = hop.get("firewall", "?")
        return f"{indent}🔥 [PBR] traffic steered through firewall: {fw}"
    src = f"{hop.get('src_device', '?')}/{hop.get('src_iface', '?')}"
    dst = f"{hop.get('dst_device', '?')}/{hop.get('dst_iface', '?')}"
    ctype = hop.get("connector_type", "")
    cable = hop.get("connector", "")
    cable_str = f" ({cable})" if cable else ""
    health = hop.get("hop_broken") and "✗" or hop.get("hop_warning") and "⚠" or "✓"
    src_st = hop.get("src_iface_status", "")
    dst_st = hop.get("dst_iface_status", "")
    status_str = f"  [{src_st}→{dst_st}]" if src_st or dst_st else ""
    return f"{indent}{health} {src}  ──{ctype}{cable_str}──▶  {dst}{status_str}"


def _dedup_paths(paths: list[list[dict]]) -> list[list[dict]]:
    """Remove duplicate paths (same sequence of cables)."""
    seen: set[tuple] = set()
    unique: list[list[dict]] = []
    for path in paths:
        key = tuple(h.get("connector", "") or f"{h.get('src_device')}→{h.get('dst_device')}" for h in path)
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def _path_to_str(path: list[dict]) -> str:
    parts = []
    for hop in path:
        if hop.get("fw_policy"):
            parts.append(f"─[FW:{hop.get('firewall', '?')}]─")
            continue
        if not parts:
            parts.append(hop.get("src_device", "?"))
        cable = hop.get("connector", "")
        parts.append(f"=[{cable}]=" if cable else "→")
        parts.append(hop.get("dst_device", "?"))
    return " ".join(parts)


def _print_result(data: dict, show_ecmp: bool, show_hops: bool) -> None:
    headers = ["#", "src_server", "dst_server", "firewall", "path", "hops", "ecmp", "status"]
    rows: list[list[str]] = []
    n = 0
    seen_pairs: set[tuple[str, str]] = set()

    for cp in data.get("component_pairs", []):
        for sp in cp["server_pairs"]:
            key = (sp["src_server"], sp["dst_server"])
            if key in seen_pairs:
                continue
            seen_pairs.add(key)
            n += 1
            status = sp.get("path_status", "?")
            icon = _STATUS_ICON.get(status, status)
            all_paths = _dedup_paths(sp.get("paths") or [])
            ecmp = len(all_paths)
            hops = sp.get("shortest_hops", 0) or (len(all_paths[0]) if all_paths else 0)
            display_paths = all_paths if show_ecmp else all_paths[:1]

            fw_col = sp.get("firewall") or ("—" if not sp.get("pbr_enabled") else "pbr/no-fw")

            if not display_paths:
                rows.append(
                    [
                        str(n),
                        sp["src_server"],
                        sp["dst_server"],
                        fw_col,
                        "—",
                        str(hops) if hops else "—",
                        "—",
                        f"{icon} {status}",
                    ]
                )
            else:
                for i, path in enumerate(display_paths):
                    path_str = _path_to_str(path)
                    path_label = f"[{i + 1}/{ecmp}] {path_str}" if show_ecmp and ecmp > 1 else path_str
                    ecmp_str = str(ecmp)
                    rows.append(
                        [
                            str(n) if i == 0 else "",
                            sp["src_server"] if i == 0 else "",
                            sp["dst_server"] if i == 0 else "",
                            fw_col if i == 0 else "",
                            path_label,
                            str(len(path)) if i == 0 else "",
                            ecmp_str if i == 0 else "",
                            (f"{icon} {status}" if i == 0 else ""),
                        ]
                    )

    _print_table(headers, rows)

    if show_hops:
        print()
        seen_hops: set[tuple[str, str]] = set()
        for cp in data.get("component_pairs", []):
            for sp in cp["server_pairs"]:
                key = (sp["src_server"], sp["dst_server"])
                if key in seen_hops:
                    continue
                seen_hops.add(key)
                all_paths = _dedup_paths(sp.get("paths") or [])
                if not all_paths:
                    continue
                display_paths = all_paths if show_ecmp else all_paths[:1]
                print(f"  {sp['src_server']} → {sp['dst_server']}")
                for i, path in enumerate(display_paths):
                    if len(display_paths) > 1:
                        print(f"    path {i + 1}:")
                    for hop in path:
                        print(_fmt_hop(hop))


async def main(src: str, dst: str, max_hops: int, show_ecmp: bool, show_hops: bool, branch: str) -> None:
    print(f"\n=== {src} → {dst} (branch={branch}) ===")
    result = await call_tool("trace_app_to_app", src_app=src, dst_app=dst, max_hops=max_hops, branch=branch)

    data = result.data
    if data is None:
        print(f"  ERROR: {result.error}")
        if result.remediation:
            print(f"  HINT:  {result.remediation}")
        return

    summary = data.get("summary", {})
    print(
        f"  pairs={summary.get('total_server_pairs', 0)}"
        f"  routed={summary.get('routed_pairs', 0)}"
        f"  unreachable={summary.get('unreachable_pairs', 0)}"
        f"  co-located={summary.get('co_located_pairs', 0)}"
        f"  health={summary.get('overall_health', '?')}"
    )
    print()
    _print_result(data, show_ecmp=show_ecmp, show_hops=show_hops)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Trace network paths between applications.")
    parser.add_argument("src_app", help="Source application name or label")
    parser.add_argument("dst_app", help="Destination application name or label")
    parser.add_argument("--branch", default="main", help="Infrahub branch (default: main)")
    parser.add_argument("--max-hops", type=int, default=15, help="BFS depth limit (default: 15)")
    parser.add_argument(
        "--ecmp", action="store_true", help="Show all ECMP paths (default: one representative per pair)"
    )
    parser.add_argument("--hops", action="store_true", help="Show per-hop details with cable names below the table")
    args = parser.parse_args()

    asyncio.run(main(args.src_app, args.dst_app, args.max_hops, args.ecmp, args.hops, args.branch))
